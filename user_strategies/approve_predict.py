"""Check or explicitly grant scoped Predict.fun approvals; never places orders."""

import argparse
import json
import logging
import os
import time
from decimal import Decimal

import httpx
from eth_account import Account
from eth_abi import encode, decode
from eth_utils import keccak
from predict_sdk import OrderBuilder, ChainId, OrderBuilderOptions
from predict_sdk.constants import ADDRESSES_BY_CHAIN_ID, RPC_URLS_BY_CHAIN_ID
from synpath.trading.predict_fun_signing import MAINNET


def allowance_amount(value):
    amount = Decimal(value)
    if not amount.is_finite() or not 0 < amount <= 15 or amount * 10**18 != (amount * 10**18).to_integral_value():
        raise ValueError("Allowance must be >0 and <=15 USDT, with at most 18 decimals")
    return int(amount * 10**18)


def collateral_token(neg_risk, yield_bearing):
    suffix = "NEG_RISK_CONDITIONAL_TOKENS" if neg_risk else "CONDITIONAL_TOKENS"
    return getattr(ADDRESSES_BY_CHAIN_ID[ChainId.BNB_MAINNET], "YIELD_BEARING_" + suffix if yield_bearing else suffix)


def rpc_call(client, url, method, params):
    response = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    response.raise_for_status()
    result = response.json()
    if result.get("error") or "result" not in result:
        raise ValueError("RPC read failed")
    return result["result"]


def read_plan(amount, market_id=None):
    signer = Account.from_key(os.environ["PREDICT_FUN_PRIVATE_KEY"])
    account = os.environ["PREDICT_FUN_ACCOUNT_ADDRESS"]
    url = os.getenv("PREDICT_FUN_RPC_URL") or "https://bsc-dataseed.bnbchain.org"
    with httpx.Client(timeout=15, trust_env=False) as client:
        headers = {"x-api-key": os.environ["PREDICT_FUN_API_KEY"]}
        if market_id:
            response = client.get(f"https://api.predict.fun/v1/markets/{market_id}", headers=headers)
            response.raise_for_status()
            market = response.json()["data"]
        else:
            slot = int(time.time()) // 300 * 300
            response = client.get(f"https://api.predict.fun/v1/categories/btc-updown-5m-{slot}", headers=headers)
            response.raise_for_status()
            markets = response.json()["data"]["markets"]
            if len(markets) != 1:
                raise ValueError("BTC market missing or ambiguous")
            market = markets[0]
        neg_risk, yield_bearing = bool(market["isNegRisk"]), bool(market["isYieldBearing"])
        spender = MAINNET.exchange(neg_risk=neg_risk, yield_bearing=yield_bearing)
        token = collateral_token(neg_risk, yield_bearing)
        data = "0x" + (keccak(text="allowance(address,address)")[:4] + encode(["address", "address"], [account, spender])).hex()
        current = int(rpc_call(client, url, "eth_call", [{"to": MAINNET.usdt, "data": data}, "latest"]), 16)
        data = "0x" + (keccak(text="isApprovedForAll(address,address)")[:4] + encode(["address", "address"], [account, spender])).hex()
        operator = decode(["bool"], bytes.fromhex(rpc_call(client, url, "eth_call", [{"to": token, "data": data}, "latest"])[2:]))[0]
        data = "0x" + (keccak(text="ecdsaValidatorStorage(address)")[:4] + encode(["address"], [account])).hex()
        owner = decode(["address"], bytes.fromhex(rpc_call(client, url, "eth_call", [{"to": MAINNET.ecdsa_validator, "data": data}, "latest"])[2:]))[0]
        gas = int(rpc_call(client, url, "eth_getBalance", [signer.address, "latest"]), 16)
    return {"market_id": str(market["id"]), "spender": spender, "usdt": MAINNET.usdt,
            "share_token": token, "target_allowance_usdt": str(Decimal(amount) / 10**18),
            "current_allowance_covers_target": current >= amount, "current_allowance_is_zero": current == 0,
            "share_operator_approved": operator, "signer_matches_owner": owner.lower() == signer.address.lower(),
            "gas_signer_address": signer.address, "gas_signer_bnb": str(Decimal(gas) / 10**18),
            "is_neg_risk": neg_risk, "is_yield_bearing": yield_bearing,
            "execute": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--amount", default="15", help="Finite USDT allowance, maximum 15")
    parser.add_argument("--market", help="Native numeric market ID; default current BTC five-minute market")
    parser.add_argument("--execute", action="store_true", help="SEND ON-CHAIN approvals: limited USDT allowance and share-token exchange operator permission")
    args = parser.parse_args()
    if args.market and not args.market.isdigit():
        parser.error("Market ID must be numeric")
    logging.disable(logging.CRITICAL)
    try:
        amount = allowance_amount(args.amount)
        plan = read_plan(amount, args.market)
        print(json.dumps(plan))
        if not args.execute:
            return
        if not plan["signer_matches_owner"]:
            raise ValueError("Signing wallet does not own the Predict account")
        if plan["current_allowance_covers_target"] and plan["share_operator_approved"]:
            print(json.dumps({"state": "ALREADY_AUTHORIZED"}))
            return
        if Decimal(plan["gas_signer_bnb"]) == 0:
            raise ValueError("Signing wallet has no BNB for approval gas")
        if os.getenv("PREDICT_FUN_RPC_URL"):
            RPC_URLS_BY_CHAIN_ID[ChainId.BNB_MAINNET] = os.environ["PREDICT_FUN_RPC_URL"]
        builder = OrderBuilder.make(ChainId.BNB_MAINNET, os.environ["PREDICT_FUN_PRIVATE_KEY"],
                                    OrderBuilderOptions(predict_account=os.environ["PREDICT_FUN_ACCOUNT_ADDRESS"]))
        flags = {"is_neg_risk": plan["is_neg_risk"], "is_yield_bearing": plan["is_yield_bearing"]}
        if not plan["current_allowance_covers_target"]:
            result = builder.set_ctf_exchange_allowance(**flags, amount=amount)
            if not result.success:
                raise ValueError("USDT allowance transaction failed")
        if not plan["share_operator_approved"]:
            result = builder.set_ctf_exchange_approval(**flags, approved=True)
            if not result.success:
                raise ValueError("Share operator transaction failed")
        verified = read_plan(amount, plan["market_id"])
        print(json.dumps({"state": "VERIFIED" if verified["current_allowance_covers_target"] and verified["share_operator_approved"] else "NOT_YET_VERIFIED"}))
    except Exception as error:
        print(json.dumps({"state": "FAILED", "error_type": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
