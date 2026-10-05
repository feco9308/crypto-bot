"""Internal ledger, not a wallet or order sender."""

from datetime import datetime

from sqlalchemy import select

from crypto_bot.storage.paper_models import (
    PaperAccount,
    PaperPosition,
    PortfolioSnapshot,
)
from crypto_bot.trading.domain import ZERO, D, PortfolioView, PositionView, q


class PortfolioService:
    def __init__(self, session):
        self.session = session

    @property
    def account(self):
        account = self.session.get(PaperAccount, 1)
        if account is None:
            raise RuntimeError("Paper account not initialized")
        return account

    def positions(self):
        return list(
            self.session.scalars(
                select(PaperPosition)
                .where(PaperPosition.status == "OPEN")
                .order_by(PaperPosition.id)
            )
        )

    def view(self):
        a = self.account
        positions = tuple(
            PositionView(
                p.id,
                p.symbol,
                p.quantity,
                p.entry_price,
                p.current_price,
                p.stop_price,
                p.entry_fee,
                p.strategy_name,
                p.take_profit_price,
            )
            for p in self.positions()
        )
        equity = q(a.cash_balance + sum((p.market_value for p in positions), ZERO))
        return PortfolioView(
            equity,
            a.cash_balance,
            a.reserved_cash,
            positions,
            a.realized_pnl,
            a.fees_paid,
            q(equity - a.day_start_equity),
            a.day_start_equity,
            a.peak_equity,
            a.max_drawdown,
        )

    def mark(self, prices, now, quote_audits=None):
        a = self.account
        # At UTC day rollover anchor previous marked equity (before the new price).
        if a.day != now.date().isoformat():
            a.day, a.day_start_equity = now.date().isoformat(), self.view().equity
        for p in self.positions():
            if p.symbol in prices:
                price = D(prices[p.symbol])
                if price <= 0:
                    raise ValueError("Invalid mark price")
                p.current_price = q(price)
                p.mark_quote = (quote_audits or {}).get(p.symbol)
                p.marked_at = (
                    datetime.fromisoformat(p.mark_quote["received_at"].rstrip("Z"))
                    if p.mark_quote
                    else None
                )
                p.unrealized_pnl = q(
                    q(p.current_price * p.quantity) - p.notional - p.entry_fee
                )
        self.session.flush()
        self.update_drawdown()

    def update_drawdown(self):
        a, v = self.account, self.view()
        a.peak_equity = max(a.peak_equity, v.equity)
        drawdown = (
            (a.peak_equity - v.equity) / a.peak_equity * D(100)
            if a.peak_equity > 0
            else ZERO
        )
        a.max_drawdown = q(max(a.max_drawdown, drawdown))

    def reserve(self, amount):
        amount = q(amount)
        if amount <= 0 or amount > self.view().available_cash:
            raise ValueError("Insufficient available cash for reservation")
        self.account.reserved_cash = q(self.account.reserved_cash + amount)

    def release(self, amount):
        amount = q(amount)
        if amount < 0 or amount > self.account.reserved_cash:
            raise ValueError("Invalid cash release")
        self.account.reserved_cash = q(self.account.reserved_cash - amount)

    def record_buy(self, order, signal, fill_price, fee, now):
        if any(p.symbol == order.symbol for p in self.positions()):
            raise ValueError("Duplicate long position")
        cost = q(order.quantity * fill_price) + fee
        self.release(order.reserved_amount)
        if cost > self.view().available_cash:
            raise ValueError("Insufficient cash at fill")
        a = self.account
        a.cash_balance = q(a.cash_balance - cost)
        a.fees_paid = q(a.fees_paid + fee)
        p = PaperPosition(
            symbol=order.symbol,
            entry_price=fill_price,
            quantity=order.quantity,
            notional=q(order.quantity * fill_price),
            stop_price=signal.stop_reference,
            take_profit_price=signal.take_profit_price,
            opened_at=now,
            current_price=order.reference_price,
            entry_fee=fee,
            unrealized_pnl=q(
                q(order.reference_price * order.quantity)
                - q(fill_price * order.quantity)
                - fee
            ),
            strategy_name=signal.strategy_name,
            signal_id=signal.id,
            entry_order_id=order.id,
            status="OPEN",
        )
        self.session.add(p)
        self.session.flush()
        self.update_drawdown()
        return p

    def record_sell(self, order, signal, fill_price, fee, now):
        p = next((p for p in self.positions() if p.symbol == order.symbol), None)
        if p is None or order.quantity != p.quantity:
            raise ValueError("Full existing long position required for SELL")
        a = self.account
        proceeds = q(order.quantity * fill_price) - fee
        pnl = q(proceeds - p.notional - p.entry_fee)
        a.cash_balance = q(a.cash_balance + proceeds)
        a.realized_pnl = q(a.realized_pnl + pnl)
        a.fees_paid = q(a.fees_paid + fee)
        (
            p.status,
            p.closed_at,
            p.exit_price,
            p.exit_fee,
            p.realized_pnl,
            p.unrealized_pnl,
            p.exit_reason,
            p.exit_order_id,
        ) = (
            "CLOSED",
            now,
            fill_price,
            fee,
            pnl,
            ZERO,
            signal.exit_reason or "STRATEGY",
            order.id,
        )
        p.current_price = fill_price
        self.session.flush()
        self.update_drawdown()
        return p

    def snapshot(self, now, quote_status=None):
        self.update_drawdown()
        v = self.view()
        self.session.add(
            PortfolioSnapshot(
                timestamp=now,
                total_equity=v.equity,
                cash_balance=v.cash_balance,
                reserved_cash=v.reserved_cash,
                available_cash=v.available_cash,
                realized_pnl=v.realized_pnl,
                unrealized_pnl=v.unrealized_pnl,
                fees_paid=v.fees_paid,
                exposure=v.exposure,
                daily_pnl=v.daily_pnl,
                max_drawdown=v.max_drawdown,
                open_positions=len(v.positions),
                quote_status=quote_status,
            )
        )
        return v
