# Market Regime Attribution Diagnostic v1

Base: `0b8deee581a61d1be5f9f892f7e7600c9cafbd07`.
Branch: `research/market-regime-attribution-v1`.

This is descriptive attribution, not a strategy, feature search, entry gate or
replay. `strategy_candidate` is always `NONE`. The standalone research module
imports no production strategy, scanner, RiskManager, portfolio or execution code.
It opens no DB, performs no network capture and changes no production runtime.

## Sources and observations

The exact Low-Turnover v1 official Binance Spot daily source contract is reused:
BTCUSDT/ETHUSDT, 62 monthly ZIPs, official `.CHECKSUM` SHA256 verification, exact
12-field schema, UTC completed-day boundaries, and the 2025 microsecond timestamp
normalization. Only existing cache objects are read. Missing or changed source
artifacts stop the audit. There is no automatic download or archive refresh.

The original benchmark bundle is pinned to SHA256
`3d1547e474e4e490dde81fa5a2860839601199e277d14f22086b6c66ada8d899`.
Its ZIP CRC, every manifest member and every corresponding loose artifact are
verified before and after attribution. The loaded daily archive manifest must
match the original byte-for-byte. Existing institutional compact/bundle sources
are checked against their original whole-file and member fingerprints.

Cohorts are exactly `[2024-01-01, 2025-01-01)` (366 days) and
`[2025-02-01, 2026-01-01)` (334 days), UTC. The 2023 history and January 2025 are
used only as preceding indicator history for the respective cohorts. No 2023
research results or 2026 candle/outcome are produced or consumed. A boundary
label at 2026-01-01 denotes the end/availability of the final 2025 candle, not a
2026 price observation.

## Frozen definitions

Only the seven requested families are calculated: buy/hold, SMA200 state,
SMA200 distance, RV20/RV60, efficiency60, daily lag-1 serial structure and the
monthly regime table. Per-day rows, spells and attribution episodes are audit
records inside those families, not additional candidate features.

* An observation for day t becomes available after that day's candle completes.
  SMA200 is the arithmetic mean of exactly 200 closes ending at t. Strict
  `close > SMA200` is above; equality is below/equal. SMA distance is
  `close/SMA200 - 1`.
* Crossings compare the prior completed daily state to the current one. A change
  is assigned to the current cohort day. The first day may use a warmup state;
  no crossing outside the cohort is counted. Counts per 365 observed days are
  included because cohort lengths differ, without fitting a threshold.
* State durations are cohort-clipped. A continuing first spell is left-censored;
  the final spell is right-censored. No future candle is consulted to discover
  its actual end. Medians and maxima do not claim full lifetime spell durations.
* RV20/RV60 use the last 20/60 completed daily **simple close-to-close returns**,
  sample standard deviation (`ddof=1`), annualized by `sqrt(365)`.
* Efficiency60 uses 61 closes for 60 absolute close changes:
  `abs(close_t-close_t-60)/sum(abs(changes))`. A flat zero denominator is null.
  The only reporting bucket is `>0.30`; it is never a trading rule.
* Quantiles use type-7 linear interpolation at `(n-1)*p`.
* Pearson and Spearman use lagged return pairs wholly inside the cohort.
  Spearman uses average tie ranks. Zero variance is null. Positive/negative/zero
  day percentages are reported separately.
* Buy/hold starts at the first cohort open and marks subsequent completed closes;
  drawdown includes initial capital. Monthly returns start from the initial
  cohort open, then from the previous month's close, so they compound to the
  whole-period return. RV/serial returns retain the standard close-to-close
  definition. These are gross market observations, without benchmark trading.

## Existing benchmark attribution, without rerunning

T1/T2 PnL, transitions and days invested come from existing benchmark results,
curves and execution audit. Neither benchmark `run` nor `_run` is called. An
additional real-pipeline validation replaces both functions and every HTTP
request with exceptions; the full audit still completes successfully.

Allocation episodes run from pre-transition open equity to the next
pre-transition open equity, or to the original final net value. Quantities,
notionals and fees are taken from the **existing executed fill records**.
Opening/transition costs belong to the episode starting at that transition.
All episode PnLs reconcile to the original net PnL. Cash-only episodes can carry
exit costs; they are exported separately. Primary winning/losing transition
counts include only episodes invested after the transition. Additional all-
allocation counts avoid hiding cash exit costs. These are allocation episodes,
not a claim that each T2 rebalance is a closed asset trade.

Position durations start at zero-to-positive asset quantity and end at a full
close. Partial sells and subsequent buys preserve the original start. Open
positions are right-censored at cohort end; they are not force-closed. A
`1e-12` quantity tolerance handles the original audit's float serialization,
solely for position-identity bookkeeping. The original final-close liquidation
cost estimate stays a valuation adjustment and never becomes a new fill.

