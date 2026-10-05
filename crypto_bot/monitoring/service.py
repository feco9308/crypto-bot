"""Observer only: operational metadata cannot alter strategy/risk/bookkeeping."""

import logging
import subprocess
import threading
import uuid
from functools import lru_cache
from pathlib import Path

from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import SQLAlchemyError

from crypto_bot.storage.database import iso_utc, utcnow
from crypto_bot.storage.models import ScannerRun, SchemaVersion
from crypto_bot.storage.paper_models import (
    PaperAccount,
    PortfolioSnapshot,
    ServiceStatus,
    SystemEvent,
)
from crypto_bot.version import __version__

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def build_info():
    root = Path(__file__).resolve().parents[2]

    def git(*args):
        try:
            return (
                subprocess.check_output(
                    ["git", "-C", str(root), *args],
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    text=True,
                ).strip()
                or None
            )
        except (OSError, subprocess.SubprocessError):
            return None

    return dict(
        version=__version__,
        git_commit=git("rev-parse", "HEAD"),
        git_branch=git("branch", "--show-current"),
    )


class Monitoring:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings

    def available(self):
        return inspect(self.database.engine).has_table("service_status")

    def event(self, component, message, level="INFO", now=None):
        if not self.available():
            return
        with self.database.session.begin() as session:
            session.add(
                SystemEvent(
                    timestamp=now or utcnow(),
                    component=component,
                    level=level,
                    message=str(message)[:300],
                )
            )
            session.flush()
            cutoff = session.scalar(
                select(SystemEvent.id)
                .order_by(SystemEvent.id.desc())
                .offset(self.settings.event_retention_count)
                .limit(1)
            )
            if cutoff is not None:
                session.execute(delete(SystemEvent).where(SystemEvent.id <= cutoff))

    def start(self, name, owner, now=None):
        if not self.available():
            return
        now = now or utcnow()
        with self.database.session.begin() as session:
            row = session.get(ServiceStatus, name)
            if row is None:
                row = ServiceStatus(
                    name=name,
                    type="service",
                    status="RUNNING",
                    started_at=now,
                    restart_count=0,
                    **build_info(),
                )
                session.add(row)
            else:
                if name != "Web Dashboard":
                    row.restart_count += 1
                row.started_at = now
                for key, value in build_info().items():
                    setattr(row, key, value)
            row.owner, row.status, row.last_heartbeat = owner, "RUNNING", now

    def heartbeat(self, name, owner, now=None):
        with self.database.session.begin() as session:
            row = session.get(ServiceStatus, name)
            if row and (row.owner == owner or name == "Web Dashboard"):
                row.last_heartbeat = now or utcnow()

    def result(self, name, status="RUNNING", error=None, now=None):
        now = now or utcnow()
        with self.database.session.begin() as session:
            row = session.get(ServiceStatus, name)
            if row is None:
                return
            row.status = status
            if error:
                row.last_error_at, row.last_error_message = now, str(error)[:300]
            if status in {"RUNNING", "DEGRADED", "PAUSED"}:
                row.last_success = now
        self.event(
            name, error or "Cycle completed", "WARNING" if error else "INFO", now
        )

    def stop(self, name, owner):
        if name == "Web Dashboard":
            return  # Aggregate worker heartbeat expires after the last worker exits.
        with self.database.session.begin() as session:
            row = session.get(ServiceStatus, name)
            if row and (row.owner == owner or name == "Web Dashboard"):
                row.status = "STOPPED"

    def status(self, scanner_settings, now=None):
        now = now or utcnow()
        info = build_info()
        statuses = []

        def component(name, status="READY", **details):
            return dict(
                name=name,
                type="component",
                status=status,
                **info,
                started_at=None,
                last_heartbeat=None,
                last_success=None,
                last_error_at=None,
                last_error_message=None,
                restart_count=None,
                **details,
            )

        try:
            with self.database.session() as session:
                session.execute(select(SchemaVersion.version).limit(1))
                installed = self.available()
                records = (
                    list(session.scalars(select(ServiceStatus))) if installed else []
                )
                for row in records:
                    status = row.status
                    if status != "STOPPED" and (
                        row.last_heartbeat is None
                        or (now - row.last_heartbeat).total_seconds()
                        > self.settings.stale_threshold
                    ):
                        status = "STALE"
                    statuses.append(
                        {
                            key: iso_utc(getattr(row, key))
                            if key
                            in {
                                "started_at",
                                "last_heartbeat",
                                "last_success",
                                "last_error_at",
                            }
                            else getattr(row, key)
                            for key in [
                                "name",
                                "type",
                                "version",
                                "git_commit",
                                "git_branch",
                                "started_at",
                                "last_heartbeat",
                                "last_success",
                                "last_error_at",
                                "last_error_message",
                                "restart_count",
                            ]
                        }
                        | dict(status=status)
                    )
                for entry in statuses:
                    if entry["name"] == "Web Dashboard":
                        entry["restart_count"] = None
                known = {s["name"] for s in statuses}
                if "Market Scanner" not in known:
                    run = session.scalar(
                        select(ScannerRun)
                        .where(ScannerRun.timeframe == scanner_settings.timeframe)
                        .order_by(ScannerRun.id.desc())
                        .limit(1)
                    )
                    success = session.scalar(
                        select(ScannerRun)
                        .where(
                            ScannerRun.timeframe == scanner_settings.timeframe,
                            ScannerRun.status.in_(["complete", "partial"]),
                        )
                        .order_by(ScannerRun.id.desc())
                        .limit(1)
                    )
                    inferred = run.completed_at or run.timestamp if run else None
                    status = (
                        "STOPPED"
                        if run is None
                        else "STALE"
                        if (now - inferred).total_seconds()
                        > max(
                            scanner_settings.scanner_interval * 2,
                            self.settings.stale_threshold,
                        )
                        else "ERROR"
                        if run.status == "failed"
                        else "DEGRADED"
                        if run.errors
                        else "RUNNING"
                    )
                    error = (
                        "; ".join(f"{k}: {v}" for k, v in run.errors.items())[:300]
                        if run and run.errors
                        else None
                    )
                    statuses.append(
                        dict(
                            name="Market Scanner",
                            type="service",
                            status=status,
                            version=None,
                            git_commit=None,
                            git_branch=None,
                            started_at=None,
                            last_heartbeat=None,
                            last_success=iso_utc(
                                success.completed_at if success else None
                            ),
                            last_error_at=iso_utc(run.completed_at) if error else None,
                            last_error_message=error,
                            restart_count=None,
                            observation="Legacy: status inferred from scanner runs; no process heartbeat",
                        )
                    )
                if "Web Dashboard" not in known:
                    statuses.append(
                        component(
                            "Web Dashboard",
                            "READY",
                            observation="HTTP request handled; heartbeat not yet installed",
                        )
                    )
                account = session.get(PaperAccount, 1) if installed else None
                trading = bool(account and account.enabled)
                if "Paper Trading Engine" not in known:
                    statuses.append(
                        dict(
                            component("Paper Trading Engine", "STOPPED"), type="service"
                        )
                    )
                for name in ["Strategy Engine", "Risk Manager", "Portfolio / Wallet"]:
                    statuses.append(
                        component(
                            name,
                            "READY" if account else "PAUSED",
                            observation="Internal module; not a separate process",
                        )
                    )
                from alembic.runtime.migration import MigrationContext

                revision = (
                    MigrationContext.configure(
                        session.connection()
                    ).get_current_revision()
                    if installed
                    else None
                )
                portfolio_write = (
                    session.scalar(
                        select(PortfolioSnapshot.timestamp)
                        .order_by(PortfolioSnapshot.id.desc())
                        .limit(1)
                    )
                    if installed
                    else None
                )
                scanner_write = session.scalar(
                    select(ScannerRun.completed_at)
                    .where(ScannerRun.completed_at.is_not(None))
                    .order_by(ScannerRun.id.desc())
                    .limit(1)
                )
                writes = [t for t in [portfolio_write, scanner_write] if t]
                url = self.database.engine.url
                statuses.append(
                    component(
                        "Database",
                        "READY",
                        connection="OK",
                        database_type=url.get_backend_name(),
                        database_name=url.database,
                        schema_version=session.get(SchemaVersion, 1).version
                        if session.get(SchemaVersion, 1)
                        else None,
                        migration_version=revision,
                        last_successful_write=iso_utc(max(writes)) if writes else None,
                    )
                )
                events = (
                    [
                        dict(
                            timestamp=iso_utc(e.timestamp),
                            component=e.component,
                            level=e.level,
                            message=e.message,
                        )
                        for e in session.scalars(
                            select(SystemEvent)
                            .order_by(SystemEvent.id.desc())
                            .limit(30)
                        )
                    ]
                    if installed
                    else []
                )
        except SQLAlchemyError:
            statuses = [component("Database", "ERROR", connection="ERROR")]
            statuses[0]["last_error_message"] = "Database operation failed"
            trading = False
            revision = None
            events = []
        summary = {
            status: sum(s["status"] == status for s in statuses)
            for status in [
                "RUNNING",
                "READY",
                "PAUSED",
                "DEGRADED",
                "ERROR",
                "STALE",
                "STOPPED",
            ]
        }
        healthy = not any(s["status"] in {"ERROR", "STALE"} for s in statuses)
        return dict(
            **info,
            migration_version=revision,
            components=statuses,
            summary=summary,
            healthy=healthy,
            trading_enabled=trading,
            events=events,
        )


