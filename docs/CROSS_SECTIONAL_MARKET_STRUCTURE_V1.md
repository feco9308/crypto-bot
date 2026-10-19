# Cross-Sectional Market Structure Diagnostic v1

Offline **EXPLORATION**, not a strategy, ranking, filter, score change or replay.
Base: `74ae2e80a5a0c47c0bdf2e7c4ef902b69ada0917`.

## Reproduce

From this isolated research worktree, using the existing project Python environment:

```bash
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python -m research.cross_sectional_market_structure --root /home/ndvi/crypto-bot
```

The root supplies immutable source data and the output directory, not executable
production code. No external package installation is needed. Outputs are under
`artifacts/cross-sectional-market-structure-v1/` in that root. The script uses
columnar standard-library arrays and one symbol's archive at a time to avoid
holding the previous ~1 GB verbose eligibility CSVs in RAM.

The script checks all relevant prior-file hashes against the eligibility manifest,
source-run config hashes, exact source commits, canonical isolated context and the
previous diagnostic's source table fingerprints. It uses the completed 2024 run
`31423539f64e4650840df5b6b3fe9a26` (S, index 1, financially equivalent baseline)
and 2025 run `f9d41057d61e4714b61df69d320620e0` (A, index 0). Both are exploration;
2025's original dataset label does not make it clean validation again.

The 2024 cohort is `[2024-01-01, 2025-01-01)`; the comparable 2025 cohort is
`[2025-02-01, 2026-01-01)`. Historical Top20 membership is the previously validated
strict closed-candle rolling-volume universe, not today's coins. Held-only rows
are rejected. 2023 can only supply source-required past history; no 2023 research
population and no 2026 archive/future data is used.

## Frozen features

Exactly six relative-strength features and seven market-context features:

- cross-sectional percentile rank of 4h and 24h returns;
- 4h and 24h excess return vs the current Top20 median;
- 4h and 24h excess return vs BTC;
- positive-return breadth over 4h and 24h;
- breadth of price > EMA50, and EMA50 > EMA200;
- current Top20 median returns over 4h and 24h;
- population standard deviation of Top20 24h returns (`ddof=0`).

Return is percentage change between the last completed hourly close and the close
exactly 4/24 completed hours earlier. Every feature's source candle must close
strictly before the decision timestamp. Contiguous hours are mandatory; stale,
missing or unclosed bars produce unavailable values. No current-hour OHLC is used
in features. BTC uses the same rule, even when outside Top20. Its missing return
makes only the BTC-relative feature unavailable.

Ranks use average ties and `(rank - 1)/(n - 1)`, giving 0 and 1 for distinct weakest
and strongest members. Equal returns share their percentile. Breadth uses exactly
20 source members, with strict `>` comparisons. A missing required member value
makes the corresponding cohort context unavailable rather than rescaling a
partial cohort. Every member of the same timestamp receives identical breadth.
EMA/ATR are the previously validated decision-time institutional inputs; no
indicators are redefined or optimized. Controls are Market Score, delta4h and
relative volume, using exactly the same future labels as the new features.

## Primary outcomes, separately computed

`POST_HOC_FUTURE_LABEL_1H.*` is a separate namespace and computation step, with
fixed 24/72/168h windows. For decision `t`, the window consists of hourly candles
opening at `t, t+1h, ..., t+(h-1)h`, all fully closed before `t+h`. The candle
opening at `t` is future information and is used **only for labels**. The reference
is the prior scanner row's decision price, and the ATR is that row's past ATR.

Each complete horizon has:

- final close return %, maximum high return %, minimum low return %;
- `(max future high - reference)/decision ATR`;
- `(min future low - reference)/decision ATR`.

These are signed extrema, without clamping to zero. They are neither hypothetical
stop trades nor fee-adjusted returns. Missing bars censor the entire horizon;
run-end censoring never shortens it or reads into the next year. Invalid ATR
removes ATR-normalized outcomes while percentage returns can remain observable.
Coverage and censoring counts are reported independently for every horizon/year.

Secondary 1m +1R/+2R/+5R/stop-before-1R labels are copied unchanged by symbol/time
from manifest-verified eligibility rows, including UNKNOWN. No minute data is
fetched or recomputed. They cannot establish the primary conclusion and have
substantial nonrandom coverage limitations.

