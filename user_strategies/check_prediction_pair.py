"""Read-only WS cost/fee checker for one Polymarket/Predict.fun pair."""

import argparse
import asyncio
import json
import logging
import math
import os
import time
import traceback
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path

import httpx
import synpath
from websockets.asyncio.client import connect

from user_strategies.prediction_paper import PaperRecorder
from user_strategies.prediction_trading import LiveTrader, LiveTradeHalt, LiveRunComplete, LiveSettlementPending, LiveConfirmationPending, credentials_status, live_entry_reason

LOG = logging.getLogger("prediction-check")


def decimal(value):
    try:
        value = Decimal(str(value))
    except ArithmeticError as error:
        raise ValueError("Invalid decimal amount") from error
    if not value.is_finite():
        raise ValueError("Non-finite amount")
    return value


def fee_config(venue, market, schedule):
    if venue == "polymarket":
        if market.info.get("feesEnabled") is False:
            return {"model": "quadratic_theta", "rate": Decimal(0), "places": "0.00001"}
        raw = (schedule.info.get("feeSchedule") or {}) if schedule else {}
        if schedule and schedule.fee_type == "quadratic_theta" and raw.get("rate") is not None:
            if decimal(raw.get("exponent") if raw.get("exponent") is not None else 1) == 1 and decimal(raw["rate"]) >= 0:
                return {"model": "quadratic_theta", "rate": decimal(raw["rate"]), "places": "0.00001"}
    else:
        bps = schedule.info.get("fee_rate_bps") if schedule else None
        if schedule and schedule.fee_type == "min_price" and bps is not None and decimal(bps) >= 0:
            return {"model": "min_price", "rate": decimal(bps) / 10000, "places": "0.000001"}
    return None


def fill_cost(asks, quantity, config):
    remaining, cost, fee = quantity, Decimal(0), Decimal(0) if config else None
    for level in asks:
        price, size = decimal(level.price), decimal(level.size)
        if not 0 < price < 1 or size <= 0:
            continue
        take = min(size, remaining)
        cost += price * take
        if config:
            basis = price * (1 - price) if config["model"] == "quadratic_theta" else min(price, 1 - price)
            fee += config["rate"] * basis * take
        remaining -= take
        if remaining == 0:
            break
    if fee is not None:
        fee = fee.quantize(Decimal(config["places"]), rounding=ROUND_HALF_UP)
    return {"filled": quantity - remaining, "complete": remaining == 0, "cost": cost, "fee": fee}


def calculate(asks_a, asks_b, quantity, configs):
    with localcontext() as context:
        context.prec = 64
        legs = [fill_cost(asks, quantity, config) for asks, config in zip((asks_a, asks_b), configs)]
        result = {"legs": legs, "quantity": quantity, "net": None, "gross": None}
        if not all(leg["complete"] for leg in legs):
            result["state"] = "INSUFFICIENT_DEPTH"
            return result
        result["gross"] = quantity - sum(leg["cost"] for leg in legs)
        if any(leg["fee"] is None for leg in legs):
            result["state"] = "FEES_UNKNOWN"
            return result
        result["net"] = result["gross"] - sum(leg["fee"] for leg in legs)
        result["net_per_pair"] = result["net"] / quantity
        result["state"] = "POSITIVE_QUOTE_EDGE" if result["net"] > 0 else "NO_NET_EDGE"
        return result


def load_market(venue, market_id):
    with (synpath.Polymarket() if venue == "polymarket" else synpath.PredictFun()) as adapter:
        market = adapter.fetch_market(market_id)
        try:
            schedule = adapter.fetch_fee_schedule(market_id)
        except Exception:
            schedule = None
        config = fee_config(venue, market, schedule)
        return market, config


async def connector(url, **kwargs):
    return await connect(url, proxy=None, max_size=None, **kwargs)


