"""Pure read-only LONG counterfactuals. No execution, strategy or ledger imports.

OHLC results are bounds under continuous monotone price legs plus open gaps,
not tick reconstructions. Branches retain early exits and surviving paths.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from statistics import mean, median

D = Decimal
UNIT = D("0.000000000001")
SOURCE = "BINANCE_PUBLIC_KLINES_APPROXIMATION"


def money(value):
    return format(value.quantize(UNIT, rounding=ROUND_DOWN), "f")


def timestamp(ms):
    return (
        datetime.fromtimestamp(ms / 1000, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class Scenario:
    name: str
    kind: str
    parameters: dict


def scenarios(profit_locks=None):
    """Versioned analysis presets; callers can configure profit-lock pairs."""
    out = [Scenario("BASELINE", "BASELINE", {})]
    for pct in ("0.5", "1", "1.5", "2", "3", "4", "5"):
        out.append(Scenario(f"TP_{pct}PCT", "FIXED_TP", {"tp_pct": pct}))
    for r in ("0.5", "1", "1.5", "2", "2.5", "3"):
        out.append(Scenario(f"TP_{r}R", "R_TP", {"tp_r": r}))
    for r in ("0.5", "1", "1.5"):
        for buffer in ("0", "0.1", "0.2"):
            out.append(
                Scenario(
                    f"BE_{r}R_BUFFER_{buffer}PCT",
                    "BREAK_EVEN",
                    {"activation_r": r, "buffer_pct": buffer},
                )
            )
    for activation in ("1", "1.5", "2", "3"):
        for distance in ("0.5", "1", "1.5", "2"):
            out.append(
                Scenario(
                    f"TRAIL_{activation}PCT_{distance}PCT",
                    "TRAILING",
                    {"activation_pct": activation, "distance_pct": distance},
                )
            )
    for activation in ("1", "1.5", "2"):
        for distance in ("0.5", "1"):
            out.append(
                Scenario(
                    f"TRAIL_{activation}R_{distance}R",
                    "R_TRAILING",
                    {"activation_r": activation, "distance_r": distance},
                )
            )
    for activation, lock in profit_locks or [
        ("1", "0.2"),
        ("2", "1"),
        ("3", "2"),
        ("5", "3"),
    ]:
        out.append(
            Scenario(
                f"LOCK_{activation}PCT_{lock}PCT",
                "PROFIT_LOCK",
                {"activation_pct": str(activation), "lock_pct": str(lock)},
            )
        )
    return out


def baseline(trade):
    return dict(
        triggered=False,
        trigger_reason="ACTUAL_EXIT_FALLBACK",
        actual_exit_reason=trade.get("exit_reason"),
        trigger_time_utc=trade["exit_time"],
        trigger_price=trade.get("exit_bid"),
        fill_price=trade["exit_fill_price"],
        fee=trade["exit_fee"],
        slippage=trade.get("exit_slippage"),
        return_pct=trade["return_pct"],
        realized_pnl=trade["realized_pnl"],
        difference_vs_actual_pct=0.0,
        difference_vs_actual_pnl=money(D(0)),
        time_precision="ACTUAL_RECORDED_TIME",
    )


def costs(trade):
    """Use recorded original config only; never silently substitute current defaults."""
    cfg = trade.get("paper_config_snapshot", {})
    fee, slip = D(str(cfg["paper_fee_pct"])), D(str(cfg["paper_slippage_pct"]))
    if (
        not fee.is_finite()
        or not slip.is_finite()
        or not (0 <= fee < 100 and 0 <= slip < 100)
    ):
        raise ValueError("Invalid recorded fee/slippage")
    return fee / 100, slip / 100


def exit_result(trade, price, time, reason, fee_rate, slip_rate):
    qty = D(trade["quantity"])
    fill = D(money(price * (1 - slip_rate)))
    notional = D(money(qty * fill))
    fee = D(money(notional * fee_rate))
    pnl = D(money(notional - fee - D(trade["notional"]) - D(trade["entry_fee"])))
    cost = D(trade["notional"]) + D(trade["entry_fee"])
    ret = float(pnl / cost * 100)
    return dict(
        triggered=True,
        trigger_reason=reason,
        trigger_time_utc=timestamp(time),
        trigger_price=str(price),
        fill_price=str(fill),
        fee=str(fee),
        slippage=money(abs(price - fill) * qty),
        return_pct=ret,
        realized_pnl=str(pnl),
        difference_vs_actual_pct=ret - trade["return_pct"],
        difference_vs_actual_pnl=money(pnl - D(trade["realized_pnl"])),
        time_precision="CANDLE_OPEN_TIME",
        actual_exit_reason=trade.get("exit_reason"),
    )


@dataclass(frozen=True)
class State:
    stop: Decimal
    highest: Decimal
    active: bool = False


def levels(scenario, entry, risk):
    p, kind = scenario.parameters, scenario.kind
    tp = (
        entry * (1 + D(p["tp_pct"]) / 100)
        if kind == "FIXED_TP"
        else entry + risk * D(p["tp_r"])
        if kind == "R_TP"
        else None
    )
    activation = (
        entry + risk * D(p["activation_r"])
        if "activation_r" in p
        else entry * (1 + D(p["activation_pct"]) / 100)
        if "activation_pct" in p
        else None
    )
    return tp, activation


def ratchet(state, price, scenario, entry, risk, activation):
    high = max(state.highest, price)
    active = state.active or (activation is not None and high >= activation)
    stop = state.stop
    if active:
        p = scenario.parameters
        if scenario.kind == "BREAK_EVEN":
            stop = max(stop, entry * (1 + D(p["buffer_pct"]) / 100))
        elif scenario.kind == "PROFIT_LOCK":
            stop = max(stop, entry * (1 + D(p["lock_pct"]) / 100))
        elif scenario.kind == "TRAILING":
            stop = max(stop, high * (1 - D(p["distance_pct"]) / 100))
        elif scenario.kind == "R_TRAILING":
            stop = max(stop, high - risk * D(p["distance_r"]))
    return State(stop, high, active)


def paths(candle, scenario, state, entry, risk, activation):
    o, h, lo, c = [D(str(candle[k])) for k in ("open", "high", "low", "close")]
    out = [(o, h, lo, c), (o, lo, h, c)]
    # OHLC also permits activation/partial ratcheting before a retracement,
    # before the full recorded high. Include the earliest feasible stop touch.
    if scenario.kind in ("TRAILING", "R_TRAILING", "BREAK_EVEN", "PROFIT_LOCK"):
        peak = max(o, state.highest, activation)
        if scenario.kind == "TRAILING":
            peak = max(peak, lo / (1 - D(scenario.parameters["distance_pct"]) / 100))
        elif scenario.kind == "R_TRAILING":
            peak = max(peak, lo + risk * D(scenario.parameters["distance_r"]))
        if o <= peak <= h:
            out.append((o, peak, lo, h, c))
    return sorted(set(out))


def walk(points, state, scenario, entry, risk, tp, activation):
    """Return first valid exit on each path; open gaps fill at open, not stop."""
    original_stop = entry - risk
    first = points[0]
    if first <= state.stop:
        return state, (
            first,
            "ORIGINAL_STOP" if state.stop == original_stop else scenario.kind,
        )
    if tp is not None and first >= tp:
        return state, (first, scenario.kind)
    state = ratchet(state, first, scenario, entry, risk, activation)
    if first <= state.stop:
        return state, (first, scenario.kind)
    for previous, price in zip(points, points[1:]):
        if price >= previous:
            if tp is not None and previous <= tp <= price:
                return state, (tp, scenario.kind)
            state = ratchet(state, price, scenario, entry, risk, activation)
        elif price <= state.stop:
            reason = "ORIGINAL_STOP" if state.stop == original_stop else scenario.kind
            return state, (state.stop, reason)
    return state, None


def interior(trade, candles, interval):
    step = {"1m": 60000, "5m": 300000}[interval]
    first = (trade["entry_ms"] + step - 1) // step * step
    last = trade["exit_ms"] // step * step
    rows = {int(c["time"]): c for c in candles}
    times = range(first, last, step)
    if not times or any(t not in rows for t in times):
        return None
    return [rows[t] for t in times]


def simulate(trade, candles, interval, selected=None):
    """One stored entry and one cached candle path, all analysis-only scenarios."""
    observed = interior(trade, candles, interval)
    try:
        fee, slip = costs(trade)
        entry, stop = D(trade["entry_fill_price"]), D(trade["stop_price"])
        risk = entry - stop
        valid_config = risk > 0 and stop > 0 and D(trade["quantity"]) > 0
    except (KeyError, ValueError, ArithmeticError):
        valid_config = False
    result = []
    for scenario in scenarios():
        if selected and scenario.name != selected:
            continue
        base = baseline(trade)
        record = dict(
            scenario_name=scenario.name,
            scenario_type=scenario.kind,
            parameters=scenario.parameters,
            data_timeframe=interval,
            data_source=SOURCE,
            intrabar_ambiguity=False,
            boundary_policy="FULLY_CONTAINED_CANDLES_ONLY",
            path_model="MONOTONE_OHLC_LEGS_WITH_PARTIAL_ACTIVATION_AND_OPEN_GAPS",
            original_stop_active=True,
            config_source="ENTRY_SIGNAL_CONFIGURATION",
            simulation_status="READY",
            usable=True,
        )
        if scenario.kind == "BASELINE":
            record.update(
                base,
                conservative_result=base,
                optimistic_result=base,
                data_source="ACTUAL_PAPER_LEDGER",
            )
        elif not observed or not valid_config:
            empty = dict.fromkeys(base)
            record.update(
                empty,
                conservative_result=None,
                optimistic_result=None,
                simulation_status="UNAVAILABLE",
                usable=False,
                unavailable_reason="MISSING_INTERIOR_CANDLES"
                if not observed
                else "MISSING_OR_INVALID_ORIGINAL_CONFIGURATION",
            )
        else:
            tp, activation = levels(scenario, entry, risk)
            states, exits = {State(stop, entry)}, []
            for candle in observed:
                survivors = set()
                for state in states:
                    for points in paths(
                        candle, scenario, state, entry, risk, activation
                    ):
                        updated, hit = walk(
                            points, state, scenario, entry, risk, tp, activation
                        )
                        if hit:
                            exits.append(
                                exit_result(
                                    trade, hit[0], candle["time"], hit[1], fee, slip
                                )
                            )
                        else:
                            survivors.add(updated)
                states = survivors
                if not states:
                    break
            if states:
                exits.append(base)

            # Keep first exits on valid branches; never assume a high/low order.
            def ordering(x):
                return (
                    D(x["realized_pnl"]),
                    x["trigger_time_utc"],
                    x["trigger_reason"],
                    str(x["trigger_price"]),
                )

            conservative = min(exits, key=ordering)
            optimistic = max(exits, key=ordering)
            signatures = {
                (x["trigger_time_utc"], x["trigger_reason"], x["trigger_price"])
                for x in exits
            }
            record.update(
                conservative,
                conservative_result=conservative,
                optimistic_result=optimistic,
                intrabar_ambiguity=len(signatures) > 1,
            )
        for key in (
            "trigger_price",
            "fill_price",
            "fee",
            "slippage",
            "realized_pnl",
            "return_pct",
        ):
            record["simulated_" + key] = record.get(key)
        result.append(record)
    return result


def aggregate(trades, selected=None):
    rows = []
    for scenario in scenarios():
        if selected and scenario.name != selected:
            continue
        matches = [
            s
            for t in trades
            for s in t["scenarios"]
            if s["scenario_name"] == scenario.name
        ]
        usable = [s for s in matches if s.get("usable")]
        returns = [s["return_pct"] for s in usable]
        pnls = [D(s["realized_pnl"]) for s in usable]
        differences = [s["difference_vs_actual_pct"] for s in usable]
        givebacks = [
            max(0, t["mfe_pct"] - s["return_pct"])
            for t in trades
            if t.get("mfe_pct") is not None
            for s in t["scenarios"]
            if s["scenario_name"] == scenario.name and s.get("usable")
        ]
        rows.append(
            dict(
                scenario_name=scenario.name,
                scenario_type=scenario.kind,
                parameters=scenario.parameters,
                trade_count=len(trades),
                usable_trade_count=len(usable),
                ambiguous_trade_count=sum(s["intrabar_ambiguity"] for s in usable),
                average_return=mean(returns) if returns else None,
                median_return=median(returns) if returns else None,
                total_pnl=money(sum(pnls, D(0))) if usable else None,
                win_rate=sum(p > 0 for p in pnls) / len(pnls) * 100 if pnls else None,
                loss_rate=sum(p < 0 for p in pnls) / len(pnls) * 100 if pnls else None,
                average_difference_vs_actual=mean(differences) if differences else None,
                total_difference_vs_actual=money(
                    sum((D(s["difference_vs_actual_pnl"]) for s in usable), D(0))
                )
                if usable
                else None,
                average_profit_giveback=mean(givebacks) if givebacks else None,
                profit_giveback_sample_count=len(givebacks),
            )
        )
    return dict(
        scenarios=rows,
        sample_size=len(trades),
        result_basis="CONSERVATIVE",
        profit_giveback_basis="ACTUAL_HORIZON_MFE_MINUS_SIMULATED_NET_RETURN_PP",
        statistically_validated=False,
        sample_message="NOT STATISTICALLY VALIDATED",
        max_drawdown_proxy=None,
        drawdown_status="NOT_DEFINED_FOR_OVERLAPPING_COUNTERFACTUAL_TRADES",
        read_only=True,
        counterfactual_only=True,
    )
