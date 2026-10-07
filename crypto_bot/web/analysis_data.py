"""Allowlisted export projection from stored audit records; SELECT queries only."""

import math
import re
from dataclasses import fields
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import aliased

from crypto_bot.storage.database import iso_utc
from crypto_bot.storage.models import Snapshot
from crypto_bot.storage.paper_models import (
    PaperFill,
    PaperOrder,
    PaperPosition,
    RiskRecord,
    SignalRecord,
)
from crypto_bot.trading.config import PaperSettings
from crypto_bot.web.trade_audit import milliseconds

SECRET = re.compile(
    r"(?i)(api[_ -]?key|secret(?:[_ -]?key)?|password|token|cookie|csrf|database[_ -]?url)\s*[:=]\s*[^\s;,]+"
)
PATH = re.compile(
    r"(?:sqlite|postgresql|mysql)://\S+|/(?:home|etc|var|root|tmp)/[^\s;,]+"
)


def text(value):
    if isinstance(value, str):
        return PATH.sub("[REDACTED]", SECRET.sub("[REDACTED]", value))
    return value if isinstance(value, (int, float, bool, type(None))) else None


def numeric(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return value if Decimal(value).is_finite() else None
        except Exception:
            return None
    return None


def config(value):
    allowed = {f.name for f in fields(PaperSettings)}
    return {
        k: text(v)
        for k, v in (value or {}).items()
        if k in allowed and isinstance(v, (str, int, float, bool, type(None)))
    }


def reasons(values):
    return [
        text(str(value))
        for value in (values or [])
        if isinstance(value, (str, int, float, bool))
    ]


def trade_query():
    eo = aliased(PaperOrder)
    ef = aliased(PaperFill)
    xs = aliased(SignalRecord)
    xo = aliased(PaperOrder)
    xf = aliased(PaperFill)
    return (
        select(PaperPosition, SignalRecord, Snapshot, RiskRecord, eo, ef, xs, xo, xf)
        .outerjoin(SignalRecord, SignalRecord.id == PaperPosition.signal_id)
        .outerjoin(Snapshot, Snapshot.id == SignalRecord.snapshot_id)
        .outerjoin(RiskRecord, RiskRecord.signal_id == SignalRecord.id)
        .outerjoin(eo, eo.id == PaperPosition.entry_order_id)
        .outerjoin(ef, (ef.order_id == eo.id) & (ef.position_id == PaperPosition.id))
        .outerjoin(xo, xo.id == PaperPosition.exit_order_id)
        .outerjoin(xs, xs.id == xo.signal_id)
        .outerjoin(xf, (xf.order_id == xo.id) & (xf.position_id == PaperPosition.id))
    )


def project(row, now):
    p, s, snap, r, eo, ef, xs, xo, xf = row
    eq = (eo.quote or {}) if eo else {}
    xq = (xo.quote or {}) if xo else {}
    permission = (s.configuration or {}).get("entry_permission", {}) if s else {}
    f = snap.features if snap else {}
    delta = snap.score_momentum if snap else {}
    portfolio = r.portfolio if r else {}
    positions = portfolio.get("positions", [])
    cost = p.notional + p.entry_fee
    pnl = p.realized_pnl if p.status == "CLOSED" else p.unrealized_pnl
    out = dict(
        position_id=p.id,
        symbol=text(p.symbol),
        status=p.status,
        strategy_name=text(p.strategy_name),
        entry_time=iso_utc(p.opened_at),
        exit_time=iso_utc(p.closed_at),
        entry_ms=milliseconds(p.opened_at),
        exit_ms=milliseconds(p.closed_at) if p.closed_at else None,
        duration_seconds=((p.closed_at or now) - p.opened_at).total_seconds(),
        quantity=str(p.quantity),
        notional=str(p.notional),
        strategy_reference_price=str(s.entry_reference) if s else None,
        entry_bid=numeric(eq.get("bid")),
        entry_ask=numeric(eq.get("ask")),
        entry_quote_source=text(eq.get("source")),
        entry_quote_received_at=text(eq.get("received_at")),
        entry_fill_price=str(p.entry_price),
        entry_slippage=str(ef.slippage) if ef else None,
        entry_fee=str(p.entry_fee),
        exit_reference_price=str(xs.entry_reference) if xs else None,
        exit_bid=numeric(xq.get("bid")),
        exit_ask=numeric(xq.get("ask")),
        exit_quote_source=text(xq.get("source")),
        exit_fill_price=str(p.exit_price) if p.exit_price is not None else None,
        exit_slippage=str(xf.slippage) if xf else None,
        exit_fee=str(p.exit_fee),
        entry_reason=reasons(s.reasons) if s else [],
        exit_reason=text(p.exit_reason),
        exit_strategy_reasons=reasons(xs.reasons) if xs else [],
        exit_trigger_price=numeric(xq.get("bid"))
        if p.exit_reason in ("STOP", "TAKE_PROFIT")
        else None,
        exit_quote_bid=numeric(xq.get("bid")),
        exit_quote_ask=numeric(xq.get("ask")),
        exit_quote_received_at=text(xq.get("received_at")),
        stop_threshold=str(p.stop_price) if p.exit_reason == "STOP" else None,
        take_profit_threshold=str(p.take_profit_price)
        if p.exit_reason == "TAKE_PROFIT"
        else None,
        realized_pnl=str(p.realized_pnl),
        unrealized_pnl=str(p.unrealized_pnl),
        total_fees=str(p.entry_fee + p.exit_fee),
        return_pct=float(pnl / cost * 100) if cost else None,
        stop_price=str(p.stop_price),
        take_profit_price=str(p.take_profit_price) if p.take_profit_price else None,
        snapshot_id=snap.id if snap else None,
        snapshot_timestamp=iso_utc(snap.timestamp) if snap else None,
        market_score=snap.total_score if snap else None,
        score_version=text(snap.score_version) if snap else None,
        scanner_timeframe=snap.timeframe if snap else None,
        delta_1h=numeric(delta.get("delta_1h")),
        delta_4h=numeric(delta.get("delta_4h")),
        delta_24h=numeric(delta.get("delta_24h")),
        trend=text(f.get("trend")),
        strategy_action=s.action if s else None,
        signal_strength=str(s.signal_strength) if s else None,
        strategy_reasons=reasons(s.reasons) if s else [],
        risk_approved=r.approved if r else None,
        risk_amount=str(r.risk_amount) if r else None,
        risk_position_notional=str(r.position_notional) if r else None,
        risk_quantity=str(r.quantity) if r else None,
        risk_stop_distance_pct=str(r.stop_distance_pct) if r else None,
        risk_reasons=reasons(r.reasons) if r else [],
        portfolio_equity_at_decision=numeric(portfolio.get("equity")),
        available_cash_at_decision=str(
            Decimal(portfolio["cash_balance"]) - Decimal(portfolio["reserved_cash"])
        )
        if "cash_balance" in portfolio and "reserved_cash" in portfolio
        else None,
        current_exposure_at_decision=str(
            sum(
                (
                    Decimal(v["quantity"]) * Decimal(v["current_price"])
                    for v in positions
                ),
                Decimal(0),
            )
        )
        if r
        else None,
        open_positions_count_at_decision=len(positions) if r else None,
        paper_config_snapshot=config(s.configuration) if s else {},
        signal_id=s.id if s else None,
        exit_signal_id=xs.id if xs else None,
        entry_order_id=p.entry_order_id,
        exit_order_id=p.exit_order_id,
    )
    for name in (
        "algorithm_watch",
        "manual_trade_enabled",
        "entry_eligible",
        "watch_override",
    ):
        out[name] = text(permission.get(name))
    for name in (
        "ema50",
        "ema200",
        "rsi",
        "atr",
        "relative_volume",
        "spread",
        "trend_score",
        "momentum_score",
        "volume_score",
        "volatility_score",
        "liquidity_score",
    ):
        out[name] = getattr(snap, name) if snap else None
    for name in ("atr_pct", "momentum", "quote_volume", "volatility", "range_position"):
        out[name] = numeric(f.get(name))
    reference = Decimal(out["strategy_reference_price"]) if s else None
    ask = Decimal(str(out["entry_ask"])) if out["entry_ask"] is not None else None
    atr = Decimal(str(out["atr"])) if out["atr"] is not None else None
    out["entry_quote_drift_pct"] = (
        float((ask / reference - 1) * 100) if ask is not None and reference else None
    )
    out["entry_quote_drift_atr"] = (
        float((ask - reference) / atr)
        if ask is not None and reference is not None and atr and atr > 0
        else None
    )
    return out
