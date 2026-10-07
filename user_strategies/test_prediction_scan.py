"""Prediction adapter, executable-depth, matching and Redis failure tests."""

import argparse
import asyncio
import json
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx

from user_strategies import prediction_markets as pm
from user_strategies import prediction_scan as ps


def quoted(venue="polymarket", id_="one", price="0.40", fee=None):
    return pm.market(venue, id_, "Will X win in 2027?", quote_currency="USDC",
                     rules="X wins; cancellation refunds", fee=fee,
                     books={side: {"asks": [[price, "100"]], "bids": [], "received_at_ms": 1000}
                            for side in ("yes", "no")}, quote_at_ms=1000)


class RedisFake:
    def __init__(self):
        self.values, self.hashes, self.expiry = {}, {}, {}

    async def set(self, key, value, ex):
        self.values[key], self.expiry[key] = value, ex

    async def get(self, key):
        return self.values.get(key)

    async def hkeys(self, key):
        return list(self.hashes.get(key, {}))

    async def hgetall(self, key):
        return self.hashes.get(key, {})

    async def hmget(self, key, fields):
        return [self.hashes.get(key, {}).get(field) for field in fields]

    async def expire(self, key, seconds):
        self.expiry[key] = seconds

    def pipeline(self, transaction):
        redis = self

        class Pipeline:
            def __init__(self):
                self.operations = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def hset(self, key, mapping):
                self.operations.append(lambda: redis.hashes.setdefault(key, {}).update(mapping))

            def hdel(self, key, *fields):
                self.operations.append(lambda: [redis.hashes.get(key, {}).pop(field, None) for field in fields])

            def expire(self, key, seconds):
                self.operations.append(lambda: redis.expiry.update({key: seconds}))

            async def execute(self):
                for operation in self.operations:
                    operation()

        return Pipeline()


