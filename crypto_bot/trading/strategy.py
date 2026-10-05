from crypto_bot.trading.domain import ZERO, D, StrategySignal


class ReferenceStrategy:
    def __init__(self, settings):
        self.settings = settings

    def evaluate(self, context, has_position=False):
        c, s = context, self.settings
        if has_position:
            exit_reasons = []
            if c.score < s.exit_score:
                exit_reasons.append(
                    f"score {c.score} below exit threshold {s.exit_score}"
                )
            if s.exit_on_bearish_ema and c.ema_fast < c.ema_slow:
                exit_reasons.append("bearish EMA structure")
            action = "SELL" if exit_reasons else "HOLD"
            reasons = exit_reasons or ["Existing position; no strategy exit"]
        else:
            failures = []
            if c.watch_state == "IGNORE":
                failures.append("user IGNORE override")
            if not (c.algorithm_watch or c.manual_trade_enabled):
                failures.append(
                    "manual trading permission required; watchlist is observation only"
                )
            if s.require_watch and not (c.algorithm_watch or c.manual_trade_enabled):
                failures.append("outside effective watchlist")
            if c.score < s.min_score:
                failures.append("score below entry threshold")
            if s.require_delta and c.delta is None:
                failures.append("score momentum history unavailable")
            if c.delta is not None and c.delta < s.min_delta:
                failures.append("score momentum below threshold")
            if s.require_bullish_ema and c.ema_fast <= c.ema_slow:
                failures.append("EMA structure not bullish")
            if not s.min_rsi <= c.rsi <= s.max_rsi:
                failures.append("RSI outside configured range")
            if c.relative_volume < s.min_relative_volume:
                failures.append("relative volume below threshold")
            if c.atr <= 0 or c.price - c.atr * s.stop_atr_multiplier <= 0:
                failures.append("invalid ATR stop")
            action = "HOLD" if failures else "BUY"
            reasons = failures or [
                f"effective watchlist {c.watch_state}; algorithm watch {c.algorithm_watch}",
                f"score {c.score}; Δ{s.delta_window}h {c.delta}",
                f"bullish EMA; RSI {c.rsi}; relative volume {c.relative_volume}",
            ]
        stop = c.price - c.atr * s.stop_atr_multiplier if action == "BUY" else None
        tp = (
            c.price + (c.price - stop) * s.take_profit_r_multiple
            if stop is not None and s.take_profit_r_multiple > 0
            else None
        )
        return StrategySignal(
            c.symbol,
            action,
            min(D(1), max(ZERO, c.score / D(100))),
            c.price,
            stop,
            c.timestamp,
            s.strategy_name,
            tuple(reasons),
            c.snapshot_id,
            tp,
        )


class StrategyEngine:
    """Exchange/execution-independent entrypoint supporting interchangeable strategies."""

    def __init__(self, strategy):
        self.strategy = strategy

    def evaluate(self, context, has_position=False):
        return self.strategy.evaluate(context, has_position)
