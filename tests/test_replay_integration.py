"""Isolation, worker recovery, archive caching and read-only research exports."""

import csv
import io
import json
import sqlite3
import threading
import zipfile
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_replay import START, Synthetic, config, row

from crypto_bot.config.settings import Settings
from crypto_bot.replay.cache import CandleCache
from crypto_bot.replay.clock import at
from crypto_bot.replay.config import isolated_paths
from crypto_bot.replay.engine import ReplayEngine, Variant
from crypto_bot.replay.exporter import bundle, csv_export, payload
from crypto_bot.replay.historical_data import HistoricalData
from crypto_bot.replay.models import ReplayStore
from crypto_bot.replay.symbols import valid_symbol
from crypto_bot.replay.universe import HistoricalUniverse
from crypto_bot.replay.worker import Cancelled, ReplayWorker, Stopping
from crypto_bot.storage.database import Database
from crypto_bot.trading.domain import D
from crypto_bot.web.app import create_app


@pytest.fixture
def store(tmp_path):
    return ReplayStore(tmp_path / "replay" / "replay.db")


class Stops(Synthetic):
    def minutes(self, *args, **kwargs):
        rows = super().minutes(*args, **kwargs)
        for candle in rows.values():
            candle["low"] = candle["open"] - 10
            candle["close"] = candle["open"] - 5
        return rows


@pytest.fixture
def completed(store):
    run_id = store.create(config(variants=[{}, dict(exit_policy="fixed_tp")]))
    store.claim("test")
    ReplayEngine(store, Stops()).run(run_id)
    return store, run_id


def test_archive_cache_reuse_and_resumable_partial(tmp_path):
    cache = CandleCache(tmp_path / "cache")
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "BTCUSDT-1h-2025-01.csv",
            "1735689600000000,100,101,99,100,5,1735693199999999,500,1,2,3,0\n",
        )
    blob = data.getvalue()
    http = Mock()
    response = Mock(status_code=200)
    response.iter_content.return_value = [blob]
    http.get.return_value = response
    provider = HistoricalData(cache, http, pause=0)
    assert provider.object("BTCUSDT", "1h", "monthly", "2025-01")
    assert provider.object("BTCUSDT", "1h", "monthly", "2025-01")
    assert http.get.call_count == 1 and provider.hits == 1
    assert cache.rows("BTCUSDT", "1h", START, START + 3600000)[0][0] == START
    import hashlib

    key = "data/spot/monthly/klines/ETHUSDT/1h/ETHUSDT-1h-2025-01.zip"
    part = cache.root / "objects" / (hashlib.sha256(key.encode()).hexdigest() + ".part")
    part.write_bytes(blob[:20])
    response.status_code = 206
    response.iter_content.return_value = [blob[20:]]
    assert provider.object("ETHUSDT", "1h", "monthly", "2025-01")
    assert http.get.call_args.kwargs["headers"] == {"Range": "bytes=20-"}
    assert not part.exists()
    http.post.assert_not_called()


def test_archive_missing_cached_and_no_fabricated_prices(tmp_path):
    cache = CandleCache(tmp_path / "cache")
    http = Mock()
    http.get.return_value.status_code = 404
    provider = HistoricalData(cache, http, pause=0)
    assert provider.minutes("BTCUSDT", START, START + 3600000) == {}
    assert provider.minutes("BTCUSDT", START, START + 3600000) == {}
    assert http.get.call_count == 1
    assert provider.missing == 2


@pytest.mark.parametrize("symbol", ["BUSDT", "CUSDT", "币安人生USDT", "1MBABYDOGEUSDT"])
def test_archive_valid_single_character_and_unicode_identifiers(tmp_path, symbol):
    assert valid_symbol(symbol)
    captured = config(candidate_symbols=[symbol])
    assert captured["candidate_symbols"] == [symbol]
    http = Mock()
    http.get.return_value.status_code = 404
    provider = HistoricalData(CandleCache(tmp_path / "cache"), http, pause=0)
    assert provider.hourly(symbol, START, START + 3600000) == []
    http.get.assert_called_once()
    assert http.get.call_args.args[0].startswith(
        "https://data.binance.vision/data/spot/"
    )


