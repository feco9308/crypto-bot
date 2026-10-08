# Historical Replay Lab

Historical research, isolated from the running scanner and paper account.
**NO LIVE TRADING. NO PRODUCTION STRATEGY CHANGE. NO SCORE CHANGE.
NO PAPER RESET. NO PRODUCTION DB REPLAY WRITES. STRICT NO-LOOKAHEAD REPLAY.**

## Architecture

`crypto_bot/replay/` owns the simulated clock, immutable configuration snapshots,
archive/cache provider, historical universe, engine, registries, local execution,
metrics, exports, separate SQLite storage and background worker.

```
Public Spot archive → indexed replay cache → historical universe (past volume)
→ unchanged scanner features / heuristic-v1 / score momentum / auto watchlist
→ registered reference strategy → unchanged RiskManager
→ run-local PortfolioService → local historical execution → replay.db
```

The production `ReferenceStrategy`, `RiskManager` and `PortfolioService` are
reused without edits. PortfolioService operates through a memory ledger containing
only that variant's account and positions; it has no production SQL connection.
Each variant gets a separate immutable PaperSettings snapshot constructed from
explicit replay parameters and captured defaults. PaperSettings.load(), production
configuration files, symbol permissions and the production execution adapter are
not used. There is no authenticated exchange adapter or order endpoint.

Web requests queue work and read results; they never execute a replay synchronously.
`crypto-replay-worker` is a separate singleton process. Only the web service needs
restarting when deploying the UI. Scanner and paper engine keep their existing PIDs.

## Start and operate

Open `/replay` behind the existing authenticated reverse proxy. Choose a UTC period,
universe, entry/exit/risk parameters and **START REPLAY**. Start/end must be whole
UTC hours; end is exclusive. For a complete calendar year use January 1 through
January 1 of the following year. Defaults: closed 1h strategy candles, Top 50,
1000 USDT, risk 0.5%, three positions, total exposure 50%, symbol exposure 20%,
daily loss 2%, drawdown 5%, minimum order 10 USDT, fee 0.1%, slippage 0.05%,
pyramiding disabled. Production settings and `.env` risk values do not override
these run defaults.

Single Run submits one variant; Compare Run supports 2–10 manually configured
variants. The variants share one market/scanner pass and downloaded candles, but
have independent portfolios, risk decisions, exits and equity.

```bash
# One-time user-service installation; adjust paths when using another checkout.
mkdir -p ~/.config/systemd/user
cp deploy/user/crypto-replay-worker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now crypto-replay-worker
systemctl --user status crypto-replay-worker
journalctl --user -u crypto-replay-worker -n 100 --no-pager

# Foreground alternative (development, not alongside the service)
.venv/bin/python -m crypto_bot.replay.worker
# Process one queued run, then exit
.venv/bin/python -m crypto_bot.replay.worker --once
```

User lingering must already be enabled to run after logout/reboot; verify with
`loginctl show-user "$USER" -p Linger`. Production uses lingering. Closing the
browser or terminal does not stop the service. `systemctl --user stop
crypto-replay-worker` gracefully requeues an active run at its last committed
checkpoint; starting it continues the run. It does not stop production trading.
The supplied worker unit lowers CPU priority and limits worker memory/CPU only.

Optional `REPLAY_DATA_DIR` sets the parent of `replay.db` and `replay-cache`.
Default is `data/` for the worker, and the production database's parent for web.
Set the same absolute path for both if overriding it. No Binance API key is needed.

## No-lookahead and timing

Only hourly candles with `close_time < simulated_time` enter scanner features.
The engine preloads archives but exposes only each timestamp's closed lookback.
At 15:00 UTC the 14:00–14:59:59 candle is available; the 15:00 hourly candle is not.
Warmup preloads 524 hours and evaluates 24 pre-start scanner timestamps to seed
score deltas using up to 499 closed candles per calculation. Instruments require
at least 400 continuous valid closed hourly candles before becoming eligible.
Warmup cannot create orders or positions.

