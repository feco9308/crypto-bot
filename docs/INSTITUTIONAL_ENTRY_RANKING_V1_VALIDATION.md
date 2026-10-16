# Institutional Entry Ranking Research v1

Research-only replay implementation: **Explicit Candidate Ranking / Opportunity Selection**. NO LIVE TRADING. No deployment, annual replay, production database operation or production service restart.

## Revision / handoff

- Branch: `feature/institutional-entry-ranking-v1`
- Base SHA: `51e11378b953be9e026ba52c03c2e652b19759ee` (`feature/institutional-entry-research-v3`).
- Final implementation SHA: `7ed3ce226426497e406eb5707fcbeaf184966e2e` (all executable code, config and tests).
- Final review HEAD is the documentation commit containing this report, resolving with `git log -1 --format=%H -- docs/INSTITUTIONAL_ENTRY_RANKING_V1_VALIDATION.md`. Its exact SHA is recorded after commit in `artifacts/institutional-entry-ranking-v1/review-ready.json` and the final handover. A file cannot contain its own commit hash literally without changing that hash.

## Config and frozen formulas

Variant-level `entry_ranking` accepts only OFF, market_score_desc_v1 and acceleration_efficiency_v1. Active policies require institutional_style_v1. Missing/OFF is omitted from the normalized snapshot and from legacy public exports: old config/hash semantics are preserved. The normal backend validator, UI and JSON import/export use the same field.

- **OFF:** original pending signal order and execution sorting; ranking helper is never invoked. No new audit, result or checkpoint fields.
- **market_score_desc_v1:** Market Score descending, symbol ascending. This is the explicit legacy control.
- **acceleration_efficiency_v1:** `delta_4h / relative_volume` descending, then Market Score descending, then symbol ascending. No thresholds, coefficients, clamping, preselected top-N or parameter search.

Inputs use the existing finite Decimal canonicalization. Ordering compares exact rational values made from those Decimals, avoiding both binary floating-point and global Decimal-context rounding. Audit `ranking_value` is a deterministic 50-digit Decimal representation of the exact value; the display precision never controls sorting. Zero and negative finite delta values are not clamped.

For acceleration ranking, missing/non-finite delta or volume, or volume <=0, produces `ranking_value=null` and `ranking_value_available=false`. The already accepted BUY remains in the list, behind every valid priority; unavailable candidates sort by score then symbol. Unavailable numeric audit inputs are null, not fabricated values. The score-only policy uses its own feature, Market Score, independently of delta/volume availability. Existing strategy prerequisites remain unchanged; ranking cannot make an invalid strategy snapshot eligible.

## Pipeline / isolation

`Variant.signals` first evaluates the original strategy, admission/rearm/BTC gates and entry filters. Only then does active ranking collect and order the entire per-variant BUY list. No candidate is dropped or preselected because of rank. Existing execution processes all candidates and checks the actual post-SELL/post-earlier-fill portfolio through the unchanged institutional correlation checks, RiskManager and ReplayExecution.

SELLs keep their relative legacy order and execute before BUYs, using the existing execution sort. SELLs bypass ranking entirely. Held-only (`auto_eligible=false`) exit rows do not enter the ranked pool. A defensive BUY unexpectedly supplied on a held-only row is retained unranked, rather than silently changing eligibility; normal held-only rows are SELL/HOLD because the strategy observes the existing holding.

Ranking reads only the current accepted signal and its current scanner row; it reads neither future archives nor another variant's holdings. It copies rows before attaching temporary audit metadata. It does not mutate signals, shared MarketFrame, score history, strategy output, risk inputs or portfolio state.

## Audit schema / trade linkage

Each active BUY candidate emits ENTRY_CANDIDATE_RANKED with:

- UTC timestamp, symbol, ranking_policy, rank (1-based), candidate_pool_size;
- ranking_value, ranking_value_available, market_score, delta_4h, relative_volume;
- original_strategy_decision;
- entry_candidate_id, entry_rank, entry_candidate_pool_size, entry_ranking_policy, entry_ranking_value, entry_ranking_value_available.

The candidate ID is the signal's UTC timestamp plus symbol, scoped by the existing run ID and variant columns. Subsequent STRATEGY_SIGNAL, RISK_DECISION, RISK_REJECTED, NO_FILL and BUY ENTRY_FILL audit events carry the same entry metadata. A successful ENTRY_CANDIDATE_RANKED event is also linked to order_id and position_id. Rejected candidates retain their exact existing reason and their rank; the next candidate is still processed.

Ranking metadata deliberately stays in the audit projection, rather than changing stored position entry_data, order/fill or financial trade records. This permits literal A-vs-S equality of those records, while supporting trade-level opportunity-cost analysis by joining ENTRY_CANDIDATE_RANKED on run_id, variant and position_id (or candidate ID for rejected decisions). S adds only the documented ranking audit; removing the new event/metadata restores the exact legacy audit stream.

