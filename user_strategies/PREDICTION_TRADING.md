# BTC prediction real-order runner

## Current status: explicit cash-budget market entry

Live entry calculates cash budgets by walking the current asks for about 10 shares.
Polymarket's signed maker amount is the actual dollars to spend; its broad legal
price boundary encodes a minimum output, so actual shares can vary. Predict.fun
uses quantity-based MARKET: signed `takerAmount` is the requested share quantity
(rounded down to five significant digits), `makerAmount` is the actual USDT
budget, and `isMinAmountOut=false`. It is never `10 * 0.99`. This fixes the incident
where a $3.20 budget signed only 3.232323 shares instead of the requested 10.
Predict's cash/share ratio bounds its execution price; a worse quote may cause
partial or no fill. Gross/net shares can differ due to partial fills and fees.
A net-share difference <=0.5 is accepted
when both legs filled; a larger gap triggers the existing excess-share exit and
stop. Historical incidents and reconciliation records are preserved. No new real
orders were sent during the cash-budget change; signed-payload tests and read-only
preflight do not prove acceptance or execution of a future real order.

Run from `D:/WorkSpace/prediction_market_arbitrage` in the uv-managed application environment.
The paper program has not been turned into an automatic live service. Only the
explicit `--live` flag enables real orders. No real orders were sent during
implementation; integration tests use fake adapters. Authentication, approvals,
chain confirmation and actual order acceptance still require funded accounts.

## Local configuration

Set these locally in the ignored `.env`; do not put signing keys in chat or Git:

```dotenv
POLYMARKET_PRIVATE_KEY=your-signing-wallet-private-key
POLYMARKET_SIGNATURE_TYPE=your-actual-type
POLYMARKET_FUNDER=your-funded-wallet-address
PREDICT_FUN_PRIVATE_KEY=your-signing-wallet-private-key
PREDICT_FUN_API_KEY=your-mainnet-api-key
PREDICT_FUN_ACCOUNT_ADDRESS=your-predict-smart-wallet-address
```

Polymarket wallet types: 0 EOA (allowlisted), 1 legacy proxy, 2 Safe, 3 Deposit
Wallet. Types 1–3 require the actual funder address; do not assume type 3 for an
existing wallet. Existing `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET` and
`POLYMARKET_API_PASSPHRASE` may be supplied together; otherwise the SDK derives
credentials from wallet authentication. Predict.fun's account address must be
its funded Predict account if using its web-created Smart Wallet, not the
owner's unrelated address. Leave it unset only for a funded EOA.

Enable the required collateral/token trading approvals through the venues'
wallet flow before use. This runner does not grant allowances, transfer assets,
withdraw, split/merge positions or redeem payouts. Predict.fun RPC may be set
with `PREDICT_FUN_RPC_URL`; it uses BNB Chain mainnet. Polymarket uses global
mainnet. Mainnet and testnet cannot be combined in this runner.

Check configuration presence without authentication, network or orders:

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --trade-credentials
```

This outputs booleans, never keys or wallet credential values. Presence is not
proof that the signing key controls the funded account or that approvals exist.

## Explicit live start

Check current funded-account readiness without creating any orders:

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --auto-btc --live --quantity 10 --max-trades 1 --preflight-only
```

Both balances, buy collateral allowances and share-token operator approvals
must pass on the current BTC markets. A missing Predict allowance stops before
either venue order rather than letting one leg execute alone.

Inspect Predict approvals without chain writes:

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.approve_predict --amount 15
```

The output shows the actual market exchange, finite target allowance, whether
the existing allowance covers it, share operator approval, owner matching and
the signing wallet's BNB gas balance. If already authorized, no new approval is
needed. Approval amount is permission, not a USDT transfer or deposit.

Only if missing and explicitly requested, `--execute` sends an on-chain allowance
of at most 15 USDT and the selected exchange's ERC1155 share-operator permission.
The latter is Boolean permission over that share-token contract, not a 15-USDT
limit on NFT transfers. This script does not grant every exchange unlimited access.
The official Predict SDK performs the account-owner checks and smart-wallet calls.
Any approval transaction requires BNB in the signing wallet; do not send BNB to
the Predict Account/global deposit address. A failed/unknown transaction should
be checked on-chain before rerunning. The implementation was only run in
read-only mode during this fix, and found current approvals already sufficient.

The following command **sends real orders** once conditions pass:

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --auto-btc --live --quantity 10 --max-trades 1
```

Risk rules:

- `--max-trades N` limits paired entry attempts in this process run. Default 1;
  N must be a positive integer. One attempt sends two cash-budget market orders concurrently. Count at
  INTENT before submission; rejection, no fill or unknown response counts too.
  Quote skips do not count. Complete confirmation and any required unwind before
  stopping; the limit never interrupts that handling. After any filled attempt,
  close the quote streams and wait for both official event resolutions. After
  the final attempt settles, log LIVE_RUN_COMPLETE and exit normally. No-fill
  attempts need no event-resolution wait. Redemption transactions are not sent.
  A restart with AWAIT_CONFIRMATION/AWAIT_SETTLEMENT resumes read-only tracking, without
  sending new entries; other unresolved/HALT records still require reconciliation.
