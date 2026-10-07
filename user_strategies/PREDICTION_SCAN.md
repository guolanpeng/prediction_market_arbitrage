# Prediction-market scanning into Redis

The default command now imports the public Synpath scanner snapshot from
`https://api.synpath.dev/arbitrage` every 15 seconds into `prediction:v1:scan`.
It does not discover or poll the five venues. The original source response is
stored at `prediction:v1:synpath:raw`; both keys expire after 120 seconds.
Prices and fees are parsed with Decimal and preserved as decimal strings.
The dashboard shows Synpath's matching-pair count, excluded-pair count, outcome
labels, two quotes and per-share fees/net spread. These are provider estimates,
not locally verified opportunities. Actual leg quote timestamps are retained;
old quotes and default fee rates are labelled. Sizes are absent from this
endpoint, so no executable size or 100-share estimate is invented. Raw-market
and locally verified tabs are hidden in this mode.

This is the website's public endpoint, not a documented stable SDK API. It
currently needs no API key. An HTTP/schema error clears the displayed results;
a snapshot older than 120 seconds is rejected. Market data is fetched directly
without the local proxy, which failed TLS for this host during verification.

To explicitly use the original direct-venue scanner, add `--source venues`.
The rest of this document describes that mode.

This independent read-only worker collects production market metadata and YES/NO
best bids/asks and their quantities from Kalshi, Polymarket, Opinion, Hyperliquid
and Predict.fun. It never
signs transactions, places orders or sends notifications. Existing `market:v1:*`
and strategy Redis contracts are unchanged. No engine compilation or extra Python
dependencies are required.

Run from the application root using the existing uv environment:

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.prediction_scan
```

In direct-venue mode, collection uses full pagination of active binary market metadata, followed by
repeated book polling **only for cross-platform matched outcomes**. Catalog size
is not the book-request count. Exact-title/outcome matches, platform references and
reviewed mappings select the target markets before any order-book request.
Unmatched and already closed markets make no book requests. Matching itself does
not establish settlement equivalence. Liquidity is checked using fetched depth.
Existing complete catalogs can be reused at startup; raw unmatched records may
have no book or an old book, which consumers must mark unavailable/stale.
Polymarket uses `/markets/keyset` and opaque `after_cursor` tokens rather than
offset pagination, which has a deep-page limit. Its books use the read-only batch
`/books` endpoint; token IDs, not response ordering, associate YES and NO books.
Kalshi multivariate combo markets and non-unit payout contracts are excluded because
the YES+NO cross-platform calculation assumes the same unit-payout proposition.
Categorical markets are expanded into their individual binary outcomes. Polymarket
currently collects CTF binary markets with explicit YES/NO token IDs; other position
systems are not silently interpreted as CTF markets.

Set `MARKET_REDIS_URL` locally in `.env`. Predict.fun requires
`PREDICT_FUN_API_KEY` for mainnet. Opinion supports `OPINION_API_KEY`; public reads
worked without it during verification, though the documentation requires a key.
Keys are sent only to their own platform in HTTP headers. No wallet private keys
are needed. Missing keys and authentication errors are published as unavailable/error
states, not successful empty market lists.

Hyperliquid is queried at `https://api.hyperliquid.xyz/info`, never its testnet URL.
The production endpoint returned outcome metadata and both-side order books during
verification on 2026-10-06. Its official documentation still labels `outcomeMeta`
testnet-only. Therefore the worker reports `environment=production` and
`trading_availability=unverified`; availability of live order entry is not inferred.

## Redis contract

| Key | Type | Contents |
| --- | --- | --- |
| `prediction:v1:{venue}:markets` | Hash | Native market ID → JSON metadata and books |
| `prediction:v1:{venue}:status` | String/JSON | Coverage, progress, errors, heartbeat |
| `prediction:v1:scan` | String/JSON | Fresh-market counts, matched pairs, candidates, opportunities, venue status |

Venue identifiers are `kalshi`, `polymarket`, `opinion`, `hyperliquid`, `predictfun`.
Polymarket native market IDs are condition IDs; other venues use tickers or numeric
market/outcome IDs represented as strings. Prices, sizes, volume, costs, fees and
profits are decimal strings. Each record retains title, outcome labels, settlement
rules, cutoff, quote currency, references, source timestamps and a review fingerprint.

Books contain `yes` and `no`, each with `asks` ascending and `bids` descending;
levels are `[price, quantity]` strings. Asks represent executable buys, never last
prices, midpoints or indicative outcome probabilities. The default `--quote-mode top`
retains only the best bid and best ask and their real sizes. Kalshi uses batched
`/markets?tickers=...` quotes; NO ask size is the corresponding YES bid size, and
NO bid size is the corresponding YES ask size. Other platforms use their available
book APIs (Polymarket batches tokens) and retain only the best level, because price-only
APIs do not supply quantities. No quantity is guessed when the source omits it.
Explicit `--quote-mode depth` retains multi-level depth. In that mode Kalshi exposes bids only;
YES asks are `1 - NO bid` and NO asks are `1 - YES bid`. Predict.fun's single YES
book is complemented the same way. Polymarket, Opinion and Hyperliquid books are
read for both outcome tokens.

