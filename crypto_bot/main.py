import argparse
import logging
import signal
import sys
import threading

from crypto_bot.config.settings import Settings
from crypto_bot.data.binance import BinanceData
from crypto_bot.logging_config import configure_logging
from crypto_bot.scanner.scanner import Scanner
from crypto_bot.storage.database import Database


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "paper":
        from crypto_bot.trading.cli import paper_main

        configure_logging()
        paper_main(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(
        description="Public market intelligence scanner; no execution"
    )
    parser.add_argument("command", choices=["init-db", "migrate", "scan", "web"])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    configure_logging()
    settings = Settings.load()
    database = Database(settings.database_url)
    if args.command == "migrate":
        from crypto_bot.storage.migrate import upgrade_database

        upgrade_database(database)
        from crypto_bot.monitoring.config import MonitorSettings
        from crypto_bot.monitoring.service import Monitoring

        Monitoring(database, MonitorSettings.load()).event(
            "Database", "Database migration completed"
        )
        return
    database.initialize()
    if args.command == "init-db":
        return
    if args.command == "web":
        from crypto_bot.web.app import create_app
        from crypto_bot.web.server import web_bind

        host, port = web_bind(args.host, args.port)
        create_app(settings, database).run(host=host, port=port)
        return
    from crypto_bot.monitoring.config import MonitorSettings
    from crypto_bot.monitoring.service import Heartbeat, Monitoring

    try:
        monitoring_settings = MonitorSettings.load()
    except ValueError:
        monitoring_settings = MonitorSettings()
    heartbeat = Heartbeat(
        Monitoring(database, monitoring_settings), "Market Scanner"
    ).start()
    scanner = Scanner(BinanceData(settings), database, settings)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            rows = scanner.run_once()
            run = database.latest_run(settings.timeframe)
            errors = run["errors"] if run else {}
            heartbeat.result(
                "DEGRADED" if errors and rows else "ERROR" if not rows else "RUNNING",
                str(errors)[:300] if errors else None,
            )
            if args.once:
                if not rows:
                    heartbeat.close()
                    raise SystemExit(1)
                heartbeat.close()
                return
        except Exception:
            heartbeat.result("ERROR", "Scanner cycle failed; see scanner run errors")
            if args.once:
                heartbeat.close()
                raise SystemExit(1)
            logging.getLogger(__name__).warning("SCANNER_RETRY_NEXT_INTERVAL")
        stop.wait(settings.scanner_interval)
    heartbeat.close()


if __name__ == "__main__":
    main()
