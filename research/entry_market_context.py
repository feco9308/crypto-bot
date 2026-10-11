"""Read-only context reconstruction of EXISTING trades; never runs a portfolio.

Run with python -m research.entry_market_context --root /path/to/production.
Only the output directory is written. SQLite and pinned archive files are read
directly, without ReplayStore/CandleCache constructors, workers or HTTP clients.
"""

import argparse
import hashlib
import io
import json
import math
import sqlite3
import statistics as st
import zipfile
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from crypto_bot.config.settings import Settings
from crypto_bot.data.base import Candle
from crypto_bot.indicators.technical import features
from crypto_bot.replay.clock import at, ms
from crypto_bot.replay.historical_data import HistoricalData
from crypto_bot.replay.universe import CandleHistory, HistoricalUniverse
from crypto_bot.scanner.momentum import score_momentum
from crypto_bot.scanner.scoring import score

ANCHOR = "b9275979397440f2aabbe4a89e4f9cc4"
HOUR = 3600000
COIN = [
    "entry_score",
    "delta_1h",
    "delta_4h",
    "delta_24h",
    "rsi",
    "atr_pct",
    "relative_volume",
    "momentum",
    "range_position",
]
BTC = [
    "btc_price",
    "btc_ema50",
    "btc_ema200",
    "btc_ema_spread_pct",
    "btc_price_vs_ema50_pct",
    "btc_price_vs_ema200_pct",
    "btc_ema50_slope_6h_pct",
    "btc_ema50_slope_24h_pct",
    "btc_ema200_slope_24h_pct",
    "btc_return_1h_pct",
    "btc_return_4h_pct",
    "btc_return_24h_pct",
    "btc_return_7d_pct",
    "btc_rsi14",
    "btc_atr_pct",
]
BREADTH = [
    "breadth_symbol_count",
    "breadth_bullish_ema_pct",
    "breadth_score70_pct",
    "breadth_score80_pct",
    "breadth_positive_delta4h_pct",
    "breadth_positive_delta24h_pct",
    "breadth_relvol_gt1_pct",
    "breadth_relvol_gt1_5_pct",
    "breadth_median_score",
    "breadth_median_delta4h",
    "breadth_median_delta24h",
    "breadth_median_rsi",
    "breadth_median_atr_pct",
    "alt_breadth_bullish_ema_pct",
    "alt_breadth_positive_delta4h_pct",
    "alt_breadth_score70_pct",
]
ALL = COIN + BTC + BREADTH

# Fixed before observing/ranking any outcome. Reporting bins, never BUY gates.
BUCKETS = {
    **{k: [-math.inf, -5, -2, 0, 2, 5, math.inf] for k in BTC if "return" in k},
    **{k: [-math.inf, -1, 0, 0.5, 1, 2, math.inf] for k in BTC if "slope" in k},
    **{
        k: [-math.inf, -2, 0, 2, 5, 10, math.inf]
        for k in BTC
        if "spread" in k or "price_vs" in k
    },
    "btc_rsi14": [-math.inf, 40, 50, 60, 70, math.inf],
    "btc_atr_pct": [-math.inf, 0.25, 0.5, 1, 2, math.inf],
    **{
        k: [-math.inf, 20, 40, 60, 80, math.inf]
        for k in BREADTH
        if k.endswith("_pct") and "atr" not in k
    },
    "breadth_median_score": [-math.inf, 40, 50, 60, 70, 80, math.inf],
    **{
        k: [-math.inf, -10, -5, 0, 5, 10, math.inf]
        for k in ("breadth_median_delta4h", "breadth_median_delta24h")
    },
    "breadth_median_rsi": [-math.inf, 40, 50, 60, 70, math.inf],
    "breadth_median_atr_pct": [-math.inf, 1, 2, 3, 5, math.inf],
    "breadth_symbol_count": [-math.inf, 40, 50, 51, 54, math.inf],
}


