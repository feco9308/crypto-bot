"""Post-trade descriptive metrics only. No strategy, risk or ledger imports."""

from datetime import datetime, timezone
from statistics import mean, median

EXCURSION_FIELDS = [
    "mfe_pct",
    "mae_pct",
    "mfe_price",
    "mae_price",
    "time_to_mfe_seconds",
    "time_to_mae_seconds",
]
TIMING_FIELDS = [
    name
    for minutes in (15, 30, 60)
    for name in (
        f"price_change_{minutes}m_after_entry_pct",
        f"lowest_price_first_{minutes}m",
        f"highest_price_first_{minutes}m",
    )
]
QUALITY_FIELDS = [
    f"entry_to_next_{minutes}m_{side}_pct"
    for minutes in (30, 60)
    for side in ("low", "high")
]
HISTORICAL_FIELDS = (
    EXCURSION_FIELDS
    + TIMING_FIELDS
    + QUALITY_FIELDS
    + [f"timing_price_{m}m_observed_at_utc" for m in (15, 30, 60)]
)


def empty_metrics(status="PENDING"):
    return dict.fromkeys(HISTORICAL_FIELDS) | dict(
        analysis_data_status=status,
        excursion_timeframe=None,
        excursion_source=None,
        excursion_coverage=None,
        extrema_time_precision="CANDLE_OPEN_TIME",
        boundary_policy="FULLY_CONTAINED_CANDLES_ONLY",
    )


def historical_metrics(trade, candles, interval, now_ms):
    """Whole candles only: never attribute pre-entry/post-exit extrema to the trade.

    OHLC cannot identify intra-candle extrema time; times refer to candle opens.
    A complete contiguous candle set is required; missing candles yield nulls.
    """
    result = empty_metrics("PARTIAL")
    step = {"1m": 60000, "5m": 300000}[interval]
    entry, exit = trade["entry_ms"], trade["exit_ms"]
    price = float(trade["entry_fill_price"])
    rows = {int(c["time"]): c for c in candles if int(c["time"]) + step <= now_ms}

    def window(end):
        first = ((entry + step - 1) // step) * step
        last = (end // step) * step
        times = list(range(first, last, step))
        if end > now_ms or not times or any(t not in rows for t in times):
            return None
        return [rows[t] for t in times]

    if exit:
        observed = window(exit)
        if observed:
            high = max(observed, key=lambda c: c["high"])
            low = min(observed, key=lambda c: c["low"])
            # Favorable/adverse excursions are capped at zero on the opposite side.
            result.update(
                mfe_pct=max(0, (high["high"] / price - 1) * 100),
                mae_pct=min(0, (low["low"] / price - 1) * 100),
                mfe_price=high["high"],
                mae_price=low["low"],
                time_to_mfe_seconds=(high["time"] - entry) / 1000,
                time_to_mae_seconds=(low["time"] - entry) / 1000,
                excursion_coverage="COMPLETE_INTERIOR_CANDLES",
            )
    complete_timing = True
    for minutes in (15, 30, 60):
        end = entry + minutes * 60000
        observed = window(end)
        # Use the actual last fully contained candle close, never interpolation.
        if observed:
            lo, hi = min(c["low"] for c in observed), max(c["high"] for c in observed)
            result[f"lowest_price_first_{minutes}m"] = lo
            result[f"highest_price_first_{minutes}m"] = hi
            result[f"price_change_{minutes}m_after_entry_pct"] = (
                observed[-1]["close"] / price - 1
            ) * 100
            result[f"timing_price_{minutes}m_observed_at_utc"] = (
                datetime.fromtimestamp(
                    (observed[-1]["time"] + step) / 1000, timezone.utc
                )
                .isoformat()
                .replace("+00:00", "Z")
            )
            if minutes in (30, 60):
                result[f"entry_to_next_{minutes}m_low_pct"] = (lo / price - 1) * 100
                result[f"entry_to_next_{minutes}m_high_pct"] = (hi / price - 1) * 100
        if not observed:
            complete_timing = False
    result.update(
        excursion_timeframe=interval, excursion_source="BINANCE_PUBLIC_KLINES"
    )
    if (not exit or result["mfe_pct"] is not None) and complete_timing:
        result["analysis_data_status"] = "READY"
    return result


def outcomes(trades, sample_threshold=30):
    closed = [t for t in trades if t["status"] == "CLOSED"]
    pnls = [float(t["realized_pnl"]) for t in closed]
    returns = [t["return_pct"] for t in closed if t["return_pct"] is not None]
    profit = sum(p for p in pnls if p > 0)
    loss = -sum(p for p in pnls if p < 0)
    mfes = [t["mfe_pct"] for t in closed if t.get("mfe_pct") is not None]
    maes = [t["mae_pct"] for t in closed if t.get("mae_pct") is not None]
    return dict(
        trade_count=len(trades),
        closed_trades=len(closed),
        winners=sum(p > 0 for p in pnls),
        losers=sum(p < 0 for p in pnls),
        breakeven=sum(p == 0 for p in pnls),
        win_rate=sum(p > 0 for p in pnls) / len(pnls) * 100 if pnls else None,
        gross_profit=profit,
        gross_loss=loss,
        net_realized_pnl=sum(pnls),
        profit_factor=profit / loss if loss else None,
        profit_factor_status="NO_LOSSES"
        if not loss and profit
        else "UNDEFINED"
        if not loss
        else "DEFINED",
        average_trade_pnl=mean(pnls) if pnls else None,
        median_trade_pnl=median(pnls) if pnls else None,
        average_return=mean(returns) if returns else None,
        median_return=median(returns) if returns else None,
        average_holding_time=mean(t["duration_seconds"] for t in closed)
        if closed
        else None,
        average_mfe=mean(mfes) if mfes else None,
        average_mae=mean(maes) if maes else None,
        mfe_sample_count=len(mfes),
        mae_sample_count=len(maes),
        sample_small=len(closed) < sample_threshold,
    )


def summary(trades):
    result = outcomes(trades)
    result.update(
        total_trades=len(trades),
        open_trades=sum(t["status"] == "OPEN" for t in trades),
        total_fees=sum(float(t["total_fees"]) for t in trades),
    )
    buckets = []
    for low, high, label in [
        (0, 50, "<50"),
        (50, 60, "50-59"),
        (60, 70, "60-69"),
        (70, 80, "70-79"),
        (80, 90, "80-89"),
        (90, 101, "90-100"),
    ]:
        bucket = [
            t
            for t in trades
            if t["market_score"] is not None and low <= t["market_score"] < high
        ]
        buckets.append(dict(bucket=label, **outcomes(bucket)))
    missing = [t for t in trades if t["market_score"] is None]
    if missing:
        buckets.append(dict(bucket="UNKNOWN", **outcomes(missing)))
    result["score_buckets"] = buckets
    result["sample_message"] = (
        "Insufficient sample size"
        if result["sample_small"]
        else "Descriptive sample; no profitability prediction"
    )
    result["sample_threshold"] = 30
    return result
