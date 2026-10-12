"""Parameterized replay-only peak lock; defaults delegate to existing policy."""

from crypto_bot.replay.clock import at
from crypto_bot.replay.shadow_adaptive import AdaptivePeakLock, PeakState
from crypto_bot.trading.domain import D

DEFAULTS = {
    "activation_break_even_r": "0.75",
    "activation_trailing_r": "1.0",
    "tighten_trailing_r": "2.0",
    "trail_atr_multiplier": "0.75",
    "tight_trail_atr_multiplier": "0.50",
}


def custom(parameters):
    return any(D(parameters.get(k, v)) != D(v) for k, v in DEFAULTS.items())


class ParameterizedPeakLock(AdaptivePeakLock):
    def __init__(self, entry, settings, parameters):
        super().__init__(entry, settings)
        self.parameters = {k: D(parameters.get(k, v)) for k, v in DEFAULTS.items()}
        self.thresholds = tuple(
            self.entry + self.risk / self.quantity * self.parameters[k]
            for k in (
                "activation_break_even_r",
                "activation_trailing_r",
                "tighten_trailing_r",
            )
        )

    def ratchet(self, state, price, now):
        highest = max(state.highest, price)
        stage = sum(highest >= x for x in self.thresholds)
        times = tuple(
            old or (at(now).isoformat() + "Z" if stage > i else None)
            for i, old in enumerate(state.times)
        )
        stop = state.stop
        if stage:
            stop = max(stop, self.break_even)
        if stage >= 2:
            distance = self.parameters[
                "tight_trail_atr_multiplier" if stage == 3 else "trail_atr_multiplier"
            ]
            stop = max(stop, highest - self.atr * distance)
        return PeakState(stop, highest, stage, times)

    def observe(self, candle, position, now, conservative):
        o, h, lo, c = [D(str(candle[k])) for k in ("open", "high", "low", "close")]
        paths = {(o, h, lo, c), (o, lo, h, c)}
        for threshold in self.thresholds:
            peak = max(o, self.state.highest, threshold)
            if o <= peak <= h:
                paths.add((o, peak, lo, h, c))
        for threshold, distance in (
            (self.thresholds[1], self.parameters["trail_atr_multiplier"]),
            (self.thresholds[2], self.parameters["tight_trail_atr_multiplier"]),
        ):
            peak = max(o, self.state.highest, threshold, lo + self.atr * distance)
            if o <= peak <= h:
                paths.add((o, peak, lo, h, c))
        candidates = []
        survivors = []
        for points in sorted(paths):
            state, hit, visited = self.walk(points, self.state, now)
            (survivors if hit is None else candidates).append(
                (hit, points, state, visited)
            )
        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]))
            hit, _, state, visited = candidates[0] if conservative else candidates[-1]
            ambiguous = len({x[0] for x in candidates}) > 1 or bool(survivors)
            result = (
                hit,
                "ORIGINAL_STOP" if state.stage == 0 else "ADAPTIVE_PEAK_LOCK",
            )
        else:
            _, _, state, visited = survivors[0]
            ambiguous = False
            result = None
        self.state = state
        position.highest = state.highest
        position.stop_price = state.stop
        position.overlay_active = state.stage > 0
        return result, ambiguous, visited
