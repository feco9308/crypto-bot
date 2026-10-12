# Institutional Exit Research v2 — 2024 EXPLORATION

Research/replay only. NO LIVE TRADING. No deployment or new historical run is
performed by this implementation. The importable config is
[replay/institutional-exit-v2-2024.config.json](replay/institutional-exit-v2-2024.config.json).
It uses the original institutional C entry/risk configuration from commit
6a17ea4, identically for all five variants. No 2025/2026 market data, parameter
search, automatic optimization, or strategy promotion is used.

## Predeclared experiment

2024-01-01T00:00:00Z through 2025-01-01T00:00:00Z (exclusive end), historical
rolling 24h quote-volume Top20, conservative closed-minute execution, strict
no-lookahead, fallback_5m=false, RESEARCH_QUARANTINE_STALE. Initial balance 1000,
risk 0.5%, 3 positions, total/symbol exposure 50%/20%, daily loss/drawdown guards
100%, fee 0.1%, slippage 0.05%. Outer BTC gate OFF; original institutional BTC,
multi-timeframe and correlation parameters unchanged.

| Variant | Exit | Re-arm |
| --- | --- | --- |
| A | baseline_v1, unchanged control | OFF |
| B | adaptive_peak_lock_v1, unchanged old adaptive control | OFF |
| C | break_even_guard_v1 | OFF |
| D | delayed_peak_lock_v2 | OFF |
| E | delayed_peak_lock_v2 | setup_rearm_v1 |

Primary comparisons: A/C (break-even), A/D (later/wider trailing), D/E (re-arm),
B/D (old/new protection). Primary metrics: expectancy R, PF, net return, max DD.
Secondary: turnover, fee, slippage, holding time, trade count, MFE capture,
profitable excursions subsequently lost, and same-symbol rapid reentry. Do not
pick a winner by win rate. Portfolio replays have different cash/slots and may
have different entries: they are not matched-entry causal exit comparisons.

## New exits

Both preserve the original 2 × entry ATR stop from the unchanged entry strategy
and the normal strategy SELL. They reuse the existing quantization-aware net
break-even formula and closed-minute conservative/optimistic OHLC path model.
A gap below the stop fills at the available open, not at a fabricated stop price;
therefore protection is not a guaranteed nonnegative realized result.

- `break_even_guard_v1`: `activation_break_even_r=1.0`. Below +1R, original stop.
  At +1R, max(previous stop, net break-even). No trailing at any later peak.
- `delayed_peak_lock_v2`: `initial_stop_atr=2.0`,
  `break_even_activation_r=1.0`, `peak_trailing_activation_r=2.0`,
  `peak_trail_atr_multiplier=1.25`, `tightening_activation_r=4.0`,
  `tight_trail_atr_multiplier=0.75`. +1R activates net break-even, +2R starts
  highest_price−1.25×entry_ATR, +4R tightens to highest_price−0.75×entry_ATR.
  Stop never decreases. R uses stored entry risk_amount / quantity, not an
  assumption that the original ATR distance exactly equals net risk.

Entry slippage is already in fill/notional. Break-even covers entry fee,
exit fee and exit slippage exactly once, with ceiling quantization. No future
candle or archive-end knowledge is used. Stale positions remain quarantined;
there are no retroactive, stale-price, or end-of-year forced exits.

Old adaptive remains stop-only (its existing strategy SELL suppression is
preserved). The new exits allow strategy SELL. This difference is explicit in
B/D interpretation; the old adaptive is not silently changed to match v2.

## Setup re-arm

Optional variant field `setup_rearm`: `OFF` (default) or `setup_rearm_v1`.
The shared backend validator requires the institutional strategy and a research
profit-protection exit. Builder, API, import and export use this single field.
OFF is omitted from stored snapshots to preserve existing config hashes.
No database schema migration is needed.

Only a successfully filled protective exit in BREAK_EVEN, PEAK_TRAIL or
TIGHT_PEAK_TRAIL blocks the symbol. ORIGINAL_STOP and STRATEGY_EXIT do not.

A fully closed hourly setup strictly AFTER exit must first be FALSE; a later
closed hourly setup must then be TRUE. The same snapshot cannot reset and BUY.
A cached signal from before exit/reset cannot execute. There is no fixed-hour
cooldown. Missing/unavailable context and absence from scanner rows do not count
as an observed FALSE. Setup checks use the unchanged institutional predicate
with has_position=false; portfolio correlation/slots/cash/risk cannot themselves
cause a setup reset. Correlation and RiskManager still run normally for BUY.

State and counters are persisted inside the existing hourly replay checkpoint,
including exit/false timestamps, armed flag, block count, success count and
reset durations. Audit events: SETUP_REARM_BLOCKED, SETUP_NOT_REARMED,
SETUP_REARMED. SELL remains unblocked.

## Additive reporting

Existing full/compact result structure and old fields remain supported. New
activation fields explicitly identify +1R/+2R/+4R; old adaptive time/stage
labels stay unchanged. New exits use ORIGINAL_STOP, BREAK_EVEN, PEAK_TRAIL,
TIGHT_PEAK_TRAIL, or STRATEGY_EXIT.

- same_symbol_reentry_within_1h/4h/24h: entry minus preceding same-symbol exit,
  inclusive windows [0, window], counting a new open trade too.
- rearm_block_count: blocked BUY opportunities (separate from strategy HOLD).
- rearm_success_count: first observed FALSE reset after each protective exit.
- average_time_to_rearm: seconds from protective exit to that FALSE reset;
  null when no reset was observed. TRUE must still occur later before BUY.
- trades_reached_1R_then_lost / trades_reached_2R_then_lost: CLOSED trades with
  stored gross MFE R at/above threshold that end with materially negative net
  return (existing research loss definition; fee-quantization dust excluded).
- average/median_mfe_capture_ratio: realized_r / mfe_r over CLOSED trades with
  positive MFE R. Negative capture is retained; zero MFE is excluded.

Turnover is sum of entry and filled exit notional, including entry notional of
open positions. Fees/slippage and average holding time keep existing meanings.
Results with stale valuations retain existing warnings and ranking exclusions.

## Validation

Golden baseline and old-adaptive trade/cash/order/fill fixtures were generated
from the unchanged production 6a17ea4 code using fixed synthetic input only.
No historical replay was restarted. New tests cover stop stages, cost-aware
break-even, monotonic stops, gaps, no future candles, re-arm sequencing,
checkpoint state, original/strategy exit exemptions, context availability,
audit categorization, metrics, full/compact exports and desktop/mobile builder
round-trips with no implicit run creation.

For v2 runs, metadata `institutional_exit_v2_evaluation` records the primary
metrics and warning flags without selecting a winner. Trade count >=1.5× control
is a predeclared descriptive warning, not statistical significance. Lower
maximum realized R warns about a large-winner proxy; different portfolio entries
prevent a causal claim. Higher PF AND win rate without higher expectancy R,
and higher turnover/fees with worse net return, receive separate warnings.
These report checks never feed strategy, execution, sizing or parameters.
