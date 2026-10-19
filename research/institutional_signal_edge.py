"""Offline univariate candidate diagnostic. Never a strategy or replay runner.

Only run-scoped read-only SQLite, exact pinned local ZIPs and pure calculations.
python -m research.institutional_signal_edge --root /home/ndvi/crypto-bot
"""

import argparse
import csv
import hashlib
import io
import json
import math
import sqlite3
import statistics as st
import subprocess
import sys
import zipfile
from array import array
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from crypto_bot.config.settings import Settings
from crypto_bot.indicators.technical import features
from crypto_bot.replay.clock import at, ms
from crypto_bot.replay.config import config_hash
from crypto_bot.replay.institutional import InstitutionalContext, InstitutionalSettings
from crypto_bot.replay.market_context import canonical_scan_histories
from crypto_bot.replay.universe import CandleHistory, HistoricalUniverse
from crypto_bot.scanner.scoring import score

BASE = "226facef1f68f967fc1d7b1bb75e282849185a56"
RUNS = {
    2024: "31423539f64e4650840df5b6b3fe9a26",
    2025: "f9d41057d61e4714b61df69d320620e0",
}
SOURCE_SHA = {
    2024: "75998cae198f09f7be33bf5777f048b9653d7f81",
    2025: "524125eeaf1b2e87d3957b915810b55db7972ff8",
}
HOUR, MINUTE = 3600000, 60000
LEVELS = (1, 2, 5, 10, 20)
HORIZONS = (6, 24, 72, 168)
FEATURES = (
    "market_score",
    "component_trend",
    "component_momentum",
    "component_volume",
    "component_volatility",
    "component_liquidity",
    "delta_1h",
    "delta_4h",
    "delta_24h",
    "relative_volume",
    "rsi",
    "atr",
    "atr_pct",
    "range_position",
    "trend_strength",
    "volatility",
    "price_momentum",
    "spread",
    "quote_volume",
    "ema50_distance_pct",
    "ema200_distance_pct",
    "ema50_ema200_distance_pct",
    "4h_ema50_distance_pct",
    "4h_ema200_distance_pct",
    "4h_ema50_ema200_distance_pct",
    "1d_ema50_distance_pct",
    "1d_ema200_distance_pct",
    "1d_ema50_ema200_distance_pct",
)


def encoded(value):
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def iso(t):
    return at(t).isoformat() + "Z"


def finite(x):
    return isinstance(x, (int, float)) and math.isfinite(x)


@contextmanager
def read_only(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    try:
        yield db
    finally:
        db.close()


def no_network(event, args):
    if event in {
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.gethostbyname",
    }:
        raise RuntimeError("NETWORK_FORBIDDEN_OFFLINE_RESEARCH")


def source_fingerprint(db, run_ids):
    """Logical immutable source rows; exclude unrelated live worker heartbeats."""
    result = {}
    for (table,) in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'replay_%' ORDER BY name"
    ):
        columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
        key = (
            "id"
            if table == "replay_runs"
            else "run_id"
            if "run_id" in columns
            else None
        )
        if key is None:
            continue
        for rid in run_ids:
            h, n = hashlib.sha256(), 0
            for row in db.execute(
                f'SELECT * FROM "{table}" WHERE "{key}"=? ORDER BY rowid', (rid,)
            ):
                h.update(json.dumps(row, separators=(",", ":")).encode() + b"\n")
                n += 1
            result[f"{rid}/{table}"] = {"rows": n, "sha256": h.hexdigest()}
    # Creation guard covers every run, not just the two sources.
    result["all_run_ids"] = digest(
        db.execute("SELECT id FROM replay_runs ORDER BY id").fetchall()
    )
    return result


def load_source(db, year):
    rid = RUNS[year]
    row = db.execute(
        "SELECT status,config,metadata FROM replay_runs WHERE id=?", (rid,)
    ).fetchone()
    if row is None or row[0] != "COMPLETED":
        raise ValueError(f"Source {rid} must be COMPLETED")
    config, meta = json.loads(row[1]), json.loads(row[2])
    if (
        meta["git_commit"] != SOURCE_SHA[year]
        or config_hash(config) != meta["config_hash"]
    ):
        raise ValueError("Source commit/config hash mismatch")
    if (
        config["market_context_version"] != "ISOLATED_TOPN_V1"
        or not meta["strict_no_lookahead"]
    ):
        raise ValueError("Strict isolated source required")
    variant = 1 if year == 2024 else 0
    trades = [
        json.loads(r[0])
        for r in db.execute(
            "SELECT data FROM replay_trades WHERE run_id=? AND variant=? ORDER BY position_id",
            (rid, variant),
        )
    ]
    # Stream once; never retain the 150k large HOLD context objects.
    kinds = Counter()
    events = []
    for (raw,) in db.execute(
        "SELECT data FROM replay_audit_events WHERE run_id=? AND variant=? ORDER BY id",
        (rid, variant),
    ):
        event = json.loads(raw)
        kinds[event["event"]] += 1
        if event["event"] != "INSTITUTIONAL_ENTRY_REJECTED":
            events.append(event)
    return dict(
        year=year,
        run_id=rid,
        config=config,
        metadata=meta,
        variant=variant,
        trades=trades,
        events=events,
        event_counts=dict(kinds),
    )


def baseline_comparability(a, b, repository):
    ca, cb = a["config"], b["config"]
    va, vb = ca["variants"][0], cb["variants"][0]
    keys = (
        "strategy",
        "strategy_version",
        "strategy_parameters",
        "exit_policy",
        "exit_parameters",
        "risk_parameters",
        "paper_settings",
    )
    differences = {k: [va.get(k), vb.get(k)] for k in keys if va.get(k) != vb.get(k)}
    for label, v in [("2024", va), ("2025", vb)]:
        for key in (
            "entry_ranking",
            "entry_admission",
            "setup_rearm",
            "btc_regime_entry_gate",
        ):
            if v.get(key, "OFF") not in ("OFF", False, None):
                differences[f"{label}/{key}"] = v[key]
        if any(x is not None for x in v.get("entry_filter", {}).values()):
            differences[label + "/entry_filter"] = v["entry_filter"]
    for k in (
        "scanner_settings",
        "universe_size",
        "universe_method",
        "market_context_version",
        "warmup_hours",
        "context_warmup_hours",
        "spread_approximation_pct",
        "fallback_5m",
        "conservative",
        "stale_position_policy",
        "score_version",
        "score_weights",
    ):
        if ca.get(k) != cb.get(k):
            differences[k] = [ca.get(k), cb.get(k)]
    # Exact source bytes prove the correlation/strategy/risk/score definitions
    # used by the two source commits match; no semantic guess from names.
    paths = [
        "crypto_bot/replay/institutional.py",
        "crypto_bot/trading/domain.py",
        "crypto_bot/scanner/scoring.py",
        "crypto_bot/indicators/technical.py",
    ]
    code_hashes = {}
    for path in paths:
        hashes = []
        for y in (2024, 2025):
            raw = subprocess.check_output(
                ["git", "show", f"{SOURCE_SHA[y]}:{path}"], cwd=repository
            )
            hashes.append(hashlib.sha256(raw).hexdigest())
        code_hashes[path] = hashes
        if hashes[0] != hashes[1]:
            differences[path] = hashes
    return dict(
        usable=not differences,
        differences=differences,
        code_hashes=code_hashes,
        role="EXPLORATION_CONSISTENCY_ONLY_NOT_VALIDATION",
        allowed_differences=[
            "period",
            "dataset_role",
            "name",
            "companion variants",
            "source/catalog revision",
        ],
    )


