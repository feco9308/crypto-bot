"""Versioned explainable ranking, not a probability or a trading signal."""
import math

SCORE_VERSION = "heuristic-v1"


def clamp(value):
    if not math.isfinite(value):
        raise ValueError("Non-finite score input")
    return max(0.0, min(1.0, value))


def score(features, settings):
    f = features
    trend = (float(f["ema50"] > f["ema200"]) + float(f["price"] > f["ema50"]) + float(f["price"] > f["ema200"])) / 3
    momentum = (clamp(0.5 + f["momentum"] / (2 * settings.momentum_full_score_pct)) + clamp((f["rsi"] - 30) / 40) + clamp(f["range_position"])) / 3
    volume = clamp(f["relative_volume"] / settings.rvol_full_score)
    # Prefer interpretable moderate volatility, penalize extremes symmetrically.
    volatility = clamp(1 - abs(f["atr_pct"] - settings.atr_target_pct) / settings.atr_target_pct)
    liquidity = (clamp(f["quote_volume"] / settings.liquidity_full_score_volume) + clamp(1 - f["spread"] / settings.maximum_spread)) / 2
    fractions = dict(trend=trend, momentum=momentum, volume=volume, volatility=volatility, liquidity=liquidity)
    denominator = sum(settings.weights.values())
    components = {k: clamp(v) * settings.weights[k] / denominator * 100 for k, v in fractions.items()}
    reasons = [f"{f['trend']} EMA structure: fast {f['ema50']:.6g}, slow {f['ema200']:.6g}", f"RSI {f['rsi']:.1f}; price momentum {f['momentum']:+.2f}%; range position {f['range_position']:.0%}", f"relative volume {f['relative_volume']:.2f}x", f"ATR {f['atr_pct']:.2f}% (target {settings.atr_target_pct:.2f}%)", f"spread {f['spread']:.4f}%; 24h quote volume {f['quote_volume']:,.0f}"]
    return dict(total_score=sum(components.values()), components=components, reasons=reasons, score_version=SCORE_VERSION)
