# Standalone WS pair checker

Run from `D:/WorkSpace/prediction_market_arbitrage`, using the uv-managed application environment.
This program prints logs and optionally saves paper records; it does not write
Redis, modify the dashboard, sign orders or trade. `PREDICT_FUN_API_KEY` must be
present in `.env`.

Explicit real orders are a separate opt-in `--live` mode; see
[PREDICTION_TRADING.md](PREDICTION_TRADING.md) for wallet setup, limits and incidents.
Normal checker and `--paper` never place orders.

## BTC simulation records

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --auto-btc --paper --quantity 10
```

This is a data-collection experiment, not an established profitable strategy.
Each window compares both Polymarket Up + Predict.fun Down and Polymarket Down
+ Predict.fun Up, using connected, complete WS books. It simulates equal shares
in the eligible direction with the highest difference after both fees. If one
direction fails freshness, depth or minimum value checks, it cannot hide an
eligible other direction. Paper mode evaluates both regardless of side flags.
Entry now requires the quote difference
after both estimated taker fees to be **strictly greater than $0.20 per paired
share** (two venues each buying one share). For 10 shares on each venue, the
total quoted net difference must exceed $2.00; equality is rejected. This is
the payout-minus-cost calculation assuming ONE_WIN, not an assertion of matching
settlement rules. All ask levels are walked. It records at most one entry per
window (across both directions) in `data/prediction/paper/btc.jsonl`; restarting with
the same file avoids repeating that entry and resumes pending settlements.
Use only one process per record file. `--records` chooses another file.

An entry requires known fees, enough depth, the market share minimum,
Predict.fun order value >= 1 USDT, >= 10 seconds until window end,
SDK book timestamps no older than 15 seconds and no more than 5 seconds apart.
Rejected snapshots are recorded as SKIP with a reason. Book timestamps are
provider/SDK timestamps, not a guarantee of simultaneous executable quotes.
If both books are disconnected/incomplete no entry is created.

Every 30 seconds it checks expired entries against each venue's official market
API. Only explicit resolved outcomes produce SETTLEMENT records; pending,
unknown, tie/void statuses with unconfirmed payouts remain pending, never inferred
from the price or market closure. Polymarket payout factors are read only after
`umaResolutionStatus=resolved`, or (for Chainlink markets without that field)
the official CLOB reports closed with exactly one explicit Up/Down token winner;
near-0/1 quotes alone never establish a payout. Predict.fun requires `status=RESOLVED` and
explicit WON/LOST outcomes. Logs summarize ONE_WIN, BOTH_WIN, BOTH_LOSE,
fractional payouts, pending entries, and simulated net after estimated fees.
Keep it running past expiry to collect settlements; a later restart also checks
unsettled historical entries.

Entries preserve native IDs, UTC window, directions, exact quantities, ask depth,
timestamps, costs, fee configuration and both rule descriptions. Decimal values
are serialized as strings. Collateral conversion is assumed 1:1. The simulation
assumes both displayed books could fill simultaneously; it does not model queue
position, latency, partial execution, transfer costs or actual order acceptance.
No orders, signatures, Redis writes or dashboard changes are made. Simulation
records are excluded from Git. Account balances are not simulated.

## Automatic BTC five-minute rollover

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --auto-btc --quantity 10
```

This discovers the exact current UTC five-minute slug on both official venue
APIs, validates matching start/end times, BTC asset and Up/Down orientation,
then subscribes through Synpath WS. At the boundary it cancels and closes old
subscriptions before selecting the new window. Metadata/fee schedules are read
again for every window. Missing/closed/mismatched markets wait and retry every
10 seconds; old-window prices are not reused. Directory requests only discover
the market; executable prices/sizes still come from WS.

Default directions are Polymarket Up (yes) and Predict.fun Down (no). Reverse
with `--polymarket-side no --predictfun-side yes`. Equal directions are rejected.
In auto mode, `--seconds 30` limits the whole run, including discovery and rolls.
The application logs `AUTO_SWITCH`, `AUTO_WAIT`, and boundary unsubscribe events.

**Same window does not mean equivalent settlement.** The live markets inspected
on 2026-10-06 used BTC/USD TWAP with Up on equality on Polymarket, but BTC/USDT
closing prices with a 50/50 tie on Predict.fun. Each window's rules are logged
and the output includes `settlement_equivalence=NOT_VERIFIED`; positive quote
edges cannot be treated as locked-in arbitrage on these differing conditions.
Auto-mode results use `state=SETTLEMENT_NOT_VERIFIED`, show arithmetic under
`quoted_net_difference`/`quote_state`, and suppress `net_estimate` and
`net_per_pair`. They are quote observations, not validated profit estimates.

List current BTC source candidates to obtain native market IDs and directions:

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --list --query BTC
```

Copy the four ID/side arguments from the selected line, and set your share quantity:

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --polymarket <ID> --polymarket-side yes --predictfun <ID> --predictfun-side no --quantity 10
```

Replace `<ID>` placeholders; do not paste the literal angle brackets into PowerShell.
Use `--seconds 30` for a bounded check; Ctrl+C closes a continuous subscription.
`--interval 2` controls log frequency, not WS data frequency.

The checker fetches market metadata and fee schedules through Synpath once at
startup, then consumes complete local books maintained by Synpath WS. It walks
all available ask levels for the requested equal quantity, using Decimal for
prices, sizes, costs and fee estimates. Supported fees: Polymarket's published
rate with exponent 1, rounded to 5 decimals; Predict.fun's published basis-point
rate times `min(price, 1-price)`, rounded to 6 decimals. Unsupported/missing fees
remain unknown. Order-level rounding is an estimate; fills may round separately.

Logs include filled size, leg costs/fees, gross, net estimate and net per pair.
`INSUFFICIENT_DEPTH` and `FEES_UNKNOWN` have no net estimate.
`MARKET_OR_SIZE_NOT_ELIGIBLE` suppresses the net estimate for inactive/closed
markets or quantities below a known minimum. Disconnected or incomplete books
produce `WAITING`. Unsupported order constraints remain to be checked on venue.

`POSITIVE_QUOTE_EDGE` means positive quoted cost difference after estimated fees,
assuming the two conditions are complementary, payout is one unit and collateral
currencies are worth 1:1. It is not proof of profit: rules, cutoff/oracle equality,
FX, actual execution, fees after fill, funding and transfer costs are not verified.
Fee schedules and market metadata are fetched at startup; restart to refresh them.
