from unittest.mock import Mock

import pytest
import requests

from crypto_bot.config.settings import Settings
from crypto_bot.data.binance import BinanceData
from crypto_bot.strategies.legacy_rsi_ema import LegacyRsiEma, adjusted_ema


def response(status=200, data=None, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = data
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(response=result)
    return result


def adapter(responses):
    session = Mock()
    session.get.side_effect = responses
    sleeps = []
    return (
        BinanceData(Settings(request_pause=0), session, sleep=sleeps.append),
        session,
        sleeps,
    )


def test_public_api_mapping():
    data = [
        response(
            data=dict(
                symbols=[
                    dict(
                        symbol="BTCUSDT",
                        baseAsset="BTC",
                        quoteAsset="USDT",
                        status="TRADING",
                        isSpotTradingAllowed=True,
                    )
                ]
            )
        ),
        response(
            data=[
                dict(symbol="BTCUSDT", quoteVolume="100000000", priceChangePercent="3")
            ]
        ),
        response(data=[dict(symbol="BTCUSDT", bidPrice="100", askPrice="100.01")]),
        response(data=[[0, "100", "101", "99", "100", "123", 3599999]]),
    ]
    api, session, _ = adapter(data)
    markets = api.markets()
    assert markets[0].spot and markets[0].quote_volume == 100_000_000
    assert api.candles("BTCUSDT", "1h", 500)[0].volume == 123
    assert session.get.call_count == 4
    assert all(
        "api_key" not in call.kwargs and "headers" not in call.kwargs
        for call in session.get.call_args_list
    )
    with pytest.raises(ValueError):
        api._get("order")


def test_retries_and_nonretryable_errors():
    api, session, sleeps = adapter(
        [requests.Timeout(), response(503), response(data={"ok": True})]
    )
    assert api._get("exchangeInfo") == {"ok": True}
    assert session.get.call_count == 3 and 1 in sleeps and 2 in sleeps
    api, session, _ = adapter([response(400)])
    with pytest.raises(requests.HTTPError):
        api._get("exchangeInfo")
    assert session.get.call_count == 1


@pytest.mark.parametrize("status", [418, 429])
def test_rate_limit_cooldown(status):
    api, session, _ = adapter([response(status, headers={"Retry-After": "120"})])
    with pytest.raises(RuntimeError, match="cooldown"):
        api._get("exchangeInfo")
    with pytest.raises(RuntimeError, match="cooldown"):
        api._get("exchangeInfo")
    assert session.get.call_count == 1


def test_retry_bound():
    api, session, _ = adapter([requests.Timeout()] * 4)
    with pytest.raises(requests.Timeout):
        api._get("exchangeInfo")
    assert session.get.call_count == 4


def test_request_weight_pacing():
    api, session, sleeps = adapter([response(data={})])
    api.used_weight = 999
    api._get("exchangeInfo")
    assert any(59 <= delay <= 60 for delay in sleeps)


def test_legacy_original_semantics():
    strategy = LegacyRsiEma()
    assert strategy.evaluate(list(range(1, 50)))["signal_rsi"] == "SELL"
    assert strategy.evaluate(list(range(50, 1, -1)))["signal_rsi"] == "BUY"
    assert (
        strategy.evaluate([10] * 50)["rsi"] == 100
    )  # original ta flat-series semantics
    assert strategy.evaluate([10] * 5)["rsi"] is None
    assert adjusted_ema([1, 2, 3], 2) == pytest.approx(
        (1 / 9 + 2 / 3 + 3) / (1 / 9 + 1 / 3 + 1)
    )
