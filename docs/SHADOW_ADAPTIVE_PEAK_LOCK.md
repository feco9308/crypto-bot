# Adaptive peak lock v1 — Shadow Research only

`adaptive_peak_lock_v1` was introduced on `/replay/shadow-exit`.
It is absent from the production/paper exit registry. A later research extension
also makes the same stop model available to normal portfolio Replay Research;
see [REPLAY_BTC_REGIME_GATE.md](REPLAY_BTC_REGIME_GATE.md).
No tunable parameters: `exit_parameters={}`. EXPLORATION, NOT STATISTICALLY
VALIDATED; no policy promotion or optimizer.

## Price, risk and costs

Canonical entry fill, quantity, original stop, ATR and risk amount are immutable.
The gross R at price P is `(P-entry_fill)*quantity/canonical_risk_amount`.
Activation thresholds are entry fill plus 0.75, 1.0 and 2.0 times canonical
risk divided by quantity. ATR is the frozen entry ATR, never a future ATR.

Initial protection is only the original stop. At 0.75R, stop becomes at least
cost-correct break-even; at 1R, peak tracking locks `high - 0.75*entry_atr`;
at 2R it tightens to `high - 0.50*entry_atr`. Every update takes the maximum
of previous stop, applicable break-even and trail: stop cannot decrease.
A gap through a stop fills at observed open, so protection cannot guarantee
profit during gaps. This policy exits through its stops only; the existing
ReferenceStrategy held-position exits still apply unchanged to older policies.

Entry cost is **canonical notional plus booked entry fee**. Entry fill/notional
already includes entry slippage, which is not charged twice. The raw break-even
reference is `cost / quantity / (1-exit_fee_fraction) / (1-exit_slippage_fraction)`.
The implementation rounds the required sell fill and then its reference upward
to the existing Decimal ledger quantum and includes one ledger quantum of
rounding allowance. Executed fill/notional/fee still use the unchanged shadow
execution arithmetic. This accounts for rounding at each execution step.

## Intrabar clock and audit

Only completed post-entry candles are observed. Both OHLC paths and earliest
feasible activation/partial-peak retracement paths are considered, consistent
with the local conservative/optimistic ambiguity model of existing overlays.
The conservative/optimistic setting chooses the lowest/highest feasible exit
in the current bar; carry paths mark ambiguity. Only the chosen path up to the
actual exit updates peak, activation stage and MFE/MAE. A high that occurs after
a stop on that path never activates protection or inflates the peak.
Activation timestamps are UTC candle-close **observation times**, not fabricated
intraminute timestamps. Gap exits use current candle open. No future candles or
coverage-end knowledge choose an exit. Stale/resume/no forced year-end fill are
the existing shadow rules.

Every adaptive outcome adds activation times, highest price, gross highest R,
net highest R/profit percentage, break-even/final stop, stage, realized return/R
and giveback percentage/R. `exit_stage` is the stage reached at closure or the
last observed stage if still open. MFE/MAE remain gross path excursions.

**Giveback = net liquidatable profit at observed peak minus net realized PnL.**
Both quantities include entry cost and the same sell fee/slippage model. It uses
the canonical entry-cost denominator for percentage and canonical risk for R.
Open/stale outcomes have null realized return and null giveback.

## Reporting and sample rules

Policy summaries, common matched summaries and canonical group breakdowns add
mean MFE and mean giveback percentage/R. Means exclude incomplete/open outcomes.
The adaptive win/loss/flat classification uses the existing pairwise tolerance
of 0.000001 percentage points; negligible rounding profits at break-even are
flat, not wins. Adaptive normalized profit factor excludes these flat returns;
raw outcomes and means keep their exact observed values. Other policies retain
their existing summary classification and execution semantics.

`adaptive_peak_lock_analysis.cohorts` reports 0.75R/1R/2R, reached/complete/
profitable/flat/loss/incomplete counts, giveback counts/means, and matched
baseline loss rescue counts. Two distinct rescue metrics are exported:

- `adaptive_saved_baseline_loss_count`: baseline reached this MFE threshold,
  closed at a net loss, adaptive closed at a meaningful net profit.
- `adaptive_avoided_baseline_loss_count`: same baseline cohort, adaptive closed
  at meaningful profit **or flat**. This counts capital protection separately.

Incomplete/stale outcomes never count as rescued wins. Baseline cohorts use the
baseline's own reached MFE; adaptive activation cohorts use its own observed
pre-exit peak. A shadow that exited early cannot be credited with a later peak.
The compact and detailed JSON both include these cohorts, pairwise statistics,
BTC regime/ATR breakdowns, fixed rules and reproducibility metadata.

## Verification and deployment

Dedicated tests cover stages, monotone stop/new peak/retracement, costs and gaps,
canonical risk sizing of activation, selected-path audit, no-lookahead,
stale/resume, policy order, old-policy outcome invariance, shadow-only validation
and matched rescue classification. Existing production tests also run.
Only web and an idle replay worker require restart. No scanner/paper restart,
DB migration, reset, setting change, historical audit edit or normal registry
change is needed.

A completed 2024 run and all numeric breakdowns are recorded in
[SHADOW_ADAPTIVE_VALIDATION.md](SHADOW_ADAPTIVE_VALIDATION.md).
