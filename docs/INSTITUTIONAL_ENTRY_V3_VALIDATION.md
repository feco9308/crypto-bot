# Institutional Entry Research v3 — Fresh Setup Admission

Research-only hypothesis implementation. NO LIVE TRADING. No deployment or annual replay has been performed.

Base: `a34a4238f308c5efef6bb831e3a88ce994b44eb9`.
Branch: `feature/institutional-entry-research-v3`.

## Configuration

`docs/replay/institutional-entry-v3-2024-exploration.config.json` contains A and L only, for 2024-01-01 through 2025-01-01, EXPLORATION, historical Top20, backend-derived ISOLATED_TOPN_V1, RESEARCH_QUARANTINE_STALE, conservative minute execution and no 5m fallback.

A retains the existing institutional baseline. L adds `entry_admission: "fresh_setup_admission_v1"`. All strategy, risk and exit parameters are identical. Both use baseline_v1; both entry_filter objects are empty. No delta15 or volume2 filter is applied. The strategy's existing momentum and volume conditions remain unchanged.

The optional admission field defaults to OFF. OFF/absent is omitted from normalized snapshots, maintaining old canonical configs and hashes. The existing public validator and UI import/export handle the field; it is accepted only for institutional_style_v1.

## Observation and admission

The observer calls the unchanged InstitutionalStrategy with `has_position=False` and an independently constructed feature mapping with neutral correlation. It therefore observes the entire institutional entry predicate (watch membership, BTC, required timeframes, score/momentum, RSI, volume, spread and valid initial stop), without holdings, correlation, cash, exposure or RiskManager decisions.

Only exogenous TopN snapshot rows are observations. Portfolio-dependent, held-only exit rows (`auto_eligible=false`) cannot seed a FALSE, arm or rearm. They still pass through ordinary position/SELL processing. No shared row or MarketFrame is changed.

| Previous observable state | Current observation | Admission |
|---|---|---|
| UNKNOWN | TRUE | blocked; an observable FALSE is required first |
| FALSE | TRUE | allowed once, only at this snapshot timestamp |
| TRUE | TRUE | blocked |
| TRUE | FALSE | reset; no BUY |
| FALSE after prior setup | TRUE | new opportunity / rearmed |
| any | missing/unobservable | retain last observable state; no fresh admission |

There is no pre-start admission seeding. Missing symbol rows, unavailable required timeframe/BTC data and required missing momentum do not become FALSE. An observation gap retains the last observable state: observed FALSE → gap → observed TRUE can arm; observed TRUE → gap → observed TRUE cannot. The implementation reads neither future snapshots nor archive end dates.

Once the first TRUE snapshot passes admission, a BUY attempt consumes that opportunity before risk/execution. Rejection or missing execution data never carries it forward. A correlation rejection or existing holding at that transition also consumes the opportunity. A fill records consumption; closing a position does not reset setup state. Re-reading the same snapshot cannot create another attempt.

SELL decisions bypass the admission gate. Existing exits, stale quarantine, portfolio accounting, risk sizing, scanner, score and production paper code are unchanged.

## Audit and checkpoints

FRESH_SETUP_ARMED, FRESH_SETUP_REARMED, ENTRY_NOT_FRESH_SETUP and FRESH_SETUP_MISSED_CAPACITY are persisted in the ordinary variant audit stream, with symbol, UTC timestamp, previous/current observable state, setup_true_since, setup_age_hours and the original unchanged strategy decision. Observation events also identify whether setup was observable. Missed opportunities retain the actual rejection/no-fill reason; the MISSED_CAPACITY event also covers missing execution data and records that distinction rather than inventing a fill.

ENTRY_NOT_FRESH_SETUP is included in the existing blocked-entry reason/category reporting. Missed-opportunity annotations do not double-count ordinary risk/no-fill rejections.

State, timestamps, attempted/filled/missed flags and setup generation count are included in each enabled variant's existing checkpoint JSON. No schema migration or production database access is needed.

## Verification

Deterministic tests exercise transitions, missed max-position/exposure/cash capacity using the real RiskManager, missing execution data, existing-position exits, SELL bypass, missing setup/momentum, held-only rows, stale reservations, immutable shared MarketFrame, legacy/OFF normalization and config round-trip.

The A ledger/trades/orders/fills/timestamps/cash/equity match the independently captured pre-filter institutional golden control. A solo matches A+L and reversed L+A. Both variants match uninterrupted versus checkpoint/resume executions. Desktop and mobile browser tests validate import/export without creating a run.

Final results: **733 tests passed** (714 existing + 19 new), including desktop/mobile browser tests. Final full run: 221.34 seconds. Maintained lint: `ruff check crypto_bot tests research` passed. `git diff --check` passed. Repository-wide `ruff check .` reports the same four pre-existing I001 errors as the unchanged production base (`backtest.py`, `legacy/backtest.py`, `legacy/dashboard.py`, `legacy/trading_bot.py`); these were not changed.

Commands (in the feature worktree, using the existing shared virtualenv):

```sh
PYTHONPATH=. LD_LIBRARY_PATH=/home/ndvi/.local/lib/crypto-bot-browser/usr/lib/x86_64-linux-gnu /home/ndvi/crypto-bot/.venv/bin/pytest -q
/home/ndvi/crypto-bot/.venv/bin/ruff check crypto_bot tests research
git diff --check
```

Changed files:

- `crypto_bot/replay/fresh_setup.py` (new observer/state machine)
- `crypto_bot/replay/config.py` (optional replay admission config)
- `crypto_bot/replay/engine.py` (replay admission/audit/checkpoint integration)
- `crypto_bot/replay/rejections.py` (reason reporting)
- `crypto_bot/web/static/replay.js` (config import/export)
- `crypto_bot/web/templates/replay.html` (research admission selector)
- `tests/test_institutional_entry_v3.py` (17 deterministic unit/integration cases)
- `tests/test_institutional_entry_v3_browser.py` (desktop/mobile cases)
- `docs/replay/institutional-entry-v3-2024-exploration.config.json`
- `docs/INSTITUTIONAL_ENTRY_V3_VALIDATION.md`

Production checkout remained clean at a34a4238f308c5efef6bb831e3a88ce994b44eb9. No services were restarted, no production databases were changed, no annual run was created, and no thresholds were optimized. No changes to ReferenceStrategy, InstitutionalStrategy, RiskManager, PortfolioService, scanner, score, existing exits or stale handling.
