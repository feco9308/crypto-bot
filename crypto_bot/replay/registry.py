from dataclasses import dataclass

from crypto_bot.scanner.scoring import score
from crypto_bot.trading.strategy import ReferenceStrategy


@dataclass(frozen=True)
class StrategyDefinition:
    name: str
    version: str
    factory: object
    parameters: tuple


class Registry:
    def __init__(self):
        self.items = {}

    def register(self, name, value):
        if name in self.items:
            raise ValueError("Duplicate registry entry")
        self.items[name] = value
        return value

    def get(self, name):
        if name not in self.items:
            raise ValueError(f"Unknown definition: {name}")
        return self.items[name]


STRATEGIES = Registry()
STRATEGIES.register(
    "watchlist_reference_v1",
    StrategyDefinition(
        "watchlist_reference_v1",
        "1.0.0",
        ReferenceStrategy,
        (
            "min_score",
            "min_delta",
            "delta_window",
            "require_bullish_ema",
            "min_rsi",
            "max_rsi",
            "min_relative_volume",
            "require_delta",
            "require_watch",
            "stop_atr_multiplier",
            "exit_score",
            "exit_on_bearish_ema",
        ),
    ),
)
SCORES = Registry()
SCORES.register("heuristic-v1", score)
EXITS = Registry()
for name, params in {
    "baseline_v1": {},
    "fixed_tp": {"tp_pct": "1.5"},
    "r_tp": {"tp_r": "0.5"},
    "break_even": {"activation_r": "1", "buffer_pct": "0"},
    "trailing_pct": {"activation_pct": "1.5", "distance_pct": "0.5"},
    "trailing_r": {"activation_r": "1", "distance_r": "0.5"},
    "profit_lock": {"activation_pct": "2", "lock_pct": "1"},
}.items():
    EXITS.register(name, params)
