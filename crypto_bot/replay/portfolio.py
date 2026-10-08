"""Run-local memory session for the UNCHANGED production PortfolioService.

Only PaperAccount(1) and the OPEN position query are supported. There is no DB
connection and no production account/permission/order sender in this adapter.
"""

from crypto_bot.portfolio.service import PortfolioService
from crypto_bot.storage.paper_models import PaperAccount, PaperPosition
from crypto_bot.trading.domain import ZERO


class MemoryLedger:
    def __init__(self, settings, now):
        balance = settings.initial_paper_balance
        self.account = PaperAccount(
            id=1,
            generation=1,
            quote_asset="USDT",
            initial_balance=balance,
            cash_balance=balance,
            reserved_cash=ZERO,
            realized_pnl=ZERO,
            fees_paid=ZERO,
            enabled=True,
            created_at=now,
            day=now.date().isoformat(),
            day_start_equity=balance,
            peak_equity=balance,
            max_drawdown=ZERO,
            configuration=settings.public_dict(),
        )
        self.all_positions = []
        self.next_position = 1
        self.portfolio = PortfolioService(self)

    def get(self, model, key):
        if model is not PaperAccount or key != 1:
            raise ValueError("Only isolated replay account is supported")
        return self.account

    def scalars(self, statement):
        descriptions = statement.column_descriptions
        if len(descriptions) != 1 or descriptions[0]["entity"] is not PaperPosition:
            raise ValueError("Only the portfolio OPEN position query is supported")
        return [p for p in self.all_positions if p.status == "OPEN"]

    def add(self, position):
        if not isinstance(position, PaperPosition):
            raise ValueError("Only memory positions are supported")
        if position.id is None:
            position.id = self.next_position
            self.next_position += 1
        position.side = "LONG"
        position.exit_fee = ZERO
        position.realized_pnl = ZERO
        self.all_positions.append(position)

    def flush(self):
        pass

    def checkpoint(self):
        from sqlalchemy import inspect

        def record(obj):
            return {c.key: getattr(obj, c.key) for c in inspect(type(obj)).columns}

        return dict(
            account=record(self.account),
            positions=[
                record(p)
                | {
                    k: getattr(p, k)
                    for k in (
                        "highest",
                        "overlay_active",
                        "original_stop",
                        "mfe_price",
                        "mae_price",
                        "slippage_total",
                        "entry_data",
                    )
                }
                for p in self.all_positions
                if p.status == "OPEN"
            ],
            next_position=self.next_position,
        )

    def restore(self, state):
        from datetime import datetime

        from crypto_bot.trading.domain import D

        account_money = (
            "initial_balance",
            "cash_balance",
            "reserved_cash",
            "realized_pnl",
            "fees_paid",
            "day_start_equity",
            "peak_equity",
            "max_drawdown",
        )
        for k, v in state["account"].items():
            if k in account_money:
                v = D(v)
            elif k in ("created_at", "lease_until") and v:
                v = datetime.fromisoformat(v)
            setattr(self.account, k, v)
        self.all_positions = []
        self.next_position = state["next_position"]
        for row in state["positions"]:
            p = PaperPosition()
            for k, v in row.items():
                if (
                    k
                    in (
                        "entry_price",
                        "quantity",
                        "notional",
                        "stop_price",
                        "take_profit_price",
                        "current_price",
                        "entry_fee",
                        "exit_fee",
                        "unrealized_pnl",
                        "realized_pnl",
                        "highest",
                        "original_stop",
                        "mfe_price",
                        "mae_price",
                        "slippage_total",
                    )
                    and v is not None
                ):
                    v = D(v)
                elif k in ("opened_at", "closed_at", "marked_at") and v:
                    v = datetime.fromisoformat(v)
                setattr(p, k, v)
            self.all_positions.append(p)
