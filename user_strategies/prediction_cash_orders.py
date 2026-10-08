"""Cash-denominated market BUY payloads. Signing helpers are from pinned Synpath."""

import time
from decimal import Decimal, ROUND_DOWN, localcontext

from synpath.trading import polymarket_signing as poly
from synpath.trading import predict_fun_signing as predict
from synpath.trading.types import Order, OrderType, OrderStatus, Side, TimeInForce

PREDICT_SLIPPAGE_BPS = 100


def cash_budget(value, venue):
    value = Decimal(str(value))
    if not value.is_finite() or value <= 0:
        raise ValueError("Invalid cash budget")
    step = Decimal("0.01") if venue == "polymarket" else Decimal("0.000000000000000001")
    return value.quantize(step, rounding=ROUND_DOWN)


def poly_payload(plan, token, credentials, signer, api_key, now_ms=None, salt=None):
    with localcontext() as context:
        context.prec = 100
        budget = cash_budget(plan["cash_budget"], "polymarket")
        boundary = Decimal(1) - token.tick
        # Matches the venue's market BUY amount rounding: maker is cash, not shares.
        places = poly.round_config(token.tick).amount
        minimum = (budget / boundary).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
        maker, taker = poly.to_raw(budget), poly.to_raw(minimum)
        if maker < 1_000_000 or taker <= 0:
            raise ValueError("Polymarket market buy needs at least $1")
        wallet = credentials.funder if credentials.signature_type else signer.address
        order = poly.build_order(maker=wallet, signer=wallet if credentials.signature_type == 3 else signer.address,
                                 token_id=token.token_id, maker_amount=maker, taker_amount=taker, side="BUY",
                                 signature_type=credentials.signature_type, timestamp_ms=now_ms or time.time_ns() // 1000000,
                                 salt=poly.new_salt() if salt is None else salt, expiration=0, builder=credentials.builder_code)
        order["signature"] = poly.sign_order(signer, order, neg_risk=token.neg_risk)
        return {"order": order, "owner": api_key, "orderType": "FAK", "deferExec": False}, poly.order_hash(order, neg_risk=token.neg_risk)


def predict_payload(plan, info, signer, maker, network, account, now_s=None, salt=None):
    with localcontext() as context:
        context.prec = 100
        budget = cash_budget(plan["cash_budget"], "predict_fun")
        quoted_budget = cash_budget(plan["quoted_cash_budget"], "predict_fun")
        maker_amount = int(budget * 10**18)
        target = Decimal(str(plan["target_shares"]))
        if not target.is_finite() or target <= 0:
            raise ValueError("Invalid Predict target shares")
        shares = predict.significant(int(target * 10**18), 5)
        if quoted_budget < 1 or shares < predict.MIN_SHARES_WEI:
            raise ValueError("Predict market buy needs at least 1 USDT")
        if maker_amount >= shares:
            raise ValueError("Predict cash budget per share must be below $1")
        order = predict.build_order(maker=maker, token_id=info.token(plan["outcome"]), maker_amount=maker_amount,
                                    taker_amount=shares, side="BUY", fee_rate_bps=info.fee_rate_bps,
                                    salt=predict.new_salt() if salt is None else salt, expiration=(now_s or int(time.time())) + 300)
        exchange = network.exchange(neg_risk=info.neg_risk, yield_bearing=info.yield_bearing)
        signature, order_hash = predict.sign_order(signer, order, exchange=exchange, network=network, account=account)
        wire = {key: str(value) for key, value in order.items()}
        wire.update(expiration=int(order["expiration"]), side=int(order["side"]), signatureType=int(order["signatureType"]),
                    hash=order_hash, signature=signature)
        # Use the standard quantity-based MARKET path: takerAmount is actual shares.
        # The signed cash/share ratio also caps spend, without deflating the target.
        expected_price = predict.to_wei(plan["expected_price"])
        return {"data": {"order": wire, "strategy": "MARKET", "pricePerShare": str(expected_price),
                          "slippageBps": str(PREDICT_SLIPPAGE_BPS), "isMinAmountOut": False}}, order_hash


async def submit_cash_buy(adapter, plan, before_submit):
    """Submit one already-budgeted market BUY. No retries creating a new signature."""
    if plan["venue"] == "polymarket":
        from synpath.trading.polymarket import error_of
        tokens = await adapter.market_tokens(plan["market_id"])
        token = await adapter.token_info(tokens.token(plan["outcome"]), refresh=True)
        api_key = await adapter.ensure_api_credentials()
        body, order_id = poly_payload(plan, token, adapter._order_credentials(), adapter.signer, api_key)
        minimum = Decimal(body["order"]["takerAmount"]) / 10**6
        before_submit(order_id, Decimal(body["order"]["makerAmount"]) / 10**6, minimum)
        raw = await adapter._private("POST", "/order", body=body, kind="write", cost=1)
        if not raw or not raw.get("success", True) or raw.get("errorMsg"):
            raise error_of(str((raw or {}).get("errorMsg") or "order refused"), body=raw)
        acknowledged = str(raw.get("orderID") or order_id)
        status = OrderStatus.PENDING
    else:
        from synpath.trading.predict_fun import error_of
        from synpath.errors import BadRequest
        info = await adapter.market_info(plan["market_id"])
        body, order_id = predict_payload(plan, info, adapter.signer, adapter.address, adapter.network, adapter.predict_account)
        minimum = Decimal(body["data"]["order"]["takerAmount"]) / 10**18
        before_submit(order_id, Decimal(body["data"]["order"]["makerAmount"]) / 10**18, minimum)
        try:
            raw = await adapter._call("POST", "/orders", body=body, kind="write")
        except BadRequest as error:
            mapped = error_of(error)
            mapped.status = error.status
            raise mapped from None
        if (raw or {}).get("success") is False:
            from synpath.trading.errors import OrderRejected
            raise OrderRejected("Predict.fun rejected the cash market order", body=raw)
        acknowledged = str((raw or {}).get("data", {}).get("orderHash") or order_id)
        status = OrderStatus.PENDING
    semantics = "SIGNED_MINIMUM_SHARES" if plan["venue"] == "polymarket" else "SIGNED_TARGET_SHARES"
    price = Decimal(1) - plan["tick"] if plan["venue"] == "polymarket" else Decimal(body["data"]["order"]["makerAmount"]) / Decimal(body["data"]["order"]["takerAmount"])
    return Order(id=acknowledged, market_id=plan["market_id"], venue=plan["venue"],
                 side=Side.BUY if plan["outcome"] == "yes" else Side.SELL, type=OrderType.MARKET,
                 time_in_force=TimeInForce.IOC, status=status, amount=minimum,
                 price=price if plan["outcome"] == "yes" else Decimal(1) - price,
                 info={"amount_semantics": semantics, "target_shares": str(plan["target_shares"]),
                       "cash_budget": str(plan["cash_budget"]), "computed_id": order_id})
