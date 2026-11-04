# BTC S2 Realtime Forward Paper v1

PAPER ONLY. NO LIVE TRADING IMPLEMENTED. No exchange order or authenticated API.
Implementation is based on production `f679c290d28a6f57782a267473363304548254f3`,
not the research commit chain. Scanner, score, replay and the reference RiskManager
calculations are unchanged.

## Deliberate OOS decision

The user explicitly chose realtime 2026 forward paper. After activation:
`2026_final_oos_pristine=false`,
`2026_final_oos_abandoned_for_forward_paper=true`,
reason `USER_CHOSE_REALTIME_FORWARD_PAPER`.
The frozen preregistration branch at
`811df39866878d619db2e0c39f374f58d0134b22` remains unchanged.
Candidate: BTCUSDT S2 BTC_ONLY, exploration
`37811156c7a5916e42fba1924945d66bf8980944`, clean validation
`4f8129668a2828b69fde5f148fad6149ac72bb10` (CLEAN_VALIDATION_PASS).
The separate decision and activation records are under
`artifacts/btc-s2-forward-paper-v1/`; the activation records frozen provenance hashes.
No historical trade backfill, inherited position, YTD reconstruction or YTD PnL.

## Frozen rule and causal boundary

BTCUSDT only, UTC 1d. Last 20 completed closes **include** the decision candle.
SMA is the arithmetic mean, standard deviation is sample ddof=1, lower band is
SMA20 minus 2 STD20. FLAT: strict close < lower produces BUY. LONG: close >=
SMA20 produces SELL, including equality. Otherwise HOLD. No stop, TP, RSI, ATR,
SMA200, derivatives, trend/volume gates or other assets.

Public Spot `/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=22` provides warmup.
Validate UTC alignment, monotonic contiguous timestamps, duplicates and positive
finite OHLC. An open daily candle is excluded; source close must be < recognition
wall time. Only the latest completed UTC day is evaluated, never historical catch-up.
Pre-activation closes only update warmup indicators. The first eligible close is
strictly after the actual activation time (normally 23:59:59.999 UTC that day).
The unique generation/strategy/symbol/source-close constraint persists decisions
across restarts. BUY/SELL signals commit **before** requesting the first fresh quote.
HOLD also has one decision record. If recognition is delayed, the actual recognition
and fill times are audited; no historical daily OPEN is used.

Missing quote: RETRYABLE, no fill and no second signal. The same daily decision may
retry while it remains latest. A newer daily candle makes older unfilled decisions
MISSED_SUPERSEDED, never backfilled. Trading OFF consumes a BUY as MISSED_DISABLED;
turning ON later does not resurrect it. SELL/mark updates remain possible while OFF.
`state_after` is the actual ledger state; a pending BUY remains FLAT until filled.

## Explicit full-equity execution

`FULL_EQUITY_NO_STOP_V1` bypasses legacy stop-distance RiskManager sizing and fill
revalidation, while reusing PortfolioService, account, positions, orders, fills,
signals, risk audit, snapshots, quote provider, lease and heartbeat. It is not a
second wallet. Existing reference BUYs still require a valid stop. Nullable stop
means no STOP protection; a missing TP means no TP protection. No fake stop.

BUY reference = fresh public bookTicker ask; SELL and long mark = bid. Paper fill
is q(reference * (1 +/- 0.0005)); q truncates to the ledger's 12 decimal places.
Quantity = q(available_cash / (fill_price * 1.001)); fee = q(q(qty*fill)*0.001).
The actual notional+fee is checked against cash, with no borrowing or short.
SELL uses the entire BTC quantity. Rounding dust remains cash. PnL and fees are
booked by the existing portfolio. Risk audit is approved=true, risk_amount=0,
stop_distance_pct=0, reason `Frozen S2 full-equity paper allocation; no stop`.
Legacy exposure/drawdown/risk settings do not resize or gate this frozen profile.

Audit includes source close, decision, quote reception, fill, decision-to-fill
latency, bid, ask, spread percent, adverse slippage and fee. The daily decision,
signal, risk, order, fill and position are linked. No stop/TP means daily exits only;
manual close is disabled for this profile to preserve the frozen execution rules.