@pytest.mark.parametrize(
    "symbol", ["USDT", "../BTCUSDT", "BTC/USDT", "BTCUSDT?x=1", "btcUSDT", "BTC\nUSDT"]
)
def test_archive_identifier_rejects_path_or_query_injection(symbol):
    assert not valid_symbol(symbol)


def test_future_close_is_excluded_from_universe():
    rows = Synthetic().hourly("BTCUSDT", START - 524 * 3600000, START)
    candle, volume = rows[-2]
    rows[-2] = (replace(candle, close_time=START + 3600000), volume)
    universe = HistoricalUniverse({"BTCUSDT": rows}, Settings())
    assert universe.market("BTCUSDT", START) is None
    assert all(c.close_time < START for c, v in universe.as_of("BTCUSDT", START))


def test_future_data_cannot_change_past_decisions(tmp_path):
    class AlterFuture(Synthetic):
        def hourly(self, symbol, start, end):
            return [
                (
                    replace(
                        c,
                        open=c.open * 4,
                        high=c.high * 4,
                        low=c.low * 4,
                        close=c.close * 4,
                    ),
                    v * 1000,
                )
                if c.open_time >= START + 24 * 3600000
                else (c, v)
                for c, v in super().hourly(symbol, start, end)
            ]

        def minutes(self, symbol, start, end, interval="1m"):
            result = super().minutes(symbol, start, end, interval)
            if start >= START + 24 * 3600000:
                for candle in result.values():
                    for key in ("open", "high", "low", "close"):
                        candle[key] *= 4
            return result

    results = []
    for i, provider in enumerate((Synthetic(), AlterFuture())):
        db = ReplayStore(tmp_path / str(i) / "replay.db")
        run_id = db.create(config())
        ReplayEngine(db, provider).run(run_id)
        with db.connection() as sql:
            events = [
                json.loads(r[0])
                for r in sql.execute("SELECT data FROM replay_audit_events ORDER BY id")
            ]
        cutoff = at(START + 24 * 3600000).isoformat() + "Z"
        results.append([e for e in events if e["timestamp"] < cutoff])
    assert results[0] and results[0] == results[1]


def test_bounded_parallel_preload_preserves_deterministic_results(tmp_path):
    class Parallel(Synthetic):
        parallelism = 3

    results = []
    for i, provider in enumerate((Synthetic(), Parallel())):
        db = ReplayStore(tmp_path / str(i) / "replay.db")
        run_id = db.create(config(end="2025-01-02"))
        ReplayEngine(db, provider).run(run_id)
        results.append((db.data(run_id), db.get(run_id)["variants"][0]["summary"]))
    assert results[0] == results[1]


def test_restart_checkpoint_matches_uninterrupted_run(tmp_path):
    class SimulatedRestart(Exception):
        pass

    results = []
    for interrupted in (False, True):
        db = ReplayStore(tmp_path / str(interrupted) / "replay.db")
        run_id = db.create(config())

        def check():
            if (
                interrupted
                and db.get(run_id)["progress"].get("processed_timestamps", 0) >= 12
            ):
                raise SimulatedRestart()

        if interrupted:
            with pytest.raises(SimulatedRestart):
                ReplayEngine(db, Synthetic(), check).run(run_id)
            assert db.checkpoint(run_id)["next_time"] == START + 12 * 3600000
            db.recover()
            assert db.get(run_id)["status"] == "QUEUED"
        ReplayEngine(db, Synthetic()).run(run_id)
        trades, curve = db.data(run_id)
        with db.connection() as sql:
            fills = [
                json.loads(r[0])
                for r in sql.execute("SELECT data FROM replay_fills ORDER BY id")
            ]
        results.append((trades, curve, fills, db.get(run_id)["variants"][0]["summary"]))
    assert results[0] == results[1]


def test_worker_cancel_pause_resume_and_recovery(store, tmp_path):
    run_id = store.create(config())
    store.claim("test")
    worker = ReplayWorker(store, CandleCache(tmp_path / "cache"), Synthetic)
    worker.active = run_id
    store.control(run_id, "PAUSE")
    timer = threading.Timer(0.1, lambda: store.control(run_id, "RUN"))
    timer.start()
    worker.check()
    timer.join()
    assert store.get(run_id)["status"] == "RUNNING"
    store.control(run_id, "CANCEL")
    worker.last_control = 0
    with pytest.raises(Cancelled):
        worker.check()
    store.recover()
    assert store.get(run_id)["status"] == "CANCELLED"
    worker.stop.set()
    with pytest.raises(Stopping):
        worker.check()
    queued = store.create(config())
    store.control(queued, "CANCEL")
    assert store.get(queued)["status"] == "CANCELLED"
    assert store.claim("none") is None