def read_only(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def anchor(db):
    row = db.execute(
        "SELECT status,config,metadata FROM replay_runs WHERE id=?", (ANCHOR,)
    ).fetchone()
    if not row or row[0] != "COMPLETED":
        raise ValueError("Completed immutable B anchor required")
    trades = [
        json.loads(r[0])
        for r in db.execute(
            "SELECT data FROM replay_trades WHERE run_id=? AND variant=0 ORDER BY position_id",
            (ANCHOR,),
        )
    ]
    counts = Counter(
        "ACTIVATED"
        if t.get("activation_075r_time")
        else "ORIGINAL_STOP"
        if t["exit_reason"] == "ORIGINAL_STOP"
        else "UNCLASSIFIED"
        for t in trades
    )
    if len(trades) != 1506 or counts != {"ACTIVATED": 847, "ORIGINAL_STOP": 659}:
        raise ValueError(f"Unexpected B anchor outcomes: {counts}")
    if any(t["status"] != "CLOSED" for t in trades):
        raise ValueError("This research requires all 1506 closed anchor trades")
    return json.loads(row[1]), json.loads(row[2]), trades


def database_fingerprint(db):
    tables = [
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'replay_%'"
        )
    ]
    result = {}
    for table in sorted(tables):
        columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
        if "run_id" in columns or table == "replay_runs":
            key = "id" if table == "replay_runs" else "run_id"
            rows = db.execute(
                f'SELECT * FROM "{table}" WHERE "{key}"=? ORDER BY rowid', (ANCHOR,)
            ).fetchall()
            result[table] = {"rows": len(rows), "sha256": digest(rows)}
    return result


def load_histories(cache, config, metadata):
    """Parse the pinned ZIP bytes, not a possibly superseded merged candle table."""
    start = ms(config["start"]) - config["warmup_hours"] * HOUR
    end = ms(config["end"])
    pins = metadata["historical_data_revision"]
    symbols = config["candidate_symbols"] or metadata["resolved_candidate_symbols"]
    settings = Settings(**config["scanner_settings"])
    month = at(start).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    months = []
    while month < at(end):
        months.append(month.strftime("%Y-%m"))
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    histories, audit = {}, {}
    with read_only(cache / "candles.sqlite") as db:
        for index, symbol in enumerate(symbols):
            if not HistoricalUniverse.allowed(symbol, settings):
                continue
            rows = []
            for stamp in months:
                key = f"data/spot/monthly/klines/{symbol}/1h/{symbol}-1h-{stamp}.zip"
                if key not in pins:
                    raise ValueError("Unpinned source object: " + key)
                sha = pins[key]
                manifest = db.execute(
                    "SELECT status,sha256 FROM objects WHERE key=?", (key,)
                ).fetchone()
                if manifest != ("COMPLETE" if sha else "MISSING", sha):
                    raise ValueError("Changed anchor source revision: " + key)
                audit[key] = sha
                if sha is None:
                    continue
                blob = (cache / "objects" / (sha + ".zip")).read_bytes()
                if hashlib.sha256(blob).hexdigest() != sha:
                    raise ValueError("Corrupt pinned archive: " + key)
                with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                    parsed = HistoricalData.parse(
                        archive.read(archive.namelist()[0]).decode("utf-8-sig")
                    )
                rows.extend(
                    (Candle(r[0], r[1], *r[2:7]), r[7])
                    for r in parsed
                    if start <= r[0] < end
                )
            if rows:
                histories[symbol] = CandleHistory(
                    sorted(rows, key=lambda r: r[0].open_time)
                )
            if index % 100 == 0:
                print(f"Pinned hourly archives: {index}/{len(symbols)}", flush=True)
    return histories, audit


def btc_context(universe, timestamp, settings, previous):
    """Lag EMAs are the same rolling-window EMA as the replay at that lag time."""
    market = universe.market("BTCUSDT", timestamp)
    if market is None:
        return {k: None for k in BTC}, {"missing": "no fresh closed BTC history"}
    candles = [c for c, _ in universe.as_of("BTCUSDT", timestamp)]
    f = features(candles, market, settings)
    result = {
        "btc_price": f["price"],
        "btc_ema50": f["ema50"],
        "btc_ema200": f["ema200"],
        "btc_rsi14": f["rsi"],
        "btc_atr_pct": f["atr_pct"],
    }

    def ratio(a, b):
        return (a / b - 1) * 100

    result.update(
        btc_ema_spread_pct=ratio(f["ema50"], f["ema200"]),
        btc_price_vs_ema50_pct=ratio(f["price"], f["ema50"]),
        btc_price_vs_ema200_pct=ratio(f["price"], f["ema200"]),
    )
    sources = {k: at(candles[-1].close_time).isoformat() + "Z" for k in result}
    lag_sources = {}
    for hours, period in ((6, 50), (24, 50), (24, 200)):
        key = f"btc_ema{period}_slope_{hours}h_pct"
        lag = previous.get(timestamp - hours * HOUR)
        result[key] = ratio(f[f"ema{period}"], lag[f"btc_ema{period}"]) if lag else None
        sources[key] = sources["btc_price"] if lag else None
        lag_sources[key] = (
            at(timestamp - hours * HOUR - 1).isoformat() + "Z" if lag else None
        )
    for hours, label in ((1, "1h"), (4, "4h"), (24, "24h"), (168, "7d")):
        key = f"btc_return_{label}_pct"
        lag = candles[-hours - 1] if len(candles) > hours else None
        result[key] = ratio(f["price"], lag.close) if lag else None
        sources[key] = sources["btc_price"] if lag else None
        lag_sources[key] = at(lag.close_time).isoformat() + "Z" if lag else None
    return result, {
        "feature_source_timestamps": sources,
        "lag_source_timestamps": lag_sources,
        "window_first_close": at(candles[0].close_time).isoformat() + "Z",
        "window_last_close": sources["btc_price"],
        "closed_candle_count": len(candles),
    }


