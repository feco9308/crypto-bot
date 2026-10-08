"""Replay-only reporting of existing rejection reasons, without risk decisions."""

from collections import Counter

# Exact existing RiskManager/replay strings. This mapping never gates a trade.
CATEGORIES = {
    "daily loss limit reached": "daily_loss",
    "drawdown limit reached": "drawdown",
    "total exposure limit reached": "total_exposure",
    "symbol exposure limit reached": "symbol_exposure",
    "max open positions reached": "max_positions",
    "invalid stop distance": "invalid_stop_distance",
    "duplicate open position; pyramiding disabled": "duplicate_position",
    "insufficient free balance": "insufficient_free_balance",
    "sized order below minimum value or insufficient free balance/exposure room": "below_minimum_order_or_balance_exposure_room",
    "nonpositive portfolio equity": "nonpositive_equity",
    "take profit already reached at current quote": "take_profit_already_reached",
    "paper trading OFF": "trading_disabled",
    "HOLD; no order requested": "hold_no_order",
    "no open long position": "no_open_position",
    "Fresh portfolio marks unavailable": "missing_portfolio_marks",
    "MISSING_1M_EXECUTION_CANDLE; fallback disabled": "missing_execution_minute",
    "MISSING_EXECUTION_CANDLE": "missing_execution_candle",
}


def reason_key(reasons):
    return reasons if isinstance(reasons, str) else " | ".join(reasons)


def legacy_group(reasons):
    """Retain the historical aggregate, including its substring-based grouping."""
    reason = reason_key(reasons)
    return (
        "daily_loss"
        if "daily loss" in reason
        else "drawdown"
        if "drawdown" in reason
        else "exposure"
        if "exposure" in reason
        else "max_positions"
        if "max open" in reason
        else "other"
    )


def details(reasons):
    raw = dict(reasons)
    categories = Counter()
    for reason, count in raw.items():
        categories[CATEGORIES.get(reason, "other")] += count
    return dict(
        blocked_entry_reasons=raw,
        blocked_entry_categories=dict(categories),
        blocked_entry_attempts=sum(raw.values()),
    )


def from_audit(events, blocked):
    """Read old audits without rewriting results or guessing missing counts.

    Old NO_FILL events omit action: infer it from the then-open fill ledger.
    Old rejected risk decisions are BUY except the explicit SELL/HOLD reasons.
    Keep unmatched historical counts visible rather than labeling them as known.
    """
    held = set()
    reasons = Counter()
    for event in events:
        kind, symbol = event["event"], event["symbol"]
        if kind == "ENTRY_FILL":
            held.add(symbol)
        elif kind == "EXIT_FILL":
            held.discard(symbol)
        elif kind in ("NO_FILL", "RISK_REJECTED", "RISK_DECISION"):
            decision = event.get("decision", {})
            if kind == "RISK_DECISION" and decision.get("approved"):
                continue
            raw = event.get("reasons") or event.get("reason") or decision.get("reasons")
            key = reason_key(raw or ())
            action = event.get("action")
            if action is None:
                if kind == "NO_FILL":
                    action = "SELL" if symbol in held else "BUY"
                elif key in ("no open long position", "HOLD; no order requested"):
                    action = "SELL" if key == "no open long position" else "HOLD"
                else:
                    action = "BUY"
            if action == "BUY":
                reasons[key or "UNKNOWN_REJECTION_REASON"] += 1
    result = details(reasons)
    result["blocked_entry_breakdown_source"] = "STORED_AUDIT_EVENTS"
    result["blocked_entry_unresolved_attempts"] = max(
        0, sum(blocked.values()) - result["blocked_entry_attempts"]
    )
    legacy = Counter()
    for reason, count in reasons.items():
        legacy[legacy_group(reason)] += count
    result["blocked_entry_breakdown_complete"] = (
        result["blocked_entry_attempts"] == sum(blocked.values())
        and legacy == Counter(blocked)
    )
    return result
