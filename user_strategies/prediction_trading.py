"""Synpath real-order bridge, with explicit outcome prices and persistent risk limits."""

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import AsyncExitStack
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, localcontext
from pathlib import Path

import synpath
import httpx
from synpath.trading.credentials import PolymarketCredentials, PredictFunCredentials
from synpath.trading.errors import InvalidOrder, InsufficientFunds, OrderRejected, MarketHalted
from synpath.trading.types import Order, OrderRequest, OrderType, Side, TimeInForce, OrderStatus, SettlementState, Liquidity

LOG = logging.getLogger("prediction-check")
MIN_AVAILABLE = Decimal(15)
MIN_NET_TOTAL = Decimal("1")
MAX_NET_SHARE_GAP = Decimal("0.5")


def live_entry_reason(window, result, timestamps, now):
    from user_strategies.prediction_paper import entry_reason

    reason = entry_reason(window, result, timestamps, now, min_net_per_pair=Decimal(0))
    if reason is not None:
        return reason
    if result["net"] - Decimal("0.02") <= MIN_NET_TOTAL:
        return "NET_TOTAL_NOT_ABOVE_1_AFTER_BUFFER"
    return None


class LiveTradeHalt(RuntimeError):
    """Stop the live loop; journal and venue orders require review."""


class LiveRunComplete(Exception):
    """The requested number of entry attempts has completed; exit normally."""


class LiveSettlementPending(Exception):
    """Close quote subscriptions and wait for this attempt's event resolution."""

    def __init__(self, record):
        self.record = record
        super().__init__("Entry stopped; awaiting both event settlements")


class LiveConfirmationPending(Exception):
    """Stop quote scanning, but continue read-only confirmation queries."""

    def __init__(self, record):
        self.record = record
        super().__init__("Entry stopped; confirmation queries will continue")


def credentials_status():
    names = ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_SIGNATURE_TYPE", "POLYMARKET_FUNDER",
             "PREDICT_FUN_PRIVATE_KEY", "PREDICT_FUN_API_KEY", "PREDICT_FUN_ACCOUNT_ADDRESS")
    return {name: bool(os.getenv(name)) for name in names}


def make_request(venue, market_id, outcome, action, quantity, token_price, client_id):
    if venue not in ("polymarket", "predict_fun") or outcome not in ("yes", "no") or action not in ("buy", "sell"):
        raise ValueError("Invalid venue, outcome or action")
    if not quantity.is_finite() or quantity <= 0 or not token_price.is_finite() or not 0 < token_price < 1:
        raise ValueError("Invalid amount or protection price")
    if not str(market_id).isdigit():
        raise ValueError("Numeric native market ID required")
    # Synpath always expresses price on the YES leg. SELL opens NO unless reduce_only.
    side = Side.BUY if (outcome == "yes") == (action == "buy") else Side.SELL
    return OrderRequest(market_id=f"{venue}:{market_id}", side=side, amount=quantity,
                        price=token_price if outcome == "yes" else 1 - token_price,
                        type=OrderType.LIMIT, time_in_force=TimeInForce.GTC if action == "buy" else TimeInForce.IOC,
                        reduce_only=action == "sell", client_order_id=client_id)


def depth_limit(levels, quantity):
    remaining = quantity
    for level in levels:
        if not 0 < level.price < 1 or level.size <= 0:
            continue
        remaining -= min(level.size, remaining)
        if remaining == 0:
            return level.price
    return None


def safe_order(order):
    return {name: getattr(order, name) for name in
            ("id", "venue", "market_id", "side", "status", "amount", "filled", "remaining", "price", "reduce_only")}


def safe_rejection(error):
    """Emit only known reason codes/descriptions, never provider bodies or keys."""
    body = getattr(error, "body", None)
    messages = [str(error), str(getattr(error, "reason", "") or "")]
    if isinstance(body, dict):
        for source in (body, body.get("error"), body.get("data")):
            if isinstance(source, dict):
                messages.extend(str(source[key]) for key in ("error", "errorMsg", "message", "code")
                                if isinstance(source.get(key), str))
            elif isinstance(source, str):
                messages.append(source)
    text = " ".join(messages).lower().replace("_", " ")
    reasons = (
        (("no orders found to match", "fak no match"), "FAK_NO_MATCH", "当前价格范围内没有可立即成交的挂单"),
        (("fully filled or killed", "fok not filled"), "FOK_NOT_FILLED", "无法立即全部成交，FOK 订单被拒绝"),
        (("allowance", "approval"), "ALLOWANCE", "授权额度或代币操作权限不足；余额也需核对"),
        (("not enough balance", "insufficient funds", "insufficient balance"), "BALANCE", "可用余额或持仓不足"),
        (("tick size",), "INVALID_TICK", "价格不符合最小报价单位"),
        (("lower than the minimum", "minimum order", "minimum amount"), "BELOW_MINIMUM", "订单低于平台最小数量或金额"),
        (("invalid signature",), "INVALID_SIGNATURE", "订单签名无效"),
        (("duplicated", "duplicate"), "DUPLICATE_ORDER", "重复订单"),
        (("crosses book", "crosses the book", "post only would cross"), "POST_ONLY_WOULD_CROSS", "只挂单订单会立即成交"),
        (("cancel-only", "cancel only", "post-only mode", "post only mode", "trading is currently disabled", "closed only mode"),
         "TRADING_RESTRICTED", "平台当前限制新订单交易"),
        (("order owner has to", "order signer address has to"), "WALLET_KEY_MISMATCH", "订单钱包与 API 身份不匹配"),
        (("invalid order payload", "invalid amount", "invalid price", "precision"), "INVALID_ORDER_PARAMETERS", "订单参数或精度不符合平台要求"),
    )
    code, message = "UNCLASSIFIED", "平台拒单，未识别到明确原因；不推断为滑点"
    for phrases, known_code, description in reasons:
        if any(phrase in text for phrase in phrases):
            code, message = known_code, description
            break
    category = code if code in ("ALLOWANCE", "BALANCE") else "VENUE_REJECTED"
    status = getattr(error, "status", None)
    return {"reason_category": category, "venue_reason": code, "reason_message": message,
            "http_status": status if isinstance(status, int) and 100 <= status <= 599 else None}


