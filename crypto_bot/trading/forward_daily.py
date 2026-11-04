"""Public BTC UTC daily observations and frozen S2 indicators; no order methods."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

import requests

from crypto_bot.trading.domain import D
from crypto_bot.trading.profiles import S2_SYMBOL

DAY_MS = 86400000


def millis(value):
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    delta = value - datetime(1970, 1, 1)
    return delta.days * DAY_MS + delta.seconds * 1000 + delta.microseconds // 1000


def from_millis(value):
    return datetime(1970, 1, 1) + timedelta(milliseconds=value)


class DailyDataUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class DailyCandle:
    open_ms: int
    close_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def completed_window(payload, now):
    if not isinstance(payload, list):
        raise DailyDataUnavailable("Expected public daily kline array")
    bars = []
    for row in payload:
        if not isinstance(row, list) or len(row) != 12:
            raise DailyDataUnavailable("Invalid daily kline schema")
        try:
            start, end = int(row[0]), int(row[6])
            op, hi, lo, cl = (D(row[k]) for k in (1, 2, 3, 4))
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise DailyDataUnavailable("Invalid daily numeric input") from exc
        if (
            start % DAY_MS
            or end != start + DAY_MS - 1
            or min(op, hi, lo, cl) <= 0
            or hi < max(op, cl)
            or lo > min(op, cl)
            or hi < lo
        ):
            raise DailyDataUnavailable("Invalid positive UTC daily OHLC")
        if bars and start != bars[-1].open_ms + DAY_MS:
            raise DailyDataUnavailable(
                "Duplicate, unordered or missing UTC daily candle"
            )
        bars.append(DailyCandle(start, end, op, hi, lo, cl))
    ready = [b for b in bars if b.close_ms < millis(now)]
    if len(ready) < 20:
        raise DailyDataUnavailable("At least 20 completed daily closes required")
    expected = millis(now.replace(hour=0, minute=0, second=0, microsecond=0)) - 1
    if ready[-1].close_ms != expected:
        raise DailyDataUnavailable("Latest completed UTC daily candle is stale")
    return tuple(ready[-20:])


def s2_observation(window, has_position):
    if len(window) != 20:
        raise ValueError("Frozen S2 requires exactly 20 completed closes")
    with localcontext() as ctx:
        ctx.prec = 45
        closes = [D(b.close) for b in window]
        mean = sum(closes, D(0)) / 20
        std = (sum(((x - mean) ** 2 for x in closes), D(0)) / 19).sqrt()
        lower = mean - 2 * std
        action = (
            "SELL"
            if has_position and closes[-1] >= mean
            else "BUY"
            if not has_position and closes[-1] < lower
            else "HOLD"
        )
        return dict(
            source_open_ms=window[-1].open_ms,
            source_close_ms=window[-1].close_ms,
            source_close_UTC=from_millis(window[-1].close_ms).isoformat() + "Z",
            close=str(closes[-1]),
            sma20=str(mean),
            std20=str(std),
            lower_band=str(lower),
            period=20,
            std_multiplier=2,
            ddof=1,
            completed_closes=[str(x) for x in closes],
            action=action,
        )


class BinanceBTCDaily:
    def __init__(self, base_url, timeout=5, session=None):
        self.url = base_url.rstrip("/") + "/api/v3/klines"
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.trust_env = False

    def get_window(self, now):
        try:
            response = self.session.get(
                self.url,
                params=dict(symbol=S2_SYMBOL, interval="1d", limit=22),
                timeout=self.timeout,
                allow_redirects=False,
            )
            response.raise_for_status()
            return completed_window(response.json(), now)
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise DailyDataUnavailable(str(exc)) from exc
