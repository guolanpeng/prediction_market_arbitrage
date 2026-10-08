"""Offline parity against the locked Python implementation and official SDK."""

import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_account import Account
from predict_sdk import OrderBuilder, ChainId
from predict_sdk.constants import ADDRESSES_BY_CHAIN_ID, Side, SignatureType
from predict_sdk.logger import Logger
from predict_sdk.types import Order, Book, MarketHelperInput

from user_strategies.check_prediction_pair import calculate

ROOT = Path(__file__).resolve().parents[1]
NAME = "prediction-market.exe" if os.name == "nt" else "prediction-market"
BINARY = Path(os.environ.get("PREDICTION_RUST_TEST_BINARY", ROOT / "target" / "debug" / NAME))
native = pytest.mark.skipif(not BINARY.is_file(), reason="Build the Rust binary or set PREDICTION_RUST_TEST_BINARY")
TEST_KEY = "0x" + "1" * 64


def call(operation, data):
    result = subprocess.run([str(BINARY), "offline", "--operation", operation],
                            input=json.dumps(data, default=str), capture_output=True,
                            text=True, check=True, cwd=ROOT, timeout=15)
    return json.loads(result.stdout)


def financial(value):
    if isinstance(value, dict):
        return {key: financial(item) for key, item in value.items()}
    if isinstance(value, list):
        return [financial(item) for item in value]
    if isinstance(value, (str, Decimal)):
        try:
            return Decimal(value)
        except ArithmeticError:
            pass
    return value


@native
@pytest.mark.parametrize("configs", [
    [None, None],
    [{"model": "quadratic_theta", "rate": Decimal("0.07"), "places": "0.00001"},
     {"model": "min_price", "rate": Decimal("0.02"), "places": "0.000001"}],
])
@pytest.mark.parametrize("asks_a,asks_b,quantity", [
    ([["0.4", "10"]], [["0.45", "10"]], "10"),
    ([["0.49", "10"]], [["0.49", "10"]], "10"),
    ([["0.1", "4"], ["0.5", "6"]], [["0.32", "100"]], "10"),
    ([["0.4", "0.61"]], [["0.45", "10"]], "10"),
    ([["0.12345678901234567890123456789", "3"]], [["0.2", "10"]], "3"),
    ([["0", "100"], ["0.2", "3"]], [["0.3", "3"]], "3"),
])
def test_native_calculation_matches_python(asks_a, asks_b, quantity, configs):
    levels = lambda rows: [SimpleNamespace(price=Decimal(p), size=Decimal(s)) for p, s in rows]
    expected = calculate(levels(asks_a), levels(asks_b), Decimal(quantity), configs)
    actual = call("calculate", {"asks_a": asks_a, "asks_b": asks_b, "quantity": quantity, "configs": configs})
    assert financial(actual) == financial(expected)


