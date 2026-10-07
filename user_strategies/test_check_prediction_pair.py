import unittest
import argparse
import asyncio
from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from user_strategies.check_prediction_pair import auto_btc, live_btc, calculate, decimal, fee_config, validate_btc_window


def asks(*pairs):
    return [SimpleNamespace(price=Decimal(p), size=Decimal(q)) for p, q in pairs]


class PairCheckTest(unittest.TestCase):
    def test_decimal_depth_and_price_dependent_fees(self):
        configs = [{"model": "quadratic_theta", "rate": Decimal("0.07"), "places": "0.00001"},
                   {"model": "min_price", "rate": Decimal("0.02"), "places": "0.000001"}]
        result = calculate(asks(("0.4", "2.5"), ("0.5", "7.5")), asks(("0.45", "10")), Decimal("10"), configs)
        self.assertEqual(result["legs"][0]["cost"], Decimal("4.75"))
        self.assertEqual(result["legs"][0]["fee"], Decimal("0.17325"))
        self.assertEqual(result["legs"][1]["fee"], Decimal("0.09"))
        self.assertEqual(result["net"], Decimal("0.48675"))
        self.assertEqual(result["net_per_pair"], Decimal("0.048675"))

    def test_missing_fees_are_not_free_and_partial_depth_is_not_profitable(self):
        result = calculate(asks(("0.4", "10")), asks(("0.45", "10")), Decimal("10"), [None, None])
        self.assertEqual(result["state"], "FEES_UNKNOWN")
        self.assertIsNone(result["net"])
        result = calculate(asks(("0.4", "0.61")), asks(("0.45", "10")), Decimal("10"), [None, None])
        self.assertEqual(result["state"], "INSUFFICIENT_DEPTH")
        self.assertIsNone(result["gross"])
        self.assertEqual(result["legs"][0]["filled"], Decimal("0.61"))

    def test_positive_gross_can_be_negative_after_fees(self):
        configs = [{"model": "quadratic_theta", "rate": Decimal("0.07"), "places": "0.00001"},
                   {"model": "min_price", "rate": Decimal("0.02"), "places": "0.000001"}]
        result = calculate(asks(("0.49", "10")), asks(("0.49", "10")), Decimal("10"), configs)
        self.assertGreater(result["gross"], 0)
        self.assertLess(result["net"], 0)
        self.assertEqual(result["state"], "NO_NET_EDGE")

    def test_fee_is_price_dependent_not_fixed_percent_of_cash_invested(self):
        configs = [{"model": "quadratic_theta", "rate": Decimal("0.07"), "places": "0.00001"},
                   {"model": "min_price", "rate": Decimal("0.02"), "places": "0.000001"}]
        for price, poly_fee, predict_fee in (("0.1", "0.63", "0.20"), ("0.3", "1.47", "0.60"),
                                             ("0.5", "1.75", "1.00"), ("0.7", "1.47", "0.60"),
                                             ("0.9", "0.63", "0.20")):
            result = calculate(asks((price, "100")), asks((price, "100")), Decimal(100), configs)
            self.assertEqual(result["legs"][0]["fee"], Decimal(poly_fee))
            self.assertEqual(result["legs"][1]["fee"], Decimal(predict_fee))

    def test_known_zero_and_unknown_fee_configuration(self):
        market = SimpleNamespace(info={"feesEnabled": False})
        self.assertEqual(fee_config("polymarket", market, None)["rate"], Decimal(0))
        schedule = SimpleNamespace(fee_type="min_price", info={"fee_rate_bps": None})
        self.assertIsNone(fee_config("predictfun", market, schedule))
        schedule.info["fee_rate_bps"] = 200
        self.assertEqual(fee_config("predictfun", market, schedule)["rate"], Decimal("0.02"))

    def test_invalid_numeric_inputs(self):
        for value in ("NaN", "Infinity", "bad"):
            with self.assertRaises(ValueError): decimal(value)


def window_data(start=600):
    iso = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat()
    slug = f"btc-updown-5m-{start}"
    poly = {"id": "1", "slug": slug, "eventStartTime": iso(start), "endDate": iso(start + 300),
            "outcomes": '["Up", "Down"]', "closed": False, "acceptingOrders": True}
    market = {"id": "2", "categorySlug": slug, "marketVariant": "CRYPTO_UP_DOWN", "tradingStatus": "OPEN",
              "outcomes": [{"indexSet": 1, "name": "Up"}, {"indexSet": 2, "name": "Down"}],
              "variantData": {"priceFeedSymbol": "BTCUSDT"}}
    category = {"slug": slug, "startsAt": iso(start), "endsAt": iso(start + 300), "status": "OPEN", "markets": [market]}
    return [poly], category


class WindowTest(unittest.TestCase):
    def test_time_and_outcome_orientation_validation(self):
        poly, category = window_data()
        self.assertEqual(validate_btc_window(poly, category, 600)["end"], 900)
        category["endsAt"] = datetime.fromtimestamp(1200, timezone.utc).isoformat()
        with self.assertRaises(ValueError): validate_btc_window(poly, category, 600)
        poly, category = window_data()
        poly[0]["outcomes"] = '["Down", "Up"]'
        with self.assertRaises(ValueError): validate_btc_window(poly, category, 600)

    def test_rejects_wrong_asset_closed_or_ambiguous_market(self):
        poly, category = window_data()
        category["markets"][0]["variantData"]["priceFeedSymbol"] = "ETHUSDT"
        with self.assertRaises(ValueError): validate_btc_window(poly, category, 600)
        poly, category = window_data()
        poly[0]["acceptingOrders"] = False
        with self.assertRaises(ValueError): validate_btc_window(poly, category, 600)
        poly, category = window_data()
        with self.assertRaises(ValueError): validate_btc_window(poly * 2, category, 600)


