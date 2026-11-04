# BTC Mean Reversion 2023 Clean Validation v1

Exploration base: `37811156c7a5916e42fba1924945d66bf8980944`.
Branch: `research/mean-reversion-2023-clean-validation-v1`.

This is one confirmatory consumption of the 2023 clean cohort. Only the frozen
S2 BTCUSDT Bollinger 20/2 candidate is admissible. No other asset, strategy,
portfolio, threshold, filter or parameter is evaluated. No production runtime,
configuration, database or service is modified. No 2026 data or final OOS runs.

## Preregistration before outcomes

The candidate comes from the checksum-pinned Orthogonal v1 exploration ZIP:
`fe8f2b4c1f0cc8f01f141b5d53e02a86dd75889733c976d69c6a4a33709a8231`.
Its classification must be `ORTHOGONAL_CLASS_SIGNAL_FOUND`, class
`MEAN_REVERSION`, passing variant exactly `S2 BTC_ONLY`.
The existing 2024/2025 results are references only, never rerun or tuned.

The implementation/tests are committed and the worktree must be clean before
`prepare`. That action creates an exclusive, immutable
`candidate-provenance.json` BEFORE collecting missing source objects. It records
exact implementation HEAD, adapter SHA256, original strategy/accounting/parser
source hashes, cost constants, source plan and the four frozen PASS criteria.
After official checksum validation, `frozen-data-provenance.json` pins all 13
monthly archive hashes. Neither provenance file is overwritten after freezing.
Preparation validates data quality only; it computes no performance or strategy
outcome. A changed implementation, HEAD, screen or archive pin stops execution.

Before the first performance calculation, `validate` exclusively creates and
fsyncs `cohort-consumption.json` with `2023_clean_cohort_consumed: YES`.
All subsequent preparation/execution attempts fail. The marker persists for
PASS, FAIL and technical interruptions; it is never silently deleted or reset.
Gross/net primary calculations and independent audit are parts of the SAME
confirmatory execution, not alternative validation attempts.

## Frozen strategy and source contract

* BTCUSDT Spot 1d candles only, official Binance archive plus `.CHECKSUM`.
* Performance `[2023-01-01, 2024-01-01)` UTC, exactly 365 days.
* December 2022 is solely indicator warmup, with no 2022 position/PnL simulation.
* Only 13 monthly archives are read: BTC 2022-12 and BTC 2023-01 ... 2023-12.
  Existing complete objects are reused; only missing fixed archives may be
  downloaded during preparation. No third-party, authenticated or trading API.
* Original Spot parser, schema, OHLC validity, complete-month checks and exact
  UTC candle boundaries are preserved. There is no 2024 terminal candle/open.
* SMA20 is the arithmetic mean of 20 completed closes including decision close;
  sample std uses the same closes with `ddof=1`; lower = SMA20 - 2*std.
* Flat enters strictly below lower; equality does not enter. Long exits at
  close >= SMA20; equality exits. No extra rule, stop, overlay or gate.
* All fills use the next IN-COHORT daily open. No same-bar/close execution.
* Start flat with 1,000 USDT. Warmup may supply the first completed decision
  candle, but no preexisting warmup holding is inherited.
* Gross fee/slippage zero; net 0.10% fee and 0.05% adverse slippage per side.
* Remaining holdings mark to final completed 2023 close, less estimated sell
  costs. This is valuation only, not a SELL or completed episode.

## Exact reuse of frozen accounting

The original S2 `observation` function is called without changes. The original
`_simulate`, `reallocate` and `_reallocate` function CODE OBJECTS are reused.
Their isolated global namespaces restrict quote iteration to BTC alone, avoiding
both ETH data and fabricated inert ETH quotes. Original modules/globals/files
are never mutated. The original ledger, full-equity allocation, fee/slippage,
closed/open episodes, daily metrics and terminal accounting are unchanged.
The exploration's public 2024/2025 scope gate is replaced by an exact 2023-only
adapter; the strategy/accounting mathematics are not rewritten.

The original source-function hashes and exact implementation code hashes are
frozen before outcomes. Unit tests assert code-object identity, BTC-only scope
and unchanged original globals. Average/median holding time, return and win rate
use completed episodes only; terminal censored holdings cannot satisfy the count
minimum. Episode audit links entry/exit decision timestamps and actual gross/net
fills; open episodes have no exit and separate terminal valuation fields.

## Four immutable PASS criteria

ALL must hold; no MIXED category:

1. 2023 gross return strictly > 0.
2. 2023 net return strictly > 0.
3. At least five COMPLETED NET BTC position episodes.
4. All twelve leave-one-calendar-month NET results strictly > 0.

Leave-month audit deletes that month's unchanged NET daily equity returns and
compounds the rest. It never refits, alters indicators or reopens/replays signals.
Missing, duplicate or out-of-cohort months cannot satisfy criterion 4.

PASS -> `CONFIRMED_ON_2023_CLEAN_COHORT`, `final_oos_eligible: YES`.
FAIL -> `CLEAN_VALIDATION_FAILED`, `final_oos_eligible: NO`.
The 2023 cohort remains consumed in either case. No repair/tuning is permitted
from its result, and eligibility never starts a 2026 final OOS automatically.

## Independent validation and tests

The independent oracle computes its own float SMA20, sample standard deviation,
lower band, state machine and direct single-asset buy/sell cash formulas. It
calls neither the frozen strategy/allocator nor the primary simulator. It checks
365 signal/indicator observations, 730 daily gross/net cash observations, fill
times/prices, fees/slippage, closed episode counts and all leave-month results.
It evaluates only the SAME frozen BTC candidate and cohort.

Synthetic tests cover the exact cohort, warmup/terminal boundaries, absence of
inherited positions, inclusive sample window, strict/equality entry/exit,
completed bars, next-open timing, no debt/short, costs per executed side,
terminal estimate versus fill, episode audit, minimum closed count, all-four
screen, deterministic leave-month/artifacts, unchanged-code BTC binding and
permanent exclusive consumption. No real clean-cohort outcome is read by tests.

## Controlled execution

```bash
cd /home/ndvi/crypto-bot-clean-validation
# After committing reviewed/tested implementation, freeze provenance + data only:
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python \
  -m research.mean_reversion_clean_validation prepare \
  --root /home/ndvi/crypto-bot --capture
# Exactly one confirmatory execution, offline after checksum-pinned preparation:
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python \
  -m research.mean_reversion_clean_validation validate \
  --root /home/ndvi/crypto-bot
```

Output: `/home/ndvi/crypto-bot/artifacts/mean-reversion-2023-clean-validation-v1/`.
The required result, report, strategy/episode/monthly/leave-month/cost/execution
exports, immutable provenance, consumed marker and deterministic ZIP are kept
there. Raw new official cache objects stay outside Git and ZIP under
`data/research-cache/mean-reversion-2023-clean-validation-v1/`; existing valid
BTC 2023 cache objects are read in place without download/refresh.

Before any real 2023 performance computation: **27 targeted tests PASS**,
**1002 full maintained tests PASS in 242.11 seconds**, Ruff PASS and staged
`git diff --check` PASS. Browser tests use the already installed local libraries
via the test-process-only
`LD_LIBRARY_PATH=/home/ndvi/.local/lib/crypto-bot-browser/usr/lib/x86_64-linux-gnu`.
No production environment or dependency installation is changed.