async def drain(stream):
    async for event in stream:
        if isinstance(event, synpath.StreamStatusEvent):
            # Never log provider details: they can include authenticated URLs.
            LOG.info("WS venue=%s state=%s", event.venue, event.state)


def list_pairs(query):
    with httpx.Client(timeout=30, trust_env=False) as client:
        response = client.get("https://api.synpath.dev/arbitrage")
        response.raise_for_status()
        rows = response.json()["results"]
    found = 0
    for row in rows:
        if query.casefold() not in (row["event_title"] + " " + row["outcome_label"]).casefold():
            continue
        legs = {row[side]["venue"]: (row[side]["native_market_id"], side) for side in ("yes", "no")}
        if set(legs) != {"polymarket", "predict_fun"}:
            continue
        found += 1
        LOG.info("PAIR title=%s outcome=%s --polymarket %s --polymarket-side %s --predictfun %s --predictfun-side %s",
                 row["event_title"], row["outcome_label"], *legs["polymarket"], *legs["predict_fun"])
    LOG.info("Found %d source candidates; IDs and rules must be checked before comparing.", found)


def epoch(value):
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Window timestamp must include timezone")
    return int(date.timestamp())


def validate_btc_window(polymarket_rows, category, start):
    slug = f"btc-updown-5m-{start}"
    matches = [row for row in polymarket_rows if row.get("slug") == slug]
    if len(matches) != 1 or category.get("slug") != slug:
        raise ValueError("Missing or ambiguous BTC window")
    poly = matches[0]
    if (epoch(poly["eventStartTime"]) != start or epoch(poly["endDate"]) != start + 300
            or epoch(category["startsAt"]) != start or epoch(category["endsAt"]) != start + 300):
        raise ValueError("Venue time windows do not match")
    if poly.get("closed") is not False or poly.get("acceptingOrders") is not True or category.get("status") != "OPEN":
        raise ValueError("Current market is not accepting orders")
    labels = poly.get("outcomes")
    labels = json.loads(labels) if isinstance(labels, str) else labels
    if labels != ["Up", "Down"]:
        raise ValueError("Polymarket outcome orientation is not verified")
    markets = [market for market in category.get("markets", []) if market.get("categorySlug") == slug
               and market.get("marketVariant") == "CRYPTO_UP_DOWN" and market.get("tradingStatus") == "OPEN"]
    if len(markets) != 1:
        raise ValueError("Predict.fun window is missing or ambiguous")
    predict = markets[0]
    orientation = {row.get("indexSet"): row.get("name") for row in predict.get("outcomes", [])}
    if orientation != {1: "Up", 2: "Down"}:
        raise ValueError("Predict.fun outcome orientation is not verified")
    variant = predict.get("variantData") or category.get("variantData") or {}
    if variant.get("priceFeedSymbol") not in ("BTCUSD", "BTCUSDT"):
        raise ValueError("Predict.fun asset is not verified as BTC")
    return {"polymarket": str(poly["id"]), "predictfun": str(predict["id"]),
            "polymarket_rule": poly.get("description") or (poly.get("events") or [{}])[0].get("description"),
            "predictfun_rule": predict.get("description"), "start": start, "end": start + 300}


def discover_btc_window(start):
    slug = f"btc-updown-5m-{start}"
    with httpx.Client(timeout=10, trust_env=False) as client:
        poly = client.get("https://gamma-api.polymarket.com/markets", params={"slug": slug})
        poly.raise_for_status()
        predict = client.get(f"https://api.predict.fun/v1/categories/{slug}",
                             headers={"x-api-key": os.environ["PREDICT_FUN_API_KEY"]})
        if predict.status_code == 404:
            raise ValueError("Predict.fun current window has not been published")
        predict.raise_for_status()
        data = predict.json()
        if data.get("success") is not True:
            raise ValueError("Predict.fun category lookup failed")
        return validate_btc_window(poly.json(), data["data"], start)