SMA state is observed at close and benchmark decisions execute next open.
Initial allocations and unavailable terminal fills explain differences between
crossing counts and transitions; the one-day shift also affects days invested.

## Institutional reference

The exact existing cohort controls are retained:
2024 `bf1c15aa369a4fd78ddf13b07636ce6d`, 2025
`f9d41057d61e4714b61df69d320620e0`, variant A institutional baseline.
Only annual closed trades, return, PF, modeled cost and win rate are summarized.
Closed-trade entry counts are labeled using the last completed BTC daily candle
strictly before entry; a same-day unfinished daily candle cannot contribute.
No regime-conditioned PnL, filter or optimized cutoff is calculated.

## Results and interpretation

Final classification: **`TREND_REGIME_SHIFT_PARTIAL`**.
`strategy_candidate: NONE`.

The documented concordance screen requires both assets to show weaker buy/hold,
lower SMA occupancy, higher crossing rate and lower efficiency before calling
the explanation CLEAR. Occupancy and crossings are one related state family;
efficiency is the separate directional-persistence check. Volatility is reported
independently; it is not silently required or assumed to rise. This is a
transparent descriptive screen, not a trained classifier or significance test.

| Metric | 2024 | 2025 |
|---|---:|---:|
| BTC gross market return | +121.31% | -14.43% |
| ETH gross market return | +46.27% | -9.98% |
| BTC above SMA200 | 80.05% | 70.66% |
| ETH above SMA200 | 70.49% | 39.52% |
| BTC crossings | 10 | 17 |
| ETH crossings | 6 | 13 |
| BTC median efficiency60 | 0.1055 | 0.1295 |
| ETH median efficiency60 | 0.1432 | 0.2067 |
| BTC median annualized RV20 | 49.56% | 36.28% |
| ETH median annualized RV20 | 59.89% | 67.52% |

More SMA-neighborhood whipsaw, less above-SMA occupancy and shorter invested
positions support a harder long-trend environment. They do not establish a
universally bearish, sideways or higher-volatility year. Efficiency is absolute:
a coherent downtrend can raise it. Its median rises for both assets. The fraction
above the frozen 0.30 bucket falls for BTC but rises for ETH. Pearson/Spearman
changes are small and not uniformly concordant.

The monthly table exposes losses and recoveries rather than labeling all 2025
bearish: BTC February -17.65%, April +14.08%, May +11.06%, November -17.56%; ETH
February -32.21%, May +40.95%, July +48.80%, November -22.26%.

Existing T1 gross/net PnL changes from +835.20/+802.47 to -228.96/-249.50 USDT;
T2 from +705.78/+662.94 to -189.93/-226.48. T1 transitions rise 11 -> 18,
T2 16 -> 31; average asset-position duration including censored holdings falls
48.83 -> 26.33 days for T1 and 55.10 -> 23.13 for T2. Invested winning/losing
allocation episodes are 3/3 -> 1/8 and 6/5 -> 7/15, respectively.

The institutional controls remain negative in both years: -11.54% and -14.37%.
BTC/ETH market structure alone therefore cannot identify the cause of those
Top20 portfolio losses, particularly the 2024 loss despite favorable broad
market returns. Different universe, exposure, timing and costs remain descriptive
limitations. No production rule or strategy candidate follows from this audit.

## Artifacts and reproduction

```bash
cd /home/ndvi/crypto-bot-regime-attribution
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python \
  -m research.market_regime_attribution --root /home/ndvi/crypto-bot
```

Output: `/home/ndvi/crypto-bot/artifacts/market-regime-attribution-v1/`.
Required CSV/JSON/report files and a deterministic ZIP include all frozen
statistics, the 46-row monthly table, 1,400 cohort daily observations, censored
spells, existing-fill episode/position audit, institutional entry-state counts
and source fingerprints. ZIP timestamps are fixed; JSON excludes NaN/Infinity;
manifest SHA256 values verify every member. No raw source or prior result is
changed. No database, migration, replay, service restart or deployment is involved.

Final validation: **28 targeted tests passed**, **944 full maintained tests passed
in 237.86 seconds**, Ruff PASS and staged `git diff --check` PASS. Browser tests
use the already installed local libraries via the test-process-only
`LD_LIBRARY_PATH=/home/ndvi/.local/lib/crypto-bot-browser/usr/lib/x86_64-linux-gnu`.
Independent high-precision variance/efficiency calculations and independent
serial correlation checks agree on all 1,400 observations. Market buy/hold and
monthly results match the original gross benchmark exports; all 76 allocation
episodes reconcile to the original net PnL. The 513 institutional entry-state
observations use strictly earlier completed BTC candles. Original source
fingerprints and production checkout/service PIDs remain unchanged.
