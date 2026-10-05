"""Paper and operational UI additions; existing scanner routes stay intact."""

import hmac
import os

from flask import abort, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import select

from crypto_bot.monitoring.config import MonitorSettings
from crypto_bot.monitoring.service import Heartbeat, Monitoring
from crypto_bot.storage.models import Snapshot
from crypto_bot.storage.paper_models import (
    PaperFill,
    PaperOrder,
    PaperPosition,
    RiskRecord,
    SignalRecord,
)
from crypto_bot.trading.config import PaperSettings
from crypto_bot.trading.engine import PaperEngine
from crypto_bot.trading.repository import PaperRepository


def register_paper(app, database, scanner_settings):
    settings = PaperSettings.load()
    repo = PaperRepository(database, settings)
    monitor = Monitoring(database, MonitorSettings.load())
    heartbeat = Heartbeat(monitor, "Web Dashboard").start()
    app.extensions["web_heartbeat"] = heartbeat

    @app.context_processor
    def system_context():
        return dict(
            system=monitor.status(scanner_settings),
            preview_mode=os.getenv("APP_PREVIEW") == "1",
        )

    def csrf_check():
        token = request.form.get("csrf", "")
        if not token or not hmac.compare_digest(token, session.get("csrf", "")):
            abort(403, "Invalid CSRF token")

    @app.get("/paper")
    def paper():
        status = repo.status()
        positions, trades, signals = [], [], []
        if status.get("initialized"):
            with database.session() as db:
                positions = list(
                    db.scalars(
                        select(PaperPosition)
                        .where(PaperPosition.status == "OPEN")
                        .order_by(PaperPosition.id)
                    )
                )
                trades = list(
                    db.scalars(
                        select(PaperPosition)
                        .where(PaperPosition.status == "CLOSED")
                        .order_by(PaperPosition.id.desc())
                        .limit(200)
                    )
                )
                for signal in db.scalars(
                    select(SignalRecord).order_by(SignalRecord.id.desc()).limit(200)
                ):
                    risk = db.scalar(
                        select(RiskRecord).where(RiskRecord.signal_id == signal.id)
                    )
                    order = db.scalar(
                        select(PaperOrder).where(PaperOrder.signal_id == signal.id)
                    )
                    signals.append(
                        dict(
                            id=signal.id,
                            timestamp=signal.timestamp,
                            symbol=signal.symbol,
                            strategy_name=signal.strategy_name,
                            action=signal.action,
                            strength=signal.signal_strength,
                            reasons=signal.reasons,
                            risk=risk,
                            order=order,
                        )
                    )
        return render_template(
            "paper.html",
            portfolio=status,
            positions=positions,
            trades=trades,
            signals=signals,
            permissions=repo.permissions() if status.get("initialized") else [],
        )

    @app.post("/paper/permissions")
    def paper_permission():
        csrf_check()
        symbol = request.form.get("symbol", "").strip().upper()
        value = request.form.get("enabled")
        if value not in {"ON", "OFF"}:
            abort(400, "Expected ON or OFF")
        try:
            repo.set_manual_trade_enabled(symbol, value == "ON")
        except (ValueError, RuntimeError) as exc:
            abort(400, str(exc))
        monitor.event(
            "Paper Trading Engine", f"Manual entry permission {symbol}: {value}"
        )
        return redirect(url_for("paper"))

    @app.post("/paper/control")
    def paper_control():
        csrf_check()
        value = request.form.get("enabled")
        if value not in {"ON", "OFF"}:
            abort(400, "Expected ON or OFF")
        try:
            repo.set_enabled(value == "ON")
        except RuntimeError as exc:
            abort(409, str(exc))
        monitor.event("Paper Trading Engine", f"Paper trading {value} by user")
        return redirect(url_for("paper"))

    @app.post("/paper/positions/<int:position_id>/close")
    def close_paper(position_id):
        csrf_check()
        try:
            PaperEngine(repo, scanner_settings).manual_close(position_id)
        except (ValueError, RuntimeError) as exc:
            abort(409, str(exc))
        monitor.event(
            "Paper Trading Engine", f"Manual close of paper position {position_id}"
        )
        return redirect(url_for("paper"))

    @app.get("/paper/signals/<int:signal_id>")
    def paper_audit(signal_id):
        if not repo.available():
            abort(404)
        with database.session() as db:
            signal = db.get(SignalRecord, signal_id)
            if signal is None:
                abort(404)
            risk = db.scalar(
                select(RiskRecord).where(RiskRecord.signal_id == signal.id)
            )
            order = db.scalar(
                select(PaperOrder).where(PaperOrder.signal_id == signal.id)
            )
            fill = (
                db.scalar(select(PaperFill).where(PaperFill.order_id == order.id))
                if order
                else None
            )
            snapshot = (
                db.get(Snapshot, signal.snapshot_id) if signal.snapshot_id else None
            )
            position = db.get(PaperPosition, fill.position_id) if fill else None
            return render_template(
                "paper_audit.html",
                signal=signal,
                risk=risk,
                order=order,
                fill=fill,
                snapshot=snapshot,
                position=position,
                exit_signal_id=db.get(PaperOrder, position.exit_order_id).signal_id
                if position and position.exit_order_id
                else None,
            )

    @app.get("/services")
    def services():
        return render_template("services.html")

    @app.get("/api/system/status")
    def system_status():
        return jsonify(monitor.status(scanner_settings))

    @app.get("/health")
    def health():
        system = monitor.status(scanner_settings)
        return jsonify(
            status="ok" if system["healthy"] else "degraded", version=system["version"]
        ), 200 if system["healthy"] else 503
