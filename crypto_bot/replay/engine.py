"""Hourly scanner replay and minute execution. No production database handle."""

import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from types import SimpleNamespace

from crypto_bot.config.settings import Settings
from crypto_bot.indicators.technical import features
from crypto_bot.replay import stale
from crypto_bot.replay.clock import ReplayClock, at, ms
from crypto_bot.replay.config import settings_for
from crypto_bot.replay.execution import ReplayExecution
from crypto_bot.replay.exit_v2 import DEFAULTS as V2_DEFAULTS
from crypto_bot.replay.exit_v2 import STAGES, SetupRearm, evaluation
from crypto_bot.replay.exits import levels, observe, policy
from crypto_bot.replay.fresh_setup import FreshSetupAdmission, setup_observation
from crypto_bot.replay.institutional import NAME as INSTITUTIONAL
from crypto_bot.replay.institutional import InstitutionalContext
from crypto_bot.replay.institutional_exit import custom as custom_peak_lock
from crypto_bot.replay.institutional_metrics import research_summary, trade_observations
from crypto_bot.replay.market_context import (
    ISOLATED,
    LEGACY,
    canonical_scan_histories,
    exit_rows,
    trim,
)
from crypto_bot.replay.market_context import frame as market_frame
from crypto_bot.replay.market_context import version as context_version
from crypto_bot.replay.metrics import summary
from crypto_bot.replay.portfolio import MemoryLedger
from crypto_bot.replay.registry import SCORES, STRATEGIES
from crypto_bot.replay.rejections import legacy_group, reason_key
from crypto_bot.replay.research import (
    NAME,
    RESEARCH_EXITS,
    adaptive,
    entry_filter_active,
    entry_filter_reason_keys,
    entry_filter_reasons,
    gate_state,
    save_adaptive,
)
from crypto_bot.replay.universe import CandleHistory, HistoricalUniverse
from crypto_bot.risk.manager import RiskManager
from crypto_bot.scanner.momentum import score_momentum
from crypto_bot.scanner.selection import select_watchlist
from crypto_bot.trading.domain import ZERO, D, MarketContext, StrategySignal