def candidate_events(source):
    events = source["events"]
    if source["year"] == 2024:
        selected = [e for e in events if e["event"] == "ENTRY_CANDIDATE_RANKED"]
        assert len(selected) == source["event_counts"]["ENTRY_CANDIDATE_RANKED"]
        assert all(e["original_strategy_decision"]["action"] == "BUY" for e in selected)
    else:
        # OFF ranking source: BUY signals are emitted before risk, except
        # explicit pre-risk rejections / NO_FILL, which are added as well.
        selected = [
            e
            for e in events
            if (e["event"] == "STRATEGY_SIGNAL" and e.get("action") == "BUY")
            or (
                e["event"] in {"RISK_REJECTED", "NO_FILL"}
                and e.get("action", "BUY") == "BUY"
            )
        ]
    selected.sort(key=lambda e: (e["timestamp"], e.get("rank", 0), e["symbol"]))
    keys = [(e["symbol"], e["timestamp"]) for e in selected]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate authoritative candidate")
    return selected


class Archives:
    """Pinned content-addressed bytes only. Never cache/client constructors."""

    def __init__(self, root, pins):
        self.root, self.pins, self.used = Path(root), pins, {}
        self.index = defaultdict(list)
        for key, sha in sorted(pins.items()):
            parts = key.split("/")
            if len(parts) != 7 or parts[:4] != ["data", "spot", "monthly", "klines"]:
                raise ValueError("Unexpected pinned archive key: " + key)
            self.index[(parts[4], parts[5])].append((key, sha))

    def object(self, key, sha):
        if key not in self.pins or self.pins[key] != sha:
            raise ValueError("UNPINNED_ARCHIVE")
        self.used[key] = sha
        if sha is None:
            return None
        path = self.root / "objects" / (sha + ".zip")
        blob = path.read_bytes()
        if hashlib.sha256(blob).hexdigest() != sha:
            raise ValueError("Pinned archive SHA mismatch: " + key)
        return blob

    def history(self, symbol, interval, start, end):
        cols = [array("q"), array("q")] + [array("d") for _ in range(6)]
        first_month, end_month = (
            at(start).strftime("%Y-%m"),
            at(end - 1).strftime("%Y-%m"),
        )
        for key, sha in self.index.get((symbol, interval), []):
            stamp = key.rsplit("-", 2)[-2] + "-" + key.rsplit("-", 1)[-1][:2]
            if not first_month <= stamp <= end_month:
                continue
            blob = self.object(key, sha)
            if blob is None:
                continue
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                with z.open(z.namelist()[0]) as stream:
                    for row in csv.reader(
                        io.TextIOWrapper(stream, encoding="utf-8-sig")
                    ):
                        if not row or not row[0].isdigit():
                            continue
                        t, close = int(row[0]), int(row[6])
                        if t >= 100_000_000_000_000:
                            t //= 1000
                            close //= 1000
                        if not start <= t < end:
                            continue
                        numbers = [float(row[i]) for i in (1, 2, 3, 4, 5, 7)]
                        o, hi, lo, c, v, qv = numbers
                        if (
                            not all(math.isfinite(x) for x in numbers)
                            or min(o, hi, lo, c) <= 0
                            or min(v, qv) < 0
                            or not lo <= min(o, c) <= max(o, c) <= hi
                        ):
                            raise ValueError("Invalid pinned candle")
                        if cols[0] and t <= cols[0][-1]:
                            raise ValueError("Unordered/duplicate pinned candle")
                        for col, val in zip(cols, (t, close, *numbers)):
                            col.append(val)
        history = object.__new__(CandleHistory)
        history.columns = cols
        return history

    def fingerprint(self):
        result = {}
        with read_only(self.root / "candles.sqlite") as db:
            for key, sha in sorted(self.pins.items()):
                row = db.execute(
                    "SELECT status,sha256 FROM objects WHERE key=?", (key,)
                ).fetchone()
                if row != ("COMPLETE" if sha else "MISSING", sha):
                    raise ValueError("Pinned metadata mismatch: " + key)
                result[key] = row
        return digest(result)

    def verify_used(self):
        for key, sha in sorted(self.used.items()):
            self.object(key, sha)
        return digest(self.used)


def institutional_settings(snapshot):
    defaults = InstitutionalSettings()
    return InstitutionalSettings(
        **{
            key: Decimal(str(value))
            if isinstance(getattr(defaults, key), Decimal)
            else value
            for key, value in snapshot.items()
        }
    )


class DecisionFeatures:
    def __init__(self, archives, source, candidates):
        config = source["config"]
        start, end = ms(config["start"]), ms(config["end"])
        self.start = start
        self.settings = Settings(**config["scanner_settings"])
        symbols = source["metadata"]["resolved_candidate_symbols"]
        raw_start = (
            start - max(config["warmup_hours"], config["context_warmup_hours"]) * HOUR
        )
        full = {}
        needed = {e["symbol"] for e in candidates} | {"BTCUSDT"}
        scan = {}
        for i, symbol in enumerate(symbols):
            h = archives.history(
                symbol,
                "1h",
                raw_start
                if symbol in needed
                else start - config["warmup_hours"] * HOUR,
                end,
            )
            if len(h):
                if symbol in needed:
                    full[symbol] = h
                scan[symbol] = h
            if i % 150 == 0:
                print(f"{source['year']} pinned hourly {i}/{len(symbols)}", flush=True)
        scan = canonical_scan_histories(scan, start - config["warmup_hours"] * HOUR)
        self.universe = HistoricalUniverse(
            scan, self.settings, float(config["spread_approximation_pct"])
        )
        self.context = InstitutionalContext(full, start)
        self.institutional_settings = institutional_settings(
            config["variants"][source["variant"]]["paper_settings"]
        )
        stamps = {
            ms(e["timestamp"]) - n * HOUR for e in candidates for n in (0, 1, 4, 24)
        }
        self.membership = {}
        for i, now in enumerate(sorted(stamps)):
            self.membership[now] = frozenset(
                m.symbol for m in self.universe.ranked(now)
            )
            if i % 1500 == 0:
                print(
                    f"{source['year']} Top20 membership {i}/{len(stamps)}", flush=True
                )
        self.cache = {}

    def raw(self, symbol, now):
        key = symbol, now
        if key not in self.cache:
            market = self.universe.market(symbol, now)
            if market is None:
                self.cache[key] = None
            else:
                candles = [c for c, _ in self.universe.as_of(symbol, now)]
                if not all(c.close_time < now for c in candles):
                    raise ValueError("Unclosed/future decision candle")
                f = features(candles, market, self.settings)
                self.cache[key] = (f, score(f, self.settings))
        return self.cache[key]

    def get(self, symbol, now, positions):
        raw = self.raw(symbol, now)
        if raw is None or symbol not in self.membership[now]:
            raise ValueError("Authoritative candidate lacks fresh Top20 data")
        f, s = raw
        result = {
            "market_score": s["total_score"],
            **{f"component_{k}": v for k, v in s["components"].items()},
        }
        result.update(
            {
                k: f[k]
                for k in (
                    "relative_volume",
                    "rsi",
                    "atr",
                    "atr_pct",
                    "range_position",
                    "volatility",
                    "spread",
                    "quote_volume",
                )
            }
        )
        result.update(
            price_momentum=f["momentum"],
            trend_strength=None,
            trend_strength_unavailable_reason="NO_EXISTING_SCANNER_FIELD; not invented",
            decision_source_timestamp=iso(f["candle_time"]),
        )
        for n in (1, 4, 24):
            old = now - n * HOUR
            old_raw = (
                self.raw(symbol, old)
                if symbol in self.membership.get(old, ())
                else None
            )
            result[f"delta_{n}h"] = (
                s["total_score"] - old_raw[1]["total_score"] if old_raw else None
            )
        context = self.context.context(
            symbol, now, self.institutional_settings, positions
        )
        result.update(
            btc_regime=context["btc"]["regime"],
            btc_risk_multiplier=float(context["btc"]["risk_multiplier"]),
            institutional_context_available=all(
                t["ready"] for t in context["coin_trends"].values()
            ),
            institutional_context=context,
        )
        pairs = context["correlation"]["pairs"]
        result["correlation_max"] = max(
            (p["correlation"] for p in pairs if p["correlation"] is not None),
            default=None,
        )
        result["correlation_observation_count"] = min(
            (p["observations"] for p in pairs), default=0
        )
        for tf, trends in [
            ("1h", context["coin_trends"]["1h"]),
            ("4h", context["coin_trends"]["4h"]),
            ("1d", context["coin_trends"]["1d"]),
        ]:
            if trends["source_timestamp"] and ms(trends["source_timestamp"]) >= now:
                raise ValueError("Partial/future HTF candle")
            prefix = "" if tf == "1h" else tf + "_"
            a, b = trends["ema_fast"], trends["ema_slow"]
            result[prefix + "ema50_distance_pct"] = (
                (f["price"] / a - 1) * 100 if a else None
            )
            result[prefix + "ema200_distance_pct"] = (
                (f["price"] / b - 1) * 100 if b else None
            )
            result[prefix + "ema50_ema200_distance_pct"] = (
                (a / b - 1) * 100 if a and b else None
            )
            result[prefix + "trend_source_timestamp"] = trends["source_timestamp"]
        return result, f["price"]


