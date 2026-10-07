import unittest
from decimal import Decimal

from synpath.trading.polymarket import TokenInfo
from synpath.trading.polymarket_signing import WalletSigner
from synpath.trading.credentials import PolymarketCredentials
from synpath.trading.predict_fun import MarketInfo
from synpath.trading.predict_fun_signing import MAINNET
from user_strategies.prediction_cash_orders import poly_payload, predict_payload, cash_budget


class CashPayloadTest(unittest.TestCase):
    def setUp(self):
        self.key = "0x" + "1" * 64
        self.signer = WalletSigner(self.key)
        self.plan = {"venue": "polymarket", "market_id": "polymarket:123", "outcome": "no",
                     "target_shares": Decimal(10), "cash_budget": Decimal("1.70"), "expected_price": Decimal("0.17"), "tick": Decimal("0.01")}

    def test_poly_signs_1_70_dollars_not_9_90_even_at_wide_boundary(self):
        token = TokenInfo(token_id="123", condition_id="0x" + "0" * 64, tick=Decimal("0.01"), min_size=Decimal(5), neg_risk=False)
        for signature_type in (0, 3):
            credentials = PolymarketCredentials(private_key=self.key, signature_type=signature_type,
                                                funder="0x" + "2" * 40 if signature_type else None)
            body, order_id = poly_payload(self.plan, token, credentials, self.signer, "test", now_ms=1, salt=1)
            self.assertEqual(body["orderType"], "FAK")
            self.assertEqual(body["order"]["side"], "BUY")  # Buy NO token, never sell unheld YES.
            self.assertEqual(body["order"]["makerAmount"], "1700000")
            self.assertEqual(body["order"]["takerAmount"], "1717100")
            self.assertEqual(body["order"]["tokenId"], "123")
            self.assertTrue(order_id.startswith("0x"))

    def test_predict_signs_cash_budget_and_market_without_fill_or_kill(self):
        info = MarketInfo(native="123", yes_token="1", no_token="2", neg_risk=False, yield_bearing=False, fee_rate_bps=200)
        body, order_id = predict_payload(self.plan, info, self.signer, self.signer.address, MAINNET, None, now_s=1, salt=1)
        self.assertEqual(body["data"]["strategy"], "MARKET")
        self.assertEqual(body["data"]["order"]["makerAmount"], "1700000000000000000")
        self.assertEqual(body["data"]["order"]["tokenId"], "2")
        self.assertFalse(body["data"]["isMinAmountOut"])
        self.assertEqual(body["data"]["pricePerShare"], "170000000000000000")
        self.assertEqual(Decimal(body["data"]["order"]["makerAmount"]) / Decimal(body["data"]["pricePerShare"]), Decimal(10))
        self.assertNotIn("isFillOrKill", body["data"])
        self.assertEqual(int(body["data"]["order"]["takerAmount"]), 10 * 10**18)
        self.assertTrue(order_id.startswith("0x"))

    def test_predict_incident_3_20_budget_signs_ten_shares_instead_of_3_232323(self):
        from predict_sdk import OrderBuilder
        from predict_sdk.constants import ChainId, Side
        from predict_sdk.types import Book, MarketHelperInput

        self.plan.update(cash_budget=Decimal("3.20"), expected_price=Decimal("0.32"))
        info = MarketInfo(native="123", yes_token="1", no_token="2", neg_risk=False, yield_bearing=False, fee_rate_bps=200)
        body, _ = predict_payload(self.plan, info, self.signer, self.signer.address, MAINNET, None, now_s=1, salt=1)
        data = body["data"]
        official = OrderBuilder.make(ChainId.BNB_MAINNET).get_market_order_amounts(
            MarketHelperInput(side=Side.BUY, quantity_wei=10 * 10**18),
            Book(market_id=123, update_timestamp_ms=1, asks=[(0.32, 100)], bids=[]))
        self.assertEqual(int(data["order"]["makerAmount"]), official.maker_amount)
        self.assertEqual(int(data["order"]["takerAmount"]), official.taker_amount)
        self.assertEqual(int(data["pricePerShare"]), official.price_per_share)
        self.assertEqual(data["isMinAmountOut"], official.is_min_amount_out)
        self.assertEqual(Decimal(data["order"]["takerAmount"]) / 10**18, Decimal(10))
        # At the observed improved price, ten shares cost $2.80, within the $3.20 budget.
        self.assertLessEqual(Decimal(10) * Decimal("0.28"), Decimal(data["order"]["makerAmount"]) / 10**18)

    def test_predict_share_precision_rounds_down_and_budget_does_not_inflate(self):
        self.plan.update(target_shares=Decimal("10.000123"), cash_budget=Decimal("3.20"))
        info = MarketInfo(native="123", yes_token="1", no_token="2", neg_risk=False, yield_bearing=False, fee_rate_bps=200)
        body, _ = predict_payload(self.plan, info, self.signer, self.signer.address, MAINNET, None, now_s=1, salt=1)
        self.assertEqual(body["data"]["order"]["takerAmount"], str(10 * 10**18))
        self.assertEqual(body["data"]["order"]["makerAmount"], str(3200000000000000000))
        for target in ("NaN", "Infinity", "0", "-1"):
            self.plan["target_shares"] = Decimal(target)
            with self.assertRaises(ValueError):
                predict_payload(self.plan, info, self.signer, self.signer.address, MAINNET, None, now_s=1, salt=1)

    def test_rounding_never_increases_cash_budget_and_small_budget_is_rejected(self):
        self.assertEqual(cash_budget("1.709", "polymarket"), Decimal("1.70"))
        token = TokenInfo(token_id="123", condition_id="0x" + "0" * 64, tick=Decimal("0.01"), min_size=Decimal(5), neg_risk=False)
        self.plan["cash_budget"] = Decimal("0.99")
        with self.assertRaises(ValueError):
            poly_payload(self.plan, token, PolymarketCredentials(private_key=self.key), self.signer, "test", now_ms=1, salt=1)
        for invalid in ("NaN", "Infinity", "0", "-1"):
            with self.assertRaises(ValueError): cash_budget(invalid, "polymarket")


class PredictSubmissionTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_market_post_preserves_target_and_selected_token(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from user_strategies.prediction_cash_orders import submit_cash_buy

        signer = WalletSigner("0x" + "1" * 64)
        info = MarketInfo(native="123", yes_token="1", no_token="2", neg_risk=False, yield_bearing=False, fee_rate_bps=200)
        adapter = SimpleNamespace(market_info=AsyncMock(return_value=info), signer=signer,
                                  address=signer.address, network=MAINNET, predict_account=None,
                                  _call=AsyncMock(return_value={"success": True, "data": {}}))
        for outcome, token in (("yes", "1"), ("no", "2")):
            plan = {"venue": "predict_fun", "market_id": "predict_fun:123", "outcome": outcome,
                    "target_shares": Decimal(10), "cash_budget": Decimal("3.20"),
                    "expected_price": Decimal("0.32"), "tick": Decimal("0.01")}
            recorded = []
            order = await submit_cash_buy(adapter, plan, lambda *args: recorded.append(args))
            data = adapter._call.call_args.kwargs["body"]["data"]
            self.assertEqual(adapter._call.call_args.args, ("POST", "/orders"))
            self.assertEqual(data["order"]["takerAmount"], str(10 * 10**18))
            self.assertEqual(data["order"]["makerAmount"], str(3200000000000000000))
            self.assertEqual(data["order"]["tokenId"], token)
            self.assertFalse(data["isMinAmountOut"])
            self.assertEqual(data["strategy"], "MARKET")
            self.assertNotIn("isFillOrKill", data)
            self.assertEqual(recorded[0][1:], (Decimal("3.20"), Decimal(10)))
            self.assertEqual(order.amount, Decimal(10))
            self.assertEqual(order.info["amount_semantics"], "SIGNED_TARGET_SHARES")
            self.assertEqual(order.price, Decimal("0.32") if outcome == "yes" else Decimal("0.68"))
