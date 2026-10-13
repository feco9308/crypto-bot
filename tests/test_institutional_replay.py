"""Institutional research isolation, multi-timeframe as-of and portfolio pipeline."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_replay import START, Synthetic, config, row

from crypto_bot.data.base import Candle
from crypto_bot.replay.clock import at
from crypto_bot.replay.config import (
    ConfigValidationError,
    input_config,
    settings_for,
    validate,
)
from crypto_bot.replay.engine import ReplayEngine, Variant
from crypto_bot.replay.exporter import payload
from crypto_bot.replay.institutional import (
    HOUR,
    NAME,
    InstitutionalContext,
    InstitutionalSettings,
    InstitutionalStrategy,
    resample,
)
from crypto_bot.replay.institutional_exit import DEFAULTS, ParameterizedPeakLock
from crypto_bot.replay.institutional_metrics import research_outcome, trade_observations
from crypto_bot.replay.models import ReplayStore
from crypto_bot.replay.research import adaptive
from crypto_bot.replay.shadow_adaptive import AdaptivePeakLock
from crypto_bot.replay.stale import QUARANTINE
from crypto_bot.replay.universe import CandleHistory
from crypto_bot.trading.domain import D, MarketContext


def history(hours=5000, descending=False):
    values = []
    for i in range(hours):
        price = 200 + i * 0.01 * (-1 if descending else 1)
        values.append(
            (
                Candle(
                    i * HOUR,
                    (i + 1) * HOUR - 1,
                    price,
                    price + 0.1,
                    price - 0.1,
                    price,
                    10,
                ),
                10000000,
            )
        )
    return CandleHistory(values)


@pytest.mark.parametrize("hours", [4, 24])
def test_strict_resampled_boundaries_and_future_mutation(hours):
    source = history(hours * 201 + hours)
    now = hours * 201 * HOUR + HOUR
    ctx = InstitutionalContext({"BTCUSDT": source})
    before = ctx.trend("BTCUSDT", hours, now, 50, 200)
    assert (
        before["ready"]
        and before["source_timestamp"] == at(hours * 201 * HOUR - 1).isoformat() + "Z"
    )
    changed = []
    for c, qv in source:
        if c.open_time >= hours * 201 * HOUR:
            c = replace(c, high=999999, close=999999)
        changed.append((c, qv))
    after = InstitutionalContext({"BTCUSDT": CandleHistory(changed)}).trend(
        "BTCUSDT", hours, now, 50, 200
    )
    assert after == before
    assert len(resample(source, hours)) == 202


@pytest.mark.parametrize("hours", [4, 24])
def test_partial_or_gapped_higher_candle_never_emitted(hours):
    rows = list(history(hours))
    assert len(resample(rows[:-1], hours)) == 0
    del rows[1]
    assert len(resample(rows, hours)) == 0


def test_btc_context_independent_of_tradable_universe_and_future():
    ctx = InstitutionalContext({"BTCUSDT": history(), "ALTUSDT": history()})
    s = InstitutionalSettings()
    now = 4800 * HOUR
    before = ctx.btc(now, s)
    assert before["regime"] == "BULL"
    changed = [
        (replace(c, close=1, low=1) if c.open_time >= now else c, qv)
        for c, qv in history()
    ]
    assert (
        InstitutionalContext({"BTCUSDT": CandleHistory(changed)}).btc(now, s) == before
    )
    assert InstitutionalContext({}).btc(now, s)["regime"] == "UNKNOWN"
    assert (
        InstitutionalContext({"BTCUSDT": history(descending=True)}).btc(now, s)[
            "regime"
        ]
        == "BEAR"
    )


def test_correlations_align_past_returns_and_never_future():
    data = history(300)
    s = InstitutionalSettings()
    ctx = InstitutionalContext({"ALTUSDT": data, "ETHUSDT": data})
    positions = [SimpleNamespace(symbol="ETHUSDT")]
    before = ctx.correlations("ALTUSDT", positions, 250 * HOUR, s)
    assert before["blocked"] and before["pairs"][0]["observations"] == 168
    changed = [
        (replace(c, close=c.close * 10) if c.open_time >= 250 * HOUR else c, qv)
        for c, qv in data
    ]
    after = InstitutionalContext(
        {"ALTUSDT": data, "ETHUSDT": CandleHistory(changed)}
    ).correlations("ALTUSDT", positions, 250 * HOUR, s)
    assert after == before


def test_insufficient_and_constant_correlation_are_explicit_allow():
    ctx = InstitutionalContext({"ALTUSDT": history(50), "ETHUSDT": history(50)})
    a = ctx.correlations(
        "ALTUSDT",
        [SimpleNamespace(symbol="ETHUSDT")],
        50 * HOUR,
        InstitutionalSettings(),
    )
    assert not a["blocked"] and a["pairs"][0]["correlation"] is None
    assert a["pairs"][0]["reason"] == "CORRELATION_INSUFFICIENT_DATA"


def audit(regime="BULL", multiplier="1"):
    trend = {"ready": True, "ema_fast": 110, "ema_slow": 100}
    return {
        "coin_trends": {k: dict(trend) for k in ("1h", "4h", "1d")},
        "btc": {"regime": regime, "risk_multiplier": multiplier},
        "correlation": {"blocked": False, "pairs": []},
    }


def context(a=None):
    return MarketContext(
        1,
        "ALTUSDT",
        at(START),
        D(100),
        D(80),
        D(5),
        D(55),
        D(99),
        D(98),
        D(1),
        D(2),
        "WATCH",
        True,
        {"institutional": a or audit(), "spread": 0.02},
    )


@pytest.mark.parametrize(
    "tf,reason", [("4h", "MULTITF_4H_NOT_BULLISH"), ("1d", "MULTITF_1D_NOT_BULLISH")]
)
def test_strategy_multitf_failure_codes(tf, reason):
    a = audit()
    a["coin_trends"][tf]["ema_fast"] = 90
    signal = InstitutionalStrategy(InstitutionalSettings()).evaluate(context(a))
    assert signal.action == "HOLD" and reason in signal.reasons


def test_strategy_bear_missing_zero_delta_and_sell_not_blocked():
    strategy = InstitutionalStrategy(InstitutionalSettings())
    assert "BTC_BEAR_REGIME" in strategy.evaluate(context(audit("BEAR", "0"))).reasons
    assert (
        "BTC_REGIME_UNAVAILABLE"
        in strategy.evaluate(context(audit("UNKNOWN", "0"))).reasons
    )
    assert (
        "SCORE_DELTA_NOT_POSITIVE"
        in strategy.evaluate(replace(context(), delta=D(0))).reasons
    )
    signal = strategy.evaluate(
        replace(context(audit("BEAR", "0")), score=D(30)), has_position=True
    )
    assert signal.action == "SELL"


class Adapter:
    def __init__(self, regime="BULL", multiplier="1", correlated=False):
        self.regime = regime
        self.multiplier = multiplier
        self.correlated = correlated

    def context(self, symbol, now, s, positions):
        a = audit(self.regime, self.multiplier)
        a["correlation"]["blocked"] = self.correlated and bool(positions)
        return a


def inst_variant(**params):
    cfg = config(
        variants=[
            {
                "strategy": NAME,
                "strategy_parameters": params,
                "risk_parameters": {
                    "daily_loss_limit_pct": 100,
                    "max_drawdown_limit_pct": 100,
                    "max_symbol_exposure_pct": 100,
                    "max_total_exposure_pct": 100,
                },
            }
        ]
    )
    v = Variant(cfg["variants"][0], START, QUARANTINE)
    v.research_context = Adapter()
    return v


@pytest.mark.parametrize("multiplier", ["1", ".5"])
def test_risk_multiplier_scales_budget_without_mutating_settings(multiplier):
    v = inst_variant()
    v.research_context = Adapter(
        "NEUTRAL" if multiplier == ".5" else "BULL", multiplier
    )
    r = row()
    r["symbol"] = "ALTUSDT"
    v.signals([r], ["ALTUSDT"], START)
    assert len(v.pending) == 1
    v.execute_signals({"ALTUSDT": {START: dict(open=100, interval="1m")}}, START, False)
    p = v.ledger.portfolio.positions()[0]
    assert abs(D(p.entry_data["risk_amount"]) - D(5) * D(multiplier)) < D(".00000001")
    assert v.settings.risk_per_trade_pct == D(".5")
    assert p.entry_data["risk_multiplier"] == multiplier


def test_execution_correlation_includes_newly_filled_position_same_cycle():
    v = inst_variant()
    v.research_context = Adapter(correlated=True)
    a = row()
    a["symbol"] = "ALTUSDT"
    b = deepcopy(a)
    b["symbol"] = "ETHUSDT"
    v.signals([a, b], ["ALTUSDT", "ETHUSDT"], START)
    assert len(v.pending) == 2
    v.execute_signals(
        {s: {START: dict(open=100, interval="1m")} for s in ("ALTUSDT", "ETHUSDT")},
        START,
        False,
    )
    assert len(v.ledger.portfolio.positions()) == 1
    assert v.blocked_reasons["CORRELATION_LIMIT"] == 1


def test_quarantine_keeps_cash_exposure_no_fabricated_exit():
    v = inst_variant()
    r = row()
    r["symbol"] = "ALTUSDT"
    v.signals([r], ["ALTUSDT"], START)
    v.execute_signals({"ALTUSDT": {START: dict(open=100, interval="1m")}}, START, False)
    cash = v.ledger.portfolio.view().cash_balance
    exposure = v.decision_view().exposure
    v.execute_signals({}, START + HOUR, False)
    p = v.ledger.portfolio.positions()[0]
    assert v.trade(p, START + HOUR)["status"] == "OPEN_STALE"
    assert v.ledger.portfolio.view().cash_balance == cash
    assert v.decision_view().exposure >= exposure


@pytest.mark.parametrize(
    "field,value",
    [
        ("ema_slow_period", 20),
        ("max_position_correlation", "1.1"),
        ("btc_neutral_risk_multiplier", "1.1"),
        ("correlation_min_observations", 200),
        ("ema_fast_period", 50.5),
    ],
)
def test_parameter_field_errors(field, value):
    with pytest.raises(ConfigValidationError) as exc:
        config(variants=[{"strategy": NAME, "strategy_parameters": {field: value}}])
    assert any(field in e["field"] for e in exc.value.errors)


def test_config_old_new_roundtrip_and_old_settings_type():
    old = config()
    assert "context_warmup_hours" not in old
    assert type(settings_for(old["variants"][0])).__name__ == "PaperSettings"
    c = config(variants=[{"strategy": NAME, "exit_policy": "adaptive_peak_lock_v1"}])
    assert c["context_warmup_hours"] == 4824
    assert validate(input_config(c)) == c
    assert type(settings_for(c["variants"][0])) is InstitutionalSettings


def peak_entry():
    return {
        "entry_fill_price": "100",
        "original_stop_price": "98",
        "quantity": "5",
        "risk_amount": "10",
        "atr": 1,
        "notional": "500",
        "entry_fee": ".5",
    }


def test_default_adaptive_identical_and_custom_thresholds_ratchet():
    e = peak_entry()
    s = InstitutionalSettings()
    original = AdaptivePeakLock(e, s)
    custom = ParameterizedPeakLock(e, s, DEFAULTS)
    for price in ("101.4", "101.5", "102", "104", "103"):
        old = original.ratchet(original.state, D(price), START)
        new = custom.ratchet(custom.state, D(price), START)
        assert new == old
        assert new.stop >= custom.state.stop
        original.state = old
        custom.state = new
    assert old.stage == 3
    p = SimpleNamespace(
        entry_price=D(100),
        original_stop=D(98),
        quantity=D(5),
        notional=D(500),
        entry_fee=D(".5"),
        entry_data={"risk_amount": "10", "atr": 1},
    )
    assert type(adaptive(p, s)) is AdaptivePeakLock
    p.entry_data["adaptive_exit_parameters"] = DEFAULTS | {
        "activation_break_even_r": ".5"
    }
    assert type(adaptive(p, s)) is ParameterizedPeakLock
    assert adaptive(p, s).thresholds[0] == D(101)


def test_fee_slippage_break_even_and_giveback_metrics():
    s = InstitutionalSettings()
    model = AdaptivePeakLock(peak_entry(), s)
    assert model.break_even > D(100)
    p = SimpleNamespace(
        entry_data={
            "risk_amount": "10",
            "adaptive_peak_state": {"stage": 2, "times": ["t", "t", None]},
        },
        entry_price=D(100),
        quantity=D(5),
        notional=D(500),
        entry_fee=D(".5"),
        mfe_price=D(104),
        mae_price=D(99),
        status="CLOSED",
        realized_pnl=D(10),
        exit_reason="ADAPTIVE_PEAK_LOCK",
        stop_price=D(103),
    )
    value = trade_observations(p, s, START)
    assert value["mfe_r"] == 2 and value["mae_r"] == 0.5
    assert value["maximum_high_since_entry"] == "104"
    assert value["research_exit_stage"] == "PEAK_TRAIL"
    assert value["peak_to_exit_giveback_r"] > 0
    t = {
        "status": "CLOSED",
        "r_multiple": -1,
        "mfe_r": 2,
        "peak_to_exit_giveback_r": 3,
        "realized_pnl": "-10",
        "return_pct": -2,
        "notional": "500",
        "quantity": "5",
        "exit_price": "98",
    }
    assert research_outcome([t])["losers_reached_2R"] == 1
    assert research_outcome([t])["turnover_notional"] == "990.000000000000"


def test_mixed_run_preserves_control_core_results_and_btc_context(tmp_path):
    base = config(
        candidate_symbols=["ETHUSDT"], universe_size=1, end="2025-01-01T06:00:00Z"
    )
    mixed = validate(
        input_config(base)
        | {
            "variants": [
                input_config(base)["variants"][0],
                {
                    "strategy": NAME,
                    "strategy_parameters": {"correlation_filter_enabled": False},
                },
            ]
        }
    )
    # Existing legacy snapshots retain their original mixed reporting semantics.
    base.pop("market_context_version", None)
    mixed.pop("market_context_version", None)
    store = ReplayStore(tmp_path / "replay.db")
    first = store.create(base)
    second = store.create(mixed)

    class Recording(Synthetic):
        def __init__(self):
            super().__init__()
            self.hourly_symbols = []

        def hourly(self, symbol, start, end):
            self.hourly_symbols.append(symbol)
            return super().hourly(symbol, start, end)

    providers = []
    for run in (first, second):
        p = Recording()
        providers.append(p)
        ReplayEngine(store, p).run(run)
    a, ac = store.data(first, 0)
    b, bc = store.data(second, 0)
    assert a
    assert "BTCUSDT" not in providers[0].hourly_symbols
    assert "BTCUSDT" in providers[1].hourly_symbols
    assert all(t["research_btc_regime"] == "BULL" for t in b)
    assert ac == bc
    assert len(a) == len(b)
    for old, new in zip(a, b):
        assert {k: new[k] for k in old} == old
    assert set(t["symbol"] for t in b) <= {"ETHUSDT"}
    report = payload(store, second, 0, True)
    assert "average_MFE_R" in report["summary"]
    assert "research_btc_regime" in report["breakdowns"]
    if report["trades"]:
        assert "peak_to_exit_giveback_r" in report["trades"][0]
    assert store.get(second)["status"] == "COMPLETED"


def test_neutral_regime_half_risk_and_zero_variance_information():
    class Mixed(InstitutionalContext):
        def trend(self, symbol, hours, now, fast, slow):
            return dict(
                ready=True,
                ema_fast=101 if hours == 4 else 99,
                ema_slow=100,
                source_timestamp="2024-01-01T00:00:00Z",
            )

    assert Mixed({}).btc(5000 * HOUR, InstitutionalSettings())["regime"] == "NEUTRAL"
    assert (
        Mixed({}).btc(5000 * HOUR, InstitutionalSettings())["risk_multiplier"] == "0.5"
    )
    constant = CandleHistory(
        (Candle(i * HOUR, (i + 1) * HOUR - 1, 100, 100, 100, 100, 1), 1000)
        for i in range(200)
    )
    value = InstitutionalContext(
        {"ALTUSDT": constant, "ETHUSDT": constant}
    ).correlations(
        "ALTUSDT",
        [SimpleNamespace(symbol="ETHUSDT")],
        200 * HOUR,
        InstitutionalSettings(),
    )
    assert (
        value["pairs"][0]["reason"] == "CORRELATION_INSUFFICIENT_DATA"
        and not value["blocked"]
    )


def test_existing_stop_distance_sizing_already_adjusts_for_volatility():
    from crypto_bot.replay.portfolio import MemoryLedger
    from crypto_bot.risk.manager import RiskManager
    from crypto_bot.trading.domain import StrategySignal

    s = replace(
        InstitutionalSettings(),
        max_symbol_exposure_pct=D(100),
        max_total_exposure_pct=D(100),
    )
    manager = RiskManager(s)
    view = MemoryLedger(s, at(START)).portfolio.view()
    signal = StrategySignal(
        "ALTUSDT", "BUY", D(1), D(100), D(98), at(START), "x", ("x",)
    )
    small = manager.evaluate(signal, view)
    large = manager.evaluate(replace(signal, stop_reference=D(96)), view)
    assert small.approved and large.approved
    assert large.quantity < small.quantity
    assert abs(small.risk_amount - large.risk_amount) < D(".00000001")


def test_entry_diagnostic_rejections_preserve_legacy_hold_counters_and_checkpoint():
    v = inst_variant()
    v.research_context = Adapter("BEAR", "0")
    r = row()
    r["symbol"] = "ALTUSDT"
    v.signals([r], ["ALTUSDT"], START)
    assert v.pending == []
    assert v.institutional_rejections["BTC_BEAR_REGIME"] == 1
    assert sum(v.blocked.values()) == 0
    assert v.checkpoint()["institutional_rejections"]["BTC_BEAR_REGIME"] == 1
    assert any(e["event"] == "INSTITUTIONAL_ENTRY_REJECTED" for e in v.events)


def test_bear_regime_does_not_block_execution_sell():
    v = inst_variant()
    r = row()
    r["symbol"] = "ALTUSDT"
    v.signals([r], ["ALTUSDT"], START)
    v.execute_signals({"ALTUSDT": {START: dict(open=100, interval="1m")}}, START, False)
    v.research_context = Adapter("BEAR", "0")
    r["total_score"] = 30
    v.signals([r], [], START + HOUR)
    assert v.pending[0][0].action == "SELL"
    v.execute_signals(
        {"ALTUSDT": {START + HOUR: dict(open=100, interval="1m")}}, START + HOUR, False
    )
    assert not v.ledger.portfolio.positions()


def test_configurable_adaptive_threshold_order_validation():
    with pytest.raises(ConfigValidationError) as exc:
        config(
            variants=[
                {
                    "exit_policy": "adaptive_peak_lock_v1",
                    "exit_parameters": {"activation_break_even_r": 2},
                }
            ]
        )
    assert any("activation_trailing_r" in e["field"] for e in exc.value.errors)
    with pytest.raises(ConfigValidationError):
        config(
            variants=[
                {
                    "exit_policy": "adaptive_peak_lock_v1",
                    "exit_parameters": {"tight_trail_atr_multiplier": 1},
                }
            ]
        )