@native
@pytest.mark.parametrize("account", [None, "0x" + "2" * 40])
@pytest.mark.parametrize("negative,yield_bearing", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("outcome", ["yes", "no"])
def test_predict_hash_and_every_signature_byte_match_official_sdk(account, negative, yield_bearing, outcome):
    plan = {"outcome": outcome, "target_shares": "10", "quoted_cash_budget": "3.20",
            "cash_budget": "3.232", "expected_price": "0.32"}
    info = {"yes_token": "1", "no_token": "2", "neg_risk": negative,
            "yield_bearing": yield_bearing, "fee_rate_bps": 200}
    result = call("predict-sign", {"plan": plan, "info": info, "private_key": TEST_KEY,
                                  "account": account, "now_s": 1, "salt": 1})
    owner = Account.from_key(TEST_KEY)
    # Use the official constructor for a pure cryptographic test. Factory smart
    # wallet ownership validation is an on-chain read and isn't part of signing.
    sdk = OrderBuilder(chain_id=ChainId.BNB_MAINNET, precision=18,
                       addresses=ADDRESSES_BY_CHAIN_ID[ChainId.BNB_MAINNET],
                       generate_salt_fn=lambda: 1, logger=Logger(), signer=owner,
                       predict_account=account, contracts=None, web3=None)
    maker = account or owner.address
    order = Order(salt="1", maker=maker, signer=maker, taker="0x" + "0" * 40,
                  token_id="1" if outcome == "yes" else "2", maker_amount="3232000000000000000",
                  taker_amount="10000000000000000000", expiration="301", nonce="0",
                  fee_rate_bps="200", side=Side.BUY, signature_type=SignatureType.EOA)
    typed = sdk.build_typed_data(order, is_neg_risk=negative, is_yield_bearing=yield_bearing)
    digest = sdk.build_typed_data_hash(typed)
    signed = sdk.sign_typed_data_order(typed)
    assert result["hash"].lower() == digest.lower()
    signature = signed.signature if signed.signature.startswith("0x") else "0x" + signed.signature
    wire = result["payload"]["data"]["order"]
    assert wire["signature"].lower() == signature.lower()
    assert wire["hash"].lower() == digest.lower()
    for field, expected in {"salt": "1", "tokenId": order.token_id,
                            "makerAmount": order.maker_amount, "takerAmount": order.taker_amount,
                            "expiration": 301, "nonce": "0", "feeRateBps": "200",
                            "side": 0, "signatureType": 0}.items():
        assert wire[field] == expected
    assert wire["maker"].lower() == maker.lower() == wire["signer"].lower()
    data = result["payload"]["data"]
    assert data["pricePerShare"] == "320000000000000000"
    assert data["slippageBps"] == "100" and data["isMinAmountOut"] is False


@native
@pytest.mark.parametrize("depth", [
    [["0.32", "100"]], [["0.30", "4"], ["0.40", "6"]],
    [["0.123456789012345678", "4"], ["0.333333333333333333", "10"]],
])
def test_native_slippage_sizing_matches_official_sdk(depth):
    quoted = sum(Decimal(price) * min(Decimal(size), Decimal(10) - sum(Decimal(s) for _, s in depth[:index]))
                 for index, (price, size) in enumerate(depth))
    candidate = {"sides": ["yes", "no"], "result": {"legs": [{"cost": "3.20"}, {"cost": quoted}]},
                 "asks": [[], depth], "ticks": ["0.01", "0.001"]}
    plans = call("plans", {"candidate": candidate, "quantity": "10", "token": {"tick": "0.01"},
                           "window": {"polymarket": "1", "predictfun": "2"}})
    expected = OrderBuilder.make(ChainId.BNB_MAINNET).get_market_order_amounts(
        MarketHelperInput(side=Side.BUY, quantity_wei=10 * 10**18, slippage_bps=100, is_min_amount_out=False),
        Book(market_id=2, update_timestamp_ms=1, asks=[(Decimal(p), Decimal(s)) for p, s in depth], bids=[]))
    assert Decimal(plans[1]["cash_budget"]) * 10**18 == expected.maker_amount
    assert Decimal(plans[1]["expected_price"]) * 10**18 == expected.price_per_share
    assert Decimal(plans[1]["worst_price"]) * 10**18 == expected.last_price


def test_compatibility_launcher_does_not_enable_live_by_default(monkeypatch):
    from user_strategies import prediction_rust
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    calls = []
    monkeypatch.setattr(prediction_rust.subprocess, "call", lambda command, **kwargs: calls.append(command) or 0)
    args = SimpleNamespace(auto_btc=True, list=False, paper=False, polymarket=None, predictfun=None,
                           quantity=Decimal(10), max_trades=1, interval=2.0, seconds=0.0,
                           live=False, preflight_only=False, trade_credentials=False)
    assert prediction_rust.btc(args) == 0
    assert "--live" not in calls[0]
    args.live = True
    prediction_rust.btc(args)
    assert "--live" in calls[1]
    for quantity in (Decimal(0), Decimal(-1), Decimal("NaN"), Decimal(11)):
        args.quantity = quantity
        with pytest.raises(ValueError):
            prediction_rust.btc(args)
    assert len(calls) == 2
