# Low-Turnover Benchmark Diagnostic v1

Base: `70b30e65d1a8015790930880640f3986c908f75a`.
Branch: `research/low-turnover-benchmark-v1`.

This standalone research harness imports no production strategy, score, scanner,
portfolio, risk or replay implementation. It opens no database and does not
restart any service. It runs exactly B0/B1/B2/T1/T2, with no parameter grid.

## Frozen scope

* Binance Spot BTCUSDT and ETHUSDT, official monthly archives of **1d** klines.
* 2024: `[2024-01-01, 2025-01-01)` UTC, 366 days.
* 2025: `[2025-02-01, 2026-01-01)` UTC, 334 days.
* 2023 June–December is indicator warmup only. January 2025 is not part of the
  2025 performance cohort. No 2026 archive or price is requested or evaluated.
* Independent 1,000 USDT accounts; fractional long Spot positions; no leverage,
  borrowing, shorting, minimum-order rules or liquidity/market-impact simulation.
* Gross: zero fee/slippage. Net: 0.10% fee and 0.05% adverse slippage on each side.
* SMA200 is the arithmetic mean of exactly the preceding 200 completed daily
  closes, including the just-completed decision candle. Close strictly greater
  than SMA means active; equality means cash. No EMA, delta, score or extra filter.

Official source contract and the 2025 Spot microsecond timestamp transition:
<https://github.com/binance/binance-public-data/blob/master/README.md>.
Every monthly ZIP is checked against its official SHA256 `.CHECKSUM`. The parser
requires 12 fields, UTC daily alignment, valid finite positive OHLC, unique ordered
rows, a complete month and exact bar close boundaries. Cached response bodies
are rehashed before reuse. Invalid/missing data stops the diagnostic rather than
silently backfilling. No REST account/order endpoints or credentials are used.

## Execution and terminal accounting

B0/B1 enter BTC/ETH once at the first cohort day's open. B2 splits the initial
budget equally, then lets weights drift without rebalancing. T1 holds BTC or
cash. T2 targets equal weights among currently active BTC/ETH; one active asset
receives 100%, neither means cash. T2 rebalances **only when the active-symbol set
changes**, not every day while the signal stays unchanged.

Every strategy fill uses the open strictly after the source daily close. No
current-day close participates in that day's opening decision. Completed source
close, SMA200, source timestamp and target weights are recorded with orders.
An unavailable signal cannot fabricate a fill or force an exit. Missing required
cohort candles fail validation. There is no end-of-period signal fill without a
next in-cohort candle.

On allocation changes, sell excess assets first and buy only the shortfall.
Target quantities solve equal **post-cost reference-open-valued** weights; fee
and slippage are deducted from cash, so allocating 100% cannot borrow fees.
The Decimal solver uses 45 digits of precision and records actual fee and
slippage amounts. No repeated sell-all/rebuy tax is imposed on retained assets.

**Terminal value is a liquidation-cost estimate, not an execution.** The last
completed in-cohort close marks remaining positions. Estimated adverse sell
slippage and fee are subtracted from terminal equity. This includes buy-and-hold
exit costs without inventing a same-close fill or reading a 2026 open. The audit
labels this `TERMINAL_LIQUIDATION_COST_ESTIMATE_NOT_A_FILL`; positions remain
open. Actual entries/exits and executed turnover exclude this estimate;
`terminal_estimated_exit_notional` and `turnover_including_terminal_estimate`
provide the additional boundary amounts explicitly. All cost totals and net
returns include the estimated terminal costs.

## Metrics and sensitivity definitions

The result has separate gross/net metric records and daily/monthly curves.
Drawdown uses initial capital and daily close equity, including the final cost
estimate; it does not infer intraday drawdown. Daily volatility is sample standard
deviation of simple equity returns. Descriptive Sharpe uses mean daily return /
standard deviation times sqrt(365), risk-free zero; zero volatility gives null.
Time in market is the proportion of cohort daily intervals with any asset held,
not an average exposure weight.

