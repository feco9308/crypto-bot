"""Pure candle calculations, shared by current and future historical analysis."""

import math
import statistics


def ema(values, period):
    if period < 1 or len(values) < period:
        raise ValueError("Insufficient EMA data")
    result = values[0]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        result += alpha * (value - result)
    return result


def wilder(values, period):
    if period < 1 or len(values) < period:
        raise ValueError("Insufficient Wilder data")
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = (result * (period - 1) + value) / period
    return result


def rsi(values, period=14):
    changes = [b - a for a, b in zip(values, values[1:])]
    gain = wilder([max(x, 0) for x in changes], period)
    loss = wilder([max(-x, 0) for x in changes], period)
    return (
        50.0
        if gain == loss == 0
        else 100.0
        if loss == 0
        else 100 - 100 / (1 + gain / loss)
    )


def atr(candles, period=14):
    ranges = [
        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
        for a, b in zip(candles, candles[1:])
    ]
    return wilder(ranges, period)


def features(candles, market, settings):
    if (
        not all(
            math.isfinite(x)
            for x in (market.quote_volume, market.price_change_pct, market.spread_pct)
        )
        or market.quote_volume < 0
    ):
        raise ValueError("Invalid market statistics")
    required = max(
        settings.ema_slow * 2,
        settings.volume_period + 1,
        settings.range_period,
        settings.rsi_period + 1,
        settings.atr_period + 1,
        settings.price_momentum_period + 1,
    )
    if len(candles) < required:
        raise ValueError(f"Need at least {required} closed candles")
    step = {"5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000, "1d": 86400000}[
        settings.timeframe
    ]
    for i, c in enumerate(candles):
        if (
            not all(
                math.isfinite(x) for x in (c.open, c.high, c.low, c.close, c.volume)
            )
            or min(c.open, c.high, c.low, c.close) <= 0
            or c.volume < 0
            or c.high < max(c.open, c.close)
            or c.low > min(c.open, c.close)
            or c.high < c.low
            or c.close_time < c.open_time
        ):
            raise ValueError("Invalid candle")
        if i and c.open_time - candles[i - 1].open_time != step:
            raise ValueError("Missing, duplicated or unordered candles")
    close = [c.close for c in candles]
    price = close[-1]
    average = statistics.mean(
        c.volume for c in candles[-settings.volume_period - 1 : -1]
    )
    recent = candles[-settings.range_period :]
    high, low = max(c.high for c in recent), min(c.low for c in recent)
    atr_value = atr(candles, settings.atr_period)
    fast, slow = ema(close, settings.ema_fast), ema(close, settings.ema_slow)
    returns = [
        (b / a - 1) * 100
        for a, b in zip(
            close[-settings.range_period - 1 :], close[-settings.range_period :]
        )
    ]
    return dict(
        price=price,
        ema50=fast,
        ema200=slow,
        rsi=rsi(close, settings.rsi_period),
        atr=atr_value,
        atr_pct=atr_value / price * 100,
        volume=candles[-1].volume,
        average_volume=average,
        relative_volume=candles[-1].volume / average if average else 0,
        spread=market.spread_pct,
        momentum=(price / close[-settings.price_momentum_period - 1] - 1) * 100,
        price_change_pct=market.price_change_pct,
        range_position=(price - low) / (high - low) if high > low else 0.5,
        volatility=statistics.pstdev(returns),
        quote_volume=market.quote_volume,
        trend="bullish"
        if fast > slow and price > fast
        else "bearish"
        if fast < slow and price < fast
        else "mixed",
        candle_time=candles[-1].close_time,
    )
