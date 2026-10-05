import math

import pytest
from conftest import candles, market

from crypto_bot.data.base import Candle
from crypto_bot.indicators.technical import atr, ema, features, rsi, wilder


def test_ema_recurrence():
    assert ema([1, 2, 3], 2) == pytest.approx(23 / 9)
    assert ema([10] * 200, 50) == 10


def test_wilder_seed_and_smoothing():
    assert wilder([2, 4, 8], 2) == 5.5


def test_rsi_reference_and_edges():
    closes = [
        44.34,
        44.09,
        44.15,
        43.61,
        44.33,
        44.83,
        45.10,
        45.42,
        45.84,
        46.08,
        45.89,
        46.03,
        45.61,
        46.28,
        46.28,
    ]
    assert rsi(closes) == pytest.approx(70.464135021, abs=1e-8)
    assert rsi(list(range(30))) == 100
    assert rsi(list(range(30, 0, -1))) == 0
    assert rsi([10] * 30) == 50


def test_atr_true_range_includes_gap():
    cs = [
        Candle(0, 1, 10, 11, 9, 10, 1),
        Candle(2, 3, 15, 16, 14, 15, 1),
        Candle(4, 5, 15, 16, 14, 15, 1),
    ]
    assert atr(cs, 2) == 4


def test_features_no_lookahead_volume(settings):
    f = features(candles(), market(), settings)
    assert f["relative_volume"] == 1.8
    assert f["average_volume"] == 100
    assert f["volume"] == 180
    assert f["atr"] == 2
    assert f["trend"] == "bullish"
    assert 0 <= f["range_position"] <= 1
    assert all(math.isfinite(v) for v in f.values() if isinstance(v, (float, int)))


def test_missing_invalid_and_short_candles(settings):
    with pytest.raises(ValueError):
        features(candles()[:100], market(), settings)
    cs = candles()
    del cs[10]
    with pytest.raises(ValueError, match="Missing"):
        features(cs, market(), settings)
    cs = candles()
    cs[-1] = Candle(cs[-1].open_time, cs[-1].close_time, 100, 101, 99, float("nan"), 1)
    with pytest.raises(ValueError, match="Invalid"):
        features(cs, market(), settings)
