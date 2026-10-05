"""Original RSI14 + adjusted EMA9/21 semantics; informational only."""

import math


def adjusted_ema(values, period):
    decay = 1 - 2 / (period + 1)
    numerator = denominator = 0.0
    for value in values:
        numerator = value + decay * numerator
        denominator = 1 + decay * denominator
    return numerator / denominator


def legacy_rsi(values):
    # Match ta.RSIIndicator: first zero delta; adjust=False Wilder EMA seed.
    if len(values) < 14:
        return float("nan")
    gain = loss = 0.0
    for a, b in zip(values, values[1:]):
        gain = gain * 13 / 14 + max(b - a, 0) / 14
        loss = loss * 13 / 14 + max(a - b, 0) / 14
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


class LegacyRsiEma:
    def evaluate(self, closes):
        if not closes or any(not math.isfinite(v) or v <= 0 for v in closes):
            raise ValueError("Finite positive closes required")
        rsi = legacy_rsi(closes)
        e9, e21 = adjusted_ema(closes, 9), adjusted_ema(closes, 21)
        rsi_signal = "BUY" if rsi < 30 else "SELL" if rsi > 70 else "WAIT"
        combined = (
            "BUY"
            if rsi < 30 and e9 > e21
            else "SELL"
            if rsi > 70 and e9 < e21
            else "WAIT"
        )
        return dict(
            rsi=rsi if math.isfinite(rsi) else None,
            ema9=e9,
            ema21=e21,
            signal_rsi=rsi_signal,
            signal=combined,
        )
