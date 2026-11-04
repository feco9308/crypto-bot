# Orthogonal Strategy Class Benchmark v1

Base: `61a9de499c7e5477d6c7ce1008eddb90cd33957d`.
Branch: `research/orthogonal-strategy-class-v1`.

This is a standalone EXPLORATION benchmark, not a production strategy or a Replay
Lab run. Only S1/S2 and the three requested portfolios are evaluated. No parameter
search, new indicator family, regime filter, strategy combination or promotion
is implemented. No production code, database, configuration or service is changed.

## Frozen data and cohort contract

Official Binance Spot BTCUSDT/ETHUSDT 1d OHLC candles use the exact validated
Low-Turnover v1 source contract: 62 monthly archives, their official `.CHECKSUM`
pins and 124 cached archive/checksum responses. Cache bodies, checksums, CSV
schema, UTC daily boundaries, gaps and OHLC validity are checked again offline.
The 2025 Spot microsecond timestamps are normalized using the original parser.
High/low are read from the original CSV fields 2/3, not inferred from open/close.
There is no download, archive refresh or authenticated API.

The source benchmark ZIP remains pinned to
`3d1547e474e4e490dde81fa5a2860839601199e277d14f22086b6c66ada8d899`.
The ZIP CRC, member hashes and loose artifact hashes are verified before/after.
Institutional reference source files retain their original fingerprints.

* 2024: `[2024-01-01, 2025-01-01)` UTC, 366 days.
* 2025: `[2025-02-01, 2026-01-01)` UTC, 334 days.
* Each cohort starts with 1,000 USDT and flat signal/financial positions.
* 2023 prices are indicator warmup only; no 2023 positions or PnL are simulated.
  The first cohort opening decision may observe the previous completed warmup
  candle. A preexisting warmup position is never inherited.
* January 2025 is history for the February start, not a performance cohort.
* No 2026 candle/open/outcome is requested or consumed. The end boundary is not
  a 2026 price observation. Eligibility does not launch 2023 clean validation.

## Exactly two frozen strategies

S1 Donchian Breakout 20/10:

* Flat -> long only when the completed decision close is strictly greater than
  the highest HIGH of the previous 20 completed candles.
* Long -> flat only when that close is strictly less than the lowest LOW of the
  previous 10 completed candles.
* The decision candle's own high/low is excluded from both channels. Equality
  does not trigger a transition. No stop, ATR, SMA, volume or regime condition.

S2 Bollinger Mean Reversion 20/2:

* SMA20 and sample standard deviation (`ddof=1`) use the last 20 completed closes
  **including** the decision close. Lower band = SMA20 - 2*sample std.
* Flat -> long only when close < lower band, strictly.
* Long -> flat when close >= SMA20, including equality.
* No SMA200 gate, RSI, volatility cutoff, time stop, stop loss or take profit.

Signal state is per asset and independent of portfolio occupancy and cost mode.
Signals are computed once per class/cohort. Gross/net and portfolio evaluation
cannot mutate them. Decisions have integer source-close and known-at timestamps;
a same-bar or future observation is rejected before execution.

## Portfolio and execution accounting

BTC only, ETH only and BTC/ETH equal-weight are the only portfolios. Equal weight
means 50/50 if both signal states are active, 100% in the sole active asset, cash
if neither is active. Weights drift until the active asset set changes. No daily
rebalance is added. Switching portfolios does not change an asset's signal state.

Every fill is at the next in-cohort daily open, strictly after the completed
source close. No decision-close fill, intrabar stop or lookahead is used. A final
signal without a next in-cohort open cannot fill.

Gross uses zero cost; net uses 0.10% fee and 0.05% adverse slippage on each executed
side. The existing research-only generic allocator solves post-cost open-valued
target weights, sells only excess holdings first and buys only shortfalls. This
reuses bookkeeping math, not a previous strategy/benchmark run. It cannot borrow
fees, go short or use leverage. Decimal arithmetic uses 45 digits of precision.

Final open positions mark to the last completed cohort close, less estimated
sell slippage and fee. The audit explicitly records
`TERMINAL_COST_ESTIMATE_NOT_A_FILL`. There is no terminal SELL or fabricated closed
episode. Actual executed fees/slippage/turnover and terminal estimates are exported
separately; summary total costs include the terminal valuation estimate.

## Episode and metric definitions

An asset position episode begins on its first BUY and completes only when fully
flat. Additional buys and partial sells from portfolio reallocations remain in
the same episode. Only completed episodes enter the minimum-count screen,
win rate, average/median holding time and mean/median episode return.

