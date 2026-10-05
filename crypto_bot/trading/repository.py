from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import delete, inspect, select
from sqlalchemy.orm import Session

from crypto_bot.portfolio.service import PortfolioService
from crypto_bot.storage.database import utcnow
from crypto_bot.storage.paper_models import (
    PaperAccount,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PortfolioSnapshot,
    ProcessedSnapshot,
    RiskRecord,
    SignalRecord,
)
from crypto_bot.trading.domain import ZERO, q


class PaperRepository:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings

    def available(self):
        return inspect(self.database.engine).has_table("paper_account")

    @contextmanager
    def transaction(self):
        with self.database.engine.connect() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.begin()
            session = Session(bind=connection, expire_on_commit=False)
            try:
                # Serialize control, execution and account changes on every backend.
                if connection.dialect.name != "sqlite":
                    session.execute(
                        select(PaperAccount)
                        .where(PaperAccount.id == 1)
                        .with_for_update()
                    )
                yield session
                session.flush()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                session.close()

    def initialize_account(self, now=None):
        now = now or utcnow()
        if not self.available():
            raise RuntimeError("Run market-scanner migrate before paper initialization")
        with self.transaction() as session:
            a = session.get(PaperAccount, 1)
            if a is None:
                balance = q(self.settings.initial_paper_balance)
                session.add(
                    PaperAccount(
                        id=1,
                        quote_asset=self.settings.quote_asset,
                        initial_balance=balance,
                        cash_balance=balance,
                        created_at=now,
                        day=now.date().isoformat(),
                        day_start_equity=balance,
                        peak_equity=balance,
                        configuration=self.settings.public_dict(),
                        enabled=False,
                    )
                )
            elif a.quote_asset != self.settings.quote_asset:
                raise ValueError(
                    "Account quote asset differs from configuration; reset explicitly"
                )

    def set_enabled(self, enabled, now=None):
        now = now or utcnow()
        with self.transaction() as session:
            a = session.get(PaperAccount, 1)
            if a is None:
                raise RuntimeError("Paper account not initialized")
            a.enabled = bool(enabled)
            if not enabled:
                from crypto_bot.execution.paper import PaperExecutionService

                execution = PaperExecutionService(
                    session, PortfolioService(session), self.settings
                )
                for order in session.scalars(
                    select(PaperOrder).where(
                        PaperOrder.status == "CREATED", PaperOrder.side == "BUY"
                    )
                ):
                    execution.cancel_order(order.id, now)

    def lease(self, owner, now, duration):
        with self.transaction() as session:
            a = session.get(PaperAccount, 1)
            if (
                a.lease_owner not in {None, owner}
                and a.lease_until
                and a.lease_until > now
            ):
                raise RuntimeError("Another paper engine holds the account lease")
            a.lease_owner, a.lease_until = owner, now + timedelta(seconds=duration)

    def release_lease(self, owner):
        with self.transaction() as session:
            a = session.get(PaperAccount, 1)
            if a and a.lease_owner == owner:
                a.lease_owner, a.lease_until = None, None

    def reset(self, confirmed=False, now=None):
        if not confirmed:
            raise ValueError("Reset requires --confirm RESET-PAPER")
        now = now or utcnow()
        with self.transaction() as session:
            a = session.get(PaperAccount, 1)
            if a is None:
                raise RuntimeError("Paper account not initialized")
            if a.enabled:
                raise ValueError("Turn paper trading OFF before reset")
            if a.lease_until and a.lease_until > now:
                raise ValueError("Stop paper engine before reset")
            # Dependencies removed in FK-safe order; scanner tables never touched.
            for model in [
                PaperFill,
                PaperPosition,
                PaperOrder,
                RiskRecord,
                SignalRecord,
                PortfolioSnapshot,
                ProcessedSnapshot,
            ]:
                session.execute(delete(model))
            balance = q(self.settings.initial_paper_balance)
            a.generation += 1
            a.quote_asset, a.initial_balance, a.cash_balance = (
                self.settings.quote_asset,
                balance,
                balance,
            )
            a.reserved_cash, a.realized_pnl, a.fees_paid, a.max_drawdown = (
                ZERO,
                ZERO,
                ZERO,
                ZERO,
            )
            a.day, a.day_start_equity, a.peak_equity = (
                now.date().isoformat(),
                balance,
                balance,
            )
            a.configuration = self.settings.public_dict()
            a.created_at = now
            a.lease_owner, a.lease_until = None, None

    def status(self):
        if not self.available():
            return {"installed": False}
        with self.database.session() as session:
            a = session.get(PaperAccount, 1)
            if a is None:
                return {"installed": True, "initialized": False}
            v = PortfolioService(session).view()
            return dict(
                installed=True,
                initialized=True,
                enabled=a.enabled,
                quote_asset=a.quote_asset,
                generation=a.generation,
                total_equity=str(v.equity),
                cash_balance=str(v.cash_balance),
                reserved_cash=str(v.reserved_cash),
                available_cash=str(v.available_cash),
                realized_pnl=str(v.realized_pnl),
                unrealized_pnl=str(v.unrealized_pnl),
                daily_pnl=str(v.daily_pnl),
                fees_paid=str(v.fees_paid),
                current_exposure=str(v.exposure),
                max_drawdown=str(v.max_drawdown),
                open_positions=len(v.positions),
                configuration=a.configuration,
            )
