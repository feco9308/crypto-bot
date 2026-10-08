# Replay blocked-entry investigation

Run: `8afd54494906450180e7eea8c012dd19`, **2024 Exploration - 4 Exit Comparison - Research Guards**.
Investigated on 2026-10-08 against production HEAD `7a0bcbe`, using the existing
replay database and candle cache. No replay was rerun and no historical record,
production setting, strategy, risk rule, score, fill or position was changed.

## Proven cause

Baseline's `other = 5000` is exactly:

| Existing audit reason | Attempts |
| --- | ---: |
| `Fresh portfolio marks unavailable` | 4999 |
| `MISSING_1M_EXECUTION_CANDLE; fallback disabled` | 1 |

The other 7054 blocked BUY attempts were `max open positions reached`.
These are repeated attempted BUYs, not 5000 unique orders or missing closed trades.

Baseline position 338, FRONTUSDT, opened at **2024-08-20 11:00 UTC**, with entry
price 0.746373, quantity 61.072287825932 and original stop 0.700965025090162936.
It remains OPEN. The cache's final FRONTUSDT 1m candle starts at
**2024-08-27 02:59 UTC**, closes at 02:59:59.999, and closes at price **0.88**.
The final 1h candle starts at 02:00 and closes at the same boundary. August's
archive objects are COMPLETE (627 hourly / 37620 minute rows); September through
December FRONTUSDT archive objects are MISSING. This is evidence that this run
has no subsequent FRONT prices; we do not assume a delisting/migration or invent
an exchange conversion from that evidence alone.

The first fresh-mark rejection is FTMUSDT at **2024-08-27 03:00 UTC**; the last
is STEEMUSDT at **2024-12-31 18:00 UTC**. Monthly counts: August 134, September
1202, October 1228, November 1331, December 1104. Baseline opens **zero** new
positions after the first fresh-mark rejection.

In `Variant.execute_signals()`:

1. Portfolio marks use the current execution candle's open, where available.
2. A missing candidate execution candle produces NO_FILL.
3. For a BUY, if **any held symbol** lacks a current mark, the existing replay
   guard emits `RISK_REJECTED: Fresh portfolio marks unavailable` and continues,
   before calling RiskManager. FRONT's missing data blocks the entire portfolio's
   new BUYs, including when other symbols have usable execution candles.
4. Existing holdings are still processed for exits if their own data exists.
   Missing FRONT data supplies neither a strategy context after its last usable
   hourly snapshot nor a minute bar to trigger a stop/overlay. No candle-close
   substitute, conversion, forced exit or end-of-run liquidation exists.

This is the explicitly implemented missing-data guard and an opaque reporting /
data-coverage limitation, not evidence of a baseline execution bug or silent
trade deletion. Relaxing it, liquidating FRONT, mapping to a replacement symbol,
or using stale prices to authorize new risk would change replay semantics and
requires a separate, explicit research policy. None was implemented here.

The one direct NO_FILL is USUALUSDT at **2024-12-17 09:00 UTC**, shared by all
four variants. No invalid-stop, duplicate, insufficient-balance or other RiskManager
rejection explains baseline's 5000 count. HOLDs and rejected SELLs do not contribute.

## Comparison

| Variant | BUY fills | Closed | End open | Mean closed hold, hours | Missing marks | Missing entry minute | Max positions | Below-minimum/balance/exposure room |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline_v1 | 341 | 340 | 1 | 45.17 | 4999 | 1 | 7054 | 0 |
| fixed_tp | 4875 | 4875 | 0 | 3.71 | 0 | 1 | 7195 | 92 |
| r_tp | 3702 | 3702 | 0 | 5.36 | 0 | 1 | 8086 | 68 |
| trailing_pct | 4047 | 4047 | 0 | 4.08 | 0 | 1 | 6110 | 2148 |

All use the same `execute_signals()` / RiskManager / local fill path. Overlays
operate in the separate completed-minute `bar()` exit path. Their earlier exits
free slots and permit re-entry, explaining higher turnover even before the data
loss. Fixed TP and trailing opened FRONT on August 20 at 11:00 too, but closed it
at **12:02** (FIXED_TP / TRAILING). R-TP's last FRONT position closed May 13 at
18:22; it had no FRONT holding when data ended. After the first baseline block,
these variants opened 1731 / 1353 / 966 further positions respectively.

Baseline closes 229 ORIGINAL_STOP and 111 STRATEGY trades. Its accounting reconciles:
**341 BUY fills = 340 closed positions + 1 open position**. Every overlay also
reconciles its BUY/SELL fills with its persisted positions. No trade is missing.

