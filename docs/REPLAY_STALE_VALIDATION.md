# Stale-position quarantine: 2024 validation

Validated/deployed on 2026-10-08. This is historical replay research, not a
production trading change or strategy optimization.

## Configuration and deployment

- Production branch: `feature/trading-core-paper`.
- Development branch: `feature/replay-stale-quarantine`.
- First deployed completed blocked-entry observability (`dc4d742`, `ae0dfeb`),
  then quarantine implementation `63472cb` and valuation documentation `d31b17f`.
- Source run: `8afd54494906450180e7eea8c012dd19`.
- Completed research run: `04c2ef76ceac41199a922ae55480d16b`.
- Name: `2024 Exploration - 4 Exit Comparison - Research Guards`.
- Period: 2024-01-01 through 2025-01-01 UTC; historical Top 50;
  `watchlist_reference_v1`; conservative intrabar handling; strict no-lookahead.
- Exact expanded configuration equality was checked after removing only
  `stale_position_policy`. The 693 resolved archive candidates also match.
- New policy: `RESEARCH_QUARANTINE_STALE`; default remains `STRICT_FRESH_MARKS`.
- Fee, slippage, strategy, exit parameters and risk limits were not changed.
- Execution through the normal run API; completed in 2949.709 seconds.

Only web and the isolated replay worker were restarted, while idle. Scanner PID
20305 and paper PID 96297 remained unchanged. Deployed web PID: 138465; replay
worker PID: 138458. No DB migration, account reset or historical audit rewrite.
The replay-worker warning/error journal for this run was empty. Final report and
test-fixture edits do not require additional restarts.

## Results

| Exit policy | Closed trades | Final reported equity (USDT) | Return | Open / stale |
| --- | ---: | ---: | ---: | ---: |
| baseline_v1 | 430 | 699.021520825272 | -30.097848% | 3 / 1 |
| fixed_tp | 4877 | 164.129621570242 | -83.587038% | 0 / 0 |
| r_tp | 3704 | 177.670898132522 | -82.232910% | 0 / 0 |
| trailing_pct | 4047 | 77.231715645740 | -92.276828% | 0 / 0 |

**RESULT CONTAINS STALE OPEN POSITIONS. Final equity contains stale valuations
and must not be interpreted as a fully liquidatable portfolio value.**

Baseline metrics are retained but incomplete/stale-data affected and excluded
from ranking. Comparison and result pages display the warning. No automatic
best-variant selection is made.

Baseline's old `other = 5000` consisted of 4999 global missing-position-mark BUY
blocks and one missing execution minute. Research now has `other = 1`, exactly
`MISSING_1M_EXECUTION_CANDLE; fallback disabled`; no missing-mark global blocks.
There are still 11407 normal maximum-position rejections: stale FRONT retains a
position slot. All four reason breakdowns reconcile with zero unresolved attempts.
Baseline completed 430 trades versus 340 originally and opened 92 unrelated
positions after FRONT became stale. This does not make its valuation complete.

## Separate stale event

Only one stale event occurred, in baseline:

- Symbol: FRONTUSDT; position 338; final status **OPEN_STALE**.
- First stale observation: **2024-08-27T03:00:00Z**.
- Last valid mark: **0.880000000000**, observed at that same replay boundary
  from the preceding completed minute. No future coverage metadata was used.
- Quantity: 61.072287825932.
- Duration: **10962000 seconds** (126 days, 21 hours).
- Capital locked: **45.628289388185 USDT**.
- Exposure locked: **53.743613286820 USDT**.
- `resumed_after_stale = false`; no resume, fabricated/retroactive fill, token
  conversion or last-price/end-of-run liquidation.
- Other three variants had no stale events or stale holdings.

## Exact valuation semantics

See [the policy specification](REPLAY_STALE_POLICY.md) for the complete equations.
For each stale position, C is entry notional plus entry fee and L is quantity
times the last valid mark. F is fresh positions' marked value.

- Reported equity = cash + F + sum(L), explicitly including stale valuations.
- Risk-sizing equity = cash + F + sum(min(L, C)): stale profits cannot increase
  buying power; stale losses remain included.
- Risk exposure = F + sum(max(L, C)): stale losses cannot release exposure below
  invested capital.
- Available cash remains actual cash minus pending-order reservations. Stale
  capital was already spent; neither cash nor position slots are released.

