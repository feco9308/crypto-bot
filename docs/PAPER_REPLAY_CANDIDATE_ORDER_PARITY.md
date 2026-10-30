# Paper / replay candidate ordering parity

This is an engineering compatibility fix, not a strategy, ranking experiment,
entry filter or evidence about the predictive value of Market Score.

## Source-of-truth audit (2026-10-30 UTC)

The production checkout `/home/ndvi/crypto-bot` is clean at
`226facef1f68f967fc1d7b1bb75e282849185a56`, branch
`research/portfolio-capacity-v1`. The web and replay worker started from this
checkout on 2026-10-16. The replay worker PID observed was 171379.

The running paper engine (PID 1356) started on 2026-10-13 and its persisted
startup metadata records `be08107d11728a3e90c8ec3dbe351616074c5bf7`, branch
`fix/replay-companion-isolation`. Its working directory is the same production
checkout. A checkout HEAD is not proof of the code already loaded by a process.
Here the paper engine file is byte-identical at the startup commit and current
checkout: Git blob `edd7ebaebc39d808cdd0f25e58b9b6c2dead0ae5` at both.

Before the fix:

* PaperEngine builds and executes contexts in symbol ascending order.
* Replay Variant contexts use score descending, symbol ascending. Execution
  stably partitions pending signals to execute strategy SELL before BUY.
* Paper quote-based STOP/TP already runs before strategy signals. Its old mixed
  strategy loop could execute a BUY before a later-symbol strategy SELL.

The discrepancy is present in the current source and the paper startup source.
The fix branch is based on the **current production checkout HEAD `226face…`**,
which already contains the running replay engineering changes and descends from
`be08107…`. No research branch chain was merged to obtain this base.

There is also a newer, explicit **replay-only research override** in
`replay/entry_ranking.py`: OFF, market_score_desc_v1 and
acceleration_efficiency_v1. It is not a common production ordering abstraction.
Its behavior, eligibility and audit metadata remain unchanged. Default paper /
replay parity does not claim parity with an explicitly enabled research override.

## Shared contract

`crypto_bot.trading.ordering.candidate_order_key(context)` implements
`SCORE_DESC_SYMBOL_ASC_V1`: `(-context.score, context.symbol)`.

It reads only the existing context score and exact symbol ID. It has no DB,
clock, portfolio dependency or side effects. Both engines use it to sort their
contexts; this never recomputes a score or changes a strategy decision.

Paper cycle order is:

1. Fresh public quote marks and existing application-level STOP/TP exits.
2. Existing snapshot / instrument / exited-symbol guards and strategy evaluation.
3. Stable SELL-first processing of the resulting strategy signals.
4. BUY competition in shared score/symbol order, with unchanged permissions,
   fresh quote checks, RiskManager and execution for each attempted candidate.

HOLD bookkeeping is retained. STOP/TP-closed symbols remain excluded from the
strategy phase. Strategy SELLs release capacity before BUYs. A rejected or
unfilled candidate does not terminate processing of the remaining candidates.
Existing positions do not alter the BUY ordering key.

Replay retains its existing SELL-first execution partition, minute STOP/TP layer,
filters, ranking research overrides, stale policy and all fill semantics.

## Limits of parity

This guarantees the same **default BUY competition order**, not identical live
and historical prices, fills, timestamps or portfolio paths in general.

Paper uses fresh public bookTicker quotes: missing candidate quote means a
rejected risk decision and a REJECTED paper order, with no fill. Replay uses a
historical execution candle: missing candle means NO_FILL and no order/fill.
Both continue to the next candidate. If an existing position cannot be freshly
marked, the existing production / strict-replay fresh-mark safeguard still
blocks new BUYs. Research quarantine remains replay-only and unchanged.

Application-level paper STOP/TP uses public bids; historical replay protection
uses its existing closed minute OHLC / intrabar rules. These layers were not
combined. SELL-first is tested independently of those price-source differences.

Existing immutable historical replay results are not changed or recalculated.
The fix affects future paper ordering after an explicitly authorized deployment;
the default replay ordering is behaviorally unchanged.

No audit schema or historical audit record is changed. The policy identifier is
centralized in the helper and documented here; candidate ranks are not persisted.
No migration is required.

## Validation

`tests/test_candidate_order_parity.py` contains a fixed engine-independent input
fixture (timestamp, symbol, score, action, occupancy, capacity), and exercises
real PaperEngine / replay Variant methods against temporary or in-memory ledgers.
No historical ReplayEngine job or public HTTP request is started by these tests.

Coverage includes:

* BBB 90 / CCC 80 / AAA 75 and same-score symbol tie-breaks.
* All six input permutations; deterministic keys and unchanged input rows.
* Real RiskManager / ledger selection with one and two available slots.
* Highest-score risk rejection and missing quote / no-fill; next candidate wins.
* Existing HOLD position and deterministic remaining capacity.
* Strategy SELL before a higher-score BUY; paper STOP/TP before BUY.
* Default replay order matches its pre-fix score/symbol contract.

All production DB inspection used SQLite `mode=ro` and `PRAGMA query_only=ON`.
Only tests create/write isolated test databases. No production DB write,
migration, historical replay launch, deployment or service restart is performed.

Validated results on this branch:

* New parity tests plus existing paper core / quote tests: **93 passed**
  (including 11 new parity cases).
* Ranking, companion isolation and stale compatibility tests: **78 passed**.
* Full maintained suite: **783 passed in 232.93 seconds**.
* Maintained Ruff (`ruff check crypto_bot tests research`): **PASS**.
* `git diff --check`: **PASS**.

The full suite used the existing browser runtime library path and the production
virtualenv as a Python interpreter with `PYTHONPATH=.` pointing at this isolated
worktree; it did not change the deployed source checkout or service state.
