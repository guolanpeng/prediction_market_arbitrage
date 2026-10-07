"""Import Synpath's public scanner snapshot; never connect to trading APIs."""

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime
from decimal import Decimal

import httpx
from redis.asyncio import Redis

from user_strategies.prediction_markets import VENUES, dumps, now_ms

URL = "https://api.synpath.dev/arbitrage"
MAX_AGE_MS = 120000
LOG = logging.getLogger(__name__)


def timestamp(value):
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Source timestamp has no timezone")
    return int(date.timestamp() * 1000)


def amount(value):
    if value is None:
        return None
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError("Invalid source amount")
    return format(value, "f")


def normalize(data, received_at_ms):
    computed = timestamp(data["computed_at"])
    if not -5000 <= received_at_ms - computed <= MAX_AGE_MS:
        raise ValueError("Source snapshot is stale or in the future")
    if not isinstance(data["results"], list):
        raise ValueError("Invalid scanner results")
    candidates, counts = [], Counter()
    invalid = 0
    for row in data["results"]:
        try:
            legs = []
            reasons = ["synpath_match_unverified"]
            for side in ("yes", "no"):
                source = row[side]
                venue = "predictfun" if source["venue"] == "predict_fun" else source["venue"]
                if venue not in VENUES:
                    raise ValueError("Unsupported venue")
                price = amount(source["price"])
                fee = amount(source.get("fee"))
                if not price or not 0 < Decimal(price) < 1 or (fee is not None and Decimal(fee) < 0):
                    raise ValueError("Invalid price or fee")
                quoted_at = timestamp(source["fetched_at"]) if source.get("fetched_at") else None
                estimated = source.get("fee_source") != "venue"
                legs.append({"venue": venue, "market_id": str(source["native_market_id"]),
                             "side": side, "source_side": source.get("side"), "average_price": price,
                             "quantity": "1", "cost": price, "fees": fee, "fee_estimated": estimated,
                             "fee_source": source.get("fee_source"), "fee_detail": source.get("fee_detail"),
                             "quote_at_ms": quoted_at, "url": source.get("url")})
                if fee is None:
                    reasons.append("fees_unknown")
                elif estimated:
                    reasons.append("fees_estimated")
                if quoted_at is None or not 0 <= received_at_ms - quoted_at <= MAX_AGE_MS:
                    reasons.append("source_quote_stale")
            gross, net, fees = (amount(row.get(k)) for k in ("gross_spread", "net_profit", "fees"))
            candidate = {"id": f"{row['outcome_id']}|{legs[0]['venue']}|{legs[1]['venue']}",
                         "title": row["event_title"], "outcome": row["outcome_label"],
                         "close_time": row.get("close_at"), "event_time": row.get("event_time"),
                         "domain": row.get("domain"), "match_method": "synpath", "match_verified": False,
                         "source": "synpath", "quantity": "1", "available_quantity": None,
                         "quote_mode": "source", "yes_leg": legs[0], "no_leg": legs[1],
                         "gross_spread": gross, "gross_profit": gross, "fees_per_share": fees,
                         "net_profit": net, "net_profit_per_share": net,
                         "review_reasons": list(dict.fromkeys(reasons))}
            candidates.append(candidate)
            counts.update(leg["venue"] for leg in legs)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            invalid += 1
    return {"schema_version": 1, "source": "synpath", "source_url": URL, "source_state": "ok",
            "scanned_at_ms": computed, "received_at_ms": received_at_ms, "max_quote_age_ms": MAX_AGE_MS,
            "matched_pairs": data["pairs"], "mismatched_pairs": data.get("mismatched"),
            "market_count": None, "target_count": None, "quantity": None,
            "coverage_complete": False, "source_ttl_s": amount(data.get("ttl_s")),
            "invalid_records": invalid, "venue_result_counts": dict(counts),
            "candidates": candidates, "opportunities": []}


async def run(args):
    async with Redis.from_url(args.redis_url, decode_responses=True, socket_connect_timeout=5,
                              socket_timeout=15) as redis:
        await redis.ping()
        # The local proxy breaks this host's TLS; access the public endpoint directly.
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            while True:
                try:
                    response = await client.get(URL)
                    response.raise_for_status()
                    raw = json.loads(response.text, parse_float=Decimal)
                    payload = normalize(raw, now_ms())
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                    code = f"http_{error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else type(error).__name__
                    payload = {"schema_version": 1, "source": "synpath", "source_url": URL,
                               "source_state": "error", "source_error": code,
                               "scanned_at_ms": now_ms(), "max_quote_age_ms": MAX_AGE_MS,
                               "candidates": [], "opportunities": [], "venue_result_counts": {}}
                    LOG.warning("Synpath import failed: %s", code)
                    raw = None
                async with redis.pipeline(transaction=True) as pipe:
                    if raw is not None:
                        pipe.set("prediction:v1:synpath:raw", dumps(raw), ex=120)
                    pipe.set("prediction:v1:scan", dumps(payload), ex=120)
                    await pipe.execute()
                LOG.info("Synpath: %s, %d results", payload["source_state"], len(payload["candidates"]))
                if args.once:
                    if payload["source_state"] != "ok":
                        raise RuntimeError("Synpath import failed")
                    return
                await asyncio.sleep(args.interval)