Signals use hourly candle references. An approved BUY uses the execution minute
open at the scanner boundary, with upward slippage; a strategy SELL uses that
minute open with downward slippage. The minute open is a newly observed execution
price, not a completed candle passed to the strategy. Risk sizing uses this
execution reference. A missing exact execution-minute candle produces **NO FILL**
and an audit reason; the signal is not carried forward to an invented price.
Fresh marks for existing holdings must be available before sizing another BUY.

After a minute closes, its OHLC may trigger a stop/TP/trailing overlay. Those
intrabar approximations are recorded at the minute-close boundary, with both
candle timestamps and the trigger precision. Fills use the trigger/open-gap price
and downward slippage. There is no claim of the exact tick/time of execution.
Portfolio marks use completed execution-minute closes, not strategy candle closes.
No execution data means no mark/exit; the prior mark remains until data returns.
This missing-data limitation must be considered when interpreting drawdown.
Daily equity anchoring and limits use UTC days, including periods with no holdings.

Optional 5m fallback is **off by default**. It is used only for missing execution
windows containing no 1m candles, never mixed with a 1m candle in the same window.
Fallback exits/marks wait for the full 5m close; fills identify `source_timeframe=5m`.

## Historical universe

Method: `ROLLING_24H_QUOTE_VOLUME_APPROXIMATION`.

Candidates come from the public archive catalog, which also includes archived
symbols no longer traded. Today's exchangeInfo/top-volume ticker is not used.
At each simulated hour we apply USDT/non-stable/non-leveraged eligibility and
liquidity gates, then rank by the strictly preceding 24 closed hourly candles'
quote volume. Top N is configurable from 1 to 200. Insufficient/gapped/invalid
hourly history is excluded. Held instruments outside Top N continue receiving
exit observations without becoming new auto-watch candidates.

This is **not** a reconstruction of historical exchangeInfo, listing permissions,
exchange filters, suspensions or delisting announcements. Archives missing from
the catalog or missing warmup may introduce survivorship/data-availability bias.
An optional explicit candidate list is labeled `EXPLICIT_RESEARCH_SET`; it must
not be interpreted as a complete exchange universe. HistoricalUniverse is a
separate provider boundary for future historical listing metadata integration.

Spread defaults to a configured constant 0.02%; hourly price-change and quote-volume
features replace historical ticker observations. Historical books are unavailable
here. These approximations are captured in config/exports. The score formula and
weights themselves remain unchanged.

## Registries and exits

`STRATEGIES`, `SCORES`, `EXITS` validate known definitions/parameters. The initial
strategy is `watchlist_reference_v1`, version `1.0.0`, backed by the actual
production ReferenceStrategy. The score is the existing `heuristic-v1`. Full
strategy, risk, score weights, scanner settings and exit parameters are saved on
creation, so a future default change cannot silently change a saved run.

Entry defaults: score ≥70, Δ4h ≥0, algorithm watch required, bullish EMA50/200,
RSI 40–70, relative volume ≥1. WATCH/PINNED and manual production permissions
are never imported into replay. Signal strength retains its relative-strength
meaning; it is not a win probability.

Baseline exit: original ATR stop, score below 45 or bearish EMA exit. Optional
`fixed_tp`, `r_tp`, `break_even`, `trailing_pct`, `trailing_r`, `profit_lock` reuse
existing pure counterfactual OHLC path functions. Baseline stop/strategy exits
remain active with every overlay. Dynamic UI fields expose only the chosen exit's
parameters. There is no optimizer, automatic grid search or best-strategy claim.

Both plausible OHLC paths are checked. If paths disagree (including exit versus
carry), the trade records `intrabar_ambiguity`. Conservative mode selects the
lowest valid exit in that bar; optimistic selects the highest. Neither selects
using a later final PnL. These are **local exit-priority policies**, not bounds on
full-run portfolio returns. A carry path can diverge indefinitely; the model does
not simulate a branching portfolio tree. MFE/MAE stop at the selected exit path.