Per-venue workers run independently. Catalog metadata is published page by page
with a progress heartbeat, so large catalogs are visible before discovery finishes.
In continuous matched mode, matched quotes are refreshed concurrently with catalog
pagination: discovering a market does not require waiting for the catalog's final page.
Books are published every 20 markets and
delisted entries are removed after successful discovery. A failed book is cleared
and its error code retained. Failed market discovery leaves partially updated
metadata and retained old entries for inspection, but its venue is excluded from
scanning. Delisting is reconciled only after successful discovery. Hashes and status expire after
`--ttl` seconds (default 600). Scan results expire after `--max-age` seconds (60).
Consumers must reject expired status and old individual quotes, not just check the
hash TTL. Source timestamps may describe the last book change; freshness uses each
successful local book read's `received_at_ms` and `quote_at_ms`. Closed markets are
excluded when their cutoff is available.

Markets with a missing title (observed on Kalshi) retain their subtitle or native
ID as a display fallback and set `title_missing=true`; missing titles do not abort
the whole catalog.

Default catalog refresh is 900 seconds, scan/poll interval 15 seconds. REST quotas
mean even a large matched-book sweep can take longer than 15 seconds. The scanner does not
claim every market was refreshed within that interval; `target_count`, `cycle_seconds`, `processed`,
`quote_failures`, and `fresh_market_count` expose actual coverage. Every matched
leg must be fresh. Multiple instances should not share this namespace concurrently.

## Price-gap candidates and net profit

By default, best asks determine the per-share gap. `available_quantity` is the
smaller size on the two best asks. Fee calculations use up to `--quantity 100`
shares, reduced to this available quantity when the best level has fewer shares.
The scanner never assumes the rest can fill at the same best price. Published
minimum-order constraints still apply. Explicit `--quote-mode depth` estimates
the requested quantity by consuming successive ask levels; insufficient depth
excludes the pair in that mode.
Both directions (YES at A / NO at B and the reverse) are considered.

Exact full-title plus affirmative-outcome matches and Predict.fun's explicit cross-platform
references create candidates. Numbers, punctuation and dates are preserved during
matching. Generic YES labels and labels repeated in the question are treated alike;
distinct outcome labels on a shared event title remain separate. These links are not
proof that cancellation, deadline, oracle or settlement
rules agree. The candidate contains `review_reasons` and `match_verified=false` until
reviewed. This deliberately avoids declaring title similarity a guaranteed arbitrage.

Kalshi's general taker schedule is estimated as `0.07 * C * p * (1-p)`, rounding
up to cents per consumed level. Series/event overrides are not verified, so it stays
`fees_estimated`. Polymarket uses the market's returned fee schedule when exponent
is 1, rounding to five decimals; explicit fee-free markets use zero. Predict.fun
uses zero only when `feeRateBps=0`; other rates remain unknown. Opinion and
Hyperliquid fee parameters remain unknown rather than being assumed zero. Unknown
fees produce `net_profit=null`. The worker does not account for withdrawal,
conversion, borrowing or settlement transaction costs.

USD, USDC and USDT are retained separately. Cross-currency candidates show a nominal
gross dollar spread, not a confirmed cash profit. Net estimates require identical
supported quote currencies or explicitly reviewed dollar parity. No automatic FX
conversion or stablecoin parity assumption is made.

`opportunities` includes only positive net results with reviewed equivalent outcomes,
known non-estimated trading fees, sufficient depth and comparable collateral. It can
be empty while `candidates` contains price gaps. This is a scanner, not an execution
guarantee; prices can change before orders could be filled.

## Reviewed matches (optional)

Pass `--matches path/to/matches.json`. After comparing settlement rules, copy each
record's `fingerprint` from Redis into a group:

```json
[
  {
    "id": "reviewed-event-outcome",
    "usd_parity": false,
    "members": [
      {"venue": "polymarket", "market_id": "CONDITION_ID", "fingerprint": "64_HEX_CHARACTERS_FROM_REDIS", "invert": false},
      {"venue": "predictfun", "market_id": "MARKET_ID", "fingerprint": "64_HEX_CHARACTERS_FROM_REDIS", "invert": false}
    ]
  }
]
```

`invert=true` means this market's native NO is the group's affirmative outcome.
`usd_parity=true` explicitly accepts nominal dollar parity for different supported
collateral currencies. Fingerprints bind the exact rules, outcome labels, cutoff
and currency. Changed fingerprints invalidate review and appear in `invalid_matches`.
The file is loaded at startup; restart the scanner after changing it.

## Verification and one-shot use

```powershell
uv run --project python --no-sync python -m pytest user_strategies/test_prediction_scan.py user_strategies/test_market_data.py -q
uv run --project python --no-sync --env-file .env python -m user_strategies.prediction_scan --once
```

For a bounded connectivity check that explicitly reads every selected book, append
`--max-markets 5 --book-scope all --interval 1`. This cap is
explicitly marked `coverage_complete=false`; omit it for full scanning. A full
`--once` discovers all catalogs first and then reads only matched books. Explicit
`--book-scope all` enables the expensive exhaustive book sweep and should generally
be used only with a small smoke-test cap. The default never does that sweep.

Official API references:

- [Kalshi markets](https://docs.kalshi.com/api-reference/market/get-markets)
- [Kalshi orderbook](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
- [Polymarket market data](https://docs.polymarket.com/market-data/prices-order-books)
- [Polymarket fees](https://docs.polymarket.com/trading/fees)
- [Opinion markets](https://docs.opinion.trade/developer-guide/opinion-open-api/market)
- [Opinion orderbook](https://docs.opinion.trade/developer-guide/opinion-open-api/token)
- [Hyperliquid outcome metadata](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot)
- [Hyperliquid outcome asset IDs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids)
- [Predict.fun markets](https://dev.predict.fun/get-markets-25326905e0)
- [Predict.fun orderbook](https://dev.predict.fun/get-the-orderbook-for-a-market-25326908e0)
