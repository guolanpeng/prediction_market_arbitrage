import unittest
from decimal import Decimal

from user_strategies.synpath_scan import normalize, timestamp


class SynpathScanTest(unittest.TestCase):
    def sample(self):
        leg = {"venue": "polymarket", "native_market_id": "a", "price": Decimal("0.152"),
               "fee": Decimal("0.000620000000000000001"), "fee_source": "venue",
               "fetched_at": "2026-10-06T12:00:00Z"}
        return {"computed_at": "2026-10-06T12:00:00Z", "pairs": 21234, "mismatched": 4468,
                "ttl_s": Decimal("30"), "results": [{"event_title": "Q", "outcome_id": "o",
                "outcome_label": "Above $100M", "yes": leg,
                "no": {**leg, "venue": "predict_fun", "native_market_id": "b", "price": Decimal("0.84"),
                       "fee_source": "default"}, "gross_spread": Decimal("0.008"),
                "fees": Decimal("0.001240000000000000002"), "net_profit": Decimal("0.00676")}]}

    def test_preserves_source_amounts_and_does_not_invent_size_or_verification(self):
        data = self.sample()
        result = normalize(data, timestamp(data["computed_at"]))
        row = result["candidates"][0]
        self.assertEqual(row["fees_per_share"], "0.001240000000000000002")
        self.assertEqual(row["no_leg"]["venue"], "predictfun")
        self.assertTrue(row["no_leg"]["fee_estimated"])
        self.assertIsNone(row["available_quantity"])
        self.assertFalse(row["match_verified"])
        self.assertEqual(result["opportunities"], [])

    def test_stale_source_quote_is_labelled_without_rewriting_timestamp(self):
        data = self.sample()
        data["results"][0]["yes"]["fetched_at"] = "2026-10-06T11:00:00Z"
        row = normalize(data, timestamp(data["computed_at"]))["candidates"][0]
        self.assertIn("source_quote_stale", row["review_reasons"])
        self.assertEqual(row["yes_leg"]["quote_at_ms"], timestamp("2026-10-06T11:00:00Z"))

    def test_event_time_is_not_a_trading_deadline(self):
        data = self.sample()
        data["results"][0]["event_time"] = "2026-10-07T13:00:00Z"
        at = timestamp(data["computed_at"])
        row = normalize(data, at)["candidates"][0]
        self.assertIsNone(row["close_time"])
        self.assertEqual(row["event_time"], "2026-10-07T13:00:00Z")
        data["results"][0]["close_at"] = "2026-10-06T13:05:00Z"
        self.assertEqual(normalize(data, at)["candidates"][0]["close_time"], "2026-10-06T13:05:00Z")

    def test_rejects_stale_snapshot_and_skips_invalid_price(self):
        data = self.sample()
        at = timestamp(data["computed_at"])
        with self.assertRaises(ValueError):
            normalize(data, at + 120001)
        data["results"][0]["yes"]["price"] = "NaN"
        result = normalize(data, at)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["invalid_records"], 1)
