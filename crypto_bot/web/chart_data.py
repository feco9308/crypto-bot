"""Visualization-only public GET client; bounded in-memory cache, no DB writes.

Binance market-data docs: https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints
This module is owned by the web UI and is never imported by the trading pipeline.
"""

import math
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

import requests

INTERVALS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


class ChartUnavailable(ValueError):
    pass


class PublicChartData:
    def __init__(self, base_url, session=None, timeout=5):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.retry_at = 0

    def _get(self, path, params, key, ttl):
        # Serialize misses to avoid a browser refresh stampede per worker.
        with self.lock:
            now = time.monotonic()
            cached = self.cache.get(key)
            if cached and cached[0] > now:
                self.cache.move_to_end(key)
                return cached[1]
            if now < self.retry_at:
                raise ChartUnavailable("Public Binance data temporarily rate limited.")
            try:
                response = self.session.get(
                    self.base_url + path,
                    params=params,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                if response.status_code in {418, 429}:
                    try:
                        delay = float(response.headers.get("Retry-After", 60))
                        if not math.isfinite(delay):
                            delay = 60
                    except (ValueError, TypeError):
                        delay = 60
                    self.retry_at = now + max(1, min(delay, 86400))
                if response.status_code != 200:
                    raise ChartUnavailable("Public Binance data unavailable.")
                data = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise ChartUnavailable("Public Binance data unavailable.") from exc
            if path == "/api/v3/ticker/bookTicker" and isinstance(data, dict):
                data["_received_at"] = (
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                )
            self.cache[key] = (time.monotonic() + ttl, data)
            self.cache.move_to_end(key)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
            return data

    def candles(self, symbol, interval, start, end):
        if interval not in INTERVALS:
            raise ValueError("Unsupported visualization timeframe")
        # Align down so the entry-minus-two-hours boundary candle is included.
        step = INTERVALS[interval]
        aligned = start // step * step
        page_end = min(end, aligned + 1000 * step - 1)
        raw = self._get(
            "/api/v3/klines",
            dict(
                symbol=symbol,
                interval=interval,
                startTime=aligned,
                endTime=page_end,
                limit=1000,
            ),
            ("klines", symbol, interval, aligned, page_end // step),
            30,
        )
        if not isinstance(raw, list):
            raise ChartUnavailable("Invalid public kline response.")
        candles = []
        try:
            for row in raw:
                timestamp = int(row[0])
                values = [float(row[i]) for i in range(1, 5)]
                if not all(math.isfinite(v) and v > 0 for v in values):
                    raise ValueError("Invalid candle")
                if not (
                    values[2]
                    <= min(values[0], values[3])
                    <= max(values[0], values[3])
                    <= values[1]
                ):
                    raise ValueError("Invalid OHLC")
                if aligned <= timestamp <= page_end:
                    candles.append(
                        dict(
                            time=timestamp,
                            open=values[0],
                            high=values[1],
                            low=values[2],
                            close=values[3],
                        )
                    )
        except (TypeError, IndexError, ValueError) as exc:
            raise ChartUnavailable("Invalid public kline response.") from exc
        candles = list({r["time"]: r for r in candles}.values())
        candles.sort(key=lambda r: r["time"])
        return dict(
            candles=candles, next_start=page_end + 1 if page_end < end else None
        )

    def quote(self, symbol):
        raw = self._get(
            "/api/v3/ticker/bookTicker", dict(symbol=symbol), ("quote", symbol), 15
        )
        try:
            bid, ask = float(raw["bidPrice"]), float(raw["askPrice"])
            if raw["symbol"] != symbol or not (
                math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask
            ):
                raise ValueError("Invalid bid/ask")
            # Receipt time is preserved with the cached observation.
            received = raw["_received_at"]
            return dict(
                symbol=symbol,
                bid=raw["bidPrice"],
                ask=raw["askPrice"],
                source="BINANCE_BOOK_TICKER",
                received_at=received,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ChartUnavailable(
                "Public bid unavailable; no candle fallback."
            ) from exc
