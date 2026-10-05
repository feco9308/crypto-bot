"""Validated settings. JSON config handles nested values; env handles deployment."""

import json
import math
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Settings:
    database_url: str = "sqlite:///data/market.db"
    quote_asset: str = "USDT"
    number_of_markets: int = 50
    timeframe: str = "1h"
    scanner_interval: float = 300
    minimum_quote_volume: float = 5_000_000
    maximum_spread: float = 0.2  # percentage points
    auto_watchlist_size: int = 5
    top_score_size: int = 5
    top_risers_size: int = 5
    weights: dict = field(
        default_factory=lambda: dict(
            trend=25, momentum=20, volume=20, volatility=15, liquidity=20
        )
    )
    momentum_windows: list = field(default_factory=lambda: [1, 4, 24])
    history_tolerance_minutes: float = 20
    ema_fast: int = 50
    ema_slow: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    volume_period: int = 20
    range_period: int = 20
    price_momentum_period: int = 4
    candle_limit: int = 500
    rvol_full_score: float = 2
    momentum_full_score_pct: float = 3
    atr_target_pct: float = 2.5
    liquidity_full_score_volume: float = 100_000_000
    auto_min_score: float = 50
    auto_min_relative_volume: float = 0.5
    auto_min_atr_pct: float = 0.1
    auto_max_atr_pct: float = 10
    selection_momentum_weight: float = 0.5
    riser_window: int = 4
    stable_assets: list = field(
        default_factory=lambda: [
            "USDT",
            "USDC",
            "BUSD",
            "DAI",
            "TUSD",
            "FDUSD",
            "USDP",
            "USDD",
            "USD1",
            "USDE",
            "EUR",
            "EURI",
            "AEUR",
            "USTC",
            "PYUSD",
            "RLUSD",
        ]
    )
    excluded_assets: list = field(default_factory=list)
    leveraged_suffixes: list = field(
        default_factory=lambda: ["UP", "DOWN", "BULL", "BEAR"]
    )
    leveraged_exceptions: list = field(default_factory=lambda: ["JUP"])
    api_url: str = "https://api.binance.com"
    api_timeout: float = 10
    api_retries: int = 3
    request_pause: float = 0.25
    request_weight_budget: int = 1000
    secret_key: str = ""

    def __post_init__(self):
        positive = [
            "number_of_markets",
            "scanner_interval",
            "maximum_spread",
            "history_tolerance_minutes",
            "ema_fast",
            "ema_slow",
            "rsi_period",
            "atr_period",
            "volume_period",
            "range_period",
            "price_momentum_period",
            "candle_limit",
            "rvol_full_score",
            "momentum_full_score_pct",
            "atr_target_pct",
            "liquidity_full_score_volume",
            "api_timeout",
            "request_weight_budget",
        ]
        for key in positive:
            if not math.isfinite(getattr(self, key)) or getattr(self, key) <= 0:
                raise ValueError(f"{key} must be finite and positive")
        for key in [
            "minimum_quote_volume",
            "request_pause",
            "selection_momentum_weight",
            "auto_min_relative_volume",
            "auto_min_atr_pct",
        ]:
            if not math.isfinite(getattr(self, key)) or getattr(self, key) < 0:
                raise ValueError(f"{key} must be finite and nonnegative")
        for key in [
            "number_of_markets",
            "ema_fast",
            "ema_slow",
            "rsi_period",
            "atr_period",
            "volume_period",
            "range_period",
            "price_momentum_period",
            "candle_limit",
            "auto_watchlist_size",
            "top_score_size",
            "top_risers_size",
            "api_retries",
            "riser_window",
            "request_weight_budget",
        ]:
            if type(getattr(self, key)) is not int or getattr(self, key) < 0:
                raise ValueError(f"{key} must be a nonnegative integer")
        if self.timeframe not in {"5m", "15m", "1h", "4h", "1d"}:
            raise ValueError("Unsupported timeframe")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be less than ema_slow")
        required = max(
            self.ema_slow * 2,
            self.rsi_period + 1,
            self.atr_period + 1,
            self.volume_period + 1,
            self.range_period,
            self.price_momentum_period + 1,
        )
        if not required < self.candle_limit <= 1000:
            raise ValueError(
                f"candle_limit must be > {required} and <= 1000 (includes open candle)"
            )
        if (
            set(self.weights)
            != {"trend", "momentum", "volume", "volatility", "liquidity"}
            or any(not math.isfinite(v) or v < 0 for v in self.weights.values())
            or sum(self.weights.values()) <= 0
        ):
            raise ValueError("Invalid score weights")
        if not {1, 4, 24, self.riser_window}.issubset(self.momentum_windows) or any(
            type(w) is not int or w <= 0 for w in self.momentum_windows
        ):
            raise ValueError("momentum_windows must include 1, 4, 24 and riser_window")
        if (
            not 0 <= self.auto_min_score <= 100
            or not math.isfinite(self.auto_max_atr_pct)
            or self.auto_max_atr_pct < self.auto_min_atr_pct
        ):
            raise ValueError("Invalid watchlist thresholds")
        if (
            not self.quote_asset.isalnum()
            or self.quote_asset != self.quote_asset.upper()
        ):
            raise ValueError("quote_asset must be uppercase alphanumeric")
        if not self.api_url.startswith("https://"):
            raise ValueError("api_url requires HTTPS")

    def public_dict(self):
        return {
            k: v
            for k, v in asdict(self).items()
            if k not in {"secret_key", "database_url"}
        }

    @classmethod
    def load(cls):
        load_dotenv()
        path = os.getenv("SCANNER_CONFIG")
        data = json.loads(Path(path).read_text()) if path else {}
        defaults = cls()
        for f in fields(cls):
            env = os.getenv(f"SCANNER_{f.name.upper()}")
            if env is not None:
                old = getattr(defaults, f.name)
                data[f.name] = (
                    json.loads(env) if isinstance(old, (dict, list)) else type(old)(env)
                )
        return cls(**data)