- Compare both Up/Down combinations; verify exact native IDs, time window and
  outcome orientation before using the matching WS books.
- At most 10 TARGET shares for cash-budget calculation and one attempt per
  five-minute window. This is not a hard cap on actual market-filled shares.
- Require complete connected books, timestamps <=15 seconds old, skew <=5 seconds,
  known fees, and market minimums (Predict.fun order value >=1 USDT).
- Require snapshot after-fee/rounding-buffer total difference >$1.00, accounting
  conservatively for Predict's share-deducted buy fee. This uses projected
  minimum payout from the quoted cash budget, not a guarantee of future profit.
  Polymarket cash rounds DOWN to cents, Predict cash rounds DOWN to 18 decimals;
  either market-buy budget below $1 / 1 USDT is skipped, never increased to the
  minimum (which would buy substantially more than intended).
- Do not enter during the final 60 seconds, leaving time for confirmation/unwind.
- Both venues must have at least $15 available collateral. Exactly $15 passes;
  either venue below $15 stops the live loop. Check at startup, every 15 seconds
  while scanning, before each entry, and before resuming entries after settlement. There
  is no cumulative purchase or turnover cap; old purchases do not stop new ones.
- Check enough funds/allowance for the actual cash budgets plus conservative fee
  reserves, with both buy and sell approvals verified before either submission.
  Initial budgets come from the requested target quantity's full ask-depth cost.
  Preserve $15 stop threshold and per-run attempt count.

Both buys are **concurrent MARKET orders**, not GTC then cancel.
Polymarket uses cash-budget FAK; Predict.fun uses quantity-based MARKET without
fill-or-kill. Budgets bind principal spend, while fees may be extra cash or
deducted shares. Polymarket retains its broad legal price boundary. Predict signs
the target shares directly, with `isMinAmountOut=false` and `pricePerShare` equal
to the signed cash/share ratio. A $3.20 budget for 10 shares therefore bounds the
price at $0.32: a better price can fill 10 for less cash, a worse price may not fill.
On multiple price levels, the average-cost budget may not buy the full requested
size; the runner never increases it to the worst-tier cost. ORDER records label
Polymarket's signed minimum and Predict's signed target separately; actual counts
still come from confirmed fills. This correction is tested against the official
quantity-based SDK helper and mocked POST payloads; no real order was sent to
verify future venue acceptance or execution.
Zero-zero is NO_FILL. Both positive and <=0.5 NET-share gap is accepted as paired,
including fees/precision differences, without top-up orders. Both actual sizes
may be greater or less than 10. Otherwise try the excess-net-share exit, stop new
entries, and keep monitoring the remaining position until both events resolve.
Remaining gaps below venue minimum sale sizes can be untradeable and require
review; exit attempts never guarantee a completed hedge or risk-free outcome.
Synpath represents orders on
the YES leg: opening NO uses SELL and `1 - NO price`; closing an inventory uses
`reduce_only`. Orders are not reported successful merely because they match:
only CONFIRMED fill quantities count. FAILED/MATCHED fills are distinguished.
Receipts now distinguish gross traded shares from net shares after a
share-denominated buy fee. A Predict.fun fill of 10 with a 0.1030303-share fee
leaves about 9.8969697 shares. FILLS records include `confirmed_net_shares` and
`share_fees`; the gross amount must never be used blindly as sellable inventory.
The final entry check also converts the estimated Predict buy fee into shares
at each quoted ask price and uses the smaller possible winning payout, rather than
subtracting only its USDT-equivalent trading value. INTENT records the estimated
minimum payout and conservative entry net. This does not establish equivalent
event resolution rules.
There is no automatic retry of an order whose submission outcome is unknown.
Exit accounting retains `reduce_only` from the submitted request and recognizes
historical UNWIND_ORDER IDs. Polymarket's SDK response can omit that flag: using
the response alone would mistake a SELL closing YES for a BUY opening NO and
value a 3-cent sale at 97 cents. The local context restores the flag for logging
and price/fee accounting without changing the venue order's signed token or side.

If confirmed NET sizes differ by more than 0.5 (or only one leg filled), the runner tries one reduce-only IOC sell for
the excess NET shares against current connected/fresh bids, capped by the actual
on-chain token balance and rounded DOWN to the venue's quantity precision, then
stops new entries and waits for both event resolutions. Known rejected/partial
sells or unavailable depth keep the unsold net quantities in the resolution record.
A rejected/partial sell, unavailable depth or market
expiry can leave exposure. Unwind price movement and fee can incur loss. If a
submission or chain settlement cannot be determined, halt without resubmission
or a blind unwind (the uncertain leg may still confirm). An entry confirmation
timeout now closes quote subscriptions and writes AWAIT_CONFIRMATION instead of
exiting. Read-only queries continue in 30-second rounds, approximately every two
seconds, with on-chain token balances logged between rounds as diagnostics only.
Zero balances can reflect redemption and never substitute for missing fill records.
Polymarket orders with associated trade IDs are also queried by those IDs if the
market trade listing is incomplete; only this order's confirmed receipts count.
Unknown counts/costs remain null, not zero. After delayed confirmation the runner
records the real net-share gap, monitors both event resolutions and exits; it
does not use stale quotes to top up or unwind automatically. Restarting resumes
this tracking without new entries, even if the $15 entry balance gate no longer
passes. Earlier HALT records and an interrupted submission before a durable
tracking record still require explicit reconciliation. Unknown unwind receipts
also retain the existing HALT protection.

