# Institutional Signal Edge Diagnostic v1

Research base: `226facef1f68f967fc1d7b1bb75e282849185a56`.
This is an offline, univariate, read-only diagnostic, not a strategy, entry filter,
ranking, capacity rule, trained model, replay or deployment.

## Sources and commands

Primary: 2024 EXPLORATION run `31423539f64e4650840df5b6b3fe9a26`,
variant S (index 1), source commit `75998cae198f09f7be33bf5777f048b9653d7f81`.
The authoritative population is every `ENTRY_CANDIDATE_RANKED` event, including
candidates rejected by subsequent risk/capacity checks. No actual-trades-only
selection is made. This is conditional on the original strategy's portfolio-path
eligibility, not a hypothetical portfolio-independent strategy reevaluation.

Secondary: A (index 0) of existing 2025 run
`f9d41057d61e4714b61df69d320620e0`. Machine comparison of normalized strategy,
risk, exit, scanner/universe settings and source-code hashes is mandatory.
A mismatch excludes 2025; it never triggers a correction or new replay. Because
ranking was OFF, its pre-risk BUY audit comprises BUY `STRATEGY_SIGNAL` plus
pre-risk `RISK_REJECTED`/`NO_FILL` BUY events, with duplicate protection. No
ranking is invented for that source. 2025 is reused **EXPLORATION**, despite its
historical source run's VALIDATION label; no new validation claim is made.

```sh
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python -m research.institutional_signal_edge \
  --root /home/ndvi/crypto-bot
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python -m pytest -q tests/test_institutional_signal_edge.py
PYTHONPATH=. LD_LIBRARY_PATH=/home/ndvi/.local/lib/crypto-bot-browser/usr/lib/x86_64-linux-gnu \
  /home/ndvi/crypto-bot/.venv/bin/python -m pytest -q
/home/ndvi/crypto-bot/.venv/bin/ruff check crypto_bot tests research
git diff --check
```

Only `artifacts/institutional-signal-edge-diagnostic-v1/` is written. All replay
and cache SQLite connections use URI `mode=ro` and `PRAGMA query_only=ON`.
A process audit hook denies DNS and network connections. Local git object reads
prove source code equivalence; GitHub publishing is a separate authorized shell
action, outside the offline analysis process. No worker/store/execution/portfolio
service is instantiated. Logical fingerprints of all source-run tables and the
entire run-ID inventory are compared before/after; global live worker heartbeat
rows are deliberately not treated as immutable source data. Pinned cache object
metadata and SHA256-verified bytes are checked before use and again at the end.
No merged candle table, unpinned supplement, download or fallback is used.

## Decision-time reconstruction

The canonical scanner warmup start, finite 499-hour window, Top20 membership and
closed-candle score inputs come from existing pure source calculations. Score
history deltas require the symbol to have been in the shared Top20 at the prior
hour. Reconstructed S score/delta4h/relative-volume and all available actual-entry
score/deltas/RSI/ATR/range values must match the original audits.

The institutional context uses its existing complete UTC-aligned 4h/1d bars and
source-defined EMA seed. Every source timestamp precedes the candidate decision.
Pre-period data serves only the existing indicator seed; no 2023 candidate cohort
or 2026 archive is inspected. BTC regime is the institutional 4h/1d
`BULL/NEUTRAL/BEAR/UNKNOWN` definition, not the separate hourly reporting field.

EMA distances are explicit decision-time descriptors: latest closed 1h reference
against the completed timeframe's EMA50/200, and EMA50/EMA200 spread. A scalar
`trend_strength` does not exist in the scanner and is exported as unavailable,
without inventing an indicator. Correlation is reconstructed against the positions
observable at signal generation, including previous-minute exits at that boundary
but excluding ensuing same-timestamp strategy fills. Portfolio cash/position count
are descriptive reconstructions; exposure uses closed hourly marks, and is null
if any held symbol cannot be freshly marked. These fields are not analyzed as
asset-quality inputs or represented as the execution-time RiskManager view.

## Future labels and dependence

Every future field has `POST_HOC_FUTURE_LABEL` namespace. Proposed entry reference
minus original stop defines a **price R**, without simulated fees, slippage,
quantity, fills or orders. Separate 6/24/72/168-hour MFE/MAE require every minute
and full window coverage. Barrier observation ends at the original stop, first
missing minute or source run end. No gap is crossed and no incomplete observation
is called FALSE. The open is known first: a gap up through a target precedes later
intrabar lows; a gap down through the stop stops immediately. In remaining
same-minute high/low ambiguity, stop precedes target. Intrabar events are known
only at minute close. Actual trade outcomes have their own `ACTUAL_TRADE_LABEL`
namespace, using source net PnL/R and actual MFE/MAE unchanged. Future MAE R is
signed negative; source actual MAE R retains the export's adverse-magnitude
convention.

All raw hourly candidates remain. Same-symbol consecutive hourly candidates form
an episode; absence/unknown breaks it. Episode-first sensitivity uses the first
candidate, not the best future outcome. Symbol-day IDs are also exported. This
reduces duplication but does not establish statistical independence: forward
windows overlap and market/regime dependence remains.

## Frozen descriptive analysis

Exactly ten linear-quantile bins per feature/population, with equal values kept
together and empty bins explicit. No outcome-dependent tie splitting or cutoff
search. Full populations are used; there is no sampling. The positive group is
observed +5R before stop; the disjoint negative control is stop before +1R.
Reports include tie-aware Spearman, single-feature Mann–Whitney AUC, Cliff's delta,
median differences, IQR overlap, per-year and combined base rates, regime and
episode ordinal groups. Actual closed-trade associations are secondary only.

