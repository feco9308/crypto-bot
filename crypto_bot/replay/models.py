"""Versioned SQLite replay storage; only dedicated replay.db is accepted."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from crypto_bot.monitoring.service import build_info
from crypto_bot.replay.config import config_hash
from crypto_bot.replay.rejections import from_audit
from crypto_bot.replay.stale import STRICT

SCHEMA = (Path(__file__).parent / "migrations" / "0001_initial.sql").read_text()


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def dump(value):
    return json.dumps(value, separators=(",", ":"), default=str, allow_nan=False)


class ReplayStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if self.path.name != "replay.db":
            raise ValueError("Replay storage must be named replay.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables & {
                "paper_accounts",
                "paper_account",
                "scanner_runs",
                "snapshots",
                "market_snapshots",
                "alembic_version",
            }:
                raise ValueError(
                    "Production tables detected; refusing replay migration"
                )
            db.executescript(SCHEMA)
            db.execute(
                "INSERT OR IGNORE INTO replay_schema_migrations VALUES(1,?)", (now(),)
            )
            version = db.execute(
                "SELECT max(version) FROM replay_schema_migrations"
            ).fetchone()[0]
            if version != 1:
                raise ValueError("Unsupported replay schema version")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, config):
        run_id = uuid.uuid4().hex
        info = build_info()
        metadata = dict(
            application_version=info["version"],
            git_commit=info["git_commit"],
            git_branch=info["git_branch"],
            config_hash=config_hash(config),
            replay_schema_version=1,
            strict_no_lookahead=True,
            stale_position_policy=config.get("stale_position_policy", STRICT),
            historical_data_revision={},
            universe_limitations="Archive catalog is not historical exchangeInfo; missing/delisted archives may cause survivorship limitations",
            intrabar_model="CONSERVATIVE_EXIT_PRIORITY"
            if config["conservative"]
            else "OPTIMISTIC_EXIT_PRIORITY",
        )
        with self.connection() as db:
            queued = db.execute(
                "SELECT count(*) FROM replay_runs WHERE status IN ('QUEUED','DOWNLOADING','WARMUP','RUNNING','PAUSED')"
            ).fetchone()[0]
            if queued >= 20:
                raise ValueError("Replay queue full (20 unfinished runs)")
            db.execute(
                "INSERT INTO replay_runs(id,status,created_at,config,metadata) VALUES(?,?,?,?,?)",
                (run_id, "QUEUED", now(), dump(config), dump(metadata)),
            )
            db.executemany(
                "INSERT INTO replay_variants(run_id,number,config) VALUES(?,?,?)",
                [(run_id, i, dump(v)) for i, v in enumerate(config["variants"])],
            )
        return run_id

    def get(self, run_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM replay_runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError("Replay run not found")
            out = dict(row)
            for k in ("config", "metadata", "progress"):
                out[k] = json.loads(out[k])
            out.pop("checkpoint", None)
            out["variants"] = [
                dict(
                    number=r["number"],
                    config=json.loads(r["config"]),
                    summary=json.loads(r["summary"]) if r["summary"] else None,
                )
                for r in db.execute(
                    "SELECT * FROM replay_variants WHERE run_id=? ORDER BY number",
                    (run_id,),
                )
            ]
            for variant in out["variants"]:
                summary = variant["summary"]
                if summary and "blocked_entry_reasons" not in summary["metrics"]:
                    metrics = summary["metrics"]
                    metrics.update(
                        self.blocked_entry_details(
                            run_id,
                            variant["number"],
                            metrics.get("blocked_entries", {}),
                        )
                    )
            return out

    def blocked_entry_details(self, run_id, variant, blocked):
        # Extract only reporting fields: rejected decisions can contain large
        # portfolio snapshots, which need not be loaded into Python to count them.
        with self.connection() as db:
            rows = db.execute(
                """SELECT json_extract(data,'$.event') event,
                          json_extract(data,'$.symbol') symbol,
                          json_extract(data,'$.action') action,
                          COALESCE(json_extract(data,'$.reasons'),
                                   json_extract(data,'$.decision.reasons')) reasons,
                          json_extract(data,'$.reason') reason
                   FROM replay_audit_events WHERE run_id=? AND variant=? AND
                   (json_extract(data,'$.event') IN
                        ('ENTRY_FILL','EXIT_FILL','NO_FILL','RISK_REJECTED') OR
                    (json_extract(data,'$.event')='RISK_DECISION' AND
                     json_extract(data,'$.decision.approved')=0)) ORDER BY id""",
                (run_id, variant),
            )
            events = []
            for row in rows:
                event = dict(row)
                if event["reasons"]:
                    event["reasons"] = json.loads(event["reasons"])
                events.append(event)
        return from_audit(events, blocked)

    def merge_progress(self, run_id, values):
        with self.connection() as db:
            previous = db.execute(
                "SELECT progress FROM replay_runs WHERE id=?", (run_id,)
            ).fetchone()[0]
            db.execute(
                "UPDATE replay_runs SET progress=?,heartbeat=? WHERE id=?",
                (dump(json.loads(previous) | values), time.time(), run_id),
            )

    def compare_options(self):
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT r.id,r.created_at,json_extract(r.config,'$.name') name,json_extract(r.config,'$.start') start,json_extract(r.config,'$.end') end,v.number,json_extract(v.config,'$.name') variant_name,json_extract(v.config,'$.exit_policy') exit_policy FROM replay_runs r JOIN replay_variants v ON v.run_id=r.id WHERE r.status='COMPLETED' ORDER BY r.created_at DESC,v.number LIMIT 1000"
                )
            ]

    def runs(self):
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,status,created_at,started_at,finished_at,error FROM replay_runs ORDER BY created_at DESC LIMIT 100"
                )
            ]

    def claim(self, owner):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM replay_runs WHERE status='QUEUED' AND control!='CANCEL' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE replay_runs SET status='DOWNLOADING',worker=?,heartbeat=?,started_at=COALESCE(started_at,?) WHERE id=?",
                (owner, time.time(), now(), row["id"]),
            )
            return row["id"]

    def update(self, run_id, **values):
        allowed = {
            "status",
            "progress",
            "error",
            "finished_at",
            "metadata",
            "checkpoint",
            "heartbeat",
        }
        if set(values) - allowed:
            raise ValueError("Invalid internal replay update")
        for k in ("progress", "metadata", "checkpoint"):
            if k in values:
                values[k] = dump(values[k])
        with self.connection() as db:
            db.execute(
                "UPDATE replay_runs SET "
                + ",".join(k + "=?" for k in values)
                + " WHERE id=?",
                tuple(values.values()) + (run_id,),
            )

    def control(self, run_id, command=None):
        with self.connection() as db:
            row = db.execute(
                "SELECT control,status FROM replay_runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError("Replay run not found")
            if command is None:
                return row["control"]
            if command not in ("PAUSE", "RUN", "CANCEL"):
                raise ValueError("Invalid replay control")
            if row["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
                raise ValueError("Run already terminal")
            db.execute("UPDATE replay_runs SET control=? WHERE id=?", (command, run_id))
            if command == "CANCEL" and row["status"] == "QUEUED":
                db.execute(
                    "UPDATE replay_runs SET status='CANCELLED',finished_at=? WHERE id=?",
                    (now(), run_id),
                )

    def worker(self, owner=None, status="READY", pid=None):
        with self.connection() as db:
            if owner is None:
                row = db.execute(
                    "SELECT * FROM replay_worker_status WHERE id=1"
                ).fetchone()
                return dict(row) if row else {"status": "STOPPED"}
            info = build_info()
            db.execute(
                "INSERT OR REPLACE INTO replay_worker_status VALUES(1,?,?,?,?,?,?)",
                (owner, time.time(), status, info["version"], info["git_commit"], pid),
            )

    def flush(self, run_id, variant, trades, orders, fills, events, equity):
        with self.connection() as db:
            db.executemany(
                "INSERT INTO replay_trades(run_id,variant,position_id,symbol,status,data) VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,variant,position_id) DO UPDATE SET status=excluded.status,data=excluded.data",
                [
                    (
                        run_id,
                        variant,
                        t["position_id"],
                        t["symbol"],
                        t["status"],
                        dump(t),
                    )
                    for t in trades
                ],
            )
            for table, rows in (
                ("replay_orders", orders),
                ("replay_fills", fills),
                ("replay_audit_events", events),
            ):
                db.executemany(
                    f"INSERT INTO {table}(run_id,variant,data) VALUES(?,?,?)",
                    [(run_id, variant, dump(r)) for r in rows],
                )
            db.executemany(
                "INSERT OR REPLACE INTO replay_equity_curve VALUES(?,?,?,?)",
                [(run_id, variant, r["time"], dump(r)) for r in equity],
            )

    def data(self, run_id, variant=0):
        self.get(run_id)
        with self.connection() as db:
            trades = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT data FROM replay_trades WHERE run_id=? AND variant=? ORDER BY position_id",
                    (run_id, variant),
                )
            ]
            curve = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT data FROM replay_equity_curve WHERE run_id=? AND variant=? ORDER BY time",
                    (run_id, variant),
                )
            ]
            return trades, curve

    def audit(self, run_id, variant, position_id):
        with self.connection() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT data FROM replay_audit_events WHERE run_id=? AND variant=? ORDER BY id",
                    (run_id, variant),
                )
                if json.loads(r[0]).get("position_id") == position_id
            ]

    def presets(self, name=None, config=None):
        with self.connection() as db:
            if name is not None:
                if not isinstance(name, str) or not 1 <= len(name) <= 80:
                    raise ValueError("Invalid preset name")
                db.execute(
                    "INSERT INTO replay_presets(name,config,created_at) VALUES(?,?,?)",
                    (name, dump(config), now()),
                )
            return [
                dict(id=r["id"], name=r["name"], config=json.loads(r["config"]))
                for r in db.execute(
                    "SELECT * FROM replay_presets ORDER BY id DESC LIMIT 100"
                )
            ]

    def checkpoint(self, run_id):
        with self.connection() as db:
            raw = db.execute(
                "SELECT checkpoint FROM replay_runs WHERE id=?", (run_id,)
            ).fetchone()[0]
            return json.loads(raw) if raw else None

    def save_step(self, run_id, payloads, progress, checkpoint):
        # Global hour results and restart checkpoint commit together, across variants.
        with self.connection() as db:
            for variant, trades, orders, fills, events, equity in payloads:
                db.executemany(
                    "INSERT INTO replay_trades(run_id,variant,position_id,symbol,status,data) VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,variant,position_id) DO UPDATE SET status=excluded.status,data=excluded.data",
                    [
                        (
                            run_id,
                            variant,
                            t["position_id"],
                            t["symbol"],
                            t["status"],
                            dump(t),
                        )
                        for t in trades
                    ],
                )
                for table, rows in (
                    ("replay_orders", orders),
                    ("replay_fills", fills),
                    ("replay_audit_events", events),
                ):
                    db.executemany(
                        f"INSERT INTO {table}(run_id,variant,data) VALUES(?,?,?)",
                        [(run_id, variant, dump(r)) for r in rows],
                    )
                db.executemany(
                    "INSERT OR REPLACE INTO replay_equity_curve VALUES(?,?,?,?)",
                    [(run_id, variant, r["time"], dump(r)) for r in equity],
                )
            db.execute(
                "UPDATE replay_runs SET checkpoint=?,progress=?,heartbeat=? WHERE id=?",
                (dump(checkpoint), dump(progress), time.time(), run_id),
            )

    def complete(self, run_id, results, metadata):
        with self.connection() as db:
            for index, result in enumerate(results):
                db.execute(
                    "UPDATE replay_variants SET summary=? WHERE run_id=? AND number=?",
                    (dump(result), run_id, index),
                )
            db.execute(
                "UPDATE replay_runs SET status='COMPLETED',finished_at=?,metadata=? WHERE id=?",
                (now(), dump(metadata), run_id),
            )

    def recover(self):
        # Called only while holding the OS singleton worker lock.
        with self.connection() as db:
            db.execute(
                "UPDATE replay_runs SET status='CANCELLED',finished_at=? WHERE control='CANCEL' AND status NOT IN ('COMPLETED','FAILED','CANCELLED')",
                (now(),),
            )
            db.execute(
                "UPDATE replay_runs SET status='QUEUED',worker=NULL WHERE control!='CANCEL' AND status IN ('DOWNLOADING','WARMUP','RUNNING','PAUSED')"
            )
