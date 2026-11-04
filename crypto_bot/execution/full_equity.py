"""Explicit full-equity/no-stop PAPER execution. No exchange client or RiskManager."""

from sqlalchemy import select

from crypto_bot.storage.database import iso_utc
from crypto_bot.storage.paper_models import (
    PaperFill,
    PaperOrder,
    RiskRecord,
    SignalRecord,
)
from crypto_bot.trading.domain import ZERO, D, q
from crypto_bot.trading.profiles import (
    S2_PROFILE,
    S2_STRATEGY,
    S2_SYMBOL,
    ForwardSafetyError,
)

REASON = "Frozen S2 full-equity paper allocation; no stop"


def full_equity_quantity(cash, reference, settings):
    fill = q(D(reference) * (1 + settings.paper_slippage_pct / D(100)))
    quantity = q(D(cash) / (fill * (1 + settings.paper_fee_pct / D(100))))
    notional = q(quantity * fill)
    fee = q(notional * settings.paper_fee_pct / D(100))
    if quantity <= 0 or notional + fee > cash:
        raise ForwardSafetyError("Full-equity allocation cannot borrow cash")
    return quantity, fill, notional, fee


class FullEquityPaperExecution:
    def __init__(self, session, portfolio, settings, quotes=None):
        self.session, self.portfolio, self.settings = session, portfolio, settings
        self.quotes = quotes or {}

    def place_order(self, signal_id, risk_id, now):
        signal = self.session.get(SignalRecord, signal_id)
        risk = self.session.get(RiskRecord, risk_id)
        account = self.portfolio.account
        if (
            self.settings.execution_profile != S2_PROFILE
            or account.configuration.get("execution_profile") != S2_PROFILE
            or signal is None
            or risk is None
            or risk.signal_id != signal_id
            or not risk.approved
            or signal.symbol != S2_SYMBOL
            or signal.strategy_name != S2_STRATEGY
            or signal.stop_reference is not None
            or signal.take_profit_price is not None
            or signal.action not in {"BUY", "SELL"}
        ):
            raise ForwardSafetyError("Invalid frozen S2 execution profile/audit link")
        existing = self.session.scalar(
            select(PaperOrder).where(PaperOrder.signal_id == signal_id)
        )
        if existing:
            if existing.status != "FILLED":
                raise ForwardSafetyError("Unexpected outstanding/rejected S2 order")
            return existing
        quote = self.quotes.get(S2_SYMBOL)
        if (
            not quote
            or not quote.fresh(now, self.settings.max_quote_age_seconds)
            or quote.received_at < signal.timestamp
        ):
            raise ForwardSafetyError("S2 execution quote missing/stale/pre-decision")
        positions = self.portfolio.positions()
        if (
            account.cash_balance < 0
            or account.reserved_cash != 0
            or any(
                p.symbol != S2_SYMBOL
                or p.strategy_name != S2_STRATEGY
                or p.stop_price is not None
                or p.take_profit_price is not None
                for p in positions
            )
        ):
            raise ForwardSafetyError(
                "Unexpected position, reservation or negative cash"
            )
        side = signal.action
        reference = quote.price(side)
        if side == "BUY":
            if not account.enabled or positions:
                raise ForwardSafetyError("Disabled/duplicate S2 BUY")
            quantity, price, notional, fee = full_equity_quantity(
                self.portfolio.view().available_cash, reference, self.settings
            )
        else:
            if len(positions) != 1:
                raise ForwardSafetyError("S2 SELL requires one full BTC position")
            quantity = positions[0].quantity
            price = q(reference * (1 - self.settings.paper_slippage_pct / D(100)))
            notional = q(price * quantity)
            fee = q(notional * self.settings.paper_fee_pct / D(100))
        if risk.quantity != quantity:
            raise ForwardSafetyError("S2 allocation changed before fill")
        order = PaperOrder(
            signal_id=signal_id,
            risk_id=risk_id,
            symbol=S2_SYMBOL,
            side=side,
            status="CREATED",
            quantity=quantity,
            reference_price=reference,
            quote=quote.audit()
            | dict(execution_profile=S2_PROFILE, generation=account.generation),
            created_at=now,
            updated_at=now,
            reserved_amount=ZERO,
            reason=REASON,
        )
        self.session.add(order)
        self.session.flush()
        if side == "BUY":
            self.portfolio.reserve(notional + fee)
            order.reserved_amount = notional + fee
        position = (
            self.portfolio.record_buy if side == "BUY" else self.portfolio.record_sell
        )(order, signal, price, fee, now)
        self.portfolio.mark({S2_SYMBOL: quote.bid}, now, {S2_SYMBOL: quote.audit()})
        self.session.add(
            PaperFill(
                order_id=order.id,
                position_id=position.id,
                timestamp=now,
                price=price,
                quantity=quantity,
                notional=notional,
                fee=fee,
                slippage=q(abs(price - reference) * quantity),
            )
        )
        order.status = "FILLED"
        order.updated_at = now
        order.quote = order.quote | dict(
            signal_decision_time=iso_utc(signal.timestamp),
            quote_received_time=iso_utc(quote.received_at),
            fill_time=iso_utc(now),
            decision_to_fill_latency_ms=max(
                0, (now - signal.timestamp).total_seconds() * 1000
            ),
            spread_pct=str((quote.ask - quote.bid) / quote.bid * 100),
            slippage_pct=str(self.settings.paper_slippage_pct),
            fee_pct=str(self.settings.paper_fee_pct),
            fee=str(fee),
            slippage=str(q(abs(price - reference) * quantity)),
        )
        if account.cash_balance < 0:
            raise ForwardSafetyError("Negative cash after S2 fill")
        return order