async def auto_btc(args, discover=discover_btc_window, check=None):
    if not os.getenv("PREDICT_FUN_API_KEY"):
        raise ValueError("PREDICT_FUN_API_KEY is required for automatic BTC discovery")
    check = run if check is None else check
    stop_at = time.monotonic() + args.seconds if args.seconds else None
    while stop_at is None or time.monotonic() < stop_at:
        start = int(time.time()) // 300 * 300
        try:
            window = await asyncio.to_thread(discover, start)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            LOG.warning("AUTO_WAIT slot=%s reason=%s", start, type(error).__name__)
        else:
            # A slow directory read must never subscribe the expired window.
            if not start <= time.time() < window["end"]:
                continue
            window_args = argparse.Namespace(**{**vars(args), "polymarket": window["polymarket"],
                                                "predictfun": window["predictfun"], "seconds": 0,
                                                "paper_window": window})
            timeout = window["end"] - time.time()
            if stop_at is not None:
                timeout = min(timeout, stop_at - time.monotonic())
            if timeout <= 0:
                break
            LOG.info("AUTO_SWITCH start=%s end=%s polymarket=%s predictfun=%s",
                     datetime.fromtimestamp(start, timezone.utc).isoformat(),
                     datetime.fromtimestamp(window["end"], timezone.utc).isoformat(),
                     window["polymarket"], window["predictfun"])
            LOG.warning("SETTLEMENT_NOT_VERIFIED: same time window does not establish equivalent rules")
            LOG.info("POLY_RULE %s", (window["polymarket_rule"] or "UNKNOWN")[:1000])
            LOG.info("PREDICT_RULE %s", (window["predictfun_rule"] or "UNKNOWN")[:1000])
            try:
                await asyncio.wait_for(check(window_args, window_end=window["end"]), timeout=timeout)
            except asyncio.TimeoutError:
                trader = getattr(args, "live_trader", None)
                if trader is not None and trader.pending:
                    raise LiveTradeHalt("Live order attempt timed out; reconnect is disabled until reconciliation") from None
                LOG.info("AUTO_UNSUBSCRIBE: deadline reached, old WS subscriptions closed")
            except (LiveTradeHalt, LiveRunComplete, LiveSettlementPending, LiveConfirmationPending):
                raise
            except Exception as error:
                # SDK error messages may contain credentials; record only types and frame locations.
                LOG.error("AUTO_CHECK_FAILED error_type=%s context_type=%s frames=%s",
                          type(error).__name__, type(error.__context__).__name__ if error.__context__ else None,
                          [(Path(frame.filename).name, frame.lineno, frame.name)
                           for frame in traceback.extract_tb(error.__traceback__)])
                trader = getattr(args, "live_trader", None)
                if trader is not None and trader.pending:
                    raise LiveTradeHalt("Live execution/cleanup failed after order submission; do not reconnect") from None
                LOG.warning("AUTO_RECONNECT slot=%s reason=%s", start, type(error).__name__)
        delay = min(10, max(0, start + 300 - time.time()))
        if stop_at is not None:
            delay = min(delay, max(0, stop_at - time.monotonic()))
        await asyncio.sleep(delay)