## Dependence and statistical rules

Both years have ALL_TOP20 and unchanged CURRENT_BUY_ELIGIBLE analyses. Sensitivities:

- first Top20 observation per UTC symbol-day;
- its BUY-eligible subset;
- first BUY row of the validated eligibility episode (same symbol, consecutive
  Top20 hours, unchanged final eligibility; gap/eligibility flip resets, HOLD
  reason changes do not).

Breadth is additionally analyzed on **one row per timestamp**, using the median
future outcome of that timestamp's fixed 20 members, not future universe members.
All 20 future labels must be available. One market row per UTC day is an additional
sensitivity. These are authoritative breadth checks; 20 same-hour coin rows are
never presented as 20 independent market observations.

Each feature/population has ten descriptive percentile deciles. Equal values are
never split; empty bins are explicitly exported. Quintiles use percentiles >=.8
and <=.2, not fitted thresholds. Pairwise-complete Spearman and top-minus-bottom
median difference/Cliff's delta are exported. Cliff = 2 Mann–Whitney AUC - 1.
Decile monotonicity reports increasing/decreasing steps and decile-index Spearman;
a descriptive yes requires >=5 observed bins, |rho|>=.8 and >=75% concordant steps.
No grid, interactions, model fitting, cutoff search, p-values or independent-sample
confidence claims are used.

The conservative evidence screen is frozen before inspecting this study's output:

- both years and both 72/168h MFE ATR horizons have the same direction;
- ALL, symbol-day, BUY and BUY-episode sensitivities each have >=30 observed rows
  per quintile, |Cliff|>=.147 and same-direction |Spearman|>=.05;
- breadth must also pass timestamp and market-day sensitivities;
- no strongly inverse 72/168h forward-return association (rho >=.05 in magnitude);
- no adequately observed strongly opposite secondary +5R/stop association
  (|Cliff|>=.147, >=30 per binary group).

At most one passing new feature is nominated, ordered by its weakest required
absolute Cliff, then feature name, and exceeding the strongest stable control by
.02. This is descriptive selection among fixed hypotheses, not an entry rule.
Category A requires such a candidate; B means at least one nontrivial local effect
but no survivor; C means no such local effect. Category names use the requested
`ORTHOGONAL_*` terminology; this does **not** prove conditional orthogonality or
incremental net trading edge. ATR normalization, overlapping windows, shared market
cycles, coverage differences and selection remain material limitations.

## Safety and output verification

Only `mode=ro` SQLite connections with `PRAGMA query_only=ON`; no repository/writer,
ReplayEngine, RiskManager, PortfolioService or order creation. A Python audit hook
rejects socket connections and DNS. Only exact source-run-pinned local ZIP bytes
are loaded, SHA-checked before use and rechecked at completion. Cache metadata,
source-run tables and the inventory of all run IDs are fingerprinted before/after.
No replay checkpoints or live database records are changed.

The ZIP has sorted members, fixed timestamps/permissions and manifest SHA256/size
for every payload. Identical inputs and code yield byte-identical CSV/JSON/ZIP.
The manifest records the research script hash and base; the source validation
records exact original run SHAs/config hashes and used archive pins.

Tests cover source isolation, no network, read-only/write rejection, deterministic
ranks/deciles, return/label time boundaries, BTC freshness, exact cohort breadth,
censoring, fixed ATR, secondary-only labels, dependency screens and deterministic
exports. The full maintained suite and Ruff must pass before committing/pushing.

No deployment, new replay, production behavior change or automatic promotion.

## Completed evidence

Population: 175,680 rows / 8,784 hours in 2024 and 160,320 rows / 8,016
hours in 2025. BUY-eligible sensitivity has 4,915 / 2,069 observations;
eligible episode-first has 3,005 / 1,329. Symbol-day-first counts are
9,254 / 8,205; their eligible subsets are only 120 / 50, so those cells
must not be overinterpreted.

Primary coverage (percent of the full Top20 population):

| Year | 24h | 72h | 168h |
|---|---:|---:|---:|
| 2024 | 99.7382 | 99.1872 | 98.0168 |
| 2025 | 99.6750 | 99.0051 | 97.6198 |

