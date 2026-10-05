"""Consumes persisted scanner data only. No Binance or other network imports."""

import logging
from dataclasses import asdict, replace

from sqlalchemy import select

from crypto_bot.execution.paper import PaperExecutionService
from crypto_bot.portfolio.service import PortfolioService
from crypto_bot.risk.manager import RiskManager
from crypto_bot.scanner.selection import effective_state
from crypto_bot.storage.database import utcnow
from crypto_bot.storage.models import (
    Decision,
    Instrument,
    Override,
    ScannerRun,
    Snapshot,
)
from crypto_bot.storage.paper_models import (
    PaperPosition,
    ProcessedSnapshot,
    RiskRecord,
    SignalRecord,
)
from crypto_bot.trading.domain import D, MarketContext, StrategySignal
from crypto_bot.trading.strategy import ReferenceStrategy, StrategyEngine

log = logging.getLogger(__name__)


def portfolio_audit(view):
    return {k: str(v) for k, v in asdict(view).items() if k != "positions"} | {
        "positions": [
            {
                "id": p.id,
                "symbol": p.symbol,
                "quantity": str(p.quantity),
                "current_price": str(p.current_price),
            }
            for p in view.positions
        ]
    }


class PaperEngine:
    def __init__(
        self,
        repository,
        scanner_settings,
        strategy=None,
        execution_factory=PaperExecutionService,
    ):
        self.repository, self.settings, self.scanner_settings = (
            repository,
            repository.settings,
            scanner_settings,
        )
        self.strategy = StrategyEngine(strategy or ReferenceStrategy(self.settings))
        self.risk = RiskManager(self.settings)
        self.execution_factory = execution_factory

    def contexts(self, session, now):
        run = session.scalar(
            select(ScannerRun)
            .where(
                ScannerRun.timeframe == self.scanner_settings.timeframe,
                ScannerRun.status.in_(["complete", "partial"]),
            )
            .order_by(ScannerRun.id.desc())
            .limit(1)
        )
        if run is None:
            return []
        snapshots = {
            s.symbol: s
            for s in session.scalars(select(Snapshot).where(Snapshot.run_id == run.id))
        }
        for position in session.scalars(
            select(PaperPosition).where(PaperPosition.status == "OPEN")
        ):
            if position.symbol not in snapshots:
                old = session.scalar(
                    select(Snapshot)
                    .where(
                        Snapshot.symbol == position.symbol,
                        Snapshot.timeframe == self.scanner_settings.timeframe,
                    )
                    .order_by(Snapshot.timestamp.desc())
                    .limit(1)
                )
                if old:
                    snapshots[position.symbol] = old
        result = []
        for symbol, snapshot in sorted(snapshots.items()):
            instrument = session.get(Instrument, symbol)
            if not instrument or instrument.quote_asset != self.settings.quote_asset:
                continue
            if (
                snapshot.timestamp > now
                or (now - snapshot.timestamp).total_seconds()
                > self.settings.max_snapshot_age_seconds
            ):
                continue
            f = snapshot.features
            override = session.get(Override, symbol)
            decision = session.scalar(
                select(Decision).where(
                    Decision.run_id == snapshot.run_id, Decision.symbol == symbol
                )
            )
            auto = bool(decision and decision.algorithm_watch)
            delta = snapshot.score_momentum.get(f"delta_{self.settings.delta_window}h")
            # No rescaling/recomputation of any scanner score.
            if any(
                f.get(k) is None
                for k in ["rsi", "ema50", "ema200", "atr", "relative_volume"]
            ):
                continue
            try:
                result.append(
                    MarketContext(
                        snapshot.id,
                        symbol,
                        snapshot.timestamp,
                        D(snapshot.price),
                        D(snapshot.total_score),
                        D(delta) if delta is not None else None,
                        D(f["rsi"]),
                        D(f["ema50"]),
                        D(f["ema200"]),
                        D(f["atr"]),
                        D(f["relative_volume"]),
                        effective_state(auto, override.status if override else "AUTO"),
                        auto,
                        f
                        | {
                            "instrument_active": instrument.active,
                            "instrument_spot": instrument.spot,
                        },
                    )
                )
            except ValueError:
                log.warning(
                    "PAPER_INVALID_SNAPSHOT",
                    extra={"event_data": {"snapshot_id": snapshot.id}},
                )
        return result

    def apply_signal(self, session, signal, portfolio, now, exit_reason=None):
        record = SignalRecord(
            snapshot_id=signal.snapshot_id,
            timestamp=now,
            symbol=signal.symbol,
            action=signal.action,
            signal_strength=signal.signal_strength,
            entry_reference=signal.entry_reference,
            stop_reference=signal.stop_reference,
            take_profit_price=signal.take_profit_price,
            strategy_name=signal.strategy_name,
            reasons=list(signal.reasons),
            configuration=self.settings.public_dict(),
            exit_reason=exit_reason,
        )
        session.add(record)
        session.flush()
        log.info(
            "STRATEGY_SIGNAL",
            extra={
                "event_data": {
                    "symbol": signal.symbol,
                    "action": signal.action,
                    "strength": str(signal.signal_strength),
                }
            },
        )
        view = portfolio.view()
        decision = self.risk.evaluate(signal, view, portfolio.account.enabled)
        risk = RiskRecord(
            signal_id=record.id,
            approved=decision.approved,
            risk_amount=decision.risk_amount,
            position_notional=decision.position_notional,
            quantity=decision.quantity,
            stop_distance_pct=decision.stop_distance_pct,
            reasons=list(decision.reasons),
            portfolio=portfolio_audit(view),
        )
        session.add(risk)
        session.flush()
        log.info(
            "RISK_APPROVED" if decision.approved else "RISK_REJECTED",
            extra={
                "event_data": {
                    "symbol": signal.symbol,
                    "risk": str(decision.risk_amount),
                    "position": str(decision.position_notional),
                    "reasons": decision.reasons,
                }
            },
        )
        # Rejected BUY/SELL orders are auditable too; HOLD has no order.
        if signal.action != "HOLD":
            return self.execution_factory(
                session, portfolio, self.settings
            ).place_order(record.id, risk.id, now)
        return None

    def run_once(self, now=None, lease_owner=None):
        now = now or utcnow()
        result = {"processed": 0, "orders": 0, "fresh_markets": 0}
        with self.repository.transaction() as session:
            portfolio = PortfolioService(session)
            a = portfolio.account
            if (
                a.lease_owner
                and a.lease_until
                and a.lease_until > now
                and a.lease_owner != lease_owner
            ):
                raise RuntimeError("Account owned by another paper engine")
            a.configuration = self.settings.public_dict()
            contexts = self.contexts(session, now)
            result["fresh_markets"] = len(contexts)
            portfolio.mark({c.symbol: c.price for c in contexts}, now)
            # Risk-reducing exits run first and remain active while trading OFF.
            exited = set()
            for position in list(portfolio.positions()):
                context = next(
                    (c for c in contexts if c.symbol == position.symbol), None
                )
                if context is None:
                    continue
                reason = (
                    "STOP"
                    if context.price <= position.stop_price
                    else "TAKE_PROFIT"
                    if position.take_profit_price
                    and context.price >= position.take_profit_price
                    else None
                )
                if reason:
                    signal = StrategySignal(
                        position.symbol,
                        "SELL",
                        D(1),
                        context.price,
                        None,
                        now,
                        position.strategy_name,
                        (f"Application-level {reason} at observed scanner price",),
                        context.snapshot_id,
                    )
                    order = self.apply_signal(session, signal, portfolio, now, reason)
                    if order.status == "FILLED":
                        exited.add(position.symbol)
                    result["orders"] += 1
            for context in contexts:
                if session.get(ProcessedSnapshot, context.snapshot_id) is not None:
                    continue
                session.add(
                    ProcessedSnapshot(snapshot_id=context.snapshot_id, timestamp=now)
                )
                result["processed"] += 1
                if context.symbol in exited:
                    continue  # never re-enter in a closing cycle
                has_position = any(
                    p.symbol == context.symbol for p in portfolio.positions()
                )
                if not has_position and (
                    not context.features["instrument_active"]
                    or not context.features["instrument_spot"]
                ):
                    continue
                signal = replace(
                    self.strategy.evaluate(context, has_position), timestamp=now
                )
                order = self.apply_signal(
                    session,
                    signal,
                    portfolio,
                    now,
                    "STRATEGY" if signal.action == "SELL" else None,
                )
                if order:
                    result["orders"] += 1
            portfolio.snapshot(now)
        return result

    def manual_close(self, position_id, now=None):
        now = now or utcnow()
        with self.repository.transaction() as session:
            position = session.get(PaperPosition, position_id)
            if position is None or position.status != "OPEN":
                raise ValueError("Position is not open")
            context = next(
                (c for c in self.contexts(session, now) if c.symbol == position.symbol),
                None,
            )
            if context is None:
                raise ValueError(
                    "Fresh scanner price unavailable; manual close refused"
                )
            portfolio = PortfolioService(session)
            portfolio.mark({context.symbol: context.price}, now)
            signal = StrategySignal(
                context.symbol,
                "SELL",
                D(1),
                context.price,
                None,
                now,
                position.strategy_name,
                ("Manual paper position close",),
                context.snapshot_id,
            )
            order = self.apply_signal(session, signal, portfolio, now, "MANUAL")
            portfolio.snapshot(now)
            return order.id
