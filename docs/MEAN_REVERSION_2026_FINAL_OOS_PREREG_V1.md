# BTC Mean Reversion 2026 Final OOS Preregistration v1

Base: `4f8129668a2828b69fde5f148fad6149ac72bb10`.
Branch: `research/mean-reversion-2026-final-oos-prereg-v1`.

**This task is preregistration only. No real 2026 market or performance data is
opened, downloaded, inspected or transformed. The 2026 final OOS is NOT run.**
Only synthetic fixtures exercise the future implementation. No production
strategy, risk, scanner, account configuration or service is changed.

## Candidate provenance, checked before freezing

* Exploration commit: `37811156c7a5916e42fba1924945d66bf8980944`.
* Exploration artifact SHA256:
  `fe8f2b4c1f0cc8f01f141b5d53e02a86dd75889733c976d69c6a4a33709a8231`.
* Clean-validation preregistered implementation:
  `a05f58d7f62912c6a2caab9125a7565c0c09d4d3`.
* Clean-validation final HEAD:
  `4f8129668a2828b69fde5f148fad6149ac72bb10`.
* Clean artifact SHA256:
  `f50e24052dce33412b3f6a97d7bd9ef6e46f6e6b7a1f5a352cbe408a890ed99e`.
* Required prior result: `CLEAN_VALIDATION_PASS`;
  `CONFIRMED_ON_2023_CLEAN_COHORT`; `final_oos_eligible: YES`;
  `2023_clean_cohort_consumed: YES`.

Both original ZIP SHA256 values, CRCs and member hashes are checked. The exact
prior statuses and implementation HEADs are checked. Original strategy/accounting
source pins are extracted from the immutable clean artifact and must equal the
current functions. A mismatch is STOP, not a replacement candidate.

Historical references only: net +44.4327% / +26.7229% / +16.1190% in
2023 / 2024 / 2025; completed episodes 6 / 5 / 9; leave-month 12/12 / 12/12 /
11/11 positive. No source data from those years is rerun for tuning. The prior
2023 consumed marker is not changed.

## Exact frozen strategy and accounting

BTCUSDT only, Spot 1d, 1,000 USDT, long/cash, fractional quantity, no borrowing,
leverage or short. The original S2 observation function, Decimal accounting,
ledger, allocation math and BTC-only namespace binding are reused unchanged.
Code hashes include the strategy, accounting, ledger, allocator, parser,
independent oracle/verifier and new scope/guard module.

SMA20 includes the completed decision close among the last 20 completed closes.
STD20 is sample standard deviation, ddof=1. Lower band is SMA20 - 2*STD20.
Flat enters strictly below lower; equality does not enter. Long exits when
close >= SMA20; equality exits. Fills are only at the next in-cohort daily OPEN.
Each cohort starts flat without a position inherited from warmup.

There is no extra indicator, alternative asset, portfolio overlay, regime gate,
stop, time stop, cooldown, risk rule or parameter search. Gross has no costs.
Net costs are 0.10% fee and 0.05% adverse slippage per executed side. Full-equity
position sizing includes those costs and cannot borrow fees.

An open final position marks to the completed 2026-12-31 close, less estimated
sell fee/slippage. This is a valuation estimate, never a SELL or completed episode.
No 2027 market price or open is permitted. Closed-episode accounting and
leave-month definitions remain the same as in clean validation.

## Cohort and source plan, not source inspection

Performance: `[2026-01-01T00:00:00Z, 2027-01-01T00:00:00Z)` UTC.
Warmup: December 2025 only; no 2025 performance recalculation.
The plan contains exactly 13 official monthly Spot BTCUSDT 1d ZIP URLs and
13 `.CHECKSUM` URLs: 2025-12 and 2026-01 ... 2026-12. It expects 31 warmup and
365 cohort rows. The unchanged parser verifies 12 fields, microsecond timestamps,
completed UTC daily bars, OHLC validity, complete months and no gaps.

During preregistration even an existing 2026 cache must not be opened. The source
plan does not probe an archive, check archive availability or inspect a candle.
No real market cache is enumerated. Only the exact prior result ZIPs and code
files are read for provenance; the new artifacts contain protocol metadata only.

## Hard data and computation boundaries

Preregistration mode always raises
`FINAL_OOS_DATA_ACCESS_FORBIDDEN_DURING_PREREGISTRATION` before cache I/O, even if
the clock is mocked beyond the eligibility date. Final-execution mode raises
before any archive/cache access until UTC >= 2027-01-01. There is no CLI clock
override. Synthetic computation requires an explicit in-memory
`SyntheticFixture`; plain dictionaries or preregistration mode are not accepted
as compute authorization. Tests prohibit real network requests and any actual
production market-cache file access.

The future real-data loader requires a sealed source permit, issued only after
mode/date, external preregistration SHA256 anchor, exact HEAD/code pins and
unconsumed-state checks. It validates all 13 official ZIP/CHECKSUM pairs before
issuing a verified input bundle. Missing archives or checksum errors stop before
consumption or performance. Source capture is optional and explicit, only after
the time/identity gates; HTTP errors stop without a retry loop.

## Frozen execution eligibility gate

ALL conditions are required:

1. UTC >= 2027-01-01T00:00:00Z. This is not a promise that the December archive
   will already be published; archive completeness remains a separate gate.
2. All 13 exact monthly archives are available and complete.
3. Every official `.CHECKSUM` is available and matches its ZIP.
4. Current clean worktree HEAD equals the exact commit recorded by this prereg.
5. Strategy/accounting/oracle/adapter pins exactly match the preregistered pins.
6. No previous canonical `final-oos-consumption.json` exists.

The JSON artifacts are generated only AFTER committing the module/tests/docs,
so `implementation_HEAD` records that exact frozen commit. No later code or doc
commit may be substituted for execution. The SHA256 of `preregistration.json`
is an external anchor supplied explicitly to a separately authorized future
execution; it also prevents silently rewriting the artifact's expected HEAD.
The manifest pins all seven protocol JSON members. Re-emitting identical
preregistration artifacts is deterministic; changing frozen content is rejected.

Current artifact status is `NO_EXECUTION`, with archive/checksum availability
`NOT_INSPECTED_DURING_PREREGISTRATION`. No timer, background worker or automatic
execution is created.

## Four frozen final conditions

`FINAL_OOS_PASS` requires ALL, with no MIXED/PARTIAL/NEAR PASS:

1. 2026 gross return > 0.
2. 2026 net return > 0.
3. Completed NET BTC episodes >= 5; open/censored episodes do not count.
4. All twelve unchanged NET leave-one-calendar-month returns > 0.

Leave-month deletes each month's already computed NET daily returns and
compounds the rest, with no refit, new signal state, threshold or candidate.
PASS -> `FINAL_OOS_CONFIRMED`, `paper_implementation_eligible: YES`.
FAIL -> `FINAL_OOS_FAILED`, `paper_implementation_eligible: NO`, then STOP.
Neither outcome deploys, enables trading or starts another research task.

## Atomic one-shot consumption and audit-only recovery

Future outputs use `artifacts/mean-reversion-2026-final-oos-v1/`. After all source
and identity gates pass, exact input archive versions and code pins are frozen
in `frozen-inputs.json`. BEFORE the first performance computation, an exclusive
`final-oos-consumption.json` is created with `2026_final_oos_consumed: YES`,
preregistration SHA, implementation HEAD and frozen-input SHA. File and directory
are fsynced. A sealed performance permit is issued only after that marker exists.

The marker remains for PASS, FAIL and technical interruption. A second execution
is refused. Recovery is deliberately audit-only: verify original input/version
pins, raw cached ZIP/checksum response hashes and result manifest hashes, without
network, signal generation or resimulation. Missing/incomplete outputs after an
interruption cause STOP and require separate authorization for any further audit;
there is no automatic retry, marker deletion or new statistical experiment.
No changed data version or code is accepted.

## Independent oracle and synthetic tests

The separate oracle uses its own float SMA/std/lower-band calculations, state
machine and direct cash formulas; it never calls the primary observation,
allocator or simulator. The existing independently implemented oracle/verifier
code objects are rebound only to the exact final cohort interval. Signals,
next-open fills, gross/net daily cash, fees/slippage, completed episodes and
leave-month outputs are compared independently. The original frozen financial
and strategy code is not changed.

Only synthetic in-memory prices and synthetic monthly ZIPs in pytest temporary
directories test this future path. Tests include guards before real-file/network
access, exact cohort/source plan, date/HEAD/hash/checksum/completeness gates,
unchanged code pins, sample std and equality boundaries, next-open timing,
terminal valuation, exclusion of 2027 prices, minimum closed episodes, all-four
screen, deterministic artifacts and permanent consumption after interruption.
A synthetic future execution/audit test is not a real 2026 OOS run.

## Preregistration-only command and artifacts

```bash
cd /home/ndvi/crypto-bot-final-oos-prereg
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python \
  -m research.mean_reversion_2026_final_oos preregister \
  --root /home/ndvi/crypto-bot
```

Artifact directory:
`/home/ndvi/crypto-bot/artifacts/mean-reversion-2026-final-oos-prereg-v1/`.
Only preregistration.json, candidate-provenance.json, methodology.json,
frozen-screen.json, source-plan.json, code-pins.json, execution-gate.json and
manifest.json are emitted. No 2026 strategy result, episode, signal, monthly
performance, equity curve, consumed marker or market cache is created now.
The future `execute`/`audit` interfaces are gated and are not invoked in this task.

2026 final OOS executed: NO. Real 2026 market/performance data read: NO.
2026 YTD preview: NO. Parameters changed: NO. Production behavior changed: NO.
Production DB writes: 0. Deployment/service restart: NO.

Final checks: **31 targeted synthetic tests PASS**, **1033 full maintained tests
PASS in 238.00 seconds**, Ruff PASS and staged `git diff --check` PASS. Browser
tests use the already installed local library directory only in the test process:
`LD_LIBRARY_PATH=/home/ndvi/.local/lib/crypto-bot-browser/usr/lib/x86_64-linux-gnu`.
There is no dependency installation or service environment change. Only this
preregistration module, its synthetic tests and this protocol document are added.
The exact frozen HEAD and external SHA256 anchor are recorded in the post-commit
JSON artifacts; generating them does not create a further implementation commit.
