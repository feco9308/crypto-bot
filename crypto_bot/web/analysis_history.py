"""Bounded web-only historical cache and background work; no DB access."""

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from crypto_bot.web.analysis_metrics import empty_metrics, historical_metrics
from crypto_bot.web.chart_data import ChartUnavailable, PublicChartData


class HistoricalData(PublicChartData):
    def _get(self, path, params, key, ttl):
        # Analysis histories expire after an hour, unlike the live visualization.
        return super()._get(path, params, key, 3600)


class AnalysisHistory:
    def __init__(self, provider, executor=None, clock=time.monotonic):
        self.provider = provider
        self.executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="paper-analysis"
        )
        self.clock = clock
        self.lock = threading.RLock()
        self.results = OrderedDict()
        self.pending = set()
        self.revision = 0
        self.closed = False
        # Default workers issue at most one historical page per second.
        # Injected deterministic executors do not wait on wall-clock time.
        self.request_interval = 0 if executor is not None else 1
        self.last_request_at = 0

    def metrics(self, trade, now_ms):
        end = max(trade["exit_ms"] or trade["entry_ms"], trade["entry_ms"] + 3600000)
        end = min(end, now_ms // 60000 * 60000)
        key = (trade["position_id"], trade["entry_ms"], trade["exit_ms"], end)
        with self.lock:
            if self.closed:
                return empty_metrics("UNAVAILABLE")
            cached = self.results.get(key)
            if cached and cached[0] > self.clock():
                self.results.move_to_end(key)
                return cached[1].copy()
            if key not in self.pending and len(self.pending) < 32:
                self.pending.add(key)
                try:
                    self.executor.submit(
                        self._calculate, key, trade.copy(), end, now_ms
                    )
                except RuntimeError:
                    self.pending.discard(key)
            return empty_metrics("PENDING")

    def _candles(self, symbol, interval, start, end):
        step = {"1m": 60000, "5m": 300000}[interval]
        if (end - start) / step > 20000:
            raise ChartUnavailable(
                "Historical range exceeds 20000-candle analysis bound"
            )
        result = []
        cursor = start
        deadline = time.monotonic() + 30
        for _ in range(21):
            if cursor > end:
                break
            if self.closed or time.monotonic() > deadline:
                raise ChartUnavailable("Analysis deadline exceeded")
            delay = self.request_interval - (time.monotonic() - self.last_request_at)
            if delay > 0:
                time.sleep(delay)
            self.last_request_at = time.monotonic()
            page = self.provider.candles(symbol, interval, cursor, end)
            result.extend(page["candles"])
            nxt = page["next_start"]
            if nxt is None:
                return result
            if nxt <= cursor:
                raise ChartUnavailable("Invalid historical cursor")
            cursor = nxt
        raise ChartUnavailable("Historical pagination bound exceeded")

    def path(self, trade, now_ms):
        """Reuse the exact bounded historical job/cache used by excursion metrics."""
        metrics = self.metrics(trade, now_ms)
        end = min(
            max(trade["exit_ms"] or trade["entry_ms"], trade["entry_ms"] + 3600000),
            now_ms // 60000 * 60000,
        )
        key = (trade["position_id"], trade["entry_ms"], trade["exit_ms"], end)
        with self.lock:
            cached = self.results.get(key)
            path = (
                cached[2]
                if cached and cached[0] > self.clock() and len(cached) > 2
                else None
            )
        return metrics, path

    def _calculate(self, key, trade, end, now_ms):
        result = empty_metrics("UNAVAILABLE")
        path = None
        interval = "1m" if end - trade["entry_ms"] <= 60000 * 10000 else "5m"
        try:
            try:
                rows = self._candles(trade["symbol"], interval, trade["entry_ms"], end)
                if not rows:
                    raise ChartUnavailable("No historical candles")
            except ChartUnavailable:
                if interval == "5m":
                    raise
                interval = "5m"
                rows = self._candles(trade["symbol"], interval, trade["entry_ms"], end)
                if not rows:
                    raise ChartUnavailable("No historical candles")
            result = historical_metrics(trade, rows, interval, now_ms)
            path = (interval, tuple(rows))
        except Exception:
            # Public data failures never propagate to the web or affect trading.
            pass
        with self.lock:
            self.pending.discard(key)
            ttl = (
                3600
                if result["analysis_data_status"] == "READY" and trade["exit_ms"]
                else 60
            )
            if path:
                path = (*path, self.revision + 1)
            self.results[key] = (self.clock() + ttl, result, path)
            self.results.move_to_end(key)
            # Candle paths are shared by all scenarios, but never grow without bound.
            while (
                len(self.results) > 512
                or sum(
                    len(v[2][1]) for v in self.results.values() if len(v) > 2 and v[2]
                )
                > 100000
            ):
                self.results.popitem(last=False)
            self.revision += 1

    def close(self):
        self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
