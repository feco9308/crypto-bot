from datetime import timedelta

import pytest
from conftest import NOW
from sqlalchemy import func, select
from test_paper_core import seed

from crypto_bot.monitoring.config import MonitorSettings
from crypto_bot.monitoring.service import Monitoring
from crypto_bot.storage.migrate import upgrade_database
from crypto_bot.storage.models import ScannerRun
from crypto_bot.storage.paper_models import PaperPosition, SignalRecord, SystemEvent
from crypto_bot.trading.config import PaperSettings
from crypto_bot.trading.engine import PaperEngine
from crypto_bot.trading.repository import PaperRepository
from crypto_bot.web.app import create_app


@pytest.fixture
def setup(database, settings):
    upgrade_database(database)
    cfg = PaperSettings()
    repo = PaperRepository(database, cfg)
    repo.initialize_account(NOW)
    return repo, PaperEngine(repo, settings), Monitoring(database, MonitorSettings())


def test_heartbeat_status_stale_errors_and_stop(setup, database, settings):
    _, _, monitor = setup
    seed(database)
    monitor.start("Paper Trading Engine", "owner", NOW)
    monitor.heartbeat("Paper Trading Engine", "owner", NOW + timedelta(seconds=30))
    monitor.result(
        "Paper Trading Engine",
        "DEGRADED",
        "No fresh price",
        NOW + timedelta(seconds=31),
    )
    response = monitor.status(settings, NOW + timedelta(seconds=35))
    engine = next(
        c for c in response["components"] if c["name"] == "Paper Trading Engine"
    )
    assert (
        engine["status"] == "DEGRADED"
        and engine["last_success"]
        and engine["last_error_message"] == "No fresh price"
    )
    assert engine["type"] == "service"
    assert (
        next(c for c in response["components"] if c["name"] == "Risk Manager")["type"]
        == "component"
    )
    response = monitor.status(settings, NOW + timedelta(seconds=151))
    assert (
        next(c for c in response["components"] if c["name"] == "Paper Trading Engine")[
            "status"
        ]
        == "STALE"
    )
    assert not response["healthy"]
    monitor.stop("Paper Trading Engine", "owner")
    assert (
        next(
            c
            for c in monitor.status(settings, NOW)["components"]
            if c["name"] == "Paper Trading Engine"
        )["status"]
        == "STOPPED"
    )


def test_legacy_scanner_inference_and_partial_warning(setup, database, settings):
    monitor = setup[2]
    seed(database)
    with database.session.begin() as session:
        run = session.scalar(select(ScannerRun))
        run.completed_at = NOW
        run.errors = {"TEST": "warmup missing"}
        run.status = "partial"
    response = monitor.status(settings, NOW)
    scanner = next(c for c in response["components"] if c["name"] == "Market Scanner")
    assert scanner["status"] == "DEGRADED" and scanner["last_heartbeat"] is None
    assert "inferred" in scanner["observation"]
    response = monitor.status(settings, NOW + timedelta(minutes=11))
    assert (
        next(c for c in response["components"] if c["name"] == "Market Scanner")[
            "status"
        ]
        == "STALE"
    )


def test_event_retention_bounded(setup, database):
    monitor = Monitoring(database, MonitorSettings(event_retention_count=3))
    for i in range(10):
        monitor.event("Test", str(i), now=NOW)
    with database.session() as session:
        assert session.scalar(select(func.count(SystemEvent.id))) == 3
        assert [
            e.message
            for e in session.scalars(select(SystemEvent).order_by(SystemEvent.id))
        ] == ["7", "8", "9"]


def test_database_health_error_is_sanitized(setup, settings):
    monitor = setup[2]
    from sqlalchemy.exc import OperationalError

    class BrokenDatabase:
        def session(self):
            raise OperationalError("secret password hidden", None, Exception("secret"))

    monitor.database = BrokenDatabase()
    response = monitor.status(settings, NOW)
    assert not response["healthy"]
    assert "secret" not in str(response)
    assert response["components"][0]["connection"] == "ERROR"


def test_paper_web_control_manual_close_and_audit(setup, database, settings):
    repo, engine, monitor = setup
    from crypto_bot.storage.database import utcnow

    now = utcnow()
    seed(database, now)
    app = create_app(settings, database)
    client = app.test_client()
    try:
        for path in ["/", "/paper", "/services", "/health", "/api/system/status"]:
            assert client.get(path).status_code == 200
        response = client.get("/api/system/status").json
        assert (
            response["version"] == "0.2.0"
            and response["migration_version"] == "0002_paper"
        )
        assert not response["trading_enabled"]
        assert client.post("/paper/control", data={"enabled": "ON"}).status_code == 403
        client.get("/paper")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        assert (
            client.post(
                "/paper/control", data={"csrf": csrf, "enabled": "LIVE"}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/paper/control", data={"csrf": csrf, "enabled": "ON"}
            ).status_code
            == 302
        )
        engine.run_once(now)
        with database.session() as session:
            pid = session.scalar(select(PaperPosition.id))
            sid = session.scalar(select(SignalRecord.id))
        assert client.get(f"/paper/signals/{sid}").status_code == 200
        assert (
            client.post(
                "/paper/control", data={"csrf": csrf, "enabled": "OFF"}
            ).status_code
            == 302
        )
        assert (
            client.post(
                f"/paper/positions/{pid}/close", data={"csrf": csrf}
            ).status_code
            == 302
        )
        with database.session() as session:
            assert session.get(PaperPosition, pid).exit_reason == "MANUAL"
        assert client.get("/paper").status_code == 200
        monitor.start("Paper Trading Engine", "test", now - timedelta(hours=1))
        assert client.get("/health").status_code == 503
        assert b"SYSTEM WARNING" in client.get("/").data
    finally:
        app.extensions["web_heartbeat"].close()


def test_no_migration_ui_keeps_scanner_usable(database, settings):
    app = create_app(settings, database)
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/paper").status_code == 200
    assert client.get("/services").status_code == 200
    assert client.get("/health").status_code == 200


def test_transaction_failure_rolls_back_audit_and_cash(setup, database, settings):
    repo, _, _ = setup
    seed(database)
    repo.set_enabled(True)
    from crypto_bot.execution.paper import PaperExecutionService

    class FailingExecution(PaperExecutionService):
        def place_order(self, *args):
            super().place_order(*args)
            raise RuntimeError("simulated crash before commit")

    engine = PaperEngine(repo, settings, execution_factory=FailingExecution)
    with pytest.raises(RuntimeError):
        engine.run_once(NOW)
    assert repo.status()["cash_balance"] == "1000.000000000000"
    assert repo.status()["open_positions"] == 0
    with database.session() as session:
        assert session.scalar(select(func.count(SignalRecord.id))) == 0


@pytest.mark.parametrize(
    "kwargs",
    [{"heartbeat_interval": 0}, {"stale_threshold": 60}, {"event_retention_count": 0}],
)
def test_invalid_monitor_config(kwargs):
    with pytest.raises(ValueError):
        MonitorSettings(**kwargs)
