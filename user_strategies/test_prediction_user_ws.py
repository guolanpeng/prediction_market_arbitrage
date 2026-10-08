import asyncio
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import synpath
from synpath.trading.polymarket import MarketTokens
from synpath.trading.types import Fill, OrderStatus, SettlementState, Side, Liquidity
from user_strategies.prediction_trading import LiveTrader
from user_strategies.prediction_user_ws import PolymarketUserObserver, PredictFunUserObserver, merge_receipts


def receipt(state=SettlementState.CONFIRMED, *, quantity="7.833334", order_id="order", trade_id="trade"):
    return Fill(id=trade_id, order_id=order_id, venue="polymarket", market_id="polymarket:1",
                side=Side.SELL, price=Decimal("0.71"), amount=Decimal(quantity), timestamp=1000,
                settlement=state, fee_currency="pUSD", info={"private_payload": "must-not-be-logged"})


def observer():
    trading = SimpleNamespace(wallet="0x" + "1" * 40, signer=SimpleNamespace(address="0x" + "2" * 40))
    feed = PolymarketUserObserver(trading)
    feed.markets.add("polymarket:1")
    return feed


class ReceiptMergeTest(unittest.TestCase):
    def test_deduplicates_and_preserves_confirmation_against_late_matched(self):
        confirmed, matched = receipt(), receipt(SettlementState.MATCHED)
        for rest, ws in (([confirmed], [matched]), ([matched], [confirmed]), ([confirmed], [confirmed])):
            merged = merge_receipts(rest, ws)
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged["trade"].settlement, SettlementState.CONFIRMED)

    def test_conflicting_quantity_or_final_state_requires_reconciliation(self):
        for other in (receipt(quantity="8"), receipt(SettlementState.FAILED)):
            with self.assertRaises(ValueError):
                merge_receipts([receipt()], [other])

    def test_ws_event_times_are_local_receive_times_and_secrets_are_not_logged(self):
        feed = observer()
        with self.assertLogs("prediction-check", level="INFO") as logs:
            feed.observe(synpath.FillEvent(venue="polymarket", fill=receipt(SettlementState.MATCHED), received_at=1200))
            feed.observe(synpath.FillEvent(venue="polymarket", fill=receipt(), received_at=1800))
            feed.observe(synpath.FillEvent(venue="polymarket", fill=receipt(SettlementState.MATCHED), received_at=2000))
        self.assertEqual(feed.times["order"]["ws_first_fill_at_ms"], 1200)
        self.assertEqual(feed.times["order"]["ws_first_confirmed_at_ms"], 1800)
        self.assertEqual(feed.fills["order"]["trade"].settlement, SettlementState.CONFIRMED)
        self.assertNotIn("must-not-be-logged", " ".join(logs.output))

    def test_real_sdk_user_frame_orients_down_token_as_no(self):
        feed = observer()
        tokens = MarketTokens.from_gamma({"id": "1", "conditionId": "0x" + "0" * 64,
                                          "clobTokenIds": '["101", "102"]', "outcomes": '["Up", "Down"]'})
        feed.stream.catalog.remember(tokens)
        events = feed.stream.handle({"event_type": "trade", "id": "trade", "market": tokens.condition_id,
                                     "asset_id": "102", "outcome": "Down", "side": "BUY", "size": "7.833334",
                                     "price": "0.29", "status": "CONFIRMED", "trader_side": "TAKER",
                                     "taker_order_id": "order", "match_time": "1"})
        self.assertEqual(events[0].fill.side, Side.SELL)
        self.assertEqual(events[0].fill.price, Decimal("0.71"))
        feed.observe(events[0])
        self.assertIn("trade", feed.fills["order"])