No persistent ranking state is introduced. Temporary pending-row metadata is consumed within the hour; no additional checkpoint fields are necessary.

## Parity and safety evidence

`tests/fixtures/institutional-entry-ranking-control-51e1137.json` was captured independently using the unchanged production/base code at exact SHA 51e1137, a temporary database and fixed synthetic Tape. It contains the baseline trades/orders/fills/curve/cash/equity, full audit, variant checkpoint, result and legacy config hashes. It is not generated by the modified implementation under test.

Tests verify:

1. Missing/OFF ranking reproduces that baseline bit-exactly, including audit, checkpoint, result and old hash semantics.
2. A OFF and S score-control trades/orders/fills/timestamps/cash/equity, result and checkpoint are exactly equal. S audit has only the intentional ranking additions.
3. An additional non-vacuous competition fixture has two simultaneous valid BUY candidates competing for one slot: A/S select NEARUSDT; R selects OPUSDT by the frozen efficiency formula. Financial sizing, ledger methods and execution prices remain unchanged.
4. A solo / A+S+R, R solo / mixed companions, reversed variant order and checkpoint/resume preserve their respective outputs and audit/checkpoints exactly.
5. High rank claims a slot first; invalid-stop rejection and no-fill do not suppress subsequent candidates. Correlation rejection after an earlier fill still allows the next candidate to take the remaining slot.
6. SELL bypass, same-timestamp SELL-before-BUY, immutable shared frames/history, held-only rows, old config import/export, unavailable ranking features, zero/negative priority and exact ties.
7. Changing actual future candle OHLC/volume after a fixed cutoff cannot change the ranked prefix's checkpoint, audit or orders. Fake future metadata is ignored.
8. Stale quarantine cash, reservations, marks and risk view remain unchanged by observation/ranking.
9. Desktop (1440px) and mobile (390px) config import/export round-trip, no horizontal overflow, no POST run creation. Browser screenshots are `/tmp/institutional-entry-ranking-1440.png` and `/tmp/institutional-entry-ranking-390.png`.

## Checks

**772 tests passed** (733 existing + 39 new), full maintained suite, 238.68 seconds. All 39 new targeted cases passed, including desktop/mobile. Maintained Ruff and git diff --check passed. No failures remain.

Commands in the isolated feature worktree:

```sh
PYTHONPATH=. LD_LIBRARY_PATH=/home/ndvi/.local/lib/crypto-bot-browser/usr/lib/x86_64-linux-gnu /home/ndvi/crypto-bot/.venv/bin/pytest -q
/home/ndvi/crypto-bot/.venv/bin/ruff check crypto_bot tests research
git diff --check
```

The repository-wide lint baseline contains four existing I001 import-order errors in backtest.py, legacy/backtest.py, legacy/dashboard.py and legacy/trading_bot.py. Maintained Ruff is the existing crypto_bot/tests/research scope; unrelated legacy files are not changed.

## Ready config and methodology

Backend-validated public JSON: `docs/replay/institutional-entry-ranking-v1-2024-exploration.config.json`.

Exactly A OFF, S market_score_desc_v1, R acceleration_efficiency_v1. All use institutional_style_v1 + baseline_v1, entry_admission OFF, setup_rearm OFF and neutral entry_filter. Strategy/risk/exit settings match the v3 A baseline: initial 1000, risk/trade 0.5%, max positions 3, total/symbol exposure 50%/20%, daily loss/max DD 100%/100%, fee/slippage 0.1%/0.05%, no pyramiding. Historical Top20, backend-derived ISOLATED_TOPN_V1, RESEARCH_QUARANTINE_STALE, fallback_5m=false, conservative=true, 2024-01-01 through 2025-01-01, EXPLORATION. No delta15, volume2 or fresh admission gate.

2024 and 2025 informed this hypothesis and are exploration, not clean validation. No 2023/2025/2026 run is started. 2023 may be considered separately as clean validation; 2026 remains final out-of-sample.

## Files and production impact

- New: crypto_bot/replay/entry_ranking.py
- Modified: crypto_bot/replay/config.py, crypto_bot/replay/engine.py
- Modified: crypto_bot/web/static/replay.js, crypto_bot/web/templates/replay.html
- New: tests/test_institutional_entry_ranking_v1.py, tests/test_institutional_entry_ranking_v1_browser.py
- New: tests/fixtures/institutional-entry-ranking-control-51e1137.json
- New: docs/replay/institutional-entry-ranking-v1-2024-exploration.config.json
- New: this validation document

No database migration or dependency change. No modifications to production scanner, paper strategy, RiskManager, Market Score, PortfolioService, existing exit policies, replay execution math or live execution. No production checkout changes, deployments, service restarts or annual replay runs. Only deterministic short test fixtures run against temporary databases.
