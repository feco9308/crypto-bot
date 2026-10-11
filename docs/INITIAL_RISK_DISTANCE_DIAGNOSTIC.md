# Initial risk distance diagnostic — 2024 EXPLORATION

This is an offline, read-only diagnostic of completed anchor
`b9275979397440f2aabbe4a89e4f9cc4`, variant 0. It preserves all 1506 canonical
entries, original stops, quantities, risk amounts, fills and outcomes. No
portfolio replay, filter, candidate PnL, production deployment or restart occurs.

Run from this research worktree:

```sh
PYTHONPATH=/home/ndvi/crypto-bot-shadow /home/ndvi/crypto-bot/.venv/bin/python \
  -m research.initial_risk_distance --root /home/ndvi/crypto-bot
```

Only the new artifact directory is written. Both databases are opened through
SQLite `mode=ro`; market/paper database constructors and network clients are not
used. Input candle ZIPs are SHA256 checked against the anchor source revisions.
Additional cached minute sources, if required after an exit, are explicitly
recorded and never used for pre-entry calculations.

## Fixed methodology

The pre-entry boundary is the scanner decision timestamp. Only candles whose
close timestamp is strictly earlier are admitted. The latest 499 consecutive
closed hourly candles reproduce the existing ATR14 Wilder calculation; the same
series feeds periods 24, 48, 72 and 120. Percent ATR uses the last closed close.
Range uses window maximum high minus minimum low divided by that close. Each
true range percentage uses its preceding candle close as denominator.

At every sampled hourly close, the local peak is the maximum high of the latest
24 closed hourly candles, including that candle. Pullback is `(peak-close)/peak`.
We summarize the last 72 and 120 samples, retaining zero observations. This is a
fixed rolling peak-to-close measurement, not a swing classifier or optimized
threshold. It does not assume that a candle low followed its high.

Actual initial price distance is **entry fill minus original stop**, not exactly
2ATR14: strategy reference price, execution drift and slippage differ. Report
strategy-reference distance separately. Canonical monetary 1R divided by quantity
includes costs and differs again. Recovery price targets use this canonical unit
1R, without claiming an executable net return.

Pullback percentages become absolute distances using the same last closed close
for both normalization and candidate D. Candidate percentages also use that
reference. Actual stop percentage uses actual entry fill; its denominator is
explicitly distinct. Candidate CONTROL is exactly 2ATR14, not the actual fill-to-
stop distance. A/B/C/D are distance comparisons only; quantities never change.

## Retrospective separation and missing data

Only ORIGINAL_STOP trades have a separate `RETROSPECTIVE_DIAGNOSTIC_ONLY`
recovery object. It contains no input to `pre_features` or candidate calculations.
Recovery uses full minute candles in `[exit, exit+H)`. The exited minute ends at
exit and is excluded. A hit timestamp is the first candle-close availability
(close timestamp + 1ms), not an invented intrabar crossing time. All times are UTC;
first-hit ISO timestamps follow the existing replay clock's UTC-naive convention.

If an observed high touches a threshold, that hit is known even if other bars
are missing. No observed hit is false only with complete coverage; otherwise it
is UNKNOWN. The main recovery rate denominator is complete windows, with partial
coverage and observed partial-window hits separately reported.

TYPE1, TYPE2 and TYPE3 are independent research labels. TYPE2/TYPE3 can overlap;
the overlap count is explicit. TYPE1 is not proof that an entry was causally bad.
Neither a subsequent recovery nor a narrow stop proves an alternative stop would
improve a portfolio: the path, exposure, cost and position-slot effects are not
simulated here. Quarterly/monthly groups use entry decision UTC, not exit time.

Every outcome group has N/mean/median/P25/P75 and descriptive five equal-width
histogram bins; minima/maxima/P10/P90 and relative IQR/CV are also exported.
Sparse symbol/month/bucket results are descriptive, with no threshold selection.

## Results

All 1506 FILLED BUY entries match the immutable anchor; ATR14 parity and no-lookahead violations: zero. Original outcomes remain 659 ORIGINAL_STOP, 223 BREAK_EVEN_075R, 610 PEAK_TRAIL_1R, 14 TIGHT_PEAK_TRAIL_2R. Original-stop disjoint highest-R cohorts remain 318/128/72/66/52/23.

### Five research answers

1. ATR14 normalization is tightly clustered (actual stop/ATR14 median 2.0233), but multiday normalization varies: stop/ATR72 median 2.2768, IQR 1.9898–2.5702; stop/ATR120 median 2.3555, IQR 2.0078–2.7536. Stop/pullback P75 median 0.7865, IQR 0.5993–1.0442. The current stop is not uniform relative to multiday movement.

2. TYPE1 is 415/659 (62.97%): no original-entry +0.25R touch within 24h. This is descriptive failed recovery, not proof of a causally bad entry.

