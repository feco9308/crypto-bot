# Funding + Premium / Basis Diagnostic v1

Read-only **2024 + 2025 EXPLORATION**, based exactly on
`f679c290d28a6f57782a267473363304548254f3`. No strategy, ranking, filter, score,
ML, replay execution, parameter optimization or deployment is part of this task.
Expected association is frozen **NEGATIVE**: higher positive funding / premium /
basis should predict weaker subsequent spot long opportunity. An unexpected
positive association can never qualify as a hypothesis candidate.

## Inputs and isolation

The production checkout and runtime services are untouched. Work takes place in
an isolated worktree on `research/funding-premium-v1`. Only new research scripts,
tests and this document are added. No production module is modified.

Existing, manifest-verified inputs:

* `artifacts/cross-sectional-market-structure-v1/`: validated spot
  `POST_HOC_FUTURE_LABEL_1H` outcomes.
* `artifacts/futures-taker-flow-v1/`: exact canonical rows, unchanged outcome and
  dependency fields, and the existing taker_buy_share_4h control.
* `artifacts/derivatives-data-availability-v1/`: exact archive instrument IDs,
  official archive inventory and checksum-validated evidence samples.
* `data/research-cache/futures-usdm-1h-v1/`: pinned traded futures 1h archives.

The original cohorts are preserved: 2024 has 175,680 ALL / 4,915 BUY rows;
2025 has 160,320 ALL / 2,069 BUY rows and starts on **2025-02-01**, matching the
existing validation source. Both cohorts are exploration for this diagnostic.
There is no 2023 research cohort and no 2026 input. December 2023 archives are
only pre-start history for the first 2024 decision windows.

Exactly identical symbol IDs are required. PEPE/BONK are not mapped to multiplier
contracts. Exact mapping is not evidence of active trading: every feature also
requires the actual immediate previous completed traded futures 1h candle at
that decision. Funding, premium, mark or index records cannot extend traded
market coverage. Activity is computed from the prior bar at each timestamp,
never from knowledge of the eventual last archive candle.

## Frozen features

| Feature | Definition |
|---|---|
| funding_rate_last | Last event with original integer `fundingTime <= t` and `fundingTime > t-24h` |
| funding_sum_24h | Sum of all actual rates in `(t-24h,t]`; at least one event; no assumed cadence |
| premium_close_1h | Close of the immediate previous completed Premium Index 1h candle |
| premium_mean_24h | Arithmetic mean of exactly 24 contiguous completed premium closes |
| mark_index_basis_1h | `(mark_close/index_close)-1`, exact same previous hour, positive index |

Candle `close_time < t` is mandatory. A funding event at `t+1ms` is future and is
excluded, even if it visually belongs to the same hour. Event count, availability
reason and source timestamps are metadata only. No derived funding/volume/flow
interaction or additional predictive feature is produced. Zero is a valid actual
observation; missing data stays unavailable.

## Capture and offline analysis

Only official monthly USD-M fundingRate and 1h premiumIndexKlines,
markPriceKlines and indexPriceKlines archives are downloaded. Each complete file
is checked against its official `.CHECKSUM`, pinned by SHA256, parsed and
validated. Evidence records URL, exact key, symbol, interval, bytes, timestamps
and row count. Cache is outside Git and excluded from the result ZIP.

Network is opt-in, bounded to approved archive GET URLs, three workers, no
redirects and no retry loop. A 418/429/503 stops download. No API key, account,
order endpoint, OI, long/short, aggTrades or trade archives are used.

```bash
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python -m research.funding_premium \
  --root /home/ndvi/crypto-bot --download --capture-only

# Offline: verify pinned sources/cache, gate coverage, then analyze eligible features.
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python -m research.funding_premium \
  --root /home/ndvi/crypto-bot

# Independent direct raw-CSV export QA and deterministic packaging.
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python -m research.funding_premium_validate \
  --root /home/ndvi/crypto-bot
```

A cached COMPLETE object is verified and reused, never unnecessarily refreshed.
Absent archive is missing, not zero. Checksum-valid old evidence is reused only
when its full sample validation pin is available; alignment-only evidence is
not silently promoted to a checksum proof.

## Coverage and selection before prediction

Each feature must independently have >=80% exact row coverage in all four
2024/2025 ALL/BUY cells. If none qualify the result is COVERAGE_STOP. Features
that fail the gate do not receive predictive statistics. Source-bar coverage,
feature coverage, symbol coverage and symbol-day coverage are kept distinct.

Before predictive statistics, all five features get unweighted AVAILABLE vs
UNAVAILABLE comparisons for 72h/168h spot MFE ATR and close return: total and
paired counts, medians and Cliff delta. No weighting, correction or imputation.
Mapping-induced selection effects are explicitly retained.

## Statistical contract