def breadth_context(rows, timestamp):
    """Each metric denominator includes only fresh rows with its required values."""
    fresh = [
        r for r in rows if timestamp - HOUR <= r["features"]["candle_time"] < timestamp
    ]
    result = {"breadth_symbol_count": len(fresh)}
    denominators, included = {}, {}

    def metric(key, extract, predicate=None, subset=None):
        selected = fresh if subset is None else subset
        values = [(r["symbol"], extract(r)) for r in selected]
        values = [(s, v) for s, v in values if v is not None and math.isfinite(v)]
        denominators[key] = len(values)
        included[key] = [s for s, _ in values]
        result[key] = (
            (
                sum(predicate(v) for _, v in values) / len(values) * 100
                if predicate
                else st.median(v for _, v in values)
            )
            if values
            else None
        )

    for prefix, subset in (
        ("breadth", fresh),
        (
            "alt_breadth",
            [r for r in fresh if r["symbol"] not in ("BTCUSDT", "ETHUSDT")],
        ),
    ):
        metric(
            prefix + "_bullish_ema_pct",
            lambda r: r["features"]["ema50"] - r["features"]["ema200"],
            lambda x: x > 0,
            subset,
        )
        metric(
            prefix + "_score70_pct",
            lambda r: r["total_score"],
            lambda x: x >= 70,
            subset,
        )
        metric(
            prefix + "_positive_delta4h_pct",
            lambda r: r["momentum"]["delta_4h"],
            lambda x: x > 0,
            subset,
        )
    metric("breadth_score80_pct", lambda r: r["total_score"], lambda x: x >= 80)
    metric(
        "breadth_positive_delta24h_pct",
        lambda r: r["momentum"]["delta_24h"],
        lambda x: x > 0,
    )
    for suffix, value in (("gt1", 1), ("gt1_5", 1.5)):
        metric(
            f"breadth_relvol_{suffix}_pct",
            lambda r: r["features"]["relative_volume"],
            lambda x: x > value,
        )
    for name in ("score", "delta4h", "delta24h", "rsi", "atr_pct"):
        metric(
            "breadth_median_" + name,
            lambda r, n=name: (
                r["total_score"]
                if n == "score"
                else r["momentum"]["delta_" + n[5:]]
                if n.startswith("delta")
                else r["features"][n]
            ),
        )
    return result, {
        "fresh_symbols": sorted(r["symbol"] for r in fresh),
        "excluded_stale_rows": sorted(r["symbol"] for r in rows if r not in fresh),
        "denominators": denominators,
        "metric_symbols": included,
        "snapshot_timestamps": {
            r["symbol"]: at(r["features"]["candle_time"]).isoformat() + "Z"
            for r in fresh
        },
    }


