"""Read-only production prediction-market REST adapters."""

import asyncio
import json
import os
import time
from decimal import Decimal, localcontext
from typing import Any
from collections.abc import Awaitable, Callable

import httpx

VENUES = ("kalshi", "polymarket", "opinion", "hyperliquid", "predictfun")
BASES = {
    "kalshi": "https://api.elections.kalshi.com/trade-api/v2",
    "polymarket": "https://gamma-api.polymarket.com",
    "opinion": "https://openapi.opinion.trade/openapi",
    "hyperliquid": "https://api.hyperliquid.xyz/info",
    "predictfun": "https://api.predict.fun/v1",
}


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def levels(rows: list, *, reverse: bool = False, complement: bool = False) -> list:
    """Normalize and aggregate executable levels without a float conversion."""
    prices: dict[Decimal, Decimal] = {}
    with localcontext() as context:
        context.prec = 64
        for row in rows:
            if isinstance(row, dict):
                price, size = row.get("price", row.get("px")), row.get("size", row.get("sz"))
            else:
                price, size = row[:2]
            price, size = Decimal(str(price)), Decimal(str(size))
            if not price.is_finite() or not size.is_finite():
                continue
            if complement:
                price = Decimal(1) - price
            if not 0 < price < 1 or size <= 0:
                continue
            prices[price] = prices.get(price, Decimal(0)) + size
    return [[str(p), str(prices[p])] for p in sorted(prices, reverse=reverse)]


def book(asks: list, bids: list, timestamp: Any = None) -> dict:
    return {
        "asks": levels(asks), "bids": levels(bids, reverse=True),
        "source_at_ms": int(timestamp) if timestamp else None,
        "received_at_ms": now_ms(),
    }


def market(venue: str, id_: Any, title: str, **fields: Any) -> dict:
    return {
        "venue": venue, "market_id": str(id_), "title": title,
        "yes_label": "Yes", "no_label": "No", "rules": "",
        "close_time": None, "quote_currency": None, "volume_24h": None,
        "fee": None, "references": [], "books": {}, "quote_at_ms": None,
        **fields,
    }


def finish_quote(item: dict, books: dict, mode: str) -> dict:
    if mode == "top":
        books = {side: {**data, "asks": data["asks"][:1], "bids": data["bids"][:1]}
                 for side, data in books.items()}
    return {**item, "books": books, "quote_at_ms": now_ms(), "quote_error": None, "quote_mode": mode}


