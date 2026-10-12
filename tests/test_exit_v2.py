"""Deterministic research exits; no real market data or production account."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_institutional_replay import Adapter
from test_replay import START, config, row
from test_shadow_exit import entry

from crypto_bot.replay.clock import at
from crypto_bot.replay.config import ConfigValidationError, input_config, validate
from crypto_bot.replay.engine import Variant
from crypto_bot.replay.exit_v2 import BE, DEFAULTS, DELAYED, ExitV2, SetupRearm
from crypto_bot.replay.exporter import payload
from crypto_bot.replay.institutional_metrics import research_outcome
from crypto_bot.replay.models import ReplayStore
from crypto_bot.replay.rejections import from_audit
from crypto_bot.replay.shadow_adaptive import AdaptivePeakLock
from crypto_bot.replay.stale import QUARANTINE
from crypto_bot.trading.config import PaperSettings
from crypto_bot.trading.domain import D, q


def model(name=DELAYED):
    return ExitV2(entry(), PaperSettings(), name, DEFAULTS[name])


def variant(name=DELAYED, rearm=False):
    cfg = config(
        variants=[
            dict(
                strategy="institutional_style_v1",
                exit_policy=name,
                setup_rearm="setup_rearm_v1" if rearm else "OFF",
            )
        ]
    )
    v = Variant(cfg["variants"][0], START, QUARANTINE)
    v.research_context = Adapter()
    return v


def open_position(v, now=START):
    v.signals([row()], ["BTCUSDT"], now)
    v.execute_signals({"BTCUSDT": {now: dict(open=100, interval="1m")}}, now, False)
    return v.ledger.portfolio.positions()[0]


@pytest.mark.parametrize("name", [BE, DELAYED])
def test_original_until_exact_1r_and_fee_slippage_break_even(name):
    m = model(name)
    s = m.ratchet(m.state, D("101.999999"), START)
    assert s.stage == 0 and s.stop == m.original
    s = m.ratchet(s, D(102), START + 60000)
    assert s.stage == 1 and s.stop == m.break_even
    fill = q(s.stop * (1 - m.slip))
    notional = q(fill * m.quantity)
    assert notional - q(notional * m.fee) - m.cost >= 0


def test_delayed_stages_ratcheting_and_high_watermark():
    m = model()
    s = m.ratchet(m.state, D("103.99"), START)
    assert s.stage == 1 and s.stop == m.break_even
    s = m.ratchet(s, D(104), START + 60000)
    assert s.stage == 2 and s.stop == D("102.75")
    later = m.ratchet(s, D(105), START + 120000)
    assert later.stop == D("103.75")
    assert m.ratchet(later, D(103), START + 180000).stop == later.stop
    s = m.ratchet(later, D(108), START + 240000)
    assert s.stage == 3 and s.stop == D("107.25")
    audit = m.audit(None)
    assert audit["activation_4r_time"] is None  # ratchet returns state, does not mutate
    assert m.state.highest == 100  # no unobserved future high


def test_break_even_never_trails_even_after_10r():
    m = model(BE)
    s = m.ratchet(m.state, D(120), START)
    assert s.stage == 1 and s.stop == m.break_even
    assert s.times[1:] == (None, None)


def test_retrace_intrabar_and_no_future_candle():
    v = variant()
    p = open_position(v)
    candle = dict(
        time=START,
        close_time=START + 59999,
        open=100.1,
        high=105,
        low=100,
        close=100,
        interval="1m",
    )
    with pytest.raises(ValueError, match="not CLOSED"):
        v.bar("BTCUSDT", candle, START, True)
    assert p.highest == p.entry_price
    v.bar("BTCUSDT", candle, START + 60000, True)
    assert not v.ledger.portfolio.positions()
    assert v.closed[0]["exit_stage"] in ("BREAK_EVEN", "PEAK_TRAIL")


def test_rearm_snapshot_sequence_and_checkpoint():
    r = SetupRearm()
    r.closed("BTCUSDT", "BREAK_EVEN", START)
    assert not r.allowed("BTCUSDT", START)
    assert not r.observe("BTCUSDT", False, START)  # same snapshot
    assert not r.observe("BTCUSDT", True, START + 3600000)
    assert not r.allowed("BTCUSDT", START + 3600000)
    assert r.observe("BTCUSDT", False, START + 7200000)
    assert not r.allowed("BTCUSDT", START + 7200000)
    restored = SetupRearm(json.loads(json.dumps(r.checkpoint())))
    restored.observe("BTCUSDT", True, START + 10800000)
    assert restored.allowed("BTCUSDT", START + 10800000)
    assert not restored.allowed("BTCUSDT", START)  # cached pre-exit signal
    assert restored.metrics()["rearm_success_count"] == 1
    assert restored.metrics()["average_time_to_rearm"] == 7200


@pytest.mark.parametrize("stage", ["ORIGINAL_STOP", "STRATEGY_EXIT"])
def test_nonprotective_exits_do_not_block(stage):
    r = SetupRearm()
    r.closed("BTCUSDT", stage, START)
    assert r.allowed("BTCUSDT", START)


def test_pipeline_rearm_blocks_then_false_true_allows():
    v = variant(rearm=True)
    open_position(v)
    candle = dict(
        time=START,
        close_time=START + 59999,
        open=100.1,
        high=105,
        low=100,
        close=100,
        interval="1m",
    )
    v.bar("BTCUSDT", candle, START + 60000, True)
    assert v.rearm and not v.ledger.portfolio.positions()
    v.signals([row()], ["BTCUSDT"], START + 3600000)
    assert not v.pending and v.rearm.blocks == 1
    false = row()
    false["total_score"] = 30
    v.signals([false], ["BTCUSDT"], START + 7200000)
    assert v.rearm.successes == 1
    v.signals([row()], ["BTCUSDT"], START + 10800000)
    assert len(v.pending) == 1
    state = v.checkpoint()
    assert state["setup_rearm"]["successes"] == 1


def test_strategy_sell_kept_for_v2_but_old_adaptive_unchanged():
    for name in [BE, DELAYED, "adaptive_peak_lock_v1"]:
        v = variant(name)
        open_position(v)
        sell = row()
        sell["total_score"] = 30
        v.signals([sell], ["BTCUSDT"], START + 3600000)
        assert bool(v.pending) == (name != "adaptive_peak_lock_v1")


def test_stale_quarantine_has_no_fabricated_exit():
    v = variant(rearm=True)
    p = open_position(v)
    v.fresh_positions(set(), START + 60000)
    v.bar(
        "BTCUSDT",
        dict(
            time=START,
            close_time=START + 59999,
            open=100,
            high=110,
            low=90,
            close=100,
            interval="1m",
        ),
        START + 60000,
        True,
    )
    assert p.status == "OPEN" and not v.closed and not v.rearm.symbols


def test_schema_roundtrip_and_bad_rearm_thresholds():
    v = variant(rearm=True)
    raw = input_config(config())
    raw["variants"] = [deepcopy(v.config)]
    # Snapshot internal fields must not be imported; use the shared serializer.
    snapshot = config(
        variants=[
            dict(
                strategy="institutional_style_v1",
                exit_policy=DELAYED,
                setup_rearm="setup_rearm_v1",
            )
        ]
    )
    assert validate(input_config(snapshot)) == snapshot
    bad = input_config(snapshot)
    bad["variants"][0]["exit_parameters"]["tightening_activation_r"] = "1"
    with pytest.raises(ConfigValidationError):
        validate(bad)
    bad = input_config(snapshot)
    bad["variants"][0]["setup_rearm"] = "24h"
    with pytest.raises(ConfigValidationError):
        validate(bad)


def test_rearm_audit_not_opaque_other():
    out = from_audit(
        [dict(event="SETUP_NOT_REARMED", symbol="BTCUSDT", action="BUY")], {"other": 1}
    )
    assert out["blocked_entry_reasons"] == {"SETUP_NOT_REARMED": 1}
    assert out["blocked_entry_categories"] == {"setup_not_rearmed": 1}
    assert out["blocked_entry_unresolved_attempts"] == 0


def test_metrics_reentries_capture_and_proven_losers():
    t = dict(
        status="CLOSED",
        symbol="BTCUSDT",
        entry_time=at(START).isoformat() + "Z",
        exit_time=at(START + 3600000).isoformat() + "Z",
        r_multiple=-1,
        mfe_r=2,
        realized_pnl="-2",
        return_pct=-2,
        notional="100",
        quantity="1",
        exit_price="98",
    )
    next_trade = t | dict(
        entry_time=at(START + 7200000).isoformat() + "Z",
        exit_time=at(START + 10800000).isoformat() + "Z",
    )
    s = research_outcome([t, next_trade])
    assert s["same_symbol_reentry_within_1h"] == 1
    assert s["trades_reached_1R_then_lost"] == s["trades_reached_2R_then_lost"] == 2
    assert s["average_mfe_capture_ratio"] == -0.5


@pytest.mark.parametrize("name", [BE, DELAYED])
def test_full_compact_export_new_audit_and_open_records(tmp_path, name):
    snapshot = config(
        variants=[dict(strategy="institutional_style_v1", exit_policy=name)]
    )
    store = ReplayStore(tmp_path / "replay.db")
    rid = store.create(snapshot)
    v = Variant(snapshot["variants"][0], START, QUARANTINE)
    v.research_context = Adapter()
    open_position(v)
    t = v.trade(v.ledger.portfolio.positions()[0], START + 60000)
    store.save_step(rid, [(0, [t], [], [], [], [])], {}, None)
    store.complete(
        rid,
        [dict(metrics={"trade_count": 0, "blocked_entries": {}}, breakdowns={})],
        {},
    )
    full = payload(store, rid, 0)
    compact = payload(store, rid, 0, True)
    assert full["open_positions"] == compact["open_positions"]
    assert full["open_positions"][0]["activation_4r_time"] is None


def test_old_adaptive_default_model_bit_exact():
    old = AdaptivePeakLock(entry(), PaperSettings())
    from crypto_bot.replay.research import adaptive

    p = SimpleNamespace(
        entry_price=D(100),
        original_stop=D(98),
        quantity=D(1),
        notional=D(100),
        entry_fee=D(".1"),
        entry_data={"risk_amount": "2", "atr": 1},
    )
    new = adaptive(p, PaperSettings())
    for price in map(D, ["101", "101.5", "102", "105", "103"]):
        old.state = old.ratchet(old.state, price, START)
        new.state = new.ratchet(new.state, price, START)
        assert old.state == new.state
    assert old.audit(None) == new.audit(None)


@pytest.mark.parametrize("policy", ["baseline_v1", "adaptive_peak_lock_v1"])
def test_control_matches_prechange_6a17ea4_trades_cash_orders_fills(policy):
    from exit_v2_control_fixture import control_snapshot

    expected = json.loads(
        (Path(__file__).parent / "fixtures/exit-v2-controls-6a17ea4.json").read_text()
    )
    actual = json.loads(json.dumps(control_snapshot(policy), default=str))
    assert actual == expected[policy]


def test_pipeline_original_stop_no_rearm_and_strategy_exit_no_rearm():
    for strategy_exit in (False, True):
        v = variant(rearm=True)
        open_position(v)
        if strategy_exit:
            # Protect the stop first, then exit by strategy: no forced re-arm.
            v.bar(
                "BTCUSDT",
                dict(
                    time=START,
                    close_time=START + 59999,
                    open=102.1,
                    high=103,
                    low=102,
                    close=102.5,
                    interval="1m",
                ),
                START + 60000,
                True,
            )
            sell = row()
            sell["total_score"] = 30
            v.signals([sell], ["BTCUSDT"], START + 3600000)
            v.execute_signals(
                {"BTCUSDT": {START + 3600000: dict(open=102.5, interval="1m")}},
                START + 3600000,
                False,
            )
            assert v.closed[0]["exit_stage"] == "STRATEGY_EXIT"
        else:
            v.bar(
                "BTCUSDT",
                dict(
                    time=START,
                    close_time=START + 59999,
                    open=100,
                    high=100,
                    low=97,
                    close=98,
                    interval="1m",
                ),
                START + 60000,
                True,
            )
            assert v.closed[0]["exit_stage"] == "ORIGINAL_STOP"
        assert not v.ledger.portfolio.positions() and not v.rearm.symbols
        assert v.rearm.allowed("BTCUSDT", START + 7200000)


def test_missing_context_and_portfolio_correlation_do_not_reset_setup():
    v = variant(rearm=True)
    v.rearm.closed("BTCUSDT", "BREAK_EVEN", START)
    v.research_context = Adapter("UNKNOWN", "0")
    v.signals([row()], ["BTCUSDT"], START + 3600000)
    assert v.rearm.successes == 0

    class Correlated(Adapter):
        def context(self, *args):
            audit = super().context(*args)
            audit["correlation"]["blocked"] = True
            return audit

    v.research_context = Correlated()
    v.signals([row()], ["BTCUSDT"], START + 7200000)
    assert v.rearm.successes == 0


def test_gap_fill_not_fabricated_at_break_even():
    m = model(BE)
    m.state = m.ratchet(m.state, D(103), START)
    p = SimpleNamespace()
    hit, _, _ = m.observe(
        dict(open=99, high=100, low=98, close=99), p, START + 60000, True
    )
    assert hit[0] == 99 and hit[0] < m.break_even


def test_predeclared_evaluation_flags_not_winner_selection():
    from crypto_bot.replay.exit_v2 import evaluation

    base = dict(
        trade_count=100,
        largest_realized_R=8,
        profit_factor=0.8,
        win_rate=20,
        expectancy_r=-0.1,
        turnover_notional="1000",
        fees="1",
        return_pct=-10,
    )
    candidate = base | dict(
        trade_count=160,
        largest_realized_R=2,
        profit_factor=0.9,
        win_rate=50,
        expectancy_r=-0.2,
        turnover_notional="2000",
        fees="2",
        return_pct=-20,
    )
    configs = [
        dict(strategy="institutional_style_v1", exit_policy=p)
        for p in ["baseline_v1", DELAYED]
    ]
    e = evaluation(configs, [dict(metrics=base), dict(metrics=candidate)])
    assert len(e["warnings"][0]["warnings"]) == 4
    assert not e["winner_selected"]


def test_rearm_without_context_fails_closed_not_crashes():
    v = variant(rearm=True)
    v.research_context = None
    v.rearm.closed("BTCUSDT", "BREAK_EVEN", START)
    v.signals([row()], ["BTCUSDT"], START + 3600000)
    assert not v.pending and not v.rearm.successes


def test_five_variant_engine_result_and_checkpoint(tmp_path, monkeypatch):
    from dataclasses import replace

    import test_replay

    from crypto_bot.replay.clock import ms
    from crypto_bot.replay.engine import ReplayEngine

    monkeypatch.setattr(test_replay, "START", ms("2024-01-01"))
    raw = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/replay/institutional-exit-v2-2024.config.json"
        ).read_text()
    )
    raw.update(
        end="2024-01-03T00:00:00Z",
        candidate_symbols=["BTCUSDT", "ETHUSDT"],
        universe_size=2,
    )

    class Fixed(test_replay.Synthetic):
        def hourly(self, *args):
            return [
                (
                    replace(
                        c,
                        open=c.open + 200,
                        high=c.high + 200,
                        low=c.low + 200,
                        close=c.close + 200,
                    ),
                    vol,
                )
                for c, vol in super().hourly(*args)
            ]

        def minutes(self, *args):
            return {
                t: c | {k: c[k] + 200 for k in ("open", "high", "low", "close")}
                for t, c in super().minutes(*args).items()
            }

    s = ReplayStore(tmp_path / "replay.db")
    rid = s.create(validate(raw))
    ReplayEngine(s, Fixed()).run(rid)
    result = s.get(rid)
    assert result["status"] == "COMPLETED" and len(result["variants"]) == 5
    assert not result["metadata"]["institutional_exit_v2_evaluation"]["winner_selected"]
    assert "setup_rearm" in s.checkpoint(rid)["variants"][4]
    for i, v in enumerate(result["variants"]):
        assert "rearm_block_count" in v["summary"]["metrics"]
        assert "average_mfe_capture_ratio" in v["summary"]["metrics"]
        full = payload(s, rid, i)
        compact = payload(s, rid, i, True)
        assert full["summary"] == compact["summary"]
        assert full["trades"] or full["open_positions"]
