"""Append-only paper entries and official settlement records; never submits orders."""

import json
import logging
import os
import time
from threading import Lock
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import httpx

LOG = logging.getLogger("prediction-check")
MIN_NET_PER_PAIR = Decimal("0.20")


def entry_reason(window, result, timestamps, now, min_net_per_pair=MIN_NET_PER_PAIR):
    if window["end"] - now < 10:
        return "TOO_CLOSE_TO_END"
    if not all(isinstance(value, (int, float)) and 0 <= now * 1000 - value <= 15000 for value in timestamps):
        return "QUOTE_TIME_UNKNOWN_OR_STALE"
    if max(timestamps) - min(timestamps) > 5000:
        return "QUOTE_TIME_SKEW"
    if not all(leg["complete"] for leg in result["legs"]):
        return "INSUFFICIENT_DEPTH"
    if any(leg["fee"] is None for leg in result["legs"]):
        return "FEES_UNKNOWN"
    if result["legs"][1]["cost"] < 1:
        return "PREDICT_MINIMUM_1_USDT"
    if result["net_per_pair"] <= min_net_per_pair:
        return "NET_PER_PAIR_NOT_ABOVE_0_20"
    return None


def settlement_factors(poly, predict, sides, clob=None):
    """Return selected outcome payouts only when both APIs explicitly resolve."""
    if predict.get("status") != "RESOLVED":
        return None
    labels = poly.get("outcomes")
    labels = json.loads(labels) if isinstance(labels, str) else labels
    if labels != ["Up", "Down"]:
        return None
    if poly.get("umaResolutionStatus") == "resolved":
        prices = poly.get("outcomePrices")
        prices = json.loads(prices) if isinstance(prices, str) else prices
        if not isinstance(prices, list) or len(prices) != 2:
            return None
        factors = [Decimal(str(value)) for value in prices]
        if any(not value.is_finite() or not 0 <= value <= 1 for value in factors) or sum(factors) != 1:
            return None
    elif clob and clob.get("closed") is True:
        tokens = clob.get("tokens") or []
        winners = {token.get("outcome"): token.get("winner") for token in tokens}
        if len(tokens) != 2 or set(winners) != {"Up", "Down"}:
            return None
        flags = [winners["Up"], winners["Down"]]
        if not all(isinstance(flag, bool) for flag in flags) or sum(flags) != 1:
            return None
        factors = [Decimal(flag) for flag in flags]
    else:
        return None
    outcomes = {row.get("indexSet"): row for row in predict.get("outcomes", [])}
    if {index: row.get("name") for index, row in outcomes.items()} != {1: "Up", 2: "Down"}:
        return None
    statuses = [outcomes[index].get("status") for index in (1, 2)]
    # Do not guess tie/void payouts from start/end prices or an unknown status.
    if statuses not in (["WON", "LOST"], ["LOST", "WON"]):
        return None
    predict_factors = [Decimal(status == "WON") for status in statuses]
    return [factors[0 if sides[0] == "yes" else 1], predict_factors[0 if sides[1] == "yes" else 1]]


def fetch_settlement(entry):
    with httpx.Client(timeout=10, trust_env=False, headers={"Cache-Control": "no-cache"}) as client:
        # Gamma's cached market detail can lag an already-published resolution.
        poly = client.get(f"https://gamma-api.polymarket.com/markets/{entry['polymarket']}",
                          params={"_": time.time_ns()})
        poly.raise_for_status()
        predict = client.get(f"https://api.predict.fun/v1/markets/{entry['predictfun']}",
                             headers={"x-api-key": os.environ["PREDICT_FUN_API_KEY"]})
        predict.raise_for_status()
        response = predict.json()
        if response.get("success") is not True:
            return None
        market = poly.json()
        clob = None
        if market.get("umaResolutionStatus") != "resolved" and response["data"].get("status") == "RESOLVED" and market.get("conditionId"):
            resolution = client.get(f"https://clob.polymarket.com/markets/{market['conditionId']}",
                                    params={"_": time.time_ns()})
            resolution.raise_for_status()
            clob = resolution.json()
        return settlement_factors(market, response["data"], entry["sides"], clob)