## Records and reconciliation

Both user WS streams now supply live order/trade updates alongside REST queries.
Polymarket uses the existing CLOB credentials; Predict.fun uses its API key and
the trading adapter's refreshable wallet JWT. No trade is submitted by an
observer. Before entry the exact Polymarket market/token mapping
is cached so Up/Down NO fills have the same orientation as REST. Events wake the
confirmation loop through a shared signal; REST polling remains available if
either WS disconnects. Predict's server follows the whole wallet, with local
market filtering. Its SDK refreshes the wallet JWT if the server rejects an
expired token. JWT topic strings and headers are never logged.

Trade IDs are deduplicated across WS/REST and MATCHED does not count as final.
A WS CONFIRMED receipt can bridge a delayed REST trade listing, but confirmation
still requires a successful REST terminal-order query with matching accounted
shares. Conflicting quantities/prices or terminal states remain unknown; an older
MATCHED event cannot overwrite CONFIRMED. Reconnects do not replay missed user
events, so the REST listing/associated-trade-ID fallback remains required.
Predict match receipts are deduplicated by settlement and order hash even when
REST adds a leg suffix. Its positive share/collateral fees must be known before
using a WS-confirmed taker fill; missing fees trigger further REST queries.
Conflicting known fees also keep the result unresolved. Gross shares are never
silently treated as net inventory because the WS omitted a fee field.

`REST_QUERY elapsed_ms` measures a whole SDK query (including parsing/metadata
work), not the server's data-publication delay. `ORDER_CONFIRMATION_TIMING` logs
local submission, acknowledgement, first REST receipt/confirmation and first WS
receipt/confirmation timestamps, plus elapsed times from submission when known.
It also reports whether reconciliation used WS and how many receipts each source
supplied. A restarted order with no stored submission time does not invent one.
WS frames, credentials and provider details are never printed. Existing running
processes must finish and restart to use the new observer.

`data/prediction/live/orders.jsonl` stores INTENT before network submission,
per-leg acknowledgement/IDs or sanitized error type, confirmed fill quantities,
unwind attempts, AWAIT_CONFIRMATION, AWAIT_SETTLEMENT, SETTLEMENT, DONE and HALT. SETTLEMENT calculates
payouts from the remaining net shares after confirmed exits, not the original
target quantity. It reports gross P&L when cash flows are known; missing actual
cash fees leave net_pnl unknown rather than being treated as zero. Official
resolution is not proof of automatic redemption or collateral arrival. A delayed
resolution/read error is retried every 30 seconds without reopening quote streams
or placing orders. There is no forced exit just because the five-minute deadline
passes. Logs and the journal exclude raw signed payloads,
credential objects and provider error bodies. The journal is ignored by Git.
Known order refusals now include `venue_reason`, a fixed `reason_message`, and
the HTTP status when preserved by the SDK, for both entry and unwind orders.
Examples include FAK_NO_MATCH, INVALID_SIGNATURE, ALLOWANCE and BALANCE.
Unknown provider errors remain UNCLASSIFIED; no raw text/body is logged and a
missing historical response cannot be reconstructed from a generic OrderRejected.
This diagnostic change does not retry or resubmit a failed order. Already-running
processes use the old code until their next start.
An exclusive OS file lock prevents two processes using the same live journal.
Run only one live process; do not delete or change the journal to bypass an
unresolved attempt. Explicit RECONCILED records retain the history while allowing
a verified terminal incident to stop blocking future runs. The SDK's adapters expose `fetch_order`,
`fetch_my_trades`, `fetch_positions` and `cancel_order` for reconciliation;
this runner does not cancel unrelated account orders or auto-recover incidents.

Paired orders are held to event settlement. The journal records confirmed
execution, not a promise of outcome equivalence, risk-free profit or automatic
cash redemption. Polymarket BTC/USD TWAP and Predict.fun BTC/USDT closing-price
conditions can differ; both selected legs can lose. Balance checks compare
collateral at 1:1 and exclude deposit/withdrawal costs and future unwind costs.
Pending wins are not available balance until credited/redeemed; redemption is
not automated by this runner.

Official references:

- [Polymarket wallets and authentication](https://docs.polymarket.com/trading/wallets-auth)
- [Polymarket order submission](https://docs.polymarket.com/trading/place-orders)
- [Predict.fun order creation and approvals](https://dev.predict.fun/doc-679306)
