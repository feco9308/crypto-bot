"""Registry adapter reusing the existing pure OHLC counterfactual path functions."""

from crypto_bot.replay.registry import EXITS
from crypto_bot.trading.domain import D
from crypto_bot.web.exit_simulator import Scenario, State, levels, paths, walk

KINDS = {
    "baseline_v1": "BASELINE",
    "fixed_tp": "FIXED_TP",
    "r_tp": "R_TP",
    "break_even": "BREAK_EVEN",
    "trailing_pct": "TRAILING",
    "trailing_r": "R_TRAILING",
    "profit_lock": "PROFIT_LOCK",
}


def policy(name, parameters):
    EXITS.get(name)
    return Scenario(name, KINDS[name], parameters)


def observe(candle, position, model, conservative=True):
    """Called only after candle close. No selection based on a later trade result.

    Conservative/optimistic choose the lowest/highest valid exit in this bar.
    A carry branch is marked ambiguous but cannot use future PnL to choose an exit.
    These are local intrabar policies, NOT global portfolio return bounds.
    """
    entry = D(position.entry_price)
    risk = entry - position.original_stop
    state = State(position.stop_price, position.highest, position.overlay_active)
    tp, activation = levels(model, entry, risk)
    candidates = []
    survivors = []
    for points in paths(candle, model, state, entry, risk, activation):
        updated, hit = walk(points, state, model, entry, risk, tp, activation)
        if hit:
            candidates.append((hit, points, updated))
        else:
            survivors.append(updated)
    if not candidates:
        updated = survivors[0]
        position.highest = updated.highest
        position.stop_price = updated.stop
        position.overlay_active = updated.active
        return None, False, [D(str(candle["high"])), D(str(candle["low"]))]
    candidates.sort(key=lambda v: (v[0][0], v[0][1], v[1]))
    hit, points, updated = candidates[0] if conservative else candidates[-1]
    # Preserve the selected path's actual ratcheted threshold for closed-trade audit.
    position.highest = updated.highest
    position.stop_price = updated.stop
    position.overlay_active = updated.active
    signatures = {(h[0], h[1]) for h, _, _ in candidates}
    ambiguous = len(signatures) > 1 or bool(survivors)
    visited = [points[0]]
    for i in range(2, len(points) + 1):
        _, prefix_hit = walk(points[:i], state, model, entry, risk, tp, activation)
        if prefix_hit:
            visited.append(prefix_hit[0])
            break
        visited.append(points[i - 1])
    return hit, ambiguous, visited
