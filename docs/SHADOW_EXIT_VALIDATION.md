# Shadow Exit Research — deployment and 2024 validation

MATCHED-ENTRY EXIT COMPARISON. NOT PORTFOLIO PERFORMANCE.
NOT STATISTICALLY VALIDATED. NO LIVE TRADING.

## Implementation and isolation

Implemented on `feature/shadow-exit-research`; deployed by fast-forward to
`feature/trading-core-paper`. Previous production HEAD: `1dab7f4`.
Core commits: `f1fb41c` and `3067ca7`; subsequent UI fix wraps long progress
metadata on mobile without changing computation.

The selected completed anchor is read once into immutable canonical FILLED BUY
snapshots. Independent plain shadow positions reuse registered exit functions.
No shadow account, cash curve, equity curve, risk decision, position slot or
entry generation exists. The source anchor is not rerun or edited.

See [architecture, configuration and endpoints](SHADOW_EXIT_RESEARCH.md).

## Exact annual run

- Anchor: `04c2ef76ceac41199a922ae55480d16b`, variant 0.
- Successful shadow run: `6d4cc0c8babc41abb0f2b530d93bca10`, COMPLETED.
- Period: 2024-01-01T00:00:00Z through 2025-01-01T00:00:00Z.
- Dataset: EXPLORATION; `RESEARCH_QUARANTINE_STALE`; conservative intrabar.
- Strategy/risk/fee/slippage/settings inherited exactly from anchor.
- 433 canonical entries; four policies; 1,732 shadow outcomes.
- Engine elapsed time: 81.077 seconds.
- 430 entries are complete and closed across all four policies.

| Policy | Parameters | Complete closed | Open | Stale | Incomplete outcomes |
| --- | --- | ---: | ---: | ---: | ---: |
| baseline_v1 | Original baseline | 430 | 3 | 1 | 3 |
| fixed_tp | tp_pct=1.5 | 433 | 0 | 0 | 0 |
| r_tp | tp_r=0.5 | 433 | 0 | 0 | 0 |
| trailing_pct | activation_pct=1.5, distance_pct=0.5 | 433 | 0 | 0 | 0 |

Open/stale/incomplete counts overlap; the single stale outcome is one of the
three open/incomplete baseline outcomes. The other policies closed that entry
before data disappeared.

All 430 source baseline CLOSED trades match shadow baseline exit timestamp,
exit fill price and net realized PnL exactly. All four policies preserve every
canonical timestamp, fill price, quantity, notional, original stop, entry fee,
entry slippage and risk amount. The three original open trades remain open in
the shadow baseline.

FRONTUSDT became OPEN_STALE / SHADOW_OPEN_STALE at
2024-08-27T03:00:00Z. Last valid mark is 0.88, observed at that clock boundary.
It never resumed or received an invented exit; end-of-run stale duration is
10,962,000 seconds. Its unrealized last mark is excluded from realized results
and paired win/loss statistics. Full stale events are separately exported.

## Pairwise accounting

| A | B | Matched | A wins | B wins | Ties | Incomplete |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| baseline | fixed_tp | 430 | 61 | 250 | 119 | 3 |
| baseline | r_tp | 430 | 61 | 198 | 171 | 3 |
| baseline | trailing_pct | 430 | 63 | 248 | 119 | 3 |
| fixed_tp | r_tp | 433 | 87 | 230 | 116 | 0 |
| fixed_tp | trailing_pct | 433 | 284 | 30 | 119 | 0 |
| r_tp | trailing_pct | 433 | 239 | 78 | 116 | 0 |

For every pair, wins + losses + ties equals matched; matched + incomplete
equals 433. Primary policy metrics use the same 430-entry common intersection.
Per-policy available samples and pair-specific samples are separately labeled.
Return uses entry notional plus entry fee; R uses canonical risk; normalized
profit factor uses return percentages. No best-policy ranking is inferred.

## Verification

Full maintained suite: **429 passed**, including **33 new tests**;
`ruff check crypto_bot tests` clean. A broader `ruff check .` also checked
unmaintained legacy scripts and reported four pre-existing import-order issues
in `backtest.py` and `legacy/{backtest,dashboard,trading_bot}.py`; those unrelated
files were left unchanged. The full suite was repeated after the
mobile progress-wrap fix. Tests cover canonical equality, anchor isolation,
unchanged next BUY, policy-order independence, fee/slippage, conservative and
optimistic intrabar handling, future/pre-entry candle exclusion, stale/resume,
no invented end liquidation, minute/fallback boundaries, paired completeness,
immutable storage, CSRF/validation/no implicit start, exports and browser UI.
Pinned negative-cache and revision mismatch tests prevent silent archive
refresh; a job-local failure test verifies that a failed research job cannot
stop the replay worker or alter the anchor.

