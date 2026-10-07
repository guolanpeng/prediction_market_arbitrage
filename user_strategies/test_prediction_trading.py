import asyncio
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from synpath.trading.polymarket import wire_leg
from synpath.trading.types import OrderStatus, SettlementState, TimeInForce
from user_strategies.prediction_trading import LiveTrader, LiveTradeHalt, LiveRunComplete, LiveSettlementPending, LiveConfirmationPending, make_request, live_entry_reason, fill_inventory, sell_quantity
from user_strategies.test_check_prediction_pair import asks
from user_strategies.check_prediction_pair import calculate



class RequestTest(unittest.TestCase):
    def test_rejection_details_keep_sdk_reason_and_never_emit_provider_secrets(self):
        import json
        from synpath.trading.errors import OrderRejected
        from user_strategies.prediction_trading import safe_rejection

        secret = "sensitive-private-key-or-jwt"
        error = OrderRejected("provider body: " + secret, reason="fak_no_match",
                              body={"signature": secret, "api_key": secret}, status=400)
        details = safe_rejection(error)
        self.assertEqual(details["venue_reason"], "FAK_NO_MATCH")
        self.assertEqual(details["http_status"], 400)
        self.assertNotIn(secret, json.dumps(details))
        error = OrderRejected("refused", body={"error": "unknown refusal " + secret})
        details = safe_rejection(error)
        self.assertEqual(details["venue_reason"], "UNCLASSIFIED")
        self.assertNotIn(secret, json.dumps(details))
        error = OrderRejected("refused", body={"error": {"code": "invalid_signature", "signature": secret}})
        self.assertEqual(safe_rejection(error)["venue_reason"], "INVALID_SIGNATURE")

    def test_share_fee_is_subtracted_from_sellable_inventory_and_rounded_down(self):
        fill = SimpleNamespace(amount=Decimal(10), fee=Decimal("0.10303030303030303"), fee_currency="shares")
        inventory = fill_inventory([fill])
        self.assertEqual(inventory["net_shares"], Decimal("9.89696969696969697"))
        self.assertEqual(sell_quantity(1, inventory["net_shares"]), Decimal("9.8969"))
        self.assertEqual(sell_quantity(0, inventory["net_shares"]), Decimal("9.89"))
        fill.fee_currency = "USDT"
        self.assertEqual(fill_inventory([fill])["net_shares"], Decimal(10))
        fill.fee_currency, fill.fee = "shares", None
        with self.assertRaises(ValueError): fill_inventory([fill])
    def test_live_threshold_is_one_dollar_total_after_fees_and_buffer(self):
        now = time.time()
        window = {"end": now + 200}
        configs = [{"model": "quadratic_theta", "rate": Decimal("0.07"), "places": "0.00001"},
                   {"model": "min_price", "rate": Decimal("0.02"), "places": "0.000001"}]
        result = calculate(asks(("0.4", "10")), asks(("0.46", "10")), Decimal(10), configs)
        self.assertLess(result["net"], Decimal(2))
        self.assertGreater(result["net"] - Decimal("0.02"), Decimal(1))
        self.assertIsNone(live_entry_reason(window, result, [now * 1000, now * 1000], now))
        result["net"] = Decimal("1.02")
        self.assertEqual(live_entry_reason(window, result, [now * 1000, now * 1000], now), "NET_TOTAL_NOT_ABOVE_1_AFTER_BUFFER")
        result["net"] = Decimal("1.020001")
        self.assertIsNone(live_entry_reason(window, result, [now * 1000, now * 1000], now))
        result["quantity"] = Decimal(5)
        result["net"] = Decimal("0.75")
        self.assertEqual(live_entry_reason(window, result, [now * 1000, now * 1000], now), "NET_TOTAL_NOT_ABOVE_1_AFTER_BUFFER")
    def test_explicit_yes_no_prices_and_reduce_only_mapping(self):
        for venue in ("polymarket", "predict_fun"):
            for outcome in ("yes", "no"):
                for action, native_action in (("buy", "BUY"), ("sell", "SELL")):
                    request = make_request(venue, "123", outcome, action, Decimal(10), Decimal("0.43"), "intent")
                    self.assertEqual(wire_leg(request), (outcome, native_action))
                    self.assertEqual(request.price, Decimal("0.43") if outcome == "yes" else Decimal("0.57"))
                    self.assertEqual(request.reduce_only, action == "sell")
                    self.assertEqual(request.time_in_force, TimeInForce.GTC if action == "buy" else TimeInForce.IOC)

    def test_invalid_request_values(self):
        for amount, price in (("NaN", "0.5"), ("0", "0.5"), ("10", "1"), ("10", "Infinity")):
            with self.assertRaises(ValueError):
                make_request("polymarket", "1", "yes", "buy", Decimal(amount), Decimal(price), "intent")



class ExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_dropped_reduce_only_flag_does_not_invert_yes_exit_cash_value(self):
        from synpath.trading.types import Order, OrderType, Fill, Side
        with tempfile.TemporaryDirectory() as directory:
            trader, _, _ = self.fixtures(directory)
            order = Order(id="exit", venue="polymarket", market_id="polymarket:1", side=Side.SELL,
                          amount=Decimal("13.75"), type=OrderType.LIMIT, time_in_force=TimeInForce.IOC,
                          status=OrderStatus.PENDING, price=Decimal("0.03"), reduce_only=False)
            trader.adapters[0].create_order.return_value = order
            trader.adapters[0].fetch_order.return_value = SimpleNamespace(status=OrderStatus.CLOSED, filled=Decimal("13.75"), info={})
            class Page(list): next_cursor = None
            trader.adapters[0].fetch_my_trades.return_value = Page([
                Fill(id="trade", order_id="exit", venue="polymarket", market_id="polymarket:1", side=Side.SELL,
                     amount=Decimal("13.75"), price=Decimal("0.03"), timestamp=1000,
                     fee=Decimal("0.028"), fee_currency="pUSD", settlement=SettlementState.CONFIRMED)])
            request = make_request("polymarket", "1", "yes", "sell", Decimal("13.75"), Decimal("0.03"), "exit")
            del trader.send
            sent = await trader.send(0, request, "attempt", "UNWIND_ORDER")
            self.assertEqual(await trader.confirmed(0, sent), Decimal("13.75"))
            self.assertEqual(trader.inventories["exit"]["cash_value"], Decimal("0.4125"))
            self.assertEqual(trader.inventories["exit"]["cash_fee"], Decimal("0.028"))
            self.assertTrue(sent.reduce_only)
            # Old journal rows also lost the flag; UNWIND_ORDER identifies their action.
            resumed = LiveTrader(trader.path)
            resumed.adapters = trader.adapters
            self.assertEqual(await resumed.confirmed(0, order), Decimal("13.75"))
            self.assertEqual(resumed.inventories["exit"]["cash_value"], Decimal("0.4125"))

            no_order = order.model_copy(update={"id": "no-exit", "side": Side.BUY, "price": Decimal("0.97")})
            trader.adapters[0].create_order.return_value = no_order
            trader.adapters[0].fetch_my_trades.return_value = Page([
                Fill(id="no-trade", order_id="no-exit", venue="polymarket", market_id="polymarket:1", side=Side.BUY,
                     amount=Decimal("13.75"), price=Decimal("0.97"), timestamp=1000,
                     fee=Decimal("0.028"), fee_currency="pUSD", settlement=SettlementState.CONFIRMED)])
            no_request = make_request("polymarket", "1", "no", "sell", Decimal("13.75"), Decimal("0.03"), "no-exit")
            sent = await trader.send(0, no_request, "attempt", "UNWIND_ORDER")
            self.assertEqual(await trader.confirmed(0, sent), Decimal("13.75"))
            self.assertEqual(trader.inventories["no-exit"]["cash_value"], Decimal("0.4125"))

    async def test_entry_and_exit_journal_store_specific_refusal_without_retry(self):
        from synpath.trading.errors import OrderRejected
        with tempfile.TemporaryDirectory() as directory:
            trader, _, _ = self.fixtures(directory)
            error = OrderRejected("no orders found to match", reason="fak_no_match", status=400)
            trader.adapters[0].create_order.side_effect = error
            request = make_request("polymarket", "1", "yes", "sell", Decimal(10), Decimal("0.23"), "exit")
            del trader.send  # Exercise the real bridge, not the fixture's unwind mock.
            self.assertEqual(await trader.send(0, request, "test", "UNWIND_ORDER"), "REJECTED")
            self.assertEqual(trader.rows[-1]["venue_reason"], "FAK_NO_MATCH")
            trader.adapters[0].create_order.assert_awaited_once()
            plan = {"venue": "polymarket", "cash_budget": Decimal(3), "target_shares": Decimal(10)}
            with patch("user_strategies.prediction_cash_orders.submit_cash_buy", side_effect=error) as submit:
                self.assertEqual(await trader.send_cash(0, plan, "test"), "REJECTED")
            self.assertEqual(trader.rows[-1]["http_status"], 400)
            self.assertEqual(trader.rows[-1]["venue_reason"], "FAK_NO_MATCH")
            submit.assert_awaited_once()

    def fixtures(self, directory):
        trader = LiveTrader(Path(directory) / "orders.jsonl", max_trades=10)
        trader.adapters = [AsyncMock(), AsyncMock()]
        for adapter in trader.adapters:
            adapter.fetch_balance.return_value = SimpleNamespace(available=Decimal(50))
        now = time.time()
        window = {"start": int(now) // 300 * 300, "end": now + 200, "polymarket": "1", "predictfun": "2"}
        candidate = {"sides": ["yes", "no"], "timestamps": [now * 1000, now * 1000],
                     "asks": [asks(("0.3", "10")), asks(("0.4", "10"))],
                     "configs": [{"model": "quadratic_theta", "rate": Decimal("0.07"), "places": "0.00001"},
                                 {"model": "min_price", "rate": Decimal("0.02"), "places": "0.000001"}]}
        candidate["ticks"] = [Decimal("0.01"), Decimal("0.01")]
        candidate["result"] = calculate(*candidate["asks"], Decimal(10), candidate["configs"])
        trader.adapters[0].market_tokens.return_value = SimpleNamespace(token=lambda side: "123", neg_risk=False)
        trader.adapters[0].token_info.return_value = SimpleNamespace(tick=Decimal("0.01"), min_size=Decimal(5))
        trader.available_shares = AsyncMock(return_value=Decimal(50))
        trader.send = AsyncMock(return_value=SimpleNamespace(id="sale"))
        trader.check_polymarket_buy_approval = AsyncMock()
        trader.check_predict_buy_approval = AsyncMock()
        return trader, window, candidate

    async def test_sends_both_cash_market_orders_concurrently_without_gtc_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            arrived = [asyncio.Event(), asyncio.Event()]
            async def submit(adapter, plan, before):
                index = trader.adapters.index(adapter)
                arrived[index].set()
                await arrived[1 - index].wait()
                self.assertEqual(plan["cash_budget"], Decimal(3) if index == 0 else Decimal(4))
                self.assertEqual(plan["target_shares"], Decimal(10))
                before(str(index), plan["cash_budget"], Decimal(1))
                return SimpleNamespace(id=str(index), venue=plan["venue"], market_id=plan["market_id"],
                                       side="buy" if index == 0 else "sell", status=OrderStatus.PENDING,
                                       amount=Decimal(1), filled=Decimal(0), remaining=Decimal(1),
                                       price=Decimal("0.99"), reduce_only=False)
            trader.wait_fills = AsyncMock(return_value=[Decimal(10), Decimal(10)])
            with patch("user_strategies.prediction_cash_orders.submit_cash_buy", side_effect=submit):
                with self.assertRaises(LiveSettlementPending):
                    await asyncio.wait_for(trader.execute(window, candidate, Decimal(10), lambda i, s: []), 2)
            self.assertEqual(trader.rows[0]["execution"], "CONCURRENT_CASH_MARKET")
            self.assertEqual({r["leg"]:r["cash_budget"] for r in trader.rows if r["type"]=="CASH_SUBMIT_INTENT"}, {0:Decimal(3), 1:Decimal(4)})
            for adapter in trader.adapters:
                adapter.cancel_order.assert_not_called()
            with self.assertRaises(LiveTradeHalt):
                await trader.execute(window, candidate, Decimal(10), lambda i, s: [])
            self.assertFalse(any(row["type"] == "DONE" for row in trader.rows))

    async def test_unknown_fill_enters_read_only_confirmation_without_blind_unwind(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            trader.send_cash = AsyncMock(return_value=None)
            trader.wait_fills = AsyncMock(return_value=[Decimal(10), None])
            with self.assertRaises(LiveConfirmationPending):
                await trader.execute(window, candidate, Decimal(10), lambda i, s: self.fail("Must not blind unwind"))
            self.assertEqual(trader.send_cash.await_count, 2)
            self.assertEqual(trader.attempts, 1)
            resumed = LiveTrader(trader.path)
            self.assertTrue(resumed.pending)
            self.assertEqual(len(resumed.confirmation_waiting), 1)
            self.assertEqual(trader.rows[-2]["confirmed_net_shares"][1], None)

    async def test_timeout_persists_order_ids_for_read_only_restart(self):
        from synpath.trading.types import Order, OrderType
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            trader.send_cash = AsyncMock(side_effect=[SimpleNamespace(id="poly"), SimpleNamespace(id="predict")])
            trader.wait_fills = AsyncMock(return_value=[None, Decimal(10)])
            with self.assertRaises(LiveConfirmationPending):
                await trader.execute(window, candidate, Decimal(10), lambda i, s: self.fail("No blind unwind"))
            resumed = LiveTrader(trader.path)
            record = next(iter(resumed.confirmation_waiting.values()))
            order = Order(**{**record["orders"][0], "type": OrderType.MARKET, "time_in_force": TimeInForce.IOC})
            self.assertEqual(order.id, "poly")
            self.assertEqual(order.market_id, "polymarket:1")
            self.assertIsNone(trader.rows[-2]["cash_values"][0])

    async def test_share_fee_face_value_can_reject_edge_that_cash_fee_estimate_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            candidate["asks"] = [asks(("0.77", "10")), asks(("0.1", "10"))]
            candidate["result"] = calculate(*candidate["asks"], Decimal(10), candidate["configs"])
            self.assertGreater(candidate["result"]["net"] - Decimal("0.02"), Decimal(1))
            self.assertEqual(await trader.execute(window, candidate, Decimal(10), lambda i, s: []), "LIMIT_EDGE_NOT_ELIGIBLE")
            for adapter in trader.adapters:
                adapter.create_order.assert_not_called()

    async def test_unwind_uses_net_receipt_and_onchain_balance_not_gross_ten(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            trader.send_cash = AsyncMock(side_effect=["REJECTED", SimpleNamespace(id="buy")])
            trader.wait_fills = AsyncMock(return_value=[Decimal(0), Decimal(10)])
            trader.inventories["buy"] = {"gross_shares": Decimal(10), "share_fees": Decimal("0.10303030303030303"),
                                         "net_shares": Decimal("9.89696969696969697")}
            trader.available_shares.return_value = Decimal("9.89696969696969697")
            trader.confirmed = AsyncMock(return_value=Decimal("9.8969"))
            with self.assertRaises(LiveSettlementPending):
                await trader.execute(window, candidate, Decimal(10), lambda i, s: asks(("0.63", "20")))
            request = trader.send.await_args_list[-1].args[1]
            self.assertEqual(request.amount, Decimal("9.8969"))
            self.assertTrue(request.reduce_only)
            self.assertEqual(trader.rows[-2]["confirmed_sold"], Decimal("9.8969"))
            fill_row = next(row for row in trader.rows if row["type"] == "FILLS")
            self.assertEqual(fill_row["confirmed_net_shares"][1], Decimal("9.89696969696969697"))

    async def test_single_confirmed_fill_attempts_reduce_only_sale_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            trader.send_cash = AsyncMock(return_value=SimpleNamespace(id="order"))
            trader.wait_fills = AsyncMock(return_value=[Decimal(0), Decimal(10)])
            trader.confirmed = AsyncMock(return_value=Decimal(10))
            with self.assertRaises(LiveSettlementPending):
                await trader.execute(window, candidate, Decimal(10), lambda i, s: asks(("0.35", "10")))
            request = trader.send.await_args_list[-1].args[1]
            self.assertEqual(wire_leg(request), ("no", "SELL"))
            self.assertTrue(request.reduce_only)
            self.assertEqual(request.time_in_force, TimeInForce.IOC)
            self.assertEqual(trader.rows[-2]["confirmed_sold"], Decimal(10))

    async def test_low_balance_and_protected_depth_prevent_order_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            trader.adapters[0].fetch_balance.return_value.available = Decimal("14.99")
            with self.assertRaises(LiveTradeHalt):
                await trader.execute(window, candidate, Decimal(10), lambda i, s: [])
            trader.adapters[0].create_order.assert_not_called()
            trader.adapters[0].fetch_balance.return_value.available = Decimal(15)
            candidate["asks"][0] = asks(("0.1", "1"), ("0.6", "9"))
            candidate["result"] = calculate(*candidate["asks"], Decimal(10), candidate["configs"])
            self.assertLess(candidate["result"]["net_per_pair"], Decimal("0.20"))
            self.assertEqual(await trader.execute(window, candidate, Decimal(10), lambda i, s: []), "QUOTE_NOT_ELIGIBLE")
            trader.adapters[0].create_order.assert_not_called()

    async def test_matched_fill_is_not_confirmed_and_duplicate_fill_is_not_double_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, _, _ = self.fixtures(directory)
            order = SimpleNamespace(id="order", market_id="polymarket:1", amount=Decimal(10))
            trader.adapters[0].fetch_order.return_value = SimpleNamespace(status=OrderStatus.CLOSED, filled=Decimal(10))
            fill = SimpleNamespace(id="fill", amount=Decimal(10), settlement=SettlementState.MATCHED)
            class Page(list): next_cursor = None
            trader.adapters[0].fetch_my_trades.return_value = Page([fill, fill])
            self.assertIsNone(await trader.confirmed(0, order))
            fill.settlement = SettlementState.CONFIRMED
            self.assertEqual(await trader.confirmed(0, order), Decimal(10))

    async def test_fifteen_is_allowed_but_either_venue_below_fifteen_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, _, _ = self.fixtures(directory)
            for adapter in trader.adapters:
                adapter.fetch_balance.return_value.available = Decimal(15)
            balances = await trader.check_balances([Decimal(10), Decimal(10)])
            self.assertEqual([balance.available for balance in balances], [Decimal(15), Decimal(15)])
            self.assertGreater(trader.balance_checked_at, 0)
            with self.assertRaises(LiveTradeHalt):
                await trader.check_balances([Decimal(16), Decimal(10)])
            for index in (0, 1):
                trader.adapters[index].fetch_balance.return_value.available = Decimal("14.999999")
                with self.assertRaises(LiveTradeHalt): await trader.check_balances()
                trader.adapters[index].fetch_balance.return_value.available = Decimal(15)

    async def test_previous_purchase_totals_do_not_cap_new_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, _ = self.fixtures(directory)
            trader.write("INTENT", "historical", window_start=window["start"] - 300, reserved=[Decimal(500), Decimal(500)])
            trader.write("DONE", "historical", state="PAIRED")
            resumed = LiveTrader(trader.path)
            resumed.adapters = trader.adapters
            await resumed.check_balances([Decimal(10), Decimal(10)])
            self.assertNotIn(window["start"], resumed.windows)

    async def test_run_limit_counts_attempts_including_no_fill_and_exits_after_confirmation(self):
        for sizes in ([Decimal(10), Decimal(10)], [Decimal(0), Decimal(0)]):
            with tempfile.TemporaryDirectory() as directory:
                trader, window, candidate = self.fixtures(directory)
                trader.max_trades = 1
                trader.send_cash = AsyncMock(return_value="REJECTED")
                trader.wait_fills = AsyncMock(return_value=sizes)
                with self.assertRaises(LiveSettlementPending if any(sizes) else LiveRunComplete):
                    await trader.execute(window, candidate, Decimal(10), lambda i, s: [])
                self.assertEqual(trader.attempts, 1)
                self.assertEqual(trader.pending, bool(any(sizes)))
                self.assertEqual(trader.rows[-1]["type"], "AWAIT_SETTLEMENT" if any(sizes) else "DONE")
                self.assertEqual(trader.rows[0]["run_attempt"], 1)
                self.assertEqual(trader.rows[0]["run_max_trades"], 1)
                with self.assertRaises(LiveTradeHalt if any(sizes) else LiveRunComplete):
                    await trader.execute({**window, "start": window["start"] + 300}, candidate, Decimal(10), lambda i, s: [])
                self.assertEqual(trader.send_cash.await_count, 2)
                restarted = LiveTrader(trader.path)
                self.assertEqual(restarted.attempts, 0)

    async def test_skipped_quotes_do_not_consume_run_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            candidate["asks"] = [asks(("0.5", "10")), asks(("0.5", "10"))]
            candidate["result"] = calculate(*candidate["asks"], Decimal(10), candidate["configs"])
            self.assertEqual(await trader.execute(window, candidate, Decimal(10), lambda i, s: []), "QUOTE_NOT_ELIGIBLE")
            self.assertEqual(trader.attempts, 0)
            for invalid in (0, -1):
                with self.assertRaises(ValueError): LiveTrader(trader.path, max_trades=invalid)

    async def test_two_attempt_limit_stops_before_a_third_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            trader.max_trades = 2
            trader.send_cash = AsyncMock(return_value="REJECTED")
            trader.wait_fills = AsyncMock(return_value=[Decimal(0), Decimal(0)])
            self.assertEqual(await trader.execute(window, candidate, Decimal(10), lambda i, s: []), "NO_FILL")
            with self.assertRaises(LiveRunComplete):
                await trader.execute({**window, "start": window["start"] + 300}, candidate, Decimal(10), lambda i, s: [])
            with self.assertRaises(LiveRunComplete):
                await trader.execute({**window, "start": window["start"] + 600}, candidate, Decimal(10), lambda i, s: [])
            self.assertEqual(trader.attempts, 2)
            self.assertEqual(trader.send_cash.await_count, 4)

    async def test_cancellation_after_intent_blocks_new_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            trader.send_cash = AsyncMock(side_effect=asyncio.CancelledError)
            with self.assertRaises(asyncio.CancelledError):
                await trader.execute(window, candidate, Decimal(10), lambda i, s: [])
            with self.assertRaises(LiveTradeHalt):
                await trader.execute({**window, "start": window["start"] + 300}, candidate, Decimal(10), lambda i, s: [])
            with self.assertRaises(LiveTradeHalt): LiveTrader(trader.path)

    async def test_partial_second_unwinds_only_the_excess(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            trader.send_cash = AsyncMock(return_value=SimpleNamespace(id="order"))
            trader.wait_fills = AsyncMock(return_value=[Decimal(6), Decimal(10)])
            trader.confirmed = AsyncMock(return_value=Decimal(4))
            with self.assertRaises(LiveSettlementPending):
                await trader.execute(window, candidate, Decimal(10), lambda i, s: asks(("0.35", "10")))
            exit_request = trader.send.await_args_list[-1].args[1]
            self.assertEqual(exit_request.amount, Decimal(4))
            self.assertTrue(exit_request.reduce_only)

    async def test_terminal_ioc_partial_fill_is_confirmed_without_waiting_for_full_request(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, _, _ = self.fixtures(directory)
            order = SimpleNamespace(id="order", market_id="polymarket:1", amount=Decimal(10))
            trader.adapters[0].fetch_order.return_value = SimpleNamespace(status=OrderStatus.CLOSED, filled=Decimal(6))
            class Page(list): next_cursor = None
            trader.adapters[0].fetch_my_trades.return_value = Page([
                SimpleNamespace(id="fill", amount=Decimal(6), settlement=SettlementState.CONFIRMED)])
            self.assertEqual(await trader.confirmed(0, order), Decimal(6))

    async def test_share_limit_uses_refreshed_tick_and_unknown_tick_prevents_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            trader.adapters[0].token_info.return_value.tick = Decimal("0.001")
            plans = await trader.cash_buy_plans(window, candidate, Decimal(10))
            self.assertEqual([plan["cash_budget"] for plan in plans], [Decimal(3), Decimal(4)])
            trader.adapters[0].token_info.assert_awaited_with("123", refresh=True)
            candidate["ticks"][1] = None
            self.assertEqual(await trader.execute(window, candidate, Decimal(10), lambda i, s: []), "MARKET_CONSTRAINTS_NOT_VERIFIED")
            for adapter in trader.adapters:
                adapter.create_order.assert_not_called()

    async def test_equal_partial_market_fills_are_paired_without_top_up(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            trader.send_cash = AsyncMock(return_value=SimpleNamespace(id="order"))
            trader.wait_fills = AsyncMock(return_value=[Decimal(6), Decimal(6)])
            with self.assertRaises(LiveSettlementPending):
                await trader.execute(window, candidate, Decimal(10), lambda i, s: [])
            self.assertEqual(trader.send_cash.await_count, 2)
            self.assertEqual(trader.rows[-2]["confirmed_shares"], [Decimal(6), Decimal(6)])

    async def test_half_share_net_difference_is_accepted_but_one_empty_leg_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            trader.send_cash = AsyncMock(side_effect=[SimpleNamespace(id="poly"), SimpleNamespace(id="predict")])
            trader.wait_fills = AsyncMock(return_value=[Decimal(10), Decimal("9.7")])
            trader.inventories["predict"] = {"gross_shares": Decimal("9.7"), "net_shares": Decimal("9.5"), "share_fees": Decimal("0.2")}
            with self.assertRaises(LiveSettlementPending):
                await trader.execute(window, candidate, Decimal(10), lambda i, s: [])
            trader.send.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            trader.send_cash = AsyncMock(return_value=SimpleNamespace(id="entry"))
            trader.wait_fills = AsyncMock(return_value=[Decimal(0), Decimal("0.4")])
            trader.confirmed = AsyncMock(return_value=Decimal("0.4"))
            with self.assertRaises(LiveSettlementPending):
                await trader.execute(window, candidate, Decimal(10), lambda i, s: asks(("0.3", "10")))
            trader.send.assert_awaited_once()

    async def test_cash_market_waits_for_terminal_status_not_signed_minimum_share_count(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, _, _ = self.fixtures(directory)
            order = SimpleNamespace(id="cash", side="buy", market_id="polymarket:1", amount=Decimal(1), reduce_only=False)
            trader.cash_orders["cash"] = {"cash_budget": Decimal(3)}
            trader.adapters[0].fetch_order.return_value = SimpleNamespace(status=OrderStatus.OPEN, filled=Decimal(2), info={})
            class Page(list): next_cursor = None
            trader.adapters[0].fetch_my_trades.return_value = Page([SimpleNamespace(id="fill", amount=Decimal(2), price=Decimal("0.3"), settlement=SettlementState.CONFIRMED)])
            self.assertIsNone(await trader.confirmed(0, order))
            trader.adapters[0].fetch_order.return_value.status = OrderStatus.CANCELED
            self.assertEqual(await trader.confirmed(0, order), Decimal(2))

    async def test_cash_budget_below_one_dollar_does_not_round_up_and_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            candidate["asks"][0] = asks(("0.05", "20"))
            candidate["result"] = calculate(*candidate["asks"], Decimal(10), candidate["configs"])
            self.assertEqual(await trader.execute(window, candidate, Decimal(10), lambda i, s: []), "MARKET_CONSTRAINTS_NOT_VERIFIED")
            self.assertFalse(trader.rows)

    async def test_zero_predict_allowance_blocks_both_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, candidate = self.fixtures(directory)
            del trader.check_predict_buy_approval
            adapter = trader.adapters[1]
            adapter.address = "0x" + "1" * 40
            adapter.network = SimpleNamespace(usdt="0x" + "2" * 40, exchange=lambda **kwargs: "0x" + "3" * 40)
            adapter.rpc_url = "https://example.invalid"
            adapter.market_info.return_value = SimpleNamespace(neg_risk=False, yield_bearing=False)
            adapter.rpc.post.return_value = SimpleNamespace(json=lambda: {"result": "0x0"})
            with self.assertRaises(LiveTradeHalt):
                await trader.execute(window, candidate, Decimal(10), lambda i, s: [])
            self.assertFalse(trader.rows)
            for venue in trader.adapters:
                venue.create_order.assert_not_called()

    async def test_reconciled_incident_keeps_history_but_does_not_permanently_block_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, window, _ = self.fixtures(directory)
            trader.write("INTENT", "incident", window_start=window["start"], reserved=[Decimal(10), Decimal(10)])
            trader.write("HALT", "incident", reason="FILL_EXCEEDS_REQUEST")
            with self.assertRaises(LiveTradeHalt): LiveTrader(trader.path)
            trader.write("RECONCILED", "incident", state="TERMINAL_MARKET_RESOLVED", evidence="verified official outcomes and peer orders")
            resumed = LiveTrader(trader.path)
            self.assertEqual([row["type"] for row in resumed.rows], ["INTENT", "HALT", "RECONCILED"])
            self.assertIn(window["start"], resumed.windows)



class SettlementWaitTest(unittest.IsolatedAsyncioTestCase):
    def waiting(self, directory, *, remaining=None, fees=None):
        trader = LiveTrader(Path(directory) / "orders.jsonl", max_trades=1)
        trader.pending = True
        window = {"start": 600, "end": 900, "polymarket": "1", "predictfun": "2"}
        trader.write("INTENT", "entry", window_start=600)
        cash_fees = fees if fees is not None else [Decimal("0.1"), Decimal(0)]
        entries = [{"cash_value": Decimal(3), "cash_fee": cash_fees[0]},
                   {"cash_value": Decimal(4), "cash_fee": cash_fees[1]}]
        with self.assertRaises(LiveSettlementPending) as raised:
            trader.await_settlement("entry", window, ["yes", "no"], remaining or [Decimal(10), Decimal("9.8")],
                                    entries, [], stop_after=True, state="PAIRED")
        return trader, raised.exception.record

    async def test_does_not_finish_on_deadline_or_first_unresolved_response(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, record = self.waiting(directory)
            async def sleep(_):
                self.assertTrue(trader.pending)
                self.assertFalse(any(row["type"] == "DONE" for row in trader.rows))
            with patch("user_strategies.prediction_paper.fetch_settlement", side_effect=[None, RuntimeError(), [Decimal(1), Decimal(0)]]) as fetch, \
                    patch("user_strategies.prediction_trading.time.time", return_value=901), \
                    patch("user_strategies.prediction_trading.asyncio.sleep", side_effect=sleep) as pause:
                await trader.wait_settlement(record)
            self.assertEqual(fetch.call_count, 3)
            self.assertEqual(pause.await_count, 2)
            self.assertEqual(trader.rows[-2]["net_pnl"], Decimal("2.9"))
            self.assertEqual(trader.rows[-1]["state"], "SETTLED")
            self.assertFalse(trader.pending)

    async def test_remaining_quantity_after_unwind_drives_payout_not_original_ten(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, record = self.waiting(directory, remaining=[Decimal(6), Decimal("5.8")])
            record.update(exit_cash_values=[Decimal("1.2")], exit_cash_fees=[Decimal("0.02")])
            with patch("user_strategies.prediction_paper.fetch_settlement", return_value=[Decimal(0), Decimal(1)]), \
                    patch("user_strategies.prediction_trading.time.time", return_value=901):
                await trader.wait_settlement(record)
            settlement = trader.rows[-2]
            self.assertEqual(settlement["total_payout"], Decimal("5.8"))
            self.assertEqual(settlement["net_pnl"], Decimal("-0.12"))

    async def test_unknown_cash_fee_never_becomes_zero_or_verified_profit(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, record = self.waiting(directory, fees=[None, Decimal(0)])
            with patch("user_strategies.prediction_paper.fetch_settlement", return_value=[Decimal(1), Decimal(0)]), \
                    patch("user_strategies.prediction_trading.time.time", return_value=901):
                await trader.wait_settlement(record)
            self.assertEqual(trader.rows[-2]["gross_pnl"], Decimal(3))
            self.assertIsNone(trader.rows[-2]["net_pnl"])
            self.assertEqual(trader.rows[-2]["pnl_state"], "CASH_FLOW_OR_FEES_UNKNOWN")

    async def test_restart_resumes_waiting_and_still_blocks_new_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            trader, _ = self.waiting(directory)
            restarted = LiveTrader(trader.path)
            self.assertTrue(restarted.pending)
            self.assertIn("entry", restarted.settlement_waiting)
            with self.assertRaises(LiveTradeHalt):
                await restarted.execute({}, {}, Decimal(10), None)
            with patch("user_strategies.prediction_paper.fetch_settlement", return_value=[Decimal(0), Decimal(0)]), \
                    patch("user_strategies.prediction_trading.time.time", return_value=901):
                await restarted.wait_settlement(restarted.settlement_waiting["entry"])
            self.assertEqual(restarted.rows[-2]["net_pnl"], Decimal("-7.1"))
            self.assertFalse(LiveTrader(trader.path).pending)


class ConfirmationWaitTest(unittest.IsolatedAsyncioTestCase):
    def waiting(self, directory):
        from synpath.trading.types import Order, Side, OrderType
        from user_strategies.prediction_trading import safe_order
        trader = LiveTrader(Path(directory) / "orders.jsonl")
        trader.pending = True
        trader.write("INTENT", "delayed", window_start=600)
        trader.write("AWAIT_CONFIRMATION", "delayed", window={"start": 600, "end": 900, "polymarket": "1", "predictfun": "2"},
                     sides=["no", "yes"],
                     orders=[safe_order(Order(id="poly", venue="polymarket", market_id="polymarket:1", side=Side.SELL, amount=Decimal(10), type=OrderType.MARKET, time_in_force=TimeInForce.IOC, status=OrderStatus.PENDING)),
                             safe_order(Order(id="predict", venue="predict_fun", market_id="predict_fun:2", side=Side.BUY, amount=Decimal(10), type=OrderType.MARKET, time_in_force=TimeInForce.IOC, status=OrderStatus.PENDING))],
                     plans=[{"cash_budget": "2.3"}, {"cash_budget": "6.3"}])
        return LiveTrader(trader.path)

    async def test_missing_fills_remain_unknown_despite_zero_balance_then_resume_without_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            trader = self.waiting(directory)
            record = trader.confirmation_waiting["delayed"]
            trader.wait_fills = AsyncMock(side_effect=[[None, Decimal(10)], [Decimal(7), Decimal(10)]])
            trader.available_shares = AsyncMock(return_value=Decimal(0))
            trader.send = AsyncMock()
            trader.send_cash = AsyncMock()
            trader.inventories = {"poly": {"net_shares": Decimal(7), "cash_value": Decimal("2.3"), "cash_fee": None},
                                  "predict": {"net_shares": Decimal("9.8"), "cash_value": Decimal("5.9"), "cash_fee": Decimal(0)}}
            async def pause(_):
                self.assertTrue(trader.pending)
                self.assertEqual(trader.rows[-1]["type"], "AWAIT_CONFIRMATION")
            with patch("user_strategies.prediction_trading.asyncio.sleep", side_effect=pause):
                settlement = await trader.wait_confirmation(record)
            self.assertEqual(trader.wait_fills.await_count, 2)
            self.assertEqual(settlement["remaining_net_shares"], [Decimal(7), Decimal("9.8")])
            self.assertTrue(settlement["stop_after"])
            self.assertEqual(trader.rows[-2]["net_share_gap"], Decimal("2.8"))
            trader.send.assert_not_awaited()
            trader.send_cash.assert_not_awaited()
            self.assertTrue(LiveTrader(trader.path).settlement_waiting)

    async def test_cancelling_confirmation_preserves_resume_record(self):
        with tempfile.TemporaryDirectory() as directory:
            trader = self.waiting(directory)
            trader.wait_fills = AsyncMock(side_effect=asyncio.CancelledError)
            with self.assertRaises(asyncio.CancelledError):
                await trader.wait_confirmation(trader.confirmation_waiting["delayed"])
            resumed = LiveTrader(trader.path)
            self.assertTrue(resumed.confirmation_waiting)
            self.assertTrue(resumed.pending)
            self.assertFalse(any(row["type"] == "DONE" for row in resumed.rows))

    async def test_polymarket_missing_market_index_falls_back_to_associated_trade_id(self):
        from synpath.trading.types import Side
        with tempfile.TemporaryDirectory() as directory:
            trader = LiveTrader(Path(directory) / "orders.jsonl")
            adapter = AsyncMock()
            adapter._api_key = "fixture"
            adapter.wallet = "0x" + "1" * 40
            adapter.signer = SimpleNamespace(address=adapter.wallet)
            adapter.account = None
            trader.adapters = [adapter, AsyncMock()]
            order = SimpleNamespace(id="poly", market_id="polymarket:1", amount=Decimal("2.3232"), side=Side.SELL, reduce_only=False)
            trader.cash_orders[order.id] = {}
            adapter.fetch_order.return_value = SimpleNamespace(status=OrderStatus.CLOSED, filled=Decimal("7.833334"), info={"associate_trades": ["trade1"]})
            class Page(list): next_cursor = None
            adapter.fetch_my_trades.return_value = Page()
            adapter.market_tokens.return_value = SimpleNamespace(outcome_of=lambda token: "no")
            row = {"id": "trade1", "trader_side": "TAKER", "status": "CONFIRMED", "asset_id": "2", "side": "BUY", "price": "0.29", "size": "7.833334", "taker_order_id": "poly"}
            adapter._private.return_value = {"data": [row]}
            self.assertEqual(await trader.confirmed(0, order), Decimal("7.833334"))
            self.assertEqual(trader.inventories["poly"]["cash_value"], Decimal("2.27166686"))
            adapter._private.assert_awaited_with("GET", "/data/trades", params={"id": "trade1"})
            adapter.create_order.assert_not_awaited()
            # A different order's receipt must never satisfy this order.
            trader.inventories.clear()
            row["taker_order_id"] = "other-order"
            self.assertIsNone(await trader.confirmed(0, order))
            self.assertNotIn("poly", trader.inventories)


class WireUnitsTest(unittest.TestCase):
    def test_actual_signed_buy_no_is_gtc_ten_shares_not_9_90_market_budget(self):
        from synpath.trading.polymarket import TokenInfo, build_signed_order
        from synpath.trading.polymarket_signing import WalletSigner
        from synpath.trading.credentials import PolymarketCredentials

        key = "0x" + "1" * 64  # Test key, no funds/network.
        request = make_request("polymarket", "123", "no", "buy", Decimal(10), Decimal("0.17"), "test")
        info = TokenInfo(token_id="123", condition_id="0x" + "0" * 64, tick=Decimal("0.01"), min_size=Decimal(5), neg_risk=False)
        payload, _ = build_signed_order(request, info, signer=WalletSigner(key), creds=PolymarketCredentials(private_key=key),
                                       api_key="test", now_ms=1, salt=1)
        self.assertEqual(payload["orderType"], "GTC")
        self.assertEqual(payload["order"]["makerAmount"], "1700000")
        self.assertEqual(payload["order"]["takerAmount"], "10000000")

    def test_predict_actual_signed_payload_is_limit_with_ten_share_quantity(self):
        from synpath.trading.predict_fun import MarketInfo, build_signed_order
        from synpath.trading.predict_fun_signing import MAINNET
        from synpath.trading.polymarket_signing import WalletSigner

        signer = WalletSigner("0x" + "1" * 64)
        request = make_request("predict_fun", "123", "yes", "buy", Decimal(10), Decimal("0.55"), "test")
        info = MarketInfo(native="123", yes_token="1", no_token="2", neg_risk=False, yield_bearing=False, fee_rate_bps=200)
        payload, contracts = build_signed_order(request, info, signer=signer, maker=signer.address, network=MAINNET,
                                               account=None, now_s=1, salt=1)
        self.assertEqual(payload["data"]["strategy"], "LIMIT")
        self.assertEqual(payload["data"]["order"]["makerAmount"], "5500000000000000000")
        self.assertEqual(payload["data"]["order"]["takerAmount"], "10000000000000000000")
        self.assertEqual(contracts, Decimal(10))

    def test_approval_tool_never_accepts_unlimited_or_excessive_allowance(self):
        from user_strategies.approve_predict import allowance_amount
        self.assertEqual(allowance_amount("15"), 15 * 10**18)
        for invalid in ("0", "-1", "15.01", "NaN", "Infinity", str(2**256 - 1)):
            with self.assertRaises((ValueError, ArithmeticError)):
                allowance_amount(invalid)
