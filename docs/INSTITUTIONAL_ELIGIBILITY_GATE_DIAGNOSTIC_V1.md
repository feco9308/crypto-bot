# Institutional Eligibility Gate Diagnostic v1

Research base: `f84d00f3282ee973e2bbedd5c0552a2d07174899`.
This is a read-only exploration diagnostic, not a new strategy, ranking, filter,
threshold search, capacity/replacement rule, portfolio replay or deployment.
Production runtime and the source replay results are unchanged.

## Immutable sources

2024: completed run `31423539f64e4650840df5b6b3fe9a26`, canonical Top20 archive
revision and isolated scanner context; source commit
`75998cae198f09f7be33bf5777f048b9653d7f81`. Every prior S ranked BUY candidate
must be found and match its decision-time score, deltas, RSI, ATR, relative volume,
range, entry and stop references. The new asset-only BUY population is a superset,
because existing holdings, portfolio correlation, capacity and risk are removed.

2025: existing run `f9d41057d61e4714b61df69d320620e0`, A baseline,
`524125eeaf1b2e87d3957b915810b55db7972ff8`. Its prior Signal Edge source files
are manifest-verified; normalized A configuration/code comparability is checked
again. Both years are **EXPLORATION**, even though the old 2025 run was originally
named VALIDATION. No new validation claim or replay is created.

Periods remain the source periods: 2024-01-01 to 2025-01-01, and 2025-02-01 to
2026-01-01, end-exclusive UTC. Source-required pre-start history only seeds
indicators. There is no 2023 research cohort and no 2026 archive input.

## Authoritative eligibility

The unchanged `InstitutionalStrategy.evaluate(context, has_position=False)`
produces each action and its exact reasons. The unchanged institutional context
receives an empty positions list, so portfolio-dependent correlation has no pairs
and cannot block. There is no RiskManager, portfolio service, order/fill generation,
worker action, entry filter or replay-engine invocation.

Every source-defined fresh historical Top20 row is evaluated once per simulated
UTC hour. The unchanged pure universe/frame/scoring/watch functions reconstruct
the source's canonical scanner seed and shared score history. No held-only or
companion exit row is ever added. Source code for universe, market frame, watch
selection and score momentum was additionally checked unchanged between the
2024 and 2025 source commits.

Observed predicate failures must match the evaluator's original reasons exactly:

| Gate | Existing semantics |
|---|---|
| SCORE | score >= configured minimum, 70 |
| DELTA | missing fails when required; any present delta <= configured minimum also fails; here delta4h > 0 |
| WATCH | require_watch satisfied by the existing algorithm watch membership |
| RSI | one gate, inclusive 40 <= RSI <= 70 |
| RELATIVE_VOLUME | relative_volume >= 1 |
| 1H/4H/1D_TREND | when required, ready complete timeframe and EMA50 > EMA200 |
| SPREAD | missing is allowed by source; a present value must be <= 0.2 |
| BTC_REGIME | when enabled, known institutional regime and nonzero multiplier |
| INITIAL_STOP | ATR > 0 and the original ATR-stop expression is positive |
| USER_IGNORE | source IGNORE blocks, though canonical replay has no manual override rows |

RSI_LOW/HIGH are descriptive subdivisions of `RSI_OUTSIDE_RANGE`, not two new
independent gates. Institutional-context availability is a descriptor, not an
extra gate. Trend unavailability and a known non-bullish trend preserve their
separate original reasons, while belonging to the same production timeframe gate.
Missing delta is identified from `delta_available`, not the reason text: production
uses `SCORE_DELTA_NOT_POSITIVE` for both missing and present nonpositive values.

`failure_count` counts failed production gates. A single-failure observation must
pass every other asset-level gate. Multi-failure rows are never relabeled single.
Original strategy reasons and a sorted canonical reason combination are exported.

## Watch is DEPENDENT_GATE

The unchanged selector considers auto-eligible rows with score >= 50, relative
volume >= 0.5 and ATR% in [0.1, 10], then takes Top5 by:

`score + 0.5 * max(0, delta4h)`

Symbol is the deterministic tie break. These are the existing configuration
values, not tested alternatives. Watch therefore depends mechanically on score,
delta, volume, volatility and cross-symbol competition; it is not an independent
causal feature. Institutional trend only requires fast EMA above slow EMA; an
additional price-above-EMA predicate is not invented.

BTC uses the source institutional **4h + 1d** BULL/NEUTRAL/BEAR/UNKNOWN definition,
not the unrelated legacy hourly reporting tag. NEUTRAL's configured multiplier
0.5 is nonzero and passes asset eligibility. No position sizing is performed.

## Decision-time and future-label separation

Every candle/coin/BTC trend source timestamp is strictly before the decision.
The canonical scanner window and score-history membership are unchanged.
Research memoization aligns trend queries to the last completed UTC bucket;
direct-context equality, partial 4h/daily handling and future-data perturbation
are tested against the unchanged source implementation.

