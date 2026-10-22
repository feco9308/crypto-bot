# Futures Taker Flow Diagnostic v1

Branch: `research/futures-taker-flow-v1`.
Base: `bea87374a167fc27409c883a08707e92151db5ae`.

This is READ-ONLY EXPLORATION, not a strategy, ranking, filter, score, model or
replay. No production runtime file is changed; there is no deployment, migration,
worker control, authenticated API, order API or production database write.

## Inputs and execution

Canonical source: manifest-verified Cross-Sectional Market Structure Diagnostic
CSV rows, unchanged spot outcomes, BUY eligibility and original dependency flags.

| Cohort | UTC decision range | ALL Top20 | CURRENT_BUY_ELIGIBLE |
|---|---|---:|---:|
| 2024 EXPLORATION | 2024-01-01 to 2025-01-01 | 175,680 | 4,915 |
| 2025 EXPLORATION | 2025-02-01 to 2026-01-01 | 160,320 | 2,069 |

Exact identifiers are mapped only when present in the previously verified
Binance USD-M archive symbol inventory. No multiplier, alias or rebrand is guessed.
A directory match is not row coverage: actual required candles must exist and pass
validation. Source-required December 2023 bars can seed the first 2024 windows;
there is no 2023 research population or 2026 data.

```bash
# Explicit initial necessary official public 1h archive download:
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python -m research.futures_taker_flow --root /home/ndvi/crypto-bot --download

# Offline reproduction from immutable pinned inputs/cache; never auto-downloads:
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python -m research.futures_taker_flow --root /home/ndvi/crypto-bot
```

Only the necessary monthly USD-M **traded klines/1h** objects are requested. Each
canonical timestamp requires at most the months containing t-24h and t-1h.
Previously checksum-verified audit sample bytes are reused after verifying both
their evidence-file manifest and official-checksum sample pins. Pinned metadata
absence in the earlier audit is recorded as unavailable, not fabricated zero data.
Source warmup objects outside that audit's 2024/2025 list are fetched explicitly.

New cache: `data/research-cache/futures-usdm-1h-v1/` under the supplied root,
content-addressed `objects/<SHA256>.zip` and deterministic key metadata JSON.
The cache is excluded from Git and the artifact ZIP; the existing replay cache
is untouched. A URL is not an immutable version: every complete object is verified
against official SHA256 CHECKSUM and pinned. Cached bytes are rehashed on reuse
and at completion. The archive manifest contains URL/checksum URL, key, bytes,
symbol, interval, SHA, row count, first/last open and close timestamps and quality.
A revised archive cannot silently replace an existing pin.

Network is restricted to the exact official `data.binance.vision` monthly USD-M
1h ZIP and CHECKSUM paths. No aggTrades/trades, funding/OI/premium or REST API is
requested. Download concurrency is three; HTTP 418/429/503 stops without retry.
The per-file cap is 250 KB and per-invocation new transfer cap 300 MB. Missing
archives remain unavailable. Default offline mode rejects any missing cache key.

## Candle validation and exact coverage gate

All downloaded/reused archive rows are parsed, not inferred from filenames.
Each file must have the expected symbol-month, ordered unique aligned hourly opens,
exact close `open + 1h - 1ms`, finite valid OHLC, nonnegative volume/quote/trade/taker
fields, integer trade count and taker quantities no larger than their totals.
Duplicate/invalid archives are rejected rather than silently deduplicated.
Missing hours are retained as gaps, never filled.

For decision t, `futures_1h_available` requires the valid candle opening at t-1h
and closing strictly before t. A stale last candle cannot substitute for it.
This is source-bar availability, independent of positive volume. Rolling 4h/24h
feature availability is separately reported; a positive one-hour flag never
pretends there are 24 contiguous hours.

Exact coverage is reported for both ALL and BUY in both years, with total/available
rows, symbols and symbol-days. Symbol-day coverage has both any-available and
all-observed-rows-available definitions, avoiding an ambiguous single percentage.
Predictive statistics are gated on **all four row coverages >=80%**. If any fails,
only coverage results are produced, with `COVERAGE_STOP`; there is no fallback
mapping or predictive analysis.

