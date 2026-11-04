from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import delete, func, inspect, select
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
    SymbolTradePermission,
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

    def permissions(self, now=None):
        from crypto_bot.storage.models import Override
        from crypto_bot.trading.permissions import entry_permission

        now = now or utcnow()
        with self.database.session() as session:
            symbols = set(session.scalars(select(SymbolTradePermission.symbol)))
            symbols.update(session.scalars(select(Override.symbol)))
            symbols.update(
                r["symbol"]
                for r in self.database.latest_rows("1h")
                if r["algorithm_watch"]
            )
            return [
                dict(
                    symbol=symbol,
                    **entry_permission(
                        session, symbol, now, self.settings.max_snapshot_age_seconds
                    ),
                )
                for symbol in sorted(symbols)
            ]

    def set_manual_trade_enabled(self, symbol, enabled, now=None):
        from crypto_bot.storage.models import Instrument

        if type(enabled) is not bool:
            raise ValueError("Expected boolean permission")
        now = now or utcnow()
        with self.transaction() as session:
            instrument = session.get(Instrument, symbol)
            if (
                not instrument
                or instrument.quote_asset != self.settings.quote_asset
                or not instrument.spot
            ):
                raise ValueError("Unknown or incompatible Spot instrument")
            row = session.get(SymbolTradePermission, symbol)
            if row is None:
                row = SymbolTradePermission(
                    symbol=symbol, manual_trade_enabled=enabled, updated_at=now
                )
                session.add(row)
            else:
                row.manual_trade_enabled, row.updated_at = enabled, now
            from crypto_bot.trading.permissions import entry_permission

            if (
                not enabled
                and not entry_permission(
                    session, symbol, now, self.settings.max_snapshot_age_seconds
                )["entry_eligible"]
            ):
                from crypto_bot.execution.paper import PaperExecutionService

                execution = PaperExecutionService(
                    session, PortfolioService(session), self.settings
                )
                for order in session.scalars(
                    select(PaperOrder).where(
                        PaperOrder.symbol == symbol,
                        PaperOrder.status == "CREATED",
                        PaperOrder.side == "BUY",
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
            from crypto_bot.trading.profiles import S2_PROFILE

            if a.configuration.get("execution_profile") == S2_PROFILE:
                raise ValueError("S2 history cannot be reset destructively")
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
                stale_position_marks=[
                    p.symbol
                    for p in session.scalars(
                        select(PaperPosition).where(PaperPosition.status == "OPEN")
                    )
                    if not p.marked_at
                    or (utcnow() - p.marked_at).total_seconds()
                    > self.settings.max_quote_age_seconds
                ],
                configuration=a.configuration,
                forward=self.forward_status(session, a),
            )

    def activate_s2(self, provenance, confirmed=False, now=None):
        from crypto_bot.storage.database import iso_utc
        from crypto_bot.storage.paper_models import PaperGeneration
        from crypto_bot.trading.profiles import S2_PROFILE, S2_STRATEGY

        if (
            not confirmed
            or self.settings.execution_profile != S2_PROFILE
            or self.settings.strategy_name != S2_STRATEGY
        ):
            raise ValueError("Explicit confirmed S2 profile activation required")
        if (
            provenance.get("reason") != "USER_CHOSE_REALTIME_FORWARD_PAPER"
            or provenance.get("2026_final_oos_pristine") is not False
            or provenance.get("2026_final_oos_abandoned_for_forward_paper") is not True
        ):
            raise ValueError("Documented user OOS abandonment decision required")
        now = now or utcnow()
        with self.transaction() as session:
            p = PortfolioService(session)
            a = p.account
            if (
                a.enabled
                or p.positions()
                or a.reserved_cash
                or session.scalar(
                    select(PaperOrder.id)
                    .where(
                        PaperOrder.status.not_in(["FILLED", "CANCELLED", "REJECTED"])
                    )
                    .limit(1)
                )
            ):
                raise ValueError(
                    "STOP: turn old trading OFF; no open position/pending order/reservation allowed"
                )
            if a.lease_until and a.lease_until > now:
                raise ValueError("Stop paper service before S2 activation")
            if a.configuration.get("execution_profile") == S2_PROFILE:
                raise ValueError(
                    "S2 generation already activated; activation timestamp cannot be reset"
                )
            before = {
                c.name: (
                    str(getattr(a, c.name))
                    if c.name not in {"configuration"}
                    else a.configuration
                )
                for c in PaperAccount.__table__.columns
            }
            cutoffs = {
                m.__tablename__: session.scalar(select(func.max(m.id))) or 0
                for m in [
                    PaperPosition,
                    PaperOrder,
                    PaperFill,
                    SignalRecord,
                    RiskRecord,
                    PortfolioSnapshot,
                ]
            }
            if session.get(PaperGeneration, a.generation) is None:
                session.add(
                    PaperGeneration(
                        id=a.generation,
                        activated_at=a.created_at,
                        strategy_name=a.configuration.get("strategy_name", "legacy"),
                        execution_profile=a.configuration.get(
                            "execution_profile", "REFERENCE_STOP_RISK_V1"
                        ),
                        initial_balance=a.initial_balance,
                        status="ARCHIVED",
                        account_before=before,
                        history_cutoffs=cutoffs,
                        provenance={"history_retained": True},
                    )
                )
            a.generation += 1
            a.initial_balance = a.cash_balance = a.day_start_equity = a.peak_equity = q(
                1000
            )
            a.reserved_cash = a.realized_pnl = a.fees_paid = a.max_drawdown = ZERO
            a.created_at = now
            a.day = now.date().isoformat()
            a.lease_owner = a.lease_until = None
            a.configuration = self.settings.public_dict()
            a.enabled = True
            session.add(
                PaperGeneration(
                    id=a.generation,
                    activated_at=now,
                    strategy_name=S2_STRATEGY,
                    execution_profile=S2_PROFILE,
                    initial_balance=q(1000),
                    status="ACTIVE",
                    account_before=before,
                    history_cutoffs=cutoffs,
                    provenance=provenance,
                )
            )
            return provenance | dict(
                activated_at=iso_utc(now),
                strategy_name=S2_STRATEGY,
                execution_profile=S2_PROFILE,
                generation=a.generation,
                starting_equity="1000",
                starting_cash="1000",
                starting_state="FLAT",
                positions=0,
                first_eligible_source_candle_rule="Latest completed UTC daily candle with close_time strictly AFTER activated_at; no historical catch-up",
                no_historical_trade_backfill=True,
            )

    def forward_status(self, session, account):
        from crypto_bot.storage.database import iso_utc
        from crypto_bot.storage.paper_models import (
            ForwardDailyDecision,
            PaperGeneration,
        )
        from crypto_bot.trading.profiles import S2_PROFILE

        if account.configuration.get("execution_profile") != S2_PROFILE:
            return None
        generation = session.get(PaperGeneration, account.generation)
        if generation is None:
            return None
        latest = session.scalar(
            select(ForwardDailyDecision)
            .where(ForwardDailyDecision.generation_id == generation.id)
            .order_by(ForwardDailyDecision.source_close_ms.desc())
            .limit(1)
        )
        return dict(
            strategy_name=generation.strategy_name,
            execution_profile=generation.execution_profile,
            generation=generation.id,
            status=generation.status,
            state="LONG" if PortfolioService(session).positions() else "FLAT",
            activated_at=iso_utc(generation.activated_at),
            latest_indicators=generation.latest_indicators,
            latest_action=latest.action if latest else None,
            latest_decision_time=iso_utc(latest.decision_time) if latest else None,
            execution_status=latest.status
            if latest
            else "AWAITING_POST_ACTIVATION_CANDLE",
            last_error=generation.last_error,
            provenance=generation.provenance,
        )
