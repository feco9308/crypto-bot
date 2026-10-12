"""Additive research observations; never used for entry, risk or execution."""

from collections import defaultdict
from statistics import mean, median

from crypto_bot.replay.metrics import outcome
from crypto_bot.trading.domain import D, q


def trade_observations(position, settings, now):
    p = position
    risk = D(p.entry_data["risk_amount"])
    cost = p.notional + p.entry_fee
    peak_fill = q(p.mfe_price * (1 - settings.paper_slippage_pct / 100))
    peak_notional = q(peak_fill * p.quantity)
    peak_profit = peak_notional - q(peak_notional * settings.paper_fee_pct / 100) - cost
    closed = p.status == "CLOSED"
    giveback = peak_profit - p.realized_pnl if closed else None
    stage = p.entry_data.get("adaptive_peak_state", {}).get("stage", 0)
    label = ("INITIAL_STOP", "BREAK_EVEN", "PEAK_TRAIL", "TIGHT_PEAK_TRAIL")[stage]
    if p.status == "OPEN":
        label = (
            "OPEN_STALE" if p.entry_data.get("stale_state", {}).get("stale") else "OPEN"
        )
    elif p.exit_reason == "STRATEGY":
        label = "STRATEGY_EXIT"
    return {
        "mfe_abs_pct": max(0, float((p.mfe_price / p.entry_price - 1) * 100)),
        "mae_abs_pct": max(0, float((1 - p.mae_price / p.entry_price) * 100)),
        "mfe_r": max(0, float((p.mfe_price - p.entry_price) * p.quantity / risk)),
        "mae_r": max(0, float((p.entry_price - p.mae_price) * p.quantity / risk)),
        "maximum_high_since_entry": str(p.mfe_price),
        "maximum_unrealized_profit": str(peak_profit),
        "realized_profit": str(p.realized_pnl) if closed else None,
        "peak_to_exit_giveback_r": float(giveback / risk) if closed else None,
        "peak_to_exit_giveback_pct": float(giveback / peak_profit * 100)
        if closed and peak_profit > 0
        else None,
        "peak_to_exit_giveback_percentage_points": float(giveback / cost * 100)
        if closed
        else None,
        "research_exit_stage": label,
        "maximum_reached_r": max(
            0, float((p.mfe_price - p.entry_price) * p.quantity / risk)
        ),
        "final_stop_price": str(p.stop_price),
        "trail_activation_timestamp": p.entry_data.get("adaptive_peak_state", {}).get(
            "times", [None, None, None]
        )[1],
    }


def research_outcome(trades):
    closed = [t for t in trades if t["status"] == "CLOSED"]
    rs = [t["r_multiple"] for t in closed if t.get("r_multiple") is not None]
    mfes = [t["mfe_r"] for t in closed if t.get("mfe_r") is not None]
    givebacks = [
        t["peak_to_exit_giveback_r"]
        for t in closed
        if t.get("peak_to_exit_giveback_r") is not None
    ]
    losers = [
        t
        for t in closed
        if t["realized_pnl"] is not None
        and D(t["realized_pnl"]) < 0
        and (t.get("return_pct") or 0) < -1e-6
    ]
    winners = [t for t in closed if (t.get("return_pct") or 0) > 1e-6]
    output = {
        "research_closed_trades": len(closed),
        "expectancy_r": mean(rs) if rs else None,
        "average_MFE_R": mean(mfes) if mfes else None,
        "median_MFE_R": median(mfes) if mfes else None,
        "average_realized_R": mean(rs) if rs else None,
        "median_realized_R": median(rs) if rs else None,
        "average_giveback_R": mean(givebacks) if givebacks else None,
        "median_giveback_R": median(givebacks) if givebacks else None,
    }
    for label, value in [("0_5R", 0.5), ("1R", 1), ("2R", 2)]:
        output["losers_reached_" + label] = sum(
            t.get("mfe_r", 0) >= value for t in losers
        )
    for value in (25, 50):
        output[f"winners_gave_back_gt_{value}pct"] = sum(
            t.get("peak_to_exit_giveback_pct") is not None
            and t["peak_to_exit_giveback_pct"] > value
            for t in winners
        )
    turnover = sum(
        (
            D(t["notional"])
            + (
                q(D(t["quantity"]) * D(t["exit_price"]))
                if t.get("exit_price")
                else D(0)
            )
            for t in trades
        ),
        D(0),
    )
    output["turnover_notional"] = str(turnover)
    return output


def research_summary(trades):
    specs = {
        "research_btc_regime": lambda t: t.get(
            "institutional_btc_regime", t.get("research_btc_regime", "UNKNOWN")
        ),
        "research_symbol": lambda t: t["symbol"],
        "research_volatility": lambda t: (
            "0-1%"
            if t["atr_pct"] < 1
            else "1-2%"
            if t["atr_pct"] < 2
            else "2-3%"
            if t["atr_pct"] < 3
            else "3-5%"
            if t["atr_pct"] < 5
            else ">5%"
        ),
        "research_exit_stage": lambda t: t.get("research_exit_stage", "UNKNOWN"),
    }
    result = {}
    for key, fn in specs.items():
        groups = defaultdict(list)
        for t in trades:
            groups[fn(t)].append(t)
        if key == "research_btc_regime":
            for label in ("BULL", "NEUTRAL", "BEAR"):
                groups[label]
        result[key] = {
            k: outcome(v) | research_outcome(v) for k, v in sorted(groups.items())
        }
    return research_outcome(trades), result
