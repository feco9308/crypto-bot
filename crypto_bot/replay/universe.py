"""Archive candidates plus strictly past rolling volume; no current ranking."""

from array import array
from bisect import bisect_left

from crypto_bot.data.base import Candle, Market
from crypto_bot.scanner.selection import eligible


class CandleHistory:
    """Compact hourly archive; materialize only the current 499-candle window.

    Standard-library arrays avoid retaining millions of Python candle/float
    objects during annual runs. Compression never changes the source floats.
    """

    def __init__(self, rows):
        self.columns = [array("q"), array("q")] + [array("d") for _ in range(6)]
        for candle, volume in rows:
            values = (
                candle.open_time,
                candle.close_time,
                candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
                volume,
            )
            for column, value in zip(self.columns, values):
                column.append(value)

    def __len__(self):
        return len(self.columns[0])

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if not -len(self) <= index < len(self):
            raise IndexError(index)
        return Candle(*(column[index] for column in self.columns[:7])), self.columns[7][
            index
        ]


class HistoricalUniverse:
    method = "ROLLING_24H_QUOTE_VOLUME_APPROXIMATION"

    def __init__(self, histories, settings, spread=0.02):
        self.histories = {
            s: rows if isinstance(rows, CandleHistory) else CandleHistory(rows)
            for s, rows in histories.items()
        }
        self.settings = settings
        self.spread = spread
        self.times = {s: rows.columns[0] for s, rows in self.histories.items()}
        self.streaks = {}
        self.volumes = {}
        for symbol, rows in self.histories.items():
            streak = array("I")
            volume = array("d", [0.0])
            for i, (c, v) in enumerate(rows):
                valid = c.open_time <= c.close_time < c.open_time + 3600000
                streak.append(
                    0
                    if not valid
                    else streak[-1] + 1
                    if i and c.open_time - rows[i - 1][0].open_time == 3600000
                    else 1
                )
                volume.append(volume[-1] + v)
            self.streaks[symbol] = streak
            self.volumes[symbol] = volume

    @staticmethod
    def allowed(symbol, settings):
        base = symbol[:-4]
        return (
            base not in settings.stable_assets
            and base not in settings.excluded_assets
            and not (
                base not in settings.leveraged_exceptions
                and any(base.endswith(s) for s in settings.leveraged_suffixes)
            )
        )

    def as_of(self, symbol, timestamp):
        rows = self.histories[symbol]
        index = bisect_left(self.times[symbol], timestamp)
        observed = rows[max(0, index - (self.settings.candle_limit - 1)) : index]
        return [(c, v) for c, v in observed if c.close_time < timestamp]

    def market(self, symbol, timestamp):
        index = bisect_left(self.times[symbol], timestamp)
        if index < 400:
            return None
        rows = self.histories[symbol]
        candle = rows[index - 1][0]
        if (
            candle.open_time != timestamp - 3600000
            or candle.close_time >= timestamp
            or self.streaks[symbol][index - 1]
            < min(index, self.settings.candle_limit - 1)
        ):
            return None
        volume = self.volumes[symbol][index] - self.volumes[symbol][index - 24]
        price = candle.close
        market = Market(
            symbol,
            symbol[:-4],
            "USDT",
            True,
            True,
            volume,
            price * (1 - self.spread / 200),
            price * (1 + self.spread / 200),
            (price / rows[index - 24][0].open - 1) * 100,
        )
        return market

    def ranked(self, timestamp):
        markets = []
        for symbol in self.histories:
            if not self.allowed(symbol, self.settings):
                continue
            m = self.market(symbol, timestamp)
            if m is not None and eligible(m, self.settings):
                markets.append(m)
        return sorted(markets, key=lambda m: (-m.quote_volume, m.symbol))[
            : self.settings.number_of_markets
        ]
