# Institutional-style replay v1 — implementation and validation

Research feature only. **NO LIVE TRADING IMPLEMENTED.** This work does not deploy
or run the annual research experiment. The four-variant 2024 EXPLORATION config is
ready to import after review on this branch. Defaults are fixed, not selected by
optimizing 2024 results. Future 2025 VALIDATION and 2026 OUT_OF_SAMPLE runs must retain
predeclared parameters; this implementation does not tune or start those runs.

## Architecture audit and integration

The existing replay architecture already has clean strategy and execution
boundaries. `replay/registry.py` exposes strategy factories and parameter names;
`trading/strategy.py` consumes `MarketContext` and produces `StrategySignal`.
`replay/engine.py` owns an isolated `Variant`, using the unchanged production
`RiskManager` and `PortfolioService` on a `MemoryLedger`, then `ReplayExecution`.
There is no production account handle or exchange order sender in this path.

Hourly public Binance archive OHLCV feeds `HistoricalUniverse`, which selects a
past rolling24h quote-volume universe. Existing indicator/score calculations use
up to499 closed1h candles and524h scanner warmup. Strategy momentum comes from
actual scanner score snapshots, not visualisation candles. Minute public archive
OHLC drives fills/stops, preserving fee/slippage and conservative/optimistic
intrabar path rules. Historical spread remains a configured approximation, not
historical bookTicker reconstruction.

The existing `adaptive_peak_lock_v1` is already registered in the replay-only
`RESEARCH_EXITS` registry. It is extended with validated parameters, rather than
creating a conflicting second registration. Standard `EXITS`, production exits,
`ReferenceStrategy`, `RiskManager`, `PortfolioService`, score and scanner logic
are not changed.

Config import/export and normal run creation already share `validate` and
`input_config`. The builder now renders strategy fields from backend registry
parameter specifications so institutional fields survive import/export without a
second config format. Result/compare rendering and compact export are additive.
Existing baseline tests, including legacy adaptive config/trade equality, pass.

## New strategy and settings boundary

`institutional_style_v1`, version 1.0.0, is a replay strategy registration. Its
`InstitutionalSettings` extends an immutable settings value only within replay;
production `PaperSettings` is unchanged. The optional strategy-definition
`settings_type` defaults to `PaperSettings` for all old definitions.

Entry defaults:

- Closed1h/4h/1D EMA50 strictly aboveEMA200.
- RSI40–70 inclusive, relative volume>=1, Market Score>=70.
- Score delta strictly greater than0, default4h window; missing history blocks by
  default. The old ReferenceStrategy's delta semantics remain unchanged.
- Algorithm watch required by default; no automatic manual watch permissions.
- Spread<=0.2% where supplied; existing global quote-volume/eligibility filters
  continue unchanged. Score weights and formulas are untouched.
- Original protective stop is strategy reference minus2ATR14.

EMA fast/slow periods and the required timeframe flags are configurable, with
2<=fast<slow<=499. Readiness requires at least `slow` completed candles and the
latest expected completed timeframe bucket. Too-young listings remain explicitly
unavailable rather than receiving fabricated EMA values. All higher-timeframe
calculations use up to499 available completed observations, seeded with the
existing EMA implementation, never a partial candle.

Existing strategy exits are delegated to ReferenceStrategy's held-position
branch, preserving its score/EMA exit rules. Neither BTC nor correlation can
block a SELL. Adaptive replay retains its existing stop-only semantics, so its
strategy SELL suppression is unchanged.

## Strict no-lookahead and warmup

The context adapter resamples into UTC epoch-aligned4h and24h buckets. Every
bucket requires exactly4/24 contiguous hourly candles, each with the expected
full close timestamp. Partial/gapped buckets are discarded. A decision at `t`
uses only aggregate candles with `close_time<t`. Indicator calculations happen
only on that prefix. Future-mutation tests cover both4h and1D and BTC context.

DailyEMA200 needs200 complete daily observations. Institutional configurations
therefore derive `context_warmup_hours=4824` by default (201days), separately from
the unchanged524h scanner warmup. Custom EMA periods/correlation windows derive
the required context history similarly. New runs may need additional hourly
archive downloads; Top20 limits tradable rankings, not the catalog needed to
reconstruct historical volume rankings.