Gross minus net PnL compares independently compounded cash paths. It therefore
includes foregone compounding as well as modeled cash costs. Cost decomposition
exports both degradation / absolute gross PnL and actual modeled cash cost /
absolute gross PnL; a zero denominator gives null.

Leave-one-calendar-month excludes that month's **unchanged daily equity returns**
and compounds the others. Indicators, entries, other months and thresholds are
not re-fit. This is return-deletion sensitivity, not a tradable alternate history
with different capital allocation. The omitted final month also omits the final
cost estimate included in that month's return; this limitation is explicit.

The category wording in the request overlaps: B explicitly covers one positive
year, while C covers no benchmark positive in both years. We give the specific
mixed-year B rule precedence. A requires a frozen trend benchmark gross/net
positive in both years and positive after every leave-month deletion; otherwise
B applies if any trend-year is positive, C if none is. DAILY_TREND is returned
only for A. Neither the raw results nor next candidate depend on relabeling B/C.

## Completed results

Both frozen trend benchmarks are positive in 2024 and negative in 2025:

| Benchmark | 2024 gross / net | 2025 gross / net |
|---|---:|---:|
| T1 | +83.5204% / +80.2466% | -22.8962% / -24.9502% |
| T2 | +70.5782% / +66.2944% | -18.9927% / -22.6477% |

2024 leave-month outcomes remain positive in 12/12 cases for both. 2025 remains
negative in 11/11 cases for both. The two-period positive-edge hypothesis is not
supported. Classification: `LOW_TURNOVER_RESULT_MIXED`; next architecture
candidate: `NONE`. No production recommendation or parameter change follows.

Existing hash-validated institutional control exports have exact cohort periods:
2024 run `bf1c15aa369a4fd78ddf13b07636ce6d`, 2025 run
`f9d41057d61e4714b61df69d320620e0`. They are descriptive references only: Top20,
capped exposure and different timing versus fully invested BTC/ETH. Their
net-only results cannot identify exact high-turnover gross-to-net degradation.
No new institutional replay is run to obtain that missing counterfactual.

## Reproduction and artifacts

```bash
cd /home/ndvi/crypto-bot-low-turnover
# Offline by default; verified cache reused, missing cache is an error.
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python \
  -m research.low_turnover_benchmark --root /home/ndvi/crypto-bot
# Only initial collection requires the explicit --capture flag.
```

Artifacts: `/home/ndvi/crypto-bot/artifacts/low-turnover-benchmark-v1/`.
The ZIP contains report, summary, methodology, official daily-data manifest,
10 benchmark comparison rows, 20 gross/net metric rows, monthly and daily curves,
leave-month sensitivity, cost decomposition, execution audit, current reference
provenance, source validation and a SHA256 file manifest. Raw checksum-validated
archive response cache stays outside Git and the ZIP, under
`data/research-cache/low-turnover-benchmark-v1/`.

Tests use fixed synthetic prices, including completed-bar/next-open causality,
exact SMA200, warmup/cohort boundaries, fee/slippage, terminal cost estimates,
allocation without debt, unchanged-input/order invariance, deterministic
leave-month results/ZIP, official schema/timestamp units and cache corruption.
No production runtime file or DB is changed. No migration, deployment or service
restart is needed or performed.

Final validation: **36 targeted tests passed**, **916 full maintained tests
passed in 238.40 seconds**, Ruff PASS and staged `git diff --check` PASS.
Browser tests use the already installed local shared libraries through
`LD_LIBRARY_PATH=/home/ndvi/.local/lib/crypto-bot-browser/usr/lib/x86_64-linux-gnu`;
this setting is limited to test processes and changes no service environment.
An independent float cash-path oracle confirms B0/B1/B2/T1 gross/net results;
execution prices, completed-signal inputs, monthly compounding and leave-month
deletions reconcile with the exported raw curves. All 62 source archives and
both existing reference fingerprints remain unchanged.
