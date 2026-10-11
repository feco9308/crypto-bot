# Dynamic Initial R / Stop Shadow Research — 2024 EXPLORATION

Anchor: `b9275979397440f2aabbe4a89e4f9cc4`, variant0, 1506 actual FILLED BUYs.
This standalone matched-entry experiment is **not portfolio replay**. Original
entry times, fills, quantities, fees, signals, regime and ATR14 remain frozen.
Production strategy, risk, wallet, score, paper, registry and execution code are
unchanged. No production service restart or deployment is required.

## Predeclared distances and precise price semantics

The same completed initial-risk-distance export provides ATR14/72/120 and the
fixed 24-hour peak-to-close pullback P75 over72 samples. Its file SHA256 and
original anchor trades are checked before processing.

- CONTROL:2ATR14.
- A:2ATR72.
- B:2ATR120.
- C:max(2ATR14,1.5ATR72).
- D:max(2ATR14,pullback P75% * original strategy reference/100).

Stop price is original strategy reference minus candidate distance. Decimal
arithmetic is retained, with **no rounding of the stop**, exactly as the original
strategy. It is never changed to an entry-fill price basis. Tiny floating-point
representation differences in the descriptive diagnostic distances are avoided
by applying the same Decimal strategy arithmetic to its stored ATR inputs.

The actual persisted BUY execution reference is distinct from that strategy
reference and is read from the original FILLED BUY audit. Using unchanged
RiskManager arithmetic:

```
entry = execution_reference * (1 + slippage)
exit_fill = candidate_stop * (1 - slippage)
loss_per_unit = entry - exit_fill + (entry + exit_fill) * fee
candidate_initial_risk = q(canonical_quantity * loss_per_unit)
unit_R = candidate_initial_risk / canonical_quantity
```

Both CONTROL stop and risk amount must match exactly, for all1506 entries,
before candidate bars are processed. Initial R is fixed once per entry. Original
ATR14 still determines the .75ATR and .50ATR peak-trail distances. The only
changes are initial stop and R-scaled .75/1/2 activation thresholds.

## Execution, parity and performance

The completed early-protection experiment's unchanged conservative and
optimistic CONTROL outcomes are reused, preserving its canonical entry hash and
source DB fingerprint. Conservative parity is checked again, including stop,
risk, activations, exit time/price/stage/reason, realized PnL/R and costs.

All four candidate policies and both intrabar modes receive each cached minute
candle from **one read**. Existing `ShadowPosition` and `AdaptivePeakLock` execute
them without modification. No earlier exit is inferred from future data. No
missing candle, last known price, archive end or year end forces a fill. A gap
remains audited even if data resumes; incomplete pairs are explicitly excluded
from matched comparisons. Source SQLite and cache objects are read-only, SHA256
checked, and the original source cache is never modified. Missing unpinned archives are completed only via public GET into a separate research directory (see coverage audit below).

Per-entry atomic checkpoints are guarded by anchor/entry/diagnostic hashes and
formula identity. A resumed invocation reuses finished entries. Progress is
exported independently; a dedicated transient research service keeps the job
running after terminal closure without restarting production services.

## Hypothetical fixed-risk sizing audit

The original **actual canonical risk_amount**, including any original cap, is the
budget. Unconstrained hypothetical quantity uses the same cost-inclusive
loss-per-unit and12-decimal rounding. Immutable canonical portfolio-at-decision
views retain cash, reservations, marked exposure and replay stale-exposure
reservation. Existing `RiskManager.evaluate` is reused with a derived risk
percentage that expresses this fixed monetary budget. Its capped quantity and
rejection reasons are recorded alongside uncapped quantity, minimum order,
symbol/total exposure and available-cash constraints.

This audit never changes the matched entry set or executed shadow quantity.
It does not model the subsequent alternative portfolio path.

## Reporting interpretation

Existing flat tolerance is `abs(return_pct)<=0.000001`. Strict positive-R damage
also includes very small positive break-even residues. Outcome, activation,
stage, return, holding time and cost metrics retain explicit denominators.

The disjoint decomposition sums every comparable deltaR exactly: rescued
original stops; newly losing control winners; truncated winners; improved other
existing exits; remaining changes. In addition, we separate scaling from changed
cash exits:

```
deltaR = (candidatePnL-controlPnL)/controlRisk
       + candidatePnL*(1/candidateRisk - 1/controlRisk)
```

This prevents an apparent R improvement from being attributed entirely to
rescued trades. Independent matched-quantity PnL totals and per1000-entry-cost
returns are **not portfolio equity or attainable portfolio returns**.

Monthly/quarterly groups use entry UTC. Best two months mean the two largest
candidate-minus-control deltaR months; removing them from both sides is a
retrospective concentration diagnostic, never an optimized strategy choice.
A/B are split narrower/wider; D is split unchanged/widened and the fixed ratio
buckets1,(1,1.25),[1.25,1.5),[1.5,2),>=2. No formula or threshold search occurs.

Quantity ratio counts compare against the hypothetical CONTROL quantity using the same quantized actual risk budget. The raw ratio against canonical quantity is separately retained. This avoids treating risk-amount round-trip quantization as a candidate sizing change.

## Completed results

All five policies have1506 CLOSED complete outcomes in each mode: zero OPEN, STALE or INCOMPLETE. Entry time/fill/quantity/cost audit and actual FILLED BUY identity remain unchanged. CONTROL parity is0 mismatches, including original stop and cost-inclusive unitR.

### Conservative primary metrics

| Policy | Win / Flat / Loss | Mean R | Median R | Total R | PF_R | Mean return % |
|---|---:|---:|---:|---:|---:|---:|
| CONTROL | 634/213/659 | -0.074516 | ~0 | -112.221326 | 0.829711 | -0.269786 |
| A | 635/201/670 | -0.074690 | ~0 | -112.482482 | 0.831869 | -0.295456 |
| B | 626/206/674 | -0.086239 | ~0 | -129.876441 | 0.807024 | -0.356382 |
| C | 636/212/658 | -0.072752 | ~0 | -109.564139 | 0.833491 | -0.266003 |
| D | 647/208/651 | -0.054803 | ~0 | -82.534004 | 0.873220 | -0.391669 |

| Policy | Original stop | BE .75R | Peak1R | Tight2R |
|---|---:|---:|---:|---:|
| CONTROL | 659 | 223 | 610 | 14 |
| A | 669 | 207 | 596 | 34 |
| B | 673 | 213 | 577 | 43 |
| C | 658 | 222 | 612 | 14 |
| D | 651 | 217 | 629 | 9 |

Stage is not the same as return category: an activated protection stop can gap into a loss. Tiny break-even residuals use the unchanged flat tolerance.

| Policy | .75R activation % | 1R activation % | 2R activation % | Mean hold hours | Fees | Slippage | Ambiguous |
|---|---:|---:|---:|---:|---:|---:|---:|
| CONTROL | 56.242 | 41.434 | 0.930 | 8.459 | 261.0378 | 130.5191 | 130 |
| A | 55.578 | 41.833 | 2.258 | 7.279 | 261.0577 | 130.5291 | 137 |
| B | 55.312 | 41.169 | 2.855 | 6.785 | 261.0115 | 130.5059 | 141 |
| C | 56.308 | 41.567 | 0.930 | 8.471 | 261.0488 | 130.5246 | 131 |
| D | 56.773 | 42.364 | 0.598 | 18.887 | 260.9161 | 130.4582 | 160 |

### Six decision questions

1. C and D improve matched-entry meanR/totalR/PF_R, but every policy still has negative expectancy and PF_R below1. Direct longer-ATR A is essentially flat/slightly worse in expectancy; B worsens it.

2. C gains2.6572R, split+2.2411R from changed cash exits and+0.4161R from denominator scaling. D gains29.6873R, split**-24.0475R cash-exit effect** and**+53.7348R scaling effect**. D rescues78 original stops(62 positive,16 flat), but damages359 strict positive-control trades(206 lower-R winners,83 flat,70 loss). This is not a pure cash-profit improvement; the fixed-quantity PnL sum worsens from-439.3997 to-560.9919. These are independent trade sums, not portfolio returns.

3. C and D improve all three R dimensions. A improvesPF_R slightly but decreasesmean/totalR. B decreasesall three.

4. D deltaR is positive in all four quarters; C is Q1-dependent. Quarter/month robustness concerns the candidate delta, not a claim that all quarter returns are profitable.

