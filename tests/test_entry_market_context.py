"""Read-only research invariants, with synthetic historical data only."""

import io
import json
import sqlite3
import zipfile
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256

import pytest

from crypto_bot.config.settings import Settings
from crypto_bot.data.base import Candle
from crypto_bot.replay.clock import ms
from crypto_bot.replay.universe import HistoricalUniverse
from research.entry_market_context import (
    ALL,
    HOUR,
    auc,
    breadth_context,
    btc_context,
    bucket_diagnostics,
    chronological_fold,
    comparisons,
    load_histories,
    outcomes,
    read_only,
)


def history():
    return [
        (
            Candle(
                i * HOUR,
                (i + 1) * HOUR - 1,
                100 + i / 10,
                101 + i / 10,
                99 + i / 10,
                100 + i / 10,
                10000,
            ),
            10_000_000,
        )
        for i in range(540)
    ]


def btc(histories, now):
    settings = Settings()
    universe = HistoricalUniverse(histories, settings)
    previous = {}
    for hours in (24, 6):
        previous[now - hours * HOUR] = btc_context(
            universe, now - hours * HOUR, settings, {}
        )[0]
    return btc_context(universe, now, settings, previous)


def test_later_candles_cannot_change_prior_btc_context():
    rows = history()
    original = btc({"BTCUSDT": rows}, 520 * HOUR)
    changed = rows[:520] + [
        (Candle(c.open_time, c.close_time, 1000, 2000, 500, 1500, c.volume), q)
        for c, q in rows[520:]
    ]
    assert btc({"BTCUSDT": changed}, 520 * HOUR) == original
    assert original[0]["btc_ema_spread_pct"] > 0
    assert original[0]["btc_return_7d_pct"] > original[0]["btc_return_1h_pct"]
    assert all(
        ms(t) < 520 * HOUR for t in original[1]["feature_source_timestamps"].values()
    )


def test_open_candle_is_excluded_even_when_open_time_is_past():
    rows = history()
    now = 520 * HOUR + 30 * 60000
    # The replay accepts fresh full hours only; no partial/open-candle features.
    result, audit = btc({"BTCUSDT": rows}, now)
    assert all(v is None for v in result.values())
    assert audit["missing"]


def test_btc_missing_history_and_lag_are_explicit():
    result, _ = btc({"BTCUSDT": history()[:399]}, 399 * HOUR)
    assert all(v is None for v in result.values())
    u = HistoricalUniverse({"BTCUSDT": history()}, Settings())
    result, _ = btc_context(u, 520 * HOUR, Settings(), {})
    assert result["btc_ema50_slope_24h_pct"] is None


def scanner_row(symbol="SOLUSDT", close=100 * HOUR - 1, delta=5):
    return {
        "symbol": symbol,
        "total_score": 80,
        "features": {
            "candle_time": close,
            "ema50": 120,
            "ema200": 100,
            "relative_volume": 2,
            "rsi": 60,
            "atr_pct": 2,
        },
        "momentum": {"delta_4h": delta, "delta_24h": delta},
    }


def test_later_snapshot_changes_cannot_change_prior_breadth():
    rows = [scanner_row(), scanner_row("LATERUSDT", 101 * HOUR - 1)]
    original = breadth_context(rows, 100 * HOUR)
    rows[1]["total_score"] = -1000
    rows[1]["features"]["relative_volume"] = 1000
    rows[1]["momentum"]["delta_4h"] = -1000
    assert breadth_context(rows, 100 * HOUR) == original
    assert original[0]["breadth_symbol_count"] == 1


def test_breadth_denominators_exclude_missing_deltas_not_as_zero():
    rows = [
        scanner_row(),
        scanner_row("BTCUSDT", delta=None),
        scanner_row("OLDUSDT", close=98 * HOUR),
    ]
    result, audit = breadth_context(rows, 100 * HOUR)
    assert result["breadth_symbol_count"] == 2
    assert result["breadth_positive_delta4h_pct"] == 100
    assert audit["denominators"]["breadth_positive_delta4h_pct"] == 1
    assert audit["metric_symbols"]["alt_breadth_bullish_ema_pct"] == ["SOLUSDT"]
    assert audit["excluded_stale_rows"] == ["OLDUSDT"]


def test_breadth_empty_returns_null_not_fabricated_zero():
    result, audit = breadth_context([], 100 * HOUR)
    assert result["breadth_symbol_count"] == 0
    assert result["breadth_score70_pct"] is None
    assert audit["denominators"]["breadth_score70_pct"] == 0


@pytest.mark.parametrize(
    "positive,negative,expected",
    [
        ([3, 4], [1, 2], 1),
        ([1, 2], [3, 4], 0),
        ([1, 1], [1, 1], 0.5),
        ([1, 2], [2, 3], 0.125),
        ([], [1], None),
    ],
)
def test_auc_direction_ties_and_missing_class(positive, negative, expected):
    assert auc(positive, negative) == expected


