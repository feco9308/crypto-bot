"""GET-only, unauthenticated Spot adapter with bounded retries and pacing."""
import logging
import time
import requests
from crypto_bot.data.base import Candle, Market

log = logging.getLogger(__name__)


class BinanceData:
    ENDPOINTS = {"exchangeInfo", "ticker/24hr", "ticker/bookTicker", "klines"}

    def __init__(self, settings, session=None, sleep=time.sleep, clock=time.monotonic):
        self.settings = settings
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock
        self.last_request = -float("inf")
        self.used_weight = 0
        self.window_start = clock()
        self.blocked_until = 0

    def _get(self, endpoint, **params):
        if endpoint not in self.ENDPOINTS:
            raise ValueError("Only public market-data endpoints are permitted")
        cost = {"exchangeInfo": 20, "ticker/24hr": 80, "ticker/bookTicker": 4, "klines": 2}[endpoint]
        for attempt in range(self.settings.api_retries + 1):
            now = self.clock()
            if now < self.blocked_until:
                raise RuntimeError("Binance rate-limit cooldown is active")
            if now - self.window_start >= 60:
                self.used_weight, self.window_start = 0, now
            if self.used_weight + cost > self.settings.request_weight_budget:
                self.sleep(max(0, 60 - (now - self.window_start)))
                self.used_weight, self.window_start = 0, self.clock()
            self.sleep(max(0, self.settings.request_pause - (self.clock() - self.last_request)))
            self.last_request = self.clock()
            self.used_weight += cost
            try:
                response = self.session.get(f"{self.settings.api_url.rstrip('/')}/api/v3/{endpoint}", params=params, timeout=self.settings.api_timeout)
                self.used_weight = max(self.used_weight, int(response.headers.get("X-MBX-USED-WEIGHT-1M", 0)))
                if response.status_code in {418, 429}:
                    delay = max(1, float(response.headers.get("Retry-After", 60)))
                    self.blocked_until = self.clock() + delay
                    log.warning("API_RATE_LIMIT", extra={"event_data": {"status": response.status_code, "retry_after": delay}})
                    # Stop this scan instead of hammering every remaining instrument.
                    raise RuntimeError(f"Binance cooldown for {delay}s")
                if response.status_code >= 500:
                    raise requests.HTTPError("Binance server error", response=response)
                response.raise_for_status()
                return response.json()
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                retryable = not isinstance(exc, requests.HTTPError) or (exc.response is not None and exc.response.status_code >= 500)
                if not retryable or attempt == self.settings.api_retries:
                    raise
                self.sleep(min(30, 2 ** attempt))
        raise RuntimeError("Retry exhausted")

    def markets(self):
        metadata = self._get("exchangeInfo")["symbols"]
        tickers = {x["symbol"]: x for x in self._get("ticker/24hr")}
        books = {x["symbol"]: x for x in self._get("ticker/bookTicker")}
        result = []
        for item in metadata:
            symbol = item["symbol"]
            ticker, book = tickers.get(symbol), books.get(symbol)
            if ticker is None or book is None:
                continue
            try:
                result.append(Market(symbol, item["baseAsset"], item["quoteAsset"], item["status"] == "TRADING", item.get("isSpotTradingAllowed", False), float(ticker["quoteVolume"]), float(book["bidPrice"]), float(book["askPrice"]), float(ticker["priceChangePercent"])))
            except (ValueError, KeyError, TypeError):
                log.warning("MARKET_METADATA_INVALID", extra={"event_data": {"symbol": symbol}})
        return result

    def candles(self, symbol, timeframe, limit):
        rows = self._get("klines", symbol=symbol, interval=timeframe, limit=limit)
        return [Candle(int(r[0]), int(r[6]), *[float(r[i]) for i in range(1, 6)]) for r in rows]
