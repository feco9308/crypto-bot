from crypto_bot.trading.domain import ZERO, D, RiskDecision, q


class RiskManager:
    def __init__(self, settings):
        self.settings = settings

    def evaluate(self, signal, portfolio, trading_enabled=True):
        s, p = self.settings, portfolio

        def reject(reason):
            return RiskDecision(False, signal.symbol, reasons=(reason,))

        if signal.action == "SELL":
            position = next(
                (pos for pos in p.positions if pos.symbol == signal.symbol), None
            )
            if position is None:
                return reject("no open long position")
            return RiskDecision(
                True,
                signal.symbol,
                position_notional=q(position.quantity * signal.entry_reference),
                quantity=position.quantity,
                reasons=("long position exit permitted",),
            )
        if signal.action != "BUY":
            return reject("HOLD; no order requested")
        if not trading_enabled:
            return reject("paper trading OFF")
        if any(pos.symbol == signal.symbol for pos in p.positions):
            return reject("duplicate open position; pyramiding disabled")
        if len(p.positions) >= s.max_open_positions:
            return reject("max open positions reached")
        if (
            p.day_start_equity > 0
            and p.daily_pnl <= -p.day_start_equity * s.daily_loss_limit_pct / D(100)
        ):
            return reject("daily loss limit reached")
        current_drawdown = (
            (p.peak_equity - p.equity) / p.peak_equity * D(100)
            if p.peak_equity > 0
            else ZERO
        )
        if max(current_drawdown, p.max_drawdown) >= s.max_drawdown_limit_pct:
            return reject("drawdown limit reached")
        if p.equity <= 0:
            return reject("nonpositive portfolio equity")
        entry = signal.entry_reference * (1 + s.paper_slippage_pct / D(100))
        stop = signal.stop_reference
        if stop is None or not 0 < stop < entry:
            return reject("invalid stop distance")
        # Includes anticipated entry/exit costs and adverse exit slippage in risk budget.
        exit_fill = stop * (1 - s.paper_slippage_pct / D(100))
        fee = s.paper_fee_pct / D(100)
        loss_per_unit = entry - exit_fill + (entry + exit_fill) * fee
        risk_budget = p.equity * s.risk_per_trade_pct / D(100)
        requested = risk_budget / loss_per_unit
        total_room = p.equity * s.max_total_exposure_pct / D(100) - p.exposure
        symbol_exposure = sum(
            (pos.market_value for pos in p.positions if pos.symbol == signal.symbol),
            ZERO,
        )
        symbol_room = p.equity * s.max_symbol_exposure_pct / D(100) - symbol_exposure
        if total_room <= 0:
            return reject("total exposure limit reached")
        if symbol_room <= 0:
            return reject("symbol exposure limit reached")
        if p.available_cash <= 0:
            return reject("insufficient free balance")
        quantity = q(
            min(
                requested,
                total_room / (entry * (1 + fee)),
                symbol_room / (entry * (1 + fee)),
                p.available_cash / (entry * (1 + fee)),
            )
        )
        notional = q(quantity * entry)
        if quantity <= 0 or notional < s.minimum_order_value:
            return reject(
                "sized order below minimum value or insufficient free balance/exposure room"
            )
        risk = q(quantity * loss_per_unit)
        reasons = ["approved within all limits"]
        if quantity < requested:
            reasons.append("size capped by exposure or available cash")
        return RiskDecision(
            True,
            signal.symbol,
            risk,
            notional,
            quantity,
            q((entry - stop) / entry * D(100)),
            tuple(reasons),
        )