def test_worker_once_uses_only_replay_storage(store, tmp_path):
    run_id = store.create(config(end="2025-01-02"))
    worker = ReplayWorker(store, CandleCache(tmp_path / "cache"), Synthetic)
    worker.run(once=True)
    assert store.get(run_id)["status"] == "COMPLETED"
    assert store.worker()["status"] == "STOPPED"


@pytest.mark.parametrize(
    "policy,params,reason",
    [
        ("fixed_tp", {"tp_pct": "1"}, "FIXED_TP"),
        ("r_tp", {"tp_r": "0.5"}, "R_TP"),
        ("break_even", {"activation_r": ".5", "buffer_pct": "0"}, "BREAK_EVEN"),
        ("trailing_pct", {"activation_pct": "1", "distance_pct": ".5"}, "TRAILING"),
        ("trailing_r", {"activation_r": ".5", "distance_r": ".5"}, "R_TRAILING"),
        ("profit_lock", {"activation_pct": "1", "lock_pct": ".2"}, "PROFIT_LOCK"),
    ],
)
def test_overlay_exits_without_original_stop(policy, params, reason):
    v = Variant(
        config(variants=[dict(exit_policy=policy, exit_parameters=params)])["variants"][
            0
        ],
        START,
    )
    v.signals([row()], {"BTCUSDT"}, START)
    candle = dict(
        time=START,
        close_time=START + 59999,
        open=100,
        high=104,
        low=99,
        close=99,
        interval="1m",
    )
    v.execute_signals({"BTCUSDT": {START: candle}}, START, False)
    v.bar("BTCUSDT", candle, START + 60000, True)
    assert v.closed[0]["exit_reason"] == reason
    assert D(v.closed[0]["fees"]) > 0
    assert v.ledger.account.reserved_cash == 0
    if policy not in ("fixed_tp", "r_tp"):
        assert D(v.closed[0]["stop_price"]) > D(v.closed[0]["original_stop_price"])
        fill = v.execution.fills[-1]
        assert D(fill["reference_price"]) == D(v.closed[0]["stop_price"])


def test_strategy_exit_links_and_duplicate_protection():
    v = Variant(config()["variants"][0], START)
    price = {"BTCUSDT": {START: dict(open=100, interval="1m")}}
    v.signals([row()], {"BTCUSDT"}, START)
    v.execute_signals(price, START, False)
    v.signals([row()], {"BTCUSDT"}, START)
    v.execute_signals(price, START, False)
    assert len(v.ledger.portfolio.positions()) == 1
    v.signals([row() | dict(total_score=30)], {"BTCUSDT"}, START)
    v.execute_signals(price, START, False)
    assert not v.ledger.portfolio.positions()
    assert v.closed[0]["exit_reason"] == "STRATEGY"
    assert all(e.get("position_id") == 1 for e in v.events)
    assert any(
        "below exit threshold" in " ".join(e.get("reasons", [])) for e in v.events
    )


def test_execution_open_does_not_read_future_minute_extremes():
    accounts = []
    for high, low in ((101, 99), (900, 1)):
        v = Variant(config()["variants"][0], START)
        v.signals([row()], {"BTCUSDT"}, START)
        candle = dict(open=100, high=high, low=low, interval="1m")
        v.execute_signals({"BTCUSDT": {START: candle}}, START, False)
        accounts.append((v.ledger.portfolio.view(), deepcopy(v.execution.fills)))
    assert accounts[0] == accounts[1]


def test_daily_loss_anchor_rolls_even_when_flat():
    v = Variant(config()["variants"][0], START)
    v.ledger.account.cash_balance = D(970)
    v.signals([row()], {"BTCUSDT"}, START)
    v.execute_signals({"BTCUSDT": {START: dict(open=100, interval="1m")}}, START, False)
    assert v.blocked["daily_loss"] == 1
    tomorrow = START + 24 * 3600000
    v.signals([row()], {"BTCUSDT"}, tomorrow)
    v.execute_signals(
        {"BTCUSDT": {tomorrow: dict(open=100, interval="1m")}}, tomorrow, False
    )
    assert len(v.ledger.portfolio.positions()) == 1
    assert v.ledger.account.day_start_equity == D(970)


