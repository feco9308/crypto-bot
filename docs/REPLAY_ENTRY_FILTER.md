# Score / delta4h entry filter — Replay Research only

Each normal Replay variant accepts `entry_filter` in the existing UI/API/config
import/export schema. All four defaults are null (unrestricted, OFF):

```json
{"entry_filter":{"score_min":80,"score_max":90,"delta4h_min":5,"delta4h_max":10}}
```

Minimum is inclusive; maximum is exclusive. Every constrained dimension must
pass (AND). This example means 80 <= Market Score < 90 AND 5 <= delta_4h < 10.
Null or omitted bounds do not gate that dimension. Score bounds are finite
numbers in [0,100]; delta bounds may be negative. Maximum must exceed minimum.
Unavailable/nonfinite constrained values fail closed (`SCORE_UNAVAILABLE` or
`DELTA4H_UNAVAILABLE`), while an unconstrained missing value is ignored.

The unchanged strategy evaluates the current closed-data scanner snapshot first.
Existing BTC regime gate evaluates its BUY next. Only a BUY that passes the BTC
gate reaches this filter; only a BUY that passes both reaches normal risk/sizing
and execution. HOLD/SELL/stop/adaptive exits are never blocked by this filter.
No new score or momentum calculation is introduced; delta4h is always the row's
actual `momentum.delta_4h`, independent of the strategy's delta_window.
The filter fetches no historical/future data and never reserves cash/slots.

Each block persists `RESEARCH_ENTRY_FILTER_BLOCK`, timestamp, symbol, score,
delta_4h, full original strategy decision and all `filter_reasons`:
SCORE_BELOW_MIN, SCORE_ABOVE_MAX, DELTA4H_BELOW_MIN, DELTA4H_ABOVE_MAX.
ABOVE_MAX includes equality because the upper bound is exclusive.
Result/compact JSON summaries add `research_entry_filter_block_count` and
`research_entry_filter_blocks`. A failed BUY counts once; reason counts overlap
when more than one constraint fails. Legacy aggregate `blocked_entries.other`
includes these known research reasons; the detailed category distinguishes them
from unknown reasons. Reports also expose residual non-BTC/non-filter/non-slot
blocks separately. Counts are reconstructed from persisted audit after resume.

2024 EXPLORATION: four independent normal portfolio replay jobs, own cash,
history, positions, exposure, sizing, risk, execution; no Shadow trade subset.
Same frozen data revisions and strategy/risk settings as previous BTC-gate D.
All use BULLISH_ONLY + adaptive_peak_lock_v1; A unrestricted, B score [80,90),
C delta4h [5,10), D both. Research guards 100%, stale quarantine, conservative
intrabar, strict no-lookahead, fallback_5m=false. No parameter search.

Before B/C/D run, A must reproduce previous D's closed trades, final equity,
realized PnL, drawdown, fees, slippage and existing rejection breakdowns exactly.
A mismatch stops the research runner for investigation rather than proceeding.
Activation diagnostics use activation_075r_time (any adaptive protection) over
closed trades; exits are separately ORIGINAL_STOP / ADAPTIVE_PEAK_LOCK.
Monthly trade metrics group by exit month, while monthly drawdown uses the
existing hourly equity observations (not claimed minute-precise).
Transaction cost is fees + reported slippage cost. Slippage is already embedded
in realized fills: it must not be subtracted from net PnL a second time.
Positive trade PnL + each positive trade's fees/slippage is reported as gross
winning-trade profit estimate, not a hypothetical no-fee portfolio rerun.

Production paper, ReferenceStrategy, RiskManager, PortfolioService, scanner,
score and all existing exit models remain unchanged. No live trading or
automatic promotion. The reproducible inputs/runner/proofs/exports live under
`artifacts/replay-entry-filter-2024/`; the completed validation report records
the annual/monthly comparison and all precise export paths.
