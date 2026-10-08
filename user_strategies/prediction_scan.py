"""Scan production prediction markets into Redis without signing or placing orders."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP, localcontext
from itertools import combinations
from pathlib import Path

import httpx
from redis.asyncio import Redis

from user_strategies.prediction_markets import Adapter, VENUES, dumps, now_ms

LOG = logging.getLogger(__name__)
PREFIX = "prediction:v1"


def identity(row: dict) -> str:
    return f"{row['venue']}:{row['market_id']}"


def fingerprint(row: dict) -> str:
    """Bind reviewed equivalence to the exact rules and outcome orientation."""
    fields = {k: row.get(k) for k in ("rules", "yes_label", "no_label", "close_time", "quote_currency")}
    return hashlib.sha256(dumps(fields).encode()).hexdigest()


def title_key(row: dict) -> tuple[str, str]:
    # Preserve numbers, signs and dates; removing punctuation can change a strike.
    title = " ".join(row["title"].casefold().split()).rstrip("?")
    label = " ".join(row["yes_label"].casefold().split())
    # Kalshi may repeat the affirmative outcome already named in the question;
    # Polymarket labels the same proposition simply Yes.
    if label in ("yes", "") or label in title:
        label = ""
    return title, label


def load_matches(path: str | None) -> list[dict]:
    if not path:
        return []
    with open(path, encoding="utf-8") as stream:
        groups = json.load(stream)
    if not isinstance(groups, list):
        raise ValueError("Match file must contain a list")
    used = set()
    for group in groups:
        if not group.get("id") or len(group.get("members", [])) < 2:
            raise ValueError("Each reviewed match needs an id and at least two members")
        for member in group["members"]:
            key = identity(member)
            if member["venue"] not in VENUES or key in used or len(member.get("fingerprint", "")) != 64:
                raise ValueError("Invalid or duplicate reviewed match member")
            if not isinstance(member.get("invert", False), bool):
                raise ValueError("invert must be a boolean")
            used.add(key)
    return groups


def fee_for(row: dict, price: Decimal, size: Decimal) -> Decimal | None:
    fee = row.get("fee")
    if fee is None:
        return None
    if fee["model"] == "zero":
        return Decimal(0)
    if fee["model"] != "quadratic":
        return None
    amount = size * Decimal(fee["rate"]) * price * (1 - price)
    if row["venue"] == "kalshi":
        # General schedule only; series/event overrides are not verified.
        return amount.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return amount.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)


def fill(row: dict, side: str, quantity: Decimal) -> dict | None:
    if quantity < Decimal(row.get("minimum_shares", "0")):
        return None
    remaining, cost, fees = quantity, Decimal(0), Decimal(0)
    for price, size in row.get("books", {}).get(side, {}).get("asks", []):
        price, size = Decimal(price), min(Decimal(size), remaining)
        cost += price * size
        charge = fee_for(row, price, size)
        fees = fees + charge if fees is not None and charge is not None else None
        remaining -= size
        if remaining == 0:
            if cost < Decimal(row.get("minimum_notional", "0")):
                return None
            return {"venue": row["venue"], "market_id": row["market_id"], "side": side,
                    "quantity": str(quantity), "cost": str(cost),
                    "average_price": str(cost / quantity),
                    "fees": str(fees) if fees is not None else None,
                    "fee_estimated": bool(row.get("fee", {}).get("estimated")) if row.get("fee") else None,
                    "quote_at_ms": row["quote_at_ms"]}
    return None


def fresh(row: dict, at_ms: int, max_age_ms: int) -> bool:
    received = row.get("quote_at_ms")
    if not received or not 0 <= at_ms - received <= max_age_ms:
        return False
    # Source timestamps are retained for inspection. Some venues timestamp the
    # last book change instead of the response; age is based on successful reads.
    if not row.get("books"):
        return False
    for data in row["books"].values():
        received = data.get("received_at_ms", row["quote_at_ms"])
        if not 0 <= at_ms - received <= max_age_ms:
            return False
    return market_open(row, at_ms)


def market_open(row: dict, at_ms: int) -> bool:
    close = row.get("close_time")
    if close:
        if isinstance(close, (int, float)) or str(close).isdigit():
            close_at = int(close) * 1000
        else:
            date = datetime.fromisoformat(str(close).replace("Z", "+00:00"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            close_at = int(date.timestamp() * 1000)
        if at_ms >= close_at:
            return False
    return True


def matching_pairs(lookup: dict, matches: list[dict]) -> tuple[dict, list]:
    pairs = {}
    indexed = defaultdict(list)
    for key, row in lookup.items():
        if row["title"].strip():
            indexed[title_key(row)].append(key)
        for ref in row.get("references", []):
            other = identity(ref)
            if other in lookup and ref["venue"] != row["venue"]:
                pairs[tuple(sorted((key, other)))] = ("platform_reference", False, {}, False)
    for keys in indexed.values():
        for a, b in combinations(keys, 2):
            if lookup[a]["venue"] != lookup[b]["venue"]:
                pairs.setdefault(tuple(sorted((a, b))), ("exact_title", False, {}, False))
    invalid_matches = []
    for group in matches:
        members = {identity(member): member for member in group["members"]}
        valid = [key for key, member in members.items()
                 if key in lookup and fingerprint(lookup[key]) == member["fingerprint"]]
        for key in members:
            if key in lookup and key not in valid:
                invalid_matches.append({"group": group["id"], "market": key, "reason": "fingerprint_changed"})
        for a, b in combinations(valid, 2):
            if lookup[a]["venue"] != lookup[b]["venue"]:
                pairs[tuple(sorted((a, b)))] = (group["id"], True, members, group.get("usd_parity") is True)
    return pairs, invalid_matches


def quote_targets(catalogs: dict, matches: list[dict], at_ms: int) -> dict[str, set]:
    lookup = {identity(row): row for catalog in catalogs.values() for row in catalog.values()
              if market_open(row, at_ms)}
    pairs, _ = matching_pairs(lookup, matches)
    targets = {venue: set() for venue in VENUES}
    for pair in pairs:
        for key in pair:
            row = lookup[key]
            targets[row["venue"]].add(row["market_id"])
    return targets


def scan(rows: list[dict], matches: list[dict], quantity: Decimal,
         at_ms: int, max_age_ms: int, quote_mode: str = "depth") -> dict:
    lookup = {identity(row): row for row in rows if fresh(row, at_ms, max_age_ms)}
    pairs, invalid_matches = matching_pairs(lookup, matches)
    candidates, opportunities = [], []
    with localcontext() as context:
        context.prec = 64
        for (a, b), (method, verified, members, parity) in pairs.items():
            for yes_key, no_key in ((a, b), (b, a)):
                yes_row, no_row = lookup[yes_key], lookup[no_key]
                yes_side = "no" if members.get(yes_key, {}).get("invert") else "yes"
                no_side = "yes" if members.get(no_key, {}).get("invert") else "no"
                pair_quantity = quantity
                available_quantity = None
                if quote_mode == "top":
                    yes_asks = yes_row.get("books", {}).get(yes_side, {}).get("asks", [])
                    no_asks = no_row.get("books", {}).get(no_side, {}).get("asks", [])
                    if not yes_asks or not no_asks:
                        continue
                    available_quantity = min(Decimal(yes_asks[0][1]), Decimal(no_asks[0][1]))
                    pair_quantity = min(quantity, available_quantity)
                    if pair_quantity <= 0:
                        continue
                yes_fill, no_fill = fill(yes_row, yes_side, pair_quantity), fill(no_row, no_side, pair_quantity)
                if yes_fill is None or no_fill is None:
                    continue
                gross = pair_quantity - Decimal(yes_fill["cost"]) - Decimal(no_fill["cost"])
                if gross <= 0:
                    continue
                currencies = [yes_row["quote_currency"], no_row["quote_currency"]]
                comparable = (currencies[0] in ("USD", "USDC", "USDT") and currencies[0] == currencies[1]) or (
                    parity and all(c in ("USD", "USDC", "USDT") for c in currencies))
                known_fee = yes_fill["fees"] is not None and no_fill["fees"] is not None
                net = gross - Decimal(yes_fill["fees"]) - Decimal(no_fill["fees"]) if known_fee and comparable else None
                reasons = []
                if not verified:
                    reasons.append("settlement_equivalence_unverified")
                if not comparable:
                    reasons.append("collateral_parity_unverified")
                if not known_fee:
                    reasons.append("fees_unknown")
                if yes_fill["fee_estimated"] or no_fill["fee_estimated"]:
                    reasons.append("fees_estimated")
                candidate = {"id": f"{yes_key}|{no_key}", "title": yes_row["title"],
                             "match_method": method, "match_verified": verified,
                             "quantity": str(pair_quantity), "available_quantity": str(available_quantity) if available_quantity is not None else None,
                             "quote_mode": quote_mode, "yes_leg": yes_fill, "no_leg": no_fill,
                             "quote_currencies": currencies, "gross_spread": str(gross / pair_quantity),
                             "gross_profit": str(gross),
                             "net_profit": str(net) if net is not None else None,
                             "net_profit_per_share": str(net / pair_quantity) if net is not None else None,
                             "review_reasons": reasons}
                candidates.append(candidate)
                if not reasons and net is not None and net > 0:
                    opportunities.append(candidate)
    candidates.sort(key=lambda x: Decimal(x["gross_profit"]), reverse=True)
    opportunities.sort(key=lambda x: Decimal(x["net_profit"]), reverse=True)
    return {"schema_version": 1, "scanned_at_ms": at_ms, "max_quote_age_ms": max_age_ms,
            "quantity": str(quantity), "market_count": len(rows), "fresh_market_count": len(lookup),
            "matched_pairs": len(pairs), "candidates": candidates, "opportunities": opportunities,
            "invalid_matches": invalid_matches}


async def publish_status(redis: Redis, venue: str, ttl: int, **fields: object) -> None:
    await redis.set(f"{PREFIX}:{venue}:status", dumps({
        "schema_version": 1, "venue": venue, "environment": "production",
        "updated_at_ms": now_ms(),
        "trading_availability": "unverified" if venue == "hyperliquid" else "not_checked",
        **fields,
    }), ex=ttl)


def error_code(error: Exception) -> str:
    # Do not publish response bodies, request headers or local credentials.
    if isinstance(error, httpx.HTTPStatusError):
        return f"http_{error.response.status_code}"
    if isinstance(error, PermissionError):
        return "missing_api_key"
    if isinstance(error, KeyError) and error.args:
        return f"missing_field:{str(error.args[0])[:64]}"
    return type(error).__name__


async def collect_venue(adapter: Adapter, redis: Redis, args: argparse.Namespace,
                        catalogs: dict | None = None, matches: list | None = None) -> None:
    venue = adapter.venue
    key = f"{PREFIX}:{venue}:markets"
    catalogs = {} if catalogs is None else catalogs
    matches = matches or []
    cached = catalogs.get(venue, {})
    complete, discovered = bool(cached) and args.max_markets == 0, time.monotonic() if cached else 0.0
    discovery_progress = {"processed": 0, "target_count": 0, "quote_failures": 0}

    async def publish_page(rows: dict, count: int) -> None:
        for row in rows.values():
            row["fingerprint"] = fingerprint(row)
            previous = catalogs.get(venue, {}).get(row["market_id"])
            if previous and previous.get("fingerprint") == row["fingerprint"]:
                for field in ("books", "quote_at_ms", "quote_error"):
                    if field in previous:
                        row[field] = previous[field]
        catalogs.setdefault(venue, {}).update(rows)
        if rows:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.hset(key, mapping={id_: dumps(row) for id_, row in rows.items()})
                pipe.expire(key, args.ttl)
                await pipe.execute()
        await publish_status(redis, venue, args.ttl, state="discovering", market_count=count,
                             **discovery_progress, coverage_complete=False)

    async def write_quotes(items: list, state: str, coverage: bool) -> tuple[int, int]:
        failures, processed = 0, 0
        for offset in range(0, len(items), 20):
            results = await adapter.quote_batch(items[offset:offset + 20])
            updates = {}
            for row, result in zip(items[offset:offset + 20], results, strict=True):
                processed += 1
                if isinstance(result, Exception):
                    failures += 1
                    result = {**row, "books": {}, "quote_at_ms": None, "quote_error": error_code(result)}
                catalogs.get(venue, {})[row["market_id"]] = result
                updates[row["market_id"]] = dumps(result)
            async with redis.pipeline(transaction=True) as pipe:
                pipe.hset(key, mapping=updates)
                pipe.expire(key, args.ttl)
                await pipe.execute()
            if state == "discovering":
                discovery_progress.update(processed=processed, target_count=len(items), quote_failures=failures)
            await publish_status(redis, venue, args.ttl, state=state, market_count=len(catalogs.get(venue, {})),
                                 processed=processed, target_count=len(items), quote_failures=failures, coverage_complete=coverage)
        return processed, failures

    async def quote_while_discovering() -> None:
        while True:
            selected = quote_targets(catalogs, matches, now_ms())[venue]
            items = [row for id_, row in catalogs.get(venue, {}).items() if id_ in selected]
            if items:
                await write_quotes(items, "discovering", False)
            await asyncio.sleep(args.interval)

    while True:
        started = time.monotonic()
        try:
            if not cached or time.monotonic() - discovered >= args.discovery_interval:
                await publish_status(redis, venue, args.ttl, state="discovering", coverage_complete=False)
                stream = (asyncio.create_task(quote_while_discovering())
                          if getattr(args, "book_scope", "all") == "matched" and not args.once else None)
                try:
                    cached, complete = await adapter.discover(args.max_markets, on_page=publish_page)
                finally:
                    if stream:
                        if stream.done() and not stream.cancelled():
                            stream.result()
                        stream.cancel()
                        await asyncio.gather(stream, return_exceptions=True)
                for id_, row in cached.items():
                    previous = catalogs.get(venue, {}).get(id_)
                    if previous and fingerprint(previous) == fingerprint(row):
                        for field in ("books", "quote_at_ms", "quote_error"):
                            if field in previous:
                                row[field] = previous[field]
                catalogs[venue] = cached
                discovered = time.monotonic()
                for row in cached.values():
                    row["fingerprint"] = fingerprint(row)
                existing = set(await redis.hkeys(key))
                async with redis.pipeline(transaction=True) as pipe:
                    if cached:
                        pipe.hset(key, mapping={id_: dumps(row) for id_, row in cached.items()})
                        pipe.expire(key, args.ttl)
                    removed = existing - cached.keys()
                    if removed:
                        pipe.hdel(key, *removed)
                    await pipe.execute()
            failures, processed = 0, 0
            if getattr(args, "discovery_only", False):
                await publish_status(redis, venue, args.ttl, state="ok" if complete else "partial",
                                     market_count=len(cached), processed=0, target_count=0, coverage_complete=complete)
                return
            if getattr(args, "book_scope", "all") == "matched":
                selected = quote_targets(catalogs, matches, now_ms())[venue]
                items = [row for id_, row in cached.items() if id_ in selected]
            else:
                items = list(cached.values())
            processed, failures = await write_quotes(items, "collecting", complete)
            await redis.expire(key, args.ttl)
            await publish_status(redis, venue, args.ttl, state="partial" if failures or not complete else "ok",
                                 market_count=len(cached), processed=processed, target_count=len(items), quote_failures=failures,
                                 coverage_complete=complete, cycle_seconds=str(time.monotonic() - started))
            LOG.info("%s: %d catalog markets, %d target books, %d failures", venue, len(cached), len(items), failures)
        except Exception as error:
            code = error_code(error)
            catalogs.pop(venue, None)
            await publish_status(redis, venue, args.ttl, state="unavailable" if code == "missing_api_key" else "error",
                                 error=code, coverage_complete=False)
            LOG.warning("%s collection failed: %s", venue, code)
        if args.once:
            return
        await asyncio.sleep(max(1, args.interval - (time.monotonic() - started)))


async def scan_once(redis: Redis, args: argparse.Namespace, matches: list[dict], targets: dict | None = None) -> dict:
    rows, statuses = [], {}
    for venue in VENUES:
        status = await redis.get(f"{PREFIX}:{venue}:status")
        statuses[venue] = json.loads(status) if status else {"state": "missing", "coverage_complete": False}
        if statuses[venue]["state"] in ("error", "unavailable", "missing"):
            continue
        key = f"{PREFIX}:{venue}:markets"
        if targets is None:
            values = (await redis.hgetall(key)).values()
        else:
            ids = sorted(targets.get(venue, []))
            values = await redis.hmget(key, ids) if ids else []
        rows.extend(json.loads(value) for value in values if value is not None)
    payload = scan(rows, matches, args.quantity, now_ms(), args.max_age * 1000, getattr(args, "quote_mode", "depth"))
    payload["target_count"] = sum(len(ids) for ids in targets.values()) if targets is not None else len(rows)
    payload["market_count"] = sum(status.get("market_count", 0) for status in statuses.values())
    payload["book_scope"] = getattr(args, "book_scope", "all")
    payload["venues"] = statuses
    payload["coverage_complete"] = all(status.get("coverage_complete") is True and status["state"] == "ok"
                                       for status in statuses.values())
    await redis.set(f"{PREFIX}:scan", dumps(payload), ex=args.max_age)
    return payload


async def run(args: argparse.Namespace) -> None:
    if getattr(args, "source", "venues") == "synpath":
        from user_strategies.synpath_scan import run as run_synpath
        await run_synpath(args)
        return
    matches = load_matches(args.matches)
    async with Redis.from_url(args.redis_url, decode_responses=True, socket_connect_timeout=5,
                              socket_timeout=15) as redis:
        await redis.ping()
        catalogs = {}
        if not args.once and not args.max_markets:
            for venue in VENUES:
                status = json.loads(await redis.get(f"{PREFIX}:{venue}:status") or "{}")
                if status.get("coverage_complete") is True:
                    catalogs[venue] = {id_: json.loads(value) for id_, value in
                                       (await redis.hgetall(f"{PREFIX}:{venue}:markets")).items()}
        args.discovery_only = args.once
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "prediction-scanner/1"}) as client:
            adapters = [Adapter(venue, client, args.quote_mode) for venue in VENUES]
            workers = [asyncio.create_task(collect_venue(adapter, redis, args, catalogs, matches)) for adapter in adapters]
            try:
                if args.once:
                    await asyncio.gather(*workers)
                    args.discovery_only = False
                    workers = [asyncio.create_task(collect_venue(adapter, redis, args, catalogs, matches)) for adapter in adapters]
                    await asyncio.gather(*workers)
                    targets = quote_targets(catalogs, matches, now_ms()) if args.book_scope == "matched" else None
                    await scan_once(redis, args, matches, targets)
                    return
                while True:
                    targets = quote_targets(catalogs, matches, now_ms()) if args.book_scope == "matched" else None
                    await scan_once(redis, args, matches, targets)
                    # Check worker errors so Redis failures do not silently kill a venue.
                    for worker in workers:
                        if worker.done():
                            worker.result()
                    await asyncio.sleep(args.interval)
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("synpath", "venues"), default="synpath",
                        help="Import Synpath scanner results (default), or collect venue books directly")
    parser.add_argument("--redis-url", default=os.environ.get("MARKET_REDIS_URL", "redis://127.0.0.1:6379/0"))
    parser.add_argument("--interval", type=int, default=15, help="Scan/poll interval in seconds")
    parser.add_argument("--discovery-interval", type=int, default=900)
    parser.add_argument("--ttl", type=int, default=600, help="Market/status Redis expiry; quotes have their own age")
    parser.add_argument("--max-age", type=int, default=60, help="Maximum quote age in seconds")
    parser.add_argument("--quantity", type=Decimal, default=Decimal("100"), help="Shares per leg for depth and fees")
    parser.add_argument("--matches", help="Reviewed market-equivalence JSON file")
    parser.add_argument("--max-markets", type=int, default=0, help="Explicit per-venue smoke-test cap; 0 scans all")
    parser.add_argument("--book-scope", choices=("matched", "all"), default="matched",
                        help="Refresh only cross-platform matched books (default), or explicitly every book")
    parser.add_argument("--quote-mode", choices=("top", "depth"), default="top",
                        help="Best bid/ask and size only (default); depth explicitly estimates multi-level fills")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if (min(args.interval, args.discovery_interval, args.ttl, args.max_age) < 1 or args.max_markets < 0
            or not args.quantity.is_finite() or args.quantity <= 0):
        parser.error("Intervals/TTL/quantity must be positive and max-markets nonnegative")
    log_dir = Path(__file__).resolve().parents[1] / "logs" / "prediction"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"scan-{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_path, encoding="utf-8")])
    LOG.info("LOG_FILE %s", log_path)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