def fill_inventory(fills, reduce_only=False):
    with localcontext() as context:
        context.prec = 64
        gross = sum((fill.amount for fill in fills), Decimal(0))
        share_fee = Decimal(0)
        for fill in fills:
            currency = (getattr(fill, "fee_currency", None) or "").lower()
            if currency == "shares" and not reduce_only:
                fee = getattr(fill, "fee", None)
                if fee is None or not fee.is_finite() or fee < 0:
                    raise ValueError("Unknown share-denominated fee")
                share_fee += fee
        if share_fee > gross:
            raise ValueError("Share fees exceed gross fill")
        return {"gross_shares": gross, "share_fees": share_fee, "net_shares": gross - share_fee}


def sell_quantity(index, quantity):
    if index == 0:
        return quantity.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    from synpath.trading.predict_fun_signing import significant, to_wei, from_wei
    return from_wei(significant(to_wei(quantity), 5))


def estimated_share_fee(asks, quantity, config):
    remaining, fee = quantity, Decimal(0)
    with localcontext() as context:
        context.prec = 64
        for level in asks:
            if not 0 < level.price < 1 or level.size <= 0:
                continue
            amount = min(remaining, level.size)
            fee += config["rate"] * min(level.price, 1 - level.price) * amount / level.price
            remaining -= amount
            if remaining == 0:
                break
    return fee



