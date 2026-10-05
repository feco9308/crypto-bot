"""Exchange-independent public market data contract."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Market:
    symbol: str
    base_asset: str
    quote_asset: str
    active: bool
    spot: bool
    quote_volume: float
    bid: float
    ask: float
    price_change_pct: float

    @property
    def spread_pct(self):
        return (
            (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 100
            if self.ask >= self.bid > 0
            else float("inf")
        )


class MarketDataProvider(Protocol):
    def markets(self) -> list[Market]: ...
    def candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]: ...