class Heartbeat:
    def __init__(self, monitor, name):
        self.monitor, self.name = monitor, name
        self.owner = str(uuid.uuid4())
        self.stop_event = threading.Event()
        self.thread = None
        self.enabled = False

    def start(self):
        try:
            if not self.monitor.available():
                return self
            self.monitor.start(self.name, self.owner)
            self.enabled = True

            def pulse():
                while not self.stop_event.wait(
                    self.monitor.settings.heartbeat_interval
                ):
                    try:
                        self.monitor.heartbeat(self.name, self.owner)
                    except Exception:
                        log.warning(
                            "HEARTBEAT_WRITE_FAILED",
                            extra={"event_data": {"service": self.name}},
                        )

            self.thread = threading.Thread(
                target=pulse, daemon=True, name=f"heartbeat-{self.name}"
            )
            self.thread.start()
        except Exception:
            log.warning(
                "MONITORING_UNAVAILABLE", extra={"event_data": {"service": self.name}}
            )
        return self

    def result(self, status="RUNNING", error=None):
        if self.enabled:
            try:
                self.monitor.result(self.name, status, error)
            except Exception:
                log.warning("MONITORING_WRITE_FAILED")

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.enabled:
            try:
                self.monitor.stop(self.name, self.owner)
            except Exception:
                log.warning("MONITORING_STOP_FAILED")
