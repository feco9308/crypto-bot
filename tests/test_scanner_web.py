from datetime import timedelta

import pytest
from conftest import NOW, FakeProvider, candles, market
from sqlalchemy import select

from crypto_bot.scanner.scanner import Scanner
from crypto_bot.storage.database import Database
from crypto_bot.storage.models import OverrideEvent
from crypto_bot.web.app import create_app


def test_full_scan_persistence_and_chart(settings, database):
    provider = FakeProvider()
    rows = Scanner(provider, database, settings).run_once(NOW)
    assert len(rows) == 2
    run = database.latest_run("1h")
    assert run["status"] == "complete" and run["scored_count"] == 2
    latest = database.latest_rows("1h")
    assert latest[0]["algorithm_watch"]
    assert len(database.history("BTCUSDT", "1h")) == 1
    app = create_app(settings, database)
    client = app.test_client()
    for path in [
        "/",
        "/instrument/BTCUSDT",
        "/api/markets",
        "/api/history/BTCUSDT",
        "/static/dashboard.js",
    ]:
        assert client.get(path).status_code == 200
    assert b"canvas" in client.get("/instrument/BTCUSDT").data
    assert (
        client.get("/api/markets").json["markets"][0]["features"]["price"]
        == rows[0]["features"]["price"]
    )
    assert len(provider.calls) == 2  # web traffic never invokes provider
    assert client.get("/instrument/UNKNOWN").status_code == 404
    assert client.get("/api/history/UNKNOWN").status_code == 404
    assert database.history("BTCUSDT", "5m") == []


def test_partial_failure_and_run_failure(settings, database):
    provider = FakeProvider(fail={"ETHUSDT"})
    assert len(Scanner(provider, database, settings).run_once(NOW)) == 1
    assert database.latest_run("1h")["status"] == "partial"
    assert "ETHUSDT" in database.latest_run("1h")["errors"]
    provider.fail = {"BTCUSDT", "ETHUSDT"}
    assert (
        Scanner(provider, database, settings).run_once(NOW + timedelta(hours=1)) == []
    )
    assert database.latest_run("1h")["status"] == "failed"
    assert len(database.history("BTCUSDT", "1h")) == 1
    provider.markets = lambda: (_ for _ in ()).throw(RuntimeError("Discovery failed"))
    with pytest.raises(RuntimeError):
        Scanner(provider, database, settings).run_once(NOW + timedelta(hours=2))
    assert database.latest_run("1h")["errors"]["run"] == "Discovery failed"


def test_override_web_csrf_and_restart(tmp_path):
    from crypto_bot.config.settings import Settings

    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        auto_min_score=0,
        secret_key="test-stable-key",
    )
    db = Database(settings.database_url)
    db.initialize()
    Scanner(FakeProvider(), db, settings).run_once(NOW)
    client = create_app(settings, db).test_client()
    assert (
        client.post(
            "/override", data=dict(symbol="BTCUSDT", status="IGNORE")
        ).status_code
        == 403
    )
    client.get("/")
    with client.session_transaction() as session:
        csrf = session["csrf"]

    def post(status, symbol="BTCUSDT"):
        return client.post(
            "/override", data=dict(csrf=csrf, symbol=symbol, status=status)
        )

    assert post("IGNORE").status_code == 302
    row = {r["symbol"]: r for r in db.latest_rows("1h")}["BTCUSDT"]
    assert row["algorithm_watch"] and row["effective_state"] == "IGNORE"
    assert post("EXECUTE").status_code == 400
    assert post("WATCH", "UNKNOWN").status_code == 404
    assert post("PINNED").status_code == 302
    db.engine.dispose()
    reopened = Database(settings.database_url)
    reopened.initialize()
    assert reopened.overrides()["BTCUSDT"] == "PINNED"
    with reopened.session() as session:
        events = list(session.scalars(select(OverrideEvent)))
        assert [(e.previous, e.current) for e in events] == [
            ("AUTO", "IGNORE"),
            ("IGNORE", "PINNED"),
        ]
    reopened.engine.dispose()


