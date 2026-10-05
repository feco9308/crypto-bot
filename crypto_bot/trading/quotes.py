"""Read-only public quotes, independent of closed-candle strategy inputs."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

import requests

from crypto_bot.storage.database import iso_utc, utcnow
from crypto_bot.trading.domain import D

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    received_at: datetime
    source: str = "BINANCE_BOOK_TICKER"

    def __post_init__(self):
        object.__setattr__(self, "bid", D(self.bid))
        object.__setattr__(self, "ask", D(self.ask))
        if not self.symbol or not self.source or not 0 < self.bid <= self.ask:
            raise ValueError("Invalid public book quote")
        if self.received_at.tzinfo is not None:
            object.__setattr__(
                self,
                "received_at",
                self.received_at.astimezone(timezone.utc).replace(tzinfo=None),
            )

    def fresh(self, now, max_age):
        return 0 <= (now - self.received_at).total_seconds() <= max_age

    def price(self, side):
        return self.ask if side == "BUY" else self.bid

    def audit(self):
        return dict(
            symbol=self.symbol,
            bid=str(self.bid),
            ask=str(self.ask),
            received_at=iso_utc(self.received_at),
            source=self.source,
        )


class QuoteProvider(Protocol):
    def get_quotes(self, symbols: set[str]) -> dict[str, MarketQuote]: ...


class BinancePublicQuotes:
    """Only GET bookTicker; no signed request, key, order or candle method.

    REST bookTicker does not supply exchange timestamps. received_at is the local
    receipt time, not a claimed exchange update time. No cache or candle fallback.
    """

    def __init__(self, base_url, timeout=5, session=None, clock=utcnow):
        self.url = base_url.rstrip("/") + "/api/v3/ticker/bookTicker"
        self.timeout = timeout
        self.session = session or requests.Session()
        self.clock = clock
        self.retry_after = None

    def get_quotes(self, symbols):
        symbols = set(symbols)
        now = self.clock()
        if not symbols or (self.retry_after and now < self.retry_after):
            return {}
        try:
            response = self.session.get(
                self.url,
                params={"symbols": json.dumps(sorted(symbols), separators=(",", ":"))},
                timeout=self.timeout,
            )
            if response.status_code in {418, 429}:
                try:
                    delay = max(1, float(response.headers.get("Retry-After", 60)))
                    if not delay < float("inf"):
                        delay = 60
                except (TypeError, ValueError):
                    delay = 60
                self.retry_after = self.clock() + timedelta(seconds=min(delay, 86400))
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("Expected bookTicker array")
            received = self.clock()
            quotes, duplicates = {}, set()
            for row in payload:
                try:
                    symbol = row["symbol"]
                    if symbol not in symbols:
                        continue
                    if symbol in quotes:
                        duplicates.add(symbol)
                    quote = MarketQuote(
                        symbol, row["bidPrice"], row["askPrice"], received
                    )
                    quotes[symbol] = quote
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    continue
            for symbol in duplicates:
                quotes.pop(symbol, None)
            return quotes
        except (requests.RequestException, ValueError, TypeError):
            log.warning("PAPER_QUOTE_UNAVAILABLE")
            return {}
