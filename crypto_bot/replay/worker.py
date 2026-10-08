"""Separate singleton replay worker. OS lock + atomic hourly recovery checkpoints."""

import argparse
import fcntl
import logging
import os
import signal
import threading
import time
import uuid

from dotenv import load_dotenv

from crypto_bot.replay.cache import CandleCache
from crypto_bot.replay.config import isolated_paths
from crypto_bot.replay.engine import ReplayEngine
from crypto_bot.replay.historical_data import HistoricalData
from crypto_bot.replay.models import ReplayStore, now

log = logging.getLogger(__name__)


class Cancelled(Exception):
    pass


class Stopping(Exception):
    pass


class ReplayWorker:
    def __init__(self, store, cache, provider_factory=HistoricalData):
        self.store = store
        self.cache = cache
        self.provider_factory = provider_factory
        self.owner = uuid.uuid4().hex
        self.stop = threading.Event()
        self.active = None
        self.last_control = 0.0

    def check(self):
        if self.stop.is_set():
            raise Stopping()
        if not self.active:
            return
        if time.monotonic() - self.last_control < 0.25:
            return
        self.last_control = time.monotonic()
        command = self.store.control(self.active)
        if command == "CANCEL":
            raise Cancelled()
        if command == "PAUSE":
            self.store.update(self.active, status="PAUSED")
            while not self.stop.wait(1):
                self.store.worker(self.owner, "PAUSED", os.getpid())
                command = self.store.control(self.active)
                if command == "CANCEL":
                    raise Cancelled()
                if command == "RUN":
                    break
            if self.stop.is_set():
                raise Stopping()
            self.store.update(self.active, status="RUNNING")

    def once(self):
        run_id = self.store.claim(self.owner)
        if run_id is None:
            return False
        self.active = run_id
        try:

            def progress(**values):
                self.store.merge_progress(run_id, values)

            provider = self.provider_factory(
                self.cache, check=self.check, progress=progress
            )
            ReplayEngine(self.store, provider, self.check).run(run_id)
        except Cancelled:
            self.store.update(run_id, status="CANCELLED", finished_at=now())
        except Stopping:
            self.store.update(run_id, status="QUEUED")
        except Exception as exc:
            log.exception("REPLAY_FAILED")
            self.store.update(
                run_id,
                status="FAILED",
                finished_at=now(),
                error=f"{type(exc).__name__}: {str(exc)[:240]}",
            )
        finally:
            self.active = None
        return True

    def run(self, once=False):
        lock = (self.cache.root / "worker.lock").open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another replay worker is running") from None
        self.store.recover()

        def pulse():
            while not self.stop.wait(5):
                self.store.worker(
                    self.owner, "RUNNING" if self.active else "READY", os.getpid()
                )

        self.store.worker(self.owner, "READY", os.getpid())
        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        try:
            while not self.stop.is_set():
                worked = self.once()
                if once:
                    return
                if not worked:
                    self.stop.wait(2)
        finally:
            self.stop.set()
            thread.join(timeout=6)
            self.store.worker(self.owner, "STOPPED", os.getpid())
            lock.close()


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Isolated historical replay worker; NO LIVE TRADING"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    path, cache_path = isolated_paths(os.getenv("REPLAY_DATA_DIR", "data"))
    worker = ReplayWorker(ReplayStore(path), CandleCache(cache_path))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: worker.stop.set())
    worker.run(args.once)


if __name__ == "__main__":
    main()
