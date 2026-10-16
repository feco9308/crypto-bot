"""Replay-only, per-snapshot BUY ordering; never changes eligibility or risk."""

from dataclasses import asdict
from decimal import Context, DecimalException, localcontext
from fractions import Fraction

from crypto_bot.trading.domain import D

POLICIES = ("OFF", "market_score_desc_v1", "acceleration_efficiency_v1")


def number(value):
    try:
        return D(value)
    except (ValueError, TypeError, DecimalException):
        return None


def display(value):
    if value is None:
        return None
    # Sorting uses exact rational arithmetic; this Decimal is audit display only.
    with localcontext(Context(prec=50)):
        return str(D(value.numerator) / D(value.denominator))


def ranked(pending, policy):
    """Return copied BUY rows with metadata. Signals and caller rows stay intact.

    Held-only rows cannot join the pool. Preserve any defensive unranked BUY
    rather than silently dropping an otherwise accepted signal. Normal held-only
    rows are SELL/HOLD because the unchanged strategy knows the position exists.
    """
    candidates = []
    other = []
    for signal, row in pending:
        if signal.action != "BUY" or not row.get("auto_eligible", True):
            other.append((signal, row))
            continue
        score = D(row["total_score"])
        delta = number(row.get("momentum", {}).get("delta_4h"))
        volume = number(row.get("features", {}).get("relative_volume"))
        value = (
            Fraction(score)
            if policy == "market_score_desc_v1"
            else Fraction(delta) / Fraction(volume)
            if delta is not None and volume is not None and volume > 0
            else None
        )
        candidates.append((signal, row, score, delta, volume, value))
    candidates.sort(
        key=lambda c: (
            c[5] is None,
            -c[5] if c[5] is not None else 0,
            -Fraction(c[2]),
            c[0].symbol,
        )
    )
    result = []
    for rank, (signal, row, score, delta, volume, value) in enumerate(candidates, 1):
        audit = dict(
            entry_candidate_id=signal.timestamp.isoformat() + "Z/" + signal.symbol,
            entry_rank=rank,
            entry_candidate_pool_size=len(candidates),
            entry_ranking_policy=policy,
            entry_ranking_value=display(value),
            entry_ranking_value_available=value is not None,
        )
        event = dict(
            event="ENTRY_CANDIDATE_RANKED",
            timestamp=signal.timestamp.isoformat() + "Z",
            symbol=signal.symbol,
            ranking_policy=policy,
            rank=rank,
            candidate_pool_size=len(candidates),
            ranking_value=display(value),
            ranking_value_available=value is not None,
            market_score=str(score),
            delta_4h=str(delta) if delta is not None else None,
            relative_volume=str(volume) if volume is not None else None,
            original_strategy_decision=asdict(signal),
            **audit,
        )
        result.append(
            (signal, row | {"_entry_ranking": audit, "_ranking_event": event})
        )
    # Legacy execution already processes SELL before BUY. Keep SELL order and
    # retain defensive non-candidates; do not take a top-N or stop at a rejection.
    return (
        [item for item in other if item[0].action != "BUY"]
        + result
        + [item for item in other if item[0].action == "BUY"]
    )
