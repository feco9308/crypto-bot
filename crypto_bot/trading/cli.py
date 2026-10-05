import argparse
import json
import signal
import threading
import uuid

from crypto_bot.config.settings import Settings
from crypto_bot.monitoring.config import MonitorSettings
from crypto_bot.monitoring.service import Heartbeat, Monitoring
from crypto_bot.storage.database import Database
from crypto_bot.trading.config import PaperSettings
from crypto_bot.trading.engine import PaperEngine
from crypto_bot.trading.repository import PaperRepository


def paper_main(argv):
    parser = argparse.ArgumentParser(
        description="Paper trading only; NO LIVE TRADING IMPLEMENTED"
    )
    parser.add_argument(
        "command", choices=["init", "status", "run", "on", "off", "close", "reset"]
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--position", type=int)
    parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    scanner = Settings.load()
    settings = PaperSettings.load()
    database = Database(scanner.database_url)
    repo = PaperRepository(database, settings)
    if args.command == "init":
        repo.initialize_account()
        print(json.dumps(repo.status()))
        return
    if args.command == "status":
        print(json.dumps(repo.status()))
        return
    if not repo.status().get("initialized"):
        parser.error("Run migrate and paper init first")
    monitor = Monitoring(database, MonitorSettings.load())
    if args.command in {"on", "off"}:
        repo.set_enabled(args.command == "on")
        monitor.event(
            "Paper Trading Engine", f"Paper trading {args.command.upper()} by operator"
        )
        print(json.dumps(repo.status()))
        return
    engine = PaperEngine(repo, scanner)
    if args.command == "close":
        if args.position is None:
            parser.error("--position is required")
        print(json.dumps({"order_id": engine.manual_close(args.position)}))
        return
    if args.command == "reset":
        repo.reset(args.confirm == "RESET-PAPER")
        monitor.event(
            "Portfolio / Wallet", "Paper account reset; scanner history preserved"
        )
        print(json.dumps(repo.status()))
        return
    owner = str(uuid.uuid4())
    duration = max(monitor.settings.stale_threshold, settings.loop_interval * 3)
    heartbeat = Heartbeat(monitor, "Paper Trading Engine")
    stop = threading.Event()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda *_: stop.set())
    try:
        from crypto_bot.storage.database import utcnow

        repo.lease(owner, utcnow(), duration)
        heartbeat.start()
        while not stop.is_set():
            try:
                repo.lease(owner, utcnow(), duration)
                result = engine.run_once(lease_owner=owner)
                heartbeat.result(
                    "RUNNING" if result["fresh_markets"] else "DEGRADED",
                    None
                    if result["fresh_markets"]
                    else "No fresh scanner observations; entries/exits waiting for data",
                )
                if args.once:
                    print(json.dumps(result))
                    return
            except Exception as exc:
                from sqlalchemy.exc import SQLAlchemyError

                message = (
                    "Database operation failed"
                    if isinstance(exc, SQLAlchemyError)
                    else str(exc)
                )
                heartbeat.result("ERROR", message)
                if args.once:
                    raise
            stop.wait(settings.loop_interval)
    finally:
        repo.release_lease(owner)
        heartbeat.close()
