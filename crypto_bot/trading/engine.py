"""Closed 1h strategy inputs; independent public quotes for marks and paper fills."""

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
    SymbolTradePermission,
)
from crypto_bot.trading.domain import D, MarketContext, RiskDecision, StrategySignal
from crypto_bot.trading.permissions import entry_permission
from crypto_bot.trading.quotes import BinancePublicQuotes, MarketQuote
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
        quote_provider=None,
    ):
        self.repository, self.settings, self.scanner_settings = (
            repository,
            repository.settings,
            scanner_settings,
        )
        self.strategy = StrategyEngine(strategy or ReferenceStrategy(self.settings))
        self.risk = RiskManager(self.settings)
        self.execution_factory = execution_factory
        self.quote_provider = quote_provider or BinancePublicQuotes(
            scanner_settings.api_url, self.settings.quote_timeout
        )

    def contexts(self, session, now):
        run = session.scalar(
            select(ScannerRun)
            .where(
                ScannerRun.timeframe == "1h",
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
                        Snapshot.timeframe == "1h",
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
            permission = session.get(SymbolTradePermission, symbol)
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
                        bool(permission and permission.manual_trade_enabled),
                    )
                )
            except ValueError:
                log.warning(
                    "PAPER_INVALID_SNAPSHOT",
                    extra={"event_data": {"snapshot_id": snapshot.id}},
                )
        return result

    def apply_signal(self, session, signal, portfolio, now, quotes, exit_reason=None):
        quotes = self.fresh_quotes(quotes, now)
        permission = entry_permission(
            session, signal.symbol, now, self.settings.max_snapshot_age_seconds
        )
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
            configuration=self.settings.public_dict()
            | {"entry_permission": permission},
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
        quote = quotes.get(signal.symbol)
        missing_marks = any(p.symbol not in quotes for p in portfolio.positions())
        if signal.action == "BUY" and not permission["entry_eligible"]:
            decision = RiskDecision(
                False,
                signal.symbol,
                reasons=("No entry permission; watchlist is observation only",),
            )
        elif signal.action == "BUY" and not portfolio.account.enabled:
            decision = RiskDecision(
                False, signal.symbol, reasons=("paper trading OFF",)
            )
        elif signal.action != "HOLD" and not quote:
            decision = RiskDecision(
                False, signal.symbol, reasons=("Fresh public quote unavailable",)
            )
        elif signal.action == "BUY" and missing_marks:
            decision = RiskDecision(
                False, signal.symbol, reasons=("Fresh portfolio marks unavailable",)
            )
        else:
            decision = self.risk.evaluate(
                signal,
                portfolio.view(),
                portfolio.account.enabled,
                entry_price=quote.price(signal.action) if quote else None,
            )
        risk = RiskRecord(
            signal_id=record.id,
            approved=decision.approved,
            risk_amount=decision.risk_amount,
            position_notional=decision.position_notional,
            quantity=decision.quantity,
            stop_distance_pct=decision.stop_distance_pct,
            reasons=list(decision.reasons),
            portfolio=portfolio_audit(portfolio.view()),
            execution_quote=quote.audit() if quote else None,
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
        if signal.action != "HOLD":
            return self.execution_factory(
                session, portfolio, self.settings, quotes=quotes
            ).place_order(record.id, risk.id, now)
        return None

    def fetch_quotes(self, symbols):
        # This method is called before acquiring the ledger write transaction.
        try:
            quotes = self.quote_provider.get_quotes(set(symbols))
            return quotes if isinstance(quotes, dict) else {}
        except Exception:
            log.warning("PAPER_QUOTE_UNAVAILABLE")
            return {}

    def fresh_quotes(self, quotes, now):
        return {
            symbol: quote
            for symbol, quote in quotes.items()
            if isinstance(quote, MarketQuote)
            and quote.symbol == symbol
            and quote.fresh(now, self.settings.max_quote_age_seconds)
        }

    def run_once(self, now=None, lease_owner=None):
        explicit_now = now
        now = now or utcnow()
        with self.repository.database.session() as session:
            symbols = {c.symbol for c in self.contexts(session, now)}
            symbols.update(
                session.scalars(
                    select(PaperPosition.symbol).where(PaperPosition.status == "OPEN")
                )
            )
        raw_quotes = self.fetch_quotes(symbols)
        now = explicit_now or utcnow()
        quotes = self.fresh_quotes(raw_quotes, now)
        result = {
            "processed": 0,
            "orders": 0,
            "fresh_markets": 0,
            "fresh_quotes": len(quotes),
            "missing_quotes": sorted(symbols - quotes.keys()),
        }
        with self.repository.transaction() as session:
            now = explicit_now or utcnow()
            quotes = self.fresh_quotes(raw_quotes, now)
            result["fresh_quotes"] = len(quotes)
            result["missing_quotes"] = sorted(symbols - quotes.keys())
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
            portfolio.mark(
                {symbol: quote.bid for symbol, quote in quotes.items()},
                now,
                {symbol: quote.audit() for symbol, quote in quotes.items()},
            )
            # Quote-based protection does not depend on fresh candles or new signals.
            exited = set()
            for position in list(portfolio.positions()):
                quote = quotes.get(position.symbol)
                if not quote:
                    continue
                reason = (
                    "STOP"
                    if quote.bid <= position.stop_price
                    else "TAKE_PROFIT"
                    if position.take_profit_price
                    and quote.bid >= position.take_profit_price
                    else None
                )
                if reason:
                    entry_signal = session.get(SignalRecord, position.signal_id)
                    signal = StrategySignal(
                        position.symbol,
                        "SELL",
                        D(1),
                        quote.bid,
                        None,
                        now,
                        position.strategy_name,
                        (f"Application-level {reason} at current book bid",),
                        entry_signal.snapshot_id,
                    )
                    order = self.apply_signal(
                        session,
                        signal,
                        portfolio,
                        explicit_now or utcnow(),
                        quotes,
                        reason,
                    )
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
                    continue
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
                    explicit_now or utcnow(),
                    quotes,
                    "STRATEGY" if signal.action == "SELL" else None,
                )
                if order:
                    result["orders"] += 1
            portfolio.snapshot(
                now,
                {
                    "missing_symbols": result["missing_quotes"],
                    "position_quotes": {
                        p.symbol: quotes[p.symbol].audit()
                        if p.symbol in quotes
                        else None
                        for p in portfolio.positions()
                    },
                },
            )
        return result

    def manual_close(self, position_id, now=None):
        explicit_now = now
        now = now or utcnow()
        with self.repository.database.session() as session:
            position = session.get(PaperPosition, position_id)
            if position is None or position.status != "OPEN":
                raise ValueError("Position is not open")
            symbol = position.symbol
        quotes = self.fetch_quotes({symbol})
        now = explicit_now or utcnow()
        quotes = self.fresh_quotes(quotes, now)
        if symbol not in quotes:
            raise ValueError("Fresh public quote unavailable; manual close refused")
        with self.repository.transaction() as session:
            now = explicit_now or utcnow()
            quotes = self.fresh_quotes(quotes, now)
            if symbol not in quotes:
                raise ValueError("Fresh public quote unavailable; manual close refused")
            position = session.get(PaperPosition, position_id)
            if position is None or position.status != "OPEN":
                raise ValueError("Position is not open")
            quote = quotes[symbol]
            portfolio = PortfolioService(session)
            portfolio.mark({symbol: quote.bid}, now, {symbol: quote.audit()})
            entry_signal = session.get(SignalRecord, position.signal_id)
            signal = StrategySignal(
                symbol,
                "SELL",
                D(1),
                quote.bid,
                None,
                now,
                position.strategy_name,
                ("Manual paper position close at current book bid",),
                entry_signal.snapshot_id,
            )
            order = self.apply_signal(session, signal, portfolio, now, quotes, "MANUAL")
            portfolio.snapshot(now)
            return order.id
