# Complete Replay Research Bundle

Implementation branch: `feature/replay-run-research-export`, based on
`be08107d11728a3e90c8ec3dbe351616074c5bf7`. Deployment is deferred.

## Audited export completed first

Run `d3ba304b99cb4d8c918cbdbf6e884afb` is COMPLETED at 100%.
Execution SHA: `be08107d11728a3e90c8ec3dbe351616074c5bf7`.
Config hash: `f5a0d96a9dbf2a98610ba1d03ea6f8d4dfe9fb88c6f43760c8a3294f1bb36d53`.
Market context: `ISOLATED_TOPN_V1`.

The read-only artifact export was made before application changes. Its ZIP is:

`/home/ndvi/crypto-bot/artifacts/institutional-exit-v2-isolated/results/2024-institutional-exit-v2-isolated-results.zip`

Size: 16,064,040 bytes. SHA256:
`3b5e2ef1be167723aceea0acc4a8eedbcb77d8c2092318337facb1feb8a1d249`.
The application exporter produces byte-identical output for this real run.
No strategy was evaluated or recommended and no replay was started.

## Web behavior

The existing run page now begins with **RESEARCH EXPORT** and
**DOWNLOAD COMPLETE RESEARCH BUNDLE**. The ZIP contains every variant regardless
of the current variant selection. The previous **DOWNLOAD FULL BUNDLE** label is
now **DOWNLOAD VARIANT BUNDLE**. Its URL and contents remain compatible.

New endpoint:
`GET /api/replay/runs/<run_id>/research-bundle.zip`.

A completed run returns a ZIP attachment named `replay-research-<run_id>.zip`.
Unfinished, failed or cancelled runs return HTTP 409. The button remains visible
and disabled until COMPLETED, including transitions observed by polling. Missing
runs return 404 without initializing replay storage. Export integrity failures
(config hash/context mismatch or inconsistent record counts) return 409.

## Format and reproducibility

The 2024 ZIP has 22 entries:

- `run-summary.json`, `config.json`, `export-validation.json`, `manifest.json`.
- `run-metadata.json`, `validated-config.json`, `comparison.json`.
- A/B/C/D/E each have `.analysis.json`, `.compact.json`, `.trades.json`.

Analysis and compact JSON reuse existing payloads. Trades JSON retains all raw
position fields and includes closed, open and OPEN_STALE records with explicit
run ID, numeric variant index and stable letter label. Convenience aliases for
realized R, giveback R and holding seconds reference existing stored fields.
Public `config.json` uses the existing import schema; `validated-config.json`
retains the immutable execution snapshot, including derived context metadata.
Execution provenance stays at the run's original SHA, independent of exporter SHA.

Run summary includes stored metrics/breakdowns, closed/open/stale counts,
excursion means/medians, exit stages and reentry/rearm metrics. Pairwise deltas
are candidate minus reference. Percentage changes are percentage-point
comparisons. No winner is selected and no strategy recommendation is generated.
Unavailable historical observations are null, with coverage counts reported.

Canonical JSON uses sorted keys and no export-time timestamps. ZIP entry order,
1980 timestamps, permissions and compression settings are fixed. The same run
produces identical bytes in the same runtime. Manifest hashes all other entries,
including export-validation; validation hashes data files but excludes itself.
This avoids circular self-hashes. Cross-zlib-version byte identity is not claimed.

## Read-only guarantee and scope

`ReadOnlyReplayStore` bypasses ReplayStore initialization/migration. It opens the
existing database using SQLite URI `mode=ro`, `PRAGMA query_only=ON`, and read
transactions. Both new run-level and existing variant export endpoints use this
store. Missing storage is not created. Export calls do not invoke create, worker,
scanner, trading or execution methods.

Changed runtime files are the new replay research exporter, replay web routes,
run template, result-page JavaScript and responsive styling. Scanner, paper,
strategy, RiskManager, scoring, exits, and ReplayEngine results are unchanged.
No migration, production deployment or service restart was performed.

## Validation

Tests cover five-variant identity/content and provenance, byte reproducibility,
manifest hashes, importable public config/frozen config, unchanged legacy links
and variant ZIPs, completed-only exports, all seven non-completed statuses,
unknown runs, config corruption, missing storage, read-only connections,
forbidden ReplayStore initialization/create/ReplayEngine execution, and unchanged
DB bytes. Chromium tests at 1440px and 390px verify visibility above the fold,
no horizontal overflow, all-variant downloads after changing selection, disabled
RUNNING state, and polling enablement after completion.

Screenshots: `artifacts/replay-research-export-1440.png` and
`artifacts/replay-research-export-390.png` in the feature worktree.
Final full-suite and Ruff outputs are recorded in the feature validation artifact.

Final validation: **647 tests passed** in 190.33 seconds, including **14 new
export/UI tests**. Maintained Ruff (`crypto_bot tests research`) passes. Full
`ruff check .` has the same four pre-existing I001 errors as the base checkout
in `backtest.py`, `legacy/backtest.py`, `legacy/dashboard.py`, and
`legacy/trading_bot.py`. Those legacy files were not modified.