## Activation / operations

1. Inspect production HEAD/PIDs/account/history; disable old trading with
   `market-scanner paper off`. If OPEN position or pending order exists: STOP.
2. Run targeted/full maintained tests. Back up the SQLite DB consistently, dry-run
   migration on a copy and verify old ledger rows/foreign keys unchanged.
3. Stop **only** `crypto-paper`. Back up DB and `.env` again; run
   `market-scanner migrate` (0004_s2_forward). No scanner/replay restart.
4. Set `PAPER_CONFIG` to a copy of `config/paper.s2-forward.example.json`.
   Existing `PAPER_*` overrides take precedence: remove incompatible overrides.
5. Record the user decision JSON, then run exactly once:

   ```sh
   market-scanner paper activate-s2 --confirm ACTIVATE-S2-FORWARD \
     --provenance artifacts/btc-s2-forward-paper-v1/decision-record.json \
     > artifacts/btc-s2-forward-paper-v1/activation.json
   ```

   Activation refuses an active lease, trading ON, open position, reservation or
   outstanding order. It archives the old account totals/config and ledger ID
   cutoffs in `paper_generations`; it deletes no history. New generation starts
   1000 USDT / FLAT / ON at the transaction's actual UTC activation time.
6. Start `crypto-paper`. Gracefully HUP Gunicorn workers to load UI; master PID and
   web service stay active. Verify `/paper`, heartbeat, activation hash, zero new
   signals/orders/fills at warmup, cash/equity1000, no inherited position.

Status: `market-scanner paper status`; kill switch: `market-scanner paper off`.
Full stop: `systemctl --user stop crypto-paper`. Turning ON does not reset activation.
Destructive generic paper reset and repeat S2 activation are refused. Old history
is displayed with its strategy; current portfolio totals belong to the new
explicit generation, not the sum of archived and active PnL.

`/paper` adds strategy/profile/generation/state/activation, latest daily close,
SMA20/STD20/lower, latest action/time and execution state. Normal portfolio and
position cards remain. Warmup action is not presented as an executed signal.

## Safety and migration

Negative cash, unexpected reservation/stop/TP/symbol/strategy, duplicate fill,
foreign post-activation order or stale/pre-decision quote causes a persisted
ERROR generation, trading OFF and exit78. `RestartPreventExitStatus=78` prevents
a restart loop. No corrective trade is attempted. Missing quote/data degrades the
heartbeat without inventing a fill. Inspect the account before operator recovery.

Migration0004 makes `paper_positions.stop_price` nullable and adds
`paper_generations` / `paper_forward_daily_decisions`. Existing stops, rows and
foreign-key references are retained. SQLite batch migration disables FK enforcement
only on its own migration connection outside BEGIN, checks FK integrity before
commit and restores enforcement. History-destructive downgrade is refused.

The change does not alter scanner data, score weights, reference strategy or
RiskManager calculations, research artifacts or final-OOS preregistration.

## Implementation validation

- 30 deterministic S2 cases: strategy boundaries/sample STD, activation causality,
  no catch-up, committed decision before quote, missing quote/retry/supersession,
  ask/bid, costs/full quantity, restart idempotency, OFF behavior, safety latches,
  no fake STOP, no legacy RiskManager call, ledger-preserving migration and UI.
- Targeted S2 + monitoring + analysis: 71 passed.
- Full maintained suite: 813 passed; pytest process exit code0.
- Maintained Ruff: `ruff check crypto_bot tests research` PASS; diff check PASS.
- Browser screenshots: `artifacts/btc-s2-forward-paper-v1/paper-1440.png` and
  `paper-390.png` (isolated synthetic fixture, no real orders).
- Production-sized migration copy: every original table's ordered row count and
  SHA256 identical; integrity_check=ok, foreign_key_check empty. Detailed evidence
  is in runtime `artifacts/btc-s2-forward-paper-v1/migration-dry-run.json`.
