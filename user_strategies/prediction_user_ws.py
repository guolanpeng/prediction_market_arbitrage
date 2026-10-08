"""Read-only account streams and receipt reconciliation for both venues."""

import asyncio
import logging

import synpath
from synpath.trading.types import SettlementState, OrderStatus
from websockets.asyncio.client import connect

LOG = logging.getLogger("prediction-check")
FINAL = {SettlementState.CONFIRMED, SettlementState.FAILED}


def merge_receipts(rest, ws):
    """One receipt per trade ID; terminal states cannot regress to MATCHED."""
    merged = {}
    for fill in [*rest, *ws]:
        # Predict's REST appends a leg hash when the wallet has multiple match legs.
        key = (fill.id.split(":", 1)[0], fill.order_id) if fill.venue == "predict_fun" else fill.id
        old = merged.get(key)
        if old is not None:
            for field in ("order_id", "amount", "price", "side"):
                if getattr(old, field, None) != getattr(fill, field, None):
                    raise ValueError("Receipt identity or amounts disagree")
            if old.settlement in FINAL:
                if fill.settlement in FINAL and fill.settlement != old.settlement:
                    raise ValueError("Receipt final states disagree")
                if fill.settlement in FINAL:
                    if old.fee is not None and fill.fee is not None and (old.fee != fill.fee or old.fee_currency != fill.fee_currency):
                        raise ValueError("Receipt fees disagree")
                    if old.fee is None and fill.fee is not None:
                        merged[key] = fill
                continue
        merged[key] = fill
    return merged


async def connector(url, **kwargs):
    return await connect(url, proxy=None, max_size=None, **kwargs)


class UserOrderObserver:
    def __init__(self, trading, venue, updated=None):
        self.trading = trading
        self.venue = venue
        if venue == "polymarket":
            self.stream = synpath.PolymarketUserStream(trading=trading, connect=connector, max_queue=1000)
        else:
            self.stream = synpath.PredictFunUserStream(api_key=trading.credentials.api_key, jwt=trading.jwt,
                                                       connect=connector, max_queue=1000)
        self.markets = set()
        self.fills = {}
        self.conflicts = set()
        self.times = {}
        self.updated = updated if updated is not None else asyncio.Event()
        self.task = None

    async def __aenter__(self):
        await self.stream.__aenter__()
        self.task = asyncio.create_task(self.drain(), name=f"prediction-{self.venue}-user-ws")
        return self

    async def __aexit__(self, *exc):
        try:
            await self.stream.close()
        finally:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def watch(self, market_id):
        if self.venue == "predict_fun":
            self.markets.add(market_id)
            # The server follows the whole wallet; filter markets locally.
            if self.stream.topics:
                return
            try:
                await self.stream.watch_orders()
            except Exception as error:
                LOG.warning("USER_WS_SUBSCRIBE_INTERRUPTED venue=%s error=%s rest_fallback=true", self.venue, type(error).__name__)
            return
        if market_id not in self.markets:
            tokens = await self.trading.market_tokens(market_id)
            # Up/Down labels alone cannot orient a NO fill; cache both token IDs.
            self.stream.catalog.remember(tokens)
            self.markets.add(market_id)
            try:
                await self.stream.watch_orders([market_id])
            except Exception as error:
                # The SDK remembers subscription intent and resends after reconnect.
                LOG.warning("USER_WS_SUBSCRIBE_INTERRUPTED venue=%s error=%s rest_fallback=true", self.venue, type(error).__name__)

    def observe(self, event):
        if isinstance(event, synpath.StreamStatusEvent):
            LOG.info("USER_WS_STATE venue=%s state=%s", self.venue, event.state)
            self.updated.set()
            return
        if isinstance(event, synpath.OrderEvent):
            order = event.order
            if order.market_id not in self.markets:
                return
            self.times.setdefault(order.id, {}).setdefault("ws_first_order_at_ms", event.received_at)
            LOG.info("USER_WS_ORDER venue=%s order_id=%s status=%s filled=%s received_at_ms=%s",
                     self.venue, order.id, order.status, order.filled, event.received_at)
            if self.venue == "predict_fun" and order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED):
                reason = order.info.get("reason")
                # Emit only documented codes, never a provider body, JWT or arbitrary error string.
                safe_reason = reason if reason in ("noMarketMatch", "rejectedDuplicate", "rejectedPostOnly", "selfTradePrevention") else "UNKNOWN" if reason else "NOT_PROVIDED"
                LOG.warning("USER_WS_ORDER_REJECTED venue=%s order_id=%s status=%s reason=%s received_at_ms=%s",
                            self.venue, order.id, order.status, safe_reason, event.received_at)
        elif isinstance(event, synpath.FillEvent):
            fill = event.fill
            if fill.market_id not in self.markets:
                return
            seen = self.times.setdefault(fill.order_id, {})
            seen.setdefault("ws_first_fill_at_ms", event.received_at)
            if fill.settlement == SettlementState.CONFIRMED:
                seen.setdefault("ws_first_confirmed_at_ms", event.received_at)
            try:
                self.fills[fill.order_id] = merge_receipts(self.fills.get(fill.order_id, {}).values(), [fill])
            except ValueError:
                self.conflicts.add(fill.order_id)
                LOG.warning("USER_WS_RECEIPT_CONFLICT venue=%s order_id=%s", self.venue, fill.order_id)
            LOG.info("USER_WS_FILL venue=%s order_id=%s trade_id=%s state=%s shares=%s received_at_ms=%s",
                     self.venue, fill.order_id, fill.id, fill.settlement, fill.amount, event.received_at)
        else:
            return
        self.updated.set()

    async def drain(self):
        try:
            async for event in self.stream:
                self.observe(event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOG.warning("USER_WS_CONSUMER_STOPPED venue=%s error=%s rest_fallback=true", self.venue, type(error).__name__)


class PolymarketUserObserver(UserOrderObserver):
    def __init__(self, trading, updated=None):
        super().__init__(trading, "polymarket", updated)


class PredictFunUserObserver(UserOrderObserver):
    def __init__(self, trading, updated=None):
        super().__init__(trading, "predict_fun", updated)