Only closed minute bars and completed hourly contexts are observed; the
strategy held-position branch receives no future candles. Hourly boundary
execution uses the current boundary open consistent with normal replay.
Fallback bars cannot duplicate prior minute observations. Future archive
coverage does not trigger a liquidation.

Production result browser checks: 1440px and 390px, four summary rows, six
pairwise rows, canonical count, group selectors, no JavaScript errors and no
document horizontal overflow. Screenshots are in the artifact directory.
The annual long progress text exposed a mobile overflow not present in the
small fixture; a scoped wrapping fix and matching regression fixture address it.

Source database row counts AND SHA256 hashes are unchanged for all anchor
trades (433), orders (863), fills (863), audit events (25,405), and equity
curve points (8,785). Exact hashes are in `validation.json`. Paper generation,
trading ON, cash/equity, realized PnL and open positions were preserved; `.env`
hash unchanged. Paper had zero open positions and cash/equity
948.211234726629 at both checks.

## Deployment and operational limits

Only web and the idle replay worker were restarted for deployment; the final
documentation commit requires no restart. Scanner PID 1333 and paper PID 1334
remain unchanged. Existing market/replay schema and audits were not migrated
or reset. New isolated `data/shadow-exit.db` initializes schema 1.

First attempt `1a2486a4dfc04c3b8bdb7ac94fb28f11` failed during archive setup
before any outcome: refreshing an expired missing archive entry timed out at
the public data server. That failed job is retained. The pinned archive adapter
now honors the anchor manifest including known missing objects; differing
known revisions fail explicitly. The new run completed on commit `3067ca7`.

An already-running shadow job may delay newly queued ordinary replay jobs.
This is analysis conditional on the anchor entry stream, not an independently
funded portfolio backtest. OHLC execution remains approximate. Data gaps make
affected outcomes incomplete even if later resumed. Missing historical values
are not fabricated. 2024 exploration establishes no statistical validation,
production promotion or optimal per-symbol exit.

## Changed files

New modules:

- `crypto_bot/replay/shadow_config.py`
- `crypto_bot/replay/shadow_data.py`
- `crypto_bot/replay/shadow_engine.py`
- `crypto_bot/replay/shadow_metrics.py`
- `crypto_bot/replay/shadow_store.py`
- `crypto_bot/replay/shadow_worker.py`
- `crypto_bot/web/shadow.py`
- `crypto_bot/web/static/shadow.js`
- `crypto_bot/web/static/shadow_result.js`
- `crypto_bot/web/static/shadow_result.css`
- `crypto_bot/web/templates/shadow.html`
- `crypto_bot/web/templates/shadow_result.html`
- `tests/test_shadow_exit.py`
- `tests/test_shadow_browser.py`
- `docs/SHADOW_EXIT_RESEARCH.md`
- `docs/SHADOW_EXIT_VALIDATION.md`

Integration edits:

- `crypto_bot/replay/worker.py`: isolated queue after normal queue is idle.
- `crypto_bot/web/replay.py`: register shadow routes.
- `crypto_bot/web/templates/replay.html`: research navigation link.
- `README.md`: research documentation.

Production RiskManager, PortfolioService, ReferenceStrategy, scanner/score,
paper execution, live execution and existing replay exit policies are unchanged.

## Exports and reproducibility

Directory: `/home/ndvi/crypto-bot/artifacts/shadow-exit-2024/`.

- `2024-shadow-exit.compact.json`: metadata/config/summaries/pairs/breakdowns.
- `2024-shadow-exit.detailed.json`: also canonical entries/outcomes/events.
- `stale-outcomes.json`: separate FRONT stale outcome and event.
- `validation.json`: parity, source checksums, metrics and export hashes.
- `config.json`, `run.json`, `before.json`, `failed-attempt.json`.
- `shadow-result-1440.png`, `shadow-result-390.png`: production screenshots.

Artifact files and databases are deliberately ignored by Git. Export SHA256:

- `2024-shadow-exit.compact.json`: `f9d6bf456695a8823eb4f7788b8eaae0f6465fb7cf3d35a587f66bb4ac7821d9`.
- `2024-shadow-exit.detailed.json`: `75a2cb58bc46a022649bcab55ff43a85fbd7c270c51875dfab9648ae5ae706f6`.

Anchor entries hash: `2532623ca65fb3b5498d40ac1d9898d19f72beacceddd945bcb4fe0e72a59559`.
Shadow config hash: `195a70352c5993b92c7a96f54e24b9cb14cb1e0335c8731c149390fe461e1e6a`.
Outcomes hash: `16e8b3fbd32f2c5ab05338dd20fea2dd74a7d533a52eab932f56db009afc6419`.

Exports record run code commit `3067ca7`, app 0.2.0, replay/shadow schema 1,
archive revisions, immutable configuration, no-lookahead/intrabar/stale
settings. The later UI/documentation change does not alter these outcomes.
