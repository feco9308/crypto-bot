from dataclasses import replace

import pytest
from conftest import candles, market

from crypto_bot.config.settings import Settings
from crypto_bot.indicators.technical import features
from crypto_bot.scanner.scoring import clamp, score
from crypto_bot.scanner.selection import (
    effective_state,
    eligible,
    select_watchlist,
    universe,
)


def test_score_components_normalization(settings):
    f = features(candles(), market(), settings)
    result = score(f, settings)
    assert 0 <= result["total_score"] <= 100
    assert sum(result["components"].values()) == result["total_score"]
    assert result["components"]["trend"] == 25
    assert result["components"]["volume"] == 18
    assert result["components"]["volatility"] == pytest.approx(
        (f["atr_pct"] / 2.5) * 15
    )
    assert len(result["reasons"]) == 5
    scaled = Settings(weights={k: v * 3 for k, v in settings.weights.items()})
    assert score(f, scaled)["total_score"] == pytest.approx(result["total_score"])


def test_extremes_and_zero_weight(settings):
    f = features(candles(), market(), settings)
    f.update(
        atr_pct=1000,
        relative_volume=1e10,
        momentum=-1e5,
        quote_volume=1e20,
        spread=100,
        range_position=-100,
    )
    result = score(f, settings)
    assert result["components"]["volume"] == 20
    assert result["components"]["volatility"] == 0
    assert 0 <= result["total_score"] <= 100
    f["price"] = 1
    f["ema50"] = 2
    f["ema200"] = 3
    assert (
        score(
            f,
            Settings(
                weights=dict(trend=1, momentum=0, volume=0, volatility=0, liquidity=0)
            ),
        )["total_score"]
        == 0
    )
    with pytest.raises(ValueError):
        clamp(float("nan"))


def test_market_filtering_and_top_n(settings):
    good = market()
    values = [
        good,
        market("ETHUSDT", 90_000_000, "ETH"),
        market("USDCUSDT", 1e10, "USDC"),
        market("BTCUPUSDT", 1e9, "BTCUP"),
        market("LOWUSDT", 1, "LOW"),
        market("WIDEUSDT", 1e9, "WIDE", ask=110),
        market("BADUSDT", 1e9, "BAD", active=False),
        market("NONSPOTUSDT", 1e9, "NONSPOT", spot=False),
        market("BTCUSDC", 1e9, "BTC", "USDC"),
    ]
    assert [m.symbol for m in universe(values, settings)] == ["BTCUSDT", "ETHUSDT"]
    settings.number_of_markets = 1
    assert universe(values, settings) == [good]
    assert eligible(values[-1], settings, manual=True)
    assert not eligible(replace(good, bid=0), settings)
    assert not eligible(replace(good, ask=99), settings)


@pytest.mark.parametrize("algorithm", [True, False])
@pytest.mark.parametrize("override", ["AUTO", "WATCH", "PINNED", "IGNORE"])
def test_override_priority(algorithm, override):
    expected = override if override != "AUTO" else "WATCH" if algorithm else "AUTO"
    assert effective_state(algorithm, override) == expected


def test_selection_score_momentum_and_liquidity(settings):
    settings.auto_watchlist_size = 1

    def row(symbol, score, delta, eligible=True):
        return dict(
            symbol=symbol,
            total_score=score,
            auto_eligible=eligible,
            features=dict(relative_volume=1, atr_pct=2),
            momentum=dict(delta_4h=delta),
        )

    rows = [
        row("SOLUSDT", 85, 3),
        row("ADAUSDT", 80, 24),
        row("BADUSDT", 100, 50, False),
    ]
    assert list(select_watchlist(rows, settings)) == ["ADAUSDT"]
    rows[1]["features"]["relative_volume"] = 0.01
    assert list(select_watchlist(rows, settings)) == ["SOLUSDT"]
    settings.auto_watchlist_size = 0
    assert select_watchlist(rows, settings) == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(
            weights=dict(trend=-1, momentum=20, volume=20, volatility=15, liquidity=20)
        ),
        dict(weights=dict(trend=0, momentum=0, volume=0, volatility=0, liquidity=0)),
        dict(timeframe="3m"),
        dict(candle_limit=200),
        dict(momentum_windows=[1]),
        dict(maximum_spread=0),
        dict(number_of_markets=2.5),
        dict(api_timeout=float("nan")),
        dict(auto_max_atr_pct=-1),
    ],
)
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)