## Exactly three new features

- `taker_buy_share_1h`: prior completed candle taker-buy quote / quote volume.
- `taker_buy_share_4h`: sum of taker-buy quote / sum of quote volume over exactly
  four contiguous completed hourly bars.
- `taker_buy_share_24h`: the corresponding 24-hour sum ratio.

These are not averages of hourly shares. Missing required hours make the feature
unavailable. A zero aggregate denominator is unavailable, never zero buy share.
A zero-volume hour is still an observed candle and can participate in a longer
window whose aggregate denominator is positive. Only closed bars with
`close_time < t` enter; the candle opening at t contributes nothing. Future-bar
perturbation tests enforce this boundary.

No imbalance transform, taker delta/acceleration, volume-adjusted score, funding,
premium, OI, long/short feature, interaction, trained model or threshold search.

## Outcomes, selection and fair controls

Outcomes are copied unchanged from the validated **spot** dataset:
`POST_HOC_FUTURE_LABEL_1H` 24/72/168h close return, MFE ATR and MAE ATR.
No futures forward outcome is computed. Existing +5R-before-stop and
stop-before-1R minute labels remain secondary, with UNKNOWN preserved.

After passing coverage, available vs unavailable spot rows are compared **before
flow feature evaluation** using 72/168h MFE ATR and close returns. Counts, observed
counts, medians and Cliff/Mann–Whitney AUC are exported by year and ALL/BUY.
This diagnoses nonrandom selection; no weighting, correction or imputation is used.

Controls are Market Score, delta4h and relative volume. Base control statistics
use this study's futures-available pool, not old all-row results. In addition,
each flow feature has a common matched pool requiring that feature and all three
controls to be finite. Flow and all controls are recomputed on precisely those
same rows, with input-row and paired-outcome-row SHA256 and counts. Thus rolling
window missingness or missing delta cannot silently change a comparison's pool.

## Fixed statistics and dependency screen

For each year: ALL futures-available, BUY futures-available, inherited
symbol-day-first, BUY-symbol-day-first and BUY-episode-first sensitivities.
Flags are copied **before** futures availability filtering. An unavailable first
observation is not replaced by a convenient later available row; episodes are not
redefined by futures coverage. The validated episode is same-symbol consecutive
Top20 hours with unchanged final eligibility; gaps/boolean changes reset it.

Ten descriptive percentile deciles use deterministic average ties. Equal values
are never split; empty bins remain explicit. The top/bottom quintiles use fixed
percentile >=.8 / <=.2. Spearman, median difference, Mann–Whitney AUC and
Cliff=2*AUC-1 are reported. Decile increasing/decreasing steps, rho and descriptive
monotonicity use the unchanged prior helper (>=5 observed bins, |rho|>=.8 and
>=75% concordant adjacent observed steps). No cutoff search or p-values.

The screen is frozen before seeing these outcomes:

- Both years and both 72/168h ALL MFE ATR cells need the same direction,
  |Cliff|>=.147, same-direction |Spearman|>=.05 and >=30 observations per extreme.
- ALL symbol-day, BUY and BUY-episode-first cells must have adequate counts and
  preserve direction in both Cliff and rho.
- Exact leave-one-symbol and leave-one-UTC-calendar-quarter statistics use fixed
  full-population quintile memberships, not retuned bins. Every ALL removal must
  retain >=30 per extreme, the direction and at least 50% of the full effect.
  An inadequately observed dominant-group removal cannot establish robustness.
- Adequately observed secondary +5R/stop Cliff >=.147 in the opposite direction
  vetoes nomination. Secondary results never establish a primary signal or create
  a category from otherwise weak primary data.

The minimum-count/rho/retention conventions are descriptive safeguards, not
trading thresholds. Calendar quarters are a fixed time-concentration proxy, not a
claim to identify every latent market episode. BUY episode-first separately limits
repeated setup observations. Overlapping forward windows and correlated symbols
still prevent independent-sample or causal claims.

