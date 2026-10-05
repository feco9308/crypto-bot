# Repository audit and milestone plan

Audited GitHub baseline: `de95639`; all seven text files and image.png reviewed as repository inventory. No original server resources are required.

| File | Finding / disposition |
| --- | --- |
| README.md | Missing reproducible dependencies; requires credentials for public data; old server paths. Replace with fresh installation guide; preserve original in legacy/. |
| trading_bot.py | Useful RSI14 + EMA9/21 rule. API, calculation, signal and CSV writes coupled; missing config.py breaks import; no timeout/retry; open candles and short EMA warmup. Preserve original and extract pure legacy strategy. Replace entrypoint with scanner CLI. |
| dashboard.py | Useful Flask approach and score/price chart idea. Huge inline template, API calls and writes on GET, five-second polling, fixed timeframe assumptions. Replace with database-backed Flask app and templates/static assets. |
| backtest.py | Keep as legacy reference; CSV-only, optimistic fills, no slippage; fee attribution excludes entry fee from individual trade PnL; signal_combined column mismatch. No new trading backtest in this milestone. |
| watchlist.py/json | Preserve manual USDC reference. Fixed watchlist and inert auto_trade fields replaced by database overrides. Never interpreted as execution permission. |
| .gitignore | Extend for env, SQLite sidecars, environments, build outputs and runtime files. |
| image.png | Historical dashboard screenshot; preserved. |

Binance-specific pieces: Spot SDK, klines schema, USDC symbols, exchange metadata and volume/spread discovery. General components: candles, technical features, heuristic scoring, time-based history comparison, selection, overrides, ORM storage and web presentation.

Architecture: crypto_bot/data -> indicators -> scanner (scoring/momentum/selection) -> storage; web only reads storage and writes overrides. Legacy strategy is independent of scanner ranking. SQLAlchemy isolates database backend; SQLite schema version 1 initialized idempotently, future changes require explicit migrations. No broker or risk stubs: these belong to a later paper-trading milestone.

Schema: instruments (metadata), scanner_runs (UTC time, status, config, errors), snapshots (score components and features), decisions (per-run algorithm selection and reason), overrides (current user choice), override_events (audit). Snapshots indexed by symbol/timeframe/time. Config snapshots allow interpreting historical scores after weight changes.

See README for score formulas, historical freshness, deployment, limitations and next milestone. Implementation sequence: (1) audit/legacy preservation, (2) data/scanner/storage, (3) UI and deployment, (4) offline regression tests and fixes.
