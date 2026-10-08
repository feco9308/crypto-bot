# Historical Replay Lab — deployment validation

Validated 2026-10-08 UTC. Development branch: `feature/historical-replay-lab`.
Production branch remains `feature/trading-core-paper`; deployment uses fast-forward only.

## Commits

- `c487f4d`: isolated clock, archive/cache, universe, engine, ledger, migrations and backend tests.
- `52e320f`: Replay Lab UI, controls, results, audit charts, compare, presets and exports.
- `1cf7132`: deployment units and architecture/limitations documentation.
- `6feb946`: archive identifiers (including one-character/Unicode names), bounded parallel preload and regression tests.
- `9932331`: selected intrabar trailing threshold retained in closed-trade audit.
- `7e3cf1d`: malformed numeric API configuration returns HTTP 400.

The original Exit Simulator and risk-cap audit fix remain in the ancestry. No production core source file changes.

## Tests and browser

- Final full suite: **311 passed** (246 existing + 65 replay tests).
- Ten Chromium tests included: eight existing paper/analysis/simulator browser tests and two new replay tests at 1440px/390px.
- Additional production Chromium validation: completed results, variant switching, trade chart (1m/1h), three-variant compare, no page overflow or JavaScript errors.
- Both smoke runs: analysis JSON, compact JSON, CSV and ZIP validated; every closed trade is present.
- `ruff check crypto_bot tests`: passed in development and production.
- `ruff check .`: four pre-existing I001 import-order errors remain in `backtest.py`, `legacy/backtest.py`, `legacy/dashboard.py`, `legacy/trading_bot.py`. These unchanged legacy files were not edited.
- `git diff --check` and user service unit verification: passed.

[Production comparison screenshot](screenshots/historical-replay-comparison-desktop.png) · [Production mobile trade screenshot](screenshots/historical-replay-trade-mobile.png).

## Smoke runs

| Run | Period UTC, end exclusive | Top N | Variants | Opened / closed | Runtime |
|---|---|---:|---:|---:|---:|
| `8cc726a9227b4b78be4cba59f11e4f10` | Jan 1–3, 2025 | 10 | 1 | 4 / 1 | 358.2s |
| `9dc38dbd86d044ed858e4c1a46b308d5` | Jan 1–31, 2025 | 20 | 3 | 137 / 137 | 138.5s |

Both finished COMPLETED without replay errors. The 48h run retains three OPEN positions in its own replay account; end-of-run liquidation is intentionally absent. No annual run was launched.

The first initial download attempt (`825c5947f6634ed4bb88c1ef8ce987ea`) failed on an overly strict archive-symbol validator. This exposed actual single-character identifiers; the fix and regression tests were added before rerunning successfully. That failed research record is retained, and its completed downloads were reused. Subsequent smoke runs completed without exceptions.

| 30-day variant | Closed trades | Final equity USDT | Net PnL USDT | Max DD % | Ambiguous | Sample |
|---|---:|---:|---:|---:|---:|---|
| Production Baseline | 11 | 953.123885 | -46.876115 | 5.8791 | 0 | INSUFFICIENT SAMPLE |
| Current + TP1.5 | 64 | 966.182703 | -33.817297 | 5.0665 | 0 | SMALL SAMPLE |
| Current + Trail1.5/0.5 | 62 | 953.196326 | -46.803674 | 5.4313 | 28 | SMALL SAMPLE |

**NOT STATISTICALLY VALIDATED**. These are pipeline checks, not strategy optimization or future profit estimates. Risk guards stop new entries; existing marks/drawdown can move beyond configured limits. All three accounts exercised drawdown-blocked BUYs while exits remained available.

## Performance

30-day Top20 / three variants, shared data pass:

| Measured stage | Seconds |
|---|---:|
| database_seconds | 9.864 |
| execution_cache_seconds | 72.071 |
| execution_seconds | 12.849 |
| hourly_cache_seconds | 13.219 |
| indicator_score_seconds | 18.908 |
| scanner_seconds | 21.098 |
| universe_seconds | 2.190 |

Cache hits: 2596; additional downloaded candles: 1,383,840. The warm hourly archive cache was reused. Missing historical archives are explicitly recorded; they do not create fabricated candles.

Worker memory peak during the performance run: approximately 372 MiB; CPU approximately 54s. Unit limit: 2 GiB, CPU quota 75%, Nice=10. These measurements cover this 30-day dataset, not a year-long guarantee.

## Production isolation and services

- New replay migration namespace: `replay_schema_migrations`, schema **1**.
- New DB: `/home/ndvi/crypto-bot/data/replay.db`.
- Cache: `/home/ndvi/crypto-bot/data/replay-cache/`.
- Production schema remains `0003_quotes_permissions`; no production migration ran.
- No `replay_%` tables exist in market.db.
- Existing strategy_signals, risk_decisions, paper_orders and paper_fills prefixes have identical row counts and SHA256 before/after.
- Scanner PID **20305** and paper PID **96297** remained unchanged.
- Web restarted for deployment; final PID **120719** (before deployment: 94731).
- `crypto-replay-worker` is enabled as a separate user service; final PID **120715**, READY after the smoke runs.
- Linger=yes; the web/worker continue after terminal logout.
- Paper trading remained ON, account generation **1**, three existing positions: SANDUSDT #8, RAYUSDT #9, NEARUSDT #11.
- Production cash remained exactly `738.261040998693` USDT; no reset or manual production action.
- Replay worker journal after the final code restart contains no replay exception.

## Routes

Pages: `/replay`, `/replay/runs/<id>`, `/replay/runs/<id>/trades/<trade_id>`, `/replay/compare`, `/replay/presets`.

APIs: `/api/replay/runs`, `/api/replay/runs/<id>`, `/results`, `/trades/<trade_id>/chart`, `/export.<json|compact.json|csv|zip>`; `/api/replay/compare`, `/api/replay/presets`; POST controls `/pause`, `/resume`, `/cancel`, `/clone`. See [full route/method documentation](HISTORICAL_REPLAY.md#routes-and-apis).

Deployment code HEAD: `7e3cf1d`; a documentation/screenshots commit follows.

## Safety flags

**NO LIVE TRADING**

**NO PRODUCTION STRATEGY CHANGE**

**NO SCORE CHANGE**

**NO PAPER RESET**

**NO PRODUCTION DB REPLAY WRITES**

**STRICT NO-LOOKAHEAD REPLAY**

See [WHAT THIS BACKTEST CANNOT PROVE](HISTORICAL_REPLAY.md#what-this-backtest-cannot-prove) for execution/universe approximations, missing-data and overfitting limitations.