All hourly context histories are loaded with the extended start, but the scanner
clock still begins at its original524h warmup, and its499-candle scoring window
is unchanged. BTCUSDT is loaded for context even outside explicit candidates or
TopN. It is never inserted into the tradable universe merely because context
needs it. A mixed-run integration regression compares control trade fields and
complete equity curves to the standalone old control.

## BTC regime and volatility sizing

BTC4h and1D use the same configurable EMA periods:

- BULL: both fastEMAs above slowEMAs; risk multiplier1.
- BEAR: both below; default multiplier0, so new longs are blocked.
- NEUTRAL: mixed/equal structures; multiplier0.5.
- UNKNOWN: missing, insufficient or older-than-expected completed buckets;
  entry fails closed when the regime filter is enabled.

Each timeframe source timestamp is preserved in `institutional_context`.
Multiplier settings are restricted to0..1. Explicitly disabling the regime
filter uses multiplier1; changing the BEAR multiplier is an explicit research
configuration choice, never an automatic optimization.

Existing sizing already divides permitted risk capital by cost-inclusive
stop-loss-per-unit. LargerATR increases the stop distance and reduces quantity,
subject to unchanged position/exposure/cash/minimum-order limits. We do not add
another volatility-sizing formula. A replay-local immutable settings replacement
scales only `risk_per_trade_pct` before calling the unchanged RiskManager. Its
actual approved risk can be lower due to existing caps. Production settings are
never read from `.env` or modified.

## Correlation guard and audit

Simple hourly close-to-close returns, latest168 hours, at least120 timestamp-
aligned pairs, Pearson correlation>=0.85 blocks a candidate against any open
position. Only completed past candles are used; returns across hourly gaps are
excluded. Missing overlap or constant/zero-variance series produce a null
coefficient with `CORRELATION_INSUFFICIENT_DATA`; the sole initial missing-data
policy is `INSUFFICIENT_DATA_ALLOW`.

Correlation is evaluated at strategy decision and **again immediately before
BUY risk sizing**. The latter includes positions filled earlier in that same
cycle, avoiding a simultaneous-entry bypass. Existing slots/exposure/cash limits
still apply. SELL bypasses the entry guard.

Audit reasons include BTC_BEAR_REGIME, BTC_REGIME_UNAVAILABLE,
MULTITF_4H_NOT_BULLISH, MULTITF_1D_NOT_BULLISH, timeframe UNAVAILABLE reasons,
CORRELATION_LIMIT and informational CORRELATION_INSUFFICIENT_DATA. Strategy HOLD
reason counts may overlap and are kept in a separate
`institutional_entry_rejections` summary/checkpoint field. They do not inflate the
legacy attempted-BUY blocked counters. Execution correlation rejection belongs
to the actual attempted-BUY breakdown. Full per-trade context and source times
are stored in existing JSON audit/trade fields.

## Adaptive exit state machine

Defaults preserve the existing AdaptivePeakLock implementation exactly:

| Stage | Threshold | Stop |
|---|---|---|
| INITIAL_STOP | before0.75R | original stop |
| BREAK_EVEN | >=0.75R | max(previous, fee/slippage-aware net BE reference) |
| PEAK_TRAIL | >=1R | max(previous, BE, highest-0.75*entryATR) |
| TIGHT_PEAK_TRAIL | >=2R | max(previous, BE, highest-0.50*entryATR) |

EntryATR and approved initial risk are frozen at entry. A stop never falls.
Net BE reuses the existing inverse fee/slippage and12-decimal ceiling guard.
Gaps/intrabar constraints can still yield a loss; a protective stop does not
promise an exact-price fill.

Parameters: `activation_break_even_r=0.75`, `activation_trailing_r=1`,
`tighten_trailing_r=2`, `trail_atr_multiplier=0.75`,
`tight_trail_atr_multiplier=0.50`. Activation thresholds must be strictly ordered,
and the tight multiplier cannot exceed the ordinary multiplier.

Only non-default parameters use the new replay-only ParameterizedPeakLock.
Default/legacy configurations delegate to the original model and do not add
parameter metadata to old trade rows. Minute OHLC path selection and fees stay
unchanged. The old Shadow Research policies are not changed or registered with
production.

## Research result fields

New observations are reported for institutional runs, including their A/B
controls, and do not feed back into trading decisions:

- MFE/MAE absolute percentage and gross-price R; maximum observed high/reachedR.
- Maximum net unrealized profit after modeled liquidation costs, realized profit.
- GivebackR and final stop, trail activation time, explicit research exit stage.
- `peak_to_exit_giveback_pct`: fraction of positive maximum net profit returned.
- `peak_to_exit_giveback_percentage_points`: difference as percent of entry cost.
  These are distinct; legacy adaptive `giveback_pct` is unchanged.
- Losers reaching0.5R/1R/2R, winners returning>25%/>50% of peak net profit.
- Mean/median MFE, realizedR, givebackR; turnover notional of actual BUY/SELL fills.
- BTC BULL/NEUTRAL/BEAR/UNKNOWN, symbol, fixedATR% and exit-stage breakdowns.

Turnover is tradedUSDT notional, not portfolio profit. Existing equity/netPnL,
return, drawdown, cost, win-rate, expectancy and stale summary semantics remain
unchanged. Research breakdowns are additive; the legacy1h BTC labels remain
separate. A/B BTC research breakdowns use the first institutional variant's
higher-timeframe regime settings so the supplied comparison is consistent.

CompactJSON includes new per-trade fields/context and research aggregates.
Old compact trades may have null fields; existing fields are retained. Builder,
result detail, diagnostics and compare show the new settings/metrics. The new
strategy is never the default. Import/validation/export still require explicit
START REPLAY to create a run.

## Stale compatibility

Both STRICT_FRESH_MARKS and RESEARCH_QUARANTINE_STALE use unchanged freshness,
marking and conservative risk-view code. No substituted candle-close execution,
future archive-end knowledge or forced final-price liquidation is introduced.
Stale positions retain their existing cash/exposure reservations and OPEN_STALE
reporting. Missing correlation data can be allowed by policy, but it does not
release capital or change portfolio valuation.

## Schema, operations and validation

No new DB table or migration is needed. Existing JSON config/entry/audit/trade
columns carry the additive fields; old checkpoints restore with absent
institutional counters defaulting to empty. Config schema stays v1; strategy
version and the derived context warmup capture the new behavior. Public request
JSON never contains derived engine metadata.

Full suite: **584 passed in100.07s**, including 28 new deterministic institutional
cases and 2 new desktop/mobile browser cases. Maintained Ruff scope
`crypto_bot tests research` passes. Full `ruff check .` still reports exactly four
pre-existing I001 errors in `backtest.py`, `legacy/backtest.py`,
`legacy/dashboard.py`, `legacy/trading_bot.py`; those files were not edited.

Screenshots are under `artifacts/institutional-style-v1/`, widths 1440 and390.
They validate the isolated test application, not deployed production. Import
round-trip preserves all four variants and explicit parameters; no implicit run
POST occurs.

Production checkout remains `feature/trading-core-paper` at 5ea5d09. Scanner,
paper and web PIDs1333/1334/31948 remain unchanged. Existing replay jobs and data
were not stopped, reset, migrated or overwritten. At initial audit there was no
active/queued replay. **No deployment or annual replay was started.**

## Ready-to-import experiment

Tracked config: `docs/replay/institutional-style-v1-2024.config.json`.
Convenience copy:
`/home/ndvi/crypto-bot/artifacts/institutional-style-v1/2024-institutional-style-v1.config.json`.

2024-01-01T00:00Z→2025-01-01T00:00Z, historicalTop20, public catalog universe,
conservative minuteOHLC, fallback5m=false, quarantine stale policy. All four use
1000 initial balance,0.5% base risk,3 slots,50% total/20% symbol exposure,
100% daily-loss/drawdown research guards,0.1% fee and0.05% slippage:

| Variant | Strategy | Exit |
|---|---|---|
| A | watchlist_reference_v1 | baseline_v1 |
| B | watchlist_reference_v1 | adaptive_peak_lock_v1 |
| C | institutional_style_v1 | baseline_v1 |
| D | institutional_style_v1 | adaptive_peak_lock_v1 |

The deployed old production web cannot yet accept the new strategy; it must be
reviewed and deployed separately. On this branch, import and VALIDATE populate
the form; the user then explicitly chooses START REPLAY. No 2024 performance or
profit improvement is claimed by this implementation report.