class Extrema:
    """Range min/max and first crossing in O(log n); no future input features."""

    def __init__(self, values, maximum):
        self.maximum = maximum
        self.op = max if maximum else min
        self.neutral = -math.inf if maximum else math.inf
        size = 1
        while size < len(values):
            size *= 2
        self.size = size
        self.tree = array("d", [self.neutral]) * (2 * size)
        self.tree[size : size + len(values)] = array("d", values)
        for i in range(size - 1, 0, -1):
            self.tree[i] = self.op(self.tree[i * 2], self.tree[i * 2 + 1])

    def query(self, lo, hi):
        lo, hi = lo + self.size, hi + self.size
        answer = self.neutral
        while lo < hi:
            if lo & 1:
                answer = self.op(answer, self.tree[lo])
                lo += 1
            if hi & 1:
                hi -= 1
                answer = self.op(answer, self.tree[hi])
            lo //= 2
            hi //= 2
        return answer

    def first(self, lo, hi, threshold):
        def visit(node, start, end):
            if (
                end <= lo
                or start >= hi
                or (
                    self.tree[node] < threshold
                    if self.maximum
                    else self.tree[node] > threshold
                )
            ):
                return None
            if end - start == 1:
                return start
            middle = (start + end) // 2
            found = visit(node * 2, start, middle)
            return found if found is not None else visit(node * 2 + 1, middle, end)

        return visit(1, 0, self.size)


class FutureLabels:
    """Post-hoc labels only, proposed close-reference R, no fees/fills/sizing."""

    def __init__(self, history, end):
        self.h = history
        self.end = end
        self.high = Extrema(history.columns[3], True)
        self.low = Extrema(history.columns[4], False)
        times, closes = history.columns[:2]
        self.until = array("I", [0]) * len(times)
        for i in range(len(times) - 1, -1, -1):
            valid = closes[i] == times[i] + MINUTE - 1
            self.until[i] = (
                (
                    self.until[i + 1]
                    if i + 1 < len(times) and times[i + 1] == times[i] + MINUTE
                    else i + 1
                )
                if valid
                else i
            )

    def get(self, now, entry, stop):
        result = {"label_type": "POST_HOC_FUTURE_LABEL"}
        unit = entry - stop
        if not finite(unit) or unit <= 0:
            return result | {"censor_reason": "INVALID_REFERENCE_STOP_DISTANCE"}
        times, closes, opens = self.h.columns[:3]
        lo = bisect_left(times, now)
        hi = (
            min(self.until[lo], bisect_left(times, self.end))
            if lo < len(times) and times[lo] == now
            else lo
        )
        complete_end = times[hi - 1] + MINUTE if hi > lo else now
        result.update(price_r_unit=unit, observation_end=iso(complete_end))
        for hours in HORIZONS:
            finish = now + hours * HOUR
            available = finish <= self.end and complete_end >= finish
            end_index = lo + hours * 60
            result[f"mfe_{hours}h_r"] = (
                max(0, (self.high.query(lo, end_index) - entry) / unit)
                if available
                else None
            )
            result[f"mae_{hours}h_r"] = (
                min(0, (self.low.query(lo, end_index) - entry) / unit)
                if available
                else None
            )
            result[f"horizon_{hours}h_status"] = (
                "OBSERVED"
                if available
                else "RUN_END_CENSORED"
                if finish > self.end
                else "MISSING_MINUTE_CENSORED"
            )
        stop_index = self.low.first(lo, hi, stop)
        stop_time = (
            None
            if stop_index is None
            else (
                times[stop_index]
                if opens[stop_index] <= stop
                else closes[stop_index] + 1
            )
        )
        for level in LEVELS:
            target = entry + level * unit
            index = self.high.first(lo, hi, target)
            hit = (
                True
                if index is not None
                and (
                    stop_index is None
                    or index < stop_index
                    or (index == stop_index and opens[index] >= target)
                )
                else False
                if stop_index is not None
                else None
            )
            hit_time = (
                (times[index] if opens[index] >= target else closes[index] + 1)
                if hit
                else None
            )
            result[f"hit_{level}r_before_stop"] = hit
            result[f"time_to_{level}r_hours"] = (
                (hit_time - now) / HOUR if hit_time is not None else None
            )
            result[f"hit_{level}r_time"] = (
                iso(hit_time) if hit_time is not None else None
            )
        result.update(
            stop_before_1r=True
            if stop_index is not None and result["hit_1r_before_stop"] is False
            else False
            if result["hit_1r_before_stop"] is True
            else None,
            time_to_stop_hours=(stop_time - now) / HOUR
            if stop_time is not None
            else None,
            stop_time=iso(stop_time) if stop_time is not None else None,
            censor_reason=None
            if stop_index is not None
            else "RUN_END_CENSORED"
            if complete_end >= self.end
            else "MISSING_MINUTE_CENSORED",
        )
        return result


def episodes(rows):
    last, counters = {}, Counter()
    for row in sorted(rows, key=lambda r: (r["timestamp"], r["symbol"])):
        symbol, now = row["symbol"], ms(row["timestamp"])
        if symbol not in last or now - last[symbol][0] != HOUR:
            counters[symbol] += 1
            since, ordinal = now, 1
        else:
            since, ordinal = last[symbol][1], last[symbol][2] + 1
        last[symbol] = now, since, ordinal
        row.update(
            symbol_day_id=f"{row['year']}/{symbol}/{row['timestamp'][:10]}",
            setup_episode_id=f"{row['year']}/{symbol}/{iso(since)}",
            setup_episode_ordinal=ordinal,
        )
    return rows