class LiveTrader:
    def __init__(self, path="data/prediction/live/orders.jsonl", max_trades=1):
        if not isinstance(max_trades, int) or max_trades <= 0:
            raise ValueError("max_trades must be a positive integer")
        self.max_trades = max_trades
        self.attempts = 0
        self.path = Path(path)
        self.reload_journal()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stack = AsyncExitStack()
        self.adapters = []
        self.inventories = {}
        self.cash_orders = {}
        self.poly_observer = None
        self.predict_observer = None
        self.confirmation_updated = asyncio.Event()
        self.order_times = {}
        self.timing_reported = set()

    def reload_journal(self):
        self.rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()] if self.path.exists() else []
        intents = {row["attempt"] for row in self.rows if row["type"] == "INTENT"}
        reconciled = {row["attempt"] for row in self.rows if row["type"] == "RECONCILED"}
        finished = {row["attempt"] for row in self.rows if row["type"] == "DONE"} | reconciled
        self.settlement_waiting = {row["attempt"]: row for row in self.rows
                                   if row["type"] == "AWAIT_SETTLEMENT" and row["attempt"] not in finished}
        self.confirmation_waiting = {row["attempt"]: row for row in self.rows
                                    if row["type"] == "AWAIT_CONFIRMATION" and row["attempt"] not in finished
                                    and row["attempt"] not in self.settlement_waiting}
        if intents - finished - self.settlement_waiting.keys() - self.confirmation_waiting.keys() or any(row["type"] == "HALT" and row["attempt"] not in reconciled for row in self.rows):
            raise LiveTradeHalt("Unresolved or halted live journal; reconcile before starting again")
        self.windows = {row["window_start"] for row in self.rows if row["type"] == "INTENT"}
        self.exit_orders = {row["order"]["id"] for row in self.rows
                            if row["type"] == "UNWIND_ORDER" and row.get("order")}
        self.pending = bool(self.settlement_waiting or self.confirmation_waiting)
        self.balance_checked_at = 0

    def write(self, kind, attempt, **fields):
        row = {"type": kind, "attempt": attempt, "recorded_at_ms": time.time_ns() // 1000000, **fields}
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, default=str) + "\n")
            output.flush()
            os.fsync(output.fileno())
        self.rows.append(row)
        LOG.info("LIVE %s", json.dumps(row, default=str))

    async def __aenter__(self):
        required = ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_SIGNATURE_TYPE", "PREDICT_FUN_PRIVATE_KEY", "PREDICT_FUN_API_KEY")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))
        signature_type = int(os.environ["POLYMARKET_SIGNATURE_TYPE"])
        if signature_type not in (0, 1, 2, 3) or (signature_type != 0 and not os.getenv("POLYMARKET_FUNDER")):
            raise ValueError("Invalid wallet type or missing POLYMARKET_FUNDER")
        if os.getenv("PREDICT_FUN_TESTNET", "false").lower() not in ("false", "0", ""):
            raise ValueError("Live BTC runner requires mainnet")
        poly = PolymarketCredentials(private_key=os.environ["POLYMARKET_PRIVATE_KEY"], signature_type=signature_type,
                                    funder=os.getenv("POLYMARKET_FUNDER"), api_key=os.getenv("POLYMARKET_API_KEY"),
                                    api_secret=os.getenv("POLYMARKET_API_SECRET"), api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE"))
        predict = PredictFunCredentials(private_key=os.environ["PREDICT_FUN_PRIVATE_KEY"], api_key=os.environ["PREDICT_FUN_API_KEY"],
                                        account_address=os.getenv("PREDICT_FUN_ACCOUNT_ADDRESS"), rpc_url=os.getenv("PREDICT_FUN_RPC_URL"))
        # Never emit SDK HTTP bodies, signatures, authenticated URLs or credential objects.
        logging.getLogger("synpath").setLevel(logging.CRITICAL)
        try:
            lock = self.stack.enter_context(self.path.with_suffix(".lock").open("a+b"))
            if lock.seek(0, 2) == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise LiveTradeHalt("Another process owns this live journal") from None
            self.reload_journal()
            for index, (factory, credentials) in enumerate(((synpath.PolymarketTrading, poly), (synpath.PredictFunTrading, predict))):
                client = await self.stack.enter_async_context(httpx.AsyncClient(timeout=30, trust_env=False,
                                                                                headers={"x-api-key": predict.api_key} if index else {}))
                options = {"client": client}
                if index:
                    options["rpc_client"] = await self.stack.enter_async_context(httpx.AsyncClient(timeout=20, trust_env=False))
                self.adapters.append(await self.stack.enter_async_context(factory(credentials, **options)))
            balances = await self.check_balances(entry=not self.pending)
            LOG.info("LIVE_ACCOUNT available=%s currencies=%s", [balance.available for balance in balances],
                     [balance.currency for balance in balances])
            from user_strategies.prediction_user_ws import PolymarketUserObserver, PredictFunUserObserver
            self.poly_observer = await self.stack.enter_async_context(PolymarketUserObserver(self.adapters[0], self.confirmation_updated))
            self.predict_observer = await self.stack.enter_async_context(PredictFunUserObserver(self.adapters[1], self.confirmation_updated))
        except BaseException:
            await self.stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc):
        await self.stack.aclose()

    async def check_balances(self, required=None, entry=True):
        balances = await asyncio.gather(*(adapter.fetch_balance() for adapter in self.adapters))
        if any(not balance.available.is_finite() or balance.available < (MIN_AVAILABLE if entry else 0) for balance in balances):
            raise LiveTradeHalt("A venue's available balance is below $15 or invalid")
        if required is not None and any(balance.available < value for balance, value in zip(balances, required)):
            raise LiveTradeHalt("Insufficient available collateral for protected order cost and fees")
        self.balance_checked_at = time.monotonic()
        return balances

    async def check_predict_buy_approval(self, window, required):
        from eth_abi import encode
        from eth_utils import keccak
        from decimal import ROUND_CEILING

        adapter = self.adapters[1]
        info = await adapter.market_info(f"predict_fun:{window['predictfun']}")
        spender = adapter.network.exchange(neg_risk=info.neg_risk, yield_bearing=info.yield_bearing)
        data = "0x" + (keccak(text="allowance(address,address)")[:4] + encode(["address", "address"], [adapter.address, spender])).hex()
        response = await adapter.rpc.post(adapter.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                                               "params": [{"to": adapter.network.usdt, "data": data}, "latest"]})
        raw = response.json().get("result")
        if not isinstance(raw, str) or not raw.startswith("0x"):
            raise LiveTradeHalt("Predict.fun collateral allowance could not be verified")
        allowance = int(raw, 16)
        needed = int((required * Decimal(10) ** 18).to_integral_value(rounding=ROUND_CEILING))
        if allowance < needed:
            raise LiveTradeHalt("Predict.fun collateral allowance is insufficient; no orders submitted")
        from user_strategies.approve_predict import collateral_token
        token = collateral_token(info.neg_risk, info.yield_bearing)
        data = "0x" + (keccak(text="isApprovedForAll(address,address)")[:4] + encode(["address", "address"], [adapter.address, spender])).hex()
        response = await adapter.rpc.post(adapter.rpc_url, json={"jsonrpc": "2.0", "id": 2, "method": "eth_call",
                                                               "params": [{"to": token, "data": data}, "latest"]})
        result = response.json().get("result")
        if not isinstance(result, str) or int(result, 16) != 1:
            raise LiveTradeHalt("Predict.fun share operator approval is missing; exit cannot be guaranteed")

    async def check_polymarket_buy_approval(self, window, required):
        from synpath.trading.polymarket_signing import EXCHANGE, NEG_RISK_EXCHANGE

        adapter = self.adapters[0]
        tokens = await adapter.market_tokens(f"polymarket:{window['polymarket']}")
        spender = NEG_RISK_EXCHANGE if tokens.neg_risk else EXCHANGE
        with localcontext() as context:
            context.prec = 100  # ERC20 unlimited allowances exceed Decimal's default precision.
            allowances = await adapter.fetch_allowances()
            operators = await adapter.fetch_allowances(token_id=tokens.token("yes"))
        value = {address.lower(): amount for address, amount in allowances.items()}.get(spender.lower(), Decimal(0))
        if value < required:
            raise LiveTradeHalt("Polymarket collateral allowance is insufficient; no orders submitted")
        if {address.lower(): amount for address, amount in operators.items()}.get(spender.lower(), Decimal(0)) <= 0:
            raise LiveTradeHalt("Polymarket share operator approval is missing")

    async def send(self, index, request, attempt, kind):
        started_at = time.time_ns() // 1000000
        observer = self.poly_observer if index == 0 else self.predict_observer
        if observer is not None:
            await observer.watch(request.market_id)
        try:
            order = await self.adapters[index].create_order(request)
        except (InvalidOrder, InsufficientFunds, OrderRejected, MarketHalted) as error:
            self.write(kind, attempt, leg=index, rejected=type(error).__name__, **safe_rejection(error))
            return "REJECTED"
        except Exception as error:
            self.write(kind, attempt, leg=index, unknown=type(error).__name__)
            return None
        if request.reduce_only:
            self.exit_orders.add(order.id)
            if isinstance(order, Order):
                order = order.model_copy(update={"reduce_only": True})
        self.write(kind, attempt, leg=index, order=safe_order(order))
        self.order_times.setdefault(order.id, {}).update(submitted_at_ms=started_at, acknowledged_at_ms=time.time_ns() // 1000000)
        return order

    async def send_cash(self, index, plan, attempt):
        from user_strategies.prediction_cash_orders import submit_cash_buy

        def before_submit(order_id, cash, signed_shares):
            if cash > plan["cash_budget"]:
                raise ValueError("Signed cash exceeds the planned budget")
            quantity_field = "signed_minimum_shares" if index == 0 else "signed_target_shares"
            self.write("CASH_SUBMIT_INTENT", attempt, leg=index, order_id=order_id, cash_budget=cash,
                       target_shares=plan["target_shares"], **{quantity_field: signed_shares})
            self.order_times.setdefault(order_id, {})["submitted_at_ms"] = self.rows[-1]["recorded_at_ms"]
        try:
            order = await submit_cash_buy(self.adapters[index], plan, before_submit)
        except (InvalidOrder, InsufficientFunds, OrderRejected, MarketHalted) as error:
            self.write("ORDER", attempt, leg=index, rejected=type(error).__name__, **safe_rejection(error))
            return "REJECTED"
        except Exception as error:
            self.write("ORDER", attempt, leg=index, unknown=type(error).__name__)
            return None
        self.cash_orders[order.id] = plan
        computed_id = getattr(order, "info", {}).get("computed_id", order.id)
        self.order_times.setdefault(order.id, {}).update(self.order_times.get(computed_id, {}))
        self.order_times[order.id]["acknowledged_at_ms"] = time.time_ns() // 1000000
        self.write("ORDER", attempt, leg=index, order=safe_order(order), cash_budget=plan["cash_budget"],
                   target_shares=plan["target_shares"],
                   amount_semantics="SIGNED_MINIMUM_SHARES_NOT_TARGET" if index == 0 else "SIGNED_TARGET_SHARES")
        return order

    async def rest_query(self, index, order_id, operation, query):
        started = time.monotonic()
        try:
            result = await query()
        except Exception as error:
            LOG.warning("REST_QUERY venue=%s order_id=%s operation=%s elapsed_ms=%s error=%s",
                        "polymarket" if index == 0 else "predict_fun", order_id, operation,
                        round((time.monotonic() - started) * 1000), type(error).__name__)
            raise
        elapsed = round((time.monotonic() - started) * 1000)
        self.order_times.setdefault(order_id, {})["last_rest_query_ms"] = elapsed
        LOG.info("REST_QUERY venue=%s order_id=%s operation=%s elapsed_ms=%s",
                 "polymarket" if index == 0 else "predict_fun", order_id, operation, elapsed)
        return result

    def report_confirmation_timing(self, index, order):
        if order.id in self.timing_reported:
            return
        self.timing_reported.add(order.id)
        times = dict(self.order_times.get(order.id, {}))
        observer = self.poly_observer if index == 0 else self.predict_observer
        if observer is not None:
            times.update(observer.times.get(order.id, {}))
        started = times.get("submitted_at_ms")
        delays = {key.replace("_at_ms", "_delay_ms"): value - started
                  for key, value in times.items() if key.endswith("_at_ms") and key != "submitted_at_ms" and started is not None}
        inventory = self.inventories.get(order.id, {})
        LOG.info("ORDER_CONFIRMATION_TIMING order_id=%s source=%s rest_receipts=%s ws_receipts=%s times=%s delays_since_submit=%s",
                 order.id, inventory.get("confirmation_source"), inventory.get("rest_receipt_count"),
                 inventory.get("ws_receipt_count"), times, delays)

    async def confirmed(self, index, order):
        if order == "REJECTED":
            return Decimal(0)
        if order is None:
            return None
        try:
            times = self.order_times.setdefault(order.id, {})
            if "submitted_at_ms" not in times:
                submitted = next((row for row in self.rows if row["type"] == "CASH_SUBMIT_INTENT"
                                  and row.get("order_id") == order.id), None)
                if submitted is not None:
                    times["submitted_at_ms"] = submitted["recorded_at_ms"]
            if index == 0:
                # A cold restart must load the known market before resolving its token IDs.
                await self.adapters[index].market_tokens(order.market_id)
            observer = self.poly_observer if index == 0 else self.predict_observer
            if observer is not None:
                await observer.watch(order.market_id)
            current = await self.rest_query(index, order.id, "fetch_order", lambda: self.adapters[index].fetch_order(order.id))
            fills, cursor = {}, None
            for _ in range(5):
                page = await self.rest_query(index, order.id, "fetch_my_trades", lambda: self.adapters[index].fetch_my_trades(market_id=order.market_id, order_id=order.id, cursor=cursor))
                for fill in page:
                    fills[fill.id] = fill
                cursor = page.next_cursor
                if not cursor:
                    break
            now = time.time_ns() // 1000000
            observed = self.order_times.setdefault(order.id, {})
            rest_ids = set(fills)
            if fills:
                observed.setdefault("rest_first_fill_at_ms", now)
            if any(f.settlement == SettlementState.CONFIRMED for f in fills.values()):
                observed.setdefault("rest_first_confirmed_at_ms", now)
            ws_receipts = {}
            if observer is not None:
                from user_strategies.prediction_user_ws import merge_receipts
                if order.id in observer.conflicts:
                    return None
                ws_receipts = observer.fills.get(order.id, {})
                fills = merge_receipts(fills.values(), ws_receipts.values())
            if index == 0 and not cursor and current.filled > sum((f.amount for f in fills.values()), Decimal(0)):
                from synpath.trading.polymarket import fills_of
                adapter = self.adapters[index]
                tokens = await adapter.market_tokens(order.market_id)
                for trade_id in (getattr(current, "info", {}).get("associate_trades") or [])[:20]:
                    raw = await self.rest_query(index, order.id, "fetch_trade_by_id", lambda: adapter._private("GET", "/data/trades", params={"id": trade_id}))
                    for row in (raw or {}).get("data") or []:
                        for fill in fills_of(row, api_key=adapter._api_key or "", wallets={adapter.wallet, adapter.signer.address},
                                             market_id=order.market_id, outcome_of=tokens.outcome_of, account=adapter.account):
                            if fill.order_id.lower() == order.id.lower():
                                fills[fill.id] = fill
                                rest_ids.add(fill.id)
                                observed.setdefault("rest_first_fill_at_ms", time.time_ns() // 1000000)
                                if fill.settlement == SettlementState.CONFIRMED:
                                    observed.setdefault("rest_first_confirmed_at_ms", time.time_ns() // 1000000)
                if ws_receipts:
                    fills = merge_receipts(fills.values(), ws_receipts.values())
            if cursor or any(fill.settlement == SettlementState.MATCHED for fill in fills.values()):
                return None
            settled = [fill for fill in fills.values() if fill.settlement == SettlementState.CONFIRMED]
            if index == 1 and ws_receipts and any(fill.liquidity != Liquidity.MAKER and
                    (fill.fee is None or (fill.fee > 0 and fill.fee_currency not in ("shares", "USDT"))) for fill in settled):
                LOG.warning("CONFIRMATION_FEE_UNKNOWN order_id=%s continue_rest_queries=true", order.id)
                return None
            reducing = order.id in self.exit_orders or getattr(order, "reduce_only", False)
            inventory = fill_inventory(settled, reduce_only=reducing)
            inventory.update(confirmation_source="WS_AND_REST_ORDER" if ws_receipts else "REST",
                             rest_receipt_count=len(rest_ids), ws_receipt_count=len(ws_receipts))
            cash_fees = [getattr(fill, "fee", None) for fill in settled
                         if (getattr(fill, "fee_currency", None) or "").lower() != "shares"]
            inventory["cash_fee"] = sum(cash_fees, Decimal(0)) if all(fee is not None for fee in cash_fees) else None
            if all(getattr(fill, "price", None) is not None for fill in settled):
                outcome_yes = (order.side == Side.BUY) != reducing
                inventory["cash_value"] = sum((fill.amount * (fill.price if outcome_yes else 1 - fill.price) for fill in settled), Decimal(0))
            confirmed = inventory["gross_shares"]
            cash_mode = order.id in self.cash_orders
            if not cash_mode and not ws_receipts and confirmed >= order.amount:
                self.inventories[order.id] = inventory
                self.report_confirmation_timing(index, order)
                return confirmed
            native = getattr(current, "info", {})
            terminal_market = cash_mode and str(native.get("status", "")).lower() == "matched" and native.get("order_type") == "FAK"
            if terminal_market or current.status in (OrderStatus.CLOSED, OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
                accounted = sum((fill.amount for fill in fills.values()
                                 if fill.settlement in (SettlementState.CONFIRMED, SettlementState.FAILED)), Decimal(0))
                reconciled = accounted == current.filled if ws_receipts else accounted >= current.filled
                if reconciled:
                    self.inventories[order.id] = inventory
                    self.report_confirmation_timing(index, order)
                    return confirmed
                return None
            return None
        except ValueError:
            LOG.warning("CONFIRMATION_DATA_INVALID order_id=%s new_entries=false", order.id)
            return None
        except Exception:
            return None

    async def wait_fills(self, orders, seconds):
        deadline = time.monotonic() + seconds
        while True:
            observers = [observer for observer in (self.poly_observer, self.predict_observer) if observer is not None]
            if observers:
                self.confirmation_updated.clear()
            sizes = await asyncio.gather(*(self.confirmed(i, order) for i, order in enumerate(orders)))
            if all(size is not None for size in sizes) or time.monotonic() >= deadline:
                return sizes
            if not observers:
                await asyncio.sleep(2)
            else:
                try:
                    await asyncio.wait_for(self.confirmation_updated.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass

    async def available_shares(self, index, window, outcome):
        from eth_abi import encode
        from eth_utils import keccak
        adapter = self.adapters[index]
        if index == 0:
            from synpath.trading.polymarket_signing import CONDITIONAL_TOKENS
            tokens = await adapter.market_tokens(f"polymarket:{window['polymarket']}")
            owner, token, token_id = adapter.wallet, CONDITIONAL_TOKENS, tokens.token(outcome)
            client, url, decimals = adapter._http, os.getenv("POLYMARKET_RPC_URL") or "https://polygon-bor-rpc.publicnode.com", 6
        else:
            from user_strategies.approve_predict import collateral_token
            info = await adapter.market_info(f"predict_fun:{window['predictfun']}")
            owner, token, token_id = adapter.address, collateral_token(info.neg_risk, info.yield_bearing), info.token(outcome)
            client, url, decimals = adapter.rpc, adapter.rpc_url, 18
        data = "0x" + (keccak(text="balanceOf(address,uint256)")[:4] + encode(["address", "uint256"], [owner, int(token_id)])).hex()
        response = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                              "params": [{"to": token, "data": data}, "latest"]})
        raw = response.json().get("result")
        if not isinstance(raw, str) or not raw.startswith("0x"):
            raise LiveTradeHalt("On-chain sellable shares could not be verified")
        return Decimal(int(raw, 16)) / Decimal(10) ** decimals

    async def cash_buy_plans(self, window, candidate, quantity):
        from user_strategies.prediction_cash_orders import cash_budget, PREDICT_SLIPPAGE_BPS
        from predict_sdk import OrderBuilder
        from predict_sdk.constants import ChainId, Side as PredictSide
        from predict_sdk.types import Book, MarketHelperInput
        from synpath.trading.predict_fun_signing import significant, to_wei, from_wei
        tokens = await self.adapters[0].market_tokens(f"polymarket:{window['polymarket']}")
        info = await self.adapters[0].token_info(tokens.token(candidate["sides"][0]), refresh=True)
        ticks = [Decimal(str(info.tick)), candidate["ticks"][1]]
        if any(tick is None or not tick.is_finite() or not 0 < tick <= Decimal("0.5") for tick in ticks):
            raise ValueError("Unknown or invalid market tick size")
        plans = []
        for index, venue in enumerate(("polymarket", "predict_fun")):
            budget = cash_budget(candidate["result"]["legs"][index]["cost"], venue)
            if budget < 1:
                raise ValueError("Market buy budget below venue minimum")
            quoted_budget = budget
            expected_price = candidate["result"]["legs"][index]["cost"] / quantity
            if index == 1:
                with localcontext() as context:
                    context.prec = 100
                    # Pass Decimal levels; the SDK converts via Decimal(str(value)), without floats.
                    book = Book(market_id=int(window["predictfun"]), update_timestamp_ms=int(candidate["timestamps"][1]),
                                asks=[(level.price, level.size) for level in candidate["asks"][1]
                                      if 0 < level.price < 1 and level.size > 0], bids=[])
                    amounts = OrderBuilder.make(ChainId.BNB_MAINNET).get_market_order_amounts(
                        MarketHelperInput(side=PredictSide.BUY, quantity_wei=to_wei(quantity),
                                          slippage_bps=PREDICT_SLIPPAGE_BPS, is_min_amount_out=False), book)
                    if amounts.amount != significant(to_wei(quantity), 5):
                        raise ValueError("Insufficient Predict depth for target shares")
                    budget = from_wei(amounts.maker_amount)
                    expected_price = from_wei(amounts.price_per_share)
                    worst_price = from_wei(amounts.last_price)
                if budget >= from_wei(amounts.taker_amount):
                    raise ValueError("Predict slippage budget per share must be below $1")
            plans.append({"venue": venue, "market_id": f"{venue}:{window['polymarket' if index == 0 else 'predictfun']}",
                          "outcome": candidate["sides"][index], "target_shares": quantity,
                          "cash_budget": budget, "expected_price": expected_price,
                          "tick": ticks[index]})
            if index == 1:
                plans[-1].update(quoted_cash_budget=quoted_budget, worst_price=worst_price, slippage_bps=PREDICT_SLIPPAGE_BPS)
        return plans

    async def execute(self, window, candidate, quantity, bids):

        if self.pending:
            raise LiveTradeHalt("Earlier order attempt unresolved; no new orders")
        if self.attempts >= self.max_trades:
            raise LiveRunComplete("Entry attempt limit reached")
        if window["start"] in self.windows:
            return "ALREADY_ATTEMPTED"
        if quantity > 10 or quantity <= 0 or time.time() >= window["end"] - 60:
            return "SIZE_OR_TIME_NOT_ELIGIBLE"
        quoted = candidate["result"]
        if live_entry_reason(window, quoted, candidate["timestamps"], time.time()) is not None:
            return "QUOTE_NOT_ELIGIBLE"
        try:
            plans = await self.cash_buy_plans(window, candidate, quantity)
        except ValueError:
            return "MARKET_CONSTRAINTS_NOT_VERIFIED"
        reserved = [plan["cash_budget"] * (1 + config["rate"]) + Decimal("0.01") for plan, config in zip(plans, candidate["configs"])]
        # Slippage increases the cash limit, not the signed target share quantity.
        from synpath.trading.predict_fun_signing import significant, to_wei, from_wei
        predict_gross = from_wei(significant(to_wei(quantity), 5))
        predict_share_fee = estimated_share_fee(candidate["asks"][1], predict_gross, candidate["configs"][1])
        projected = [quantity * plans[0]["cash_budget"] / quoted["legs"][0]["cost"], predict_gross - predict_share_fee]
        minimum_payout = min(projected)
        conservative_net = minimum_payout - sum(plan["cash_budget"] for plan in plans) - quoted["legs"][0]["fee"] - Decimal("0.02")
        if conservative_net <= MIN_NET_TOTAL:
            return "LIMIT_EDGE_NOT_ELIGIBLE"
        await self.check_balances(reserved)
        await self.check_polymarket_buy_approval(window, reserved[0])
        await self.check_predict_buy_approval(window, reserved[1])
        for observer, plan in zip((self.poly_observer, self.predict_observer), plans):
            if observer is not None:
                await observer.watch(plan["market_id"])
                try:
                    await asyncio.wait_for(observer.stream.connected.wait(), timeout=3)
                except asyncio.TimeoutError:
                    LOG.warning("USER_WS_NOT_CONNECTED venue=%s rest_fallback=true", plan["venue"])
        if live_entry_reason(window, quoted, candidate["timestamps"], time.time()) is not None or time.time() >= window["end"] - 60:
            return "QUOTE_EXPIRED_DURING_PREFLIGHT"
        attempt = uuid.uuid4().hex
        venues = ["polymarket", "predict_fun"]
        natives = [window["polymarket"], window["predictfun"]]
        self.write("INTENT", attempt, window_start=window["start"], window_end=window["end"], reserved=reserved,
                   sides=candidate["sides"], run_attempt=self.attempts + 1, run_max_trades=self.max_trades,
                   execution="CONCURRENT_CASH_MARKET", quoted_net_per_pair=quoted["net_per_pair"],
                   entry_min_net_total=MIN_NET_TOTAL,
                   estimated_predict_share_fee=predict_share_fee, estimated_minimum_payout=minimum_payout,
                   estimated_received_shares=projected,
                   conservative_entry_net_total=conservative_net,
                   requests=plans, max_net_share_gap=MAX_NET_SHARE_GAP)
        self.attempts += 1
        self.pending = True
        self.windows.add(window["start"])
        orders = await asyncio.gather(*(self.send_cash(i, plan, attempt) for i, plan in enumerate(plans)))
        sizes = await self.wait_fills(orders, min(45, max(0, window["end"] - time.time() - 15)))
        inventory_rows = [self.inventories.get(getattr(order, "id", None)) for order in orders]
        net_sizes = [None if size is None else inventory["net_shares"] if inventory is not None else size
                     for inventory, size in zip(inventory_rows, sizes)]
        self.write("FILLS", attempt, confirmed_shares=sizes, confirmed_net_shares=net_sizes,
                   cash_values=[inventory.get("cash_value") if inventory and size is not None else None for inventory, size in zip(inventory_rows, sizes)],
                   share_fees=[inventory["share_fees"] if inventory and size is not None else None for inventory, size in zip(inventory_rows, sizes)])
        if any(size is None for size in sizes):
            order_rows = []
            for index, order in enumerate(orders):
                if order == "REJECTED":
                    order_rows.append("REJECTED")
                    continue
                submitted = next((r for r in reversed(self.rows) if r["type"] == "CASH_SUBMIT_INTENT"
                                  and r["attempt"] == attempt and r["leg"] == index), None)
                order_id = getattr(order, "id", None) or (submitted or {}).get("order_id")
                if not order_id:
                    order_rows.append(None)
                    continue
                plan = plans[index]
                order_rows.append(safe_order(Order(id=order_id, venue=plan["venue"], market_id=plan["market_id"],
                    side=Side.BUY if plan["outcome"] == "yes" else Side.SELL,
                    amount=getattr(order, "amount", quantity), type=OrderType.MARKET, time_in_force=TimeInForce.IOC,
                    status=OrderStatus.PENDING, reduce_only=False)))
            self.write("AWAIT_CONFIRMATION", attempt, window=window, sides=candidate["sides"], orders=order_rows,
                       plans=plans, reason="ORDER_OR_CHAIN_SETTLEMENT_UNKNOWN", new_entries=False)
            record = self.rows[-1]
            self.confirmation_waiting[attempt] = record
            raise LiveConfirmationPending(record)
        for index, inventory in enumerate(inventory_rows):
            if inventory and inventory.get("cash_value") is not None and inventory["cash_value"] > plans[index]["cash_budget"] + Decimal("0.000001"):
                excess = inventory["cash_value"] - plans[index]["cash_budget"]
                self.write("BUDGET_OVERRUN", attempt, leg=index, actual=inventory["cash_value"],
                           budget=plans[index]["cash_budget"], excess=excess)
                LOG.warning("CASH_BUDGET_EXCEEDED leg=%s actual=%s budget=%s excess=%s continue_position_handling=true",
                            index, inventory["cash_value"], plans[index]["cash_budget"], excess)
        financial_entries = [row or ({"cash_value": Decimal(0), "cash_fee": Decimal(0)} if size == 0 else {})
                             for row, size in zip(inventory_rows, sizes)]
        if net_sizes == [Decimal(0), Decimal(0)]:
            self.write("DONE", attempt, state="NO_FILL")
            self.pending = False
            await self.check_balances()
            if self.attempts >= self.max_trades:
                raise LiveRunComplete(f"Completed {self.attempts} entry attempts; last result recorded")
            return "NO_FILL"
        if all(size > 0 for size in net_sizes) and abs(net_sizes[0] - net_sizes[1]) <= MAX_NET_SHARE_GAP:
            self.await_settlement(attempt, window, candidate["sides"], net_sizes, financial_entries, [],
                                  stop_after=self.attempts >= self.max_trades, state="PAIRED")
        remaining = list(net_sizes)
        exits = []
        excess = [max(Decimal(0), net_sizes[i] - net_sizes[1 - i]) for i in range(2)]
        for i, amount in enumerate(excess):
            if not amount:
                continue
            available = await self.available_shares(i, window, candidate["sides"][i])
            amount = sell_quantity(i, min(amount, available))
            if amount <= 0:
                self.write("ENTRY_STOPPED", attempt, reason="NO_SELLABLE_INVENTORY", leg=i, onchain_shares=available)
                break
            cap = depth_limit(bids(i, candidate["sides"][i]), amount)
            if cap is None:
                self.write("ENTRY_STOPPED", attempt, reason="UNWIND_DEPTH_UNAVAILABLE", leg=i, shares=amount)
                break
            request = make_request(venues[i], natives[i], candidate["sides"][i], "sell", amount, cap, f"{attempt}-unwind-{i}")
            self.write("UNWIND_INTENT", attempt, leg=i, request=request.model_dump(exclude={"params"}))
            order = await self.send(i, request, attempt, "UNWIND_ORDER")
            deadline = time.monotonic() + 30
            sold = await self.confirmed(i, order)
            while sold is None and time.monotonic() < deadline:
                await asyncio.sleep(2)
                sold = await self.confirmed(i, order)
            if sold is None or sold < 0 or sold > remaining[i]:
                self.write("HALT", attempt, reason="UNWIND_CONFIRMATION_UNKNOWN", leg=i, shares=amount, confirmed_sold=sold)
                raise LiveTradeHalt("Unwind outcome unknown; review before settlement tracking")
            remaining[i] -= sold
            exits.append(self.inventories.get(getattr(order, "id", None)) or ({"cash_value": Decimal(0), "cash_fee": Decimal(0)} if sold == 0 else {}))
            self.write("UNWIND_RESULT", attempt, leg=i, shares=amount, confirmed_sold=sold,
                       remaining_net_shares=remaining)
        self.await_settlement(attempt, window, candidate["sides"], remaining, financial_entries, exits,
                              stop_after=True, state="UNBALANCED_ENTRY")

    def await_settlement(self, attempt, window, sides, remaining, entries, exits, *, stop_after, state):
        self.write("AWAIT_SETTLEMENT", attempt, state=state, window=window, sides=sides,
                   remaining_net_shares=remaining, stop_after=stop_after,
                   entry_cash_values=[row.get("cash_value") for row in entries],
                   entry_cash_fees=[row.get("cash_fee") for row in entries],
                   exit_cash_values=[row.get("cash_value") for row in exits],
                   exit_cash_fees=[row.get("cash_fee") for row in exits])
        record = self.rows[-1]
        self.settlement_waiting[attempt] = record
        raise LiveSettlementPending(record)

    async def wait_confirmation(self, record):
        orders = [Order(**{**row, "type": OrderType.MARKET, "time_in_force": TimeInForce.IOC})
                  if isinstance(row, dict) else row for row in record["orders"]]
        for order, plan in zip(orders, record["plans"]):
            if isinstance(order, Order):
                self.cash_orders[order.id] = plan
        LOG.warning("LIVE_WAIT_CONFIRMATION attempt=%s new_entries=false queries_only=true", record["attempt"])
        while True:
            sizes = await self.wait_fills(orders, 30)
            if all(size is not None for size in sizes):
                entries = [self.inventories.get(getattr(order, "id", None)) or
                           ({"cash_value": Decimal(0), "cash_fee": Decimal(0)} if size == 0 else {})
                           for order, size in zip(orders, sizes)]
                net_sizes = [self.inventories[order.id]["net_shares"] if size else Decimal(0)
                             for order, size in zip(orders, sizes)]
                self.write("FILLS", record["attempt"], confirmed_shares=sizes, confirmed_net_shares=net_sizes,
                           cash_values=[row.get("cash_value") for row in entries],
                           confirmation_state="CONFIRMED_AFTER_DELAY", net_share_gap=abs(net_sizes[0] - net_sizes[1]),
                           new_entries=False, automatic_unwind=False)
                try:
                    self.await_settlement(record["attempt"], record["window"], record["sides"], net_sizes, entries, [],
                                          stop_after=True, state="CONFIRMED_AFTER_DELAY_REVIEW_REQUIRED")
                except LiveSettlementPending as waiting:
                    self.confirmation_waiting.pop(record["attempt"], None)
                    return waiting.record
            balances = []
            for i, side in enumerate(record["sides"]):
                try:
                    balances.append(await self.available_shares(i, record["window"], side))
                except Exception:
                    balances.append(None)
            LOG.info("LIVE_CONFIRMATION_UNRESOLVED attempt=%s confirmed_shares=%s onchain_balances=%s balances_are_not_fill_proof=true",
                     record["attempt"], sizes, balances)
            await asyncio.sleep(5)

    async def wait_settlement(self, record):
        from user_strategies.prediction_paper import fetch_settlement

        window = record["window"]
        LOG.info("LIVE_WAIT_SETTLEMENT attempt=%s window_end=%s new_entries=false", record["attempt"], window["end"])
        while True:
            if time.time() >= window["end"]:
                try:
                    factors = await asyncio.to_thread(fetch_settlement, {**window, "sides": record["sides"]})
                except Exception as error:
                    LOG.warning("LIVE_SETTLEMENT_READ_RETRY error=%s", type(error).__name__)
                    factors = None
                if factors is not None:
                    with localcontext() as context:
                        context.prec = 64
                        payouts = [Decimal(str(size)) * factor for size, factor in zip(record["remaining_net_shares"], factors)]
                        def total(values):
                            return sum((Decimal(str(value)) for value in values), Decimal(0)) if all(value is not None for value in values) else None
                        cost, proceeds = total(record["entry_cash_values"]), total(record["exit_cash_values"])
                        fees = total(record["entry_cash_fees"] + record["exit_cash_fees"])
                        gross = sum(payouts) + proceeds - cost if cost is not None and proceeds is not None else None
                        net = gross - fees if gross is not None and fees is not None else None
                    self.write("SETTLEMENT", record["attempt"], payout_factors=factors, payouts=payouts,
                               total_payout=sum(payouts), entry_cash_cost=cost, exit_cash_proceeds=proceeds,
                               cash_fees=fees, gross_pnl=gross, net_pnl=net,
                               pnl_state="VERIFIED_FEES" if net is not None else "CASH_FLOW_OR_FEES_UNKNOWN",
                               payout_state="EVENT_RESOLVED_REDEMPTION_NOT_VERIFIED")
                    self.write("DONE", record["attempt"], state="SETTLED", stop_after=record["stop_after"])
                    self.settlement_waiting.pop(record["attempt"], None)
                    self.pending = False
                    return
            await asyncio.sleep(30)
