"""Read-only position projections. Never evaluates strategies or mutates the ledger."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from crypto_bot.storage.database import iso_utc, utcnow
from crypto_bot.storage.models import Snapshot
from crypto_bot.storage.paper_models import (
    PaperFill,
    PaperOrder,
    RiskRecord,
    SignalRecord,
)


def milliseconds(value):
    return int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)


def chart_range(position, now=None):
    now = now or utcnow()
    return (
        milliseconds(position.opened_at - timedelta(hours=2)),
        milliseconds(
            min(now, position.closed_at + timedelta(hours=1))
            if position.closed_at
            else now
        ),
    )


def bundle(db, position, order_id):
    order = db.get(PaperOrder, order_id) if order_id else None
    fill = (
        db.scalar(
            select(PaperFill).where(
                PaperFill.order_id == order_id, PaperFill.position_id == position.id
            )
        )
        if order
        else None
    )
    signal = db.get(SignalRecord, order.signal_id) if order else None
    risk = db.get(RiskRecord, order.risk_id) if order else None
    snapshot = (
        db.get(Snapshot, signal.snapshot_id) if signal and signal.snapshot_id else None
    )
    return dict(order=order, fill=fill, signal=signal, risk=risk, snapshot=snapshot)


def position_audit(db, position, now=None):
    now = now or utcnow()
    entry = bundle(db, position, position.entry_order_id)
    exit = bundle(db, position, position.exit_order_id)
    events = []

    def event(time, title, detail, signal_id=None):
        if time:
            events.append(
                dict(timestamp=time, title=title, detail=detail, signal_id=signal_id)
            )

    for name, audit in [("Entry", entry), ("Exit", exit)]:
        signal, risk, order, fill, snapshot = (
            audit[k] for k in ("signal", "risk", "order", "fill", "snapshot")
        )
        if signal:
            if snapshot:
                event(
                    snapshot.timestamp,
                    f"{name} Scanner Snapshot",
                    f"#{snapshot.id} · Score {snapshot.total_score}",
                    signal.id,
                )
            event(
                signal.timestamp,
                f"{name} Strategy Signal",
                f"#{signal.id} · {signal.action} · " + "; ".join(signal.reasons),
                signal.id,
            )
            if risk:
                event(
                    signal.timestamp,
                    f"{name} Risk Decision",
                    f"#{risk.id} · approved={risk.approved} · linked signal timestamp (no separate risk timestamp)",
                    signal.id,
                )
        if order:
            event(
                order.created_at,
                f"{name} Order",
                f"#{order.id} · {order.side} · {order.status}",
            )
        if fill:
            event(
                fill.timestamp,
                f"{name} Fill",
                f"#{fill.id} · price {fill.price} · fee {fill.fee}",
            )
        if name == "Entry":
            event(position.opened_at, "Position Open", f"#{position.id}")
            event(
                position.marked_at,
                "Latest stored mark",
                "Last retained public bid; intermediate mark events are not reconstructed.",
            )
    event(
        position.closed_at,
        "Position Closed",
        f"{position.exit_reason} · realized PnL {position.realized_pnl}",
    )
    # Equal timestamps preserve causal insertion order; no synthetic timestamps.
    events.sort(key=lambda e: e["timestamp"])
    cost = position.notional + position.entry_fee
    pnl = (
        position.realized_pnl
        if position.status == "CLOSED"
        else position.unrealized_pnl
    )
    portfolio = entry["risk"].portfolio if entry["risk"] else {}
    positions = portfolio.get("positions", [])
    total_exposure = sum(
        (Decimal(p["quantity"]) * Decimal(p["current_price"]) for p in positions),
        Decimal(0),
    )
    symbol_exposure = sum(
        (
            Decimal(p["quantity"]) * Decimal(p["current_price"])
            for p in positions
            if p["symbol"] == position.symbol
        ),
        Decimal(0),
    )
    return dict(
        position=position,
        entry=entry,
        exit=exit,
        events=events,
        duration=str((position.closed_at or now) - position.opened_at).split(".")[0],
        return_pct=pnl / cost * 100 if cost else None,
        total_exposure=total_exposure,
        symbol_exposure=symbol_exposure,
        available_cash=Decimal(portfolio["cash_balance"])
        - Decimal(portfolio["reserved_cash"])
        if "cash_balance" in portfolio and "reserved_cash" in portfolio
        else None,
        risk_budget=Decimal(portfolio["equity"])
        * Decimal(str(entry["signal"].configuration["risk_per_trade_pct"]))
        / 100
        if "equity" in portfolio
        and entry["signal"]
        and "risk_per_trade_pct" in entry["signal"].configuration
        else None,
    )


def quote_display(position, quote):
    """Indicative UI metrics only; recorded portfolio accounting stays untouched."""
    bid = Decimal(str(quote["bid"]))
    value = position.quantity * bid
    return dict(
        quote=quote,
        unrealized_pnl=str(value - position.notional - position.entry_fee),
        current_exposure=str(value),
        distance_to_stop_pct=str((bid - position.stop_price) / bid * 100),
        distance_to_tp_pct=str((position.take_profit_price - bid) / bid * 100)
        if position.take_profit_price
        else None,
    )


def score_history(db, position, start, end):
    entry_signal = db.get(SignalRecord, position.signal_id)
    snapshot = (
        db.get(Snapshot, entry_signal.snapshot_id)
        if entry_signal and entry_signal.snapshot_id
        else None
    )
    rows = db.scalars(
        select(Snapshot)
        .where(
            Snapshot.symbol == position.symbol,
            Snapshot.timeframe == (snapshot.timeframe if snapshot else "1h"),
            Snapshot.timestamp
            >= datetime.fromtimestamp(start / 1000, timezone.utc).replace(tzinfo=None),
            Snapshot.timestamp
            <= datetime.fromtimestamp(end / 1000, timezone.utc).replace(tzinfo=None),
        )
        .order_by(Snapshot.timestamp, Snapshot.id)
    )
    return [
        dict(
            id=row.id,
            timestamp=iso_utc(row.timestamp),
            time=milliseconds(row.timestamp),
            score=row.total_score,
            delta_4h=row.score_momentum.get("delta_4h"),
        )
        for row in rows
    ]