class Adapter:
    def __init__(self, venue: str, client: httpx.AsyncClient, quote_mode: str = "depth") -> None:
        self.venue = venue
        self.client = client
        self.quote_mode = quote_mode
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        # Predict's default quota is 240/minute; retain headroom for retries.
        self.spacing = {"kalshi": .12, "polymarket": .06, "opinion": .15,
                        "hyperliquid": .3, "predictfun": .3}[venue]

    async def request(self, path: str = "", *, params: dict | None = None,
                      body: Any = None, url: str | None = None) -> Any:
        headers = {}
        key_name, header = {
            "opinion": ("OPINION_API_KEY", "apikey"),
            "predictfun": ("PREDICT_FUN_API_KEY", "x-api-key"),
        }.get(self.venue, (None, None))
        if key_name and os.environ.get(key_name):
            headers[header] = os.environ[key_name]
        target = url or BASES[self.venue] + path
        for attempt in range(3):
            async with self.lock:
                await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                self.next_request = time.monotonic() + self.spacing
            try:
                response = await self.client.request(
                    "POST" if body is not None else "GET", target,
                    params=params, json=body, headers=headers,
                )
                if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    delay = response.headers.get("Retry-After", str(2 ** attempt))
                    try:
                        delay = min(60, max(1, float(delay)))
                    except ValueError:
                        delay = 2 ** attempt
                    async with self.lock:
                        self.next_request = max(self.next_request, time.monotonic() + delay)
                    continue
                response.raise_for_status()
                data = json.loads(response.text, parse_float=Decimal)
                if self.venue == "opinion":
                    if data.get("errno", data.get("code")) != 0:
                        raise ValueError("Opinion API error")
                    return data["result"]
                if self.venue == "predictfun" and data.get("success") is not True:
                    raise ValueError("Predict.fun API error")
                return data
            except httpx.TransportError:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError("Request retry limit reached")

    async def discover(self, limit: int = 0,
                       on_page: Callable[[dict[str, dict], int], Awaitable[None]] | None = None) -> tuple[dict[str, dict], bool]:
        """Return every active binary outcome; an explicit limit marks partial coverage."""
        if self.venue == "predictfun" and not os.environ.get("PREDICT_FUN_API_KEY"):
            raise PermissionError("PREDICT_FUN_API_KEY is required for mainnet")
        markets: dict[str, dict] = {}
        cursor, page, seen = "", 1, set()
        while True:
            if self.venue == "kalshi":
                data = await self.request("/markets", params={
                    "status": "open", "mve_filter": "exclude", "limit": 1000,
                    **({"cursor": cursor} if cursor else {}),
                })
                rows, next_cursor = data["markets"], data.get("cursor", "")
            elif self.venue == "polymarket":
                data = await self.request("/markets/keyset", params={
                    "active": "true", "closed": "false", "limit": 100,
                    **({"after_cursor": cursor} if cursor else {}),
                })
                rows, next_cursor = data["markets"], data.get("next_cursor", "")
            elif self.venue == "opinion":
                data = await self.request("/market", params={
                    "status": "activated", "marketType": 2, "limit": 20, "page": page,
                })
                rows = data["list"]
                next_cursor = str(page + 1) if page * 20 < data["total"] else ""
            elif self.venue == "predictfun":
                data = await self.request("/markets", params={
                    "first": 100, "status": "OPEN", **({"after": cursor} if cursor else {}),
                })
                rows, next_cursor = data["data"], data.get("cursor", "")
            else:
                data = await self.request(body={"type": "outcomeMeta"})
                parents = {}
                for question in data.get("questions", []):
                    settled = set(question.get("settledNamedOutcomes", []))
                    for id_ in [question.get("fallbackOutcome"), *question.get("namedOutcomes", [])]:
                        parents[id_] = (question, id_ in settled)
                for row in data["outcomes"]:
                    parent, settled = parents.get(row["outcome"], ({}, False))
                    if settled:
                        continue
                    item = market("hyperliquid", row["outcome"],
                                  " | ".join(filter(None, [parent.get("description"), row.get("description")])),
                                  rules=dumps({"outcome": row, "question": parent}),
                                  quote_currency=row.get("quoteToken"),
                                  tokens=[f"#{10 * int(row['outcome']) + side}" for side in (0, 1)],
                                  fee_metadata={"feeScale": data.get("feeScale"),
                                                "deployerFeeScale": row.get("deployerFeeScale")})
                    markets[item["market_id"]] = item
                if limit and len(markets) > limit:
                    markets = dict(list(markets.items())[:limit])
                    if on_page:
                        await on_page(markets, len(markets))
                    return markets, False
                if on_page:
                    await on_page(markets, len(markets))
                return markets, True
            page_items = {}
            for row in rows:
                for item in self.normalize(row):
                    markets[item["market_id"]] = item
                    page_items[item["market_id"]] = item
                    if limit and len(markets) >= limit:
                        if on_page:
                            await on_page(page_items, len(markets))
                        return markets, False
            if on_page:
                await on_page(page_items, len(markets))
            if not next_cursor:
                return markets, True
            if not rows or next_cursor in seen:
                raise ValueError("Pagination stopped before completion")
            seen.add(next_cursor)
            cursor, page = next_cursor, page + 1

    def normalize(self, row: dict) -> list[dict]:
        venue = self.venue
        if venue == "kalshi":
            if row.get("status") not in ("active", "open") or row.get("market_type") != "binary":
                return []
            if Decimal(str(row.get("notional_value_dollars", "1"))) != 1:
                return []
            return [market(venue, row["ticker"], row.get("title") or row.get("subtitle") or row["ticker"],
                           title_missing=not bool(row.get("title")),
                           yes_label=row.get("yes_sub_title") or "Yes",
                           no_label=row.get("no_sub_title") or "No",
                           rules="\n".join([row.get("rules_primary", ""), row.get("rules_secondary", "")]),
                           close_time=row.get("close_time"), quote_currency="USD",
                           volume_24h=row.get("volume_24h_fp"),
                           event_id=row.get("event_ticker"),
                           fee={"model": "quadratic", "rate": "0.07", "estimated": True},
                           url=f"https://kalshi.com/markets/{row['ticker'].split('-')[0].lower()}")]
        if venue == "polymarket":
            outcomes = row.get("outcomes", [])
            tokens = row.get("clobTokenIds", [])
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
            labels = [str(x).casefold() for x in outcomes]
            if (not row.get("acceptingOrders") or not row.get("enableOrderBook")
                    or row.get("closed") or set(labels) != {"yes", "no"} or len(tokens) != 2):
                return []
            fee = None
            if row.get("feesEnabled") is False:
                fee = {"model": "zero", "estimated": False}
            elif row.get("feeSchedule"):
                schedule = row["feeSchedule"]
                if schedule.get("exponent") == 1 and schedule.get("rate") is not None:
                    fee = {"model": "quadratic", "rate": str(schedule["rate"]), "estimated": False}
            return [market(venue, row["conditionId"], row["question"],
                           rules=row.get("description", ""), close_time=row.get("endDate"),
                           quote_currency="USDC", volume_24h=str(row["volume24hr"]) if row.get("volume24hr") is not None else None,
                           tokens=[tokens[labels.index("yes")], tokens[labels.index("no")]],
                           minimum_shares=str(row.get("orderMinSize", "0")), fee=fee,
                           fee_metadata=row.get("feeSchedule"),
                           url=f"https://polymarket.com/event/{row['slug']}")]
        if venue == "opinion":
            children = row.get("childMarkets") or [row]
            result = []
            for child in children:
                if child.get("status") != 2 or not child.get("yesTokenId") or not child.get("noTokenId"):
                    continue
                title = child["marketTitle"] if child is row else f"{row['marketTitle']} | {child['marketTitle']}"
                quote = child.get("quoteToken", row.get("quoteToken", ""))
                result.append(market(venue, child["marketId"], title,
                                     yes_label=child.get("yesLabel") or "Yes", no_label=child.get("noLabel") or "No",
                                     rules=child.get("rules") or row.get("rules", ""),
                                     close_time=child.get("cutoffAt") or row.get("cutoffAt"),
                                     quote_currency="USDT" if str(quote).lower() == "0x55d398326f99059ff775485246999027b3197955" else quote,
                                     volume_24h=child.get("volume24h"),
                                     tokens=[child["yesTokenId"], child["noTokenId"]],
                                     fee_metadata=child.get("incentiveFactor"), minimum_notional="5",
                                     url=f"https://app.opinion.trade/detail?topicId={row['marketId']}"))
            return result
        if row.get("tradingStatus") != "OPEN" or row.get("isVisible") is False:
            return []
        refs = [{"venue": "polymarket", "market_id": x} for x in (row.get("polymarketConditionIds") or [])]
        if row.get("kalshiMarketTicker"):
            refs.append({"venue": "kalshi", "market_id": row["kalshiMarketTicker"]})
        stats = row.get("stats") or {}
        return [market(venue, row["id"], row.get("question") or row["title"],
                       rules=row.get("description", ""), quote_currency="USDT",
                       fee={"model": "zero", "estimated": False} if row.get("feeRateBps") == 0 else None,
                       fee_metadata={"feeRateBps": row.get("feeRateBps")}, references=refs,
                       volume_24h=str(stats["volume24hUsd"]) if stats.get("volume24hUsd") is not None else None,
                       url=f"https://predict.fun/market/{row['id']}")]

    async def quote(self, item: dict) -> dict:
        if self.venue == "kalshi" and self.quote_mode == "top":
            result = (await self.quote_batch([item]))[0]
            if isinstance(result, Exception):
                raise result
            return result
        if self.venue == "kalshi":
            data = await self.request(f"/markets/{item['market_id']}/orderbook")
            fp = data["orderbook_fp"]
            yes, no = levels(fp.get("yes_dollars", []), reverse=True), levels(fp.get("no_dollars", []), reverse=True)
            books = {"yes": book(levels(no, complement=True), yes),
                     "no": book(levels(yes, complement=True), no)}
        elif self.venue == "predictfun":
            data = (await self.request(f"/markets/{item['market_id']}/orderbook"))["data"]
            books = {"yes": book(data["asks"], data["bids"], data.get("updateTimestampMs")),
                     "no": book(levels(data["bids"], complement=True),
                                levels(data["asks"], complement=True), data.get("updateTimestampMs"))}
        else:
            books = {}
            for side, token in zip(("yes", "no"), item["tokens"], strict=True):
                if self.venue == "polymarket":
                    data = await self.request(url="https://clob.polymarket.com/book", params={"token_id": token})
                    books[side] = book(data["asks"], data["bids"], data.get("timestamp"))
                elif self.venue == "opinion":
                    data = await self.request("/token/orderbook", params={"token_id": token})
                    books[side] = book(data["asks"], data["bids"], data.get("timestamp"))
                else:
                    data = await self.request(body={"type": "l2Book", "coin": token})
                    books[side] = book(data["levels"][1], data["levels"][0], data.get("time"))
        return finish_quote(item, books, self.quote_mode)

    async def quote_batch(self, items: list[dict]) -> list:
        if self.venue == "kalshi" and self.quote_mode == "top":
            try:
                data = await self.request("/markets", params={"tickers": ",".join(row["market_id"] for row in items), "limit": 100})
                indexed = {row["ticker"]: row for row in data["markets"]}
                results = []
                for item in items:
                    row = indexed.get(item["market_id"])
                    if not row or row.get("status") not in ("active", "open"):
                        results.append(ValueError("Market unavailable or closed"))
                        continue
                    def level(price, size):
                        return [[price, size]] if price is not None and size is not None else []
                    yes = book(level(row.get("yes_ask_dollars"), row.get("yes_ask_size_fp")),
                               level(row.get("yes_bid_dollars"), row.get("yes_bid_size_fp")))
                    no = book(level(row.get("no_ask_dollars"), row.get("yes_bid_size_fp")),
                              level(row.get("no_bid_dollars"), row.get("yes_ask_size_fp")))
                    results.append(finish_quote(item, {"yes": yes, "no": no}, "top"))
                return results
            except Exception as error:
                return [error for _ in items]
        if self.venue != "polymarket":
            return await asyncio.gather(*(self.quote(row) for row in items), return_exceptions=True)
        # /books is a read-only batch endpoint, not an order submission endpoint.
        try:
            data = await self.request(url="https://clob.polymarket.com/books",
                                      body=[{"token_id": token} for row in items for token in row["tokens"]])
            indexed = {row["asset_id"]: row for row in data}
            results = []
            for row in items:
                if any(token not in indexed for token in row["tokens"]):
                    results.append(ValueError("Missing token in batch orderbook response"))
                    continue
                books = {}
                for side, token in zip(("yes", "no"), row["tokens"], strict=True):
                    source = indexed[token]
                    books[side] = book(source["asks"], source["bids"], source.get("timestamp"))
                results.append(finish_quote(row, books, self.quote_mode))
            return results
        except Exception as error:
            return [error for _ in items]