def portfolio_before(candidates, events, initial):
    """At signal generation, before same-timestamp sells/buys; descriptive only."""
    fills = [e for e in events if e["event"] in {"ENTRY_FILL", "EXIT_FILL"}]
    fills.sort(key=lambda e: e["timestamp"])  # stable original audited ordering
    cursor, cash, positions = 0, float(initial), {}
    result = {}
    for e in candidates:
        while cursor < len(fills) and (
            fills[cursor]["timestamp"] < e["timestamp"]
            or (
                fills[cursor]["timestamp"] == e["timestamp"]
                and fills[cursor]["event"] == "EXIT_FILL"
                and fills[cursor].get("candle_close_time") == e["timestamp"]
            )
        ):
            fill = fills[cursor]
            cursor += 1
            qty, price, fee = map(
                float, (fill["quantity"], fill["fill_price"], fill["fee"])
            )
            if fill["event"] == "ENTRY_FILL":
                cash -= qty * price + fee
                positions[fill["position_id"]] = SimpleNamespace(
                    symbol=fill["symbol"], quantity=qty, entry_price=price
                )
            else:
                cash += qty * price - fee
                positions.pop(fill["position_id"], None)
        result[(e["symbol"], e["timestamp"])] = (cash, list(positions.values()))
    return result


def original_buy_record(event, entry, stop):
    if event.get("original_strategy_decision"):
        return dict(event["original_strategy_decision"])
    return dict(
        action="BUY",
        reasons=event.get("reasons") if event["event"] == "STRATEGY_SIGNAL" else None,
        entry_reference=entry,
        stop_reference=stop,
        source_event=event["event"],
        stop_reference_provenance="RECONSTRUCTED_FROM_CLOSED_CANDLE_ATR",
        pre_execution_rejection_reasons=event.get("reasons")
        if event["event"] == "RISK_REJECTED"
        else None,
    )


def make_dataset(source, archives):
    candidates = candidate_events(source)
    reconstruction = DecisionFeatures(archives, source, candidates)
    before = portfolio_before(
        candidates,
        source["events"],
        source["config"]["variants"][source["variant"]]["risk_parameters"][
            "initial_paper_balance"
        ],
    )
    actual = {(t["symbol"], t["entry_time"]): t for t in source["trades"]}
    by_symbol = defaultdict(list)
    checks = Counter()
    for event in candidates:
        symbol, timestamp = event["symbol"], event["timestamp"]
        now = ms(timestamp)
        cash, positions = before[(symbol, timestamp)]
        f, entry = reconstruction.get(symbol, now, positions)
        original = event.get("original_strategy_decision")
        stop = (
            float(original["stop_reference"])
            if original
            else entry
            - f["atr"]
            * float(
                source["config"]["variants"][0]["strategy_parameters"][
                    "stop_atr_multiplier"
                ]
            )
        )
        for field, value in [
            ("market_score", event.get("market_score")),
            ("delta_4h", event.get("delta_4h")),
            ("relative_volume", event.get("relative_volume")),
        ]:
            if value is not None and not math.isclose(
                f[field], float(value), rel_tol=1e-11, abs_tol=1e-10
            ):
                raise ValueError(
                    f"Audit reconstruction mismatch {symbol}/{timestamp}/{field}: {f[field]} vs {value}"
                )
        if original and not math.isclose(
            entry, float(original["entry_reference"]), rel_tol=1e-12
        ):
            raise ValueError("Original signal reference mismatch")
        checks[
            "ranked_features_verified" if original else "unranked_buy_reconstructed"
        ] += 1
        exposure, fresh_positions = 0.0, 0
        for position in positions:
            raw = reconstruction.raw(position.symbol, now)
            if raw:
                exposure += position.quantity * raw[0]["price"]
                fresh_positions += 1
        # No price substitution for missing marks; partial exposure is not total.
        portfolio_exposure = exposure if fresh_positions == len(positions) else None
        row = dict(
            year=source["year"],
            run_id=source["run_id"],
            variant=source["variant"],
            timestamp=timestamp,
            symbol=symbol,
            candidate_population_source="ENTRY_CANDIDATE_RANKED"
            if original
            else "UNRANKED_PRE_RISK_BUY_AUDIT",
            candidate_pool_size=event.get("candidate_pool_size"),
            entry_rank=event.get("rank"),
            auto_eligible=True,
            proposed_entry_reference=entry,
            original_stop_reference=stop,
            stop_distance_pct=(entry - stop) / entry * 100,
            currently_open_positions=len(positions),
            current_exposure=portfolio_exposure,
            free_cash=cash,
            portfolio_context_timing="SIGNAL_GENERATION: previous-minute exits included; same-timestamp strategy fills excluded; closed 1h marks; descriptive, not risk decision_view",
            original_strategy_decision=original_buy_record(event, entry, stop),
            **f,
        )
        t = actual.get((symbol, timestamp))
        row["ACTUAL_TRADE_LABEL"] = dict(actual_trade=t is not None)
        if t:
            for key, expected in [
                ("market_score", "entry_score"),
                ("delta_1h", "delta_1h"),
                ("delta_4h", "delta_4h"),
                ("delta_24h", "delta_24h"),
                ("rsi", "rsi"),
                ("atr_pct", "atr_pct"),
                ("range_position", "range_position"),
            ]:
                a, b = f[key], t.get(expected)
                if (a is None) != (b is None) or (
                    a is not None
                    and not math.isclose(a, float(b), rel_tol=1e-10, abs_tol=1e-9)
                ):
                    raise ValueError(
                        f"Actual trade feature mismatch: {symbol}/{timestamp}/{key}"
                    )
            row["ACTUAL_TRADE_LABEL"].update(
                status=t["status"],
                realized_net_pnl=t["realized_pnl"] if t["status"] == "CLOSED" else None,
                realized_r=t.get("r_multiple") if t["status"] == "CLOSED" else None,
                exit_timestamp=t.get("exit_time"),
                exit_reason=t.get("exit_reason"),
                hold_hours=t["duration_seconds"] / 3600
                if t.get("duration_seconds") is not None
                else None,
                actual_mfe_r=t.get("mfe_r"),
                actual_mae_r=t.get("mae_r"),
            )
            checks["actual_trade_features_verified"] += 1
        by_symbol[symbol].append(row)
    del reconstruction
    rows = []
    end = ms(source["config"]["end"])
    for i, (symbol, items) in enumerate(sorted(by_symbol.items())):
        print(
            f"{source['year']} pinned minute labels {i + 1}/{len(by_symbol)} {symbol}",
            flush=True,
        )
        history = archives.history(
            symbol, "1m", min(ms(r["timestamp"]) for r in items), end
        )
        labels = FutureLabels(history, end)
        for row in items:
            row["POST_HOC_FUTURE_LABEL"] = labels.get(
                ms(row["timestamp"]),
                row["proposed_entry_reference"],
                row["original_stop_reference"],
            )
            rows.append(row)
        del labels, history
    if checks["actual_trade_features_verified"] != len(source["trades"]):
        raise ValueError("Actual trade missing from candidate population")
    if source["year"] != 2024:
        pools = Counter(r["timestamp"] for r in rows)
        for r in rows:
            r["candidate_pool_size"] = pools[r["timestamp"]]
    return episodes(
        sorted(
            rows, key=lambda r: (r["timestamp"], r.get("entry_rank") or 0, r["symbol"])
        )
    ), dict(checks)


