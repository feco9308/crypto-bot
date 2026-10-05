# Current quotes / manual entry permissions — 2026-10-05

Implementation branch: `feature/paper-quotes-permissions`, based on the production
`feature/trading-core-paper` branch. Code commit: `f76627f`.

The strategy reads only persisted **closed 1h** scanner snapshots. It receives no
current quote or open-candle indicator. Market Score, scanner selection, indicator,
data-provider and scanner CLI source files are unchanged.

An independent read-only public Binance bookTicker provider retrieves one symbols
batch per cycle, outside the ledger write transaction. BUY uses ask; SELL, stop/TP
and long mark use bid. Fee/slippage are additional. Quotes have source, bid/ask and
local receipt time; REST does not supply exchange-update timestamps. There is no
candle fallback, signed API request, key configuration or live execution adapter.
Missing, malformed, crossed, nonfinite, future or stale quotes never fill an order.
Rate-limited retrieval respects cooldown. Default poll 15s, max quote age 10s, timeout 5s.

Fresh quotes can trigger protection/manual exit without a new or fresh strategy
snapshot. Missing marks preserve the last quote price with a stale warning and block
new BUY while allowing risk-reducing exits with valid quotes. Quote age is rechecked
after acquiring the ledger transaction and at fill; pending risk is resized/rechecked
against current ask, cash, exposure, stop and target.

WATCH/PINNED remain scanner/dashboard observation overrides. Automatic selection
or separate `manual_trade_enabled` permission is required for BUY. Missing flags
mean false. Manual flag denial does not revoke automatic eligibility. IGNORE/global
OFF still block entries. SELL is not blocked by entry permission. The gate is enforced
by strategy, orchestration and execution, including pending order rechecks.
Permissions survive paper reset; scanner overrides are not changed by permission actions.

CLI: `paper permissions`, `paper allow --symbol SYMBOL`, `paper deny --symbol SYMBOL`.
Paper UI: independent CSRF-protected permission controls, eligibility list, quote mark
receipt/stale indicator, separate strategy-reference and execution/risk-quote audit.

Migration `0003_quotes_permissions` adds paper_symbol_permissions and nullable quote
metadata columns only to extension tables. The existing schema_version stays 1.
On a consistent production DB backup, every original scanner table count and full-row
SHA256 matched before/after migration, integrity_check=ok, trading OFF and no manual
permission implicitly enabled. Re-running migration is idempotent.

Validation: **134 tests passed**, no warnings; ruff, compileall and diff checks passed.
This includes all previous 98 tests plus 36 new deterministic quote/permission cases.
Tests use fixed quotes/mocked HTTP. They cover price divergence, independent strategy
inputs, bid/ask spread, fee/slippage/risk, marks without new candles, stop/TP/manual
exit with stale candles/OFF/IGNORE, missing/stale/future/mismatched quotes, exposure
with missing marks, entry eligibility, custom strategy bypass protection, pending
price/permission rechecks and cash release, reset persistence, CSRF, migration,
read-only endpoint and cooldown, and the absence of a write lock during quote retrieval.

Production backup and rollback plan:
`/home/ndvi/crypto-bot-backups/quotes-permissions-20261005-203749-UTC/`.
The deployment keeps the running scanner process and restarts only web/paper after
migration. Actual deployment checks are recorded in `production-validation.json` in
that directory. Trading must remain OFF; no manual permission is enabled by deployment.