def test_missing_minutes_default_and_engine_fallback(store):
    class MissingMinutes(Synthetic):
        def minutes(self, symbol, start, end, interval="1m"):
            if interval == "1m":
                return {}
            return super().minutes(symbol, start, end, interval)

    run_id = store.create(config(end="2025-01-02"))
    ReplayEngine(store, MissingMinutes()).run(run_id)
    assert store.data(run_id)[0] == []
    with store.connection() as sql:
        assert (
            sql.execute(
                "SELECT count(*) FROM replay_audit_events WHERE json_extract(data,'$.event')='NO_FILL'"
            ).fetchone()[0]
            > 0
        )
    fallback = store.create(config(end="2025-01-02", fallback_5m=True))
    ReplayEngine(store, MissingMinutes()).run(fallback)
    assert store.data(fallback)[0]
    with store.connection() as sql:
        fills = [
            json.loads(r[0])
            for r in sql.execute(
                "SELECT data FROM replay_fills WHERE run_id=?", (fallback,)
            )
        ]
    assert fills and all(f["source_timeframe"] == "5m" for f in fills)


def test_database_step_rolls_back_results_and_checkpoint(store):
    run_id = store.create(config())
    good = dict(position_id=1, symbol="BTCUSDT", status="OPEN")
    with pytest.raises(ValueError):
        store.save_step(
            run_id,
            [
                (0, [good], [], [], [], []),
                (1, [], [{"invalid": float("nan")}], [], [], []),
            ],
            {},
            {"next_time": START},
        )
    assert store.data(run_id)[0] == []
    assert store.checkpoint(run_id) is None


def test_exports_all_closed_trades_without_raw_candles(completed):
    db, run_id = completed
    trades, _ = db.data(run_id)
    closed = [t for t in trades if t["status"] == "CLOSED"]
    full = payload(db, run_id)
    compact = payload(db, run_id, compact=True)
    assert len(full["trades"]) == len(compact["trades"]) == len(closed) > 0
    assert "equity_curve" in full and "equity_curve" not in compact
    assert "candles" not in compact and "orders" not in compact
    assert compact["metadata"]["not_statistically_validated"]
    assert compact["config"]["score_version"] == "heuristic-v1"
    rows = list(csv.DictReader(io.StringIO(csv_export(db, run_id).lstrip("\ufeff"))))
    assert len(rows) == len(closed)
    assert rows[0]["entry_fill_price"] == closed[0]["entry_fill_price"]
    with zipfile.ZipFile(io.BytesIO(bundle(db, run_id))) as archive:
        assert set(archive.namelist()) == {
            "analysis.json",
            "compact-analysis.json",
            "trades.csv",
            "audit.json",
            "README.txt",
        }
        assert json.loads(archive.read("analysis.json"))["trades"] == closed


def test_refuses_production_file_and_hardlink(tmp_path):
    import os

    production = tmp_path / "market.db"
    with sqlite3.connect(production) as sql:
        sql.execute("CREATE TABLE scanner_runs(id INTEGER)")
    os.link(production, tmp_path / "replay.db")
    with pytest.raises(ValueError, match="separate"):
        isolated_paths(tmp_path)
    with pytest.raises(ValueError, match="Production"):
        ReplayStore(tmp_path / "replay.db")
    with sqlite3.connect(production) as sql:
        assert sql.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("scanner_runs",)]


@pytest.fixture
def web_case(tmp_path, monkeypatch):
    root = tmp_path / "replay"
    monkeypatch.setenv("REPLAY_DATA_DIR", str(root))
    database = Database(f"sqlite:///{tmp_path}/market.db")
    database.initialize()
    from crypto_bot.storage.migrate import upgrade_database

    upgrade_database(database)
    app = create_app(
        Settings(database_url=str(database.engine.url), secret_key="test-only"),
        database,
    )
    app.config["TESTING"] = True
    client = app.test_client()
    yield client, ReplayStore(root / "replay.db"), database
    app.extensions["paper_analysis"].close()
    app.extensions["web_heartbeat"].close()
    database.engine.dispose()


