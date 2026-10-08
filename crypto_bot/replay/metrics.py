"""Descriptive replay metrics, marked equity and explicit sample limitations."""

from collections import defaultdict
from decimal import Decimal
from statistics import mean, median

D = Decimal


def outcome(trades):
    closed = [t for t in trades if t["status"] == "CLOSED"]
    pnls = [float(t["realized_pnl"]) for t in closed]
    returns = [t["return_pct"] for t in closed]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    rs = [t["r_multiple"] for t in closed if t.get("r_multiple") is not None]
    return dict(
        trade_count=len(closed),
        open_positions=sum(t["status"] in ("OPEN", "OPEN_STALE") for t in trades),
        win_rate=len(wins) / len(closed) * 100 if closed else None,
        average_return=mean(returns) if returns else None,
        net_pnl=sum(pnls),
        profit_factor=sum(wins) / -sum(losses) if losses else None,
        profit_factor_status="DEFINED" if losses else "NO_LOSSES_OR_NO_TRADES",
        avg_win=mean(wins) if wins else None,
        avg_loss=mean(losses) if losses else None,
        expectancy=mean(pnls) if pnls else None,
        avg_r=mean(rs) if rs else None,
        median_r=median(rs) if rs else None,
        average_hold_seconds=mean(t["duration_seconds"] for t in closed)
        if closed
        else None,
        largest_win=max(wins) if wins else None,
        largest_loss=min(losses) if losses else None,
        mfe_pct=mean(t["mfe_pct"] for t in closed) if closed else None,
        mae_pct=mean(t["mae_pct"] for t in closed) if closed else None,
        ambiguous_trade_count=sum(t["intrabar_ambiguity"] for t in closed),
        sample_label="INSUFFICIENT SAMPLE"
        if len(closed) < 30
        else "SMALL SAMPLE"
        if len(closed) < 100
        else "LARGER SAMPLE",
        statistically_validated=False,
    )


def bucket(value, edges, labels):
    if value is None:
        return "UNKNOWN"
    for i, (lo, hi) in enumerate(zip(edges, edges[1:])):
        if lo <= value < hi:
            return labels[i]
    return "OUTSIDE_CONFIGURED_BUCKETS"


def breakdowns(trades, curve):
    specs = {
        "symbol": lambda t: t["symbol"],
        "asset_group": lambda t: t["asset_group"],
        "entry_score": lambda t: bucket(
            t["entry_score"],
            [70, 75, 80, 85, 90, 101],
            ["70-75", "75-80", "80-85", "85-90", "90+"],
        ),
        "delta4h": lambda t: bucket(
            t["delta_4h"], [0, 5, 10, 20, float("inf")], ["0-5", "5-10", "10-20", "20+"]
        ),
        "rsi": lambda t: bucket(
            t["rsi"], [40, 50, 60, 70.00001], ["40-50", "50-60", "60-70"]
        ),
        "range_position": lambda t: bucket(
            t["range_position"],
            [0, 0.25, 0.5, 0.75, 1.00001],
            ["0-.25", ".25-.50", ".50-.75", ".75-1.00"],
        ),
        "btc_regime": lambda t: t.get("btc_regime", "UNKNOWN"),
    }
    result = {}
    for name, fn in specs.items():
        groups = defaultdict(list)
        for t in trades:
            groups[fn(t)].append(t)
        if name == "asset_group":
            for group in ("BTC", "ETH", "ALT"):
                groups[group]
        result[name] = {k: outcome(v) for k, v in sorted(groups.items())}
    for kind in ("monthly", "quarterly"):
        groups = defaultdict(list)
        equity = defaultdict(list)

        def period(date):
            return (
                date[:7]
                if kind == "monthly"
                else date[:4] + "-Q" + str((int(date[5:7]) - 1) // 3 + 1)
            )

        for t in trades:
            if t["status"] == "CLOSED":
                groups[period(t["exit_time"])].append(t)
        previous = curve[0] if curve else None
        for row in curve:
            key = period(row["timestamp"])
            if not equity[key] and previous:
                equity[key].append(previous)
            equity[key].append(row)
            previous = row
        result[kind] = {}
        for key, rows in sorted(equity.items()):
            first, last = float(rows[0]["equity"]), float(rows[-1]["equity"])
            peak = first
            dd = 0
            for row in rows:
                value = float(row["equity"])
                peak = max(peak, value)
                dd = max(dd, (peak - value) / peak * 100 if peak else 0)
            result[kind][key] = outcome(groups[key]) | dict(
                pnl=last - first,
                return_pct=(last / first - 1) * 100 if first else None,
                drawdown_pct=dd,
                drawdown_precision="HOURLY_EQUITY_OBSERVATIONS",
            )
    return result


def summary(trades, curve, portfolio, blocked, slippage, exposure_samples):
    v = portfolio.view()
    balance = portfolio.account.initial_balance
    result = outcome(trades)
    result.update(
        final_equity=str(v.equity),
        initial_balance=str(balance),
        net_pnl=str(v.equity - balance),
        realized_pnl=str(v.realized_pnl),
        unrealized_pnl=str(v.unrealized_pnl),
        cash=str(v.cash_balance),
        return_pct=float((v.equity / balance - 1) * 100),
        max_drawdown=str(v.max_drawdown),
        fees=str(v.fees_paid),
        slippage_cost=str(slippage),
        current_exposure=str(v.exposure),
        average_exposure_pct=mean(exposure_samples) if exposure_samples else 0,
        max_exposure_pct=max(exposure_samples, default=0),
        blocked_entries=blocked,
        equity_precision="MINUTE_CLOSED_PRICES_AND_EXECUTION_OPEN_MARKS; CHART STORES HOURLY",
        execution_source="BINANCE_PUBLIC_KLINES_APPROXIMATION",
    )
    result.update({f"blocked_entries_{k}": v for k, v in blocked.items()})
    return result, breakdowns(trades, curve)