def record(date="2024-01-01", exit_date="2024-01-02", activated=True, value=2, r=1):
    return {
        "decision_time": date,
        "activated": activated,
        "profitable": r > 0,
        "anchor_trade": {
            "exit_time": exit_date,
            "realized_r": r,
            "realized_return_pct": r,
        },
        "features": dict.fromkeys(ALL, value),
    }


def test_class_comparison_missing_counts_and_standardized_effect():
    rows = [
        record(value=3),
        record(value=4),
        record(value=1, activated=False),
        record(value=2, activated=False),
        record(value=None),
    ]
    result = comparisons(rows)["btc_ema_spread_pct"]
    assert result["positive"]["n"] == 2
    assert result["positive"]["missing_count"] == 1
    assert result["positive"]["q25"] == 3.25
    assert result["mean_difference"] == 2
    assert result["cohens_d"] == pytest.approx(2**1.5)
    assert result["roc_auc"] == 1


def test_strict_positive_r_preserves_quantization_dust():
    assert (
        outcomes([record(r=1e-12), record(r=0), record(r=-1e-12)])[
            "profitable_strict_r_gt_zero"
        ]
        == 1
    )


def test_predeclared_buckets_partition_without_search_or_threshold_changes():
    rows = [
        record(value=-1),
        record(value=0),
        record(value=2),
        record(value=10),
        record(value=None),
    ]
    result = bucket_diagnostics(rows, ["btc_ema_spread_pct"])["btc_ema_spread_pct"]
    assert sum(b["trade_count"] for b in result["bins"]) + result["missing_count"] == 5
    assert result["bins"][-1]["trade_count"] == 1
    assert result["bins"][2]["trade_count"] == 1  # zero in [0, 2)


def test_chronological_split_purges_future_labels_and_groups_same_timestamp():
    rows = [
        record(),
        record("2024-03-30", "2024-04-02"),
        record("2024-04-01", "2024-04-02"),
        record("2024-04-01", "2024-04-03"),
        record("2024-03-31", "2024-04-01"),
    ]
    train, test, purged = chronological_fold(rows, ms("2024-04-01"), ms("2024-07-01"))
    assert train == [rows[0]] and test == rows[2:4] and purged == 2
    assert not {r["decision_time"] for r in train} & {r["decision_time"] for r in test}


def test_sqlite_research_connection_rejects_writes(tmp_path):
    path = tmp_path / "test.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE example(value)")
    with (
        read_only(path) as db,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        db.execute("INSERT INTO example VALUES (1)")


def test_archive_reader_pins_bytes_and_never_calls_network(tmp_path, monkeypatch):
    import requests

    monkeypatch.setattr(
        requests.Session, "get", lambda *a, **k: pytest.fail("HTTP prohibited")
    )
    (tmp_path / "objects").mkdir()
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as zipped:
        zipped.writestr(
            "BTC.csv", "1704067200000,100,102,99,101,10,1704070799999,1000\n"
        )
    blob = raw.getvalue()
    sha = sha256(blob).hexdigest()
    (tmp_path / "objects" / (sha + ".zip")).write_bytes(blob)
    key = "data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01.zip"
    missing = "data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2023-12.zip"
    with sqlite3.connect(tmp_path / "candles.sqlite") as db:
        db.execute("CREATE TABLE objects(key,status,sha256)")
        db.executemany(
            "INSERT INTO objects VALUES(?,?,?)",
            [(key, "COMPLETE", sha), (missing, "MISSING", None)],
        )
    cfg = {
        "start": "2024-01-01",
        "end": "2024-02-01",
        "warmup_hours": 524,
        "candidate_symbols": ["BTCUSDT"],
        "scanner_settings": asdict(Settings()),
    }
    meta = {"historical_data_revision": {key: sha, missing: None}}
    histories, audit = load_histories(tmp_path, cfg, meta)
    assert len(histories["BTCUSDT"]) == 1 and audit == meta["historical_data_revision"]
    before = (tmp_path / "candles.sqlite").read_bytes()
    changed = deepcopy(meta)
    changed["historical_data_revision"][key] = "changed"
    with pytest.raises(ValueError, match="Changed anchor source"):
        load_histories(tmp_path, cfg, changed)
    (tmp_path / "objects" / (sha + ".zip")).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Corrupt pinned"):
        load_histories(tmp_path, cfg, meta)
    assert (tmp_path / "candles.sqlite").read_bytes() == before


def test_production_never_imports_research_or_predictive_models():
    from pathlib import Path

    root = Path(__file__).parents[1] / "crypto_bot"
    for path in root.rglob("*.py"):
        text = path.read_text()
        assert "from research.entry_market_context" not in text
        assert "import sklearn" not in text
        assert "from sklearn" not in text
    # Research writes JSON outputs, never trading settings or portfolio records.
    json.dumps(outcomes([record()]), allow_nan=False)
