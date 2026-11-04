"""Realtime daily S2 PAPER loop. No scanner strategy, RiskManager or live API."""

import logging

import requests
from sqlalchemy import func, select

from crypto_bot.execution.full_equity import (
    REASON,
    FullEquityPaperExecution,
    full_equity_quantity,
)
from crypto_bot.portfolio.service import PortfolioService
from crypto_bot.storage.database import iso_utc, utcnow
from crypto_bot.storage.paper_models import (
    ForwardDailyDecision,
    PaperFill,
    PaperGeneration,
    PaperOrder,
    RiskRecord,
    SignalRecord,
)
from crypto_bot.trading.domain import ZERO, D
from crypto_bot.trading.engine import portfolio_audit
from crypto_bot.trading.forward_daily import (
    BinanceBTCDaily,
    DailyDataUnavailable,
    millis,
    s2_observation,
)
from crypto_bot.trading.profiles import (
    S2_PROFILE,
    S2_STRATEGY,
    S2_SYMBOL,
    ForwardSafetyError,
)
from crypto_bot.trading.quotes import BinancePublicQuotes, MarketQuote

log = logging.getLogger(__name__)


class S2ForwardEngine:
    def __init__(
        self,
        repository,
        scanner_settings,
        daily_provider=None,
        quote_provider=None,
        clock=utcnow,
    ):
        self.repository, self.settings, self.clock = (
            repository,
            repository.settings,
            clock,
        )
        self.daily = daily_provider or BinanceBTCDaily(
            scanner_settings.api_url, self.settings.quote_timeout
        )
        safe_session = requests.Session()
        safe_session.trust_env = False
        self.quotes = quote_provider or BinancePublicQuotes(
            scanner_settings.api_url,
            self.settings.quote_timeout,
            session=safe_session,
            clock=clock,
        )

    def check(self, session, now, owner):
        p = PortfolioService(session)
        a = p.account
        generation = session.get(PaperGeneration, a.generation)
        if (
            self.settings.execution_profile != S2_PROFILE
            or a.configuration.get("execution_profile") != S2_PROFILE
            or generation is None
            or generation.strategy_name != S2_STRATEGY
            or generation.status != "ACTIVE"
        ):
            raise ForwardSafetyError("S2 generation/profile inactive or safety-halted")
        if (
            a.lease_owner
            and a.lease_until
            and a.lease_until > now
            and a.lease_owner != owner
        ):
            raise RuntimeError("Account owned by another paper engine")
        if a.cash_balance < 0 or a.reserved_cash != 0:
            raise ForwardSafetyError("Negative cash or unexpected reserved cash")
        positions = p.positions()
        if len(positions) > 1 or any(
            x.symbol != S2_SYMBOL
            or x.strategy_name != S2_STRATEGY
            or x.stop_price is not None
            or x.take_profit_price is not None
            for x in positions
        ):
            raise ForwardSafetyError("Unexpected symbol/strategy/stop/TP on S2 account")
        outstanding = session.scalar(
            select(PaperOrder.id)
            .where(PaperOrder.status.not_in(["FILLED", "CANCELLED", "REJECTED"]))
            .limit(1)
        )
        if outstanding is not None:
            raise ForwardSafetyError("Outstanding paper order on S2 account")
        cutoff = generation.history_cutoffs.get("paper_orders", 0) or 0
        foreign = session.scalar(
            select(PaperOrder.id)
            .join(SignalRecord, PaperOrder.signal_id == SignalRecord.id)
            .where(PaperOrder.id > cutoff, SignalRecord.strategy_name != S2_STRATEGY)
            .limit(1)
        )
        if foreign is not None:
            raise ForwardSafetyError("Old/foreign strategy order after S2 activation")
        duplicate_fill = session.scalar(
            select(PaperFill.order_id)
            .where(PaperFill.order_id > cutoff)
            .group_by(PaperFill.order_id)
            .having(func.count() > 1)
            .limit(1)
        )
        if duplicate_fill is not None:
            raise ForwardSafetyError("Duplicate fill after S2 activation")
        return p, generation

    def latch(self, message):
        with self.repository.transaction() as session:
            p = PortfolioService(session)
            p.account.enabled = False
            generation = session.get(PaperGeneration, p.account.generation)
            if generation:
                generation.status = "ERROR"
                generation.last_error = str(message)[:1000]

    def run_once(self, now=None, lease_owner=None):
        try:
            return self._run_once(now, lease_owner)
        except ForwardSafetyError as exc:
            self.latch(exc)
            raise

    def _run_once(self, explicit_now, owner):
        now = explicit_now or self.clock()
        result = dict(
            processed=0,
            orders=0,
            fresh_markets=0,
            fresh_quotes=0,
            missing_quotes=[],
            warning=None,
            profile=S2_PROFILE,
        )
        with self.repository.database.session() as session:
            self.check(session, now, owner)
        window = None
        try:
            window = self.daily.get_window(now)
        except (DailyDataUnavailable, requests.RequestException, ValueError) as exc:
            result["warning"] = str(exc)[:300]
        now = explicit_now or self.clock()
        if (
            window
            and window[-1].close_ms
            != millis(now.replace(hour=0, minute=0, second=0, microsecond=0)) - 1
        ):
            window = None
            result["warning"] = "Latest completed daily candle unavailable; no catch-up"
        pending_id = None
        needs_quote = False
        with self.repository.transaction() as session:
            p, generation = self.check(session, now, owner)
            if window:
                # Injection and live providers obey the same completed/contiguous window contract.
                if (
                    len(window) != 20
                    or any(
                        b.close_ms >= millis(now)
                        or b.close_ms != b.open_ms + 86400000 - 1
                        for b in window
                    )
                    or any(
                        window[i].open_ms != window[i - 1].open_ms + 86400000
                        for i in range(1, 20)
                    )
                ):
                    raise ForwardSafetyError(
                        "Uncompleted/invalid S2 daily decision window"
                    )
                observation = s2_observation(window, bool(p.positions()))
                generation.latest_indicators = observation | dict(
                    observed_at=iso_utc(now)
                )
                result["fresh_markets"] = 1
                close_ms = window[-1].close_ms
                for stale in session.scalars(
                    select(ForwardDailyDecision).where(
                        ForwardDailyDecision.generation_id == generation.id,
                        ForwardDailyDecision.status == "RETRYABLE",
                        ForwardDailyDecision.source_close_ms < close_ms,
                    )
                ):
                    stale.status = "MISSED_SUPERSEDED"
                    stale.reason = "New completed candle; old decision never backfilled"
                eligible = close_ms > millis(generation.activated_at)
                decision = session.scalar(
                    select(ForwardDailyDecision).where(
                        ForwardDailyDecision.generation_id == generation.id,
                        ForwardDailyDecision.source_close_ms == close_ms,
                        ForwardDailyDecision.strategy_name == S2_STRATEGY,
                        ForwardDailyDecision.symbol == S2_SYMBOL,
                    )
                )
                if eligible and decision is None:
                    action = observation["action"]
                    before = "LONG" if p.positions() else "FLAT"
                    reason = (
                        "Completed close below lower Bollinger band"
                        if action == "BUY"
                        else "Completed close >= SMA20"
                        if action == "SELL"
                        else "Frozen S2 HOLD"
                    )
                    signal = SignalRecord(
                        snapshot_id=None,
                        timestamp=now,
                        symbol=S2_SYMBOL,
                        action=action,
                        signal_strength=D(1) if action != "HOLD" else ZERO,
                        entry_reference=D(observation["close"]),
                        stop_reference=None,
                        take_profit_price=None,
                        strategy_name=S2_STRATEGY,
                        reasons=[reason],
                        configuration=self.settings.public_dict()
                        | dict(
                            paper_generation=generation.id,
                            s2_daily=observation,
                            activation_time=iso_utc(generation.activated_at),
                        ),
                        exit_reason="STRATEGY" if action == "SELL" else None,
                    )
                    session.add(signal)
                    session.flush()
                    status = (
                        "HOLD"
                        if action == "HOLD"
                        else "MISSED_DISABLED"
                        if action == "BUY" and not p.account.enabled
                        else "RETRYABLE"
                    )
                    decision = ForwardDailyDecision(
                        generation_id=generation.id,
                        strategy_name=S2_STRATEGY,
                        symbol=S2_SYMBOL,
                        source_open_ms=window[-1].open_ms,
                        source_close_ms=close_ms,
                        decision_time=now,
                        state_before=before,
                        state_after=before,
                        action=action,
                        indicators=observation,
                        signal_id=signal.id,
                        status=status,
                        reason=reason,
                    )
                    session.add(decision)
                    session.flush()
                    result["processed"] = 1
                    log.info(
                        "S2_DAILY_DECISION",
                        extra={
                            "event_data": dict(
                                symbol=S2_SYMBOL,
                                action=action,
                                source_close=observation["source_close_UTC"],
                                signal_id=signal.id,
                            )
                        },
                    )
                if decision and decision.status == "RETRYABLE":
                    pending_id = decision.id
            needs_quote = bool(p.positions()) or pending_id is not None
        # Fresh quote is requested only AFTER the persisted signal recognition time.
        quotes = {}
        if needs_quote:
            try:
                quotes = self.quotes.get_quotes({S2_SYMBOL})
            except requests.RequestException:
                quotes = {}
        now = explicit_now or self.clock()
        quote = quotes.get(S2_SYMBOL) if isinstance(quotes, dict) else None
        if quote is not None and (
            not isinstance(quote, MarketQuote)
            or quote.symbol != S2_SYMBOL
            or not quote.fresh(now, self.settings.max_quote_age_seconds)
        ):
            raise ForwardSafetyError("Public S2 quote freshness/identity violation")
        with self.repository.transaction() as session:
            p, generation = self.check(session, now, owner)
            if quote:
                p.mark({S2_SYMBOL: quote.bid}, now, {S2_SYMBOL: quote.audit()})
                result["fresh_quotes"] = 1
            elif needs_quote:
                result["missing_quotes"] = [S2_SYMBOL]
                result["warning"] = "Fresh BTC public quote unavailable; no fill"
            if pending_id is not None:
                decision = session.get(ForwardDailyDecision, pending_id)
                if decision.status == "RETRYABLE" and quote:
                    signal = session.get(SignalRecord, decision.signal_id)
                    if quote.received_at < decision.decision_time:
                        raise ForwardSafetyError("Quote predates S2 decision")
                    if signal.action == "BUY" and not p.account.enabled:
                        decision.status = "MISSED_DISABLED"
                        decision.reason = "Paper trading OFF before fill"
                    else:
                        quantity = (
                            full_equity_quantity(
                                p.view().available_cash, quote.ask, self.settings
                            )[0]
                            if signal.action == "BUY"
                            else p.positions()[0].quantity
                            if len(p.positions()) == 1
                            else None
                        )
                        if quantity is None:
                            raise ForwardSafetyError("No full BTC position for S2 SELL")
                        risk = RiskRecord(
                            signal_id=signal.id,
                            approved=True,
                            risk_amount=ZERO,
                            position_notional=quantity * quote.price(signal.action),
                            quantity=quantity,
                            stop_distance_pct=ZERO,
                            reasons=[REASON],
                            portfolio=portfolio_audit(p.view()),
                            execution_quote=quote.audit()
                            | dict(execution_profile=S2_PROFILE),
                        )
                        session.add(risk)
                        session.flush()
                        order = FullEquityPaperExecution(
                            session, p, self.settings, {S2_SYMBOL: quote}
                        ).place_order(signal.id, risk.id, now)
                        decision.order_id = order.id
                        decision.status = "FILLED"
                        decision.state_after = "LONG" if p.positions() else "FLAT"
                        decision.execution_audit = order.quote | dict(
                            source_candle_close_time=decision.indicators[
                                "source_close_UTC"
                            ]
                        )
                        result["orders"] = 1
                        log.info(
                            "S2_PAPER_ORDER_FILLED",
                            extra={
                                "event_data": dict(
                                    side=order.side,
                                    order_id=order.id,
                                    **decision.execution_audit,
                                )
                            },
                        )
            p.snapshot(
                now,
                dict(
                    execution_profile=S2_PROFILE,
                    paper_generation=generation.id,
                    missing_symbols=result["missing_quotes"],
                    position_quotes={S2_SYMBOL: quote.audit()} if quote else {},
                ),
            )
        return result
