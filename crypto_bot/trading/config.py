import json
import os
from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class PaperSettings:
    initial_paper_balance: Decimal = Decimal("1000")
    quote_asset: str = "USDT"
    risk_per_trade_pct: Decimal = Decimal("0.5")
    max_open_positions: int = 3
    max_total_exposure_pct: Decimal = Decimal("50")
    max_symbol_exposure_pct: Decimal = Decimal("20")
    daily_loss_limit_pct: Decimal = Decimal("2")
    max_drawdown_limit_pct: Decimal = Decimal("5")
    minimum_order_value: Decimal = Decimal("10")
    paper_fee_pct: Decimal = Decimal("0.1")
    paper_slippage_pct: Decimal = Decimal("0.05")
    pyramiding: bool = False
    loop_interval: float = 15
    max_snapshot_age_seconds: float = 900
    strategy_name: str = "watchlist_reference_v1"
    min_score: Decimal = Decimal("70")
    min_delta: Decimal = Decimal("0")
    delta_window: int = 4
    require_delta: bool = True
    require_watch: bool = True
    require_bullish_ema: bool = True
    min_rsi: Decimal = Decimal("40")
    max_rsi: Decimal = Decimal("70")
    min_relative_volume: Decimal = Decimal("1")
    stop_atr_multiplier: Decimal = Decimal("2")
    exit_score: Decimal = Decimal("45")
    exit_on_bearish_ema: bool = True
    take_profit_r_multiple: Decimal = Decimal("0")

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Decimal) and (not value.is_finite() or value < 0):
                raise ValueError(f"Invalid {f.name}")
        for name in [
            "initial_paper_balance",
            "risk_per_trade_pct",
            "max_total_exposure_pct",
            "max_symbol_exposure_pct",
            "daily_loss_limit_pct",
            "max_drawdown_limit_pct",
            "minimum_order_value",
            "stop_atr_multiplier",
        ]:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in [
            "risk_per_trade_pct",
            "max_total_exposure_pct",
            "max_symbol_exposure_pct",
            "daily_loss_limit_pct",
            "max_drawdown_limit_pct",
            "paper_fee_pct",
            "paper_slippage_pct",
            "min_score",
            "exit_score",
            "min_rsi",
            "max_rsi",
        ]:
            if getattr(self, name) > 100:
                raise ValueError(f"{name} must be <=100")
        if (
            self.paper_slippage_pct >= 100
            or self.min_rsi > self.max_rsi
            or self.exit_score > self.min_score
        ):
            raise ValueError("Invalid strategy/slippage thresholds")
        if self.pyramiding:
            raise ValueError("Pyramiding is not supported; must be false")
        import math

        for name in ["loop_interval", "max_snapshot_age_seconds"]:
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"Invalid {name}")
        for name in ["max_open_positions", "delta_window"]:
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"Invalid {name}")
        if (
            not self.quote_asset.isalnum()
            or self.quote_asset != self.quote_asset.upper()
        ):
            raise ValueError("Invalid quote asset")

    def public_dict(self):
        return {
            k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()
        }

    @classmethod
    def load(cls):
        load_dotenv()
        raw = (
            json.loads(Path(os.environ["PAPER_CONFIG"]).read_text())
            if os.getenv("PAPER_CONFIG")
            else {}
        )
        defaults = cls()
        if set(raw) - {f.name for f in fields(cls)}:
            raise ValueError("Unknown paper config field")
        for f in fields(cls):
            env = os.getenv("PAPER_" + f.name.upper())
            value = env if env is not None else raw.get(f.name)
            if value is None:
                continue
            default = getattr(defaults, f.name)
            if isinstance(default, bool):
                if value not in [True, False, "true", "false", "1", "0"]:
                    raise ValueError(f"Invalid boolean {f.name}")
                raw[f.name] = value in [True, "true", "1"]
            else:
                raw[f.name] = type(default)(str(value))
        return cls(**raw)
