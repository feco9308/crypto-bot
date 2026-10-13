# Replay companion isolation — 2026-10-13

Research-engine invariant: adding, removing or reordering companion variants must
not change a control's trades, orders, fills, timestamps, cash or equity.
NO LIVE TRADING. No production deployment or five-variant annual rerun was performed.

## Reproduction before the fix

Both single-variant 2024 controls execute the unchanged
`8929594ce5ff4863482fed10fc20f1e6373b3138` code in an isolated lab database.
Production replay records and candle cache are opened read-only; network downloads
are forbidden. The source run's candidate catalog and common archive SHA revisions
are pinned. Expired negative cache entries are frozen as observed (not refreshed).
The artifact harness initially failed before engine execution on an unsupported
store.update argument; it was corrected to claim the same queued run IDs. No
replay engine code was changed for that correction.

- A / baseline: `0c144dd20c41407995ae5ab0786902b5`.
- B / adaptive: `19b1849ac3ee47c88d05793ae679cedf`.
- Source reference: `3ac0e63caa874b1abd1e78558092bb9b` C/D.
- Historical v2: `481f49758ef747238a9aea31f0b18258` A/B.

A completed with 298 closed / 1 open, cash `771.263478477719`, final equity
`884.615084445646`. It matches neither contaminated reference: reference C
`887.835303894497`; v2 A `896.971859434663`.
Its first trade mismatch is record 76: A solo AVAXUSDT at 2024-03-19 08:00 UTC,
reference APTUSDT at 03:00 UTC. Order/fill prefix length is 148.
B completed with 1398 closed / 0 open, cash and final equity
`541.336999355530`. Its first trade mismatch is record 117,
2024-01-29 02:00 UTC: solo SUIUSDT versus reference CFXUSDT. The order/fill
prefix is 232. The final annual audit is saved separately; the earlier partial
prefix evidence remains explicitly labelled partial.

Single-variant execution cannot restore an old reference that was itself affected
by companions. The correction guarantees exogenous-context isolation for new runs,
not reproduction of those invalid old annual comparison numbers.

## Causal trace: 2024-05-04 17:00 UTC

Legacy ReplayEngine builds historical Top20 rows, adds the UNION of all portfolios'
held symbols, and writes scores for the entire union into one shared history.
Although held-only rows are not auto-eligible, their stored scores affect momentum
when those symbols later return to Top20. Watchlist priority is existing score +
0.5 * positive delta4h; the watchlist then affects every variant's signals.

At 13:00 UTC OPUSDT is outside Top20. Only reference variant 0
(Reference + Baseline), position 123, holds it (entry May 1 15:00, exit May 6 23:00).
It inserts score `69.74213460743286` into the shared history.
At 17:00 OP returns to the identical Top20 in both historical runs:

| Symbol | Score | Reference delta4h | Reference priority | v2 priority |
|---|---:|---:|---:|---:|
| OPUSDT | 75.59768858268122 | 5.855553975248256 | 78.52546557030534 | 75.59768858268122 |
| NEARUSDT | 76.36745988122428 | 2.6147184829794554 | 77.67481912271401 | 77.67481912271401 |

In v2 there is no OP holder at 13:00, so its delta4h is unavailable.
OP displaces NEAR from the reference watchlist; v2 admits NEAR BUY instead of
`OUTSIDE_ALGORITHM_WATCH`. Removing ONLY reference OP position 123 admits NEAR;
adding ONLY that holder to the v2 counterfactual excludes NEAR. This proves the
specific companion state caused the divergence. Top20 membership and NEAR's
score/indicators are unchanged, as are common archive revisions and catalog.
Trace reconstruction uses the full original warmup, avoiding floating-point
prefix differences from truncated cumulative-volume history.

Two additional dependencies were found and covered:

1. Global maximum companion warmup changes the control EMA seed. Cached BTC data
   at Apr 1 yields EMA200 `46944.88683553891` with 4824h, versus
   `47077.11539446941` with a companion's 7224h. Both are causal past data,
   but the same control must not get a different past-window origin.
2. A Reference variant's trade/report metadata was enriched only if an
   institutional companion existed, using the first companion's BTC settings.