async def run(args, window_end=None):
    if not os.getenv("PREDICT_FUN_API_KEY"):
        raise ValueError("PREDICT_FUN_API_KEY is required for Predict.fun WS")
    ids = [f"polymarket:{args.polymarket}", f"predict_fun:{args.predictfun}"]
    venues, sides = ["polymarket", "predictfun"], [args.polymarket_side, args.predictfun_side]
    metadata = await asyncio.gather(*(asyncio.to_thread(load_market, venue, id_) for venue, id_ in zip(venues, ids)))
    configs = [config for market, config in metadata]
    allowed = True
    for venue, side, (market, config) in zip(venues, sides, metadata):
        minimum = market.info.get("orderMinSize") if venue == "polymarket" else None
        LOG.info("MARKET venue=%s id=%s side=%s label=%s title=%s active=%s close=%s minimum_shares=%s fee=%s",
                 venue, market.id, side, getattr(market, side).label, market.title, market.active,
                 market.close_datetime, minimum, config or "UNKNOWN")
        if market.active is False or (market.close_timestamp and market.close_timestamp <= time.time_ns() // 1000000):
            LOG.warning("Market closed/inactive: %s", market.id)
            allowed = False
        if minimum is not None and args.quantity < decimal(minimum):
            LOG.warning("Requested quantity is below %s minimum %s", venue, minimum)
            allowed = False
    LOG.info("ASSUMPTION: complementary settlement pays 1 per pair; collateral compared at 1:1. Rules/FX/execution are not verified. Fees are rounded estimates, no rebates assumed. No orders are sent.")
    if not allowed:
        LOG.info("CHECK state=MARKET_OR_SIZE_NOT_ELIGIBLE net_estimate=UNKNOWN; subscription not started")
        return
    if window_end is not None and time.time() >= window_end:
        return
    streams = [synpath.PolymarketMarketStream(connect=connector, max_queue=1000),
               synpath.PredictFunMarketStream(connect=connector, max_queue=1000)]
    async with AsyncExitStack() as stack:
        for stream in streams:
            await stack.enter_async_context(stream)
        await asyncio.gather(*(stream.watch_order_book([id_]) for stream, id_ in zip(streams, ids)))
        tasks = [asyncio.create_task(drain(stream)) for stream in streams]
        started = time.monotonic()
        try:
            while (not args.seconds or time.monotonic() - started < args.seconds) and (window_end is None or time.time() < window_end):
                for task in tasks:
                    if task.done():
                        task.result()
                        raise RuntimeError("Venue stream stopped")
                recorder = getattr(args, "paper_recorder", None)
                trader = getattr(args, "live_trader", None)
                if trader is not None and time.monotonic() - trader.balance_checked_at >= 15:
                    await trader.check_balances()
                directions = [["yes", "no"], ["no", "yes"]] if recorder is not None or trader is not None else [sides]
                candidates = []
                for direction in directions:
                    books = [stream.book(id_, side) for stream, id_, side in zip(streams, ids, direction)]
                    if not all(stream.connected.is_set() and book is not None and book.ready for stream, book in zip(streams, books)):
                        LOG.info("WAITING: both WS books must be connected and complete")
                    else:
                        asks = [book.levels()[1] for book in books]
                        result = calculate(*asks, args.quantity, configs)
                        output = {"state": result["state"] if allowed else "MARKET_OR_SIZE_NOT_ELIGIBLE",
                                  "sides": direction, "quantity": args.quantity, "gross": result["gross"], "net_estimate": result["net"] if allowed else None,
                                  "net_per_pair": result.get("net_per_pair") if allowed else None,
                                  "legs": result["legs"], "source_timestamps_ms": [book.timestamp for book in books]}
                        if getattr(args, "auto_btc", False):
                            output["settlement_equivalence"] = "NOT_VERIFIED"
                            output["window_end_epoch"] = window_end
                            output["quote_state"] = output["state"]
                            output["quoted_net_difference"] = output["net_estimate"]
                            output["net_estimate"] = None
                            output["net_per_pair"] = None
                            output["state"] = "SETTLEMENT_NOT_VERIFIED"
                        LOG.info("CHECK %s", json.dumps(output, default=str, ensure_ascii=True))
                        if recorder is not None or trader is not None:
                            candidates.append({"sides": direction, "result": result,
                                               "timestamps": [book.timestamp for book in books], "asks": asks, "configs": configs,
                                               "ticks": [decimal(market.tick_size) if market.tick_size is not None else None for market, _ in metadata]})
                if recorder is not None and len(candidates) == 2:
                    state = recorder.observe_best(args.paper_window, args.quantity, candidates, configs)
                    LOG.info("PAPER state=%s", state)
                if trader is not None and len(candidates) == 2:
                    eligible = []
                    for candidate in candidates:
                        result = candidate["result"]
                        if live_entry_reason(args.paper_window, result, candidate["timestamps"], time.time()) is None:
                            eligible.append((result["net_per_pair"], candidate))
                    def bids(index, side):
                        book = streams[index].book(ids[index], side)
                        if not streams[index].connected.is_set() or book is None or not book.ready or book.timestamp is None or not 0 <= time.time() * 1000 - book.timestamp <= 15000:
                            return []
                        return book.levels()[0]
                    for _, candidate in sorted(eligible, key=lambda row: row[0], reverse=True):
                        state = await trader.execute(args.paper_window, candidate, args.quantity, bids)
                        LOG.info("LIVE_CHECK state=%s sides=%s", state, candidate["sides"])
                        if state in ("PAIRED", "NO_FILL", "ALREADY_ATTEMPTED"):
                            break
                delay = args.interval if window_end is None else min(args.interval, max(0, window_end - time.time()))
                await asyncio.sleep(delay)
        finally:
            try:
                # Keep consuming frames through close: the SDK queues its final marker with put_nowait.
                await stack.aclose()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


async def paper_btc(args):
    args.paper_recorder = PaperRecorder(args.records)
    async def settlements():
        while True:
            await asyncio.to_thread(args.paper_recorder.settle_pending)
            await asyncio.sleep(30)
    task = asyncio.create_task(settlements())
    checking = asyncio.create_task(auto_btc(args))
    try:
        done, _ = await asyncio.wait((task, checking), return_when=asyncio.FIRST_COMPLETED)
        for completed in done:
            completed.result()
    finally:
        task.cancel()
        checking.cancel()
        await asyncio.gather(task, checking, return_exceptions=True)
        args.paper_recorder.summary()


async def live_btc(args):
    async with LiveTrader(max_trades=args.max_trades) as trader:
        if trader.settlement_waiting or trader.confirmation_waiting:
            if args.preflight_only:
                LOG.info("LIVE_PREFLIGHT_PENDING_CONFIRMATION_OR_SETTLEMENT no_orders_sent=true")
                return
            for record in list(trader.confirmation_waiting.values()):
                await trader.wait_confirmation(record)
            for record in list(trader.settlement_waiting.values()):
                await trader.wait_settlement(record)
            raise LiveRunComplete("Resumed settlement tracking completed; no new entries sent")
        if args.preflight_only:
            slot = int(time.time()) // 300 * 300
            window = await asyncio.to_thread(discover_btc_window, slot)
            await trader.check_polymarket_buy_approval(window, Decimal(15))
            await trader.check_predict_buy_approval(window, Decimal(15))
            LOG.info("LIVE_PREFLIGHT_OK market_ids=%s,%s no_orders_sent=true", window["polymarket"], window["predictfun"])
            return
        args.live_trader = trader
        while True:
            try:
                await auto_btc(args)
                return
            except LiveSettlementPending as waiting:
                # auto_btc/run have closed their quote streams before resolution polling.
                await trader.wait_settlement(waiting.record)
                if waiting.record["stop_after"]:
                    raise LiveRunComplete("Both event settlements recorded; entry attempts stopped")
                await trader.check_balances()
            except LiveConfirmationPending as waiting:
                settlement = await trader.wait_confirmation(waiting.record)
                await trader.wait_settlement(settlement)
                raise LiveRunComplete("Delayed confirmation and event settlements recorded; no further entries sent")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("python", "rust"), default="python",
                        help="Use the standalone Rust runner for automatic BTC checks/live trading")
    parser.add_argument("--list", action="store_true", help="List current source candidates and their market IDs")
    parser.add_argument("--auto-btc", action="store_true", help="Discover and roll over the current BTC five-minute window")
    parser.add_argument("--paper", action="store_true", help="Record one simulated pair per BTC window and official settlements")
    parser.add_argument("--live", action="store_true", help="REAL cash market buys targeting about 10 shares; stop if either available balance falls below $15")
    parser.add_argument("--max-trades", type=int, default=1, help="Maximum paired entry attempts in this live run; default 1, failures count too")
    parser.add_argument("--preflight-only", action="store_true", help="Authenticate and check current BTC market approvals/balances, then exit without orders")
    parser.add_argument("--trade-credentials", action="store_true", help="Check configuration presence only; no wallet values or network requests")
    parser.add_argument("--records", default="data/prediction/paper/btc.jsonl", help="Append-only simulation records; use one process per file")
    parser.add_argument("--query", default="BTC", help="Text filter for --list")
    parser.add_argument("--polymarket", help="Numeric Polymarket market ID")
    parser.add_argument("--predictfun", help="Numeric Predict.fun market ID")
    parser.add_argument("--polymarket-side", choices=("yes", "no"), default="yes")
    parser.add_argument("--predictfun-side", choices=("yes", "no"), default="no")
    parser.add_argument("--quantity", type=decimal, help="Target shares for live cash-budget calculation; requested shares for read/paper checks")
    parser.add_argument("--interval", type=float, default=2, help="Log interval; WS updates are continuous")
    parser.add_argument("--seconds", type=float, default=0, help="Stop after this many seconds; 0 runs until Ctrl+C")
    args = parser.parse_args()
    if args.backend == "rust":
        from user_strategies.prediction_rust import btc
        try:
            raise SystemExit(btc(args))
        except ValueError as error:
            parser.error(str(error))
    if args.trade_credentials:
        print(json.dumps(credentials_status()))
        return
    if args.max_trades <= 0:
        parser.error("--max-trades must be a positive integer")
    if not math.isfinite(args.interval) or not math.isfinite(args.seconds) or args.interval < 0.2 or args.seconds < 0:
        parser.error("interval >= 0.2 and seconds >= 0 are required")
    if args.auto_btc and (args.list or args.polymarket or args.predictfun):
        parser.error("--auto-btc cannot be combined with --list or fixed market IDs")
    if args.paper and not args.auto_btc:
        parser.error("--paper requires --auto-btc")
    if args.live and (not args.auto_btc or args.paper or args.list):
        parser.error("--live requires --auto-btc and cannot be combined with --paper or --list")
    if args.preflight_only and not args.live:
        parser.error("--preflight-only requires --live")
    if args.live and args.quantity is not None and args.quantity > 10:
        parser.error("Live target quantity for cash-budget calculation cannot exceed 10")
    if args.auto_btc and args.polymarket_side == args.predictfun_side:
        parser.error("Automatic BTC checking needs opposite directions on the two venues")
    if not args.list and (args.quantity is None or args.quantity <= 0):
        parser.error("positive --quantity is required")
    if not args.list and not args.auto_btc and (not args.polymarket or not args.predictfun):
        parser.error("--polymarket, --predictfun and positive --quantity are required")
    if not args.list and not args.auto_btc and not all(value.isdigit() for value in (args.polymarket, args.predictfun)):
        parser.error("Market IDs must be numeric")
    log_dir = Path(__file__).resolve().parents[1] / "logs" / "prediction"
    log_dir.mkdir(parents=True, exist_ok=True)
    mode = "live" if args.live else "paper" if args.paper else "check"
    log_path = log_dir / f"{mode}-{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_path, encoding="utf-8")])
    LOG.info("LOG_FILE %s", log_path)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.list:
            list_pairs(args.query)
        elif args.auto_btc:
            asyncio.run(live_btc(args) if args.live else paper_btc(args) if args.paper else auto_btc(args))
        else:
            asyncio.run(run(args))
    except LiveRunComplete as completed:
        LOG.info("LIVE_RUN_COMPLETE %s", completed)
    except LiveTradeHalt as error:
        LOG.error("LIVE_HALTED %s", error)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        LOG.info("Stopped; WS subscriptions closed")
    except Exception as error:
        LOG.error("Check failed: %s", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
