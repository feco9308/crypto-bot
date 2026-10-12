"""Replay-only multi-timeframe strategy and as-of market context. No order sender."""

from bisect import bisect_left
from dataclasses import dataclass
from functools import lru_cache
from statistics import correlation

from crypto_bot.data.base import Candle
from crypto_bot.indicators.technical import ema
from crypto_bot.replay.clock import at
from crypto_bot.replay.universe import CandleHistory
from crypto_bot.trading.config import PaperSettings
from crypto_bot.trading.domain import D, StrategySignal
from crypto_bot.trading.strategy import ReferenceStrategy

NAME = "institutional_style_v1"
HOUR = 3600000


@dataclass(frozen=True)
class InstitutionalSettings(PaperSettings):
    strategy_name: str = NAME
    require_1h_bullish: bool = True
    require_4h_bullish: bool = True
    require_1d_bullish: bool = True
    ema_fast_period: int = 50
    ema_slow_period: int = 200
    btc_regime_filter: bool = True
    btc_bull_risk_multiplier: object = D("1")
    btc_neutral_risk_multiplier: object = D(".5")
    btc_bear_risk_multiplier: object = D("0")
    correlation_filter_enabled: bool = True
    correlation_lookback_hours: int = 168
    correlation_min_observations: int = 120
    max_position_correlation: object = D(".85")
    correlation_insufficient_policy: str = "INSUFFICIENT_DATA_ALLOW"
    max_spread_pct: object = D(".2")

    def __post_init__(self):
        super().__post_init__()
        if not 2 <= self.ema_fast_period < self.ema_slow_period <= 499:
            raise ValueError(
                "ema_fast_period must be >=2 and below ema_slow_period <=499"
            )
        if (
            not 2
            <= self.correlation_min_observations
            <= self.correlation_lookback_hours
            <= 8760
        ):
            raise ValueError(
                "correlation_min_observations must be >=2 and <= correlation_lookback_hours <=8760"
            )
        if not 0 <= self.max_position_correlation <= 1:
            raise ValueError("max_position_correlation must be between 0 and 1")
        if self.correlation_insufficient_policy != "INSUFFICIENT_DATA_ALLOW":
            raise ValueError(
                "correlation_insufficient_policy must be INSUFFICIENT_DATA_ALLOW"
            )
        if self.max_spread_pct > D(".2"):
            raise ValueError("max_spread_pct must be <=0.2")
        for key in (
            "btc_bull_risk_multiplier",
            "btc_neutral_risk_multiplier",
            "btc_bear_risk_multiplier",
        ):
            if not 0 <= getattr(self, key) <= 1:
                raise ValueError(key + " must be between 0 and 1")


PARAMETERS = (
    "min_score",
    "min_delta",
    "delta_window",
    "require_delta",
    "require_watch",
    "min_rsi",
    "max_rsi",
    "min_relative_volume",
    "stop_atr_multiplier",
    "exit_score",
    "exit_on_bearish_ema",
    "require_1h_bullish",
    "require_4h_bullish",
    "require_1d_bullish",
    "ema_fast_period",
    "ema_slow_period",
    "btc_regime_filter",
    "btc_bull_risk_multiplier",
    "btc_neutral_risk_multiplier",
    "btc_bear_risk_multiplier",
    "correlation_filter_enabled",
    "correlation_lookback_hours",
    "correlation_min_observations",
    "max_position_correlation",
    "correlation_insufficient_policy",
    "max_spread_pct",
)


def resample(rows, hours):
    """UTC epoch-aligned complete buckets; gaps/partial hours invalidate a bucket."""
    output = []
    group = []
    current = None

    def emit():
        if len(group) != hours:
            return
        start = group[0].open_time
        if start % (hours * HOUR) or any(
            c.open_time != start + i * HOUR or c.close_time != c.open_time + HOUR - 1
            for i, c in enumerate(group)
        ):
            return
        output.append(
            (
                Candle(
                    start,
                    start + hours * HOUR - 1,
                    group[0].open,
                    max(c.high for c in group),
                    min(c.low for c in group),
                    group[-1].close,
                    sum(c.volume for c in group),
                ),
                0,
            )
        )

    for candle, _ in rows:
        key = candle.open_time // (hours * HOUR)
        if current is not None and key != current:
            emit()
            group = []
        current = key
        group.append(candle)
    emit()
    return CandleHistory(output)


