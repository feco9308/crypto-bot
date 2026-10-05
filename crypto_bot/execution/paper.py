"""Pure local market execution. No network client, credentials or live adapter."""

import logging
from dataclasses import replace

from sqlalchemy import select

from crypto_bot.risk.manager import RiskManager
from crypto_bot.storage.paper_models import (
    PaperFill,
    PaperOrder,
    RiskRecord,
    SignalRecord,
)
from crypto_bot.trading.domain import ZERO, D, q
from crypto_bot.trading.permissions import entry_permission

log = logging.getLogger(__name__)


class PaperExecutionService:
    def __init__(self, session, portfolio, settings, quotes=None):
        self.session, self.portfolio, self.settings = session, portfolio, settings
        self.quotes = quotes or {}

    def current_quote(self, symbol, now):
        quote = self.quotes.get(symbol)
        return (
            quote
            if quote
            and quote.symbol == symbol
            and quote.fresh(now, self.settings.max_quote_age_seconds)
            else None
        )

    def entry_allowed(self, symbol, now):
        return entry_permission(
            self.session, symbol, now, self.settings.max_snapshot_age_seconds
        )["entry_eligible"]

    def marks_complete(self, now):
        return all(
            p.mark_quote
            and p.marked_at
            and 0
            <= (now - p.marked_at).total_seconds()
            <= self.settings.max_quote_age_seconds
            for p in self.portfolio.positions()
        )

    def create_order(self, signal_id, risk_id, now):
        signal = self.session.get(SignalRecord, signal_id)
        risk = self.session.get(RiskRecord, risk_id)
        if signal is None or risk is None or risk.signal_id != signal_id:
            raise ValueError("Invalid signal/risk audit link")
        existing = self.session.scalar(
            select(PaperOrder).where(PaperOrder.signal_id == signal_id)
        )
        if existing:
            return existing
        quote = self.current_quote(signal.symbol, now)
        order = PaperOrder(
            signal_id=signal_id,
            risk_id=risk_id,
            symbol=signal.symbol,
            side=signal.action,
            status="CREATED",
            quantity=risk.quantity,
            reference_price=quote.price(signal.action) if quote else ZERO,
            quote=quote.audit() if quote else None,
            created_at=now,
            updated_at=now,
            reserved_amount=ZERO,
            reason="",
        )
        self.session.add(order)
        self.session.flush()
        reason = None
        if (
            not risk.approved
            or signal.action not in {"BUY", "SELL"}
            or risk.quantity <= 0
        ):
            reason = (
                "; ".join(risk.reasons) or "Risk not approved for executable signal"
            )
        elif quote is None:
            reason = "Fresh public quote unavailable"
        elif signal.action == "BUY" and not self.entry_allowed(signal.symbol, now):
            reason = "No entry permission; watchlist is observation only"
        elif signal.action == "BUY" and not self.marks_complete(now):
            reason = "Fresh portfolio marks unavailable"
        elif signal.action == "BUY" and not self.portfolio.account.enabled:
            reason = "paper trading OFF"
        elif signal.action == "BUY" and any(
            p.symbol == signal.symbol for p in self.portfolio.positions()
        ):
            reason = "duplicate open position"
        elif signal.action == "SELL" and not any(
            p.symbol == signal.symbol and p.quantity == risk.quantity
            for p in self.portfolio.positions()
        ):
            reason = "no matching long position"
        if not reason and signal.action == "BUY":
            if (
                signal.stop_reference is None
                or not 0 < signal.stop_reference < order.reference_price
            ):
                reason = "invalid BUY stop"
            else:
                price = q(
                    order.reference_price
                    * (1 + self.settings.paper_slippage_pct / D(100))
                )
                amount = q(risk.quantity * price) + q(
                    q(risk.quantity * price) * self.settings.paper_fee_pct / D(100)
                )
                try:
                    self.portfolio.reserve(amount)
                    order.reserved_amount = amount
                except ValueError:
                    reason = "insufficient free balance"
        if reason:
            order.status, order.reason = "REJECTED", reason
        return order

    def fill_order(self, order_id, now):
        order = self.get_order(order_id)
        if order is None:
            raise KeyError(order_id)
        if order.status != "CREATED":
            return order
        signal = self.session.get(SignalRecord, order.signal_id)
        quote = self.current_quote(order.symbol, now)
        if quote is None or (
            order.side == "BUY"
            and (
                not self.entry_allowed(order.symbol, now)
                or not self.marks_complete(now)
            )
        ):
            self.portfolio.release(order.reserved_amount)
            order.status, order.reason, order.updated_at = (
                "REJECTED",
                "Fresh quote/marks or entry permission unavailable at fill",
                now,
            )
            return order
        order.reference_price, order.quote = quote.price(order.side), quote.audit()
        if order.side == "BUY" and (
            not self.portfolio.account.enabled
            or any(p.symbol == order.symbol for p in self.portfolio.positions())
        ):
            self.portfolio.release(order.reserved_amount)
            order.status, order.reason, order.updated_at = (
                "REJECTED",
                "disabled or duplicate position at fill",
                now,
            )
            return order
        if order.side == "BUY":
            view = self.portfolio.view()
            view = replace(
                view, reserved_cash=view.reserved_cash - order.reserved_amount
            )
            current_risk = RiskManager(self.settings).evaluate(
                signal,
                view,
                self.portfolio.account.enabled,
                entry_price=order.reference_price,
            )
            if not current_risk.approved or order.quantity > current_risk.quantity:
                self.portfolio.release(order.reserved_amount)
                order.status, order.reason, order.updated_at = (
                    "REJECTED",
                    "Risk limits changed before fill",
                    now,
                )
                return order
        elif not any(
            p.symbol == order.symbol and p.quantity == order.quantity
            for p in self.portfolio.positions()
        ):
            order.status, order.reason, order.updated_at = (
                "REJECTED",
                "Long position no longer available",
                now,
            )
            return order
        factor = (
            1 + self.settings.paper_slippage_pct / D(100)
            if order.side == "BUY"
            else 1 - self.settings.paper_slippage_pct / D(100)
        )
        price = q(order.reference_price * factor)
        notional = q(price * order.quantity)
        fee = q(notional * self.settings.paper_fee_pct / D(100))
        # Ledger methods never send an order; execution is the only caller applying fills.
        position = (
            self.portfolio.record_buy
            if order.side == "BUY"
            else self.portfolio.record_sell
        )(order, signal, price, fee, now)
        if order.side == "BUY":
            self.portfolio.mark(
                {order.symbol: quote.bid}, now, {order.symbol: quote.audit()}
            )
        self.session.add(
            PaperFill(
                order_id=order.id,
                position_id=position.id,
                timestamp=now,
                price=price,
                quantity=order.quantity,
                notional=notional,
                fee=fee,
                slippage=q(abs(price - order.reference_price) * order.quantity),
            )
        )
        order.status, order.updated_at = "FILLED", now
        log.info(
            "PAPER_ORDER_FILLED",
            extra={
                "event_data": {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": str(order.quantity),
                    "price": str(price),
                    "fee": str(fee),
                }
            },
        )
        if order.side == "SELL":
            log.info(
                "POSITION_CLOSED",
                extra={
                    "event_data": {
                        "symbol": order.symbol,
                        "pnl": str(position.realized_pnl),
                        "reason": position.exit_reason,
                    }
                },
            )
        return order

    def place_order(self, signal_id, risk_id, now):
        order = self.create_order(signal_id, risk_id, now)
        return self.fill_order(order.id, now)

    def cancel_order(self, order_id, now):
        order = self.get_order(order_id)
        if order is None:
            raise KeyError(order_id)
        if order.status == "CREATED":
            if order.reserved_amount:
                self.portfolio.release(order.reserved_amount)
            order.status, order.updated_at = "CANCELLED", now
        return order

    def get_order(self, order_id):
        return self.session.get(PaperOrder, order_id)

    def get_positions(self):
        return self.portfolio.positions()