## Correction and compatibility

New validated run snapshots derive `market_context_version=ISOLATED_TOPN_V1`.
The public config import/export schema and configurable thresholds are unchanged.
The marker participates in new config hashes and is stored in new run metadata.
No database migration is required.

- A frozen MarketFrame holds immutable serialized TopN rows and watchlist items.
  Every variant receives independent nested copies.
- Shared entry score history is updated ONLY by historical TopN membership.
  No portfolio state is an input to shared ranking, history or watchlist.
- Held symbols outside TopN are computed read-only for that variant's own exits.
  Those rows do not update the shared score history.
- Scanner cumulative-volume history starts at the existing fixed 524h scanner
  warmup, independent of larger companion higher-timeframe data requirements.
- Institutional context has an own slow-period seed bound per strategy, even if
  raw history is loaded further back for another variant. Existing EMA arithmetic,
  regime definitions and strategy parameters are unchanged.
- Reporting settings and institutional annotations are variant-local. Cached
  higher-timeframe audit dictionaries are copied before variant use.

Stored configurations WITHOUT the marker remain `LEGACY_SHARED_HELD_V1`;
old checkpoints resume with their original behavior. A context-version mismatch
is rejected, never silently upgraded. Existing result/config hashes are not
rewritten. Historical v2 is flagged `CONTROL_PARITY_FAILED` in an external
warning sidecar, not by editing DB rows or old exports.

## Mandatory tests and scope

Deterministic fixtures include actual companion holdings outside TopN and prove
the old legacy path changes the control. The isolated path compares complete
trade/order/fill JSON, all entry/exit timestamps, hourly equity curves, cash and
final equity for A and B solo versus X, A+B, A+B+C, A+B+C+D+E, reordered mixtures
and a companion with longer EMA warmup. Checks also cover hostile mutable row
copies, Reference reporting isolation, future candle changes, config round-trip,
legacy resume, new checkpoint/resume and context-version mismatch rejection.

Full maintained suite: **633 passed** (282.00 seconds), including **22 new
isolation tests**. It covers no-lookahead, stale quarantine, exit policies,
import/export and production paper behavior. Maintained Ruff (`crypto_bot tests
research`) passes. Full `ruff check .` reports four pre-existing I001 errors in
`backtest.py`, `legacy/backtest.py`, `legacy/dashboard.py`,
`legacy/trading_bot.py`; its output is identical to production's 8929594 baseline.
The initial full-suite attempt found one missing test-only deepcopy import; after
correction, the final complete suite passes.

Changed runtime files are limited to `crypto_bot/replay/`; new tests and this
report are outside runtime. Production scanner, score implementation,
ReferenceStrategy, RiskManager, PortfolioService, paper engine, public execution,
exit-policy arithmetic and strategy evaluate methods remain unchanged.

## Evidence paths

All artifacts are under production workspace's ignored directory
`artifacts/replay-companion-parity-2024/`:

- `trace/watchlist-trace.json` and `trace/first-divergence.json`.
- `trace/warmup-coupling-proof.json`.
- `reproduction/runs.json`, `frozen-inputs.json`, `exact-parity-audit.json`.
- `reproduction/B-prefix-audit.json` (partial, not a final annual result).
- `historical-result-fingerprints-before.json` and corresponding after proof.
- `historical-run-warnings.json` and `validation.json`.

Deployment is deferred. If subsequently authorized, web and replay-worker need
new code loaded; scanner/paper do not need a restart. No new five-variant annual
run was launched after the fix.

## Verdict

**GO for review of the replay-only isolation fix**, on local branch
`fix/replay-companion-isolation`; implementation commit
`372ce2e28ac46d5778b580de4475003e17c1fd0a`.
**NO-GO for interpreting the old annual comparison as a controlled experiment**.
Neither old-code single-variant control restores the earlier companion-affected
reference. Exact record mismatches are preserved in the audit; no old result has
been made to look like a corrected result. Both historical run record
fingerprints are unchanged. Production remains at 8929594 with clean checkout;
no production service restart, migration, optimization or replacement annual
five-variant run was performed. Deployment requires a later explicit decision.
