import argparse
import logging
import signal
import threading
from crypto_bot.config.settings import Settings
from crypto_bot.data.binance import BinanceData
from crypto_bot.logging_config import configure_logging
from crypto_bot.scanner.scanner import Scanner
from crypto_bot.storage.database import Database


def main():
    parser = argparse.ArgumentParser(description="Public market intelligence scanner; no execution")
    parser.add_argument("command", choices=["init-db", "scan", "web"])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6000)
    args = parser.parse_args()
    configure_logging()
    settings = Settings.load()
    database = Database(settings.database_url)
    database.initialize()
    if args.command == "init-db":
        return
    if args.command == "web":
        from crypto_bot.web.app import create_app
        create_app(settings, database).run(host=args.host, port=args.port)
        return
    scanner = Scanner(BinanceData(settings), database, settings)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            rows = scanner.run_once()
            if args.once:
                if not rows:
                    raise SystemExit(1)
                return
        except Exception:
            if args.once:
                raise SystemExit(1)
            logging.getLogger(__name__).warning("SCANNER_RETRY_NEXT_INTERVAL")
        stop.wait(settings.scanner_interval)


if __name__ == "__main__":
    main()