5. Removing the best two delta months leaves D only+0.4564R:98.46% of its advantage is in May/February. C falls to-0.1684R after January/March removal. Thus D survives arithmetically, with very weak residual advantage, while C does not. No robust production promotion is supported.

6. Fixed-risk audit caps99 A entries and104 B entries: symbol limits84/90, total-exposure limits28/31(overlap allowed). Available-cash/minimum-order constraints are0 in this fixed snapshot audit. C/D have0 caps, but that does not establish later portfolio feasibility.

### Pairwise delta and scaling decomposition

| Candidate | Delta totalR | Delta meanR | Delta PF_R | Cash-exit effect, anchorR | Denominator scalingR | Rescued stops |
|---|---:|---:|---:|---:|---:|---:|
| A | -0.261156 | -0.000173 | 0.002158 | 8.815982 | -9.077138 | 39 |
| B | -17.655115 | -0.011723 | -0.022687 | -5.344592 | -12.310523 | 48 |
| C | 2.657187 | 0.001764 | 0.003779 | 2.241110 | 0.416077 | 1 |
| D | 29.687322 | 0.019713 | 0.043508 | -24.047476 | 53.734797 | 78 |

All disjoint decomposition residuals reconcile to within1e-8R. The `truncated_winners` group means lower realizedR for non-losing control winners; it can include denominator effects and does not itself prove earlier exit timing. The separate cash/scaling decomposition resolves that distinction.

### Monthly / quarterly robustness

| Quarter | A deltaR | B deltaR | C deltaR | D deltaR |
|---|---:|---:|---:|---:|
| Q1 | -9.3219 | -12.4053 | 2.8256 | 2.3557 |
| Q2 | 0.4036 | -3.3309 | -0.0513 | 12.0254 |
| Q3 | 1.7975 | 1.8168 | 0.0000 | 13.7448 |
| Q4 | 6.8597 | -3.7357 | -0.1171 | 1.5615 |

| Candidate | Positive / negative delta months | Best two delta months | DeltaR without them |
|---|---:|---|---:|
| A | 6/6 | 2024-11, 2024-08 | -12.858261 |
| B | 4/8 | 2024-08, 2024-10 | -29.189695 |
| C | 2/4 | 2024-01, 2024-03 | -0.168380 |
| D | 7/5 | 2024-05, 2024-02 | 0.456445 |

| Month | A deltaR | B deltaR | C deltaR | D deltaR |
|---|---:|---:|---:|---:|
| 2024-01 | -0.8540 | -0.5436 | 1.7278 | 1.8252 |
| 2024-02 | -4.1358 | -4.3160 | 0.0000 | 11.1435 |
| 2024-03 | -4.3321 | -7.5457 | 1.0978 | -10.6130 |
| 2024-04 | 0.3085 | -1.5072 | -0.0513 | -4.3950 |
| 2024-05 | -1.0474 | -3.6584 | 0.0000 | 18.0874 |
| 2024-06 | 1.1425 | 1.8347 | -0.0000 | -1.6671 |
| 2024-07 | 0.3122 | 3.4669 | 0.0000 | 3.7734 |
| 2024-08 | 5.5018 | 7.8319 | -0.0000 | 4.9395 |
| 2024-09 | -4.0166 | -9.4820 | 0.0000 | 5.0319 |
| 2024-10 | 4.6637 | 3.7026 | -0.0764 | -1.3504 |
| 2024-11 | 7.0953 | -4.0234 | -0.0277 | 10.8863 |
| 2024-12 | -4.8994 | -3.4149 | -0.0130 | -7.9744 |

### Fixed-risk quantity and cap audit

| Policy | Median quantity ratio | P25 | P75 | Increased / decreased | Cap entries |
|---|---:|---:|---:|---:|---:|
| CONTROL | 1.0000 | 1.0000 | 1.0000 | 0/0 | 0 |
| A | 1.1144 | 0.9835 | 1.2512 | 1078/428 | 99 |
| B | 1.1511 | 0.9922 | 1.3304 | 1115/391 | 104 |
| C | 1.0000 | 1.0000 | 1.0000 | 0/24 | 0 |
| D | 0.7896 | 0.6126 | 1.0000 | 0/1091 | 0 |

### Initial R target distances