class DualSourceTest(unittest.IsolatedAsyncioTestCase):
    def trader(self, directory, rest_fills=()):
        trader = LiveTrader(Path(directory) / "orders.jsonl")
        adapter = AsyncMock()
        adapter.fetch_order.return_value = SimpleNamespace(status=OrderStatus.CLOSED, filled=Decimal("7.833334"), info={})
        class Page(list): next_cursor = None
        adapter.fetch_my_trades.return_value = Page(rest_fills)
        trader.adapters = [adapter, AsyncMock()]
        trader.poly_observer = observer()
        trader.poly_observer.updated = trader.confirmation_updated
        trader.poly_observer.watch = AsyncMock()
        order = SimpleNamespace(id="order", market_id="polymarket:1", amount=Decimal("2.3232"), side=Side.SELL, reduce_only=False)
        trader.cash_orders[order.id] = {}
        return trader, order

    async def test_ws_confirmed_plus_rest_terminal_order_works_when_rest_listing_lags(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, order = self.trader(directory)
            trader.poly_observer.observe(synpath.FillEvent(venue="polymarket", fill=receipt()))
            self.assertEqual(await trader.confirmed(0, order), Decimal("7.833334"))
            self.assertEqual(trader.inventories[order.id]["cash_value"], Decimal("2.27166686"))
            self.assertEqual(trader.inventories[order.id]["rest_receipt_count"], 0)
            self.assertEqual(trader.inventories[order.id]["ws_receipt_count"], 1)
            trader.adapters[0].fetch_order.assert_awaited_once()
            trader.adapters[0].create_order.assert_not_awaited()

    async def test_matched_is_not_final_and_rest_unavailable_never_trusts_ws_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, order = self.trader(directory)
            trader.poly_observer.observe(synpath.FillEvent(venue="polymarket", fill=receipt(SettlementState.MATCHED)))
            self.assertIsNone(await trader.confirmed(0, order))
            trader.poly_observer.observe(synpath.FillEvent(venue="polymarket", fill=receipt()))
            trader.adapters[0].fetch_order.side_effect = ConnectionError("secret-provider-details")
            with self.assertLogs("prediction-check", level="WARNING") as logs:
                self.assertIsNone(await trader.confirmed(0, order))
            self.assertNotIn("secret-provider-details", " ".join(logs.output))
            self.assertNotIn(order.id, trader.inventories)

    async def test_rest_and_ws_duplicate_trade_is_counted_once(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, order = self.trader(directory, [receipt()])
            trader.poly_observer.observe(synpath.FillEvent(venue="polymarket", fill=receipt()))
            self.assertEqual(await trader.confirmed(0, order), Decimal("7.833334"))
            self.assertEqual(trader.inventories[order.id]["rest_receipt_count"], 1)
            self.assertIn("rest_first_fill_at_ms", trader.order_times[order.id])

    async def test_conflicting_rest_receipt_and_rest_order_quantity_keep_result_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, order = self.trader(directory, [receipt(SettlementState.FAILED)])
            trader.poly_observer.observe(synpath.FillEvent(venue="polymarket", fill=receipt()))
            self.assertIsNone(await trader.confirmed(0, order))
            trader.adapters[0].fetch_my_trades.return_value.clear()
            trader.adapters[0].fetch_order.return_value.filled = Decimal(0)
            self.assertIsNone(await trader.confirmed(0, order))
            self.assertNotIn(order.id, trader.inventories)

    async def test_disconnect_falls_back_to_rest_and_other_order_receipts_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, order = self.trader(directory, [receipt()])
            trader.poly_observer.observe(synpath.StreamStatusEvent(venue="polymarket", stream="user", state="disconnected", detail="secret-provider-details"))
            trader.poly_observer.observe(synpath.FillEvent(venue="polymarket", fill=receipt(order_id="other")))
            self.assertEqual(await trader.confirmed(0, order), Decimal("7.833334"))
            self.assertEqual(trader.inventories[order.id]["ws_receipt_count"], 0)

    async def test_observer_closes_consumer_task(self):
        feed = observer()
        class Stream:
            def __init__(self): self.queue = asyncio.Queue()
            async def __aenter__(self): return self
            async def close(self): await self.queue.put(None)
            async def events(self):
                while True:
                    item = await self.queue.get()
                    if item is None: return
                    yield item
            def __aiter__(self): return self.events()
        feed.stream = Stream()
        async with feed:
            await feed.stream.queue.put(synpath.FillEvent(venue="polymarket", fill=receipt()))
            await asyncio.sleep(0)
        self.assertTrue(feed.task.done())

    async def test_metrics_separate_sdk_query_time_from_receipt_arrival_delay(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, order = self.trader(directory, [receipt()])
            trader.order_times[order.id] = {"submitted_at_ms": 1000, "acknowledged_at_ms": 1200}
            trader.poly_observer.observe(synpath.FillEvent(venue="polymarket", fill=receipt(), received_at=1800))
            with patch("user_strategies.prediction_trading.time.time_ns", return_value=2_000_000_000), \
                    self.assertLogs("prediction-check", level="INFO") as logs:
                await trader.confirmed(0, order)
            output = " ".join(logs.output)
            self.assertIn("operation=fetch_my_trades elapsed_ms=", output)
            self.assertIn("'rest_first_fill_delay_ms': 1000", output)
            self.assertIn("'ws_first_confirmed_delay_ms': 800", output)
            self.assertIn("rest_receipts=1 ws_receipts=1", output)

    async def test_ws_receipt_wakes_rest_reconciliation_before_poll_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, order = self.trader(directory)
            trader.confirmed = AsyncMock(side_effect=[None, Decimal(0), Decimal("7.833334"), Decimal(0)])
            async def update():
                await asyncio.sleep(0.02)
                trader.poly_observer.observe(synpath.FillEvent(venue="polymarket", fill=receipt()))
            task = asyncio.create_task(update())
            try:
                sizes = await asyncio.wait_for(trader.wait_fills([order, "REJECTED"], 5), timeout=1)
                self.assertEqual(sizes, [Decimal("7.833334"), Decimal(0)])
            finally:
                await task

    async def test_subscription_disconnect_keeps_rest_path_available_without_logging_body(self):
        feed = observer()
        feed.markets.clear()
        feed.trading.market_tokens = AsyncMock(return_value=SimpleNamespace())
        feed.stream.catalog.remember = MagicMock()
        feed.stream.watch_orders = AsyncMock(side_effect=ConnectionError("private-provider-body"))
        with self.assertLogs("prediction-check", level="WARNING") as logs:
            await feed.watch("polymarket:1")
        self.assertIn("polymarket:1", feed.markets)
        self.assertIn("rest_fallback=true", " ".join(logs.output))
        self.assertNotIn("private-provider-body", " ".join(logs.output))


def predict_receipt(*, fee=Decimal("0.2"), trade_id="settlement", state=SettlementState.CONFIRMED):
    return Fill(id=trade_id, order_id="pf-order", venue="predict_fun", market_id="predict_fun:1",
                side=Side.BUY, price=Decimal("0.4"), amount=Decimal(10), timestamp=1000,
                fee=fee, fee_currency="shares" if fee is not None else None, liquidity=Liquidity.TAKER,
                settlement=state)


class PredictUserWSTest(unittest.IsolatedAsyncioTestCase):
    async def test_rejection_reason_logged_without_provider_secrets(self):
        for reason in ("noMarketMatch", "rejectedDuplicate", "rejectedPostOnly", "private-wallet-jwt", None):
            with self.subTest(reason=reason):
                feed = self.feed()
                payload = {"type": "orderNotAccepted", "orderHash": "pf-order", "timestamp": 1000,
                           "reason": reason, "signature": "private-signature",
                           "details": {"marketId": 1, "outcome": "NO", "quoteType": "BID",
                                       "quantity": "10", "quantityFilled": "0", "price": "0.4", "strategyType": "MARKET"}}
                events = feed.stream.handle({"topic": "predictWalletEvents/private-wallet-jwt", "data": payload})
                with self.assertLogs("prediction-check", level="WARNING") as logs:
                    feed.observe(events[0])
                text = " ".join(logs.output)
                expected = reason if reason in ("noMarketMatch", "rejectedDuplicate", "rejectedPostOnly") else "UNKNOWN" if reason else "NOT_PROVIDED"
                self.assertIn("reason=" + expected, text)
                self.assertNotIn("private-wallet-jwt", text)
                self.assertNotIn("private-signature", text)

    def feed(self, updated=None):
        trading = SimpleNamespace(credentials=SimpleNamespace(api_key="fixture"), jwt=AsyncMock(return_value="private-wallet-jwt"))
        feed = PredictFunUserObserver(trading, updated)
        feed.markets.add("predict_fun:1")
        return feed

    def trader(self, directory, rest=()):
        trader = LiveTrader(Path(directory) / "orders.jsonl")
        adapter = AsyncMock()
        adapter.fetch_order.return_value = SimpleNamespace(status=OrderStatus.CLOSED, filled=Decimal(10), info={})
        class Page(list): next_cursor = None
        adapter.fetch_my_trades.return_value = Page(rest)
        trader.adapters = [AsyncMock(), adapter]
        trader.predict_observer = self.feed(trader.confirmation_updated)
        trader.predict_observer.watch = AsyncMock()
        order = SimpleNamespace(id="pf-order", market_id="predict_fun:1", amount=Decimal(10), side=Side.BUY, reduce_only=False)
        trader.cash_orders[order.id] = {}
        return trader, order

    async def test_predict_ws_confirmed_preserves_share_fee_and_rest_reconciles(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, order = self.trader(directory, [predict_receipt(trade_id="settlement:pf-order")])
            trader.predict_observer.observe(synpath.FillEvent(venue="predict_fun", fill=predict_receipt()))
            self.assertEqual(await trader.confirmed(1, order), Decimal(10))
            self.assertEqual(trader.inventories[order.id]["net_shares"], Decimal("9.8"))
            self.assertEqual(trader.inventories[order.id]["cash_value"], Decimal(4))
            self.assertEqual(trader.inventories[order.id]["ws_receipt_count"], 1)
            trader.adapters[1].create_order.assert_not_awaited()

    async def test_predict_fee_missing_stays_unknown_until_rest_supplies_fee(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, order = self.trader(directory)
            trader.predict_observer.observe(synpath.FillEvent(venue="predict_fun", fill=predict_receipt(fee=None)))
            self.assertIsNone(await trader.confirmed(1, order))
            self.assertNotIn(order.id, trader.inventories)
            trader.adapters[1].fetch_my_trades.return_value.append(predict_receipt())
            self.assertEqual(await trader.confirmed(1, order), Decimal(10))
            self.assertEqual(trader.inventories[order.id]["share_fees"], Decimal("0.2"))

    async def test_predict_conflicting_fees_keep_result_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, order = self.trader(directory, [predict_receipt(fee=Decimal("0.1"))])
            trader.predict_observer.observe(synpath.FillEvent(venue="predict_fun", fill=predict_receipt()))
            self.assertIsNone(await trader.confirmed(1, order))
            self.assertNotIn(order.id, trader.inventories)

    async def test_wallet_subscription_uses_jwt_callable_and_renews_without_logging_token(self):
        feed = self.feed()
        calls = []
        async def jwt(*, fresh=False):
            calls.append(fresh)
            return "private-new-jwt" if fresh else "private-wallet-jwt"
        feed.stream._jwt_source = jwt
        await feed.watch("predict_fun:1")
        await feed.watch("predict_fun:2")
        self.assertEqual(calls, [False])
        await feed.stream._renew()
        self.assertEqual(calls, [False, True])
        with self.assertLogs("prediction-check", level="INFO") as logs:
            feed.observe(synpath.StreamStatusEvent(venue="predict_fun", stream="user", state="subscribed", detail="private-new-jwt"))
        self.assertNotIn("private-new-jwt", " ".join(logs.output))

    async def test_real_predict_wallet_frame_normalizes_no_and_preserves_share_fee(self):
        feed = self.feed()
        event = {"type": "orderTransactionSuccess", "orderHash": "pf-order", "settlementId": "settlement", "timestamp": 1000,
                 "details": {"marketId": 1, "outcome": "NO", "quoteType": "BID", "quantity": "10", "quantityFilled": "10",
                             "price": "0.4", "strategyType": "MARKET"},
                 "fill": {"executedSizeWei": str(10 * 10**18), "executedPriceWei": str(4 * 10**17)},
                 "fee": {"type": "SHARES", "amountWei": str(2 * 10**17)}, "isMaker": False}
        events = feed.stream.handle({"topic": "predictWalletEvents/private-wallet-jwt", "data": event})
        for incoming in events:
            feed.observe(incoming)
        fill = next(iter(feed.fills["pf-order"].values()))
        self.assertEqual(fill.side, Side.SELL)
        self.assertEqual(fill.price, Decimal("0.6"))
        self.assertEqual(fill.fee, Decimal("0.2"))
        self.assertEqual(fill.fee_currency, "shares")
        self.assertEqual(fill.settlement, SettlementState.CONFIRMED)

    async def test_predict_ws_wakes_combined_confirmation_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, order = self.trader(directory)
            trader.confirmed = AsyncMock(side_effect=[Decimal(0), None, Decimal(0), Decimal(10)])
            async def update():
                await asyncio.sleep(0.02)
                trader.predict_observer.observe(synpath.FillEvent(venue="predict_fun", fill=predict_receipt()))
            task = asyncio.create_task(update())
            try:
                self.assertEqual(await asyncio.wait_for(trader.wait_fills(["REJECTED", order], 5), timeout=1), [Decimal(0), Decimal(10)])
            finally:
                await task