3. +0.75R recovery: 89/659 (13.51%) by12h, 159/659 (24.13%) by24h. +1R: 66/659 (10.02%) by12h, 130/659 (19.73%) by24h. TYPE2 and TYPE3 overlap in73 cases; they must not be summed as disjoint classes.

4. There is no clear monotonic narrow-stop/noise relationship. For stop/ATR72 buckets 1.5–2, 2–2.5, 2.5–3, TYPE2 rates are 11.49%,13.03%,15.54%. For stop/pullback below0.75, 0.75–1 and1–1.25, they are11.18%,19.64%,13.33%. The lowest ATR bucket has only11 observations, higher pullback buckets are sparse. This does not justify a threshold.

5. A/B have lower pooled distance-percentage dispersion (CV0.345/0.326 versus control0.387), but are narrower for1078/1115 entries. B is the smoothest distance-only candidate, not a demonstrated better stop. C is effectively unchanged (only24 wider). D is wider for1091 entries, but increases dispersion(CV0.485). No production formula is selected.

### Candidate distances only

| Candidate | Median distance % | Median ratio/control | Wider | Narrower | Equal | CV of distance % |
|---|---:|---:|---:|---:|---:|---:|
| CONTROL | 4.3468 | 1.0000 | 0 | 0 | 1506 | 0.3874 |
| A | 3.8585 | 0.8882 | 428 | 1078 | 0 | 0.3453 |
| B | 3.7231 | 0.8580 | 391 | 1115 | 0 | 0.3261 |
| C | 4.3545 | 1.0000 | 24 | 0 | 1482 | 0.3868 |
| D | 5.7788 | 1.2858 | 1091 | 0 | 415 | 0.4848 |

### Recovery touch counts

| Horizon | Complete windows | +0.25R | +0.5R | +0.75R | +1R | +2R |
|---|---:|---:|---:|---:|---:|---:|
| 1h | 659 | 11 | 7 | 2 | 0 | 0 |
| 3h | 659 | 41 | 24 | 13 | 8 | 0 |
| 6h | 659 | 86 | 58 | 41 | 27 | 5 |
| 12h | 659 | 151 | 110 | 89 | 66 | 26 |
| 24h | 659 | 244 | 200 | 159 | 130 | 58 |
| 48h | 657 | 317 | 276 | 243 | 204 | 115 |

At1–24h all659 windows are complete. At48h657 are complete; CKBUSDT(exit2024-02-28T17:32Z,1052 missing minutes) and JASMYUSDT(exit2024-07-30T01:31Z,91 missing minutes) are partial. Their observed hits remain evidence, but unknown non-hits are not counted as failures.

### Quarter robustness (entry UTC)

| Quarter | Entries | Original stops | TYPE1 % | TYPE2 % | TYPE3 % | Median stop/ATR72 |
|---|---:|---:|---:|---:|---:|---:|
| Q1 | 481 | 202 | 53.47 | 15.35 | 20.79 | 2.295 |
| Q2 | 260 | 128 | 71.88 | 10.94 | 12.50 | 2.294 |
| Q3 | 337 | 145 | 65.52 | 13.79 | 23.45 | 2.283 |
| Q4 | 428 | 184 | 65.22 | 13.04 | 20.65 | 2.250 |

### Monthly spotlight

| Month | Entries | Original stops | TYPE1 % | TYPE2 % | TYPE3 % |
|---|---:|---:|---:|---:|---:|
| 2024-01 | 113 | 57 | 68.42 | 14.04 | 21.05 |
| 2024-04 | 73 | 44 | 79.55 | 11.36 | 11.36 |
| 2024-09 | 134 | 49 | 61.22 | 16.33 | 28.57 |
| 2024-11 | 189 | 66 | 50.00 | 19.70 | 30.30 |

All12 months, detailed feature/outcome distributions, fixed buckets, cohort features and per-symbol distance dispersion are in the compact export. Individual original trades, source bounds, candidate distances and separated recovery observations are in the detailed export.

## Validation and exports

14 new deterministic regression cases; full maintained suite:535 passed in91.82s. Maintained Ruff scope (`crypto_bot tests research`) passes. Tests cover future-data mutation, existing Wilder parity, denominators, fresh/contiguous windows, canonical fill audit, original risk targets, partial data, overlapping labels, window boundaries, exclusion of exited minute, immutable original trade objects and descriptive histogram accounting.

Changed files are only this document, `research/initial_risk_distance.py`, and `tests/test_initial_risk_distance.py`. Production HEAD5ea5d09 and scanner/paper/web PIDs1333/1334/31948 stay unchanged. No restart, migration, reset or production deployment is required.

Exports:

```text
/home/ndvi/crypto-bot/artifacts/initial-risk-distance-2024/initial-risk-distance-diagnostic.compact.json
/home/ndvi/crypto-bot/artifacts/initial-risk-distance-2024/initial-risk-distance-diagnostic.detailed.json
/home/ndvi/crypto-bot/artifacts/initial-risk-distance-2024/validation.json
```