def reconstruct(histories, config, trades, progress=None):
    settings = Settings(**config["scanner_settings"])
    if config["score_version"] != "heuristic-v1":
        raise ValueError("Unsupported anchor score version")
    universe = HistoricalUniverse(
        histories, settings, float(config["spread_approximation_pct"])
    )
    entries = defaultdict(list)
    for t in trades:
        entries[ms(t["scanner_snapshot_timestamp"])].append(t)
    history, btc_previous, contexts = defaultdict(list), {}, {}
    cursor, last = ms(config["start"]) - 24 * HOUR, max(entries)
    parity_checks = 0
    while cursor <= last:
        markets = universe.ranked(cursor)
        by_symbol = {m.symbol: m for m in markets}
        # These fills have already happened as-of scanner start, unlike future outcomes.
        held = {
            t["symbol"]
            for t in trades
            if ms(t["entry_time"]) < cursor <= ms(t["exit_time"])
        }
        for symbol in sorted(held):
            if symbol not in by_symbol:
                market = universe.market(symbol, cursor)
                if market:
                    by_symbol[symbol] = market
        rows = []
        for symbol, market in by_symbol.items():
            candles = [c for c, _ in universe.as_of(symbol, cursor)]
            f = features(candles, market, settings)
            computed = score(f, settings)
            momentum = score_momentum(
                computed["total_score"],
                at(cursor),
                history[symbol],
                settings.momentum_windows,
                settings.history_tolerance_minutes,
            )
            rows.append(dict(symbol=symbol, features=f, momentum=momentum, **computed))
            history[symbol].append(
                SimpleNamespace(
                    timestamp=at(cursor), total_score=computed["total_score"]
                )
            )
        history = defaultdict(
            list,
            {
                s: [r for r in rs if ms(r.timestamp) >= cursor - 26 * HOUR]
                for s, rs in history.items()
            },
        )
        btc, btc_audit = btc_context(universe, cursor, settings, btc_previous)
        if btc["btc_price"] is not None:
            btc_previous[cursor] = btc
        btc_previous = {
            t: v for t, v in btc_previous.items() if t >= cursor - 24 * HOUR
        }
        if cursor in entries:
            breadth, breadth_audit = breadth_context(rows, cursor)
            for trade in entries[cursor]:
                row = next(r for r in rows if r["symbol"] == trade["symbol"])
                checks = {
                    "entry_score": row["total_score"],
                    **row["features"],
                    **row["momentum"],
                }
                for key in COIN:
                    observed, expected = checks[key], trade[key]
                    if (observed is None) != (expected is None) or (
                        observed is not None
                        and not math.isclose(
                            observed, expected, rel_tol=1e-12, abs_tol=1e-10
                        )
                    ):
                        raise ValueError(
                            f"Anchor snapshot mismatch {trade['position_id']} {key}: {observed} != {expected}"
                        )
                    parity_checks += 1
                if not btc["btc_ema50"] > btc["btc_ema200"]:
                    raise ValueError(
                        "BTC regime reconstruction disagrees with B BULLISH anchor"
                    )
                if (
                    btc_audit["window_last_close"]
                    != trade["btc_regime_source_timestamp"]
                ):
                    raise ValueError("BTC source timestamp parity failure")
            contexts[cursor] = {
                "features": btc | breadth,
                "source_audit": {
                    "decision_time": at(cursor).isoformat() + "Z",
                    "btc": btc_audit,
                    "breadth": breadth_audit,
                    "top50_symbols": [m.symbol for m in markets],
                    "already_held_symbols": sorted(held),
                },
            }
        if cursor % (7 * 24 * HOUR) == 0:
            print("Context reconstruction: " + at(cursor).isoformat(), flush=True)
            if progress:
                progress(cursor, len(contexts))
        cursor += HOUR
    return contexts, parity_checks


def auc(positive, negative):
    """Exact Mann-Whitney AUC with half credit for ties, increasing feature direction."""
    if not positive or not negative:
        return None
    groups = defaultdict(lambda: [0, 0])
    for v in positive:
        groups[v][0] += 1
    for v in negative:
        groups[v][1] += 1
    below, wins = 0, 0.0
    for _, (p, n) in sorted(groups.items()):
        wins += p * (below + n / 2)
        below += n
    return wins / (len(positive) * len(negative))


def quantile(values, p):
    values = sorted(values)
    if not values:
        return None
    i = (len(values) - 1) * p
    lo = int(i)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (i - lo)


def group_stats(values, total):
    return {
        "n": len(values),
        "missing_count": total - len(values),
        "mean": st.mean(values) if values else None,
        "median": st.median(values) if values else None,
        "q25": quantile(values, 0.25),
        "q75": quantile(values, 0.75),
    }


