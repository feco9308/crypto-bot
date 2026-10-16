"""Read-only diagnostic of one completed run; no execution or portfolio simulation.

python -m research.portfolio_slot_opportunity --root /home/ndvi/crypto-bot
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
import zipfile
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from crypto_bot.config.settings import Settings
from crypto_bot.data.base import Candle
from crypto_bot.indicators.technical import features
from crypto_bot.replay.clock import at, ms
from crypto_bot.replay.config import config_hash
from crypto_bot.replay.historical_data import HistoricalData
from crypto_bot.replay.metrics import outcome
from crypto_bot.replay.universe import CandleHistory, HistoricalUniverse
from crypto_bot.scanner.scoring import score
from crypto_bot.trading.domain import q

RUN = "31423539f64e4650840df5b6b3fe9a26"
BASE = "75998cae198f09f7be33bf5777f048b9653d7f81"
HOUR, MINUTE = 3600000, 60000
LEVELS = (1, 2, 5, 10, 20)
D = Decimal


def encoded(value):
    return (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()


@contextmanager
def read_only(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    try:
        yield db
    finally:
        db.close()


def fingerprint(db):
    result = {}
    for table in (
        "replay_runs",
        "replay_variants",
        "replay_trades",
        "replay_orders",
        "replay_fills",
        "replay_equity_curve",
        "replay_audit_events",
    ):
        key = "id" if table == "replay_runs" else "run_id"
        h, count = hashlib.sha256(), 0
        for row in db.execute(
            f'SELECT * FROM "{table}" WHERE {key}=? ORDER BY rowid', (RUN,)
        ):
            h.update(json.dumps(row, separators=(",", ":")).encode() + b"\n")
            count += 1
        result[table] = dict(rows=count, sha256=h.hexdigest())
    return result


class Archives:
    """Only exact run-pinned existing objects; no client/cache constructors."""

    def __init__(self, cache, pins, start, end):
        self.root, self.pins, self.start, self.end = cache, pins, start, end
        with read_only(cache / "candles.sqlite") as db:
            self.objects = {
                k: (s, h)
                for k, s, h in db.execute("SELECT key,status,sha256 FROM objects")
            }
        self.used = {}

    def history(self, symbol, interval):
        marker = f"/klines/{symbol}/{interval}/{symbol}-{interval}-"
        rows = []
        for key, sha in sorted(self.pins.items()):
            if marker not in key:
                continue
            # Hourly scanner seed is canonical, not institutional context seed.
            stamp = key.split(marker)[1].removesuffix(".zip")
            if stamp[:7] < at(self.start).strftime("%Y-%m") or stamp[:7] >= at(
                self.end
            ).strftime("%Y-%m"):
                continue
            if self.objects.get(key) != ("COMPLETE" if sha else "MISSING", sha):
                raise ValueError("Pinned object metadata changed: " + key)
            self.used[key] = sha
            if sha is None:
                continue
            blob = (self.root / "objects" / (sha + ".zip")).read_bytes()
            if hashlib.sha256(blob).hexdigest() != sha:
                raise ValueError("Archive SHA mismatch: " + key)
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                parsed = HistoricalData.parse(
                    z.read(z.namelist()[0]).decode("utf-8-sig")
                )
            rows.extend(
                (Candle(r[0], r[1], *r[2:7]), r[7])
                for r in parsed
                if self.start <= r[0] < self.end
            )
        rows.sort(key=lambda r: r[0].open_time)
        if any(a[0].open_time == b[0].open_time for a, b in zip(rows, rows[1:])):
            raise ValueError("Duplicate pinned candle")
        return CandleHistory(rows)


class DecisionFeatures:
    def __init__(self, histories, config):
        self.settings = Settings(**config["scanner_settings"])
        self.universe = HistoricalUniverse(
            histories, self.settings, float(config["spread_approximation_pct"])
        )
        self.membership, self.computed = {}, {}
        for now in range(ms(config["start"]) - 24 * HOUR, ms(config["end"]), HOUR):
            self.membership[now] = frozenset(
                m.symbol for m in self.universe.ranked(now)
            )

    def raw(self, symbol, now):
        key = symbol, now
        if key not in self.computed:
            market = self.universe.market(symbol, now)
            if market is None:
                self.computed[key] = None
            else:
                candles = [c for c, _ in self.universe.as_of(symbol, now)]
                assert all(c.close_time < now for c in candles)
                f = features(candles, market, self.settings)
                self.computed[key] = dict(
                    market_score=score(f, self.settings)["total_score"],
                    relative_volume=f["relative_volume"],
                    rsi=f["rsi"],
                    atr_pct=f["atr_pct"],
                    range_position=f["range_position"],
                    source_timestamp=iso(candles[-1].close_time),
                    row_scope="TOP20_SHARED"
                    if symbol in self.membership.get(now, ())
                    else "VARIANT_LOCAL_HELD_EXIT",
                )
        return self.computed[key]

    def get(self, symbol, now):
        raw = self.raw(symbol, now)
        if raw is None:
            return dict(available=False, reason="NO_FRESH_CONTIGUOUS_CLOSED_1H_HISTORY")
        result = dict(raw, available=True)
        for hours in (1, 4, 24):
            t = now - hours * HOUR
            old = self.raw(symbol, t) if symbol in self.membership.get(t, ()) else None
            result[f"delta_{hours}h"] = (
                raw["market_score"] - old["market_score"] if old else None
            )
        volume = raw["relative_volume"]
        result["acceleration_efficiency"] = (
            result["delta_4h"] / volume
            if result["delta_4h"] is not None and volume > 0
            else None
        )
        return result


def iso(time):
    return at(time).isoformat() + "Z"


def span(history, start, end):
    """Half-open bars; include only wholly closed minute candles."""
    lo = bisect_left(history.columns[0], start)
    hi = bisect_left(history.columns[0], end)
    times = history.columns[0][lo:hi]
    valid = len(times) == (end - start) // MINUTE
    valid &= all(
        t == start + n * MINUTE and history.columns[1][lo + n] < end
        for n, t in enumerate(times)
    )
    return lo, hi, bool(valid)


def candidate_future(history, now, entry, stop, end):
    """Price labels only, no quantity/order/fill. Low-before-high if ambiguous."""
    entry, stop = float(entry), float(stop)
    unit = entry - stop
    if not math.isfinite(unit) or unit <= 0:
        return dict(
            label_type="POST_HOC_FUTURE_LABEL",
            unavailable="INVALID_REFERENCE_STOP_DISTANCE",
        )
    result = dict(label_type="POST_HOC_FUTURE_LABEL", price_r_unit=unit, horizons={})
    times, closes, opens, highs, lows = history.columns[:5]
    for hours in (6, 24, 72, 168):
        finish = now + hours * HOUR
        lo, hi, complete = span(history, now, min(finish, end))
        complete &= finish <= end
        result["horizons"][str(hours)] = dict(
            available=complete,
            mfe_r=max(0, (max(highs[lo:hi]) - entry) / unit) if complete else None,
            mae_r=min(0, (min(lows[lo:hi]) - entry) / unit) if complete else None,
            unavailable_reason=None
            if complete
            else "RUN_END_CENSORED"
            if finish > end
            else "MISSING_PINNED_1M",
        )
    hits = {str(level): None for level in LEVELS}
    hit_times, observed_stop, stop_time = {}, False, None
    cursor, index = now, bisect_left(times, now)
    while cursor < end:
        if (
            index >= len(times)
            or times[index] != cursor
            or closes[index] != cursor + MINUTE - 1
        ):
            break
        # A gap above a target is known at open, before any later low.
        for level in LEVELS:
            if hits[str(level)] is None and opens[index] >= entry + level * unit:
                hits[str(level)], hit_times[str(level)] = True, iso(cursor)
        if opens[index] <= stop or lows[index] <= stop:
            observed_stop = True
            stop_time = iso(cursor if opens[index] <= stop else cursor + MINUTE)
            for level in LEVELS:
                if hits[str(level)] is None:
                    hits[str(level)] = False
            break
        for level in LEVELS:
            if hits[str(level)] is None and highs[index] >= entry + level * unit:
                hits[str(level)], hit_times[str(level)] = True, iso(cursor + MINUTE)
        cursor += MINUTE
        index += 1
    result.update(
        reached_before_stop=hits,
        target_observed_times=hit_times,
        original_stop_observed=observed_stop,
        stop_observed_time=stop_time,
        stop_before_1r=True
        if observed_stop and hits["1"] is False
        else False
        if hits["1"] is True
        else None,
        observation_end=iso(cursor),
        censor_reason=None
        if observed_stop
        else "RUN_END"
        if cursor >= end
        else "MISSING_PINNED_1M",
    )
    return result


def peak_state(history, opened, now, entry, mark, quantity, risk):
    lo, hi, complete = span(history, opened, now)
    peak = (
        max(
            float(entry),
            float(mark),
            max(history.columns[3][lo:hi], default=float(entry)),
        )
        if complete
        else None
    )
    return dict(
        peak_data_available=complete,
        highest_observed_price=peak,
        current_mfe_r=max(0, (peak - float(entry)) * float(quantity) / float(risk))
        if peak is not None
        else None,
        current_drawdown_from_peak_r=max(
            0, (peak - float(mark)) * float(quantity) / float(risk)
        )
        if peak is not None
        else None,
    )


def remaining_label(history, trade, now, mark, current_pnl, peak):
    result = dict(
        label_type="POST_HOC_FUTURE_LABEL", actual_exit_reason=trade["exit_reason"]
    )
    if trade["status"] != "CLOSED":
        return result | dict(
            available=False, reason="ACTUAL_POSITION_STILL_OPEN_AT_RUN_END"
        )
    risk, qty = float(trade["risk_amount"]), float(trade["quantity"])
    finish = ms(trade["exit_time"])
    lo, hi, complete = span(history, now, finish)
    highs = history.columns[3][lo:hi]
    # The stop candle's high may occur after the conservative stop exit.
    if trade["exit_reason"] in ("STOP", "ORIGINAL_STOP") and hi > lo:
        highs = list(highs[:-1]) + [history.columns[2][hi - 1]]
    future_peak = max([float(mark), *highs]) if complete else None
    result.update(
        available=True,
        actual_remaining_realized_r=(float(trade["realized_pnl"]) - current_pnl) / risk,
        actual_eventual_trade_r=trade["r_multiple"],
        remaining_holding_hours=(finish - now) / HOUR,
        future_additional_mfe_r=max(0, (future_peak - float(mark)) * qty / risk)
        if future_peak is not None
        else None,
        future_record_peak_increment_r=max(0, (future_peak - peak) * qty / risk)
        if future_peak is not None and peak is not None
        else None,
        future_peak_data_available=complete,
    )
    return result


def collect(events):
    ranked = {
        e["entry_candidate_id"]: e
        for e in events
        if e["event"] == "ENTRY_CANDIDATE_RANKED"
    }
    blocked = []
    for e in events:
        if (
            e["event"] == "RISK_DECISION"
            and e.get("action") == "BUY"
            and not e["decision"]["approved"]
            and "max open positions reached" in e["decision"]["reasons"]
        ):
            blocked.append((ranked[e["entry_candidate_id"]], e))
    return ranked, blocked


def candidate_record(rank, decision, variant):
    signal = rank["original_strategy_decision"]
    return dict(
        candidate_id=rank["entry_candidate_id"],
        variant=variant,
        timestamp=rank["timestamp"],
        symbol=rank["symbol"],
        entry_rank=rank["rank"],
        candidate_pool_size=rank["candidate_pool_size"],
        market_score=float(rank["market_score"]),
        delta_4h=float(rank["delta_4h"]) if rank["delta_4h"] is not None else None,
        relative_volume=float(rank["relative_volume"])
        if rank["relative_volume"] is not None
        else None,
        strategy_reasons=signal["reasons"],
        proposed_entry_reference=signal["entry_reference"],
        proposed_stop_reference=signal["stop_reference"],
        risk_amount=decision["decision"]["risk_amount"],
        risk_amount_note="Recorded rejected decision; zero is not an approved risk budget",
        portfolio_at_decision=decision["portfolio"],
        original_strategy_decision=signal,
        incumbents=[],
        block_reasons=decision["decision"]["reasons"],
    )


def stats(values):
    values = sorted(
        float(v) for v in values if v is not None and math.isfinite(float(v))
    )
    if not values:
        return dict(count=0, median=None, q1=None, q3=None, iqr=None)

    def percentile(p):
        index = (len(values) - 1) * p
        lo = int(index)
        return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (
            index - lo
        )

    q1, q3 = percentile(0.25), percentile(0.75)
    return dict(count=len(values), median=st.median(values), q1=q1, q3=q3, iqr=q3 - q1)


def effect(positive, negative):
    x = sorted(v for v in positive if v is not None)
    y = sorted(v for v in negative if v is not None)
    a, b = stats(x), stats(y)
    if not x or not y:
        return dict(positive=a, negative=b, cliffs_delta=None, iqr_overlap_ratio=None)
    delta = sum(bisect_left(y, v) - (len(y) - bisect_right(y, v)) for v in x) / (
        len(x) * len(y)
    )
    union = max(a["q3"], b["q3"]) - min(a["q1"], b["q1"])
    overlap = max(0, min(a["q3"], b["q3"]) - max(a["q1"], b["q1"]))
    return dict(
        positive=a,
        negative=b,
        cliffs_delta=delta,
        iqr_overlap_ratio=overlap / union
        if union
        else float(a["median"] == b["median"]),
        positive_fraction_inside_negative_iqr=sum(b["q1"] <= v <= b["q3"] for v in x)
        / len(x),
    )


def feature_distributions(big, negative):
    result = {}
    for key in (
        "market_score",
        "delta_4h",
        "relative_volume",
        "acceleration_efficiency",
        "atr_pct",
        "rsi",
        "range_position",
    ):
        result[key] = effect([r.get(key) for r in big], [r.get(key) for r in negative])
    for key in (
        "age_hours",
        "current_unrealized_r",
        "current_mfe_r",
        "current_drawdown_from_peak_r",
    ):
        # One median per candidate decision, avoiding treating its 3 slots as independent.
        def values(records):
            return [
                st.median(v)
                if (v := [p[key] for p in r["incumbents"] if p.get(key) is not None])
                else None
                for r in records
            ]

        result["incumbent_median_" + key] = effect(values(big), values(negative))
    return result


def csv_file(path, records):
    columns = list(dict.fromkeys(k for r in records for k in r)) or ["candidate_id"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    k: json.dumps(v, sort_keys=True, allow_nan=False)
                    if isinstance(v, (dict, list))
                    else v
                    for k, v in record.items()
                }
            )


def perf(trades):
    result = outcome(trades)
    return {
        key: result[key] for key in ("trade_count", "net_pnl", "profit_factor", "avg_r")
    }


def tail_analysis(trades):
    a = {(t["symbol"], t["entry_time"]): t for t in trades[0]}
    r = {(t["symbol"], t["entry_time"]): t for t in trades[2]}
    only_a = [a[k] for k in sorted(a.keys() - r.keys())]
    only_r = [r[k] for k in sorted(r.keys() - a.keys())]
    without_fet = [
        t
        for t in only_a
        if not (t["symbol"] == "FETUSDT" and t["entry_time"] == "2024-02-24T12:00:00Z")
    ]
    without_both = [
        t
        for t in without_fet
        if not (
            t["symbol"] == "PEOPLEUSDT" and t["entry_time"] == "2024-05-31T01:00:00Z"
        )
    ]
    return dict(
        A_only=perf(only_a),
        R_only=perf(only_r),
        A_only_without_FET=perf(without_fet),
        A_only_without_FET_and_PEOPLE=perf(without_both),
    )


def augment_incumbents(records, trades, archives, reconstructed, end):
    by_symbol = defaultdict(list)
    trade_indexes = {v: {t["position_id"]: t for t in ts} for v, ts in trades.items()}
    for r in records:
        for p in r["portfolio_at_decision"]["positions"]:
            trade = trade_indexes[r["variant"]][p["id"]]
            assert ms(trade["entry_time"]) <= ms(r["timestamp"])
            assert trade["exit_time"] is None or ms(trade["exit_time"]) > ms(
                r["timestamp"]
            )
            by_symbol[p["symbol"]].append((r, p, trade))
    for symbol, items in sorted(by_symbol.items()):
        history = archives.history(symbol, "1m")
        for r, p, t in items:
            now, opened = ms(r["timestamp"]), ms(t["entry_time"])
            mark, entry, qty, risk = map(
                D,
                (p["current_price"], p["entry_price"], p["quantity"], t["risk_amount"]),
            )
            unrealized = q(q(qty * mark) - q(qty * entry) - D(p["entry_fee"]))
            peak = peak_state(history, opened, now, entry, mark, qty, risk)
            index = bisect_left(history.columns[0], now)
            fresh = (
                index < len(history)
                and history.columns[0][index] == now
                and history.columns[2][index] == float(mark)
            )
            state = dict(
                symbol=symbol,
                position_id=p["id"],
                entry_time=t["entry_time"],
                age_hours=(now - opened) / HOUR,
                entry_score=t["entry_score"],
                entry_delta4h=t["delta_4h"],
                entry_relative_volume=t["relative_volume"],
                original_risk_amount=t["risk_amount"],
                original_stop=t["original_stop_price"],
                current_stop=p["stop_price"],
                current_mark=p["current_price"],
                mark_state="FRESH_EXECUTION_OPEN" if fresh else "LAST_KNOWN_AUDIT_MARK",
                mark_source_timestamp=iso(now) if fresh else None,
                current_unrealized_pnl=str(unrealized),
                current_unrealized_r=float(unrealized / risk),
                exposure=str(q(qty * mark)),
                exposure_pct=float(
                    q(qty * mark) / D(r["portfolio_at_decision"]["equity"]) * 100
                ),
                **peak,
                current_features=reconstructed.get(symbol, now),
            )
            state["maximum_reached_r"] = state["current_mfe_r"]
            state["POST_HOC_FUTURE_LABEL"] = remaining_label(
                history, t, now, mark, float(unrealized), peak["highest_observed_price"]
            )
            # Institutional BTC state comes from the original entry or current audit,
            # never from a later trade result. Current state is injected below.
            state["btc_regime"] = r.get("btc_regime", "UNKNOWN")
            r["incumbents"].append(state)
        print("Incumbent marks/labels:", symbol, len(items), flush=True)


def build(root, out):
    out.mkdir(parents=True, exist_ok=True)
    with read_only(root / "data/replay.db") as db:
        db.execute("BEGIN")
        before = fingerprint(db)
        status, raw_config, raw_meta = db.execute(
            "SELECT status,config,metadata FROM replay_runs WHERE id=?", (RUN,)
        ).fetchone()
        config, meta = json.loads(raw_config), json.loads(raw_meta)
        assert status == "COMPLETED" and meta["git_commit"] == BASE
        assert (
            meta["market_context_version"] == "ISOLATED_TOPN_V1"
            and config["dataset_role"] == "EXPLORATION"
        )
        assert (
            config_hash(config)
            == meta["config_hash"]
            == "36cee6a1fb1e5354ac67c0756f3eae9745ea91e9db03f17542d3eab97c2828a4"
        )
        events, btc_audits = {}, {}
        for variant in (1, 2):
            selected, btc = [], {}
            for (raw,) in db.execute(
                "SELECT data FROM replay_audit_events WHERE run_id=? AND variant=? ORDER BY id",
                (RUN, variant),
            ):
                event = json.loads(raw)
                if event.get("context", {}).get("btc"):
                    btc[event["timestamp"]] = event["context"]["btc"]
                if event["event"] == "ENTRY_CANDIDATE_RANKED" or (
                    event["event"] == "RISK_DECISION" and event.get("action") == "BUY"
                ):
                    selected.append(event)
            events[variant], btc_audits[variant] = selected, btc
        trades = {
            v: [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT data FROM replay_trades WHERE run_id=? AND variant=? ORDER BY position_id",
                    (RUN, v),
                )
            ]
            for v in (0, 1, 2)
        }
        for table, order in (
            ("replay_trades", "position_id"),
            ("replay_orders", "id"),
            ("replay_fills", "id"),
            ("replay_equity_curve", "time"),
        ):
            values = [
                [
                    r[0]
                    for r in db.execute(
                        f"SELECT data FROM {table} WHERE run_id=? AND variant=? ORDER BY {order}",
                        (RUN, v),
                    )
                ]
                for v in (0, 1)
            ]
            assert values[0] == values[1], "A/S financial parity: " + table
        summaries = [
            json.loads(
                db.execute(
                    "SELECT summary FROM replay_variants WHERE run_id=? AND number=?",
                    (RUN, v),
                ).fetchone()[0]
            )
            for v in (0, 1)
        ]
        assert summaries[0] == summaries[1], "A/S summary parity"
        assert db.total_changes == 0
    ranks_s, blocked_s = collect(events[1])
    ranks_r, blocked_r = collect(events[2])
    assert len(blocked_s) == 2699 and len(blocked_r) == 2677
    records = [candidate_record(a, b, 1) for a, b in blocked_s]
    target = "2024-02-24T12:00:00Z/FETUSDT"
    r_case = next(
        candidate_record(a, b, 2)
        for a, b in blocked_r
        if a["entry_candidate_id"] == target
    )
    s_event = ranks_s[target]
    assert s_event["candidate_pool_size"] == ranks_r[target]["candidate_pool_size"] == 1
    s_decision = next(
        e
        for e in events[1]
        if e["event"] == "RISK_DECISION" and e.get("entry_candidate_id") == target
    )
    s_case = candidate_record(s_event, s_decision, 1)
    assert s_decision["decision"]["approved"] and "position_id" in s_event
    all_records = records + [r_case, s_case]
    # Current institutional BTC regime for BUYs comes from same-time execution
    # context in the latest preceding institutional audit; never a future entry.
    for v in (1, 2):
        btc = btc_audits[v]
        for record in all_records:
            if record["variant"] == v:
                current = btc.get(record["timestamp"], {})
                record["btc_regime"] = current.get("regime", "UNKNOWN")
                record["btc_regime_sources"] = current.get("trends", {})
    del events
    end, start = ms(config["end"]), ms(config["start"]) - config["warmup_hours"] * HOUR
    archives = Archives(
        root / "data/replay-cache", meta["historical_data_revision"], start, end
    )
    histories = {}
    for n, symbol in enumerate(meta["resolved_candidate_symbols"]):
        if HistoricalUniverse.allowed(symbol, Settings(**config["scanner_settings"])):
            h = archives.history(symbol, "1h")
            if len(h):
                histories[symbol] = h
        if n % 100 == 0:
            print("Pinned scanner histories:", n, flush=True)
    reconstructed = DecisionFeatures(histories, config)
    parity = Counter()
    for record in all_records:
        observed = reconstructed.get(record["symbol"], ms(record["timestamp"]))
        for key in ("market_score", "relative_volume", "delta_4h"):
            assert observed[key] is not None and math.isclose(
                observed[key], record[key], rel_tol=0, abs_tol=1e-9
            ), (record["candidate_id"], key, observed[key], record[key])
            parity[key] += 1
        record.update(
            {
                k: v
                for k, v in observed.items()
                if k not in ("market_score", "delta_4h", "relative_volume")
            }
        )
    augment_incumbents(all_records, trades, archives, reconstructed, end)
    del reconstructed, histories
    grouped = defaultdict(list)
    for record in all_records:
        grouped[record["symbol"]].append(record)
    for symbol, rows in sorted(grouped.items()):
        history = archives.history(symbol, "1m")
        for r in rows:
            r["POST_HOC_FUTURE_LABEL"] = candidate_future(
                history,
                ms(r["timestamp"]),
                r["proposed_entry_reference"],
                r["proposed_stop_reference"],
                end,
            )
            r["incumbents"].sort(key=lambda p: p["symbol"])
            lowest = min(
                r["incumbents"], key=lambda p: (p["current_unrealized_r"], p["symbol"])
            )
            r["descriptive_lowest_current_r_incumbent"] = lowest["symbol"]
            r["replacement_recommendation"] = None
        print("Candidate future labels:", symbol, len(rows), flush=True)
    big = [
        r
        for r in records
        if r["POST_HOC_FUTURE_LABEL"].get("reached_before_stop", {}).get("5") is True
    ]
    negatives = [r for r in records if r["POST_HOC_FUTURE_LABEL"].get("stop_before_1r")]
    # Fixed hash sampling, independent of features, no optimized matching/cutoff.
    negative = sorted(
        negatives,
        key=lambda r: hashlib.sha256(
            ("slot-v1/" + r["candidate_id"]).encode()
        ).hexdigest(),
    )[: len(big)]
    assert len(negative) >= len(big), (
        "Not enough observed negative controls; report limitation rather than inventing controls"
    )
    cohorts = {}
    for level in (5, 10, 20):
        selected = [
            r
            for r in records
            if r["POST_HOC_FUTURE_LABEL"].get("reached_before_stop", {}).get(str(level))
            is True
        ]
        states = [p for r in selected for p in r["incumbents"]]
        cohorts[str(level)] = dict(
            candidate_count=len(selected),
            incumbent_count=len(states),
            incumbent_current_r_bins=dict(
                Counter(
                    "negative"
                    if p["current_unrealized_r"] < 0
                    else "0_to_0.5"
                    if p["current_unrealized_r"] <= 0.5
                    else "0.5_to_1"
                    if p["current_unrealized_r"] <= 1
                    else "above_1"
                    for p in states
                )
            ),
            age_hours=stats(p["age_hours"] for p in states),
            remaining_realized_r=stats(
                p["POST_HOC_FUTURE_LABEL"].get("actual_remaining_realized_r")
                for p in states
            ),
            unique_symbol_days=len(
                {(r["symbol"], r["timestamp"][:10]) for r in selected}
            ),
        )
    labels = {
        str(level): dict(
            Counter(
                "reached"
                if r["POST_HOC_FUTURE_LABEL"]
                .get("reached_before_stop", {})
                .get(str(level))
                is True
                else "stopped_before_target"
                if r["POST_HOC_FUTURE_LABEL"]
                .get("reached_before_stop", {})
                .get(str(level))
                is False
                else "unavailable_or_censored"
                for r in records
            )
        )
        for level in LEVELS
    }
    tail = tail_analysis(trades)
    assert tail["A_only"]["trade_count"] == 32 and tail["R_only"]["trade_count"] == 43
    summary = dict(
        label_type="POST_HOC_FUTURE_LABEL",
        run_id=RUN,
        source_sha=BASE,
        config_hash=meta["config_hash"],
        dataset_role="EXPLORATION",
        primary_variant="S",
        market_context_version="ISOLATED_TOPN_V1",
        max_position_blocked_candidates=len(records),
        post_hoc_label_counts=labels,
        stop_before_1r_count=len(negatives),
        big_opportunity_count=len(big),
        negative_controls=len(negative),
        incumbent_cohorts=cohorts,
        decision_time_distributions=feature_distributions(big, negative),
        A_R_tail_dependence=tail,
    )
    fet = dict(
        future_information_namespace="POST_HOC_FUTURE_LABEL",
        run_id=RUN,
        timestamp="2024-02-24T12:00:00Z",
        specification_correction="FET FILLED in S (A parity), MAX_POSITIONS blocked in R; ETH/MATIC/WLD are R incumbents, not S incumbents.",
        S=dict(
            candidate=s_case,
            pool=[s_event],
            outcome="FILLED",
            POST_HOC_FUTURE_LABEL=dict(
                actual_trade=next(
                    t for t in trades[1] if t["position_id"] == s_event["position_id"]
                )
            ),
        ),
        R=dict(
            candidate=r_case, pool=[ranks_r[target]], outcome="MAX_POSITIONS_BLOCKED"
        ),
        WLD_R=next(p for p in r_case["incumbents"] if p["symbol"] == "WLDUSDT"),
    )
    assert {p["symbol"] for p in r_case["incumbents"]} == {
        "ETHUSDT",
        "MATICUSDT",
        "WLDUSDT",
    }
    assert fet["WLD_R"]["entry_time"] == "2024-02-21T22:00:00Z"
    with read_only(root / "data/replay.db") as db:
        after = fingerprint(db)
        assert before == after and db.total_changes == 0
    methodology = dict(
        run_id=RUN,
        source_sha=BASE,
        analysis_script_sha=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        sqlite_access="file URI mode=ro; PRAGMA query_only=ON; zero total_changes",
        source_run_unchanged=before == after,
        source_fingerprints=before,
        no_replay_started=True,
        no_services_restarted=True,
        production_behavior_changed=False,
        decision_time="Original Risk Decision portfolio snapshot, including same-timestamp earlier fills; not entry/exit interval guesses. Indicators use only 499 already closed 1h candles; deltas use exact prior Top20 score observations, never held-only shared history.",
        mark="Authoritative decision snapshot current_price. Freshness cross-checked with pinned execution-minute open, whose high/low/close are NOT available at decision time.",
        incumbent_r="Original stored account-risk amount. Unrealized PnL reproduces quantized PositionView, net entry fee. Current MFE is gross price excursion * quantity / original risk; peak uses entry, known minute highs (close_time < decision), and known current mark.",
        future_labels="POST_HOC_FUTURE_LABEL only. Candidate price R=(price-proposed_entry_reference)/(proposed_entry_reference-original_stop). No hypothetical fills, sizing, fee-net realized returns or replacement PnL are fabricated.",
        horizons="Raw forward high/low over fixed 6/24/72/168h windows, including price action after a stop; require full contiguous pinned minute coverage. Not a simulated trade MFE. Signed MAE <= 0.",
        target_horizon="Until original stop, first missing pinned minute, or run end (2025-01-01). Not limited to 168h. No hypothetical baseline strategy exit is simulated; target potential is not baseline-policy trade return. Unknown never becomes false. An opening gap reaching a target precedes the later low; otherwise stop wins same-minute target/stop ambiguity.",
        remaining_r="(Actual full-trade net realized PnL - decision-time net-entry-fee unrealized PnL)/original risk = subsequent mark-to-exit equity change, including exit costs. Closed actual trades only; open end positions unavailable. Additional MFE is forward high minus decision mark; record-peak increment separately. Stop exit candle high excluded conservatively.",
        sampling="Negative pool: observed original stop before +1R. Equal-sized sample sorted by SHA256('slot-v1/'+candidate_id), no feature/outcome threshold search. Decision observations overlap in symbol/time and are NOT independent trades; all counts are opportunities, not additive PnL.",
        descriptive_incumbent="Lowest current unrealized R, tie symbol, is a descriptive column only. No executable replacement rule or recommendation. Other slot states retained in full.",
        statistics="Linear-interpolated Q1/Q3, median, IQR; Cliff's delta (positive minus negative), IQR intersection/union. Incumbent feature tests use one median per decision. No optimal cutoff, classifier, sweep or significance/validation claim.",
        source_objects=archives.used,
        feature_reconstruction_parity=dict(parity),
        future_archive_coverage_not_used_for_decisions=True,
        network_requests=0,
        external_or_2026_data_used=False,
    )
    assert len({r["candidate_id"] for r in records}) == len(records)
    assert all(len(r["incumbents"]) == 3 for r in records)
    assert all(ms(r["source_timestamp"]) < ms(r["timestamp"]) for r in records)
    validation = dict(
        run_id=RUN,
        source_sha=BASE,
        completed=True,
        A_S_financial_parity="PASS",
        candidate_count=len(records),
        incumbent_decision_observations=sum(len(r["incumbents"]) for r in records),
        duplicate_candidate_ids=0,
        all_max_position_decisions_have_3_incumbents=True,
        source_run_fingerprints_unchanged=before == after,
        sqlite_uri_mode="ro",
        query_only=True,
        database_writes=0,
        new_replay_started=False,
        services_restarted=False,
        future_fields_namespace="POST_HOC_FUTURE_LABEL",
        decision_features_use_only_closed_history=True,
        candidate_audit_feature_parity=dict(parity),
        incumbent_mark_state_counts=dict(
            Counter(p["mark_state"] for r in records for p in r["incumbents"])
        ),
        incumbent_peak_coverage=dict(
            Counter(
                "available" if p["peak_data_available"] else "unavailable"
                for r in records
                for p in r["incumbents"]
            )
        ),
        forward_window_coverage={
            str(h): dict(
                Counter(
                    "available"
                    if r["POST_HOC_FUTURE_LABEL"]["horizons"][str(h)]["available"]
                    else "unavailable_or_censored"
                    for r in records
                )
            )
            for h in (6, 24, 72, 168)
        },
        negative_control_count=len(negative),
        big_opportunity_count=len(big),
    )
    (out / "validation.json").write_bytes(encoded(validation))
    for level in (5, 10, 20):
        csv_file(
            out / f"blocked-big-{level}r.csv",
            [
                r
                for r in records
                if r["POST_HOC_FUTURE_LABEL"]["reached_before_stop"][str(level)] is True
            ],
        )
    for name, value in (
        ("summary.json", summary),
        ("methodology.json", methodology),
        ("fet-case-study.json", fet),
    ):
        (out / name).write_bytes(encoded(value))
    for name, rows in (
        ("blocked-candidates.csv", records),
        ("blocked-big-opportunities.csv", big),
        ("negative-controls.csv", negative),
    ):
        csv_file(out / name, rows)
    (out / "blocked-candidates.detailed.json").write_bytes(
        encoded(dict(run_id=RUN, records=records))
    )
    report(out, summary, fet)
    filenames = sorted(
        [
            "summary.json",
            "methodology.json",
            "fet-case-study.json",
            "blocked-candidates.csv",
            "blocked-big-opportunities.csv",
            "negative-controls.csv",
            "blocked-candidates.detailed.json",
            "report.md",
            "validation.json",
            "blocked-big-5r.csv",
            "blocked-big-10r.csv",
            "blocked-big-20r.csv",
        ]
    )
    manifest = dict(
        run_id=RUN,
        files={
            n: dict(
                bytes=(out / n).stat().st_size,
                sha256=hashlib.sha256((out / n).read_bytes()).hexdigest(),
            )
            for n in filenames
        },
    )
    (out / "manifest.json").write_bytes(encoded(manifest))
    bundle = out / "portfolio-slot-opportunity-diagnostic-v1.zip"
    with zipfile.ZipFile(
        bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as z:
        for name in filenames + ["manifest.json"]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, (out / name).read_bytes())
    print(
        json.dumps(
            dict(
                summary={
                    k: v
                    for k, v in summary.items()
                    if k != "decision_time_distributions"
                },
                zip=str(bundle),
                sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
            ),
            indent=2,
        ),
        flush=True,
    )


def report(out, summary, fet):
    lines = [
        "# Portfolio Slot Opportunity Diagnostic v1",
        "",
        "2024 EXPLORATION – kizárólag read-only diagnosztika, nem replacement stratégia.",
        "",
        f"Run: `{RUN}`; source SHA: `{BASE}`.",
        "A/S pénzügyi paritás újra ellenőrizve. Új replay, deployment, service restart és production módosítás nem történt.",
        "",
        "## Fontos FET-pontosítás",
        "",
        fet["specification_correction"],
        "",
        "A fő populáció az S variant összes max-position elutasítása. A FET-eset R-hez tartozik; nem keverjük bele az S populációba.",
        "",
        f"S: {summary['max_position_blocked_candidates']} blokkolt opportunity; +5R: {summary['big_opportunity_count']}; negatív kontroll minta: {summary['negative_controls']}.",
        "",
        "## Utólagos címkék – POST_HOC_FUTURE_LABEL",
        "",
        "Az alábbiak price-barrier címkék, nem végrehajtott trade-ek vagy összesíthető realizált profitok.",
        "",
        "| R cél | Elérte stop előtt | Stop előbb | Hiányzó/cenzorált |",
        "|---|---:|---:|---:|",
    ]
    for level, c in summary["post_hoc_label_counts"].items():
        lines.append(
            f"| +{level}R | {c.get('reached', 0)} | {c.get('stopped_before_target', 0)} | {c.get('unavailable_or_censored', 0)} |"
        )
    lines += [
        "",
        "## FET / R slotok AT DECISION TIME",
        "",
        "| Symbol | Age h | Current R | MFE R | Peak drawdown R | POST-HOC remaining R | POST-HOC remaining h |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for p in fet["R"]["candidate"]["incumbents"]:
        future = p["POST_HOC_FUTURE_LABEL"]
        lines.append(
            "| "
            + " | ".join(
                str(x)
                for x in [
                    p["symbol"],
                    p["age_hours"],
                    p["current_unrealized_r"],
                    p["current_mfe_r"],
                    p["current_drawdown_from_peak_r"],
                    future.get("actual_remaining_realized_r"),
                    future.get("remaining_holding_hours"),
                ]
            )
            + " |"
        )
    lines += [
        "",
        "WLD teljes akkori állapota és jövőbeli címkéje a fet-case-study.json fájlban. A jövőbeli exit nem döntési input.",
        "",
        "## Decision-time feature overlap",
        "",
        "Pozitív: +5R stop előtt. Negatív: eredeti stop +1R előtt. Incumbent feature: slotok mediánja, egy adatpont döntésenként.",
        "",
        "| Feature | Big median [Q1,Q3] | Negative median [Q1,Q3] | Cliff delta | IQR overlap |",
        "|---|---|---|---:|---:|",
    ]
    for key, v in summary["decision_time_distributions"].items():
        a, b = v["positive"], v["negative"]
        lines.append(
            f"| {key} | {a['median']} [{a['q1']}, {a['q3']}] n={a['count']} | {b['median']} [{b['q1']}, {b['q3']}] n={b['count']} | {v['cliffs_delta']} | {v['iqr_overlap_ratio']} |"
        )
    lines += [
        "",
        "## Incumbent slot állapotok a nagy opportunity-knél",
        "",
        "```json",
        json.dumps(summary["incumbent_cohorts"], indent=2),
        "```",
        "",
        "## A/R tail dependence",
        "",
        "| Cohort | Closed | Net USDT | PF | Avg R |",
        "|---|---:|---:|---:|---:|",
    ]
    for label, v in summary["A_R_tail_dependence"].items():
        lines.append(
            f"| {label} | {v['trade_count']} | {v['net_pnl']} | {v['profit_factor']} | {v['avg_r']} |"
        )
    wld = fet["WLD_R"]
    lines += [
        "",
        "## Diagnosztikai válaszok",
        "",
        f"A +5R-t elérő {summary['big_opportunity_count']} opportunity {summary['incumbent_cohorts']['5']['unique_symbol_days']} symbol-napot fed le. Létezik megfigyelt blokkolt árfolyam-potenciál, de ez nem bizonyít nettó replacement profitot.",
        "A döntéskori feature-ök erősen átfednek. A riportált effektusok leíró jellegűek; az elemzés nem igazol jól elkülöníthető, előre felismerhető nagy nyerő-populációt, de általános megkülönböztethetetlenséget sem bizonyít.",
        f"FET R-eset: WLD {wld['age_hours']:.0f} órás, aktuális {wld['current_unrealized_r']:.4f}R, ismert peak {wld['current_mfe_r']:.4f}R, peak-visszaesés {wld['current_drawdown_from_peak_r']:.4f}R. WLD-nek volt megfigyelhető givebackje, de aktuális R-je magasabb volt az ETH és MATIC értékénél. A leggyengébb aktuális R leíró sorrendje tehát nem WLD-t jelölte. Ez nem egyértelmű, kizárólag akkori adatokkal igazolt WLD-cserejel.",
        f"POST_HOC_FUTURE_LABEL: WLD később további {wld['POST_HOC_FUTURE_LABEL']['remaining_holding_hours']:.0f} óráig maradt nyitva, az akkori markhoz képest {wld['POST_HOC_FUTURE_LABEL']['actual_remaining_realized_r']:.4f}R-t adott vissza; teljes trade-je mégis {wld['POST_HOC_FUTURE_LABEL']['actual_eventual_trade_r']:.4f}R lett. Ez a jövőbeli kimenetel a döntéskor nem volt ismert.",
        f"A +5R opportunity-knél {summary['incumbent_cohorts']['5']['incumbent_count']} ismétlődő incumbent-állapotból {summary['incumbent_cohorts']['5']['incumbent_current_r_bins'].get('above_1', 0)} aktuálisan +1R felett, {summary['incumbent_cohorts']['5']['incumbent_current_r_bins'].get('negative', 0)} negatív R-ben volt. A slotprobléma nem kizárólag nyilvánvalóan vesztes pozíciókhoz kapcsolódott.",
        "POST_HOC_FUTURE_LABEL: az A-only pozitív PnL tail-dependent: FET nélkül negatívvá válik, FET+PEOPLE nélkül tovább romlik. Ez rögzített cohortból történő kivonás, nem új portfólió-replay, és nem a nélkülük létrejövő cash/entry pálya szimulációja.",
    ]
    lines += [
        "",
        "## Értelmezési korlátok",
        "",
        "A blokkolt lehetőségek ismétlődhetnek ugyanazon symbolon és egymást átfedő jövőbeli útvonalakon; darabszámuk nem független trade-szám. A címkézett +5R nem bizonyít nettó helyettesítési profitot: nincs új portfólió, fill, sizing vagy replacement szimuláció.",
        "A legalacsonyabb aktuális R leíró oszlop, nem replacement ajánlás. Future label kizárólag a POST_HOC_FUTURE_LABEL névtérben szerepel. Hiányzó adat nem vesztes és nem nyerő.",
        "Részletes képletek, fedezet, read-only és változatlansági hash-ek: methodology.json. Nincs threshold-optimalizálás vagy validation következtetés.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    out = (
        args.output or args.root / "artifacts/portfolio-slot-opportunity-diagnostic-v1"
    )
    if out.resolve() == args.root.resolve() or out.resolve().is_relative_to(
        (args.root / "data").resolve()
    ):
        parser.error("Output must not overwrite source/data")
    build(args.root.resolve(), out.resolve())


if __name__ == "__main__":
    main()
