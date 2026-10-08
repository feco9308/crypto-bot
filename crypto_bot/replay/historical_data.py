"""Public GET-only archive client. No exchangeInfo/24hr ticker or order API."""

import csv
import hashlib
import io
import json
import math
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone

import requests

from crypto_bot.data.base import Candle
from crypto_bot.replay.clock import at

ARCHIVE = "https://data.binance.vision/"
CATALOG = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"


class HistoricalData:
    def __init__(self, cache, session=None, check=None, progress=None, pause=0.15):
        self.cache = cache
        self.session = session or requests.Session()
        self.check = check or (lambda: None)
        self.progress = progress or (lambda **_: None)
        self.pause = pause
        self.lock = threading.Lock()
        self.last_request = 0
        self.hits = 0
        self.downloaded = 0
        self.missing = 0
        self.used = set()

    def get(self, url, **kwargs):
        if not (url == CATALOG or url.startswith(ARCHIVE + "data/spot/")):
            raise ValueError("Replay permits public Spot archives only")
        with self.lock:
            self.check()
            time.sleep(max(0, self.pause - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            response = self.session.get(
                url, timeout=(10, 20), allow_redirects=False, **kwargs
            )
        if response.status_code in (429, 418, 503):
            response.close()
            raise RuntimeError(
                "Public historical archive rate limited; completed cache objects are resumable"
            )
        return response

    def catalog(self):
        file = self.cache.root / "metadata" / "catalog.json"
        if file.exists():
            value = json.loads(file.read_text())
            if time.time() - value["downloaded_at"] < 86400:
                self.hits += 1
                return value["symbols"]
        symbols = []
        marker = ""
        for _ in range(50):
            self.check()
            response = self.get(
                CATALOG,
                params={
                    "prefix": "data/spot/monthly/klines/",
                    "delimiter": "/",
                    "max-keys": 1000,
                    "marker": marker,
                },
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
            ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
            symbols.extend(
                p.text.rstrip("/").split("/")[-1]
                for p in root.findall("s:CommonPrefixes/s:Prefix", ns)
                if p.text.endswith("USDT/")
            )
            if root.findtext("s:IsTruncated", namespaces=ns) != "true":
                break
            marker = root.findtext("s:NextMarker", namespaces=ns)
            if not marker:
                raise ValueError("Missing catalog cursor")
        else:
            raise ValueError("Historical symbol catalog bound exceeded")
        symbols = sorted(set(symbols))
        if not symbols:
            raise ValueError("Historical catalog unavailable")
        temp = file.with_suffix(".tmp")
        temp.write_text(json.dumps({"downloaded_at": time.time(), "symbols": symbols}))
        temp.replace(file)
        return symbols

    @staticmethod
    def parse(raw):
        result = []
        for row in csv.reader(io.StringIO(raw)):
            if not row or not row[0].isdigit():
                continue
            t, close = int(row[0]), int(row[6])
            if t >= 100_000_000_000_000:
                t //= 1000
                close //= 1000
            values = [float(row[i]) for i in (1, 2, 3, 4, 5, 7)]
            o, h, lo, c, v, qv = values
            if (
                not all(math.isfinite(x) for x in values)
                or min(o, h, lo, c) <= 0
                or v < 0
                or qv < 0
                or not lo <= min(o, c) <= max(o, c) <= h
                or close < t
            ):
                raise ValueError("Invalid historical OHLCV")
            result.append((t, close, o, h, lo, c, v, qv))
        return result

    def object(self, symbol, interval, kind, stamp):
        key = f"data/spot/{kind}/klines/{symbol}/{interval}/{symbol}-{interval}-{stamp}.zip"
        self.used.add(key)
        old = self.cache.object(key)
        if old and (
            old["status"] == "COMPLETE" or time.time() - old["checked_at"] < 86400
        ):
            self.hits += 1
            if old["status"] == "MISSING":
                self.missing += 1
            return old["status"] == "COMPLETE"
        partial = (
            self.cache.root
            / "objects"
            / (hashlib.sha256(key.encode()).hexdigest() + ".part")
        )
        offset = partial.stat().st_size if partial.exists() else 0
        response = self.get(
            ARCHIVE + key,
            headers={"Range": f"bytes={offset}-"} if offset else {},
            stream=True,
        )
        if response.status_code == 404:
            response.close()
            self.cache.missing(key)
            self.missing += 1
            return False
        if response.status_code not in (200, 206):
            response.close()
            raise ValueError(f"Historical archive HTTP {response.status_code}")
        mode = "ab" if response.status_code == 206 and offset else "wb"
        with partial.open(mode) as target:
            try:
                for chunk in response.iter_content(128 * 1024):
                    self.check()
                    if chunk:
                        target.write(chunk)
                    if target.tell() > 64 * 1024 * 1024:
                        raise ValueError("Archive object exceeds safety bound")
            finally:
                response.close()
        blob = partial.read_bytes()
        sha = hashlib.sha256(blob).hexdigest()
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].file_size > 256 * 1024 * 1024:
                raise ValueError("Invalid historical archive")
            rows = self.parse(archive.read(members[0]).decode("utf-8-sig"))
        self.cache.put(symbol, interval, rows, key, sha)
        partial.replace(self.cache.root / "objects" / (sha + ".zip"))
        self.downloaded += len(rows)
        self.progress(
            cache_state="CACHE DOWNLOAD",
            downloaded_candles=self.downloaded,
            cache_hits=self.hits,
            cache_missing=self.missing,
        )
        return True

    def ensure(self, symbol, interval, start, end):
        if interval not in ("1h", "1m", "5m"):
            raise ValueError("Unsupported replay interval")
        import re

        if not re.fullmatch(r"[A-Z0-9]{2,30}USDT", symbol):
            raise ValueError("Invalid symbol")
        first = at(start)
        last = at(end - 1)
        month = first.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while month <= last:
            self.check()
            nxt = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            stamp = month.strftime("%Y-%m")
            available = self.object(symbol, interval, "monthly", stamp)
            # New/recent months may not yet have a monthly archive. Old missing
            # archives are explicitly missing, not silently replaced by today's universe.
            if not available and month >= datetime.now(timezone.utc).replace(
                tzinfo=None
            ) - timedelta(days=65):
                day = max(
                    first.replace(hour=0, minute=0, second=0, microsecond=0), month
                )
                while day < min(nxt, at(end)):
                    self.object(symbol, interval, "daily", day.strftime("%Y-%m-%d"))
                    day += timedelta(days=1)
            month = nxt
        self.progress(
            cache_state="CACHE HIT",
            downloaded_candles=self.downloaded,
            cache_hits=self.hits,
            cache_missing=self.missing,
        )
        return self.cache.rows(symbol, interval, start, end)

    def hourly(self, symbol, start, end):
        rows = self.ensure(symbol, "1h", start, end)
        return [(Candle(r[0], r[1], *r[2:7]), r[7]) for r in rows]

    def minutes(self, symbol, start, end, interval="1m"):
        rows = self.ensure(symbol, interval, start, end)
        return {
            r[0]: dict(
                time=r[0],
                close_time=r[1],
                open=r[2],
                high=r[3],
                low=r[4],
                close=r[5],
                volume=r[6],
                quote_volume=r[7],
                interval=interval,
            )
            for r in rows
        }
