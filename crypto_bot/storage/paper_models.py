from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from crypto_bot.storage.decimal_type import ExactDecimal
from crypto_bot.storage.models import Instrument, Snapshot

MONEY = ExactDecimal()


class ExtensionBase(DeclarativeBase):
    pass


class PaperAccount(ExtensionBase):
    __tablename__ = "paper_account"
    id: Mapped[int] = mapped_column(primary_key=True)
    generation: Mapped[int] = mapped_column(default=1)
    quote_asset: Mapped[str] = mapped_column(String(20))
    initial_balance: Mapped[Decimal] = mapped_column(MONEY)
    cash_balance: Mapped[Decimal] = mapped_column(MONEY)
    reserved_cash: Mapped[Decimal] = mapped_column(MONEY, default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=0)
    fees_paid: Mapped[Decimal] = mapped_column(MONEY, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    day: Mapped[str] = mapped_column(String(10))
    day_start_equity: Mapped[Decimal] = mapped_column(MONEY)
    peak_equity: Mapped[Decimal] = mapped_column(MONEY)
    max_drawdown: Mapped[Decimal] = mapped_column(MONEY, default=0)
    configuration: Mapped[dict] = mapped_column(JSON)
    lease_owner: Mapped[str | None] = mapped_column(String(50))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)


class SymbolTradePermission(ExtensionBase):
    __tablename__ = "paper_symbol_permissions"
    symbol: Mapped[str] = mapped_column(ForeignKey(Instrument.symbol), primary_key=True)
    manual_trade_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class SignalRecord(ExtensionBase):
    __tablename__ = "strategy_signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey(Snapshot.id))
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    symbol: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(8))
    signal_strength: Mapped[Decimal] = mapped_column(MONEY)
    entry_reference: Mapped[Decimal] = mapped_column(MONEY)
    stop_reference: Mapped[Decimal | None] = mapped_column(MONEY)
    take_profit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    strategy_name: Mapped[str] = mapped_column(String(80))
    reasons: Mapped[list] = mapped_column(JSON)
    configuration: Mapped[dict] = mapped_column(JSON)
    exit_reason: Mapped[str | None] = mapped_column(String(20))


class RiskRecord(ExtensionBase):
    __tablename__ = "risk_decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("strategy_signals.id"), unique=True
    )
    approved: Mapped[bool] = mapped_column(Boolean)
    risk_amount: Mapped[Decimal] = mapped_column(MONEY)
    position_notional: Mapped[Decimal] = mapped_column(MONEY)
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    stop_distance_pct: Mapped[Decimal] = mapped_column(MONEY)
    reasons: Mapped[list] = mapped_column(JSON)
    portfolio: Mapped[dict] = mapped_column(JSON)
    execution_quote: Mapped[dict | None] = mapped_column(JSON)


class PaperOrder(ExtensionBase):
    __tablename__ = "paper_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("strategy_signals.id"), unique=True
    )
    risk_id: Mapped[int] = mapped_column(ForeignKey("risk_decisions.id"))
    symbol: Mapped[str] = mapped_column(String(40))
    side: Mapped[str] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(12))
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    reference_price: Mapped[Decimal] = mapped_column(MONEY)
    quote: Mapped[dict | None] = mapped_column(JSON)
    reserved_amount: Mapped[Decimal] = mapped_column(MONEY, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(Text, default="")


class PaperPosition(ExtensionBase):
    __tablename__ = "paper_positions"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40))
    side: Mapped[str] = mapped_column(String(4), default="LONG")
    entry_price: Mapped[Decimal] = mapped_column(MONEY)
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    notional: Mapped[Decimal] = mapped_column(MONEY)
    stop_price: Mapped[Decimal] = mapped_column(MONEY)
    take_profit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    opened_at: Mapped[datetime] = mapped_column(DateTime)
    current_price: Mapped[Decimal] = mapped_column(MONEY)
    mark_quote: Mapped[dict | None] = mapped_column(JSON)
    marked_at: Mapped[datetime | None] = mapped_column(DateTime)
    entry_fee: Mapped[Decimal] = mapped_column(MONEY)
    exit_fee: Mapped[Decimal] = mapped_column(MONEY, default=0)
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=0)
    strategy_name: Mapped[str] = mapped_column(String(80))
    signal_id: Mapped[int] = mapped_column(ForeignKey("strategy_signals.id"))
    entry_order_id: Mapped[int] = mapped_column(ForeignKey("paper_orders.id"))
    exit_order_id: Mapped[int | None] = mapped_column(ForeignKey("paper_orders.id"))
    status: Mapped[str] = mapped_column(String(8))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    exit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    exit_reason: Mapped[str | None] = mapped_column(String(20))
    __table_args__ = (Index("ix_paper_positions_symbol_status", "symbol", "status"),)


class PaperFill(ExtensionBase):
    __tablename__ = "paper_fills"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("paper_orders.id"), unique=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("paper_positions.id"))
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    price: Mapped[Decimal] = mapped_column(MONEY)
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    notional: Mapped[Decimal] = mapped_column(MONEY)
    fee: Mapped[Decimal] = mapped_column(MONEY)
    slippage: Mapped[Decimal] = mapped_column(MONEY)


class PortfolioSnapshot(ExtensionBase):
    __tablename__ = "paper_portfolio_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    total_equity: Mapped[Decimal] = mapped_column(MONEY)
    cash_balance: Mapped[Decimal] = mapped_column(MONEY)
    reserved_cash: Mapped[Decimal] = mapped_column(MONEY)
    available_cash: Mapped[Decimal] = mapped_column(MONEY)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    fees_paid: Mapped[Decimal] = mapped_column(MONEY)
    exposure: Mapped[Decimal] = mapped_column(MONEY)
    daily_pnl: Mapped[Decimal] = mapped_column(MONEY)
    max_drawdown: Mapped[Decimal] = mapped_column(MONEY)
    open_positions: Mapped[int] = mapped_column(Integer)
    quote_status: Mapped[dict | None] = mapped_column(JSON)


class ProcessedSnapshot(ExtensionBase):
    __tablename__ = "paper_processed_snapshots"
    snapshot_id: Mapped[int] = mapped_column(ForeignKey(Snapshot.id), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime)


class ServiceStatus(ExtensionBase):
    __tablename__ = "service_status"
    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    type: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(12))
    version: Mapped[str] = mapped_column(String(20))
    git_commit: Mapped[str | None] = mapped_column(String(50))
    git_branch: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime)
    last_success: Mapped[datetime | None] = mapped_column(DateTime)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error_message: Mapped[str | None] = mapped_column(String(300))
    restart_count: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str | None] = mapped_column(String(50))


class SystemEvent(ExtensionBase):
    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    component: Mapped[str] = mapped_column(String(50))
    level: Mapped[str] = mapped_column(String(12))
    message: Mapped[str] = mapped_column(String(300))