def quantile(values, p):
    s = sorted(values)
    if not s:
        return None
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def ranks(values):
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        rank = (cursor + 1 + end) / 2
        for i in order[cursor:end]:
            result[i] = rank
        cursor = end
    return result


def spearman(x, y):
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    return st.correlation(ranks(x), ranks(y))


def auc(positives, negatives):
    """Mann-Whitney AUC with ties; no fitted weights or cutoffs."""
    if not positives or not negatives:
        return None
    negative = sorted(negatives)
    return sum(
        bisect_left(negative, p)
        + (bisect_right(negative, p) - bisect_left(negative, p)) / 2
        for p in positives
    ) / (len(positives) * len(negative))


def feature_stats(rows, feature):
    values = [r[feature] for r in rows if finite(r.get(feature))]
    positive = [
        r[feature]
        for r in rows
        if finite(r.get(feature))
        and r["POST_HOC_FUTURE_LABEL"].get("hit_5r_before_stop") is True
    ]
    negative = [
        r[feature]
        for r in rows
        if finite(r.get(feature))
        and r["POST_HOC_FUTURE_LABEL"].get("stop_before_1r") is True
    ]
    positive_total = sum(
        r["POST_HOC_FUTURE_LABEL"].get("hit_5r_before_stop") is True for r in rows
    )
    negative_total = sum(
        r["POST_HOC_FUTURE_LABEL"].get("stop_before_1r") is True for r in rows
    )
    numerical_constant = bool(values) and max(values) - min(values) <= 1e-12 * max(
        1, abs(st.median(values))
    )
    a = None if numerical_constant else auc(positive, negative)
    result = dict(
        feature=feature,
        numerical_constant=numerical_constant,
        effect_unavailable_reason="NUMERICALLY_CONSTANT"
        if numerical_constant
        else None,
        count=len(values),
        missing_rate=1 - len(values) / len(rows) if rows else None,
        median=quantile(values, 0.5),
        q1=quantile(values, 0.25),
        q3=quantile(values, 0.75),
        positive_count=len(positive),
        negative_count=len(negative),
        positive_median=quantile(positive, 0.5),
        positive_q1=quantile(positive, 0.25),
        positive_q3=quantile(positive, 0.75),
        positive_missing_rate=1 - len(positive) / positive_total
        if positive_total
        else None,
        negative_q1=quantile(negative, 0.25),
        negative_q3=quantile(negative, 0.75),
        negative_missing_rate=1 - len(negative) / negative_total
        if negative_total
        else None,
        negative_median=quantile(negative, 0.5),
        auc_5r_vs_stop=a,
        cliffs_delta=2 * a - 1 if a is not None else None,
    )
    result["median_difference"] = (
        result["positive_median"] - result["negative_median"]
        if positive and negative
        else None
    )
    p1, p3, n1, n3 = (
        quantile(positive, 0.25),
        quantile(positive, 0.75),
        quantile(negative, 0.25),
        quantile(negative, 0.75),
    )
    result["iqr_overlap_fraction"] = (
        max(0, min(p3, n3) - max(p1, n1)) / (max(p3, n3) - min(p1, n1))
        if positive and negative and max(p3, n3) > min(p1, n1)
        else None
    )
    for hours in (72, 168):
        pairs = [
            (r[feature], r["POST_HOC_FUTURE_LABEL"].get(f"mfe_{hours}h_r"))
            for r in rows
            if finite(r.get(feature))
            and finite(r["POST_HOC_FUTURE_LABEL"].get(f"mfe_{hours}h_r"))
        ]
        result[f"spearman_{hours}h_mfe"] = (
            None
            if numerical_constant
            else spearman([x for x, y in pairs], [y for x, y in pairs])
        )
        result[f"spearman_{hours}h_count"] = len(pairs)
    pairs = [
        (r[feature], r["ACTUAL_TRADE_LABEL"].get("realized_r"))
        for r in rows
        if finite(r.get(feature)) and finite(r["ACTUAL_TRADE_LABEL"].get("realized_r"))
    ]
    result["spearman_actual_realized_r"] = (
        None
        if numerical_constant
        else spearman([x for x, y in pairs], [y for x, y in pairs])
    )
    result["actual_closed_count"] = len(pairs)
    return result


def base_rates(rows):
    result = dict(
        candidate_count=len(rows),
        unique_symbols=len({r["symbol"] for r in rows}),
        unique_symbol_days=len({r["symbol_day_id"] for r in rows}),
        setup_episode_count=len({r["setup_episode_id"] for r in rows}),
    )
    for key in ["stop_before_1r"] + [f"hit_{n}r_before_stop" for n in LEVELS]:
        values = [r["POST_HOC_FUTURE_LABEL"].get(key) for r in rows]
        known = [v for v in values if v is not None]
        result[key] = dict(
            true=sum(v is True for v in known),
            false=sum(v is False for v in known),
            unknown=len(values) - len(known),
            observed_rate=sum(v is True for v in known) / len(known) if known else None,
        )
    for h in HORIZONS:
        for measure in ("mfe", "mae"):
            values = [r["POST_HOC_FUTURE_LABEL"].get(f"{measure}_{h}h_r") for r in rows]
            values = [v for v in values if finite(v)]
            result[f"median_{h}h_{measure}_r"] = quantile(values, 0.5)
    closed = [
        r["ACTUAL_TRADE_LABEL"]["realized_r"]
        for r in rows
        if finite(r["ACTUAL_TRADE_LABEL"].get("realized_r"))
    ]
    result["actual_trade_count"] = sum(
        r["ACTUAL_TRADE_LABEL"]["actual_trade"] for r in rows
    )
    result["actual_closed_count"] = len(closed)
    result["actual_expectancy_r"] = st.mean(closed) if closed else None
    return result


def deciles(rows, feature):
    values = [r[feature] for r in rows if finite(r.get(feature))]
    constant = bool(values) and max(values) - min(values) <= 1e-12 * max(
        1, abs(st.median(values))
    )
    cuts = (
        [st.median(values)] * 9
        if constant
        else [quantile(values, n / 10) for n in range(1, 10)]
        if values
        else []
    )
    groups = defaultdict(list)
    for r in rows:
        value = r.get(feature)
        if finite(value):
            # Equal values stay together; ties are not split by future labels.
            groups[1 if constant else bisect_left(cuts, value) + 1].append(r)
    return [
        dict(
            feature=feature,
            decile=n,
            lower_quantile_edge=cuts[n - 2] if n > 1 and cuts else None,
            upper_quantile_edge=cuts[n - 1] if n < 10 and cuts else None,
            **base_rates(groups[n]),
        )
        for n in range(1, 11)
    ]


