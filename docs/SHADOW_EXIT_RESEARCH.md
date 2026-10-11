# Shadow Exit Research

**MATCHED-ENTRY EXIT COMPARISON — NOT PORTFOLIO PERFORMANCE.**
**NOT STATISTICALLY VALIDATED. NO LIVE TRADING.**

`/replay/shadow-exit` selects a completed normal replay variant as the anchor.
That variant already ran the normal strategy, RiskManager, portfolio, sizing,
position/exposure limits, execution and stale policy. Its **actual FILLED BUYs**
are frozen canonical entries; rejected signals never become shadow entries.
Every selected exit policy receives every canonical entry, including anchor
positions still open at the run end. Selecting an anchor does not rerun it.

The requested 2024 anchor is variant 0 of
`04c2ef76ceac41199a922ae55480d16b`, Production Baseline - Research, with
`RESEARCH_QUARANTINE_STALE`. It has 433 canonical BUY fills, including its
430 closed positions and three still-open positions. The four requested shadow
policies are baseline_v1, fixed_tp (1.5%), r_tp (0.5R), and trailing_pct
(activation 1.5%, distance 0.5%). Nothing in the anchor config is changed.

## Isolation and architecture

- `shadow_config.py`: validates anchor and 1–10 policies. Parameter validation
  delegates to the existing replay validator/exit registry; all seven currently
  registered policies are supported. Stable unique policy IDs distinguish
  multiple parameterizations of the same policy.
- `shadow_store.py`: isolated `data/shadow-exit.db`, schema 1. Stores immutable
  anchor entry/config snapshots and research jobs/results. Rejects symlinks,
  aliases of market/replay DBs and non-shadow tables. No scanner DB migration.
- `shadow_engine.py`: only plain position state. No PortfolioService,
  RiskManager, account reservation or order execution instance. Canonical
  quantity, price, fee, slippage, ATR, original stop and risk amount are copied.
- Existing `exits.observe/levels/policy` implement exit triggers unchanged.
  Exit fill arithmetic uses the anchor's fee/slippage and canonical quantity.
- `shadow_data.py` pins cached archives to the anchor manifest, including
  recorded missing objects. Expired negative cache objects are not refreshed
  into different source data. Unpinned newly needed archive objects still use
  the existing public GET-only client and are included in revision metadata.
- Strategy exits use the same registered strategy's **held-position branch**,
  with unchanged score/features computed from closed historical 1h data.
  No shadow BUY is evaluated or generated. Current registry strategy is
  watchlist_reference_v1; its exit branch uses score/EMA, not entry momentum.
- Shadow work runs when the existing singleton replay worker has no normal
  queued replay. It never changes an anchor record, portfolio or score history.
  New regular replay jobs may wait behind an already-running shadow job.
  Restart/shutdown requeues shadow work and deterministically recomputes it
  from its immutable snapshot; normal replay checkpoints remain unchanged.
  A shadow job's network/data failure is job-local and cannot stop the worker.

Frozen entries also eliminate the existing normal multi-variant shared-history
entry coupling: no shadow-held symbol can create an additional anchor BUY.
Overlapping same-symbol canonical entries are independent matched observations,
not additional portfolio positions.

## Clock, execution and missing data

Only a minute candle whose close is before the current replay clock can trigger
an OHLC exit. Entry-time minute exits are recorded at that minute's close,
strictly after the canonical entry timestamp. Hourly strategy signals only use
completed 1h candles; execution uses the current boundary open, matching normal
replay's public-kline approximation. Conservative/optimistic intrabar selection
and ambiguity audit reuse the existing pure path functions. Optional 5m fallback
only covers minutes without 1m data, and is observed at its completed close.

Archives may be cached/downloaded ahead, but coverage-end metadata and future
rows never decide an exit, quarantine or resume. Missing current data creates a
stale episode. No previous minute is replayed as a fresh bar, last-price exit,
forced end-of-run liquidation, conversion or invented fill.

Data returning clears stale state without creating a fill itself. A subsequent
fresh completed bar/current hourly strategy may cause an ordinary exit.
An unresolved stale position exports `status=OPEN_STALE` and
`shadow_status=SHADOW_OPEN_STALE`; ordinary unresolved positions are OPEN.
Last valid mark, observation time, first stale time, duration and resume history
are audited. These frozen marks produce no realized outcome or buying power.