## Storage, cache and reproducibility

Default resources:

- `data/replay.db`: dedicated WAL SQLite database, migration namespace
  `replay_schema_migrations`, version 1.
- `crypto_bot/replay/migrations/0001_initial.sql`: additive replay-only schema.
- `data/replay-cache/candles.sqlite`: indexed hourly/minute candles.
- `data/replay-cache/objects/`: content-addressed archive ZIPs and resumable `.part`.
- `data/replay-cache/metadata/catalog.json`: catalog cache (one-day refresh).

ReplayStore refuses non-`replay.db` paths and databases containing production
schema tables. Replay paths reject symlinks and production DB hardlinks. Web replay
handlers have no production write repository. No production migration is added.
Run config/progress/checkpoint live in replay_runs; variant summaries contain
period/symbol/bucket metrics; replay_trades represents both OPEN/CLOSED positions.
Orders, fills, equity and audit events have separate replay tables.

Hourly preload uses at most three concurrent downloads with shared 0.15-second
request pacing and independent HTTP sessions. One-character and Unicode asset
names are accepted; URL/path/query characters are rejected.

Downloads use GET-only [Binance public Spot archive data](https://github.com/binance/binance-public-data/blob/master/README.md),
not a trading API. Monthly ZIPs are reused; recent unavailable months can use daily
archives. Objects support HTTP Range resume, bounded sizes and local SHA256
fingerprints. 404 objects are explicitly cached as MISSING for one day. This does
not verify Binance's separate published checksum files; retained local content
hashes identify exactly what was ingested. Binance archive timestamps may be
milliseconds or microseconds; the importer normalizes to milliseconds.

Completed runs record application/git/strategy/score versions, immutable config
hash, resolved candidate symbols, used archive hashes, cache revision and profile.
No random seed is used because this model is deterministic. Reproduction requires
the same code, config, candidate dataset and retained archive contents. A later
catalog/archive revision can change the available dataset and must not be treated
as an identical experiment. Clone preserves the captured config, not a historical
Git checkout or an immutable remote download snapshot.

Compact array storage keeps hourly history near 64 bytes/candle plus indices;
only the current indicator window is materialized. Past-volume prefix sums and
binary searches avoid re-summing full archive histories. Variants share scanner
calculations. Profiling records hourly cache, execution cache, universe, indicator/
score, execution and database seconds. Results/checkpoints commit in batches per
simulated hour, across all variants. No transaction per indicator or minute mark.
Annual Top50 can still require many archive downloads and substantial CPU/disk;
only the requested short validation runs are launched automatically.

## Lifecycle and controls

`QUEUED → DOWNLOADING → WARMUP → RUNNING → COMPLETED`; errors produce `FAILED`,
cancel produces `CANCELLED`. PAUSE/RESUME are safe at calculation/download check
boundaries; a paused singleton worker holds the current run and queue until resumed
or cancelled. CANCEL preserves partial committed research records; exports require
completion. Worker interruption reruns only the uncommitted hour. Checkpoint and
all variants' trades/orders/fills/events/equity commit atomically, avoiding duplicate
fills on restart. Partial archive download bytes are retained. HTTP timeout may
delay controls during a network read. Queues permit at most 20 unfinished runs.

Worker heartbeat/PID is persisted separately. `/replay` displays STALE if the
heartbeat is over 30 seconds old. Worker status is independent of production paper
ENGINE/TRADING controls. Start/stop this worker never toggles paper trading.

## Results, comparisons and exports

Run results show marked final equity, cash, realized/unrealized PnL, fees/slippage,
return, drawdown, expectancy, R, wins/losses, hold duration, exposure and risk-block
counters. Closed-trade metrics exclude open positions; final equity includes open
marks. Remaining positions are **not forcibly liquidated** at the end. R equals
net realized PnL divided by approved entry risk amount. Portfolio peak/drawdown is
updated on execution observations; the equity chart stores hourly samples.

Breakdowns: symbol, BTC/ETH/ALT (symbol classification), entry score, Δ4h, RSI,
range position, optional past BTC EMA regime (UNKNOWN when BTC not observed),
monthly and quarterly. Period returns use actual marked equity changes, not the
sum of individual trade percentages. Period drawdown explicitly uses hourly
observations; it can miss intrahour peaks/troughs.

`/replay/compare` selects 2–10 completed variants, with period mismatch warnings.
Always **NOT STATISTICALLY VALIDATED**; <30 closed trades: INSUFFICIENT SAMPLE,
30–99: SMALL SAMPLE, ≥100: LARGER SAMPLE. Dataset roles EXPLORATION, VALIDATION,
OUT_OF_SAMPLE are metadata labels and do not validate an experiment automatically.
Saved presets only affect replay configuration.

Downloads from each completed variant:

- Analysis JSON: config/metadata, metrics/breakdowns, every closed trade, open
  positions and hourly equity curve.
- Compact analysis JSON: config/metadata, metrics/breakdowns and closed-trade
  analysis fields; no raw minute candles, equity rows or order audit.
- CSV: one row per closed trade, all requested indicators/drift/risk/PnL/cost fields,
  decimal money strings, UTC timestamps, spreadsheet-formula escaping.
- ZIP: both JSONs, CSV, audit timeline and explanatory README. No raw candle dump.

Trade detail shows a cache-only candlestick chart with BUY/SELL, original/active
stop and TP markers, plus linked strategy/risk/fill/exit-trigger audit. Chart GETs
never download or feed anything back into strategy. If candles are not cached,
the chart shows unavailable data rather than generating substitutes.

## Replay Config JSON import/export

On `/replay`, paste a request configuration into **Import Replay Config JSON**
and click **VALIDATE**. Validation uses the same `validate()` backend function
as normal run creation and presets. A valid response populates all form fields
and variants, selecting Compare mode for multiple variants. It never queues a
run or saves a preset: explicitly click **START REPLAY** afterward. Invalid
imports leave the form unchanged and show field paths and messages, including
variant indexes, unknown strategy/exit IDs and malformed numbers/dates.

**EXPORT CONFIG JSON** displays the current validated form configuration;
**COPY CONFIG JSON** copies it (with a selection/copy fallback), and
**DOWNLOAD CONFIG JSON** saves `replay-config.json`. Each operation validates
the current form without starting a run. Exports include effective defaults,
UTC period, dataset role, ranked universe and optional candidates, execution
settings, and every active variant's strategy/exit IDs, parameters and risk
settings. Decimal parameters are represented as strings to retain precision.

This is the existing `POST /api/replay/runs` request format, not a second schema.
For example, paste this request and validate it to expand all defaults:

```json
{
  "name": "Baseline versus trailing",
  "start": "2025-01-01T00:00:00Z",
  "end": "2025-01-03T00:00:00Z",
  "dataset_role": "EXPLORATION",
  "universe_size": 20,
  "candidate_symbols": ["BTCUSDT", "ETHUSDT"],
  "minimum_quote_volume": "5000000",
  "spread_approximation_pct": "0.02",
  "fallback_5m": false,
  "conservative": true,
  "variants": [
    {
      "name": "Baseline",
      "strategy": "watchlist_reference_v1",
      "strategy_parameters": {"min_score": "70"},
      "exit_policy": "baseline_v1",
      "exit_parameters": {},
      "risk_parameters": {"initial_paper_balance": "1000"}
    },
    {
      "name": "Trailing",
      "strategy": "watchlist_reference_v1",
      "strategy_parameters": {"min_score": "70"},
      "exit_policy": "trailing_pct",
      "exit_parameters": {"activation_pct": "1.5", "distance_pct": "0.5"},
      "risk_parameters": {"initial_paper_balance": "1000"}
    }
  ]
}
```

`POST /api/replay/config/validate` requires the existing session CSRF token and
returns `{"valid":true,"config":{...}}` with the normalized request. Send that
same `config` to `/api/replay/runs` to explicitly queue a run. Both endpoints
return HTTP 400 with `{"valid":false,"errors":[{"field":"variants[0].strategy",
"message":"..."}]}` for configuration errors. Malformed JSON uses field `$`.
Stored result snapshots contain derived engine metadata; use the Config JSON
tools to export a request instead of pasting an analysis/results export.

## Routes and APIs

Pages: `/replay`, `/replay/runs/<run_id>`,
`/replay/runs/<run_id>/trades/<trade_id>`, `/replay/compare`, `/replay/presets`.

GET: `/api/replay/runs`, `/api/replay/runs/<run_id>`,
`/api/replay/runs/<run_id>/results?variant=0&offset=0&limit=100`,
`/api/replay/runs/<run_id>/trades/<trade_id>/chart?variant=0&interval=1m`,
`/api/replay/runs/<run_id>/export.<json|compact.json|csv|zip>?variant=0`,
`/api/replay/compare?selection=<run_id>:0&selection=<run_id>:1`,
`/api/replay/presets`.

POST (existing session CSRF required): `/api/replay/config/validate`, `/api/replay/runs`,
`/api/replay/runs/<run_id>/<pause|resume|cancel|clone>`, `/api/replay/presets`.
These actions affect replay resources only. Trade tables are paginated; exports
contain all closed trades. Limits: 64 KiB request config, 10 variants, three-year
period, 200 ranked instruments, bounded chart pages.

## Verification

Replay unit/integration/browser tests use synthetic fixed OHLC data and mocked
archive HTTP. No live Binance dependency is introduced into CI. Coverage includes
clock/closed-candle boundaries, future-data invariance, warmup, strategy parity,
past-volume ranking, overlays and ambiguity, sizing/costs/limits, missing data,
archive reuse/resume, worker controls/recovery, exports, CSRF and production schema
isolation. Real Chromium checks desktop/mobile forms, chart/details, comparison,
presets and downloads. Deployment validation results are in
`docs/HISTORICAL_REPLAY_VALIDATION.md`.

## Replay stale-position policies

`stale_position_policy` defaults to `STRICT_FRESH_MARKS` (including old configs).
`RESEARCH_QUARANTINE_STALE` permits unrelated entries while retaining stale
capital, exposure and position slots; stale gains cannot increase risk sizing.
There is no forced liquidation or future-coverage lookup. Results and comparison
warn about stale valuations and exclude affected variants from ranking. See
[exact valuation, resumption and metadata semantics](REPLAY_STALE_POLICY.md).

## WHAT THIS BACKTEST CANNOT PROVE

Blocked BUY attempts are reported by exact existing audit strings in
`summary.blocked_entry_reasons`, with reporting categories in
`blocked_entry_categories`. Legacy `blocked_entries` totals remain unchanged.
Both normal and compact JSON include the breakdown; existing runs are enriched
from their audits on read without rewriting records. Incomplete audit coverage is
marked explicitly. Fresh-mark rejections identify missing symbols in new runs;
the result UI warns when new BUYs were blocked by unavailable portfolio marks.
See the [2024 baseline investigation](REPLAY_BLOCKED_ENTRY_INVESTIGATION.md):
its 5000 other attempts were 4999 missing-portfolio-mark rejections and one missing
entry minute. An open FRONTUSDT position lost archive coverage, halting new entries
and leaving a stale final mark. The guard and trading semantics were preserved.

Historical performance is not evidence of future profit. OHLC is not tick/order
book data: paths, fills, spread, liquidity and slippage are approximations.
Exchange filters, minimum lots, market impact, latency, partial fills and execution
competition are not simulated. The historical universe is approximate and archive
availability can introduce survivorship bias. Missing execution bars preserve old
marks and can understate risk. Conservative intrabar priority is not a global
worst-case result. Hourly charts/period metrics can hide intrahour variation.
Repeated parameter comparison risks overfitting; dataset-role labels and sample
counts are not statistical validation. This tool does not place real orders and
cannot demonstrate that an eventual live execution adapter would behave identically.