class PaperRecorder:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = Lock()
        self.entries, self.settled = {}, {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if row["type"] == "ENTRY":
                    self.entries[row["id"]] = row
                elif row["type"] == "SETTLEMENT":
                    self.settled[row["id"]] = row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.summary()

    def append(self, row):
        row = {"recorded_at": datetime.now(timezone.utc).isoformat(), **row}
        with self.lock, self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        return row

    def observe(self, window, sides, quantity, result, timestamps, asks, configs, now=None):
        now = time.time() if now is None else now
        identity = f"{window['start']}:{sides[0]}:{sides[1]}:{quantity}"
        if any(entry["window_start"] == window["start"] for entry in self.entries.values()):
            return "ALREADY_RECORDED"
        reason = entry_reason(window, result, timestamps, now)
        row = {"type": "SKIP" if reason else "ENTRY", "id": identity,
               "reason": reason, "polymarket": window["polymarket"], "predictfun": window["predictfun"],
               "window_start": window["start"], "window_end": window["end"], "sides": sides,
               "quantity": quantity, "legs": result["legs"], "source_timestamps_ms": timestamps,
               "quoted_net_difference": result["net"], "quoted_net_per_pair": result.get("net_per_pair"),
               "entry_min_net_per_pair": MIN_NET_PER_PAIR, "entry_comparison": "STRICTLY_GREATER",
               "fee_configs": configs, "rules": [window["polymarket_rule"], window["predictfun_rule"]],
               "asks": [[{"price": level.price, "size": level.size} for level in levels] for levels in asks],
               "execution": "SIMULTANEOUS_BOOK_SNAPSHOT_ASSUMPTION", "collateral_conversion": "ASSUMED_1_TO_1"}
        record = self.append(row)
        if reason:
            return reason
        self.entries[identity] = record
        LOG.info("PAPER_ENTRY id=%s sides=%s quantity=%s costs=%s fees=%s quoted_net_per_pair=%s", identity, sides, quantity,
                 [leg["cost"] for leg in result["legs"]], [leg["fee"] for leg in result["legs"]], result.get("net_per_pair"))
        return "RECORDED"

    def observe_best(self, window, quantity, candidates, configs, now=None):
        now = time.time() if now is None else now
        if any(entry["window_start"] == window["start"] for entry in self.entries.values()):
            return "ALREADY_RECORDED"
        eligible = [candidate for candidate in candidates
                    if entry_reason(window, candidate["result"], candidate["timestamps"], now) is None]
        if eligible:
            best = max(eligible, key=lambda candidate: candidate["result"]["net_per_pair"])
            return self.observe(window, best["sides"], quantity, best["result"], best["timestamps"], best["asks"], configs, now)
        for candidate in candidates:
            self.observe(window, candidate["sides"], quantity, candidate["result"],
                         candidate["timestamps"], candidate["asks"], configs, now)
        return "NO_ELIGIBLE_DIRECTION"

    def settle_pending(self, fetch=fetch_settlement, now=None):
        now = time.time() if now is None else now
        for identity, entry in list(self.entries.items()):
            if identity in self.settled or now < entry["window_end"]:
                continue
            try:
                factors = fetch(entry)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                LOG.warning("PAPER_SETTLEMENT_WAIT id=%s reason=%s", identity, type(error).__name__)
                continue
            if factors is None:
                LOG.info("PAPER_SETTLEMENT_WAIT id=%s official payouts not confirmed", identity)
                continue
            with localcontext() as context:
                context.prec = 64
                quantity = Decimal(str(entry["quantity"]))
                costs = sum(Decimal(str(leg["cost"])) for leg in entry["legs"])
                fees = sum(Decimal(str(leg["fee"])) for leg in entry["legs"])
                payouts = [quantity * factor for factor in factors]
                total_payout = sum(payouts)
                net = total_payout - costs - fees
            classification = ("BOTH_WIN" if factors == [Decimal(1), Decimal(1)] else
                              "BOTH_LOSE" if factors == [Decimal(0), Decimal(0)] else
                              "ONE_WIN" if sorted(factors) == [Decimal(0), Decimal(1)] else "FRACTIONAL_PAYOUT")
            row = self.append({"type": "SETTLEMENT", "id": identity, "classification": classification,
                               "payout_factors": factors, "payouts": payouts, "total_payout": total_payout,
                               "total_cost": costs, "estimated_fees": fees,
                               "simulated_net": net})
            self.settled[identity] = row
            LOG.info("PAPER_SETTLEMENT %s", json.dumps(row, default=str, ensure_ascii=False))
        self.summary()

    def summary(self):
        with localcontext() as context:
            context.prec = 64
            net = sum((Decimal(str(row["simulated_net"])) for row in self.settled.values()), Decimal(0))
        counts = {state: sum(row["classification"] == state for row in self.settled.values())
                  for state in ("ONE_WIN", "BOTH_WIN", "BOTH_LOSE", "FRACTIONAL_PAYOUT")}
        LOG.info("PAPER_SUMMARY entries=%s settled=%s pending=%s simulated_net=%s results=%s",
                 len(self.entries), len(self.settled), len(self.entries.keys() - self.settled.keys()), net, counts)
