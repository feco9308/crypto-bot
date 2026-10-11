"""Replay-only research gates/exits. No production order or account access."""

from crypto_bot.replay.clock import ms
from crypto_bot.replay.registry import EXITS
from crypto_bot.replay.shadow_adaptive import NAME, AdaptivePeakLock, PeakState
from crypto_bot.trading.domain import D

GATES = ("OFF", "BULLISH_ONLY")
RESEARCH_EXITS = {NAME: {}}


def exit_choices():
    return EXITS.items | RESEARCH_EXITS


def gate_state(row, now):
    regime = row.get("btc_regime", "UNKNOWN")
    source = row.get("btc_regime_source_timestamp")
    if row.get("btc_regime_stale"):
        return "STALE"
    if regime not in ("BULLISH", "BEARISH") or not source:
        return "UNKNOWN"
    try:
        age = now - ms(source)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if age <= 0:
        return "UNKNOWN"
    return "STALE" if age > 3600000 else regime


def adaptive(position, settings):
    model = AdaptivePeakLock(
        dict(
            entry_fill_price=str(position.entry_price),
            original_stop_price=str(position.original_stop),
            quantity=str(position.quantity),
            risk_amount=position.entry_data["risk_amount"],
            atr=position.entry_data["atr"],
            notional=str(position.notional),
            entry_fee=str(position.entry_fee),
        ),
        settings,
    )
    state = position.entry_data.get("adaptive_peak_state")
    if state:
        model.state = PeakState(
            D(state["stop"]), D(state["highest"]), state["stage"], tuple(state["times"])
        )
    return model


def save_adaptive(position, model):
    s = model.state
    position.entry_data["adaptive_peak_state"] = dict(
        stop=str(s.stop), highest=str(s.highest), stage=s.stage, times=list(s.times)
    )