A: a surviving primary candidate with no strong secondary contradiction.
B: nontrivial local primary effects, but no survivor.
C: no nontrivial primary effect. At most one candidate, selected by its weakest
ALL 72/168h cell magnitude, then feature name. Control comparisons are reported;
univariate findings cannot prove conditional incremental information.

FET 2024-02-24 12:00, ETH 2024-02-20 16:00 and PEOPLE 2024-05-31 01:00 UTC are
post-hoc anatomy examples only. Their shares and year/population percentiles are
exported; they cannot influence the screen or create a rule.

## Exports and safety

`artifacts/futures-taker-flow-v1/` contains the required datasets, exact coverage,
mapping, selection comparison, deciles/statistics, fair controls, cross-year and
dependency checks, concentration leave-outs, minute consistency, winner anatomy,
source validation, archive manifest, report, summary and methodology.
ZIP members are sorted with fixed timestamps/permissions and SHA/size manifest.
Neither raw cache ZIPs nor API credentials are exported.

SQLite is `mode=ro` with `PRAGMA query_only=ON`. Source-run tables and the complete
run-ID inventory are fingerprinted before/after, source CSVs rehashed and every
pinned complete archive revalidated. There are no ReplayEngine, RiskManager,
PortfolioService, account/order clients or production writes.

Targeted tests exercise checksum/cache pins, URL/family restrictions, exact mapping,
duplicate rejection, gaps, strict close/current/future boundaries, contiguous
windows, sum ratios, zero denominator, coverage gating, unchanged labels/flags,
matched control row hashes, exact leave-group U against brute force, primary vs
secondary category rules, dominant-group rejection, no production DB write,
rate-limit stop and deterministic ZIP. Full maintained tests and Ruff are required
before commit/push.

## Completed results

1,657 necessary monthly keys: **1,623 complete, fully parsed/checksum-verified
archives**, 34 explicitly unavailable. Complete ZIP bytes: **53,659,711**;
parsed candles: **1,166,743**. No invalid or duplicate archive was accepted.
Only source-required 2023-12 warmup is included, no 2023 cohort or 2026 input.

| Population | Available / total rows | Exact % | Available / total symbols | Fully available symbol-days % |
|---|---:|---:|---:|---:|
| 2024 ALL | 153,337 / 175,680 | 87.2820 | 219 / 271 | 87.6810 |
| 2024 BUY | 4,194 / 4,915 | 85.3306 | 92 / 109 | 86.0479 |
| 2025 ALL | 150,045 / 160,320 | 93.5909 | 248 / 270 | 93.7355 |
| 2025 BUY | 1,986 / 2,069 | 95.9884 | 44 / 46 | 97.2112 |

All four >=80% gates passed. Day completeness is relative to canonical rows in
that population, not a fabricated 24-hour membership requirement.

Finite 1h/4h/24h feature rows are 152,567 / 152,556 / 152,311 in 2024 and
149,795 / 149,790 / 149,740 in 2025. Source-bar availability and nonzero-window
feature availability are deliberately separate.

Selection is nonrandom. Available-minus-unavailable MFE Cliff at 72/168h:
2024 ALL +.0423/+.0756, BUY +.0185/+.0199; 2025 ALL +.0191/+.0148,
BUY **+.2785/+.4427**. The 83 unavailable 2025 BUY rows are BONK (56) and
PEPE (27), excluded by exact-name mapping. Their complete 168h median spot MFE is
2.8379 ATR, vs 5.2055 in the available group; median close return is -14.2641%
vs -1.9750%. This is selection bias, not a flow effect, and was not corrected.

Final category: **B — FUTURES_TAKER_FLOW_SIGNAL_WEAK_OR_UNSTABLE**.
`hypothesis_candidate_feature = NONE`.

Every pair below is **72h / 168h**, top-bottom MFE ATR Cliff and Spearman rho.