class PredictionTests(unittest.IsolatedAsyncioTestCase):
    def test_levels_preserve_precision_sort_aggregate_and_complement(self):
        rows = [["0.12345678901234567890123456789", "1"],
                ["0.4", "2"], ["0.4", "3"], ["0", "50"], ["NaN", "5"]]
        normalized = pm.levels(rows)
        self.assertEqual(normalized[0][0], rows[0][0])
        self.assertEqual(normalized[1], ["0.4", "5"])
        self.assertEqual(pm.levels(rows, complement=True)[1][0], "0.87654321098765432109876543211")

    async def test_kalshi_pagination_and_no_asks_from_yes_bids(self):
        adapter = pm.Adapter("kalshi", None)
        adapter.request = AsyncMock(side_effect=[
            {"markets": [{"ticker": "A", "title": "A", "status": "active", "market_type": "binary"}], "cursor": "next"},
            {"markets": [{"ticker": "B", "title": "B", "status": "active", "market_type": "binary"}], "cursor": ""},
            {"orderbook_fp": {"yes_dollars": [["0.7", "5"], ["0.8", "10"]], "no_dollars": [["0.3", "12"]]}},
        ])
        rows, complete = await adapter.discover()
        self.assertTrue(complete)
        self.assertEqual(set(rows), {"A", "B"})
        self.assertEqual(adapter.request.call_args_list[1].kwargs["params"]["cursor"], "next")
        result = await adapter.quote(rows["A"])
        self.assertEqual(result["books"]["no"]["asks"][0], ["0.2", "10"])
        self.assertEqual(result["books"]["yes"]["asks"][0], ["0.7", "12"])

    async def test_discovery_publishes_each_page_before_full_catalog_finishes(self):
        adapter = pm.Adapter("kalshi", None)
        row = {"ticker": "A", "title": "A", "status": "active", "market_type": "binary"}
        adapter.request = AsyncMock(side_effect=[{"markets": [row], "cursor": "next"},
                                                {"markets": [{**row, "ticker": "B"}], "cursor": ""}])
        callback = AsyncMock()
        rows, complete = await adapter.discover(on_page=callback)
        self.assertTrue(complete)
        self.assertEqual(len(rows), 2)
        self.assertEqual(callback.call_args_list[0].args[1], 1)
        self.assertEqual(callback.call_args_list[1].args[1], 2)
        self.assertEqual(set(callback.call_args_list[1].args[0]), {"B"})

    def test_kalshi_missing_title_does_not_abort_full_catalog(self):
        adapter = pm.Adapter("kalshi", None)
        row = {"ticker": "KXLLM1-26OCT31-MOON", "status": "active", "market_type": "binary",
               "yes_sub_title": "Moonshot", "rules_primary": "Settlement rules"}
        item = adapter.normalize(row)[0]
        self.assertEqual(item["title"], row["ticker"])
        self.assertTrue(item["title_missing"])
        self.assertEqual(item["rules"], "Settlement rules\n")

    async def test_polymarket_reversed_tokens_and_real_book_not_outcome_price(self):
        adapter = pm.Adapter("polymarket", None)
        row = {"conditionId": "c", "question": "Q", "slug": "q", "acceptingOrders": True,
               "enableOrderBook": True, "outcomes": '["No","Yes"]', "clobTokenIds": '["n","y"]',
               "outcomePrices": '["0.01","0.99"]', "feeSchedule": {"rate": Decimal("0.04"), "exponent": 1}}
        item = adapter.normalize(row)[0]
        self.assertEqual(item["tokens"], ["y", "n"])
        adapter.request = AsyncMock(return_value={"asks": [{"price": "0.3", "size": "3"}, {"price": "0.2", "size": "5"}], "bids": []})
        result = await adapter.quote(item)
        self.assertEqual(result["books"]["yes"]["asks"][0][0], "0.2")
        self.assertEqual(adapter.request.call_args_list[0].kwargs["params"], {"token_id": "y"})
        self.assertEqual(ps.fee_for(item, Decimal("0.5"), Decimal("100")), Decimal("1.00000"))

    def test_opinion_categorical_children_inherit_rules_and_filter_resolved(self):
        adapter = pm.Adapter("opinion", None)
        children = [{"marketId": 1, "marketTitle": "X", "status": 2, "yesTokenId": "y", "noTokenId": "n"},
                    {"marketId": 2, "marketTitle": "Y", "status": 4, "yesTokenId": "y2", "noTokenId": "n2"}]
        items = adapter.normalize({"marketTitle": "Winner", "marketId": 10, "rules": "parent rules",
                                   "quoteToken": "0x55d398326f99059fF775485246999027B3197955", "childMarkets": children})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Winner | X")
        self.assertEqual(items[0]["rules"], "parent rules")
        self.assertEqual(items[0]["quote_currency"], "USDT")
        self.assertIsNone(items[0]["fee"])

    async def test_predict_key_pagination_links_and_complement(self):
        adapter = pm.Adapter("predictfun", None)
        row = {"id": 1, "title": "Q", "tradingStatus": "OPEN", "feeRateBps": 0,
               "kalshiMarketTicker": "K", "polymarketConditionIds": ["c"]}
        adapter.request = AsyncMock(side_effect=[{"success": True, "data": [row], "cursor": "page2"},
                                                {"success": True, "data": [], "cursor": ""},
                                                {"data": {"asks": [["0.6", "20"]], "bids": [["0.5", "10"]], "updateTimestampMs": 99}}])
        with patch.dict("os.environ", {"PREDICT_FUN_API_KEY": "test"}):
            rows, complete = await adapter.discover()
        self.assertTrue(complete)
        self.assertEqual(rows["1"]["references"][0]["market_id"], "c")
        self.assertEqual(adapter.request.call_args_list[0].kwargs["params"]["status"], "OPEN")
        result = await adapter.quote(rows["1"])
        self.assertEqual(result["books"]["no"]["asks"], [["0.5", "10"]])

    async def test_hyperliquid_uses_production_outcome_ids_and_parent_context(self):
        requests = []

        def handle(request):
            requests.append((str(request.url), json.loads(request.content)))
            if requests[-1][1]["type"] == "outcomeMeta":
                return httpx.Response(200, json={"outcomes": [{"outcome": 2, "description": "participant:X", "quoteToken": "USDC"}],
                                               "questions": [{"description": "competition:League|season:2027", "namedOutcomes": [2]}]})
            return httpx.Response(200, json={"levels": [[{"px": "0.3", "sz": "10"}], [{"px": "0.4", "sz": "20"}]], "time": 123})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter = pm.Adapter("hyperliquid", client)
            adapter.spacing = 0
            rows, complete = await adapter.discover()
            await adapter.quote(rows["2"])
        self.assertTrue(complete)
        self.assertIn("season:2027", rows["2"]["title"])
        self.assertEqual([r[1].get("coin") for r in requests[1:]], ["#20", "#21"])
        self.assertTrue(all(r[0] == "https://api.hyperliquid.xyz/info" for r in requests))

    async def test_http_json_decimal_errors_and_api_credentials_stay_in_headers(self):
        def handle(request):
            self.assertEqual(request.headers["apikey"], "secret")
            self.assertNotIn("secret", str(request.url))
            return httpx.Response(200, text='{"errno":0,"result":{"price":0.12345678901234567890123456789}}')

        with patch.dict("os.environ", {"OPINION_API_KEY": "secret"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                data = await pm.Adapter("opinion", client).request("/test")
        self.assertEqual(str(data["price"]), "0.12345678901234567890123456789")

    async def test_repeated_cursor_is_error_instead_of_false_complete(self):
        adapter = pm.Adapter("kalshi", None)
        adapter.request = AsyncMock(return_value={"markets": [{"ticker": "A", "title": "A", "status": "active", "market_type": "binary"}], "cursor": "same"})
        with self.assertRaises(ValueError):
            await adapter.discover()

    async def test_polymarket_batch_matches_token_ids_not_response_order(self):
        adapter = pm.Adapter("polymarket", None)
        row = quoted()
        row["tokens"] = ["y", "n"]
        adapter.request = AsyncMock(return_value=[
            {"asset_id": "n", "asks": [["0.7", "10"]], "bids": []},
            {"asset_id": "y", "asks": [["0.2", "10"]], "bids": []},
        ])
        result = (await adapter.quote_batch([row]))[0]
        self.assertEqual(result["books"]["yes"]["asks"][0][0], "0.2")
        self.assertEqual(result["books"]["no"]["asks"][0][0], "0.7")
        adapter.request.return_value = []
        self.assertIsInstance((await adapter.quote_batch([row]))[0], ValueError)

    async def test_polymarket_follows_cursor_even_for_short_pages(self):
        adapter = pm.Adapter("polymarket", None)
        row = {"conditionId": "c1", "question": "Q", "slug": "q", "acceptingOrders": True,
               "enableOrderBook": True, "outcomes": '["Yes","No"]', "clobTokenIds": '["y","n"]'}
        adapter.request = AsyncMock(side_effect=[
            {"markets": [row], "next_cursor": "opaque"},
            {"markets": [{**row, "conditionId": "c2"}], "next_cursor": None},
        ])
        rows, complete = await adapter.discover()
        self.assertTrue(complete)
        self.assertEqual(set(rows), {"c1", "c2"})
        self.assertEqual(adapter.request.call_args_list[1].args, ("/markets/keyset",))
        self.assertEqual(adapter.request.call_args_list[1].kwargs["params"]["after_cursor"], "opaque")

    async def test_opinion_full_pagination_includes_children_on_later_pages(self):
        adapter = pm.Adapter("opinion", None)
        child = {"marketId": 2, "marketTitle": "X", "status": 2, "yesTokenId": "y", "noTokenId": "n"}
        adapter.request = AsyncMock(side_effect=[
            {"list": [{"marketId": 1, "marketTitle": "First", "status": 2, "yesTokenId": "y1", "noTokenId": "n1"}], "total": 21},
            {"list": [{"marketId": 3, "marketTitle": "Later", "childMarkets": [child]}], "total": 21},
        ])
        rows, complete = await adapter.discover()
        self.assertTrue(complete)
        self.assertEqual(set(rows), {"1", "2"})
        self.assertEqual(adapter.request.call_args_list[1].kwargs["params"]["page"], 2)

    def test_executable_depth_unknown_fees_and_stale_quotes(self):
        a, b = quoted(), quoted("opinion", "two")
        result = ps.scan([a, b], [], Decimal(100), 1100, 1000)
        self.assertEqual(len(result["candidates"]), 2)
        self.assertIsNone(result["candidates"][0]["net_profit"])
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(ps.scan([a, b], [], Decimal(101), 1100, 1000)["candidates"], [])
        b["quote_at_ms"] = 1
        self.assertEqual(ps.scan([a, b], [], Decimal(100), 1100, 1000)["candidates"], [])

    def test_reviewed_match_fingerprint_inversion_and_collateral_parity(self):
        a, b = quoted(fee={"model": "zero", "estimated": False}), quoted("predictfun", "two", fee={"model": "zero", "estimated": False})
        b["quote_currency"] = "USDT"
        b["books"]["no"]["asks"] = [["0.9", "100"]]
        group = {"id": "checked", "usd_parity": True, "members": [
            {"venue": r["venue"], "market_id": r["market_id"], "fingerprint": ps.fingerprint(r), "invert": r is b}
            for r in (a, b)]}
        result = ps.scan([a, b], [group], Decimal(100), 1100, 1000)
        self.assertEqual(len(result["opportunities"]), 1)
        self.assertEqual(result["opportunities"][0]["no_leg"]["side"], "yes")
        self.assertEqual(result["opportunities"][0]["net_profit"], "20.00")
        b["rules"] = "changed settlement policy"
        result = ps.scan([a, b], [group], Decimal(100), 1100, 1000)
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(result["invalid_matches"][0]["reason"], "fingerprint_changed")

    def test_multilevel_cost_rounding_and_minimum_orders(self):
        row = quoted("kalshi", fee={"model": "quadratic", "rate": "0.07", "estimated": True})
        row["books"]["yes"]["asks"] = [["0.4", "40"], ["0.5", "60"]]
        result = ps.fill(row, "yes", Decimal(100))
        self.assertEqual(result["cost"], "46.0")
        self.assertEqual(result["fees"], "1.73")
        row["minimum_notional"] = "50"
        self.assertIsNone(ps.fill(row, "yes", Decimal(100)))

    def test_closed_market_old_first_leg_and_strike_punctuation_are_not_matched(self):
        a, b = quoted(), quoted("opinion", "two")
        a["close_time"] = 1
        self.assertFalse(ps.fresh(a, 1100, 1000))
        a["close_time"] = None
        a["books"]["yes"]["received_at_ms"] = 1
        self.assertFalse(ps.fresh(a, 1100, 1000))
        a["title"], b["title"] = "Above 1.5?", "Above 15?"
        self.assertNotEqual(ps.title_key(a), ps.title_key(b))

    def test_title_matches_repeated_affirmative_label_but_keeps_distinct_outcomes(self):
        a, b = quoted(), quoted("kalshi", "two")
        b["yes_label"] = "X"
        self.assertEqual(ps.title_key(a), ps.title_key(b))
        a["title"], b["title"] = "Who wins?", "Who wins?"
        a["yes_label"], b["yes_label"] = "X", "Y"
        self.assertNotEqual(ps.title_key(a), ps.title_key(b))

    async def test_redis_partial_failure_removes_delisted_and_clears_failed_quotes(self):
        redis = RedisFake()
        key = f"{ps.PREFIX}:kalshi:markets"
        redis.hashes[key] = {"old": "{}"}
        adapter = pm.Adapter("kalshi", None)
        adapter.discover = AsyncMock(return_value=({"one": quoted("kalshi")}, False))
        adapter.quote = AsyncMock(side_effect=ValueError("secret response body"))
        args = argparse.Namespace(ttl=600, max_markets=1, discovery_interval=300, once=True, interval=1,
                                  quantity=Decimal(100), max_age=60)
        await ps.collect_venue(adapter, redis, args)
        self.assertNotIn("old", redis.hashes[key])
        self.assertEqual(json.loads(redis.hashes[key]["one"])["books"], {})
        self.assertEqual(redis.expiry[key], 600)
        status = json.loads(redis.values[f"{ps.PREFIX}:kalshi:status"])
        self.assertEqual(status["quote_failures"], 1)
        self.assertFalse(status["coverage_complete"])
        payload = await ps.scan_once(redis, args, [])
        self.assertFalse(payload["coverage_complete"])
        self.assertNotIn("secret", pm.dumps(redis.values))

    async def test_missing_predict_key_does_not_claim_empty_success(self):
        redis = RedisFake()
        adapter = pm.Adapter("predictfun", None)
        args = argparse.Namespace(ttl=60, max_markets=0, discovery_interval=300, once=True, interval=1)
        with patch.dict("os.environ", {}, clear=True):
            await ps.collect_venue(adapter, redis, args)
        status = json.loads(redis.values[f"{ps.PREFIX}:predictfun:status"])
        self.assertEqual(status["state"], "unavailable")
        self.assertEqual(status["error"], "missing_api_key")

    def test_quote_targets_require_cross_platform_match_and_open_market(self):
        a, b = quoted("kalshi", "a"), quoted("polymarket", "b")
        single = quoted("kalshi", "alone")
        single["title"] = "Unmatched market"
        catalogs = {"kalshi": {"a": a, "alone": single}, "polymarket": {"b": b}}
        targets = ps.quote_targets(catalogs, [], 1100)
        self.assertEqual(targets["kalshi"], {"a"})
        self.assertEqual(targets["polymarket"], {"b"})
        b["close_time"] = 1
        self.assertTrue(all(not ids for ids in ps.quote_targets(catalogs, [], 1100).values()))

    async def test_matched_scope_skips_unmatched_books_and_keeps_catalog(self):
        redis = RedisFake()
        adapter = pm.Adapter("kalshi", None)
        a, b = quoted("kalshi", "a"), quoted("polymarket", "b")
        single = quoted("kalshi", "alone")
        single["title"] = "Unmatched market"
        catalogs = {"kalshi": {"a": a, "alone": single}, "polymarket": {"b": b}}
        adapter.quote_batch = AsyncMock(return_value=[a])
        args = argparse.Namespace(ttl=600, max_markets=0, discovery_interval=900, once=True, interval=1,
                                  book_scope="matched", quantity=Decimal(100), max_age=60)
        await ps.collect_venue(adapter, redis, args, catalogs, [])
        self.assertEqual(len(adapter.quote_batch.call_args.args[0]), 1)
        self.assertEqual(adapter.quote_batch.call_args.args[0][0]["market_id"], "a")
        status = json.loads(redis.values[f"{ps.PREFIX}:kalshi:status"])
        self.assertEqual(status["market_count"], 2)
        self.assertEqual(status["target_count"], 1)

    async def test_no_matches_makes_no_book_requests(self):
        redis = RedisFake()
        adapter = pm.Adapter("kalshi", None)
        catalogs = {"kalshi": {"one": quoted("kalshi")}}
        adapter.quote_batch = AsyncMock()
        args = argparse.Namespace(ttl=600, max_markets=0, discovery_interval=900, once=True, interval=1, book_scope="matched")
        await ps.collect_venue(adapter, redis, args, catalogs, [])
        adapter.quote_batch.assert_not_called()
        status = json.loads(redis.values[f"{ps.PREFIX}:kalshi:status"])
        self.assertEqual(status["target_count"], 0)

    def test_predict_nullable_statistics_do_not_abort_catalog(self):
        adapter = pm.Adapter("predictfun", None)
        row = {"id": 1, "title": "Q", "tradingStatus": "OPEN", "feeRateBps": 0, "stats": None}
        self.assertIsNone(adapter.normalize(row)[0]["volume_24h"])

    async def test_matched_books_start_before_catalog_discovery_completes(self):
        redis = RedisFake()
        adapter = pm.Adapter("kalshi", None)
        a, b = quoted("kalshi", "a"), quoted("polymarket", "b")
        catalogs = {"polymarket": {"b": b}}
        requested = asyncio.Event()
        release = asyncio.Event()

        async def discover(limit, on_page):
            await on_page({"a": a}, 1)
            await release.wait()
            return {"a": a}, True

        async def quote_batch(items):
            requested.set()
            return [{**a, "quote_at_ms": 999999}]

        adapter.discover = discover
        adapter.quote_batch = AsyncMock(side_effect=quote_batch)
        args = argparse.Namespace(ttl=600, max_markets=0, discovery_interval=900, once=False,
                                  interval=.01, book_scope="matched")
        task = asyncio.create_task(ps.collect_venue(adapter, redis, args, catalogs, []))
        try:
            await asyncio.wait_for(requested.wait(), 1)
            self.assertFalse(release.is_set())
            self.assertEqual(json.loads(redis.values[f"{ps.PREFIX}:kalshi:status"])["state"], "discovering")
            self.assertEqual(json.loads(redis.hashes[f"{ps.PREFIX}:kalshi:markets"]["a"])["quote_at_ms"], 999999)
            release.set()
            await asyncio.sleep(.02)
            self.assertEqual(catalogs["kalshi"]["a"]["quote_at_ms"], 999999)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_kalshi_top_quotes_batch_markets_and_use_complement_sizes(self):
        adapter = pm.Adapter("kalshi", None, quote_mode="top")
        adapter.request = AsyncMock(return_value={"markets": [
            {"ticker": "a", "status": "active", "yes_ask_dollars": "0.67", "yes_ask_size_fp": "20",
             "yes_bid_dollars": "0.60", "yes_bid_size_fp": "35", "no_ask_dollars": "0.40", "no_bid_dollars": "0.33"},
        ]})
        result = (await adapter.quote_batch([quoted("kalshi", "a")]))[0]
        self.assertEqual(adapter.request.call_args.args, ("/markets",))
        self.assertEqual(adapter.request.call_args.kwargs["params"]["tickers"], "a")
        self.assertEqual(result["books"]["yes"]["asks"], [["0.67", "20"]])
        self.assertEqual(result["books"]["no"]["asks"], [["0.40", "35"]])
        self.assertEqual(result["quote_mode"], "top")

    def test_top_scan_uses_only_first_level_and_available_pair_quantity(self):
        a, b = quoted(), quoted("predictfun", "b")
        a["books"]["yes"]["asks"] = [["0.4", "30"], ["0.9", "70"]]
        b["books"]["no"]["asks"] = [["0.4", "50"], ["0.95", "50"]]
        result = ps.scan([a, b], [], Decimal(100), 1100, 1000, quote_mode="top")
        pair = next(row for row in result["candidates"] if row["yes_leg"]["venue"] == "polymarket")
        self.assertEqual(pair["quantity"], "30")
        self.assertEqual(pair["available_quantity"], "30")
        self.assertEqual(pair["gross_spread"], "0.2")
        self.assertEqual(pair["yes_leg"]["average_price"], "0.4")

    def test_top_quote_keeps_bid_ask_and_sizes_but_no_extra_levels(self):
        data = pm.finish_quote(quoted(), {"yes": pm.book([["0.4", "10"], ["0.5", "30"]], [["0.3", "15"], ["0.2", "20"]])}, "top")
        self.assertEqual(data["books"]["yes"]["asks"], [["0.4", "10"]])
        self.assertEqual(data["books"]["yes"]["bids"], [["0.3", "15"]])


if __name__ == "__main__":
    unittest.main()