def test_manual_market_outside_universe(settings, database):
    settings.number_of_markets = 1
    provider = FakeProvider(
        markets=[
            market(),
            market("ETHUSDT", 1, "ETH"),
            market("BTCUSDC", 1000, "BTC", "USDC"),
        ]
    )
    scanner = Scanner(provider, database, settings)
    scanner.run_once(NOW)
    database.set_override("BTCUSDC", "WATCH")
    provider.calls = []
    provider.timestamp = NOW + timedelta(hours=1)
    scanner.run_once(provider.timestamp)
    assert provider.calls == ["BTCUSDT", "BTCUSDC"]
    row = {r["symbol"]: r for r in database.latest_rows("1h")}["BTCUSDC"]
    assert not row["algorithm_watch"] and row["effective_state"] == "WATCH"
    provider.fail = {"BTCUSDC"}
    provider.timestamp += timedelta(hours=1)
    scanner.run_once(provider.timestamp)
    row = {r["symbol"]: r for r in database.latest_rows("1h")}["BTCUSDC"]
    assert row["stale"] and row["effective_state"] == "WATCH"


def test_momentum_config_changes_and_open_candle(settings, database):
    provider = FakeProvider()
    scanner = Scanner(provider, database, settings)
    scanner.run_once(NOW)
    provider.timestamp = NOW + timedelta(hours=1)
    rows = scanner.run_once(provider.timestamp)
    assert rows[0]["momentum"]["delta_1h"] == 0
    settings.weights["trend"] = 30
    provider.timestamp += timedelta(hours=1)
    rows = scanner.run_once(provider.timestamp)
    assert rows[0]["momentum"]["delta_1h"] is None
    base = provider.candles

    def with_open(*args):
        from dataclasses import replace

        result = base(*args)
        result.append(
            replace(
                result[-1],
                open_time=result[-1].open_time + 3600000,
                close_time=result[-1].close_time + 3600000,
                close=1e9,
            )
        )
        return result

    provider.candles = with_open
    rows = scanner.run_once(provider.timestamp)
    assert rows[0]["features"]["price"] < 200


def test_stale_candle_rejected(settings, database):
    provider = FakeProvider(timestamp=NOW - timedelta(hours=3))
    assert Scanner(provider, database, settings).run_once(NOW) == []
    assert "Stale" in database.latest_run("1h")["errors"]["BTCUSDT"]


def test_pinned_without_history_and_failed_freshness(settings, database):
    settings.number_of_markets = 1
    provider = FakeProvider()
    scanner = Scanner(provider, database, settings)
    scanner.run_once(NOW)
    database.set_override("ETHUSDT", "PINNED")
    row = {r["symbol"]: r for r in database.latest_rows("1h")}["ETHUSDT"]
    assert row["total_score"] is None and row["effective_state"] == "PINNED"
    client = create_app(settings, database).test_client()
    assert client.get("/").status_code == 200
    assert client.get("/instrument/ETHUSDT").status_code == 200
    provider.fail = {"ETHUSDT", "BTCUSDT"}
    scanner.run_once(NOW + timedelta(hours=1))
    assert all(r["stale"] for r in database.latest_rows("1h"))
    assert client.get("/").status_code == 200


def test_jup_is_not_a_leveraged_token(settings):
    from crypto_bot.scanner.selection import eligible

    assert eligible(market("JUPUSDT", base="JUP"), settings)


def test_nonfinite_manual_statistics_rejected(settings):
    from dataclasses import replace

    from crypto_bot.indicators.technical import features

    with pytest.raises(ValueError, match="statistics"):
        features(candles(), replace(market(), price_change_pct=float("nan")), settings)


@pytest.mark.parametrize(
    "timeframe,step",
    [
        ("5m", 300000),
        ("15m", 900000),
        ("1h", 3600000),
        ("4h", 14400000),
        ("1d", 86400000),
    ],
)
def test_all_supported_timeframes(settings, database, timeframe, step):
    from dataclasses import replace
    from datetime import timezone

    settings.timeframe = timeframe
    provider = FakeProvider()

    def fetch(symbol, tf, limit):
        end = int(NOW.replace(tzinfo=timezone.utc).timestamp() * 1000)
        source = candles()
        return [
            replace(
                c,
                open_time=end - (len(source) - i) * step,
                close_time=end - (len(source) - i - 1) * step - 1,
            )
            for i, c in enumerate(source)
        ]

    provider.candles = fetch
    rows = Scanner(provider, database, settings).run_once(NOW)
    assert len(rows) == 2
    assert database.latest_run(timeframe)["status"] == "complete"