class Variant:
    def __init__(self, config, start, stale_policy=stale.STRICT):
        self.config = config
        self.stale_policy = stale_policy
        self.settings = settings_for(config)
        self.strategy = STRATEGIES.get(config["strategy"]).factory(self.settings)
        self.risk = RiskManager(self.settings)
        self.ledger = MemoryLedger(self.settings, at(start))
        self.execution = ReplayExecution(self.ledger, self.settings)
        self.adaptive_exit = config["exit_policy"] in RESEARCH_EXITS
        self.fresh_setup = (
            FreshSetupAdmission()
            if config.get("entry_admission", "OFF") != "OFF"
            else None
        )
        self.rearm = SetupRearm() if config.get("setup_rearm", "OFF") != "OFF" else None
        self.exit = policy(
            "baseline_v1" if self.adaptive_exit else config["exit_policy"],
            config["exit_parameters"],
        )
        self.events = []
        self.closed = []
        self.curve = []
        self.exposure_sum = 0.0
        self.exposure_count = 0
        self.exposure_max = 0.0
        self.blocked = {
            k: 0
            for k in ("daily_loss", "drawdown", "exposure", "max_positions", "other")
        }
        self.total_slippage = ZERO
        self.blocked_reasons = defaultdict(int)
        self.pending = []
        self.research_context = None
        self.context_isolated = False
        self.research_reporting = config["strategy"] == INSTITUTIONAL
        self.reporting_btc_settings = None
        self.institutional_rejections = defaultdict(int)

    def block_entry(self, signal, reasons):
        if signal.action == "BUY":
            if self.fresh_setup and self.fresh_setup.missed(signal.symbol):
                self.event(
                    "FRESH_SETUP_MISSED_CAPACITY",
                    ms(signal.timestamp),
                    symbol=signal.symbol,
                    block_risk_reason=reasons,
                    original_strategy_decision=asdict(signal),
                    **self.fresh_setup.audit(signal.symbol, ms(signal.timestamp)),
                )
            self.blocked[legacy_group(reasons)] += 1
            self.blocked_reasons[reason_key(reasons)] += 1

    def fresh_positions(self, symbols, now):
        for position in self.ledger.portfolio.positions():
            stale.update(
                position, position.symbol in symbols, now, self.event, self.stale_policy
            )
            stale.state(position)["quarantined"] = (
                self.stale_policy == stale.QUARANTINE and stale.state(position)["stale"]
            )

    def mark(self, prices, now):
        self.ledger.portfolio.mark(prices, at(now))
        for position in self.ledger.portfolio.positions():
            if position.symbol in prices:
                value = stale.state(position)
                value["last_valid_mark_time"] = at(now).isoformat() + "Z"
                value["last_valid_mark_price"] = str(position.current_price)

    def decision_view(self):
        return (
            stale.risk_view(self.ledger.portfolio)
            if self.stale_policy == stale.QUARANTINE
            else self.ledger.portfolio.view()
        )

    def event(self, kind, now, **values):
        self.events.append(
            dict(event=kind, timestamp=at(now).isoformat() + "Z", **values)
        )

    def contexts(self, rows, watch, now):
        result = []
        for row in rows:
            f = row["features"]
            s = row["symbol"]
            delta = row["momentum"].get(f"delta_{self.settings.delta_window}h")
            if (
                self.config["strategy"] == INSTITUTIONAL
                and self.research_context is not None
            ):
                audit = self.research_context.context(
                    s, now, self.settings, self.ledger.portfolio.positions()
                )
                if self.context_isolated:
                    audit = deepcopy(audit)
                f = f | {"institutional": audit}
                row = row | {"institutional_context": audit}
            context = MarketContext(
                now // 3600000,
                s,
                at(now),
                D(f["price"]),
                D(row["total_score"]),
                D(delta) if delta is not None else None,
                D(f["rsi"]),
                D(f["ema50"]),
                D(f["ema200"]),
                D(f["atr"]),
                D(f["relative_volume"]),
                "WATCH" if s in watch else "AUTO",
                s in watch,
                f,
            )
            result.append((context, row))
        return sorted(result, key=lambda item: (-item[0].score, item[0].symbol))

    def signals(self, rows, watch, now):
        self.pending = []
        existing = {p.symbol for p in self.ledger.portfolio.positions()}
        for context, row in self.contexts(rows, watch, now):
            if self.rearm:
                # Setup transition is independent of portfolio correlation/risk/slots.
                audit = context.features.get("institutional", {})
                setup_context = replace(
                    context,
                    features=context.features
                    | {
                        "institutional": audit
                        | {"correlation": {"blocked": False, "pairs": []}}
                    },
                )
                setup_signal = self.strategy.evaluate(setup_context, False)
                setup = setup_signal.action == "BUY"
                observable = not any(
                    "UNAVAILABLE" in reason for reason in setup_signal.reasons
                )
                if observable and self.rearm.observe(context.symbol, setup, now):
                    self.event(
                        "SETUP_REARMED",
                        now,
                        symbol=context.symbol,
                        setup=False,
                        snapshot_id=context.snapshot_id,
                    )
            signal = self.strategy.evaluate(context, context.symbol in existing)
            if self.fresh_setup:
                setup, observable = setup_observation(self.strategy, context)
                # Held-only exit rows depend on portfolio occupancy. They are
                # never setup observations; absent TopN rows remain unknown.
                observable = observable and row.get("auto_eligible", True)
                transition = self.fresh_setup.observe(
                    context.symbol, setup, observable, now
                )
                audit = self.fresh_setup.audit(context.symbol, now) | {
                    "setup_observable": observable
                }
                if transition:
                    self.event(
                        transition,
                        now,
                        symbol=context.symbol,
                        original_strategy_decision=asdict(signal),
                        **audit,
                    )
                if signal.action == "BUY":
                    if not observable or not self.fresh_setup.admit(
                        context.symbol, now
                    ):
                        # Do not label this gate rejection as missed capacity.
                        self.blocked["other"] += 1
                        self.blocked_reasons["ENTRY_NOT_FRESH_SETUP"] += 1
                        self.event(
                            "ENTRY_NOT_FRESH_SETUP",
                            now,
                            symbol=context.symbol,
                            action="BUY",
                            reason="ENTRY_NOT_FRESH_SETUP",
                            original_strategy_decision=asdict(signal),
                            **audit,
                        )
                        continue
                elif transition and (
                    "CORRELATION_LIMIT" in signal.reasons or context.symbol in existing
                ):
                    self.fresh_setup.admit(context.symbol, now)
                    self.fresh_setup.missed(context.symbol)
                    self.event(
                        "FRESH_SETUP_MISSED_CAPACITY",
                        now,
                        symbol=context.symbol,
                        block_risk_reason=(
                            ["duplicate open position; pyramiding disabled"]
                            if context.symbol in existing
                            else list(signal.reasons)
                        ),
                        original_strategy_decision=asdict(signal),
                        **audit,
                    )

            if (
                self.rearm
                and signal.action == "BUY"
                and not self.rearm.allowed(context.symbol, now)
            ):
                self.rearm.blocks += 1
                self.block_entry(signal, "SETUP_NOT_REARMED")
                self.event(
                    "SETUP_NOT_REARMED",
                    now,
                    symbol=context.symbol,
                    action="BUY",
                    reason="SETUP_NOT_REARMED",
                    original_strategy_decision=asdict(signal),
                )
                continue
            if (
                self.config["strategy"] == INSTITUTIONAL
                and signal.action == "HOLD"
                and context.symbol not in existing
            ):
                for reason in signal.reasons:
                    self.institutional_rejections[reason] += 1
                self.event(
                    "INSTITUTIONAL_ENTRY_REJECTED",
                    now,
                    symbol=context.symbol,
                    reasons=list(signal.reasons),
                    context=row.get("institutional_context", {}),
                )
            if (
                signal.action == "BUY"
                and self.config.get("btc_regime_entry_gate", "OFF") == "BULLISH_ONLY"
                and gate_state(row, now) != "BULLISH"
            ):
                self.block_entry(signal, "BTC_REGIME_ENTRY_BLOCK")
                self.event(
                    "BTC_REGIME_ENTRY_BLOCK",
                    now,
                    symbol=signal.symbol,
                    action="BUY",
                    reason="BTC_REGIME_ENTRY_BLOCK",
                    btc_regime=row.get("btc_regime", "UNKNOWN"),
                    btc_regime_state=gate_state(row, now),
                    btc_regime_source_timestamp=row.get("btc_regime_source_timestamp"),
                    strategy_score=str(context.score),
                    original_strategy_decision=asdict(signal),
                )
                continue
            if signal.action == "BUY":
                bounds = self.config.get("entry_filter", {})
                failed = entry_filter_reasons(row, bounds)
                # Audit only for active new research fields. Neutral/legacy paths
                # retain their exact old event stream and ledger behavior.
                entry_audit = (
                    dict(
                        atr_pct=row.get("features", {}).get("atr_pct"),
                        range_position=row.get("features", {}).get("range_position"),
                        filter_thresholds=dict(bounds),
                    )
                    if entry_filter_active(bounds)
                    else {}
                )
                if bounds.get("max_relative_volume") is not None:
                    entry_audit["relative_volume"] = row.get("features", {}).get(
                        "relative_volume"
                    )
                if failed:
                    self.block_entry(signal, "RESEARCH_ENTRY_FILTER_BLOCK")
                    self.event(
                        "RESEARCH_ENTRY_FILTER_BLOCK",
                        now,
                        symbol=signal.symbol,
                        action="BUY",
                        reason="RESEARCH_ENTRY_FILTER_BLOCK",
                        score=str(context.score),
                        delta_4h=row.get("momentum", {}).get("delta_4h"),
                        filter_reasons=failed,
                        original_strategy_decision=asdict(signal),
                        **entry_audit,
                    )
                    continue
                if entry_audit:
                    self.event(
                        "RESEARCH_ENTRY_FILTER_PASS",
                        now,
                        symbol=signal.symbol,
                        action="BUY",
                        score=str(context.score),
                        delta_4h=row.get("momentum", {}).get("delta_4h"),
                        original_strategy_decision=asdict(signal),
                        **entry_audit,
                    )
            # Adaptive research is stop-only, as in Shadow; gate never blocks SELL.
            if signal.action != "HOLD" and not (
                self.config["exit_policy"] == NAME and signal.action == "SELL"
            ):
                self.pending.append((signal, row))
        return {signal.symbol for signal, row in self.pending} | existing

    def execute_signals(self, datasets, now, fallback):
        pfolio = self.ledger.portfolio
        prices = {
            p.symbol: D(datasets[p.symbol][now]["open"])
            for p in pfolio.positions()
            if now in datasets.get(p.symbol, {})
        }
        self.fresh_positions(prices, now)
        self.mark(prices, now)
        for signal, row in sorted(
            self.pending, key=lambda item: item[0].action != "SELL"
        ):
            if (
                self.rearm
                and signal.action == "BUY"
                and not self.rearm.allowed(signal.symbol, ms(signal.timestamp))
            ):
                self.rearm.blocks += 1
                self.block_entry(signal, "SETUP_NOT_REARMED")
                self.event(
                    "SETUP_NOT_REARMED",
                    now,
                    symbol=signal.symbol,
                    action="BUY",
                    reason="SETUP_NOT_REARMED",
                )
                continue
            c = datasets.get(signal.symbol, {}).get(now)
            held = next(
                (p for p in pfolio.positions() if p.symbol == signal.symbol), None
            )
            if held and stale.state(held)["quarantined"]:
                self.event(
                    "STALE_ACTION_SKIPPED",
                    now,
                    symbol=signal.symbol,
                    action=signal.action,
                    position_id=held.id,
                    reason="QUARANTINED_POSITION; fresh execution data unavailable",
                )
                continue
            if c is None:
                self.event(
                    "NO_FILL",
                    now,
                    symbol=signal.symbol,
                    action=signal.action,
                    signal_time=signal.timestamp.isoformat() + "Z",
                    reason="MISSING_1M_EXECUTION_CANDLE; fallback disabled"
                    if not fallback
                    else "MISSING_EXECUTION_CANDLE",
                )
                self.block_entry(signal, self.events[-1]["reason"])
                continue
            ref = D(c["open"])
            if (
                self.stale_policy == stale.STRICT
                and signal.action == "BUY"
                and any(pos.symbol not in prices for pos in pfolio.positions())
            ):
                self.event(
                    "RISK_REJECTED",
                    now,
                    symbol=signal.symbol,
                    action=signal.action,
                    reasons=["Fresh portfolio marks unavailable"],
                    missing_mark_symbols=sorted(
                        pos.symbol
                        for pos in pfolio.positions()
                        if pos.symbol not in prices
                    ),
                )
                self.block_entry(signal, self.events[-1]["reasons"])
                continue
            decision_view = self.decision_view()
            risk = self.risk
            if self.config["strategy"] == INSTITUTIONAL and signal.action == "BUY":
                if self.research_context is None:
                    self.block_entry(signal, "INSTITUTIONAL_CONTEXT_UNAVAILABLE")
                    self.event(
                        "RISK_REJECTED",
                        now,
                        symbol=signal.symbol,
                        reasons=["INSTITUTIONAL_CONTEXT_UNAVAILABLE"],
                    )
                    continue
                audit = self.research_context.context(
                    signal.symbol, now, self.settings, pfolio.positions()
                )
                if self.context_isolated:
                    audit = deepcopy(audit)
                row = row | {"institutional_context": audit}
                correlation = audit["correlation"]
                multiplier = D(audit["btc"]["risk_multiplier"])
                if correlation["blocked"] or multiplier <= 0:
                    reason = (
                        "CORRELATION_LIMIT"
                        if correlation["blocked"]
                        else "BTC_BEAR_REGIME"
                        if audit["btc"]["regime"] == "BEAR"
                        else "BTC_REGIME_UNAVAILABLE"
                    )
                    self.block_entry(signal, reason)
                    self.event(
                        "RISK_REJECTED",
                        now,
                        symbol=signal.symbol,
                        reasons=[reason],
                        context=audit,
                    )
                    continue
                risk = RiskManager(
                    replace(
                        self.settings,
                        risk_per_trade_pct=self.settings.risk_per_trade_pct
                        * multiplier,
                    )
                )
            decision = risk.evaluate(signal, decision_view, True, entry_price=ref)
            self.event(
                "STRATEGY_SIGNAL",
                now,
                symbol=signal.symbol,
                action=signal.action,
                reasons=list(signal.reasons),
                entry_reference=str(signal.entry_reference),
            )
            self.event(
                "RISK_DECISION",
                now,
                symbol=signal.symbol,
                action=signal.action,
                decision=asdict(decision),
                portfolio=asdict(decision_view),
            )
            if not decision.approved:
                self.block_entry(signal, decision.reasons)
                continue
            position, audit = self.execution.fill(
                signal,
                decision,
                ref,
                at(now),
                c["interval"],
                "STRATEGY" if signal.action == "SELL" else None,
            )
            for event in self.events[-2:]:
                event["position_id"] = position.id
                event["order_id"] = audit["order_id"]
            self.total_slippage += D(audit["slippage"])
            if signal.action == "BUY":
                if self.fresh_setup:
                    self.fresh_setup.filled(signal.symbol)
                position.original_stop = signal.stop_reference
                position.take_profit_price, _ = levels(
                    self.exit,
                    position.entry_price,
                    position.entry_price - position.original_stop,
                )
                position.highest = position.entry_price
                position.overlay_active = False
                position.mfe_price = position.entry_price
                position.mae_price = position.entry_price
                position.slippage_total = D(audit["slippage"])
                f = row["features"]
                momentum = row["momentum"]
                position.entry_data = dict(
                    entry_score=row["total_score"],
                    score_components=row["components"],
                    score_reasons=row["reasons"],
                    delta_1h=momentum["delta_1h"],
                    delta_4h=momentum["delta_4h"],
                    delta_24h=momentum["delta_24h"],
                    scanner_snapshot_timestamp=signal.timestamp.isoformat() + "Z",
                    ema50=f["ema50"],
                    ema200=f["ema200"],
                    rsi=f["rsi"],
                    atr=f["atr"],
                    atr_pct=f["atr_pct"],
                    relative_volume=f["relative_volume"],
                    momentum=f["momentum"],
                    range_position=f["range_position"],
                    entry_reference_price=str(signal.entry_reference),
                    entry_drift_pct=float((ref / signal.entry_reference - 1) * 100),
                    entry_drift_atr=float((ref - signal.entry_reference) / D(f["atr"]))
                    if f["atr"] > 0
                    else None,
                    risk_amount=str(decision.risk_amount),
                    risk_reasons=list(decision.reasons),
                    risk_decision=asdict(decision),
                    portfolio_at_decision=asdict(decision_view),
                    strategy_reasons=list(signal.reasons),
                    algorithm_watch=True,
                    btc_regime=row.get("btc_regime", "UNKNOWN"),
                    btc_regime_source_timestamp=row.get("btc_regime_source_timestamp"),
                    btc_regime_entry_gate=self.config.get(
                        "btc_regime_entry_gate", "OFF"
                    ),
                    intrabar_ambiguity=False,
                )
                if self.config["exit_policy"] in V2_DEFAULTS:
                    position.entry_data["research_protection_policy"] = self.config[
                        "exit_policy"
                    ]
                    position.entry_data["adaptive_exit_parameters"] = dict(
                        self.config["exit_parameters"]
                    )
                if self.adaptive_exit and custom_peak_lock(
                    self.config.get("exit_parameters", {})
                ):
                    position.entry_data["adaptive_exit_parameters"] = dict(
                        self.config["exit_parameters"]
                    )
                if self.config["strategy"] == INSTITUTIONAL:
                    position.entry_data["institutional_context"] = row[
                        "institutional_context"
                    ]
                    position.entry_data["institutional_btc_regime"] = row[
                        "institutional_context"
                    ]["btc"]["regime"]
                    position.entry_data["risk_multiplier"] = row[
                        "institutional_context"
                    ]["btc"]["risk_multiplier"]
                elif self.research_reporting and self.research_context is not None:
                    position.entry_data["research_btc_regime"] = (
                        self.research_context.btc(now, self.reporting_btc_settings)[
                            "regime"
                        ]
                    )
                self.mark({position.symbol: ref}, now)
                prices[position.symbol] = ref
            else:
                position.slippage_total += D(audit["slippage"])
                self.closed.append(self.trade(position, now))
            self.event(
                "ENTRY_FILL" if signal.action == "BUY" else "EXIT_FILL", now, **audit
            )
        self.pending = []

    def bar(self, symbol, candle, now, conservative):
        if candle["close_time"] >= now:
            raise ValueError("Execution candle is not CLOSED as of replay clock")
        position = next(
            (p for p in self.ledger.portfolio.positions() if p.symbol == symbol), None
        )
        if position is None or ms(position.opened_at) > candle["time"]:
            return
        if stale.state(position)["quarantined"]:
            return
        # The current completed bar is observable even if its exit closes the
        # position before the subsequent portfolio mark. This is metadata only.
        stale.state(position)["last_valid_mark_time"] = at(now).isoformat() + "Z"
        stale.state(position)["last_valid_mark_price"] = str(D(candle["close"]))
        if self.adaptive_exit:
            model = adaptive(position, self.settings)
            hit, ambiguous, visited = model.observe(candle, position, now, conservative)
            save_adaptive(position, model)
        else:
            hit, ambiguous, visited = observe(candle, position, self.exit, conservative)
        position.entry_data["intrabar_ambiguity"] |= ambiguous
        position.mfe_price = max(position.mfe_price, *visited)
        position.mae_price = min(position.mae_price, *visited)
        if hit:
            price, reason = hit
            signal = StrategySignal(
                symbol,
                "SELL",
                D(1),
                price,
                None,
                at(now),
                self.settings.strategy_name,
                (reason,),
            )
            decision = self.risk.evaluate(
                signal, self.ledger.portfolio.view(), True, entry_price=price
            )
            self.event(
                "EXIT_TRIGGER",
                now,
                position_id=position.id,
                symbol=symbol,
                reason=reason,
                reference_price=str(price),
                original_stop=str(position.original_stop),
                active_stop=str(position.stop_price),
                take_profit=str(position.take_profit_price)
                if position.take_profit_price
                else None,
            )
            self.event(
                "RISK_DECISION",
                now,
                position_id=position.id,
                symbol=symbol,
                decision=asdict(decision),
                portfolio=asdict(self.ledger.portfolio.view()),
            )
            position, audit = self.execution.fill(
                signal, decision, price, at(now), candle["interval"], reason
            )
            if self.rearm and self.adaptive_exit and model.state.stage > 0:
                stage = STAGES[model.state.stage]
                self.rearm.closed(symbol, stage, now)
                self.event("SETUP_REARM_BLOCKED", now, symbol=symbol, exit_stage=stage)
            position.slippage_total += D(audit["slippage"])
            self.total_slippage += D(audit["slippage"])
            self.closed.append(self.trade(position, now))
            self.event(
                "EXIT_FILL",
                now,
                **audit,
                intrabar_ambiguity=ambiguous,
                candle_open_time=at(candle["time"]).isoformat() + "Z",
                candle_close_time=at(candle["close_time"] + 1).isoformat() + "Z",
                trigger_precision="INTRABAR_APPROXIMATION_KNOWN_AT_CLOSE",
            )

    def trade(self, p, now):
        cost = p.notional + p.entry_fee
        adaptive_audit = {}
        if self.adaptive_exit:
            result = (
                dict(
                    realized_pnl=str(p.realized_pnl),
                    return_pct=float(p.realized_pnl / cost * 100),
                    r_multiple=float(p.realized_pnl / D(p.entry_data["risk_amount"])),
                )
                if p.status == "CLOSED"
                else None
            )
            adaptive_audit = adaptive(p, self.settings).audit(result)
            if (
                self.config["exit_policy"] in V2_DEFAULTS
                and p.exit_reason == "STRATEGY"
            ):
                adaptive_audit["exit_stage"] = "STRATEGY_EXIT"
        if self.research_reporting:
            adaptive_audit.update(trade_observations(p, self.settings, now))
        return (
            adaptive_audit
            | p.entry_data
            | stale.position_metadata(p, now)
            | dict(
                position_id=p.id,
                symbol=p.symbol,
                status="OPEN_STALE"
                if p.status == "OPEN" and stale.state(p)["stale"]
                else p.status,
                asset_group="BTC"
                if p.symbol == "BTCUSDT"
                else "ETH"
                if p.symbol == "ETHUSDT"
                else "ALT",
                entry_time=p.opened_at.isoformat() + "Z",
                exit_time=p.closed_at.isoformat() + "Z" if p.closed_at else None,
                duration_seconds=(
                    (p.closed_at or at(now)) - p.opened_at
                ).total_seconds(),
                quantity=str(p.quantity),
                notional=str(p.notional),
                entry_fill_price=str(p.entry_price),
                entry_price=str(p.entry_price),
                exit_price=str(p.exit_price) if p.exit_price else None,
                original_stop_price=str(p.original_stop),
                take_profit_price=str(p.take_profit_price)
                if p.take_profit_price
                else None,
                stop_price=str(p.stop_price),
                exit_reason=p.exit_reason,
                realized_pnl=str(p.realized_pnl),
                unrealized_pnl=str(p.unrealized_pnl),
                return_pct=float(p.realized_pnl / cost * 100)
                if p.status == "CLOSED"
                else None,
                r_multiple=float(p.realized_pnl / D(p.entry_data["risk_amount"]))
                if p.status == "CLOSED" and D(p.entry_data["risk_amount"]) > 0
                else None,
                mfe_pct=max(0, float((p.mfe_price / p.entry_price - 1) * 100)),
                mae_pct=min(0, float((p.mae_price / p.entry_price - 1) * 100)),
                fees=str(p.entry_fee + p.exit_fee),
                slippage=str(p.slippage_total),
                entry_fee=str(p.entry_fee),
                exit_fee=str(p.exit_fee),
                source="BINANCE_PUBLIC_KLINES_APPROXIMATION",
                strategy_name=self.config["strategy"],
                exit_policy=self.config["exit_policy"],
            )
        )

    def equity(self, now):
        view = self.ledger.portfolio.view()
        self.curve.append(
            dict(
                time=now,
                timestamp=at(now).isoformat() + "Z",
                equity=str(view.equity),
                cash=str(view.cash_balance),
                exposure=str(view.exposure),
                drawdown_pct=float(
                    (view.peak_equity - view.equity) / view.peak_equity * 100
                ),
                max_drawdown=str(view.max_drawdown),
            )
        )

    def checkpoint(self):
        return dict(
            **(
                {"fresh_setup": self.fresh_setup.checkpoint()}
                if self.fresh_setup
                else {}
            ),
            **({"setup_rearm": self.rearm.checkpoint()} if self.rearm else {}),
            **(
                {"institutional_rejections": dict(self.institutional_rejections)}
                if self.config["strategy"] == INSTITUTIONAL
                else {}
            ),
            ledger=self.ledger.checkpoint(),
            next_order=self.execution.next_order,
            blocked=self.blocked,
            blocked_reasons=dict(self.blocked_reasons),
            total_slippage=str(self.total_slippage),
            exposure_count=self.exposure_count,
            exposure_sum=self.exposure_sum,
            exposure_max=self.exposure_max,
        )


