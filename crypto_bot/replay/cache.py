"""Dedicated indexed candle cache and resumable archive object manifests."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class CandleCache:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "metadata").mkdir(exist_ok=True)
        (self.root / "objects").mkdir(exist_ok=True)
        self.path = self.root / "candles.sqlite"
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS candles(symbol TEXT,interval TEXT,time INTEGER,close_time INTEGER,
                open REAL,high REAL,low REAL,close REAL,volume REAL,quote_volume REAL,PRIMARY KEY(symbol,interval,time));
                CREATE TABLE IF NOT EXISTS objects(key TEXT PRIMARY KEY,status TEXT,sha256 TEXT,rows INTEGER,checked_at REAL);""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def object(self, key):
        with self.connect() as db:
            row = db.execute(
                "SELECT status,sha256,rows,checked_at FROM objects WHERE key=?", (key,)
            ).fetchone()
            return (
                dict(zip(("status", "sha256", "rows", "checked_at"), row))
                if row
                else None
            )

    def put(self, symbol, interval, rows, key=None, sha=None):
        with self.connect() as db:
            db.executemany(
                "INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(symbol, interval, *r) for r in rows],
            )
            if key:
                import time

                db.execute(
                    "INSERT OR REPLACE INTO objects VALUES(?,?,?,?,?)",
                    (key, "COMPLETE", sha, len(rows), time.time()),
                )

    def missing(self, key):
        import time

        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO objects VALUES(?,?,?,?,?)",
                (key, "MISSING", None, 0, time.time()),
            )

    def rows(self, symbol, interval, start, end, limit=None):
        with self.connect() as db:
            return db.execute(
                "SELECT time,close_time,open,high,low,close,volume,quote_volume FROM candles WHERE symbol=? AND interval=? AND time>=? AND time<? ORDER BY time LIMIT ?",
                (symbol, interval, start, end, limit if limit is not None else -1),
            ).fetchall()

    def revision(self, keys):
        with self.connect() as db:
            objects = dict(db.execute("SELECT key,sha256 FROM objects"))
        return {k: objects[k] for k in sorted(keys) if k in objects}

    def digest(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT key,status,sha256 FROM objects ORDER BY key"
            ).fetchall()
        return hashlib.sha256(json.dumps(rows).encode()).hexdigest()