Completed episode cash-flow PnL = cumulative net sale proceeds minus cumulative
buy cash including fees. Return divides by cumulative buy cash. This counts
reinvested cash on additional buys in the denominator; it is not a time-weighted
return. Open episode terminal valuation and censored holding age are reported
separately, with no realized PnL or exit timestamp. All closed plus open-valued
episode PnL reconciles to portfolio PnL.

Entries/exits count actual BUY/SELL fills including partial rebalances; completed
episodes count full asset round trips. Time in market is the fraction of cohort
days with any holdings. Drawdown uses initial capital and daily-close equity,
including the terminal cost estimate, without claiming intraday precision.

Both gross/net metric records, daily and monthly curves are exported. Gross-minus-
net degradation includes changed compounding as well as actual cash costs.
Leave-one-calendar-month deletes that month's unchanged NET daily equity returns
and compounds the rest. It never refits, deletes source candles or changes state.

## Frozen cross-year screen

The SAME portfolio within a class must satisfy every condition:

1. Gross and net returns strictly positive in both cohorts.
2. At least five completed NET asset episodes in EACH cohort. Open/censored
   positions do not count; fewer gives `INSUFFICIENT_EPISODES`.
3. Every NET leave-month deletion stays strictly positive in BOTH cohorts. This
   conservatively operationalizes no single-month dominance, without searching
   a robustness threshold.

A class passes if any own portfolio passes. No post-hoc best portfolio or class
ranking is used. If both classes pass, the candidate is NONE with
`MULTIPLE_CLASSES_REQUIRE_PREREGISTRATION`; eligibility is YES because category A
holds. If only one passes, only that class name is the exploration candidate.
The supplied B/C descriptions overlap; the specific mixed-positive B rule takes
precedence when a positive NET result exists but the screen fails. C applies
when no NET row is positive. This naming convention does not alter any screen.

## Completed exploration result

`final_classification: ORTHOGONAL_CLASS_SIGNAL_FOUND`

`exploration_candidate: MEAN_REVERSION`

`clean_validation_eligible: YES`

Only **S2 BTC_ONLY** passes: gross +28.6381% / +19.2969%, net
+26.7229% / +16.1190%, completed episodes 5 / 9, and positive leave-month outcomes
12/12 / 11/11 for 2024 / 2025. Worst leave-month returns are +13.3739% / +4.0558%.
The 2024 episode count meets the minimum exactly; this remains exploration with
limited samples, not validation or production promotion.

S2 equal-weight has positive annual gross/net in both cohorts but fails 2025
leave-month robustness (6/11 positive, worst -13.8645%). S2 ETH-only is negative
net in both cohorts. No S1 portfolio passes the cross-year screen. These outcomes
do not change any strategy, parameter, portfolio choice or subsequent dataset.

Existing T1/T2 and institutional cohort references are read from completed
fingerprint-pinned artifacts only. Neither old benchmark `run`/`_run` nor any
institutional replay is called. The old market-regime diagnostic is not rerun.

## Reproduction and artifacts

```bash
cd /home/ndvi/crypto-bot-orthogonal-strategy
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python \
  -m research.orthogonal_strategy_class --root /home/ndvi/crypto-bot
```

Output: `/home/ndvi/crypto-bot/artifacts/orthogonal-strategy-class-v1/`.
It contains 12 strategy/cohort rows, 24 separate gross/net metric records, 8,400
daily equity rows, 2,800 canonical signal observations, 208 closed/open episode
records with explicit cost mode, 276 monthly rows and 138 NET leave-month rows.
The deterministic ZIP contains all requested reports/CSV/JSON and a SHA256 member
manifest. Original source cache and prior artifacts remain unchanged.

Independent verification uses float indicators and an analytic post-cost
allocation solution, separately from the Decimal simulator and bisection allocator.
All 24 financial paths, all 8,400 daily equity values, signals, episode counts,
returns, fees/slippage, turnover, drawdown and leave-month outcomes agree.
The real pipeline also completes with every HTTP request and previous benchmark
`run`/`_run` replaced by exceptions. No database, migration, service restart,
production change or deployment is involved. The 2023 clean validation is NOT run.

Final checks: **31 targeted tests PASS**, **975 full maintained tests PASS in
243.17 seconds**, Ruff PASS, staged `git diff --check` PASS. Browser tests use the
already installed local library path only in their test process:
`LD_LIBRARY_PATH=/home/ndvi/.local/lib/crypto-bot-browser/usr/lib/x86_64-linux-gnu`.
No dependency installation or service environment change is performed.
