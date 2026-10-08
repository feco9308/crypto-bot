"""Historical local fills through the unchanged portfolio bookkeeping methods."""

from types import SimpleNamespace

from crypto_bot.trading.domain import D, q

SOURCE = "BINANCE_PUBLIC_KLINES_APPROXIMATION"


class ReplayExecution:
    def __init__(self, ledger, settings):
        self.ledger = ledger
        self.settings = settings
        self.next_order = 1
        self.orders = []
        self.fills = []

    def fill(self, signal, decision, reference, now, interval, reason=None):
        side = signal.action
        if side not in ("BUY", "SELL") or not decision.approved:
            raise ValueError("Approved replay BUY/SELL required")
        reference = D(reference)
        factor = 1 + (self.settings.paper_slippage_pct / D(100)) * (
            1 if side == "BUY" else -1
        )
        price = q(reference * factor)
        notional = q(price * decision.quantity)
        fee = q(notional * self.settings.paper_fee_pct / D(100))
        order = SimpleNamespace(
            id=self.next_order,
            symbol=signal.symbol,
            quantity=decision.quantity,
            reference_price=reference,
            reserved_amount=q(notional + fee) if side == "BUY" else q(0),
        )
        self.next_order += 1
        local_signal = SimpleNamespace(
            id=order.id,
            stop_reference=signal.stop_reference,
            take_profit_price=signal.take_profit_price,
            strategy_name=signal.strategy_name,
            exit_reason=reason,
        )
        p = self.ledger.portfolio
        if side == "BUY":
            p.reserve(order.reserved_amount)
        position = (p.record_buy if side == "BUY" else p.record_sell)(
            order, local_signal, price, fee, now
        )
        audit = dict(
            order_id=order.id,
            position_id=position.id,
            symbol=signal.symbol,
            side=side,
            status="FILLED",
            signal_time=signal.timestamp.isoformat() + "Z",
            execution_time=now.isoformat() + "Z",
            reference_price=str(reference),
            fill_price=str(price),
            quantity=str(decision.quantity),
            notional=str(notional),
            fee=str(fee),
            slippage=str(q(abs(price - reference) * decision.quantity)),
            source_timeframe=interval,
            source=SOURCE,
            reason=reason or "STRATEGY",
            time_precision="CANDLE_BOUNDARY_APPROXIMATION",
        )
        self.orders.append(audit)
        self.fills.append(audit)
        return position, audit