Any observed execution gap makes that policy's outcome incomplete even after
resume: missed exit opportunities cannot be reconstructed. A later actual
shadow close has `position_status=CLOSED`, `status=INCOMPLETE_DATA`, with its
observed PnL retained but `comparison_eligible=false`. Missing required closed
hourly strategy data also marks the affected result incomplete. This strict
rule prevents incomplete observations from being counted as wins/losses.
Other policies and symbols continue; there is no shadow portfolio to freeze.

## Statistics and normalization

No final equity, portfolio drawdown or independent cash curve is computed.

- Net realized PnL = quantized exit quantity × fill minus exit fee minus
  canonical entry notional minus canonical entry fee.
- Return denominator = canonical notional + entry fee; same for every policy.
- R denominator = canonical anchor risk amount; same for every policy.
- Normalized PnL per 1000 = return fraction × 1000.
- Normalized profit factor uses per-trade return percentages, not different
  anchor notional weights. Expectancy in R is the mean complete R outcome.
- Primary all-policy comparisons use the intersection of complete CLOSED
  outcomes across all policies. Individual complete-closed samples are separately
  descriptive and may differ in size.
- Each pair uses only entries complete/closed for both policies, reporting
  A wins, B wins, ties, incomplete, mean/median return difference and mean R
  difference. Tie tolerance is 0.000001 percentage points.
- Every breakdown uses canonical **entry-time** attributes: symbol, asset group,
  BTC regime, ATR%, range position, RSI, score, relative volume, month and quarter.
  Each group includes the same matched/common and pairwise accounting.
- Sample labels: fewer than 30 INSUFFICIENT SAMPLE, 30–99 SMALL SAMPLE,
  at least 100 LARGER SAMPLE. No automatic best policy or optimal-coin claim.

## Web/API and exports

New pages:

- `/replay/shadow-exit`: anchor/policies/config and saved jobs.
- `/replay/shadow-exit/<id>`: progress, common summaries, pairwise matrix and
  all required breakdowns, with responsive scrolling tables.

New API endpoints:

- `POST /api/replay/shadow/config/validate`: shared registry validation, no job.
- `POST /api/replay/shadow/runs`: explicit job creation, CSRF required.
- `GET /api/replay/shadow/runs/<id>`: progress plus compact result.
- `GET /api/replay/shadow/runs/<id>/export.compact.json`.
- `GET /api/replay/shadow/runs/<id>/export.json`: additionally all canonical
  entries, matched outcomes and per-outcome events.

Input example (anchor fee/slippage/conservative/stale/period are inherited):

```json
{
  "name": "2024 EXPLORATION — Shadow Exit Research",
  "anchor_run_id": "04c2ef76ceac41199a922ae55480d16b",
  "anchor_variant": 0,
  "policies": [
    {"id": "baseline", "exit_policy": "baseline_v1", "exit_parameters": {}},
    {"id": "fixed_tp", "exit_policy": "fixed_tp", "exit_parameters": {"tp_pct": "1.5"}},
    {"id": "r_tp", "exit_policy": "r_tp", "exit_parameters": {"tp_r": "0.5"}},
    {"id": "trailing_pct", "exit_policy": "trailing_pct", "exit_parameters": {"activation_pct": "1.5", "distance_pct": "0.5"}}
  ]
}
```

Both exports include metadata, immutable anchor settings, policies, canonical
count, summaries, pairwise, breakdowns, incomplete/stale counts and no-lookahead
metadata. Reproducibility records git/app version, replay/shadow schemas,
anchor selected-config hash, canonical-entry hash, research config hash,
used archive revisions/hash, execution model and intrabar/stale settings.
An archive revision differing from the frozen anchor manifest fails the job
rather than silently presenting a comparison using different source data.
Deterministic outcomes have a separate outcomes hash; elapsed/wall times are
operational metadata and naturally differ between executions.

## Deployment and limitations

Only web and isolated replay worker need restart when idle. Scanner/paper keep
their PIDs and settings. New schema 1 initializes only `shadow-exit.db`.
No existing market/replay migrations, resets or historical audit edits.
Production paper, ReferenceStrategy, RiskManager, PortfolioService, Market Score,
execution and existing exit-policy modules are unchanged.

Anchor entry selection remains conditional on that anchor's portfolio/exit
behavior. Matched outcomes do not estimate returns from a different entry stream
or a separately funded exit portfolio. OHLC ambiguity remains an approximation.
The 2024 EXPLORATION dataset is not statistical validation, optimization or a
production promotion decision.

The completed annual run and verification are recorded in
[SHADOW_EXIT_VALIDATION.md](SHADOW_EXIT_VALIDATION.md).