class RolloverTest(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_timeout_switches_to_queries_then_settlement_without_rescanning(self):
        from user_strategies.prediction_trading import LiveConfirmationPending, LiveRunComplete
        record = {"attempt": "delayed"}
        settlement = {"attempt": "delayed", "stop_after": True}
        trader = SimpleNamespace(confirmation_waiting={}, settlement_waiting={},
                                 wait_confirmation=AsyncMock(return_value=settlement), wait_settlement=AsyncMock())
        context = AsyncMock()
        context.__aenter__.return_value = trader
        args = argparse.Namespace(max_trades=1, preflight_only=False)
        with patch("user_strategies.check_prediction_pair.LiveTrader", return_value=context), \
                patch("user_strategies.check_prediction_pair.auto_btc", side_effect=LiveConfirmationPending(record)) as scanner:
            with self.assertRaises(LiveRunComplete):
                await live_btc(args)
        trader.wait_confirmation.assert_awaited_once_with(record)
        trader.wait_settlement.assert_awaited_once_with(settlement)
        scanner.assert_awaited_once()

    async def test_live_run_waits_for_resolution_before_final_exit(self):
        from user_strategies.prediction_trading import LiveSettlementPending, LiveRunComplete
        trader = SimpleNamespace(confirmation_waiting={}, settlement_waiting={}, wait_settlement=AsyncMock(), check_balances=AsyncMock())
        context = AsyncMock()
        context.__aenter__.return_value = trader
        record = {"attempt": "entry", "stop_after": True}
        args = argparse.Namespace(max_trades=1, preflight_only=False)
        with patch("user_strategies.check_prediction_pair.LiveTrader", return_value=context), \
                patch("user_strategies.check_prediction_pair.auto_btc", side_effect=LiveSettlementPending(record)) as scanner:
            with self.assertRaises(LiveRunComplete):
                await live_btc(args)
        scanner.assert_awaited_once()
        trader.wait_settlement.assert_awaited_once_with(record)
        trader.check_balances.assert_not_awaited()

    async def test_resumed_settlement_never_starts_a_new_quote_scan(self):
        from user_strategies.prediction_trading import LiveRunComplete
        record = {"attempt": "entry", "stop_after": True}
        trader = SimpleNamespace(confirmation_waiting={}, settlement_waiting={"entry": record}, wait_settlement=AsyncMock())
        context = AsyncMock()
        context.__aenter__.return_value = trader
        args = argparse.Namespace(max_trades=1, preflight_only=False)
        with patch("user_strategies.check_prediction_pair.LiveTrader", return_value=context), \
                patch("user_strategies.check_prediction_pair.auto_btc") as scanner:
            with self.assertRaises(LiveRunComplete):
                await live_btc(args)
        trader.wait_settlement.assert_awaited_once_with(record)
        scanner.assert_not_awaited()

    async def test_settlement_signal_closes_quote_check_without_reconnect_or_halt(self):
        from user_strategies.prediction_trading import LiveSettlementPending
        closed = []
        args = argparse.Namespace(seconds=0, live_trader=SimpleNamespace(pending=True))
        def discover(start):
            return {"polymarket": "1", "predictfun": "2", "end": start + 300,
                    "polymarket_rule": None, "predictfun_rule": None}
        async def check(config, window_end):
            try:
                raise LiveSettlementPending({"attempt": "entry"})
            finally:
                closed.append(True)
        with patch.dict("os.environ", {"PREDICT_FUN_API_KEY": "fixture"}), \
                patch("user_strategies.check_prediction_pair.time.time", return_value=601):
            with self.assertRaises(LiveSettlementPending):
                await auto_btc(args, discover, check)
        self.assertEqual(closed, [True])

    async def test_switches_window_only_after_old_check_has_closed(self):
        clock = {"wall": 601, "mono": 0}
        calls, closed = [], []
        args = argparse.Namespace(seconds=10, quantity=Decimal(10), polymarket_side="yes", predictfun_side="no", auto_btc=True)
        def discover(start):
            return {"polymarket": str(start), "predictfun": str(start + 1), "start": start,
                    "end": start + 300, "polymarket_rule": "TWAP", "predictfun_rule": "close"}
        async def check(config, window_end):
            self.assertEqual(len(calls), len(closed))
            calls.append((config.polymarket, config.predictfun, window_end))
            try:
                clock["wall"] = window_end + 0.01
                clock["mono"] += 1 if len(calls) == 1 else 10
            finally:
                closed.append(config.polymarket)
        with patch.dict("os.environ", {"PREDICT_FUN_API_KEY": "fixture"}), \
                patch("user_strategies.check_prediction_pair.time.time", side_effect=lambda: clock["wall"]), \
                patch("user_strategies.check_prediction_pair.time.monotonic", side_effect=lambda: clock["mono"]):
            await auto_btc(args, discover, check)
        self.assertEqual(calls, [("600", "601", 900), ("900", "901", 1200)])
        self.assertEqual(closed, ["600", "900"])

    async def test_deadline_cancels_and_closes_pending_subscription(self):
        closed = []
        args = argparse.Namespace(seconds=0.5, quantity=Decimal(10), polymarket_side="yes", predictfun_side="no", auto_btc=True)
        def discover(start):
            return {"polymarket": "1", "predictfun": "2", "end": start + 300,
                    "polymarket_rule": None, "predictfun_rule": None}
        async def check(config, window_end):
            try: await asyncio.Future()
            finally: closed.append(True)
        with patch.dict("os.environ", {"PREDICT_FUN_API_KEY": "fixture"}), \
                patch("user_strategies.check_prediction_pair.time.time", return_value=601):
            await auto_btc(args, discover, check)
        self.assertEqual(closed, [True])
