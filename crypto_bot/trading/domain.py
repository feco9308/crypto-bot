from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

ZERO = Decimal("0")
UNIT = Decimal("0.000000000001")


def D(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Nonfinite monetary input")
    return result


def q(value):
    return D(value).quantize(UNIT, rounding=ROUND_DOWN)


@dataclass(frozen=True)
class MarketContext:
    snapshot_id: int
    symbol: str
    timestamp: datetime
    price: Decimal
    score: Decimal
    delta: Decimal | None
    rsi: Decimal
    ema_fast: Decimal
    ema_slow: Decimal
    atr: Decimal
    relative_volume: Decimal
    watch_state: str
    algorithm_watch: bool
    features: dict = field(default_factory=dict)
    manual_trade_enabled: bool = False


@dataclass(frozen=True)
class StrategySignal:
    symbol: str
    action: str
    signal_strength: Decimal
    entry_reference: Decimal
    stop_reference: Decimal | None
    timestamp: datetime
    strategy_name: str
    reasons: tuple[str, ...]
    snapshot_id: int | None = None
    take_profit_price: Decimal | None = None

    def __post_init__(self):
        if self.action not in {"BUY", "SELL", "HOLD"}:
            raise ValueError("Invalid signal action")
        if (
            not D(self.signal_strength).is_finite()
            or not 0 <= self.signal_strength <= 1
        ):
            raise ValueError("Invalid relative signal strength")
        if D(self.entry_reference) <= 0:
            raise ValueError("Invalid entry reference")
        if self.stop_reference is not None and D(self.stop_reference) <= 0:
            raise ValueError("Invalid stop reference")
        if not self.symbol or not self.strategy_name or not self.reasons:
            raise ValueError("Signal identity and explanation required")


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    requested_symbol: str
    risk_amount: Decimal = ZERO
    position_notional: Decimal = ZERO
    quantity: Decimal = ZERO
    stop_distance_pct: Decimal = ZERO
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PositionView:
    id: int
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    current_price: Decimal
    stop_price: Decimal
    entry_fee: Decimal
    strategy_name: str
    take_profit_price: Decimal | None = None

    @property
    def market_value(self):
        return q(self.quantity * self.current_price)

    @property
    def unrealized_pnl(self):
        return q(
            self.market_value - q(self.quantity * self.entry_price) - self.entry_fee
        )


@dataclass(frozen=True)
class PortfolioView:
    equity: Decimal
    cash_balance: Decimal
    reserved_cash: Decimal
    positions: tuple[PositionView, ...]
    realized_pnl: Decimal
    fees_paid: Decimal
    daily_pnl: Decimal
    day_start_equity: Decimal
    peak_equity: Decimal
    max_drawdown: Decimal

    @property
    def available_cash(self):
        return self.cash_balance - self.reserved_cash

    @property
    def exposure(self):
        return sum((p.market_value for p in self.positions), ZERO)

    @property
    def unrealized_pnl(self):
        return sum((p.unrealized_pnl for p in self.positions), ZERO)


class SignalStrategy(Protocol):
    def evaluate(
        self, context: MarketContext, has_position: bool
    ) -> StrategySignal: ...
