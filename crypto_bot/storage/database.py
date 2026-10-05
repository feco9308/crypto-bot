"""Repository boundary. All timestamps stored as naive UTC for backend parity."""

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from crypto_bot.scanner.selection import OVERRIDES, effective_state
from crypto_bot.storage.models import (
    Base,
    Decision,
    Instrument,
    Override,
    OverrideEvent,
    ScannerRun,
    SchemaVersion,
    Snapshot,
)

log = logging.getLogger(__name__)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(value):
    return value.isoformat() + "Z" if value else None


class Database:
    def __init__(self, url):
        parsed = make_url(url)
        if parsed.get_backend_name() == "sqlite" and parsed.database not in {
            None,
            ":memory:",
        }:
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            url,
            connect_args={"timeout": 30, "check_same_thread": False}
            if parsed.get_backend_name() == "sqlite"
            else {},
        )
        if parsed.get_backend_name() == "sqlite":

            @event.listens_for(self.engine, "connect")
            def sqlite_pragmas(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")

        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        Base.metadata.create_all(self.engine)
        with self.session.begin() as session:
            version = session.get(SchemaVersion, 1)
            if version is None:
                session.add(SchemaVersion(id=1, version=1))
            elif version.version != 1:
                raise RuntimeError("Unsupported database schema; migration required")

    def overrides(self):
        with self.session() as session:
            return {o.symbol: o.status for o in session.scalars(select(Override))}

    def set_override(self, symbol, status):
        if status not in OVERRIDES:
            raise ValueError("Invalid override")
        now = utcnow()
        with self.session.begin() as session:
            if session.get(Instrument, symbol) is None:
                raise KeyError(symbol)
            old = session.get(Override, symbol)
            previous = old.status if old else "AUTO"
            session.merge(Override(symbol=symbol, status=status, updated_at=now))
            session.add(
                OverrideEvent(
                    symbol=symbol, timestamp=now, previous=previous, current=status
                )
            )
        log.info(
            "USER_ACTION",
            extra={
                "event_data": {
                    "symbol": symbol,
                    "previous": previous,
                    "current": status,
                }
            },
        )

    def start_run(self, settings, timestamp):
        with self.session.begin() as session:
            run = ScannerRun(
                timestamp=timestamp,
                timeframe=settings.timeframe,
                configuration=settings.public_dict(),
                status="running",
                errors={},
            )
            session.add(run)
            session.flush()
            return run.id

    def fail_run(self, run_id, error):
        with self.session.begin() as session:
            run = session.get(ScannerRun, run_id)
            run.status, run.errors, run.completed_at = (
                "failed",
                {"run": str(error)},
                utcnow(),
            )

    def history(self, symbol, timeframe, since=None, limit=1000):
        with self.session() as session:
            query = select(Snapshot).where(
                Snapshot.symbol == symbol, Snapshot.timeframe == timeframe
            )
            if since is not None:
                query = query.where(Snapshot.timestamp >= since)
            return list(
                reversed(
                    list(
                        session.scalars(
                            query.order_by(
                                Snapshot.timestamp.desc(), Snapshot.id.desc()
                            ).limit(limit)
                        )
                    )
                )
            )

    def finish_run(self, run_id, markets, rows, selected, errors, candidate_count):
        # One transaction publishes metadata, snapshots and algorithm decisions.
        with self.session.begin() as session:
            run = session.get(ScannerRun, run_id)
            for market in markets:
                session.merge(
                    Instrument(
                        symbol=market.symbol,
                        base_asset=market.base_asset,
                        quote_asset=market.quote_asset,
                        active=market.active,
                        spot=market.spot,
                        updated_at=run.timestamp,
                    )
                )
            session.flush()
            for row in rows:
                f, components = row["features"], row["components"]
                session.add(
                    Snapshot(
                        run_id=run_id,
                        timestamp=run.timestamp,
                        symbol=row["symbol"],
                        timeframe=run.timeframe,
                        total_score=row["total_score"],
                        **{k + "_score": v for k, v in components.items()},
                        **{
                            k: f[k]
                            for k in (
                                "price",
                                "rsi",
                                "ema50",
                                "ema200",
                                "atr",
                                "relative_volume",
                                "spread",
                            )
                        },
                        features=f,
                        reasons=row["reasons"],
                        score_momentum=row["momentum"],
                        score_version=row["score_version"],
                    )
                )
                session.add(
                    Decision(
                        run_id=run_id,
                        symbol=row["symbol"],
                        algorithm_watch=row["symbol"] in selected,
                        reason=selected.get(
                            row["symbol"],
                            "Outside automatic selection thresholds or rank",
                        ),
                    )
                )
            run.status = (
                "partial" if errors and rows else "failed" if not rows else "complete"
            )
            run.errors, run.scored_count, run.candidate_count, run.completed_at = (
                errors,
                len(rows),
                candidate_count,
                utcnow(),
            )

    def latest_run(self, timeframe):
        with self.session() as session:
            run = session.scalar(
                select(ScannerRun)
                .where(ScannerRun.timeframe == timeframe)
                .order_by(ScannerRun.id.desc())
                .limit(1)
            )
            return (
                None
                if run is None
                else dict(
                    id=run.id,
                    timestamp=iso_utc(run.timestamp),
                    status=run.status,
                    completed_at=iso_utc(run.completed_at),
                    errors=run.errors,
                    candidate_count=run.candidate_count,
                    scored_count=run.scored_count,
                )
            )

    def latest_rows(self, timeframe):
        with self.session() as session:
            run = session.scalar(
                select(ScannerRun)
                .where(
                    ScannerRun.timeframe == timeframe,
                    ScannerRun.status.in_(["complete", "partial"]),
                )
                .order_by(ScannerRun.id.desc())
                .limit(1)
            )
            latest = session.scalar(
                select(ScannerRun)
                .where(ScannerRun.timeframe == timeframe)
                .order_by(ScannerRun.id.desc())
                .limit(1)
            )
            overrides = self.overrides()
            decisions = (
                {
                    d.symbol: d
                    for d in session.scalars(
                        select(Decision).where(Decision.run_id == run.id)
                    )
                }
                if run
                else {}
            )
            snapshots = (
                {
                    s.symbol: s
                    for s in session.scalars(
                        select(Snapshot).where(Snapshot.run_id == run.id)
                    )
                }
                if run
                else {}
            )
            # Keep manual instruments visible through errors / universe changes.
            for symbol, status in overrides.items():
                if status != "AUTO" and symbol not in snapshots:
                    old = session.scalar(
                        select(Snapshot)
                        .where(
                            Snapshot.symbol == symbol, Snapshot.timeframe == timeframe
                        )
                        .order_by(Snapshot.timestamp.desc())
                        .limit(1)
                    )
                    if old is not None:
                        snapshots[symbol] = old
            rows = []
            for symbol, snapshot in snapshots.items():
                current = bool(
                    run
                    and snapshot.run_id == run.id
                    and not (latest and latest.status == "failed")
                )
                decision = decisions.get(symbol) if current else None
                algorithm_watch = bool(decision and decision.algorithm_watch)
                override = overrides.get(symbol, "AUTO")
                rows.append(
                    self.serialize(snapshot)
                    | dict(
                        algorithm_watch=algorithm_watch,
                        algorithm_reason=decision.reason
                        if decision
                        else "No current successful observation",
                        user_override=override,
                        effective_state=effective_state(algorithm_watch, override),
                        stale=not current,
                    )
                )
            for symbol, status in overrides.items():
                if status != "AUTO" and symbol not in snapshots:
                    # A newly pinned/unavailable market must remain visible without history.
                    rows.append(
                        dict(
                            symbol=symbol,
                            timeframe=timeframe,
                            timestamp=None,
                            total_score=None,
                            components={},
                            features=dict(
                                price=None,
                                trend="unavailable",
                                rsi=None,
                                relative_volume=None,
                                atr_pct=None,
                                spread=None,
                                momentum=None,
                                volume=None,
                            ),
                            reasons=[
                                "No successful observation for this instrument/timeframe"
                            ],
                            momentum={},
                            score_version="pending",
                            algorithm_watch=False,
                            algorithm_reason="No successful observation",
                            user_override=status,
                            effective_state=effective_state(False, status),
                            stale=True,
                        )
                    )
            return rows

    @staticmethod
    def serialize(snapshot):
        return dict(
            symbol=snapshot.symbol,
            timeframe=snapshot.timeframe,
            timestamp=iso_utc(snapshot.timestamp),
            total_score=snapshot.total_score,
            components={
                key: getattr(snapshot, key + "_score")
                for key in ("trend", "momentum", "volume", "volatility", "liquidity")
            },
            features=snapshot.features,
            reasons=snapshot.reasons,
            momentum=snapshot.score_momentum,
            score_version=snapshot.score_version,
        )
