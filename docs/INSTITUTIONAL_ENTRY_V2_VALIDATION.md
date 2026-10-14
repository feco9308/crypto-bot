# Institutional Entry Research v2

Fresh Acceleration Without Volume Crowding. Research only. NO LIVE TRADING.

2024 and 2025 are exploration data for this new hypothesis. No thresholds were
tuned, no parameter sweep was performed and no research replay was launched.
2026 market data was not accessed. Production and its services remain unchanged.

Worktree: `/home/ndvi/crypto-bot-entry-v2`.
Branch: `feature/institutional-entry-research-v2`.
Base: `61141109e1a510c5f2a3d9bcee7f61e77fc53439` (current production export/UI
release; no unrelated work was reset or discarded).

## Frozen 2023 validation study

| Variant | Entry filter |
| --- | --- |
| A — Institutional Baseline Control | OFF |
| I — Strong Acceleration | min_delta_4h = 15.0 |
| J — Moderate Volume | max_relative_volume = 2.0 |
| K — Fresh Acceleration + Moderate Volume | both frozen constraints |

Every variant uses institutional_style_v1 and baseline_v1. All other strategy,
risk and exit parameters match A and the previous institutional A configuration.
The existing strategy's minimum relative volume remains unchanged; this research
gate adds only the requested inclusive upper bound.

Importable config:
`docs/replay/institutional-entry-v2-2023-validation.config.json`.
Period: 2023-01-01T00:00:00Z to 2024-01-01T00:00:00Z (exclusive end),
dataset_role=VALIDATION, historical Top20. Backend-derived ISOLATED_TOPN_V1,
RESEARCH_QUARANTINE_STALE, fallback_5m=false, conservative=true.

Initial balance 1000 USDT, risk/trade 0.5%, max positions 3, total/symbol exposure
50%/20%, daily loss and drawdown guards 100%, fee 0.1%, slippage 0.05%, no
pyramiding. Outer BTC entry gate and setup rearm remain OFF.

Validated config hash:
`fd9f2ba6ecf4634e45b731d709d7a0f84716f65fa5c2bfb3fd45e05bbb3c49a7`.
The public JSON does not inject internal market_context_version metadata.

## Implementation boundary

The existing replay entry_filter schema adds max_relative_volume. It reads
features.relative_volume directly from the current scanner row, after strategy
BUY and before RiskManager/execution. No alternative volume definition or data
source is introduced. A finite value <=2.0 passes; a value >2.0 yields
ENTRY_RELATIVE_VOLUME_ABOVE_MAX. Missing or nonfinite constrained input yields
ENTRY_RELATIVE_VOLUME_UNAVAILABLE. SELL and exit processing bypass the gate.

Active volume-filter pass/block audits include symbol, UTC timestamp, score,
delta_4h, relative_volume, effective thresholds and the original strategy BUY.
The existing persisted audit projection already includes thresholds; no schema
change is needed. Diagnostics count attempted/passed/blocked BUYs and reasons,
including both failures when K violates both constraints. A blocked opportunity
is counted once even if it fails multiple constraints.

When the new field is absent or null, canonical configs and hashes are unchanged.
Old filter audits omit the new volume field and retain the previous reason-key
set. New diagnostic reason keys are included only for active volume constraints,
including the zero-opportunity result case. Off and legacy paths retain their
event stream and ledger behavior. Existing UI import/export uses the same
backend schema and exposes the field in the existing entry-filter section.

## Verification

Tests include the exact 2.000/2.001 boundary through the BUY gate; missing,
NaN and infinities; SELL bypass for I/J/K; both frozen K constraints and
overlapping reasons; invalid config fields; old canonical configs and audit
shape; stored pass/block diagnostics; current-row/future-row separation;
shared MarketFrame immutability; and stale quarantine wallet/risk preservation.

OFF/default/null/missing control core is compared exactly against the independent
unchanged be08107 fixture already maintained by v1 (actual institutional
strategy, synthetic tape, nonempty control). A solo, A+I/J/K and reversed K/J/I/A
match that control across trades/orders/fills/timestamps/cash/equity. Full and
checkpoint-resumed I/J/K runs match all core records and complete summaries.
This is deterministic temporary-DB verification, not a historical 2024 rerun.

Desktop 1440px and mobile 390px tests import the actual 2023 artifact, verify
I/J/K values and backend roundtrip, no overflow, no JavaScript errors and no
implicit POST /api/replay/runs or run creation.

Test/lint logs and config validation evidence are saved under
`artifacts/institutional-entry-v2/` in the feature worktree.

Final results: **714 passed** in 205.26 seconds, including **23 new v2 cases**.
The v2 targeted suite passed all 23 cases. Maintained Ruff
(`ruff check crypto_bot tests research`) and diff whitespace checks pass.
Global Ruff reports the same four legacy I001 errors as unchanged production.
The first full-suite attempt had one missing-fixture-import failure (713 passed);
the test import was repaired, then targeted and full suites passed.
The old v1 public config was independently normalized by the unchanged production
validator and the new validator: full snapshots and its original
`4ee7e7d346400230a03263dbe70cff0000132ec4eb08257b479af79380d317db`
config hash remain identical.

## Scope and release

No migration is required. Production scanner/score, ReferenceStrategy,
institutional strategy, RiskManager, PortfolioService, paper execution and
all exit policies remain untouched. The only engine changes are within the
replay-only gate's audit and diagnostic-default handling. Shared market context,
no-lookahead timing and stale valuation semantics are unchanged.

Implementation is review-only: no deployment, no service restarts, no annual
validation run and no automatic promotion. Existing 2025 config and production
DB/cache files were not modified.
