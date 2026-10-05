from datetime import datetime, timezone

import pytest

from crypto_bot.config.settings import Settings
from crypto_bot.data.base import Candle, Market
from crypto_bot.storage.database import Database

NOW = datetime(2026, 10, 5, 12)


def market(
    symbol="BTCUSDT",
    volume=100_000_000,
    base="BTC",
    quote="USDT",
    bid=100,
    ask=100.01,
    active=True,
    spot=True,
):
    return Market(symbol, base, quote, active, spot, volume, bid, ask, 3)


def candles(timestamp=NOW, count=499):
    end = int(timestamp.replace(tzinfo=timezone.utc).timestamp() * 1000)
    result = []
    for i in range(count):
        price = 100 + i * 0.1
        opened = end - (count - i) * 3600000
        result.append(
            Candle(
                opened,
                opened + 3600000 - 1,
                price - 0.05,
                price + 1,
                price - 1,
                price,
                100 if i < count - 1 else 180,
            )
        )
    return result


class FakeProvider:
    def __init__(self, markets=None, timestamp=NOW, fail=None):
        self.items = (
            markets
            if markets is not None
            else [market(), market("ETHUSDT", 80_000_000, "ETH")]
        )
        self.timestamp, self.fail, self.calls = timestamp, fail or set(), []

    def markets(self):
        return self.items

    def candles(self, symbol, timeframe, limit):
        self.calls.append(symbol)
        if symbol in self.fail:
            raise RuntimeError("Mock unavailable")
        return candles(self.timestamp)


@pytest.fixture
def settings():
    return Settings(database_url="sqlite:///:memory:", auto_min_score=0)


@pytest.fixture
def database(settings):
    db = Database(settings.database_url)
    db.initialize()
    yield db
    db.engine.dispose()