def score_monotonicity(decile_rows):
    result = []
    keys = sorted(
        {
            (str(r["year"]), r["scope"])
            for r in decile_rows
            if r["feature"] == "market_score"
        }
    )
    for year, scope in keys:
        rows = sorted(
            [
                r
                for r in decile_rows
                if str(r["year"]) == year
                and r["scope"] == scope
                and r["feature"] == "market_score"
            ],
            key=lambda r: r["decile"],
        )
        for measure in [
            "hit_1r_before_stop",
            "hit_2r_before_stop",
            "hit_5r_before_stop",
            "stop_before_1r",
            "median_72h_mfe_r",
            "median_72h_mae_r",
        ]:
            pairs = [
                (
                    r["decile"],
                    r[measure]["observed_rate"]
                    if isinstance(r[measure], dict)
                    else r[measure],
                )
                for r in rows
            ]
            pairs = [(x, y) for x, y in pairs if finite(y)]
            values = [v for _, v in pairs]
            up = sum(b > a for a, b in zip(values, values[1:]))
            down = sum(b < a for a, b in zip(values, values[1:]))
            result.append(
                dict(
                    year=year,
                    scope=scope,
                    measure=measure,
                    observed_deciles=len(pairs),
                    nondecreasing=down == 0 if len(values) > 1 else None,
                    nonincreasing=up == 0 if len(values) > 1 else None,
                    increasing_steps=up,
                    decreasing_steps=down,
                    spearman_decile_outcome=spearman([x for x, _ in pairs], values),
                )
            )
    return result


def csv_bytes(rows):
    if not rows:
        return b""
    columns = sorted({k for r in rows for k in r})
    f = io.StringIO(newline="")
    writer = csv.DictWriter(f, columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                k: json.dumps(v, sort_keys=True, allow_nan=False)
                if isinstance(v, (dict, list))
                else v
                for k, v in row.items()
            }
        )
    return f.getvalue().encode()


def write_csv(path, rows):
    path.write_bytes(csv_bytes(rows))


def flatten_candidate(row):
    flat = {
        k: v
        for k, v in row.items()
        if k
        not in {"POST_HOC_FUTURE_LABEL", "ACTUAL_TRADE_LABEL", "institutional_context"}
    }
    for prefix in ("POST_HOC_FUTURE_LABEL", "ACTUAL_TRADE_LABEL"):
        flat.update({prefix + "." + k: v for k, v in row[prefix].items()})
    flat["institutional_context"] = row["institutional_context"]
    return flat


def analyze(datasets, out):
    stats, decile_rows, episode_rows, regimes, bases = [], [], [], [], {}
    populations = dict(datasets)
    populations["combined_exploration"] = [
        r for y in sorted(datasets) for r in datasets[y]
    ]
    for year, rows in populations.items():
        first = [r for r in rows if r["setup_episode_ordinal"] == 1]
        bases[str(year)] = dict(
            raw_hourly=base_rates(rows), episode_first_candidate=base_rates(first)
        )
        # Episode sensitivity is first observable candidate only, not an
        # outcome-selected best member. All hourly rows remain exported.
        for scope, subset in [("raw_hourly", rows), ("episode_first", first)]:
            for feature in FEATURES:
                stats.append(
                    dict(year=year, scope=scope, **feature_stats(subset, feature))
                )
                decile_rows.extend(
                    dict(year=year, scope=scope, **r) for r in deciles(subset, feature)
                )
        for ordinal in (1, 2, 3, 4, "5+"):
            group = [
                r
                for r in rows
                if (
                    r["setup_episode_ordinal"] >= 5
                    if ordinal == "5+"
                    else r["setup_episode_ordinal"] == ordinal
                )
            ]
            episode_rows.append(dict(year=year, ordinal=ordinal, **base_rates(group)))
        for regime in ("BULL", "NEUTRAL", "BEAR", "UNKNOWN"):
            regimes.append(
                dict(
                    year=year,
                    regime=regime,
                    **base_rates([r for r in rows if r["btc_regime"] == regime]),
                )
            )
    consistency = []
    for scope in ("raw_hourly", "episode_first"):
        for feature in FEATURES:
            entries = {
                y: next(
                    (
                        r
                        for r in stats
                        if r["year"] == y
                        and r["scope"] == scope
                        and r["feature"] == feature
                    ),
                    {},
                )
                for y in (2024, 2025)
            }
            effects = [entries[y].get("cliffs_delta") for y in (2024, 2025)]
            # Descriptive convention, frozen here: |Cliff|>=.147 (small effect).
            tag = (
                "WEAK/UNAVAILABLE"
                if any(x is None or abs(x) < 0.147 for x in effects)
                else "SAME_DIRECTION"
                if effects[0] * effects[1] > 0
                else "OPPOSITE_DIRECTION"
            )
            consistency.append(
                dict(
                    feature=feature,
                    scope=scope,
                    consistency=tag,
                    **{
                        f"{y}_{k}": entries[y].get(k)
                        for y in (2024, 2025)
                        for k in (
                            "cliffs_delta",
                            "auc_5r_vs_stop",
                            "spearman_72h_mfe",
                            "spearman_168h_mfe",
                        )
                    },
                    **{
                        f"{y}_direction": "POSITIVE"
                        if effects[i] is not None and effects[i] > 0.147
                        else "INVERSE"
                        if effects[i] is not None and effects[i] < -0.147
                        else "WEAK/UNAVAILABLE"
                        for i, y in enumerate((2024, 2025))
                    },
                )
            )
    anatomy = []
    for year, rows in datasets.items():
        episode_first = [r for r in rows if r["setup_episode_ordinal"] == 1]
        for level in (5, 10, 20):
            winners = [
                r
                for r in episode_first
                if r["POST_HOC_FUTURE_LABEL"].get(f"hit_{level}r_before_stop") is True
            ]
            winners.sort(
                key=lambda r: (
                    -(r["POST_HOC_FUTURE_LABEL"].get("mfe_168h_r") or -1),
                    r["timestamp"],
                    r["symbol"],
                )
            )
            for r in winners[:25]:
                anatomy.append(
                    dict(
                        selection=f"TOP25_EPISODE_FIRST_HIT_{level}R",
                        **flatten_candidate(r),
                        **percentiles(r, rows),
                    )
                )
        for symbol in ("FETUSDT", "PEOPLEUSDT", "ETHUSDT"):
            winners = [
                r
                for r in rows
                if r["symbol"] == symbol
                and (r["ACTUAL_TRADE_LABEL"].get("realized_r") or 0) > 0
            ]
            winners.sort(key=lambda r: -r["ACTUAL_TRADE_LABEL"]["realized_r"])
            for r in winners[:5]:
                anatomy.append(
                    dict(
                        selection="KNOWN_ACTUAL_WINNER",
                        **flatten_candidate(r),
                        **percentiles(r, rows),
                    )
                )
    write_csv(
        out / "market-score-deciles.csv",
        [r for r in decile_rows if r["feature"] == "market_score"],
    )
    write_csv(out / "feature-deciles.csv", decile_rows)
    write_csv(
        out / "score-component-analysis.csv",
        [r for r in stats if r["feature"].startswith("component_")],
    )
    write_csv(out / "raw-feature-analysis.csv", stats)
    write_csv(out / "cross-year-consistency.csv", consistency)
    write_csv(out / "setup-episode-analysis.csv", episode_rows)
    write_csv(out / "btc-regime-analysis.csv", regimes)
    write_csv(out / "winner-anatomy.csv", anatomy)
    return dict(
        base_rates=bases,
        score_decile_monotonicity=score_monotonicity(decile_rows),
        feature_statistics=stats,
        cross_year_consistency=consistency,
        episode_analysis=episode_rows,
        btc_regimes=regimes,
    )


def percentiles(row, population):
    result = {}
    for feature in FEATURES:
        value = row.get(feature)
        for name, rows in [
            ("population", population),
            (
                "negative_control",
                [
                    r
                    for r in population
                    if r["POST_HOC_FUTURE_LABEL"].get("stop_before_1r") is True
                ],
            ),
        ]:
            values = sorted(r[feature] for r in rows if finite(r.get(feature)))
            result[feature + "_" + name + "_percentile"] = (
                (bisect_left(values, value) + bisect_right(values, value))
                / 2
                / len(values)
                * 100
                if finite(value) and values
                else None
            )
    return result