def comparisons(records, target="activated"):
    result = {}
    for feature in ALL:
        p = [
            r["features"][feature]
            for r in records
            if r[target] and r["features"].get(feature) is not None
        ]
        n = [
            r["features"][feature]
            for r in records
            if not r[target] and r["features"].get(feature) is not None
        ]
        difference = st.mean(p) - st.mean(n) if p and n else None
        pooled = (
            math.sqrt(
                ((len(p) - 1) * st.variance(p) + (len(n) - 1) * st.variance(n))
                / (len(p) + len(n) - 2)
            )
            if len(p) > 1 and len(n) > 1
            else None
        )
        a = auc(p, n)
        result[feature] = {
            "positive": group_stats(p, sum(r[target] for r in records)),
            "negative": group_stats(n, sum(not r[target] for r in records)),
            "mean_difference": difference,
            "median_difference": st.median(p) - st.median(n) if p and n else None,
            "cohens_d": difference / pooled if pooled else None,
            "roc_auc": a,
            "auc_distance_from_chance": abs(a - 0.5) if a is not None else None,
        }
    return result


def outcomes(records):
    rs = [float(r["anchor_trade"]["realized_r"]) for r in records]
    positive, negative = sum(max(0, r) for r in rs), -sum(min(0, r) for r in rs)
    return {
        "trade_count": len(records),
        "activated": sum(r["activated"] for r in records),
        "original_stop": sum(not r["activated"] for r in records),
        "activation_pct": sum(r["activated"] for r in records) / len(records) * 100
        if records
        else None,
        "original_stop_pct": sum(not r["activated"] for r in records)
        / len(records)
        * 100
        if records
        else None,
        "profitable_strict_r_gt_zero": sum(r["profitable"] for r in records),
        "avg_r": st.mean(rs) if rs else None,
        "profit_factor_r": positive / negative if negative else None,
        "profit_factor_r_no_losses": bool(rs) and negative == 0,
        "avg_return_pct": st.mean(
            r["anchor_trade"]["realized_return_pct"] for r in records
        )
        if records
        else None,
    }


def bucket_diagnostics(records, features_to_show):
    result = {}
    for feature in features_to_show:
        if feature not in BUCKETS:
            continue
        edges = BUCKETS[feature]
        bins = []
        for lo, hi in zip(edges, edges[1:]):
            subset = [
                r
                for r in records
                if r["features"].get(feature) is not None
                and lo <= r["features"][feature] < hi
            ]
            bins.append(
                {
                    "lower_inclusive": lo if math.isfinite(lo) else None,
                    "upper_exclusive": hi if math.isfinite(hi) else None,
                    **outcomes(subset),
                }
            )
        result[feature] = {
            "bins": bins,
            "missing_count": sum(r["features"].get(feature) is None for r in records),
        }
    return result


def chronological_fold(records, start, end):
    """Purge unresolved training labels; group equal decision timestamps together."""
    candidate = [r for r in records if ms(r["decision_time"]) < start]
    train = [r for r in candidate if ms(r["anchor_trade"]["exit_time"]) < start]
    test = [r for r in records if start <= ms(r["decision_time"]) < end]
    return train, test, len(candidate) - len(train)


def predictive(records):
    # Research-only dependencies live in a separate environment, never production.
    import numpy as np
    import sklearn
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.tree import DecisionTreeClassifier

    sets = {"coin_local": COIN, "market_context": BTC + BREADTH, "combined": ALL}
    result = {
        "sklearn_version": sklearn.__version__,
        "feature_sets": sets,
        "parameters": {
            "logistic": {"C": 1.0, "max_iter": 2000, "solver": "lbfgs"},
            "tree": {"max_depth": 3, "min_samples_leaf": 30, "random_state": 0},
        },
        "folds": [],
        "models": {},
    }
    for quarter, start, end in (
        ("Q2", "2024-04-01", "2024-07-01"),
        ("Q3", "2024-07-01", "2024-10-01"),
        ("Q4", "2024-10-01", "2025-01-01"),
    ):
        train, test, purged = chronological_fold(records, ms(start), ms(end))
        result["folds"].append(
            {
                "test_quarter": quarter,
                "test_start": start,
                "test_end": end,
                "train_n": len(train),
                "test_n": len(test),
                "purged_unresolved_labels": purged,
                "train_last_label_available": max(
                    r["anchor_trade"]["exit_time"] for r in train
                ),
                "train_activated": sum(r["activated"] for r in train),
                "test_activated": sum(r["activated"] for r in test),
            }
        )
        for name, keys in sets.items():

            def x(rows):
                return np.array(
                    [
                        [
                            r["features"].get(k)
                            if r["features"].get(k) is not None
                            else np.nan
                            for k in keys
                        ]
                        for r in rows
                    ],
                    dtype=float,
                )

            y_train, y_test = (
                [int(r["activated"]) for r in train],
                [int(r["activated"]) for r in test],
            )
            for kind, model in (
                ("logistic", LogisticRegression(C=1.0, max_iter=2000, solver="lbfgs")),
                (
                    "shallow_tree",
                    DecisionTreeClassifier(
                        max_depth=3, min_samples_leaf=30, random_state=0
                    ),
                ),
            ):
                pipeline = make_pipeline(
                    SimpleImputer(strategy="median", keep_empty_features=True),
                    StandardScaler(),
                    model,
                )
                pipeline.fit(x(train), y_train)
                predicted = pipeline.predict_proba(x(test))[:, 1]
                a = (
                    float(roc_auc_score(y_test, predicted))
                    if len(set(y_test)) == 2
                    else None
                )
                key = name + "/" + kind
                result["models"].setdefault(key, {"fold_auc": []})["fold_auc"].append(
                    {"quarter": quarter, "auc": a}
                )
    for value in result["models"].values():
        values = [f["auc"] for f in value["fold_auc"] if f["auc"] is not None]
        value["mean_auc"] = st.mean(values) if values else None
    return result


