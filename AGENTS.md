# Application development

This is a standalone prediction-market application. Use the uv-managed
`python/.venv` from the repository root. This project does not depend on or
build NautilusTrader. Use the locked dependencies; do not compile an engine.

Preserve exact prices, quantities, fees and money with Decimal or domain types.
Keep edits scoped to the task and run relevant tests. Preserve the
`user_strategies` module paths and `prediction:v1` Redis contracts.

Do not start live trading, send notifications, grant allowances, commit, push,
create remote repositories or deploy without explicit user authorization.
Keep credentials, environments, logs, runtime data, binary wheels and release
archives out of Git. Keep unresolved trade journals intact and never bypass
reconciliation by clearing a journal. Only one live process may use an account.

Publish releases with uv.lock and uv-exported hashed requirements; offline
installations must require hashes. No runtime credentials are bundled.
