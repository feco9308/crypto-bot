"""Separate research job database. Never opens the scanner or paper database."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from crypto_bot.monitoring.service import build_info
from crypto_bot.replay.config import config_hash
from crypto_bot.replay.models import dump, now

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_schema(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO shadow_schema VALUES(1);
CREATE TABLE IF NOT EXISTS shadow_jobs(
 id TEXT PRIMARY KEY,status TEXT NOT NULL,created_at TEXT NOT NULL,
 config TEXT NOT NULL,source TEXT NOT NULL,metadata TEXT NOT NULL,
 progress TEXT NOT NULL DEFAULT '{}',result TEXT,error TEXT,
 finished_at TEXT,control TEXT NOT NULL DEFAULT 'RUN');
"""


def canonical_entries(source, run_id, index):
    """Snapshot actual fills + entry-only observations; never copy future exits."""
    trades, _ = source.data(run_id, index)
    by_id = {t["position_id"]: t for t in trades}
    with source.connection() as db:
        fills = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT data FROM replay_fills WHERE run_id=? AND variant=? ORDER BY id",
                (run_id, index),
            )
        ]
    fields = (
        "asset_group",
        "original_stop_price",
        "atr",
        "atr_pct",
        "entry_score",
        "delta_1h",
        "delta_4h",
        "delta_24h",
        "rsi",
        "relative_volume",
        "momentum",
        "range_position",
        "btc_regime",
        "risk_amount",
        "scanner_snapshot_timestamp",
        "strategy_reasons",
        "score_components",
    )
    entries = []
    for f in fills:
        if f["side"] != "BUY" or f["status"] != "FILLED":
            continue
        trade = by_id.get(f["position_id"])
        if trade is None:
            raise ValueError("Anchor BUY has no persisted entry audit")
        e = {k: trade.get(k) for k in fields}
        e.update(
            shadow_entry_id=f"{run_id}:{index}:{f['position_id']}",
            anchor_position_id=f["position_id"],
            anchor_order_id=f["order_id"],
            symbol=f["symbol"],
            entry_time=f["execution_time"],
            entry_fill_price=f["fill_price"],
            quantity=f["quantity"],
            notional=f["notional"],
            entry_fee=f["fee"],
            entry_slippage=f["slippage"],
            entry_source_timeframe=f["source_timeframe"],
        )
        if not e["original_stop_price"] or e["atr"] is None:
            raise ValueError("Canonical entry lacks stop/ATR audit")
        entries.append(e)
    entries.sort(key=lambda e: (e["entry_time"], e["shadow_entry_id"]))
    if len({e["shadow_entry_id"] for e in entries}) != len(entries):
        raise ValueError("Duplicate canonical BUY fill")
    return entries


class ShadowStore:
    def __init__(self, path):
        raw = Path(path)
        if raw.name != "shadow-exit.db" or raw.is_symlink():
            raise ValueError("Separate shadow-exit.db required")
        self.path = raw.resolve()
        for name in ("market.db", "replay.db"):
            other = self.path.parent / name
            if self.path.exists() and other.exists() and self.path.samefile(other):
                raise ValueError("Shadow database cannot alias an existing database")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables - {"shadow_schema", "shadow_jobs"}:
                raise ValueError("Non-shadow database detected")
            db.executescript(SCHEMA)
            if db.execute("SELECT max(version) FROM shadow_schema").fetchone()[0] != 1:
                raise ValueError("Unsupported shadow schema")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, config, source):
        anchor = source.get(config["anchor_run_id"])
        if anchor["status"] != "COMPLETED":
            raise ValueError("Completed immutable anchor required")
        entries = canonical_entries(source, anchor["id"], config["anchor_variant"])
        if not entries:
            raise ValueError("Anchor has no FILLED BUY entries")
        snapshot = dict(
            config=anchor["config"],
            variant=anchor["variants"][config["anchor_variant"]]["config"],
            metadata=anchor["metadata"],
            entries=entries,
        )
        info = build_info()
        metadata = dict(
            application_version=info["version"],
            git_commit=info["git_commit"],
            git_branch=info["git_branch"],
            replay_schema_version=1,
            shadow_schema_version=SCHEMA_VERSION,
            anchor_config_hash=config_hash(
                dict(
                    run_config=snapshot["config"],
                    variant_index=config["anchor_variant"],
                    variant_config=snapshot["variant"],
                )
            ),
            anchor_run_config_hash=config_hash(snapshot["config"]),
            anchor_entries_hash=config_hash(entries),
            shadow_config_hash=config_hash(config),
            mode="MATCHED_ENTRY_EXIT_COMPARISON",
            portfolio_performance=False,
            statistically_validated=False,
        )
        ident = uuid.uuid4().hex
        with self.connection() as db:
            count = db.execute(
                "SELECT count(*) FROM shadow_jobs WHERE status IN ('QUEUED','RUNNING')"
            ).fetchone()[0]
            if count >= 20:
                raise ValueError("Shadow queue full")
            db.execute(
                "INSERT INTO shadow_jobs(id,status,created_at,config,source,metadata) VALUES(?,?,?,?,?,?)",
                (ident, "QUEUED", now(), dump(config), dump(snapshot), dump(metadata)),
            )
        return ident

    def get(self, ident, include_source=False):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM shadow_jobs WHERE id=?", (ident,)
            ).fetchone()
        if row is None:
            raise KeyError("Shadow job not found")
        result = dict(row)
        for k in ("config", "metadata", "progress", "result", "source"):
            result[k] = json.loads(result[k]) if result[k] else None
        if not include_source:
            result.pop("source")
        return result

    def jobs(self):
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,status,created_at,config,error FROM shadow_jobs ORDER BY created_at DESC LIMIT 100"
                )
            ]

    def claim(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM shadow_jobs WHERE status='QUEUED' AND control='RUN' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE shadow_jobs SET status='RUNNING',error=NULL WHERE id=?",
                (row[0],),
            )
            return row[0]

    def update(self, ident, **values):
        if set(values) - {
            "status",
            "progress",
            "result",
            "error",
            "finished_at",
            "control",
        }:
            raise ValueError("Invalid shadow update")
        for k in ("progress", "result"):
            if k in values:
                values[k] = dump(values[k])
        with self.connection() as db:
            db.execute(
                "UPDATE shadow_jobs SET "
                + ",".join(k + "=?" for k in values)
                + " WHERE id=?",
                (*values.values(), ident),
            )

    def recover(self):
        # Under the existing replay singleton lock; deterministic restart from snapshot.
        with self.connection() as db:
            db.execute(
                "UPDATE shadow_jobs SET status='QUEUED' WHERE status='RUNNING' AND control='RUN'"
            )
