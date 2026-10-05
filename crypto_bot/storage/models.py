from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column()


class Instrument(Base):
    __tablename__ = "instruments"
    symbol: Mapped[str] = mapped_column(String(40), primary_key=True)
    base_asset: Mapped[str] = mapped_column(String(30))
    quote_asset: Mapped[str] = mapped_column(String(30))
    active: Mapped[bool] = mapped_column(Boolean)
    spot: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ScannerRun(Base):
    __tablename__ = "scanner_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    timeframe: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(20))
    configuration: Mapped[dict] = mapped_column(JSON)
    errors: Mapped[dict] = mapped_column(JSON, default=dict)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    scored_count: Mapped[int] = mapped_column(Integer, default=0)


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("scanner_runs.id"))
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"))
    timeframe: Mapped[str] = mapped_column(String(8))
    total_score: Mapped[float] = mapped_column(Float)
    trend_score: Mapped[float] = mapped_column(Float)
    momentum_score: Mapped[float] = mapped_column(Float)
    volume_score: Mapped[float] = mapped_column(Float)
    volatility_score: Mapped[float] = mapped_column(Float)
    liquidity_score: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    rsi: Mapped[float] = mapped_column(Float)
    ema50: Mapped[float] = mapped_column(Float)
    ema200: Mapped[float] = mapped_column(Float)
    atr: Mapped[float] = mapped_column(Float)
    relative_volume: Mapped[float] = mapped_column(Float)
    spread: Mapped[float] = mapped_column(Float)
    features: Mapped[dict] = mapped_column(JSON)
    reasons: Mapped[list] = mapped_column(JSON)
    score_momentum: Mapped[dict] = mapped_column(JSON)
    score_version: Mapped[str] = mapped_column(String(40))
    __table_args__ = (
        Index("ix_snapshot_history", "symbol", "timeframe", "timestamp"),
        Index("ix_snapshot_run_symbol", "run_id", "symbol", unique=True),
    )


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("scanner_runs.id"))
    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"))
    algorithm_watch: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    __table_args__ = (Index("ix_decision_run_symbol", "run_id", "symbol", unique=True),)


class Override(Base):
    __tablename__ = "overrides"
    symbol: Mapped[str] = mapped_column(
        ForeignKey("instruments.symbol"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(8))
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class OverrideEvent(Base):
    __tablename__ = "override_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"))
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    previous: Mapped[str] = mapped_column(String(8))
    current: Mapped[str] = mapped_column(String(8))