Spot labels are reused, not regenerated. Existing symbol-day and BUY setup
first flags are inherited; no new episode definition is introduced. Average
tie ranks define deterministic ten deciles and fixed top/bottom quintiles, as in
the previous validated diagnostic. Empty bins are retained. All requested median
outcomes, monotonicity summaries, Spearman, median differences, Mann-Whitney AUC
and Cliff delta are exported.

The shared pure descriptive helpers in `funding_premium_stats.py` are copied
from validated research at `26351dde5e09017c822f931b9afd1101380390bd`, without
bringing that research branch or its production changes into the base. They do
not construct outcomes or run engines. This provenance is explicit to make the
new base self-contained and keep the earlier statistical conventions.

Candidate evidence requires both years and both 72h/168h ALL MFE outcomes to
have Cliff <= -0.147 and Spearman <= -0.05, with >=30 observations in each extreme
group. BUY, symbol-day and BUY episode-first must not reverse either direction.
Leave-one-symbol and leave-one UTC calendar quarter use the **original full-pool
quintile membership**, require adequate remaining counts and retain the direction
and at least 50% of the signed original effect. No rebinning or cutoff search.

Selection ambiguity is handled conservatively and declared before results:
when AVAILABLE/UNAVAILABLE MFE has |Cliff| >=0.147 with >=30 observations in both
groups, candidate promotion is vetoed. This does not correct the association or
create a trading threshold; the affected cells remain visible for interpretation.
Secondary existing 1m +5R-before-stop and stop-before-1R labels can veto a primary
candidate on a strong contrary association, but cannot create a candidate or drive
the primary category. At most one candidate is reported, by frozen minimum ALL
absolute effect and exact name tie-break. No implementation follows qualification.

The three spot controls are recomputed on each feature's exact common finite
row pool. A second pool includes the existing taker 4h control when available;
target and all controls in each pool have identical input and paired outcome
hashes. This is univariate comparison, not a claim of conditional predictive edge.

## Outputs and safety

Output directory: `/home/ndvi/crypto-bot/artifacts/funding-premium-v1/`.
Required CSV/JSON/Markdown artifacts and deterministic `funding-premium-v1.zip`
include coverage, selection, statistics, controls, dependency/concentration,
secondary labels, post-hoc FET/ETH/PEOPLE examples, source and export validation,
and the complete archive manifest. Raw ZIP caches are excluded.

The independent validator reparses raw archives, checks every exported feature
and inherited field, verifies no duplicate canonical rows, checks matching
control hashes, and repackages twice to require bit-identical ZIP bytes. An extra
FRONT coverage audit is post-hoc source inspection only; its last-coverage times
are never feature inputs.

All production SQLite inspection uses `mode=ro` plus `PRAGMA query_only=ON`.
Logical fingerprints cover both completed source runs and the entire run-ID
inventory before/after analysis. Live service heartbeat changes are not confused
with research writes. Source artifact hashes must remain unchanged.

NO LIVE TRADING. No replay, no database migration, no production DB write, no
service control and no deployment.

## Validation results

* Targeted correctness / safety tests: **58 passed**.
* Full maintained suite: **841 passed in 239.07 seconds**.
* Maintained Ruff (`ruff check crypto_bot tests research`): **PASS**.
* `git diff --check`: **PASS**.

The full suite uses the existing interpreter with `PYTHONPATH=.` pointing at the
isolated worktree, plus the existing browser library path. Test fixtures are
isolated. No historical replay job is submitted by this diagnostic.

## Recovery from completed dataset artifacts

The disconnected attempt left both complete datasets, exact coverage, selection
analysis and all pinned cache objects. No process remained and no final summary
or report had been produced. Resume begins at statistics, not at capture:

```bash
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python -m research.funding_premium \
  --root /home/ndvi/crypto-bot --resume-existing
```

This mode rejects `--download` and `--capture-only`, verifies unchanged source
hashes and frozen methodology, loads the existing dataset bytes and verifies
cohort counts / duplicate keys. It requires byte-identical recomputed coverage
and selection output without overwriting those completed artifacts. No feature
is rebuilt and no cache object is fetched. Existing cache pins were independently
rehashed and raw-parsed during recovery, and final export QA rechecks all feature
values directly against those archives. This is orchestration support only;
feature definitions, NEGATIVE direction and all thresholds remain unchanged.

## Completed research results

Category **B — FUNDING_PREMIUM_SIGNAL_WEAK_OR_UNSTABLE**; candidate **NONE**.
Expected direction remains **NEGATIVE**. All five features pass exact coverage,
but all five fail the main two-year ALL evidence screen independently of selection
flags. The 2025 BUY funding sum has a local negative association, not reproduced
in 2024 BUY or 2025 ALL. No strategy follows from these descriptive results.