class InstitutionalContext:
    def __init__(self, histories):
        self.histories = histories

    @lru_cache(maxsize=512)
    def series(self, symbol, hours):
        rows = self.histories.get(symbol, CandleHistory([]))
        return rows if hours == 1 else resample(rows, hours)

    @lru_cache(maxsize=4096)
    def trend(self, symbol, hours, now, fast, slow):
        rows = self.series(symbol, hours)
        end = bisect_left(rows.columns[1], now)
        # Match existing hourly EMA window; use the same finite 499 completed
        # higher-timeframe window (minimum slow-period observations).
        candles = [c for c, _ in rows[max(0, end - 499) : end]]
        fresh = (
            bool(candles)
            and candles[-1].close_time == (now // (hours * HOUR)) * (hours * HOUR) - 1
        )
        ready = len(candles) >= slow and fresh
        return {
            "ready": ready,
            "fresh": fresh,
            "observations": len(candles),
            "source_timestamp": at(candles[-1].close_time).isoformat() + "Z"
            if candles
            else None,
            "ema_fast": ema([c.close for c in candles], fast) if ready else None,
            "ema_slow": ema([c.close for c in candles], slow) if ready else None,
        }

    def btc(self, now, s):
        trends = {
            key: self.trend("BTCUSDT", h, now, s.ema_fast_period, s.ema_slow_period)
            for key, h in [("4h", 4), ("1d", 24)]
        }
        if not all(t["ready"] for t in trends.values()):
            regime = "UNKNOWN"
        elif all(t["ema_fast"] > t["ema_slow"] for t in trends.values()):
            regime = "BULL"
        elif all(t["ema_fast"] < t["ema_slow"] for t in trends.values()):
            regime = "BEAR"
        else:
            regime = "NEUTRAL"
        multiplier = (
            {
                "BULL": s.btc_bull_risk_multiplier,
                "NEUTRAL": s.btc_neutral_risk_multiplier,
                "BEAR": s.btc_bear_risk_multiplier,
            }.get(regime, D(0))
            if s.btc_regime_filter
            else D(1)
        )
        return {"regime": regime, "trends": trends, "risk_multiplier": str(multiplier)}

    @lru_cache(maxsize=4096)
    def returns(self, symbol, now, lookback):
        rows = self.series(symbol, 1)
        end = bisect_left(rows.columns[1], now)
        result = {}
        start = max(1, bisect_left(rows.columns[0], now - lookback * HOUR))
        for i in range(start, end):
            a, b = rows[i - 1][0], rows[i][0]
            if (
                b.open_time - a.open_time == HOUR
                and a.close_time < a.open_time + HOUR
                and b.close_time < b.open_time + HOUR
            ):
                result[b.close_time] = b.close / a.close - 1
        return result

    def correlations(self, symbol, positions, now, s):
        if not s.correlation_filter_enabled:
            return {"enabled": False, "blocked": False, "pairs": []}
        candidate = self.returns(symbol, now, s.correlation_lookback_hours)
        pairs = []
        for other in sorted({p.symbol for p in positions if p.symbol != symbol}):
            data = self.returns(other, now, s.correlation_lookback_hours)
            stamps = sorted(candidate.keys() & data.keys())
            value = None
            if len(stamps) >= s.correlation_min_observations:
                try:
                    value = correlation(
                        [candidate[t] for t in stamps], [data[t] for t in stamps]
                    )
                except (ValueError, ZeroDivisionError):
                    pass
            pairs.append(
                {
                    "symbol": other,
                    "observations": len(stamps),
                    "correlation": value,
                    "reason": "CORRELATION_INSUFFICIENT_DATA"
                    if value is None
                    else "CORRELATION_LIMIT"
                    if value >= float(s.max_position_correlation)
                    else "CORRELATION_OK",
                    "latest_source_timestamp": at(stamps[-1]).isoformat() + "Z"
                    if stamps
                    else None,
                }
            )
        return {
            "enabled": True,
            "blocked": any(p["reason"] == "CORRELATION_LIMIT" for p in pairs),
            "pairs": pairs,
            "insufficient_policy": s.correlation_insufficient_policy,
        }

    def context(self, symbol, now, s, positions):
        return {
            "decision_time": at(now).isoformat() + "Z",
            "coin_trends": {
                key: self.trend(symbol, h, now, s.ema_fast_period, s.ema_slow_period)
                for key, h in [("1h", 1), ("4h", 4), ("1d", 24)]
            },
            "btc": self.btc(now, s),
            "correlation": self.correlations(symbol, positions, now, s),
        }


class InstitutionalStrategy:
    def __init__(self, settings):
        self.settings = settings

    def evaluate(self, c, has_position=False):
        if has_position:
            return ReferenceStrategy(self.settings).evaluate(c, True)
        s = self.settings
        audit = c.features.get("institutional", {})
        failed = []
        if s.require_watch and not c.algorithm_watch:
            failed.append("OUTSIDE_ALGORITHM_WATCH")
        if c.watch_state == "IGNORE":
            failed.append("USER_IGNORE")
        btc = audit.get("btc", {})
        if s.btc_regime_filter:
            if btc.get("regime", "UNKNOWN") == "UNKNOWN":
                failed.append("BTC_REGIME_UNAVAILABLE")
            elif D(btc.get("risk_multiplier", "0")) == 0:
                failed.append(
                    "BTC_BEAR_REGIME"
                    if btc["regime"] == "BEAR"
                    else "BTC_REGIME_RISK_ZERO"
                )
        for key in ("1h", "4h", "1d"):
            if not getattr(s, "require_" + key + "_bullish"):
                continue
            trend = audit.get("coin_trends", {}).get(key, {})
            if not trend.get("ready"):
                failed.append("MULTITF_" + key.upper() + "_UNAVAILABLE")
            elif trend["ema_fast"] <= trend["ema_slow"]:
                failed.append("MULTITF_" + key.upper() + "_NOT_BULLISH")
        if c.score < s.min_score:
            failed.append("SCORE_BELOW_MIN")
        if (s.require_delta and c.delta is None) or (
            c.delta is not None and c.delta <= s.min_delta
        ):
            failed.append("SCORE_DELTA_NOT_POSITIVE")
        if not s.min_rsi <= c.rsi <= s.max_rsi:
            failed.append("RSI_OUTSIDE_RANGE")
        if c.relative_volume < s.min_relative_volume:
            failed.append("RELATIVE_VOLUME_BELOW_MIN")
        spread = c.features.get("spread_pct", c.features.get('spread'))
        if spread is not None and D(spread) > s.max_spread_pct:
            failed.append("SPREAD_LIMIT")
        if c.atr <= 0 or c.price - c.atr * s.stop_atr_multiplier <= 0:
            failed.append("INVALID_INITIAL_STOP")
        if audit.get("correlation", {}).get("blocked"):
            failed.append("CORRELATION_LIMIT")
        reasons = failed or [
            "MULTITF_BULLISH",
            "BTC_" + btc.get("regime", "UNKNOWN"),
            "SCORE_AND_MOMENTUM_APPROVED",
        ]
        if not failed and any(
            p["reason"] == "CORRELATION_INSUFFICIENT_DATA"
            for p in audit.get("correlation", {}).get("pairs", [])
        ):
            reasons.append("CORRELATION_INSUFFICIENT_DATA")
        return StrategySignal(
            c.symbol,
            "HOLD" if failed else "BUY",
            min(D(1), max(D(0), c.score / 100)),
            c.price,
            None if failed else c.price - c.atr * s.stop_atr_multiplier,
            c.timestamp,
            NAME,
            tuple(reasons),
            c.snapshot_id,
        )