def conclusion(analysis, usable_2025):
    # Classification convention is diagnostic, never a feature cutoff or model.
    paired = [
        r
        for r in analysis["cross_year_consistency"]
        if r["scope"] == "episode_first" and r["consistency"] == "SAME_DIRECTION"
    ]
    informative = [
        r
        for r in analysis["feature_statistics"]
        if r["year"] == 2024
        and r["scope"] == "episode_first"
        and r["cliffs_delta"] is not None
        and abs(r["cliffs_delta"]) >= 0.147
    ]
    if usable_2025 and len(paired) >= 2:
        return "CURRENT_FEATURE_SET_SHOWS_STABLE_SIGNAL"
    return (
        "CURRENT_FEATURE_SET_SIGNAL_WEAK_OR_UNSTABLE"
        if informative
        else "CURRENT_FEATURE_SET_HAS_LITTLE_PREDICTIVE_EDGE"
    )


def package_artifacts(out):
    """Deterministic bytes from already validated study files; no source access."""
    files = sorted(
        p
        for p in out.iterdir()
        if p.is_file()
        and p.suffix in {".json", ".csv", ".md"}
        and p.name != "manifest.json"
    )
    manifest = dict(
        source_runs=RUNS,
        research_base=BASE,
        research_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        files={
            p.name: dict(
                bytes=p.stat().st_size,
                sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
            )
            for p in files
        },
    )
    (out / "manifest.json").write_bytes(encoded(manifest))
    bundle = out / "institutional-signal-edge-diagnostic-v1.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(files + [out / "manifest.json"]):
            info = zipfile.ZipInfo(p.name, date_time=(2024, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, p.read_bytes())
    return bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    out = (
        args.output or root / "artifacts/institutional-signal-edge-diagnostic-v1"
    ).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sys.addaudithook(no_network)
    print("Opening read-only source database", flush=True)
    with read_only(root / "data/replay.db") as db:
        before = source_fingerprint(db, list(RUNS.values()))
        (out / "source-fingerprint-before.json").write_bytes(encoded(before))
        sources = {year: load_source(db, year) for year in (2024, 2025)}
    comparison = baseline_comparability(sources[2024], sources[2025], root)
    (out / "2025-source-comparability.json").write_bytes(encoded(comparison))
    print("2025 source usable:", comparison["usable"], flush=True)
    datasets, providers, checks, fingerprints = {}, {}, {}, {}
    for year in (2024, 2025):
        if year == 2025 and not comparison["usable"]:
            continue
        source = sources[year]
        provider = Archives(
            root / "data/replay-cache", source["metadata"]["historical_data_revision"]
        )
        fingerprints[year] = provider.fingerprint()
        providers[year] = provider
        datasets[year], checks[year] = make_dataset(source, provider)
        write_csv(
            out / f"candidate-dataset-{year}.csv",
            [flatten_candidate(r) for r in datasets[year]],
        )
        (out / f"candidate-dataset-{year}.json").write_bytes(encoded(datasets[year]))
    analysis = analyze(datasets, out)
    category = conclusion(analysis, comparison["usable"])
    summary = dict(
        research="Institutional Signal Edge Diagnostic v1",
        category=category,
        source_runs=RUNS,
        source_sha=SOURCE_SHA,
        source_2025_usable=comparison["usable"],
        role="2024 + optional 2025 EXPLORATION ONLY",
        analysis=analysis,
        new_replay_started=False,
        production_behavior_changed=False,
        database_writes=0,
        deployment=False,
    )
    methodology = dict(
        decision_features=list(FEATURES),
        deciles=10,
        quantile_method="linear type7; equal values same bin; empty bins retained; no outcome splitting",
        negative_control="All observed stop_before_1r=True; no sampling",
        positive_group="hit_5r_before_stop=True; disjoint from negative control",
        outcome_type="POST_HOC_FUTURE_LABEL",
        barrier_horizon="until original stop, first missing minute, or source run end; forward windows separately 6/24/72/168h",
        reference="proposed closed-1h strategy entry reference; R=reference-original_stop; no simulated fill/fees/slippage",
        conservative="Open is observed first. Gap up >=target wins before later low; gap down <=stop stops immediately. Otherwise stop wins same-minute high/low ambiguity. Intrabar hit known at minute close.",
        missing="UNKNOWN/CENSORED; never false absent observed stop; no hourly fallback; no crossing data gaps",
        feature_clock="Only candle close_time<decision timestamp. Canonical scanner start/Top20 score membership and institutional closed UTC resampling identical to source code.",
        warmup="Pre-period history is used exclusively for source-defined indicator seeds. No 2023 research cohort, no 2026 archive data loaded.",
        btc_definition="InstitutionalContext.btc 4h+1d EMA regime; NOT legacy reporting btc_regime hourly BULLISH/BEARISH",
        trend_strength="Unavailable: no existing scanner trend_strength scalar; not invented. EMA distances are explicitly derived descriptors.",
        portfolio_context="Descriptive signal-generation state includes preceding minute exits observed at this timestamp, excludes same-timestamp strategy fills; free cash reconstructed from prior audited fills, exposure uses last closed hourly prices, missing mark => null. Never asset-quality inputs.",
        episodes="Contiguous same-symbol hourly candidate rows; absence/unknown breaks episode. First-candidate episode sensitivity, not outcome-selected best candidate.",
        dependence="Raw hourly observations overlap and are NOT independent trades. Episode-first sensitivity and per-year results mandatory; no significance/validation claim.",
        statistics="Numerical-constant tolerance 1e-12 * max(1, abs(median)) suppresses floating-noise pseudo-signals (notably configured constant spread); not a feature cutoff. Tie-aware Spearman, Mann-Whitney single-feature AUC, Cliff=2*AUC-1. No fitted model, threshold search or score weight optimization.",
        descriptive_effect_convention="|Cliff|>=0.147 small nontrivial difference, fixed before run; consistency requires same sign in BOTH years, category A needs >=2 such episode-first features. Correlated features and multiple comparisons remain limitations.",
        winner_selection="Top25 episode-FIRST candidates reaching each fixed target, sorted by 168h MFE then UTC/symbol, plus top5 positive actual FET/PEOPLE/ETH each year; winner anatomy is post-hoc.",
    )
    (out / "methodology.json").write_bytes(encoded(methodology))
    (out / "summary.json").write_bytes(encoded(summary))
    print("Verifying unchanged source fingerprints", flush=True)
    with read_only(root / "data/replay.db") as db:
        after = source_fingerprint(db, list(RUNS.values()))
    if before != after:
        raise ValueError("Source DB changed")
    for year, provider in providers.items():
        if fingerprints[year] != provider.fingerprint():
            raise ValueError("Pinned cache metadata changed")
        provider.verify_used()
    validation = dict(
        before=before,
        after=after,
        source_fingerprint_unchanged=True,
        read_only_uri=True,
        query_only=True,
        no_network=True,
        database_writes=0,
        no_new_replay=True,
        comparability=comparison,
        checks=checks,
        candidate_counts={y: len(rows) for y, rows in datasets.items()},
        cache_metadata_fingerprints=fingerprints,
        pinned_objects_used={y: p.used for y, p in providers.items()},
    )
    (out / "source-validation.json").write_bytes(encoded(validation))
    report(summary, methodology, out)
    bundle = package_artifacts(out)

    print(
        json.dumps(
            dict(
                category=category,
                candidate_counts=validation["candidate_counts"],
                zip=str(bundle),
                zip_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
            ),
            indent=2,
        ),
        flush=True,
    )


def report(summary, methodology, out):
    a = summary["analysis"]

    def number(value):
        return f"{value:.5f}" if value is not None else "UNAVAILABLE"

    def rate(value):
        return f"{value * 100:.2f}%" if value is not None else "UNAVAILABLE"

    text = [
        "# Institutional Signal Edge Diagnostic v1",
        "",
        "READ-ONLY / EXPLORATION. No strategy, filter, ranking, model, optimization or replay.",
        "",
        f"Category: **{summary['category']}**",
        "",
        "2025 is an exploration consistency check, not clean validation.",
        "",
        "## Population and base rates",
        "",
        "Rates use only the observed denominator; UNKNOWN is never FALSE. Episode scope uses the first candidate of every episode.",
        "",
        "| Population | Scope | Candidates | Symbols | Symbol-days | Episodes | +1R | +2R | +5R | +10R | +20R | Stop before +1R |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for year, scopes in a["base_rates"].items():
        for scope, r in scopes.items():
            rates = [rate(r[f"hit_{n}r_before_stop"]["observed_rate"]) for n in LEVELS]
            text.append(
                f"|{year}|{scope}|{r['candidate_count']}|{r['unique_symbols']}|{r['unique_symbol_days']}|{r['setup_episode_count']}|"
                + "|".join(rates)
                + f"|{rate(r['stop_before_1r']['observed_rate'])}|"
            )
    text += [
        "",
        "## Market Score predictive strength",
        "",
        "| Year | Scope | AUC +5R vs stop | Cliff | Spearman 72h MFE | Spearman 168h MFE |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for r in a["feature_statistics"]:
        if r["feature"] == "market_score":
            text.append(
                f"|{r['year']}|{r['scope']}|{number(r['auc_5r_vs_stop'])}|{number(r['cliffs_delta'])}|{number(r['spearman_72h_mfe'])}|{number(r['spearman_168h_mfe'])}|"
            )
    text += [
        "",
        "## Strongest episode-first effects (descriptive)",
        "",
        "Ranks below summarize fixed univariate statistics, not a candidate ranking or feature-selection model. Raw ATR is price-unit/symbol-scale dependent; quote volume is market-size dependent.",
        "",
        "| Year | Direction | Feature | Cliff | AUC |",
        "|---|---|---|---:|---:|",
    ]
    for year in (2024, 2025):
        entries = [
            r
            for r in a["feature_statistics"]
            if r["year"] == year
            and r["scope"] == "episode_first"
            and r["cliffs_delta"] is not None
        ]
        for direction, items in [
            (
                "POSITIVE",
                sorted(
                    [r for r in entries if r["cliffs_delta"] > 0],
                    key=lambda r: -r["cliffs_delta"],
                )[:5],
            ),
            (
                "INVERSE",
                sorted(
                    [r for r in entries if r["cliffs_delta"] < 0],
                    key=lambda r: r["cliffs_delta"],
                )[:5],
            ),
        ]:
            for r in items:
                text.append(
                    f"|{year}|{direction}|{r['feature']}|{number(r['cliffs_delta'])}|{number(r['auc_5r_vs_stop'])}|"
                )
    text += [
        "",
        "## Market Score decile monotonicity",
        "",
        "| Year | Scope | Outcome | Nondecreasing | Nonincreasing | Up/down steps |",
        "|---|---|---|---|---|---|",
    ]
    for r in a.get("score_decile_monotonicity", []):
        text.append(
            f"|{r['year']}|{r['scope']}|{r['measure']}|{r['nondecreasing']}|{r['nonincreasing']}|{r['increasing_steps']}/{r['decreasing_steps']}|"
        )
    stable = [
        r["feature"]
        for r in a["cross_year_consistency"]
        if r["scope"] == "episode_first" and r["consistency"] == "SAME_DIRECTION"
    ]
    text += [
        "",
        "## Cross-year consistency",
        "",
        "Nontrivial same-direction episode-first features: "
        + (", ".join(stable) if stable else "**NONE**"),
        "",
        "| Feature | Scope | 2024 Cliff | 2025 Cliff | Consistency |",
        "|---|---|---:|---:|---|",
    ]
    for r in a["cross_year_consistency"]:
        text.append(
            f"|{r['feature']}|{r['scope']}|{number(r['2024_cliffs_delta'])}|{number(r['2025_cliffs_delta'])}|{r['consistency']}|"
        )
    text += [
        "",
        "## Episode lifecycle",
        "",
        "| Year | Ordinal | Count | +1R | +2R | +5R | Stop before +1R | Median 72h MFE R |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in a["episode_analysis"]:
        text.append(
            f"|{r['year']}|{r['ordinal']}|{r['candidate_count']}|{rate(r['hit_1r_before_stop']['observed_rate'])}|{rate(r['hit_2r_before_stop']['observed_rate'])}|{rate(r['hit_5r_before_stop']['observed_rate'])}|{rate(r['stop_before_1r']['observed_rate'])}|{number(r['median_72h_mfe_r'])}|"
        )
    text += [
        "",
        "The ordinal groups are descriptive, composition-dependent and not a fresh-setup rule. Raw hourly repeats and overlapping future windows are not independent trades.",
        "",
        "## Winner anatomy",
        "",
        "Top25 episode-first opportunities for each fixed target and top positive actual FET/PEOPLE/ETH entries are in winner-anatomy.csv, with decision-time features and both full-population and negative-control percentiles.",
        "",
        "| Year | Symbol | UTC entry | Actual R | Score | Delta4h | RelVol | RSI | ATR% | Range position | Score percentile vs negative control |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    with (out / "winner-anatomy.csv").open() as f:
        for r in csv.DictReader(f):
            if r["selection"] == "KNOWN_ACTUAL_WINNER":
                fields = [
                    r.get(k, "")
                    for k in (
                        "year",
                        "symbol",
                        "timestamp",
                        "ACTUAL_TRADE_LABEL.realized_r",
                        "market_score",
                        "delta_4h",
                        "relative_volume",
                        "rsi",
                        "atr_pct",
                        "range_position",
                        "market_score_negative_control_percentile",
                    )
                ]
                text.append("|" + "|".join(fields) + "|")
    text += [
        "",
        "Individual winner features may be extreme, but examples are post-hoc. The group-level Cliff/IQR overlap tables, not selected anecdotes, determine whether winners are separable from the negative control. No rule is derived from this anatomy.",
        "",
        "## Interpretation limits",
        "",
        "No predictive deployment claim follows from these descriptive associations. Candidate eligibility truncates feature ranges. Numerical constants, including floating noise in the configured spread approximation, cannot demonstrate discriminatory power. Episode-first sensitivity reduces within-episode duplication but does not eliminate shared market/regime dependence. Multiple correlated feature comparisons are not independent confirmation. 2025 is reused exploration. Full per-group IQRs, missing rates, denominators, BTC regimes and all ten quantile bins are exported.",
        "",
        "## Methodology",
        "",
        "```json",
        json.dumps(methodology, indent=2),
        "```",
        "",
        "new replay started: NO; production behavior changed: NO; database writes: 0; deployment: NO",
    ]
    (out / "report.md").write_text("\n".join(text) + "\n")


if __name__ == "__main__":
    main()
