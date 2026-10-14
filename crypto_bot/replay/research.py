"""Replay-only research gates/exits. No production order or account access."""

from decimal import DecimalException

from crypto_bot.replay.clock import ms
from crypto_bot.replay.exit_v2 import DEFAULTS as V2_DEFAULTS
from crypto_bot.replay.exit_v2 import ExitV2
from crypto_bot.replay.institutional_exit import DEFAULTS, ParameterizedPeakLock, custom
from crypto_bot.replay.registry import EXITS
from crypto_bot.replay.shadow_adaptive import NAME, AdaptivePeakLock, PeakState
from crypto_bot.trading.domain import D

GATES = ("OFF", "BULLISH_ONLY")
RESEARCH_EXITS = {NAME: DEFAULTS} | V2_DEFAULTS
FILTER_DEFAULTS = dict(
    score_min=None, score_max=None, delta4h_min=None, delta4h_max=None
)


ENTRY_FILTER_FIELDS = dict(
    min_delta_4h=None, min_atr_pct=None, max_atr_pct=None, max_range_position=None
)
ENTRY_FILTER_REASONS = (
    "ENTRY_DELTA_4H_BELOW_MIN",
    "ENTRY_ATR_PCT_BELOW_MIN",
    "ENTRY_ATR_PCT_ABOVE_MAX",
    "ENTRY_RANGE_POSITION_ABOVE_MAX",
    "ENTRY_DELTA_4H_UNAVAILABLE",
    "ENTRY_ATR_PCT_UNAVAILABLE",
    "ENTRY_RANGE_POSITION_UNAVAILABLE",
)


def entry_filter_active(bounds):
    return any(bounds.get(key) is not None for key in ENTRY_FILTER_FIELDS)


def entry_filter_reasons(row, bounds):
    """Current scanner row only; legacy maxima exclusive, new maxima inclusive.

    Unavailable values fail closed only when their dimension is constrained.
    Reasons may overlap: one blocked opportunity can fail both dimensions.
    """
    reasons = []
    for dimension, raw, label in (
        ("score", row.get("total_score"), "SCORE"),
        ("delta4h", row.get("momentum", {}).get("delta_4h"), "DELTA4H"),
    ):
        lo, hi = bounds.get(dimension + "_min"), bounds.get(dimension + "_max")
        if lo is None and hi is None:
            continue
        try:
            value = D(raw) if raw is not None else None
        except (ValueError, TypeError, DecimalException):
            value = None
        if value is None:
            reasons.append(label + "_UNAVAILABLE")
        else:
            if lo is not None and value < D(lo):
                reasons.append(label + "_BELOW_MIN")
            if hi is not None and value >= D(hi):
                reasons.append(label + "_ABOVE_MAX")
    # Entry Research v1 maxima are inclusive; existing maxima above stay exclusive.
    for minimum, maximum, raw, label in (
        (
            "min_delta_4h",
            None,
            row.get("momentum", {}).get("delta_4h"),
            "ENTRY_DELTA_4H",
        ),
        (
            "min_atr_pct",
            "max_atr_pct",
            row.get("features", {}).get("atr_pct"),
            "ENTRY_ATR_PCT",
        ),
        (
            None,
            "max_range_position",
            row.get("features", {}).get("range_position"),
            "ENTRY_RANGE_POSITION",
        ),
    ):
        lo, hi = bounds.get(minimum), bounds.get(maximum)
        if lo is None and hi is None:
            continue
        try:
            value = D(raw) if raw is not None else None
        except (ValueError, TypeError, DecimalException):
            value = None
        if value is None:
            reasons.append(label + "_UNAVAILABLE")
        else:
            if lo is not None and value < D(lo):
                reasons.append(label + "_BELOW_MIN")
            if hi is not None and value > D(hi):
                reasons.append(label + "_ABOVE_MAX")
    return reasons


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
    entry = dict(
        entry_fill_price=str(position.entry_price),
        original_stop_price=str(position.original_stop),
        quantity=str(position.quantity),
        risk_amount=position.entry_data["risk_amount"],
        atr=position.entry_data["atr"],
        notional=str(position.notional),
        entry_fee=str(position.entry_fee),
    )
    parameters = position.entry_data.get("adaptive_exit_parameters", {})
    name = position.entry_data.get("research_protection_policy")
    model = (
        ExitV2(entry, settings, name, parameters)
        if name in V2_DEFAULTS
        else ParameterizedPeakLock(entry, settings, parameters)
        if custom(parameters)
        else AdaptivePeakLock(entry, settings)
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