def analysis(records):
    compared = comparisons(records)
    ranking = sorted(
        compared,
        key=lambda k: compared[k]["auc_distance_from_chance"] or 0,
        reverse=True,
    )
    strongest = [k for k in ranking if k in BTC and k in BUCKETS][:4] + [
        k for k in ranking if k in BREADTH
    ][:4]
    time = {}
    for q in range(1, 5):
        time[f"Q{q}"] = [
            r for r in records if (int(r["decision_time"][5:7]) - 1) // 3 + 1 == q
        ]
    for m in range(1, 13):
        time[f"2024-{m:02d}"] = [
            r for r in records if r["decision_time"].startswith(f"2024-{m:02d}")
        ]
    robust = {
        key: {
            "outcomes": outcomes(rows),
            "feature_comparisons": {
                k: v for k, v in comparisons(rows).items() if k in strongest
            },
            "context_distribution": {
                k: group_stats(
                    [
                        r["features"][k]
                        for r in rows
                        if r["features"].get(k) is not None
                    ],
                    len(rows),
                )
                for k in strongest
            },
            "minimum_monthly_sample_30_met": len(rows) >= 30,
        }
        for key, rows in time.items()
    }
    return {
        "outcome_counts": outcomes(records),
        "activated_vs_original_stop": compared,
        "profitable_vs_non_profitable": comparisons(records, "profitable"),
        "auc_effect_size_ranking": [{"feature": k, **compared[k]} for k in ranking],
        "strongest_context_features": strongest,
        "strongest_selection": "Top 4 derived BTC strengths and top 4 breadth features by whole-year AUC distance; absolute BTC price/EMA levels remain in all-feature tables and model inputs, but have no meaningful fixed-price bucket across 2024",
        "bucket_diagnostics": bucket_diagnostics(records, strongest),
        "time_robustness": robust,
        "predictive_diagnostics": predictive(records),
        "missing_data_summary": {
            k: sum(r["features"].get(k) is None for r in records) for k in ALL
        },
        "near_zero_r_count_abs_lte_1e_minus_8": sum(
            abs(r["anchor_trade"]["realized_r"]) <= 1e-8 for r in records
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--analyze-existing-detail",
        action="store_true",
        help="Recompute statistics of saved contexts only, never a replay",
    )
    args = parser.parse_args()
    output = args.output or args.root / "artifacts/entry-market-context-2024"
    output.mkdir(parents=True, exist_ok=True)
    detailed_path = output / "entry-market-context-2024.detailed.json"
    compact_path = output / "entry-market-context-2024.compact.json"
    with read_only(args.root / "data/replay.db") as db:
        config, metadata, trades = anchor(db)
        before = database_fingerprint(db)
    if args.analyze_existing_detail:
        detailed = json.loads(detailed_path.read_text())
        if detailed["run_id"] != ANCHOR or detailed["anchor_fingerprint"] != before:
            raise ValueError("Saved context anchor changed")
    else:
        histories, revisions = load_histories(
            args.root / "data/replay-cache", config, metadata
        )
        contexts, parity = reconstruct(histories, config, trades)
        records = []
        for t in trades:
            context = contexts[ms(t["scanner_snapshot_timestamp"])]
            records.append(
                {
                    "position_id": t["position_id"],
                    "symbol": t["symbol"],
                    "entry_time": t["entry_time"],
                    "decision_time": t["scanner_snapshot_timestamp"],
                    "outcome": "ACTIVATED"
                    if t["activation_075r_time"]
                    else "ORIGINAL_STOP",
                    "activated": t["activation_075r_time"] is not None,
                    "profitable": t["realized_r"] > 0,
                    "features": {k: t[k] for k in COIN} | context["features"],
                    "source_audit_key": t["scanner_snapshot_timestamp"],
                    "anchor_trade": t,
                }
            )
        detailed = {
            "schema_version": 1,
            "run_id": ANCHOR,
            "dataset_role": "EXPLORATION",
            "anchor_fingerprint": before,
            "config": config,
            "pinned_hourly_source_revisions": revisions,
            "source_audits": {
                at(k).isoformat() + "Z": v["source_audit"] for k, v in contexts.items()
            },
            "no_lookahead_proof": {
                "anchor_coin_feature_parity_checks": parity,
                "anchor_coin_feature_mismatches": 0,
                "btc_regime_source_timestamp_mismatches": 0,
                "source_archive_sha256_verified": len(
                    [v for v in revisions.values() if v]
                ),
                "source_timestamps_strictly_before_decision": all(
                    ms(ts) < k
                    for k, v in contexts.items()
                    for ts in list(
                        v["source_audit"]["btc"]["feature_source_timestamps"].values()
                    )
                    + list(v["source_audit"]["btc"]["lag_source_timestamps"].values())
                    + list(v["source_audit"]["breadth"]["snapshot_timestamps"].values())
                    if ts
                ),
            },
            "trades": records,
        }
        detailed_path.write_text(
            json.dumps(detailed, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
    compact = {
        "schema_version": 1,
        "run_id": ANCHOR,
        "dataset_role": "2024 EXPLORATION",
        "methodology": {
            "anchor": "1506 existing B fills only; no portfolio replay",
            "btc": "Same technical.features rolling 499 closed 1h candles; lag EMA recomputed as-of lag timestamp",
            "breadth": "HistoricalUniverse.ranked rolling-volume Top50 PLUS already-held fresh B symbols, exactly as anchor scanner",
            "denominator": "Feature-specific fresh valid rows; missing score deltas excluded, never set to zero; per-metric symbols exported",
            "alt": "Excludes BTCUSDT and ETHUSDT, matching existing asset_group convention",
            "sources": "SHA256-pinned local Binance ZIP archives; cached only, SQLite mode=ro; no HTTP",
            "outcomes": "activation_075r_time != null vs ORIGINAL_STOP; profitable STRICT realized_r > 0; rounding dust retained",
            "statistics": "Positive minus negative; pooled sample-variance Cohen d; linear percentiles; increasing-direction rank AUC with 0.5 ties",
            "ranking": "Exploratory whole-year absolute AUC distance; no multiple-testing correction, no threshold or hyperparameter search",
            "buckets": "Fixed edges declared in source before reconstruction; lower inclusive, upper exclusive; no optimized cuts",
            "time_groups": "UTC ENTRY DECISION month/quarter, not exit month; monthly minimum 30",
            "predictive": "Expanding train Q1/test Q2, Q1-Q2/test Q3, Q1-Q3/test Q4; exclude training exit_time >= test_start; same timestamps grouped; train-only median imputation/scaling; fixed models; all features, no whole-year ranking-based selection",
            "limitations": "Selected realized entries only, shared market snapshots and overlapping holds are dependent; descriptive associations, no causal/filter recommendation; not HOLDOUT; absolute BTC prices may encode time",
        },
        "no_lookahead_proof": detailed["no_lookahead_proof"],
        **analysis(detailed["trades"]),
        "exports": {
            "detailed": str(detailed_path.resolve()),
            "compact": str(compact_path.resolve()),
        },
    }
    with read_only(args.root / "data/replay.db") as db:
        after = database_fingerprint(db)
    if before != after:
        raise ValueError("Anchor data changed during read-only analysis")
    compact["no_lookahead_proof"]["anchor_tables_unchanged"] = True
    compact_path.write_text(
        json.dumps(compact, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    print(
        json.dumps(
            {
                "exports": compact["exports"],
                "outcomes": compact["outcome_counts"],
                "predictive": compact["predictive_diagnostics"]["models"],
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
