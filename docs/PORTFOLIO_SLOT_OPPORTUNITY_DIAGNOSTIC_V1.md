# Portfolio Slot Opportunity Diagnostic v1

This is an offline, read-only analysis of completed run
`31423539f64e4650840df5b6b3fe9a26`, source commit
`75998cae198f09f7be33bf5777f048b9653d7f81` (2024 EXPLORATION).
It is never imported by production runtime. It creates no replay, portfolio,
replacement execution, strategy, filter, ranking or exit policy.

Run from the research worktree:

```sh
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python \
  -m research.portfolio_slot_opportunity --root /home/ndvi/crypto-bot
```

Output: `artifacts/portfolio-slot-opportunity-diagnostic-v1/`, including the
requested CSVs, detailed JSON, methodology, source fingerprints, report, manifest
and deterministic ZIP. Repeating the analysis with identical code/data produces
identical artifacts. No wall-clock timestamp or random sample is used.

Both SQLite sources use URI `mode=ro` and `PRAGMA query_only=ON`. No cache/provider
constructor, order API, network request, worker control or production DB is used.
The replay run/table fingerprints are checked before and after analysis. Archive
objects must match the exact source-run pinned SHA and manifest; missing/unpinned
minutes are not downloaded and are not filled in with hourly candles.

## Important source correction

The FET entry at 2024-02-24 12:00 UTC **filled in S**, as required by its exact A
parity. It was **blocked in R**. ETH/MATIC/WLD are the R incumbents at that time.
The analysis retains S as primary population and explains the R FET case
separately, rather than fabricating an S rejection.

## Time separation and units

- Candidate population: original S BUY Risk Decisions rejected specifically for
  `max open positions reached`; preserve every hourly opportunity and rank.
- The authoritative incumbents are the original Risk Decision portfolio snapshot,
  including any earlier same-timestamp fills. No guessing from trade intervals.
- Hourly reconstruction uses the original canonical 524-hour scanner seed,
  499 closed bars, original historical Top20 and pure, unchanged indicators/score.
  Past score observations must have been in Top20. Held-only rows do not write
  shared history. Recorded candidate score/delta4h/rvol are cross-checked.
- Decision-time peaks use only already closed minute bars, entry fill and the
  current known execution-open mark. No current/future minute high is used.
- Incumbent unrealized PnL reproduces quantized PositionView, net entry fee.
  R uses the stored original account-risk amount. Future remaining R subtracts
  that current unrealized PnL from the eventual net realized PnL.
- Candidate future price-R uses proposed strategy reference minus original stop;
  no hypothetical quantity/fill/fee-net profit or replacement PnL is invented.
- Fixed 6/24/72/168-hour extrema are raw price excursions, including after a stop;
  full contiguous minute coverage is required. MAE is signed negative.
- Target-before-stop labels run until original stop, first missing minute or run
  end. Stop wins a same-minute ambiguity unless a target was reached at the open
  first. Unknown/censored does not mean false. No end-of-year forced exit.
- All future information is in `POST_HOC_FUTURE_LABEL`; it is never a decision
  input. Actual future stop-candle highs are excluded conservatively.

## Descriptive comparisons

The negative control pool stopped before reaching +1R. A fixed SHA256 order
selects the same number as the +5R group, without feature threshold tuning.
Median, linearly interpolated quartiles, IQR, Cliff's delta and IQR overlap are
reported. Incumbent feature comparisons use one slot-median per decision.
Overlapping symbol/time opportunities are correlated and are not independent
trades or additive opportunity PnL. Unique symbol-day counts qualify the totals.

`descriptive_lowest_current_r_incumbent` identifies the weakest currently marked
R for inspection only; it is not a recommendation or executable replacement rule.
The full incumbent states are retained so it does not hide age/peak differences.

Tests: `pytest -q tests/test_portfolio_slot_opportunity.py`.

The before-stop barrier labels do not simulate hypothetical baseline strategy
SELLs. A price reaching +20R later does not imply the baseline would have held
until then or captured it. Tail-dependence removals are fixed-cohort subtractions,
not rerun portfolio paths with those entries removed.