| Policy | Median unitR % | .75R % | 1R % | 2R % |
|---|---:|---:|---:|---:|
| CONTROL | 4.6319 | 3.4739 | 4.6319 | 9.2638 |
| A | 4.1530 | 3.1148 | 4.1530 | 8.3060 |
| B | 4.0184 | 3.0138 | 4.0184 | 8.0369 |
| C | 4.6332 | 3.4749 | 4.6332 | 9.2665 |
| D | 6.0546 | 4.5409 | 6.0546 | 12.1092 |

D has415 unchanged and1091 widened stops. Among widened entries, all1091 pairs are now complete. A has1078 narrower/428 wider, with deltaR-21.2198/+20.9587; B has1115 narrower/391 wider, with deltaR-31.8578/+14.2027. No narrower/wider subgroup was used to select a filter.

### D width buckets

| Bucket | N | DeltaR | Rescues | Damaged positive controls |
|---|---:|---:|---:|---:|
| unchanged | 415 | 0.0000 | 0 | 0 |
| (1,1.25) | 288 | 5.7057 | 10 | 113 |
| [1.25,1.5) | 268 | -1.8661 | 13 | 102 |
| [1.5,2) | 348 | 17.8006 | 34 | 97 |
| >=2 | 187 | 8.0471 | 21 | 47 |

### Intrabar sensitivity

| Candidate | Conservative deltaR | Optimistic deltaR |
|---|---:|---:|
| A | -0.2612 | -0.4044 |
| B | -17.6551 | -16.5028 |
| C | 2.6572 | 2.5082 |
| D | 29.6873 | 28.9263 |

C/D advantages exist in both modes; neither is optimistic-only. All costs/thresholds remain predeclared.

## Source coverage completion audit

The first offline-cache pass found UMAUSDT February–April and INJUSDT December absent from the manifest, **not confirmed missing market history**. Only D entry85 and1377 extended into these uncached periods. Its initial exports/checkpoints are preserved under `initial-cache-pass/` and `checkpoints/`.

The research-only reader fetched only the actually needed UMAUSDT February and INJUSDT December monthly1m ZIPs plus official CHECKSUM files through the existing restricted public GET client. All bytes/manifests live in `supplemental-archives/`, never the shared cache. Only D for these two entries was recomputed in both modes; all other15056 outcomes are byte-equivalent and canonical entries/control trades are unchanged. The replacement records live under `coverage-completion-checkpoints/`.

UMAUSDT D now exits on2024-02-01T01:16Z at ORIGINAL_STOP; INJUSDT D exits2024-12-01T05:59Z at BREAK_EVEN_075R. Neither is retrospectively closed at a last-known price. No unpinned cache absence is silently treated as proven archive unavailability in the final results. Existing pinned MISSING revisions remain immutable.

## Tests, code and operational checks

Full maintained suite:554 passed in92.81s, including19 new deterministic tests. Maintained Ruff scope (`crypto_bot tests research`) passes. New tests cover exact stop/risk parity, reference versus fill basis, frozen quantity/ATR, invalid-stop aborts, all four hypothetical cap types, fixed-risk round-trip rounding, shared candles, policy-order independence, missing data/no year-end fill, post-exit future exclusion, production registry isolation, cash/scaling reconciliation, public GET-only private archives and immutable pinned MISSING revisions.

Changed files: only `research/shadow_dynamic_initial_r.py`, `tests/test_shadow_dynamic_initial_r.py`, and this report. Production HEAD remains5ea5d09; scanner/paper/web PIDs1333/1334/31948 remain active and unchanged. No migration, reset, live execution, service restart or production promotion. Research services finish successfully.

Main computation used4min8.697s CPU, reported1.2G memory peak and0B swap peak; source completion/aggregation used5.621s CPU. Existing CONTROLs and completed candidates were reused.

Exports:

```text
/home/ndvi/crypto-bot/artifacts/shadow-dynamic-initial-r-2024/2024-B-shadow-dynamic-initial-r.compact.json
/home/ndvi/crypto-bot/artifacts/shadow-dynamic-initial-r-2024/2024-B-shadow-dynamic-initial-r.detailed.json
/home/ndvi/crypto-bot/artifacts/shadow-dynamic-initial-r-2024/validation.json
```
