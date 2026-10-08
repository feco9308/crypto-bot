# Replay-only stale-position policy

The default is `STRICT_FRESH_MARKS`. Old configurations/checkpoints without the
new field retain that default. Config import/export, presets and the normal run
POST share `stale_position_policy`; the UI offers the same two policy IDs.

## Strict production parity

`STRICT_FRESH_MARKS` retains the existing global BUY guard: at the execution
boundary, any held symbol without the current execution candle's open blocks
new BUYs. Risk sizing, portfolio bookkeeping, exits, fills and prices are
unchanged. Added state/events/statuses are reporting only. Production
RiskManager, PortfolioService, ReferenceStrategy, scanner/score and paper/live
configuration are never edited or selected through this replay option.

## Research quarantine

`RESEARCH_QUARANTINE_STALE` detects missing data solely at the current replay
clock, for each held position. It does not inspect the last archive timestamp,
future candles or a delisting/replacement-symbol list. A missing execution open
at the hourly boundary or a missing completed minute marks that position stale.
Explicit 5m fallback is observed at its existing supported boundaries; absence
of intervening 1m bars does not fabricate minute marks or fills.

A quarantined symbol cannot execute a BUY/SELL/stop/overlay while unpriced.
Unrelated symbols continue through the unchanged strategy and RiskManager with
the conservative replay risk view below. Normal cash, exposure, maximum-position,
daily loss and drawdown limits still apply. Quarantine is not permission to
ignore them. Held stale positions still occupy position slots.

When the current execution open or a newly completed supported bar returns,
`POSITION_RESUMED` clears quarantine. Only current data may mark or trigger a
normal exit. Past absent bars are never replayed later to invent an earlier fill.
There is no forced last-price liquidation, token conversion or end-of-run close.
A position lacking data at the end is exported as **OPEN_STALE**, while its
internal ledger status stays OPEN so production PortfolioService semantics and
position-limit accounting remain intact.

## Exact valuation and locked-capital semantics

For each stale position:

- **C** = entry fill notional + paid entry fee (invested capital already debited
  from cash).
- **L** = quantity × last known valid mark, with normal Decimal quantization.
- Its public reporting mark remains unchanged, with explicit stale status,
  observation time/price and stale duration. It is not a fresh quote.

Let **F** be the sum of current marks for non-stale positions:

| Quantity | Formula |
| --- | --- |
| Reported equity | cash + F + sum(L) |
| Reported exposure | F + sum(L) |
| Risk-sizing equity | cash + F + sum(min(L, C)) |
| Risk exposure reservation | F + sum(max(L, C)) |
| Capital locked by stale positions | sum(C) |
| Exposure locked by stale positions | sum(max(L, C)) |
| Available cash | actual ledger cash − existing order reserved_cash |

Stale gains are withheld from risk-sizing equity. Stale losses are retained and
cannot free exposure below original invested capital. Cash is never credited,
capital is never released and positions are never omitted from maximum-position
counts. `capital_locked_by_stale_positions` is an informational amount already
spent, not an additional cash debit or pending-order reserve. Production
PortfolioService.view/mark/reserve/record_buy/record_sell remain unchanged.

The replay-only `ReplayRiskView` preserves every position and available-cash
value, reduces equity and daily PnL by positive stale unrealized gains, and adds
the shortfall-to-cost exposure reservation. The production RiskManager evaluates
that input with unchanged formulas. Reported day-start/peak/drawdown anchors are
retained, so this treatment can be more restrictive; it cannot increase allowable
risk compared with using the unadjusted reporting view. Summaries expose both
`risk_sizing_equity` and `risk_exposure` separately from reported final equity.

Fees, realized PnL and cash retain normal accounting. Reported unrealized PnL and
return/drawdown may include frozen last marks. They are explicitly incomplete as
research valuations, never represented as fully liquidatable values.

## Metadata, exports and warnings

Each affected trade carries first_stale_time, last_valid_mark_time/price,
stale_duration_seconds, resumed_after_stale, stale/quarantined flags and locked
capital/exposure. Duration sums all stale episodes up to resume or the run end.
Last valid mark time is when the open/completed-bar price became observable to
the replay clock (e.g. a 02:59 candle close is observed at 03:00).

Per-variant summaries include stale_position_count, stale_symbols, stale_positions,
first_stale_time, aggregate stale_duration_seconds, resumed_after_stale, locked
capital/exposure, stale_data_affected, metrics_complete, valuation_complete and
ranking_eligible. Run metadata includes the policy and details by variant.
Closed trades retain any prior stale/resume history. Checkpoints persist position
state through existing entry audit metadata; old checkpoints initialize safely.

Both full and compact JSON exports include open positions, including OPEN_STALE.
The UI displays the prominent warning:

> RESULT CONTAINS STALE OPEN POSITIONS. Final equity contains stale valuations
> and must not be interpreted as a fully liquidatable portfolio value.

Comparison preserves all metrics but excludes stale-data-affected variants from
ranking, including resolved historical episodes. No automatic best selection is
made. Existing results whose blocked-entry audits show missing portfolio marks
also warn and are excluded, without rewriting historical records. Comparison
uses the explicit open-position warning for current stale holdings and a separate
incomplete-metrics warning for resolved/legacy episodes.

## Operation and isolation

Only web and the isolated replay worker require restart when idle. Scanner and
production paper must retain their running processes. No database migration,
production account reset, historical record correction or API key is required.
The dedicated replay database/cache remain separate from the production database.
The 2024 rerun and its four compact exports are documented in the companion
validation report after completion.
