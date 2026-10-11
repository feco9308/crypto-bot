# BTC regime entry gate — Replay Research only

The normal Replay Lab supports per-variant `btc_regime_entry_gate`:
`OFF` (default/backward compatibility) or `BULLISH_ONLY`. It uses the existing
scanner row's BTC regime, not a new indicator or separately fetched BTC feed:
BTCUSDT row EMA50 > EMA200 = BULLISH; otherwise BEARISH; absent row = UNKNOWN.
This is exactly the regime persisted on existing canonical entries and used
in Shadow breakdowns. If BTC is not in the scanner's current rows, the gate
fails closed; it does not redefine the universe to obtain another regime.

The BTC source timestamp is the last closed BTC candle used for that row.
Market freshness/contiguous history are checked by unchanged HistoricalUniverse;
all indicator observations have close_time < simulated now. The BUY gate also
requires a source strictly before now and no more than one hour old. Missing,
uncomputable, future or stale source cannot pass BULLISH_ONLY.

The strategy evaluates unchanged first. Only actual BUY opportunities are gated
before execution/risk sizing. HOLD is not counted as a blocked BUY; SELL/stop/TP
are not gated. A rejected opportunity reserves no cash/slot/exposure. Passed
BUYs still use unchanged RiskManager and PortfolioService with run-local
MemoryLedger, real simulated cash/slots/exposure/sizing and ReplayExecution.
This is a portfolio replay, not filtering persisted Shadow trades.

Each rejection records `BTC_REGIME_ENTRY_BLOCK`, symbol, decision timestamp,
raw regime and effective regime state, source timestamp, strategy score and
full original strategy decision. Stored audits reconstruct exact BEARISH /
UNKNOWN / STALE counts and rejection-category totals, including after resume.
Result JSON and compact export include `btc_regime_entry_block_count` and
`btc_regime_entry_blocks`. Gate field uses the normal shared config schema and
is retained by UI import/export, presets and POST /api/replay/runs.

## Adaptive exit in normal research replay

D uses the same fixed adaptive_peak_lock_v1 stop model as Shadow, through a
small replay-only adapter. It is not registered in production/paper execution.
Its serializable peak/stop/activation state lives in replay entry_data and
therefore survives existing checkpoints. Fee/slippage/sizing use the normal
portfolio replay. This policy remains stop-only; the existing policies continue
to use original stop plus their existing strategy exits. The BTC gate never
blocks an exit. R uses each actual simulated entry's risk amount and quantity.

Quantized break-even dust in adaptive outcome classification is flat using the
existing 0.000001 percentage-point research tolerance. Other policy statistics
are unchanged. No strategy/risk/score/existing exit implementation is edited.

## Annual experiment method

A OFF fixed_tp 1.5%; B BULLISH_ONLY fixed_tp 1.5%; C BULLISH_ONLY r_tp 0.5R;
D BULLISH_ONLY adaptive_peak_lock_v1. Period 2024-01-01 → 2025-01-01,
EXPLORATION, daily loss/drawdown guards 100%, quarantine stale positions,
conservative OHLC, strict no-lookahead, no 5m fallback. Other strategy/risk
settings are copied from anchor 04c2ef76ceac41199a922ae55480d16b variant 0.

Each variant runs as a separate normal ReplayEngine job. This avoids the current
multi-variant scanner history's held-symbol union coupling the control to gate
variants. All use the same frozen full candidate list and anchor archive
revisions (including known missing archives), using the already-tested pinned
public-data provider. Known revision mismatches fail rather than silently
refreshing the source. Newly required archive objects retain their hashes in
each result. No data-coverage knowledge influences entries or exits.

The dedicated systemd research runner temporarily replaces only the idle replay
worker under its existing lock, processes at most two independent jobs at once,
and restores the ordinary worker on completion. Jobs stay visible/controlable
in the existing normal Replay UI. Scanner/paper services remain running.
No production DB migration/reset/settings change or historical audit rewrite.
The runner/config/checkpoints/logs and per-variant compact exports are retained
under artifacts/replay-btc-gate-2024. This remains EXPLORATION; no optimizer,
EMA search, extra filter or production promotion.