End censoring accounts for 460 / 1,420 / 3,340 rows at 24/72/168h in each
year; hourly archive gaps additionally censor 0/8/144 rows in 2024 and
61/175/476 in 2025. Missing stays unavailable. No next-year bars are used
to improve these figures.

Final category: **B — ORTHOGONAL_FEATURE_SIGNAL_WEAK_OR_UNSTABLE**.
Strongest stable new feature: **NONE**. `hypothesis_candidate_feature`: **NONE**.

72h MFE ATR top-minus-bottom-quintile Cliff (a descriptive excursion outcome,
not profit or causality):

| Feature | 2024 ALL | 2025 ALL | 2024 BUY | 2025 BUY |
|---|---:|---:|---:|---:|
| 4h cross-sectional rank | +.0550 | +.0531 | -.1238 | -.0215 |
| 24h cross-sectional rank | -.0440 | -.0325 | -.1879 | -.0809 |
| 4h excess vs Top20 median | +.0586 | +.0547 | -.0957 | -.0851 |
| 24h excess vs Top20 median | -.0419 | -.0292 | -.2029 | -.1488 |
| 4h BTC excess | +.0377 | +.0529 | -.1514 | -.1031 |
| 24h BTC excess | -.0161 | -.0094 | -.1706 | -.1070 |
| Positive 4h breadth | +.0054 | +.0034 | +.0033 | -.0505 |
| Positive 24h breadth | +.0801 | +.0553 | +.0159 | -.0212 |
| Above EMA50 breadth | +.1166 | +.0482 | +.1346 | +.0683 |
| Bullish EMA breadth | +.0504 | +.0328 | +.1995 | +.0892 |
| Median Top20 4h return | +.0074 | +.0119 | -.0043 | -.0436 |
| Median Top20 24h return | +.0912 | +.0291 | +.0606 | -.0297 |
| 24h dispersion | +.0929 | +.0970 | +.1507 | +.0209 |
| Market Score control | +.0262 | -.0007 | -.0481 | -.0658 |
| Delta4h control | +.0484 | +.0515 | +.0320 | -.0429 |
| Relative-volume control | +.0673 | +.0721 | -.0027 | +.0564 |

The 4h relative-strength direction changes between the full population and eligible
subset. The 24h eligible negative association weakens in 2025 and is small in the
full population; this does not justify a new momentum/reversal entry rule.

Market-level dispersion has a local positive association: one-row-per-timestamp
72h Cliff +.1542 / +.1775 and 168h +.1615 / +.1119. However, the 2025 BUY-episode-first
72h Cliff is -.0051, versus +.1421 in 2024. It fails the required cross-population,
horizon and dependence screen. It is not a nominated feature.

Breadth above EMA50 also weakens between years. Its authoritative hourly-market
72h Cliff is +.1835 / +.1016, while individual ALL rows give +.1166 / +.0482.
Bullish-EMA breadth shows a secondary contradiction in 2024's small
BUY-symbol-day-first sample: higher breadth has stop-before-1R AUC .6073,
Cliff +.2145 (65 stop-positive vs 55 negative). This subset is not a primary
finding. None of the 13 new features passes the complete frozen screen;
Market Score and the other controls remain weak/unstable as well.

All statistics, deciles, 168h results, return associations, monotonicity counts,
unknown-1m coverage and dependency sensitivities are exported. No strategy,
ranking, gate or score change follows from this diagnostic.

## Validation result

34 targeted tests passed. Full maintained suite: **881 passed** in 236.32s.
Maintained `ruff check crypto_bot tests research` and `git diff --check` passed.
Production runtime files are untouched; no migration or restart is required.

Independent export QA checked all 336,000 rows against the manifest-verified
eligibility source, including exact secondary labels, strict feature timestamp
bounds, episode/daily flags, duplicate absence and exact hourly ranks 1..20.
A separate direct pinned-archive calculation checked 26 sampled rows in 2024 and
23 in 2025: zero return/primary-label mismatches. Before/after source table and
cache metadata fingerprints match. ZIP record SHA/size/CRC and repeated packaging
are verified separately; `export-validation.json` records these row-level checks.