For every row with legitimate positive ATR-stop construction, the diagnostic
reference uses the **same Decimal price - ATR * stop_atr_multiplier expression**.
For BUY it must equal the evaluator's actual stop reference. For HOLD this is
explicitly a diagnostic reference, not a stop emitted by the strategy or an order.
This applies to all evaluable groups so the ALL population is not artificially
restricted to BUY/single-failure cases. Invalid construction makes R labels
unavailable; no substitute stop is invented.

The exact previous `institutional_signal_edge.FutureLabels` implementation is
reused. Every future field has the `POST_HOC_FUTURE_LABEL` prefix. Price R is not
net trade R. Complete 6/24/72/168h MFE/MAE and fixed 1/2/5/10/20R barriers use
source-pinned minute candles only. The open is observed first; otherwise stop wins
same-minute ambiguity. First missing minute or source end censors observation.
UNKNOWN is not FALSE. No hourly fallback, unpinned cache object or download.

## Dependency and descriptive comparisons

An eligibility episode is consecutive same-symbol hourly Top20 observations with
the same final eligibility boolean. A gap or eligibility change starts a new one.
A change of HOLD reasons within a continuous HOLD state does not. Every raw hourly
row remains exported, with symbol-day and episode identifiers. Episode-first is
sensitivity analysis, never a fresh-setup admission rule or an outcome-selected
best row.

Reports compare ALL / current eligible / not eligible / multi-failure, each
single-failure gate, and each gate's pass/fail groups. Availability-only and
known-failure subgroups remain separate. Score distributions use only the fixed
production boundary; no alternative cutoff is tried.

Cliff's delta and distribution AUC describe complete 72h MFE distributions. A
separate binary AUC describes pass/eligibility as an indicator for the disjoint
+5R-before-stop and stop-before-1R groups. It is not a trained model. Gate pass/fail
comparisons contain co-failure confounding; single-failure comparisons are still
observational, not causal estimates.

Frozen descriptive conventions, specified before inspecting outcome comparisons:

- At least 30 observed +5R, stop and complete 72h MFE observations in each group.
- Helpful: current eligible minus ONLY_FAIL has +5R difference >= 2 percentage
  points, stop difference <= -2 points and Cliff72 >= 0.147 in both years and both
  raw/episode-first scopes. Harmful is the inverse; otherwise unclear.
- Balanced coverage requires known +5R, stop and complete 72h MFE coverage >= 80%
  in both groups and a coverage gap <= 10 percentage points.
- A later frozen ablation hypothesis additionally requires balanced coverage,
  no unavailable-data-only single failures, and exactly one qualifying harmful
  gate. Zero or multiple qualifying gates gives `NONE`. No ablation is implemented.
- Overall category A requires consistently helpful enrichment with balanced
  coverage. C requires consistently little enrichment with balanced coverage;
  otherwise B (mixed or coverage-limited).

These are transparent descriptive evidence conventions, not optimized strategy
thresholds. Counts and actual effects are exported regardless of classification.
The source's pinned minute coverage follows historical wanted symbols, so missing
labels can be selection-dependent. It cannot be silently treated as random or as
negative outcomes. Repeated hourly rows and overlapping windows are dependent.

## Running and verification

```sh
PYTHONPATH=. nice -n 10 /home/ndvi/crypto-bot/.venv/bin/python \
  -m research.institutional_eligibility_gate --root /home/ndvi/crypto-bot
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python -m pytest -q \
  tests/test_institutional_eligibility_gate.py
PYTHONPATH=. LD_LIBRARY_PATH=/home/ndvi/.local/lib/crypto-bot-browser/usr/lib/x86_64-linux-gnu \
  /home/ndvi/crypto-bot/.venv/bin/python -m pytest -q
/home/ndvi/crypto-bot/.venv/bin/ruff check crypto_bot tests research
git diff --check
```

Only the research artifact directory is written. All source SQLite uses URI
`mode=ro` and `PRAGMA query_only=ON`. A process audit hook denies network/DNS.
Source-run table fingerprints and the current complete run-ID inventory are
measured before/after; prior unrelated inventory is not treated as source data.
Pinned metadata and every used archive SHA256 are checked again at completion.

Per-symbol deterministic gzip feature files permit resuming this **diagnostic**
only, with source fingerprint, transformation signature and file SHA checks.
They are not replay checkpoints. `--reports-only` reaggregates manifest-verified
exported CSVs without any source DB/archive access, strategy evaluation or new
labels. It does not create or resume any replay. Production/base source code and strategy parameters are
unchanged; feature transformations are fingerprinted. Source trade/score/risk/paper data is never written.

Outputs live under `artifacts/institutional-eligibility-gate-diagnostic-v1/` and
include all requested CSVs, summary/methodology/source validation, manifest and
`institutional-eligibility-gate-diagnostic-v1.zip`. ZIP timestamps, member ordering
and permissions are fixed; file hashes are included. Migration and deployment
are not required. GitHub publishing is a separate authorized operation outside
the offline research process.

## Completed exploration findings

The source checks pass: 175,680 opportunities in 8,784 hourly slots for 2024,
and 160,320 in 8,016 slots for 2025. Every slot has unique ranks 1–20. Every row
has closed decision/HTF sources and empty portfolio correlation. The final feature
transformation signature matches both extraction stages.