| Feature | 2024 ALL coverage | 2024 BUY | 2025 ALL | 2025 BUY |
|---|---:|---:|---:|---:|
| funding_rate_last | 87.2746% | 85.3306% | 93.5635% | 95.9884% |
| funding_sum_24h | 87.2746% | 85.3306% | 93.5635% | 95.9884% |
| premium_close_1h | 86.9467% | 84.7813% | 93.5099% | 95.9884% |
| premium_mean_24h | 86.7657% | 84.6999% | 93.4506% | 95.9884% |
| mark_index_basis_1h | 87.2820% | 85.3306% | 93.5909% | 95.9884% |

Pairs below are **72h / 168h MFE ATR**.

| Feature | Year | Population | Cliff | Spearman |
|---|---:|---|---:|---:|
| funding_rate_last | 2024 | ALL_TOP20 | +0.0077 / -0.0257 | +0.0051 / -0.0070 |
| funding_rate_last | 2024 | BUY_ELIGIBLE | +0.0771 / +0.0684 | +0.0423 / +0.0350 |
| funding_rate_last | 2025 | ALL_TOP20 | +0.0942 / +0.0947 | +0.0457 / +0.0438 |
| funding_rate_last | 2025 | BUY_ELIGIBLE | +0.0047 / +0.2611 | -0.0595 / -0.0918 |
| funding_sum_24h | 2024 | ALL_TOP20 | -0.0042 / -0.0428 | -0.0137 / -0.0366 |
| funding_sum_24h | 2024 | BUY_ELIGIBLE | +0.0730 / +0.0524 | +0.0399 / +0.0268 |
| funding_sum_24h | 2025 | ALL_TOP20 | +0.0682 / +0.0755 | +0.0346 / +0.0346 |
| funding_sum_24h | 2025 | BUY_ELIGIBLE | -0.1351 / -0.1995 | -0.1078 / -0.1316 |
| premium_close_1h | 2024 | ALL_TOP20 | -0.0038 / -0.0357 | -0.0165 / -0.0388 |
| premium_close_1h | 2024 | BUY_ELIGIBLE | +0.0194 / +0.0130 | +0.0202 / -0.0016 |
| premium_close_1h | 2025 | ALL_TOP20 | +0.0632 / +0.0637 | +0.0345 / +0.0330 |
| premium_close_1h | 2025 | BUY_ELIGIBLE | +0.0829 / +0.0529 | +0.0263 / +0.0212 |
| premium_mean_24h | 2024 | ALL_TOP20 | +0.0043 / -0.0364 | -0.0228 / -0.0459 |
| premium_mean_24h | 2024 | BUY_ELIGIBLE | +0.0203 / +0.0040 | +0.0259 / +0.0077 |
| premium_mean_24h | 2025 | ALL_TOP20 | +0.0575 / +0.0745 | +0.0329 / +0.0368 |
| premium_mean_24h | 2025 | BUY_ELIGIBLE | -0.0151 / -0.0406 | -0.0432 / -0.0507 |
| mark_index_basis_1h | 2024 | ALL_TOP20 | +0.0005 / -0.0316 | -0.0161 / -0.0381 |
| mark_index_basis_1h | 2024 | BUY_ELIGIBLE | +0.0746 / +0.0475 | +0.0404 / +0.0100 |
| mark_index_basis_1h | 2025 | ALL_TOP20 | +0.0852 / +0.0929 | +0.0496 / +0.0514 |
| mark_index_basis_1h | 2025 | BUY_ELIGIBLE | +0.0070 / -0.0361 | +0.0108 / -0.0100 |

2025 BUY selection is substantial for every feature: available/unavailable MFE
Cliff +0.27850/+0.44266, with 83 missing exact-mapping observations (BONK 56, PEPE 27).
No correction or multiplier mapping is applied. Funding-rate-last 2025 BUY has
only 29 observations in the top tied-rank group; its positive 168h Cliff is not
monotonic NEGATIVE evidence and falls below the minimum extreme-group count.

The local 2025 BUY funding-sum effect survives all 44 symbol and all 4 quarter
removals at both horizons, but fails the cross-year/ALL requirements. Existing
1m +5R labels support that local negative direction (AUC 0.38396, Cliff -0.23208);
secondary evidence cannot create a primary candidate.

See the completed artifact report for dependency, fixed-membership leave-group,
matched-control, secondary and winner tables. Independent raw-CSV QA confirms
all 336,000 rows without a feature/label mismatch. FRONT safety checks distinguish
required cohort-cache boundaries from global instrument coverage and use
checksum-validated prior samples to prove stale funding cannot activate a market.

Final exports are in `/home/ndvi/crypto-bot/artifacts/funding-premium-v1/`,
including `report.md`, `summary.json`, `export-validation.json`, and
`funding-premium-v1.zip`. Raw archive caches are not committed or bundled.