| Feature | Year/population | Cliff | Spearman |
|---|---|---:|---:|
| 1h share | 2024 ALL | +.0175 / +.0184 | +.0100 / +.0099 |
| 1h share | 2025 ALL | +.0184 / +.0212 | +.0105 / +.0107 |
| 1h share | 2024 BUY | +.0135 / -.0037 | +.0190 / +.0064 |
| 1h share | 2025 BUY | +.0048 / +.0615 | +.0210 / +.0539 |
| 4h share | 2024 ALL | +.0394 / +.0343 | +.0230 / +.0196 |
| 4h share | 2025 ALL | +.0566 / +.0545 | +.0309 / +.0281 |
| 4h share | 2024 BUY | +.0564 / +.0410 | +.0291 / +.0203 |
| 4h share | 2025 BUY | +.0453 / +.1131 | +.0444 / +.0935 |
| 24h share | 2024 ALL | +.0438 / +.0436 | +.0308 / +.0323 |
| 24h share | 2025 ALL | +.0987 / +.0873 | +.0532 / +.0460 |
| 24h share | 2024 BUY | -.0618 / -.0712 | -.0410 / -.0503 |
| 24h share | 2025 BUY | +.1294 / +.1815 | +.0640 / +.1217 |

No feature meets the nontrivial ALL requirement in both years/horizons.
The 4h dependency direction survives but remains small. The 1h BUY direction
is unstable. The 24h BUY direction reverses by year, including episode-first:
2024 -.0116/-.0154 versus 2025 +.1483/+.2023. Symbol-day-first 24h Cliff is
+.0438/+.0284 in 2024 and +.1177/+.0983 in 2025. Removing 2024Q3 reduces
24h ALL Cliff from +.0438/+.0436 to +.0218/+.0089, failing the frozen retention
screen. No single-year local effect is promoted.

Matched 24h-share control comparison (same rows within each cell):

| Population | Flow Cliff | Market Score Cliff | Delta4h Cliff | Relative volume Cliff |
|---|---:|---:|---:|---:|
| 2024 ALL | +.0478 / +.0490 | +.0222 / -.0334 | +.0517 / +.0507 | +.0632 / +.0524 |
| 2025 ALL | +.0959 / +.0864 | -.0096 / -.0443 | +.0506 / +.0442 | +.0705 / +.0742 |
| 2024 BUY | -.0618 / -.0712 | -.0337 / -.0631 | +.0301 / +.0301 | +.0199 / +.0345 |
| 2025 BUY | +.1294 / +.1815 | -.0446 / -.0658 | -.0454 / -.0324 | +.0627 / +.0592 |

The stronger 2025 local flow association does not establish stable two-year
improvement over the controls. Full matched counts, input/paired-row hashes,
medians and AUC are exported, including separate 1h/4h comparisons.

Secondary BUY +5R-label Cliff for 1h/4h/24h shares: 2024 -.0005/+.0303/-.0237;
2025 +.0629/+.0898/+.1055. Corresponding stop-before-1R Cliff is
2024 -.0147/-.0193/+.0426 and 2025 +.0171/+.0096/+.0095. No strong secondary
contradiction veto occurs, but secondary data cannot rescue failed primary screens.

Post-hoc known-winner anatomy, share followed by **2024 ALL percentile**:

| Example, UTC | 1h | 4h | 24h |
|---|---:|---:|---:|
| FET 2024-02-24 12:00 | .54383 (P92.14) | .54649 (P99.29) | .48436 (P26.38) |
| ETH 2024-02-20 16:00 | .48065 (P37.22) | .49017 (P47.49) | .49004 (P45.81) |
| PEOPLE 2024-05-31 01:00 | .53987 (P90.63) | .53218 (P97.51) | .48722 (P35.62) |

BUY percentiles/reference counts are also exported. The examples are not a shared
rule or selection criterion; ETH differs from the short-window FET/PEOPLE pattern.

Validation: **40 targeted tests PASS; 972 full maintained tests PASS** in 240.18s.
Maintained Ruff and `git diff --check` PASS. Independent export QA recomputed all
336,000 row windows directly from raw pinned ZIPs: zero feature/availability
mismatches, exact copied outcome/dependency fields, no duplicate observations.
Matched control row identities, source fingerprints, file hashes, ZIP CRC and
byte-identical repeated packaging were verified. No production database writes,
new replay, deployment or production behavior change.
