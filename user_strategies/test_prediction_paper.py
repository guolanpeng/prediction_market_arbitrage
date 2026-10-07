import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from user_strategies.check_prediction_pair import calculate
from user_strategies.prediction_paper import PaperRecorder, settlement_factors
from user_strategies.test_check_prediction_pair import asks


class PaperTest(unittest.TestCase):
    def fixtures(self):
        window = {"start": 600, "end": 900, "polymarket": "1", "predictfun": "2",
                  "polymarket_rule": "TWAP", "predictfun_rule": "close"}
        books = [asks(("0.2", "2"), ("0.3", "8")), asks(("0.45", "10"))]
        configs = [{"model": "quadratic_theta", "rate": Decimal("0.07"), "places": "0.00001"},
                   {"model": "min_price", "rate": Decimal("0.02"), "places": "0.000001"}]
        return window, books, configs, calculate(*books, Decimal(10), configs)

    def test_persists_entry_resumes_and_settles_exact_payout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.jsonl"
            recorder = PaperRecorder(path)
            window, books, configs, result = self.fixtures()
            self.assertEqual(recorder.observe(window, ["yes", "no"], Decimal(10), result,
                                              [700000, 699000], books, configs, now=700), "RECORDED")
            recorder = PaperRecorder(path)
            self.assertEqual(recorder.observe(window, ["yes", "no"], Decimal(10), result,
                                              [700000, 699000], books, configs, now=700), "ALREADY_RECORDED")
            recorder.settle_pending(lambda entry: None, now=901)
            self.assertFalse(recorder.settled)
            recorder.settle_pending(lambda entry: [Decimal(0), Decimal(0)], now=901)
            row = next(iter(recorder.settled.values()))
            self.assertEqual(row["classification"], "BOTH_LOSE")
            self.assertEqual(row["simulated_net"], Decimal("-7.53"))
            recorder.settle_pending(lambda entry: self.fail("Already settled"), now=902)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row["type"] for row in rows], ["ENTRY", "SETTLEMENT"])
            self.assertEqual(rows[0]["legs"][0]["cost"], "2.8")
            self.assertEqual(len(rows[0]["asks"][0]), 2)

    def test_rejects_stale_skew_small_orders_and_partial_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = PaperRecorder(Path(directory) / "paper.jsonl")
            window, books, configs, result = self.fixtures()
            for times, reason in (([None, 700000], "QUOTE_TIME_UNKNOWN_OR_STALE"),
                                  ([680000, 700000], "QUOTE_TIME_UNKNOWN_OR_STALE"),
                                  ([694000, 700000], "QUOTE_TIME_SKEW")):
                self.assertEqual(recorder.observe(window, ["yes", "no"], Decimal(10), result,
                                                  times, books, configs, now=700), reason)
            books[1] = asks(("0.01", "100"))
            result = calculate(*books, Decimal(10), configs)
            self.assertEqual(recorder.observe(window, ["yes", "no"], Decimal(10), result,
                                              [700000, 700000], books, configs, now=700), "PREDICT_MINIMUM_1_USDT")
            books[1] = asks(("0.5", "1"))
            result = calculate(*books, Decimal(10), configs)
            self.assertEqual(recorder.observe(window, ["yes", "no"], Decimal(10), result,
                                              [700000, 700000], books, configs, now=700), "INSUFFICIENT_DEPTH")
            self.assertFalse(recorder.entries)

    def test_entry_requires_strictly_more_than_twenty_cents_after_both_fees(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = PaperRecorder(Path(directory) / "paper.jsonl")
            window, _, configs, _ = self.fixtures()
            books = [asks(("0.4", "10")), asks(("0.4", "10"))]
            result = calculate(*books, Decimal(10), configs)
            self.assertEqual(result["gross"], Decimal(2))
            self.assertEqual(result["net_per_pair"], Decimal("0.1752"))
            self.assertEqual(recorder.observe(window, ["yes", "no"], Decimal(10), result,
                                              [700000, 700000], books, configs, now=700), "NET_PER_PAIR_NOT_ABOVE_0_20")
            # Exactly $0.20 net per pair is rejected too.
            result["net"] = Decimal(2)
            result["net_per_pair"] = Decimal("0.20")
            self.assertEqual(recorder.observe(window, ["yes", "no"], Decimal(10), result,
                                              [700000, 700000], books, configs, now=700), "NET_PER_PAIR_NOT_ABOVE_0_20")
            self.assertFalse(recorder.entries)
            books = [asks(("0.3", "10")), asks(("0.45", "10"))]
            result = calculate(*books, Decimal(10), configs)
            self.assertEqual(result["net_per_pair"], Decimal("0.2263"))
            self.assertEqual(recorder.observe(window, ["yes", "no"], Decimal(10), result,
                                              [700000, 700000], books, configs, now=700), "RECORDED")
            entry = next(iter(recorder.entries.values()))
            self.assertEqual(entry["entry_min_net_per_pair"], Decimal("0.20"))
            self.assertEqual(entry["quoted_net_difference"], Decimal("2.263"))

    def test_official_settlement_orientation_and_unknown_payouts(self):
        poly = {"umaResolutionStatus": "resolved", "outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]'}
        predict = {"status": "RESOLVED", "outcomes": [
            {"indexSet": 1, "name": "Up", "status": "LOST"},
            {"indexSet": 2, "name": "Down", "status": "WON"}]}
        self.assertEqual(settlement_factors(poly, predict, ["yes", "no"]), [Decimal(1), Decimal(1)])
        self.assertEqual(settlement_factors(poly, predict, ["no", "yes"]), [Decimal(0), Decimal(0)])
        poly["umaResolutionStatus"] = "proposed"
        self.assertIsNone(settlement_factors(poly, predict, ["yes", "no"]))
        poly["umaResolutionStatus"] = "resolved"
        predict["outcomes"][0]["status"] = "TIE"
        self.assertIsNone(settlement_factors(poly, predict, ["yes", "no"]))
        predict["outcomes"][0]["status"] = "LOST"
        poly["outcomePrices"] = '["NaN", "0"]'
        self.assertIsNone(settlement_factors(poly, predict, ["yes", "no"]))

    def test_selects_best_eligible_direction_and_never_opens_both(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = PaperRecorder(Path(directory) / "paper.jsonl")
            window, _, configs, _ = self.fixtures()
            def candidate(sides, poly_price, predict_price):
                books = [asks((poly_price, "10")), asks((predict_price, "10"))]
                return {"sides": sides, "asks": books, "timestamps": [700000, 700000],
                        "result": calculate(*books, Decimal(10), configs)}
            forward = candidate(["yes", "no"], "0.3", "0.45")
            reverse = candidate(["no", "yes"], "0.2", "0.45")
            self.assertEqual(recorder.observe_best(window, Decimal(10), [forward, reverse], configs, now=700), "RECORDED")
            entry = next(iter(recorder.entries.values()))
            self.assertEqual(entry["sides"], ["no", "yes"])
            self.assertEqual(recorder.observe(window, ["yes", "no"], Decimal(10), forward["result"],
                                              forward["timestamps"], forward["asks"], configs, now=700), "ALREADY_RECORDED")
            self.assertEqual(len(recorder.entries), 1)
            # A larger quoted edge cannot hide an eligible other direction if stale.
            window = {**window, "start": 601}
            reverse["timestamps"] = [680000, 700000]
            self.assertEqual(recorder.observe_best(window, Decimal(10), [forward, reverse], configs, now=700), "RECORDED")
            self.assertEqual(list(recorder.entries.values())[-1]["sides"], ["yes", "no"])

    def test_chainlink_resolution_without_uma_requires_closed_and_explicit_winner(self):
        poly = {"outcomes": '["Up", "Down"]', "outcomePrices": '["0.0005", "0.9995"]'}
        predict = {"status": "RESOLVED", "outcomes": [
            {"indexSet": 1, "name": "Up", "status": "LOST"},
            {"indexSet": 2, "name": "Down", "status": "WON"}]}
        clob = {"closed": True, "tokens": [{"outcome": "Down", "winner": True},
                                          {"outcome": "Up", "winner": False}]}
        self.assertEqual(settlement_factors(poly, predict, ["yes", "no"], clob), [Decimal(0), Decimal(1)])
        clob["closed"] = False
        self.assertIsNone(settlement_factors(poly, predict, ["yes", "no"], clob))
        clob["closed"] = True
        clob["tokens"][0]["winner"] = False
        self.assertIsNone(settlement_factors(poly, predict, ["yes", "no"], clob))
        for token in clob["tokens"]:
            token["winner"] = True
        self.assertIsNone(settlement_factors(poly, predict, ["yes", "no"], clob))