Baseline final equity **579.320398699798**, realized PnL **-428.794925198837**,
unrealized PnL **8.115323898635**, return **-42.0679601300202%**, and drawdown
**46.552280941145%** match stored summaries. The open FRONT mark is **0.88 from
August 27**, not a year-end quote. Consequently this is not a fully current-marked
annual result; stopped entry activity and stale equity make optimization or
performance comparisons from this result premature.

## Exactly what legacy `other` means

`blocked_entries` counts blocked **BUY attempts only**. It has three sources:

- NO_FILL when the current execution candle is absent: the two existing reason
  strings are `MISSING_1M_EXECUTION_CANDLE; fallback disabled` and
  `MISSING_EXECUTION_CANDLE` (when explicit fallback is enabled).
- Replay fresh-mark guard: `Fresh portfolio marks unavailable`.
- RiskManager rejection: a substring classifier chooses daily_loss, drawdown,
  exposure, max_positions, then other.

Risk rejection strings that fall into other are:

- `duplicate open position; pyramiding disabled`
- `invalid stop distance`
- `insufficient free balance`
- `nonpositive portfolio equity`
- `take profit already reached at current quote`
- `paper trading OFF` (unreachable during the normal replay path, which passes True)
- any unknown rejection without a recognized legacy substring

`HOLD; no order requested` and `no open long position` also match the legacy
fallback, but cannot increment a BUY counter in the normal path: HOLD signals are
not queued and the latter is a SELL rejection. Strategy entry failures (score,
watch eligibility, RSI, momentum, EMA, ATR stop) produce HOLD and are not counted
as blocked entries. They are not 5000 hidden executions. Invalid execution calls
or bookkeeping exceptions fail a run rather than silently adding an other count.

The legacy classifier also puts `sized order below minimum value or insufficient
free balance/exposure room` in **exposure**, because it contains that substring.
That is a combined reason; it does not prove that the exposure limit itself fired.
The fix preserves legacy totals and presents the exact combined reason separately.

## Observability changes

Replay summaries, normal analysis JSON, compact analysis JSON, result API and UI
now include:

- `blocked_entry_reasons`: counts keyed by exact existing audit reason strings.
- `blocked_entry_categories`: reporting-only categories, including
  missing_portfolio_marks, missing_execution_minute, invalid_stop_distance,
  duplicate_position, insufficient_free_balance, and the combined minimum-order
  reason. Unknown strings are retained and categorized as other.
- `blocked_entry_attempts`, `blocked_entry_breakdown_source`,
  `blocked_entry_breakdown_complete`, `blocked_entry_unresolved_attempts`.

Existing aggregate and flattened `blocked_entries` fields are retained. New
rejection events explicitly include BUY/SELL action, and fresh-mark events list
`missing_mark_symbols`. New checkpoints retain exact reason counts. Old checkpoints
recover their counts from persisted audits when resumed.

Existing summaries are enriched **on read**, without updating database rows or
requiring a rerun or migration. Old NO_FILL actions are inferred from chronological
entry/exit fills; the reference strategy's existing open-position rule emits SELL
or HOLD, never another BUY for that symbol. Old rejected SELL/HOLD risk reasons
are excluded explicitly. If historical audits do not reconcile, the breakdown is
marked incomplete and unresolved attempts remain visible rather than fabricated.
The UI displays exact reasons and a stale-mark warning when the fresh-mark guard fired.

## Verification and deployment scope

New deterministic tests cover every known/unknown reason, compatibility grouping,
all four policies' identical entry/rejection path, data disappearance and restored
exits, no silent trade loss, HOLD/SELL/duplicate/invalid-stop handling, read-only
legacy enrichment, both export formats, API output, old checkpoint recovery,
closed-candle gating, production RiskManager parity and unchanged trades/equity
when instrumentation is disabled. Chromium checks desktop/mobile rendering.
Existing future-data-invariance and production strategy/risk/portfolio tests remain
part of the full suite.

Changes are confined to replay engine reporting/storage readers, replay UI and
these tests/docs. No production scanner/paper/strategy/risk/score/settings files
are edited. No database migration, account reset or historical audit modification
is needed. To deploy, restart **web only** for existing-result API/export/UI changes;
restart the **replay worker when idle** for explicit action/missing-symbol events
on future runs. Do not restart scanner or paper. No services were restarted as
part of this investigation.

Verification results: **374 tests passed**, including **38 new** deterministic /
Chromium cases; `ruff check crypto_bot tests` and `git diff --check` passed.
Commands: `LD_LIBRARY_PATH=/tmp/crypto-bot-browser-libs/usr/lib/x86_64-linux-gnu
.venv/bin/pytest -q` and `.venv/bin/ruff check crypto_bot tests`. All four stored
variant breakdowns reconcile with legacy totals (zero unresolved attempts).
Scanner PID 20305, paper PID 96297, web PID 123885, replay-worker PID 120715
remained unchanged. Production checkout remained at `7a0bcbe`.