Final baseline reported equity is 699.021520825272 versus conservative sizing
equity **690.906196926637**. The 8.115323898635 stale gain is withheld from sizing.
Cash is 438.155636270795 and reported/risk exposure is 260.865884554477.
Production RiskManager and PortfolioService calculate with their existing
semantics; only replay constructs the conservative input view.

## Comparison coupling discovered during validation

All four variants' raw fills match the source byte-for-byte through
2024-07-22T03:00:00Z. Trailing's entire 8094-fill sequence matches byte-for-byte
over the full year. Fixed TP and R-TP each finish with two more closed trades.

The small differences have a proven cause in the existing engine: scanner
contexts/score history cover Top N **plus the union of symbols held by any
variant**. The unfrozen baseline therefore retains additional history outside
Top 50, making momentum available to other variants when a symbol re-enters.
This pre-existing shared-history coupling was not modified by this work.

The first extra fill for both overlays is VIDTUSDT at 2024-08-28T09:00:00Z:

| UTC observation | VIDT in Top 50 | Score | Held in original / research |
| --- | --- | ---: | --- |
| 2024-08-28 05:00 | no | 61.22240262841504 | none / baseline |
| 2024-08-28 09:00 | yes | 71.3550022397404 | none / baseline and both overlays |

The research run has the earlier baseline-held context and therefore a genuine
observed delta of 10.132599611325375. The frozen source run lacks that context.
Later entries, balances and sizing diverge accordingly. Strategy, exit rules,
Market Score formulas and production behavior remain unchanged. Interpret this
as a limitation of the existing multi-variant comparison architecture; isolating
variant history would be separate research work, not part of this change.

## Verification

- Full maintained suite: **395 passed**, including **21 new tests**.
- Targeted stale regression suite after consistent resume-candle fixture cleanup:
  **19 passed**; application code unchanged after the full-suite run.
- `ruff check crypto_bot tests`: passed. Repository-wide Ruff retains four
  pre-existing I001 errors in legacy scripts; those scripts were not changed.
- Tests cover strict parity, unrelated trades during quarantine, conservative
  stale gain/loss treatment, capital/exposure/slot retention, resume, missing
  future data invariance, no retroactive/forced fills, checkpoints, OPEN_STALE
  exports, comparison warnings/ranking exclusions and unchanged exit policies.
- Config import/export browser regressions exercise the shared policy field.
- Production completed result and four-variant comparison checked in Chromium
  at desktop 1440px and mobile 390px; warnings/ranking exclusion are visible,
  comparison has no page overflow and no browser JavaScript errors.

## Changed files and scope

New files: `crypto_bot/replay/stale.py`, `tests/test_replay_stale.py`,
`tests/test_replay_stale_browser.py`, `docs/REPLAY_STALE_POLICY.md`, this report.

Modified replay files: `config.py`, `engine.py`, `exporter.py`, `metrics.py`,
`models.py` under `crypto_bot/replay/`.

Modified web files: `crypto_bot/web/replay.py`; static `replay.js`,
`replay_compare.js`, `replay_results.js`, `style.css`; templates `replay.html`,
`replay_compare.html`, `replay_run.html`, `replay_trade.html`.

Other modified files: `README.md`, `docs/HISTORICAL_REPLAY.md`,
`tests/test_replay_config_browser.py`.

Production RiskManager, PortfolioService, ReferenceStrategy, scanner/score,
paper execution and production settings were **not touched**. NO LIVE TRADING.

## Export artifacts

The four compact JSONs and separate events/proofs/screenshots are retained in
`artifacts/replay-stale-2024/` in the production checkout (ignored by Git):

- `1-baseline_v1.compact.json`
- `2-fixed_tp.compact.json`
- `3-r_tp.compact.json`
- `4-trailing_pct.compact.json`
- `stale-events.json`: separate stale event export.
- `validation.json`: full metrics, reconciliation, config/catalog checks,
  byte sizes and SHA-256 hashes of the compact exports.
- `prefix-parity.json` and `shared-history-proof.json`: comparison evidence.
- `result-1440.png`, `result-390.png`, `comparison-1440.png`,
  `comparison-390.png`: production browser validation.

The same four exports remain available via the existing
`/api/replay/runs/04c2ef76ceac41199a922ae55480d16b/export.compact.json?variant=N`
endpoint, with N from 0 through 3. No new config format or trading API is used.