class ReplayEngine:
    def __init__(self, store, provider, check=None):
        self.store = store
        self.provider = provider
        self.check = check or (lambda: None)
        self.profile = defaultdict(float)

    def run(self, run_id):
        began = time.monotonic()
        run = self.store.get(run_id)
        config = run["config"]
        mode = context_version(config)
        isolated = mode == ISOLATED
        start, end = ms(config["start"]), ms(config["end"])
        scanner = Settings(**config["scanner_settings"])
        clock = ReplayClock(start - config["warmup_hours"] * 3600000)
        candidates = config["candidate_symbols"] or self.provider.catalog()
        candidates = [s for s in candidates if HistoricalUniverse.allowed(s, scanner)]
        institutional = any(v["strategy"] == INSTITUTIONAL for v in config["variants"])
        load_start = (
            start
            - max(config["warmup_hours"], config.get("context_warmup_hours", 0))
            * 3600000
        )
        context_symbols = (
            sorted(set(candidates) | {"BTCUSDT"}) if institutional else candidates
        )
        cache_began = time.perf_counter()
        histories = {}

        def load(symbol):
            self.check()
            return symbol, CandleHistory(self.provider.hourly(symbol, load_start, end))

        # Bounded in-flight downloads; consume deterministically in catalog order.
        # Submit only a small batch, so futures cannot retain all archive objects.
        concurrency = getattr(self.provider, "parallelism", 1)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for offset in range(0, len(context_symbols), concurrency):
                for i, (symbol, rows) in enumerate(
                    pool.map(load, context_symbols[offset : offset + concurrency]),
                    offset,
                ):
                    if rows:
                        histories[symbol] = rows
                    if i % 10 == 0:
                        self.store.merge_progress(
                            run_id,
                            dict(
                                cache_state="CACHE DOWNLOAD",
                                candidate_count=len(candidates),
                                candidates_loaded=i + 1,
                                cache_hits=self.provider.hits,
                                downloaded_candles=self.provider.downloaded,
                                cache_missing=self.provider.missing,
                            ),
                        )
        self.profile["hourly_cache_seconds"] += time.perf_counter() - cache_began
        if not histories:
            raise ValueError("No historical candles in selected research universe")
        scan_histories = {s: histories[s] for s in candidates if s in histories}
        if isolated:
            scan_histories = canonical_scan_histories(
                scan_histories, start - config["warmup_hours"] * 3600000
            )
        universe = HistoricalUniverse(
            scan_histories,
            scanner,
            float(config["spread_approximation_pct"]),
        )
        stale_policy = config.get("stale_position_policy", stale.STRICT)
        variants = [Variant(v, start, stale_policy) for v in config["variants"]]
        if institutional:
            research_context = InstitutionalContext(
                histories, start if isolated else None
            )
            reporting_settings = next(
                v.settings for v in variants if v.config["strategy"] == INSTITUTIONAL
            )
            for v in variants:
                v.research_context = research_context
                v.context_isolated = isolated
                if not isolated:
                    v.research_reporting = True
                    v.reporting_btc_settings = reporting_settings
                else:
                    v.reporting_btc_settings = (
                        v.settings if v.config["strategy"] == INSTITUTIONAL else None
                    )
        history = defaultdict(list)
        checkpoint = self.store.checkpoint(run_id)
        cursor = start - 24 * 3600000
        if checkpoint:
            if checkpoint.get("market_context_version", LEGACY) != mode:
                raise ValueError(
                    "Cannot resume a checkpoint under a different market context version"
                )
            self.provider.used.update(checkpoint.get("source_objects", []))
            cursor = checkpoint["next_time"]
            clock.advance(cursor)
            history.update(
                {
                    s: [
                        SimpleNamespace(
                            timestamp=datetime.fromisoformat(r["timestamp"]),
                            total_score=r["total_score"],
                        )
                        for r in rows
                    ]
                    for s, rows in checkpoint["score_history"].items()
                }
            )
            for i, (v, state) in enumerate(zip(variants, checkpoint["variants"])):
                v.ledger.restore(state["ledger"])
                if v.fresh_setup:
                    v.fresh_setup = FreshSetupAdmission(state.get("fresh_setup"))
                if v.rearm:
                    v.rearm = SetupRearm(state.get("setup_rearm"))
                v.execution.next_order = state["next_order"]
                v.blocked = state["blocked"]
                v.blocked_reasons.update(
                    state["blocked_reasons"]
                    if "blocked_reasons" in state
                    else self.store.blocked_entry_details(run_id, i, v.blocked)[
                        "blocked_entry_reasons"
                    ]
                )
                v.total_slippage = D(state["total_slippage"])
                v.institutional_rejections.update(
                    state.get("institutional_rejections", {})
                )
                _, v.curve = self.store.data(run_id, i)
                v.exposure_count = state["exposure_count"]
                v.exposure_sum = state["exposure_sum"]
                v.exposure_max = state["exposure_max"]
        else:
            for v in variants:
                v.equity(start)
        self.store.update(run_id, status="WARMUP" if cursor < start else "RUNNING")
        while cursor < end:
            self.check()
            clock.advance(cursor)
            began_scan = time.perf_counter()
            if isolated:
                markets = universe.ranked(cursor)
                self.profile["universe_seconds"] += time.perf_counter() - began_scan
                began_indicators = time.perf_counter()
                current_frame = market_frame(
                    universe,
                    scanner,
                    cursor,
                    history,
                    SCORES.get(config["score_version"]),
                    features,
                    markets=markets,
                )
                history = trim(history, cursor)
            else:
                markets = universe.ranked(cursor)
                held = {
                    p.symbol for v in variants for p in v.ledger.portfolio.positions()
                }
                by_symbol = {m.symbol: m for m in markets}
                for symbol in held:
                    if symbol not in by_symbol:
                        m = universe.market(symbol, cursor)
                        if m:
                            by_symbol[symbol] = m
                self.profile["universe_seconds"] += time.perf_counter() - began_scan
                began_indicators = time.perf_counter()
                rows = []
                for symbol, market in by_symbol.items():
                    observed = [c for c, qv in universe.as_of(symbol, cursor)]
                    assert all(c.close_time < clock.now_ms for c in observed)
                    f = features(observed, market, scanner)
                    computed = SCORES.get(config["score_version"])(f, scanner)
                    past = history[symbol]
                    momentum = score_momentum(
                        computed["total_score"],
                        at(cursor),
                        past,
                        scanner.momentum_windows,
                        scanner.history_tolerance_minutes,
                    )
                    rows.append(
                        dict(
                            symbol=symbol,
                            features=f,
                            momentum=momentum,
                            auto_eligible=symbol in {m.symbol for m in markets},
                            **computed,
                        )
                    )
                    past.append(
                        SimpleNamespace(
                            timestamp=at(cursor), total_score=computed["total_score"]
                        )
                    )
                history = {
                    s: [r for r in rs if ms(r.timestamp) >= cursor - 26 * 3600000]
                    for s, rs in history.items()
                }
                history = defaultdict(list, {s: rs for s, rs in history.items() if rs})
                watch = select_watchlist(rows, scanner)
                btc = next((r for r in rows if r["symbol"] == "BTCUSDT"), None)
                btc_source = (
                    universe.as_of("BTCUSDT", cursor)[-1][0].close_time if btc else None
                )
                for row in rows:
                    row["btc_regime_source_timestamp"] = (
                        at(btc_source).isoformat() + "Z"
                        if btc_source is not None
                        else None
                    )
                    row["btc_regime"] = (
                        "BULLISH"
                        if btc and btc["features"]["ema50"] > btc["features"]["ema200"]
                        else "BEARISH"
                        if btc
                        else "UNKNOWN"
                    )
            self.profile["indicator_score_seconds"] += (
                time.perf_counter() - began_indicators
            )
            self.profile["scanner_seconds"] += time.perf_counter() - began_scan
            if cursor < start:
                cursor += 3600000
                continue
            self.store.update(run_id, status="RUNNING", heartbeat=time.time())
            wanted = set()
            for v in variants:
                if isolated:
                    local_rows = current_frame.rows() + exit_rows(
                        current_frame,
                        universe,
                        scanner,
                        cursor,
                        history,
                        SCORES.get(config["score_version"]),
                        features,
                        v.ledger.portfolio.positions(),
                    )
                    wanted |= v.signals(local_rows, current_frame.watch(), cursor)
                else:
                    wanted |= v.signals(rows, watch, cursor)
            cache_began = time.perf_counter()
            datasets = {}
            for symbol in wanted:
                self.check()
                datasets[symbol] = self.provider.minutes(
                    symbol, cursor, cursor + 3600000
                )
                if config["fallback_5m"] and cursor not in datasets[symbol]:
                    fallback = self.provider.minutes(
                        symbol, cursor, cursor + 3600000, "5m"
                    )
                    if cursor in fallback and not any(
                        t in datasets[symbol]
                        for t in range(cursor, cursor + 300000, 60000)
                    ):
                        for t, candle in fallback.items():
                            if not any(
                                m in datasets[symbol]
                                for m in range(t, t + 300000, 60000)
                            ):
                                datasets[symbol][t] = candle
            self.profile["execution_cache_seconds"] += time.perf_counter() - cache_began
            execution_began = time.perf_counter()
            for v in variants:
                v.execute_signals(datasets, cursor, config["fallback_5m"])
            for minute in range(cursor, cursor + 3600000, 60000):
                self.check()
                clock.advance(minute + 60000)
                for v in variants:
                    marks = {}
                    fresh = set()
                    for p in v.ledger.portfolio.positions():
                        candle = datasets.get(p.symbol, {}).get(minute)
                        fallback = datasets.get(p.symbol, {}).get(minute - 240000)
                        if candle and candle["interval"] == "1m":
                            fresh.add(p.symbol)
                        elif fallback and fallback["interval"] == "5m":
                            fresh.add(p.symbol)
                        elif config["fallback_5m"]:
                            # A supported 5m bar is only observable at its close;
                            # absence of an intervening 1m bar is not a data gap.
                            t = minute - minute % 300000
                            pending = datasets.get(p.symbol, {}).get(t)
                            if pending and pending["interval"] == "5m":
                                fresh.add(p.symbol)
                    v.fresh_positions(fresh, clock.now_ms)
                    for p in list(v.ledger.portfolio.positions()):
                        # Only a completed execution candle can affect exits/marks.
                        candle = datasets.get(p.symbol, {}).get(minute)
                        if candle and candle["interval"] == "1m":
                            v.bar(
                                p.symbol, candle, clock.now_ms, config["conservative"]
                            )
                            marks[p.symbol] = D(candle["close"])
                        fallback = datasets.get(p.symbol, {}).get(minute - 240000)
                        if fallback and fallback["interval"] == "5m":
                            v.bar(
                                p.symbol, fallback, clock.now_ms, config["conservative"]
                            )
                            marks[p.symbol] = D(fallback["close"])
                    if marks:
                        v.mark(marks, clock.now_ms)
                    view = v.ledger.portfolio.view()
                    sample = (
                        float(view.exposure / view.equity * 100)
                        if view.equity > 0
                        else 0
                    )
                    v.exposure_sum += sample
                    v.exposure_count += 1
                    v.exposure_max = max(v.exposure_max, sample)
            self.profile["execution_seconds"] += time.perf_counter() - execution_began
            cursor += 3600000
            payloads = []
            for i, v in enumerate(variants):
                v.equity(cursor)
                trades = v.closed + [
                    v.trade(p, cursor) for p in v.ledger.portfolio.positions()
                ]
                payloads.append(
                    (
                        i,
                        trades,
                        v.execution.orders,
                        v.execution.fills,
                        v.events,
                        v.curve[-2:] if cursor == start + 3600000 else v.curve[-1:],
                    )
                )
                v.ledger.all_positions = [
                    p for p in v.ledger.all_positions if p.status == "OPEN"
                ]
            elapsed = time.monotonic() - began
            progress = dict(
                current_simulated_date=at(cursor).isoformat() + "Z",
                progress_pct=(cursor - start) / (end - start) * 100,
                processed_timestamps=(cursor - start) // 3600000,
                trades_opened=sum(v.ledger.next_position - 1 for v in variants),
                trades_closed=sum(
                    v.ledger.next_position - 1 - len(v.ledger.portfolio.positions())
                    for v in variants
                ),
                cache_hits=self.provider.hits,
                downloaded_candles=self.provider.downloaded,
                cache_missing=self.provider.missing,
                cache_state="CACHE HIT",
                elapsed_seconds=elapsed,
                estimated_remaining_seconds=elapsed
                * (end - cursor)
                / max(cursor - start, 1),
                profile=dict(self.profile),
            )
            checkpoint = dict(
                next_time=cursor,
                **({"market_context_version": mode} if isolated else {}),
                source_objects=sorted(self.provider.used),
                score_history={
                    s: [
                        dict(
                            timestamp=r.timestamp.isoformat(), total_score=r.total_score
                        )
                        for r in rs
                    ]
                    for s, rs in history.items()
                },
                variants=[v.checkpoint() for v in variants],
            )
            began_write = time.perf_counter()
            self.store.save_step(run_id, payloads, progress, checkpoint)
            self.profile["database_seconds"] += time.perf_counter() - began_write
            for v in variants:
                v.closed = []
                v.events = []
                v.execution.orders = []
                v.execution.fills = []
        results = []
        for i, v in enumerate(variants):
            trades, curve = self.store.data(run_id, i)
            result, breakdowns = summary(
                trades, curve, v.ledger.portfolio, v.blocked, v.total_slippage, []
            )
            result["average_exposure_pct"] = (
                v.exposure_sum / v.exposure_count if v.exposure_count else 0
            )
            result["max_exposure_pct"] = v.exposure_max
            result.update(stale.summary(trades, stale_policy))
            risk_view = v.decision_view()
            result.update(
                risk_sizing_equity=str(risk_view.equity),
                risk_exposure=str(risk_view.exposure),
            )
            # Read the persisted audit too, so pre-observability checkpoints resume
            # without discarding the already-counted rejection reasons.
            result.update(self.store.blocked_entry_details(run_id, i, v.blocked))
            if entry_filter_active(v.config.get("entry_filter", {})):
                result.setdefault(
                    "entry_filter_diagnostics",
                    dict(
                        attempted_buy_count=0,
                        passed_filter_count=0,
                        blocked_filter_count=0,
                        block_rate_pct=None,
                        reason_counts=dict.fromkeys(
                            entry_filter_reason_keys(v.config["entry_filter"]), 0
                        ),
                        source="STORED_AUDIT_EVENTS",
                        scope="Strategy BUYs reaching entry filter before risk/execution",
                    ),
                )
            if (institutional and not isolated) or v.research_reporting:
                research_metrics, research_breakdowns = research_summary(trades)
                result.update(research_metrics)
                result.update(
                    v.rearm.metrics()
                    if v.rearm
                    else dict(
                        rearm_block_count=0,
                        rearm_success_count=0,
                        average_time_to_rearm=None,
                    )
                )
                if v.config["strategy"] == INSTITUTIONAL:
                    result["institutional_entry_rejections"] = dict(
                        v.institutional_rejections
                    )
                breakdowns.update(research_breakdowns)
            results.append(dict(metrics=result, breakdowns=breakdowns))
        v2_evaluation = (
            evaluation(config["variants"], results)
            if any(v["exit_policy"] in V2_DEFAULTS for v in config["variants"])
            else None
        )
        metadata = (
            run["metadata"]
            | (
                {"institutional_exit_v2_evaluation": v2_evaluation}
                if v2_evaluation
                else {}
            )
            | dict(
                stale_position_policy=stale_policy,
                stale_position_count=sum(
                    r["metrics"]["stale_position_count"] for r in results
                ),
                stale_symbols=sorted(
                    {s for r in results for s in r["metrics"]["stale_symbols"]}
                ),
                stale_data_affected=any(
                    r["metrics"]["stale_data_affected"] for r in results
                ),
                stale_details_by_variant=[
                    dict(
                        variant=i,
                        **{
                            k: r["metrics"][k]
                            for k in (
                                "stale_position_count",
                                "stale_symbols",
                                "stale_positions",
                                "first_stale_time",
                                "stale_duration_seconds",
                                "resumed_after_stale",
                                "capital_locked_by_stale_positions",
                                "exposure_locked_by_stale_positions",
                            )
                        },
                    )
                    for i, r in enumerate(results)
                ],
                historical_data_revision=self.provider.cache.revision(
                    self.provider.used
                ),
                cache_revision=self.provider.cache.digest(),
                resolved_candidate_symbols=candidates,
                profile=dict(self.profile),
                elapsed_seconds=time.monotonic() - began,
            )
        )
        self.store.complete(run_id, results, metadata)