def token(client):
    client.get("/replay")
    with client.session_transaction() as session:
        return {"X-CSRF-Token": session["csrf"]}


def test_web_create_progress_control_presets_and_isolation(web_case):
    client, db, production = web_case
    headers = token(client)
    with production.engine.connect() as sql:
        original_tables = sql.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    raw = dict(start="2025-01-01", end="2025-01-02", universe_size=10)
    assert client.post("/api/replay/runs", json=raw).status_code == 403
    r = client.post("/api/replay/runs", json=raw, headers=headers)
    assert r.status_code == 202
    run_id = r.json["run_id"]
    assert client.get(f"/api/replay/runs/{run_id}").json["status"] == "QUEUED"
    assert client.get(f"/replay/runs/{run_id}").status_code == 200
    assert client.get(f"/api/replay/runs/{run_id}/export.csv").status_code == 409
    assert (
        client.post(
            "/api/replay/presets",
            json=dict(name="Fixed preset", config=raw),
            headers=headers,
        ).status_code
        == 200
    )
    preset = db.presets()[0]["config"]
    cloned = client.post(f"/api/replay/runs/{run_id}/clone", headers=headers).json[
        "run_id"
    ]
    assert db.get(cloned)["config"] == preset == db.get(run_id)["config"]
    assert (
        client.post(f"/api/replay/runs/{run_id}/cancel", headers=headers).status_code
        == 200
    )
    assert db.get(run_id)["status"] == "CANCELLED"
    assert (
        client.post(f"/api/replay/runs/{run_id}/resume", headers=headers).status_code
        == 409
    )
    assert client.get("/api/replay/runs/missing").status_code == 404
    assert (
        client.post(
            "/api/replay/runs", json=raw | dict(live=True), headers=headers
        ).status_code
        == 400
    )
    with production.engine.connect() as sql:
        assert (
            sql.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            == original_tables
        )
        assert sql.exec_driver_sql("SELECT count(*) FROM paper_orders").scalar() == 0


@pytest.mark.parametrize(
    "variant",
    [
        {"strategy_parameters": {"min_score": ""}},
        {"risk_parameters": {"initial_paper_balance": "not-a-number"}},
        {"exit_policy": "fixed_tp", "exit_parameters": {"tp_pct": "bad"}},
        {"strategy_parameters": {"min_delta": "NaN"}},
    ],
)
def test_invalid_numeric_web_config_returns_400(web_case, variant):
    client, _, _ = web_case
    raw = dict(start="2025-01-01", end="2025-01-02", variants=[variant])
    assert (
        client.post("/api/replay/runs", json=raw, headers=token(client)).status_code
        == 400
    )


def test_web_results_trade_chart_exports_and_comparison(web_case):
    client, db, production = web_case
    run_id = db.create(config(variants=[{}, dict(exit_policy="fixed_tp")]))
    ReplayEngine(db, Synthetic()).run(run_id)
    result = client.get(f"/api/replay/runs/{run_id}/results")
    assert result.status_code == 200 and result.json["trade_count"] > 0
    t = result.json["trades"][0]
    url = f"/replay/runs/{run_id}/trades/{t['position_id']}"
    assert client.get(url).status_code == 200
    chart = client.get("/api" + url + "/chart")
    assert chart.status_code == 200 and chart.json["candles"] == []
    assert chart.json["source"] == "REPLAY_LOCAL_CACHE_VISUALIZATION_ONLY"
    assert client.get("/api" + url + "/chart?interval=evil").status_code == 400
    for extension in ("json", "compact.json", "csv", "zip"):
        export = client.get(f"/api/replay/runs/{run_id}/export.{extension}")
        assert (
            export.status_code == 200
            and "attachment" in export.headers["Content-Disposition"]
        )
    compare = client.get(
        f"/api/replay/compare?selection={run_id}:0&selection={run_id}:1"
    )
    assert compare.status_code == 200 and compare.json["periods_match"]
    assert compare.json["not_statistically_validated"]
    assert (
        client.get(
            f"/api/replay/compare?selection={run_id}:0&selection={run_id}:0"
        ).status_code
        == 400
    )
    for route in ("/replay", "/replay/compare", "/replay/presets"):
        assert client.get(route).status_code == 200
    assert len(db.audit(run_id, 0, t["position_id"])) >= 3