A fixed descriptive convention uses |Cliff's delta| >= 0.147 (small effect) to
label cross-year direction, never to build a trading threshold. Category A needs
at least two episode-first features with such effects in the same direction in
both years. Without that, a nontrivial 2024 episode effect gives B; otherwise C.
This is transparent descriptive categorization, not significance, independent
confirmation, model fitting or clean validation. Correlated features and multiple
comparisons constrain interpretation. Constant components within this already
selected BUY population cannot demonstrate discriminatory power.

Winner anatomy shows first candidates from top episodes reaching fixed 5/10/20R
labels, ordered by 168h MFE, and the largest actual positive FET/PEOPLE/ETH trades.
Decision-time feature population percentiles are included. Such post-hoc examples
must never become entry rules from this diagnostic.

## Artifacts

`report.md`, `summary.json`, `methodology.json`, per-year candidate CSV/JSON,
`market-score-deciles.csv`, `feature-deciles.csv`, `score-component-analysis.csv`,
`raw-feature-analysis.csv`, `cross-year-consistency.csv`,
`setup-episode-analysis.csv`, `btc-regime-analysis.csv`, `winner-anatomy.csv`,
`2025-source-comparability.json`, `source-validation.json`, `manifest.json`, and
`institutional-signal-edge-diagnostic-v1.zip`.
JSON uses stable sorting; ZIP member order, timestamps and permissions are fixed.
No migration, runtime import, production behavior change or deployment is needed.

## Recorded exploration result

The immutable-source check passed: source-run table fingerprints and run-ID
inventory are unchanged, pinned metadata agrees, and every used archive SHA256
was verified again. 2025 baseline comparability is YES. The read-only audit also
verifies every actual entry appears in its candidate population: 299 entries
(298 closed + 1 open) in 2024 S and 215 closed entries in 2025 A.

| Population | Candidates | Symbols | Symbol-days | Episodes |
|---|---:|---:|---:|---:|
| 2024 S | 2999 | 109 | 1269 | 1982 |
| 2025 A, exploration reuse | 959 | 46 | 495 | 716 |

Market Score +5R-versus-stop AUC is 0.48304 / 0.41272 in raw 2024 / 2025
observations, and 0.46864 / 0.42065 in episode-first sensitivity. Raw score vs
72h MFE Spearman is -0.01812 / -0.06298; vs 168h MFE -0.02877 / -0.11032.
Neither year's +5R score-decile rates are monotone. This selected BUY population
does not show a strong positive high-score advantage.

2024's strongest episode-first inverse effect is daily EMA50/EMA200 distance
(Cliff -0.22342); its 2025 effect is only -0.09837. Quote volume is a small
positive 2024 effect (+0.15279), weaker in 2025 (+0.07637). The stronger 2025
ATR/volatility-related inverse associations are weak in 2024. No feature passes
the fixed nontrivial same-direction convention in both episode-first years.
Absolute ATR is additionally confounded by symbol price scale.

Episode ordinal +5R rates for 1 / 2 / 3 / 4 / 5+ are:
2024: 14.71 / 14.17 / 16.46 / 16.38 / 23.68%;
2025: 15.78 / 10.32 / 5.08 / 4.17 / 20.00%.
The 5+ samples are only 38 / 5 candidates. There is no stable cross-year
monotone lifecycle relation to turn into an admission rule.

FET's large 2024 actual winner had score 72.58, at the 3.86th percentile of
the negative-control score distribution; the PEOPLE anchor was at 52.55th,
and the largest ETH winner at 25.42nd. These do not share a high-score outlier
profile. Full decision-time profiles and negative-control feature percentiles
are in winner-anatomy.csv; no rule is derived from these selected examples.

Final category: **CURRENT_FEATURE_SET_SIGNAL_WEAK_OR_UNSTABLE (B)**.
This is conditional on the existing BUY-eligible population and reuses both years
as exploration. It is not proof that all possible signals lack information, nor
a basis for automatically changing production score weights or entry rules.

Numerical-constant fields use a fixed 1e-12 * max(1, |median|) precision tolerance;
effects from floating noise in the configured constant spread are UNAVAILABLE,
not predictive signals. Equal/near-constant values are not artificially split
into outcome-distinguishing bins.

The unranked 2025 audit lacks a complete original StrategySignal at its single
pre-risk correlation rejection. Its BUY provenance and ATR-based stop are
explicitly reconstructed; unknown strategy reasons remain null, and the audited
execution-time correlation rejection is preserved separately. No risk reason is
relabeled as an original strategy reason.

## Final verification

- Targeted offline research tests: **33 passed**.
- Full maintained suite on the final implementation: **805 passed**, 241.42 s.
- Maintained Ruff: **PASS** (`crypto_bot tests research`).
- Staged diff whitespace check: **PASS**.
- Source fingerprints and complete run-ID inventory: **unchanged**.
- ZIP: **19 files**, 6,216,301 bytes; all manifest SHA256 values verified.
- ZIP SHA256: `581d97d74402693d4226b85808bcaa1c060a16fc8e576932d765d84a747ae662`.
- All candidate identities are unique, year/source-run scoped and audit-matched.
- Every coin/BTC trend timestamp precedes its decision; no 2026 archive object
  was read. 2023 inputs were source-defined warmup only, never a research cohort.
- New replay started: **NO**. Production behavior changed: **NO**.
- Database writes: **0**. Deployment: **NO**. Migration: **NO**.
