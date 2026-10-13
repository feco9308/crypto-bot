"""Replay-only exogenous snapshots; portfolio state is never an input."""

import json
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from types import SimpleNamespace

from crypto_bot.replay.clock import at, ms
from crypto_bot.replay.universe import CandleHistory
from crypto_bot.scanner.momentum import score_momentum
from crypto_bot.scanner.selection import select_watchlist

LEGACY = "LEGACY_SHARED_HELD_V1"
ISOLATED = "ISOLATED_TOPN_V1"


def version(config):
    value = config.get("market_context_version", LEGACY)
    if value not in (LEGACY, ISOLATED):
        raise ValueError("Unsupported replay market context version")
    return value


def canonical_scan_histories(histories, start):
    """Compact copies with a fixed scanner start, independent of companions.

    Rolling-volume cumulative sums must not inherit a companion's longer EMA
    warmup. Higher-timeframe context keeps the original immutable raw series.
    """
    result = {}
    for symbol, rows in histories.items():
        offset = bisect_left(rows.columns[0], start)
        view = object.__new__(CandleHistory)
        view.columns = [column[offset:] for column in rows.columns]
        result[symbol] = view
    return result


def row_for(universe, scanner, market, now, history, score, features, eligible):
    observed = [c for c, _ in universe.as_of(market.symbol, now)]
    if not all(c.close_time < now for c in observed):
        raise ValueError("Market context contains an unclosed candle")
    f = features(observed, market, scanner)
    computed = score(f, scanner)
    momentum = score_momentum(
        computed["total_score"],
        at(now),
        history.get(market.symbol, ()),
        scanner.momentum_windows,
        scanner.history_tolerance_minutes,
    )
    return dict(
        symbol=market.symbol,
        features=f,
        momentum=momentum,
        auto_eligible=eligible,
        **computed,
    )


@dataclass(frozen=True)
class MarketFrame:
    rows_json: tuple
    watch_items: tuple
    symbols: frozenset
    btc_regime: str
    btc_source: object

    def rows(self):
        # A caller may mutate its copy, never another variant's frame.
        return [json.loads(raw) for raw in self.rows_json]

    def watch(self):
        return dict(self.watch_items)


def frame(universe, scanner, now, history, score, features, markets=None):
    if markets is None:
        markets = universe.ranked(now)
    rows = []
    for market in markets:
        row = row_for(universe, scanner, market, now, history, score, features, True)
        history[market.symbol].append(
            SimpleNamespace(timestamp=at(now), total_score=row["total_score"])
        )
        rows.append(row)
    watch = select_watchlist(rows, scanner)
    btc = next((r for r in rows if r["symbol"] == "BTCUSDT"), None)
    source = (
        at(universe.as_of("BTCUSDT", now)[-1][0].close_time).isoformat() + "Z"
        if btc
        else None
    )
    regime = (
        "BULLISH"
        if btc and btc["features"]["ema50"] > btc["features"]["ema200"]
        else "BEARISH"
        if btc
        else "UNKNOWN"
    )
    for row in rows:
        row.update(btc_regime=regime, btc_regime_source_timestamp=source)
    return MarketFrame(
        tuple(json.dumps(r, allow_nan=False) for r in rows),
        tuple(watch.items()),
        frozenset(r["symbol"] for r in rows),
        regime,
        source,
    )


def exit_rows(frame, universe, scanner, now, history, score, features, positions):
    """Read-only held-symbol context for this variant's exits; no history writes."""
    result = []
    for symbol in sorted({p.symbol for p in positions} - frame.symbols):
        market = universe.market(symbol, now)
        if market is None:
            continue
        row = row_for(universe, scanner, market, now, history, score, features, False)
        row.update(
            btc_regime=frame.btc_regime, btc_regime_source_timestamp=frame.btc_source
        )
        result.append(row)
    return result


def trim(history, now):
    return defaultdict(
        list,
        {
            s: kept
            for s, rs in history.items()
            if (kept := [r for r in rs if ms(r.timestamp) >= now - 26 * 3600000])
        },
    )