All 2,999 prior 2024 S and 959 prior 2025 A candidates match reconstructed BUY
eligibility/features. Their barrier labels match exactly; 2024 MFE/MAE also match
exactly. 2025's prior float-inferred stop versus the production Decimal expression
has maximum MFE/MAE difference 9.38e-13 R, below the fixed 1e-9 comparison tolerance.
No source record or prior diagnostic dataset was changed.

| Year / population | Rows | Observed +5R rate | Known +5R coverage | Stop before +1R | Median 72h MFE R |
|---|---:|---:|---:|---:|---:|
| 2024 ALL Top20 | 175680 | 17.0521% | 57.4488% | 50.6854% | 1.74973 |
| 2024 current BUY eligible | 4915 | 14.3649% | 99.1455% | 52.3431% | 1.68015 |
| 2025 ALL Top20 | 160320 | 20.3796% | 37.2362% | 48.8660% | 1.86262 |
| 2025 current BUY eligible | 2069 | 15.6265% | 99.9033% | 51.9594% | 1.78877 |

Current eligibility does not show useful enrichment in the **observed pinned-data
subset**: raw +5R difference is -2.6872 / -4.7531 percentage points (2024 / 2025),
with stop-rate difference +1.6578 / +3.0933 points. The 72h MFE Cliff effects are
small, -0.02267 / -0.02831. Episode-first +5R differences shrink to -0.4072 /
-0.7085 points. Coverage is strongly unbalanced, so these numbers do not establish
that the gate layer harms the complete Top20 population. Missing outcomes remain
UNKNOWN; the source's wanted-symbol archive selection is not random.

Every production gate is classified **GATE_VALUE_UNCLEAR** under the frozen
two-year/raw/episode/sample criteria: SCORE, DELTA, WATCH, RSI, RELATIVE_VOLUME,
1H_TREND, 4H_TREND, 1D_TREND, SPREAD, BTC_REGIME, INITIAL_STOP and USER_IGNORE.
This does not mean all effects are zero; some are weak, contradictory, sparse,
confounded by availability, or unsupported under episode-first sensitivity.

- SCORE: ONLY_SCORE_FAIL has +5R rates 15.27% / 22.74%, but 72h MFE effects are
  tiny (-0.00368 / -0.04768), and stop-rate episode sensitivity is inconsistent.
  No cutoff search and no inference from the previous BUY-only AUC were used.
- WATCH: current-eligible vs ONLY_WATCH_FAIL +5R difference flips from +2.32 points
  in 2024 to -3.86 points in 2025. It remains explicitly DEPENDENT_GATE.
- RELATIVE_VOLUME: 72h MFE differences are small; no stable nontrivial benefit.
- RSI: ONLY_RSI_HIGH_FAIL can have a higher target rate, but other measures do not
  consistently establish a nontrivial harmful gate effect. ONLY_RSI_LOW_FAIL has
  only 23 / 18 raw observations. RSI remains one gate, no new boundary tested.
- 1D_TREND: largest notable inverse raw 2024 comparison (-16.40 points in eligible
  minus ONLY_FAIL +5R rate, Cliff -0.18563), weaker in 2025 (Cliff -0.13247).
  Episode-first ONLY_1D_TREND_FAIL has only 19 / 22 rows, with still fewer observed
  labels; single failures also include 817 / 255 unavailable-context rows in the
  raw groups. No whole-gate ablation can be nominated from this evidence.
- 1H/4H trend episode-first single-failure counts are only 3/1 and 3/5 respectively.
- SPREAD, INITIAL_STOP and USER_IGNORE have no single-failure examples; BTC_REGIME
  lacks a supported two-year episode-first single-failure sample.

Strongest qualifying helpful gate: **NONE**.
Strongest qualifying potentially harmful gate: **NONE**.
`ablation_candidate_gate`: **NONE**.
Final category: **CURRENT_ELIGIBILITY_MIXED (B)**, with material coverage limits.
No strategy/ranking/filter/threshold/ablation was implemented or promoted.

## Final verification

- Targeted tests: **42 passed**.
- Full maintained suite: **847 passed**, 230.25 seconds.
- Maintained Ruff and diff whitespace checks: **PASS**.
- Source-run logical fingerprints and run inventory: unchanged; source cache
  metadata and every used archive SHA256 verified before/after use.
- The full source population, partitions, uniqueness, 1–20 ranks, feature clocks
  and known-candidate label parity are independently checked in
  `final-export-validation.json`.
- Production runtime files changed: **0**. Production checkout remains at
  `226facef1f68f967fc1d7b1bb75e282849185a56`.
- New replay: **NO**. Database writes: **0**. Deployment: **NO**. Migration: **NO**.

The final ZIP has 16 members, 143,797,024 bytes. All member sizes, SHA256 values,
CRC checks and sorted ordering were verified. ZIP SHA256:
`9a1475858274cfa70a1db4a36f59d72c79695544b267673be25ae8dbfc5ecba0`.
The original 25-run production inventory remains unchanged.
