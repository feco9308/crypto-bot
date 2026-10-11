"""Pinned anchor archive revisions, including immutable negative cache objects."""

from crypto_bot.replay.historical_data import HistoricalData


class ShadowHistoricalData(HistoricalData):
    def __init__(self, cache, revisions, **kwargs):
        super().__init__(cache, **kwargs)
        self.revisions = revisions

    def object(self, symbol, interval, kind, stamp):
        key = f"data/spot/{kind}/klines/{symbol}/{interval}/{symbol}-{interval}-{stamp}.zip"
        if key not in self.revisions:
            return super().object(symbol, interval, kind, stamp)
        self.check()
        self.used.add(key)
        old = self.cache.object(key)
        expected = self.revisions[key]
        if old is None or old["sha256"] != expected:
            raise ValueError(
                "Pinned anchor archive revision unavailable or changed: " + key
            )
        if expected is None and old["status"] != "MISSING":
            raise ValueError(
                "Pinned anchor missing archive became inconsistent: " + key
            )
        if expected is not None and old["status"] != "COMPLETE":
            raise ValueError("Pinned anchor complete archive unavailable: " + key)
        self.hits += 1
        if expected is None:
            self.missing += 1
        # Negative cache is part of this anchor's source revision, not future
        # trading knowledge. Engine still observes missing data only at its clock.
        return expected is not None
