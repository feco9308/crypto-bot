"""Fresh setup admission uses the complete institutional predicate, not capacity."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_institutional_entry_v1 import GOLDEN
from test_replay_btc_gate import btc_row
from test_replay_companion_isolation import (
    START,
    Tape,
    controlled,  # noqa: F401
    core,
)
from test_shadow_exit import bar

from crypto_bot.replay import stale
from crypto_bot.replay.config import ConfigValidationError, input_config, validate
from crypto_bot.replay.engine import ReplayEngine, Variant
from crypto_bot.replay.fresh_setup import HOUR, NAME, FreshSetupAdmission
from crypto_bot.replay.models import ReplayStore
from crypto_bot.replay.rejections import from_audit

ARTIFACT = (
    Path(__file__).parents[1]
    / "docs/replay/institutional-entry-v3-2024-exploration.config.json"
)


def candidate():
    r = btc_row()
    r["total_score"] = 90
    r["momentum"]["delta_4h"] = 20
    r["features"].update(rsi=55, relative_volume=2, atr=1)
    return r


def institutional_context(blocked=False, ready=True):
    return dict(
        btc=dict(regime="BULL", risk_multiplier="1"),
        coin_trends={
            key: dict(ready=ready, ema_fast=110, ema_slow=100)
            for key in ("1h", "4h", "1d")
        },
        correlation=dict(blocked=blocked, pairs=[]),
    )


def variant():
    raw = json.loads(ARTIFACT.read_text())
    v = Variant(validate(raw)["variants"][1], START, stale.QUARANTINE)
    v.research_context = SimpleNamespace(context=lambda *args: institutional_context())
    return v


def observe(v, r, hour, setup=True):
    r = copy.deepcopy(r)
    r["total_score"] = 90 if setup else 1
    now = START + hour * HOUR
    before = copy.deepcopy(r)
    ledger = copy.deepcopy(v.ledger.checkpoint())
    v.signals([r], {r["symbol"]}, now)
    assert r == before and v.ledger.checkpoint() == ledger
    return now


def test_false_true_true_false_true_and_initial_unknown():
    s = FreshSetupAdmission()
    assert s.observe("X", True, True, START) is None
    assert not s.admit("X", START)
    s.observe("X", False, True, START + HOUR)
    assert s.observe("X", True, True, START + 2 * HOUR) == "FRESH_SETUP_ARMED"
    assert s.admit("X", START + 2 * HOUR)
    assert not s.admit("X", START + 2 * HOUR)
    s.observe("X", True, True, START + 3 * HOUR)
    assert not s.admit("X", START + 3 * HOUR)
    s.observe("X", False, True, START + 4 * HOUR)
    assert s.observe("X", True, True, START + 5 * HOUR) == "FRESH_SETUP_REARMED"
    assert s.admit("X", START + 5 * HOUR)


def test_full_strategy_gate_audit_and_immutable_row():
    v, r = variant(), candidate()
    observe(v, r, 0, False)
    observe(v, r, 1)
    assert len(v.pending) == 1
    e = v.events[-1]
    assert e["event"] == "FRESH_SETUP_ARMED"
    assert e["previous_setup_state"] is False and e["current_setup_state"] is True
    assert e["setup_age_hours"] == 0
    assert e["original_strategy_decision"]["action"] == "BUY"
    observe(v, r, 2)
    assert not v.pending
    assert v.events[-1]["event"] == "ENTRY_NOT_FRESH_SETUP"
    assert v.events[-1]["setup_age_hours"] == 1
    d = from_audit(v.events, v.blocked)
    assert d["blocked_entry_categories"]["entry_not_fresh_setup"] == 1
    assert d["blocked_entry_breakdown_complete"]
    observe(v, r, 3, False)
    observe(v, r, 4)
    assert v.pending and v.events[-1]["event"] == "FRESH_SETUP_REARMED"


@pytest.mark.parametrize(
    "failure",
    [
        "max open positions reached",
        "total exposure limit reached",
        "insufficient free balance",
    ],
)
def test_fresh_capacity_miss_expires(failure):
    v, r = variant(), candidate()
    observe(v, r, 0, False)
    now = observe(v, r, 1)
    v.risk = SimpleNamespace(evaluate=lambda *args, **kwargs: SimpleNamespace())
    # Actual RiskManager rejection is exercised separately below. Recording any
    # original rejection expires this opportunity without resetting its setup.
    v.block_entry(v.pending[0][0], [failure])
    assert v.events[-1]["event"] == "FRESH_SETUP_MISSED_CAPACITY"
    assert v.events[-1]["block_risk_reason"] == [failure]
    assert v.events[-1]["timestamp"].startswith("2024-")
    observe(v, r, 2)
    assert not v.pending and v.events[-1]["event"] == "ENTRY_NOT_FRESH_SETUP"
    assert v.fresh_setup.states[r["symbol"]]["fresh_time"] == now


def test_real_risk_rejection_and_no_fill_do_not_carry_forward():
    for no_fill in [False, True]:
        v, r = variant(), candidate()
        v.settings = v.settings.__class__(
            **{**v.settings.__dict__, "minimum_order_value": 10000}
        )
        from crypto_bot.risk.manager import RiskManager

        v.risk = RiskManager(v.settings)
        observe(v, r, 0, False)
        now = observe(v, r, 1)
        v.execute_signals({} if no_fill else {r["symbol"]: {now: bar()}}, now, False)
        assert not v.execution.fills
        missed = [e for e in v.events if e["event"] == "FRESH_SETUP_MISSED_CAPACITY"]
        assert len(missed) == 1 and missed[0]["block_risk_reason"]
        observe(v, r, 2)
        assert not v.pending


def test_sell_bypass_exit_and_reentry_same_setup_blocked():
    v, r = variant(), candidate()
    observe(v, r, 0, False)
    now = observe(v, r, 1)
    v.execute_signals({r["symbol"]: {now: bar()}}, now, False)
    assert v.execution.fills[-1]["side"] == "BUY"
    # Reference exit's bearish hourly EMA may differ from institutional MTF
    # context: complete entry setup remains TRUE during this strategy SELL.
    r["features"].update(ema50=90, ema200=100)
    now = observe(v, r, 2)
    assert v.pending[0][0].action == "SELL"
    assert v.fresh_setup.states[r["symbol"]]["current"] is True
    v.execute_signals({r["symbol"]: {now: bar()}}, now, False)
    assert v.execution.fills[-1]["side"] == "SELL"
    observe(v, r, 3)
    assert not v.pending and v.events[-1]["event"] == "ENTRY_NOT_FRESH_SETUP"


def test_correlation_and_holdings_do_not_define_setup():
    v, r = variant(), candidate()
    observe(v, r, 0, False)
    v.research_context = SimpleNamespace(
        context=lambda *args: institutional_context(blocked=True)
    )
    observe(v, r, 1)
    assert v.fresh_setup.states[r["symbol"]]["current"] is True
    assert not v.pending
    assert any(e["event"] == "FRESH_SETUP_MISSED_CAPACITY" for e in v.events)
    v.research_context = SimpleNamespace(context=lambda *args: institutional_context())
    observe(v, r, 2)
    assert not v.pending


def test_missing_unobservable_no_false_transition_and_stale_unchanged():
    v, r = variant(), candidate()
    observe(v, r, 0, False)
    now = observe(v, r, 1)
    v.execute_signals({r["symbol"]: {now: bar()}}, now, False)
    v.fresh_positions({}, now + 60000)
    assert stale.state(v.ledger.portfolio.positions()[0])["quarantined"]
    wallet, risk = copy.deepcopy(v.ledger.checkpoint()), v.decision_view()
    v.signals([], set(), now + HOUR)
    v.research_context = SimpleNamespace(
        context=lambda *args: institutional_context(ready=False)
    )
    observe(v, r, 3, False)
    assert v.fresh_setup.states[r["symbol"]]["current"] is True
    v.research_context = SimpleNamespace(context=lambda *args: institutional_context())
    observe(v, r, 4)
    assert v.fresh_setup.states[r["symbol"]]["armed_count"] == 1
    assert v.ledger.checkpoint() == wallet and v.decision_view() == risk
    s = FreshSetupAdmission()
    s.observe("X", True, False, START)
    assert s.states["X"]["current"] is None
    assert s.observe("X", True, True, START + HOUR) is None


def test_unavailable_momentum_does_not_fake_false():
    v, r = variant(), candidate()
    observe(v, r, 0)
    r["momentum"]["delta_4h"] = None
    observe(v, r, 1)
    assert v.fresh_setup.states[r["symbol"]]["current"] is True
    r["momentum"]["delta_4h"] = 20
    observe(v, r, 2)
    assert not v.pending


def small_run(path, names, stop=None, off=False):
    raw = json.loads(GOLDEN.read_text())["public_config"]
    base = raw["variants"][0]
    raw["variants"] = [
        base
        | dict(name=n)
        | (
            {"entry_admission": NAME}
            if n == "L"
            else {"entry_admission": "OFF"}
            if off
            else {}
        )
        for n in names
    ]
    store = ReplayStore(path / "replay.db")
    rid = store.create(validate(raw))

    def check():
        cp = store.checkpoint(rid)
        if stop and cp and cp["next_time"] >= stop:
            raise InterruptedError("fixture checkpoint pause")

    try:
        ReplayEngine(store, Tape(), check).run(rid)
    except InterruptedError:
        pass
    return store, rid


def test_control_companions_order_checkpoint_and_no_lookahead(request, tmp_path):
    request.getfixturevalue("controlled")
    golden = json.loads(GOLDEN.read_text())["core"]
    for names, off in [("A", False), ("A", True), ("AL", False), ("LA", False)]:
        store, rid = small_run(tmp_path / (names + str(off)), names, off=off)
        assert core(store, rid, names.index("A")) == golden
    full, a = small_run(tmp_path / "full", "AL")
    rev, c = small_run(tmp_path / "reverse", "LA")
    resumed, b = small_run(tmp_path / "resumed", "AL", stop=START + 8 * HOUR)
    assert "fresh_setup" in resumed.checkpoint(b)["variants"][1]
    ReplayEngine(resumed, Tape()).run(b)
    for i in range(2):
        assert core(full, a, i) == core(resumed, b, i) == core(rev, c, 1 - i)
        assert (
            full.get(a)["variants"][i]["summary"]
            == resumed.get(b)["variants"][i]["summary"]
        )
        assert full.checkpoint(a)["variants"][i] == resumed.checkpoint(b)["variants"][i]
        assert full.checkpoint(a)["variants"][i] == rev.checkpoint(c)["variants"][1 - i]

        def events(store, rid, index):
            with store.connection() as db:
                return [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT data FROM replay_audit_events WHERE run_id=? AND variant=? ORDER BY id",
                        (rid, index),
                    )
                ]

        assert events(full, a, i) == events(resumed, b, i) == events(rev, c, 1 - i)

    # No archive future metadata influences the state: only observed contexts.
    v, r = variant(), candidate()
    r["future_snapshot"] = {"total_score": 1, "coverage_ends": START + HOUR}
    observe(v, r, 0, False)
    observe(v, r, 1)
    assert v.pending


def test_config_roundtrip_legacy_and_frozen_parameters():
    raw = json.loads(ARTIFACT.read_text())
    cfg = validate(raw)
    assert validate(input_config(cfg)) == cfg
    assert cfg["market_context_version"] == "ISOLATED_TOPN_V1"
    assert cfg["dataset_role"] == "EXPLORATION"
    assert (cfg["start"], cfg["end"]) == (
        "2024-01-01T00:00:00Z",
        "2025-01-01T00:00:00Z",
    )
    a, research = cfg["variants"]
    assert {k: v for k, v in a.items() if k != "name"} == {
        k: v for k, v in research.items() if k not in ("name", "entry_admission")
    }
    assert all(v is None for v in a["entry_filter"].values())
    assert "entry_admission" not in a
    assert research["entry_admission"] == NAME
    old = json.loads(GOLDEN.read_text())["public_config"]
    neutral = copy.deepcopy(old)
    neutral["variants"][0]["entry_admission"] = "OFF"
    assert validate(old) == validate(neutral)
    for wrong in ["wrong", True]:
        bad = copy.deepcopy(raw)
        bad["variants"][1]["entry_admission"] = wrong
        with pytest.raises(ConfigValidationError):
            validate(bad)


@pytest.mark.parametrize(
    "limit,reason",
    [
        ("positions", "max open positions reached"),
        ("exposure", "total exposure limit reached"),
        ("cash", "insufficient free balance"),
    ],
)
def test_actual_capacity_rejection_consumes_fresh_hour(limit, reason):
    from dataclasses import replace

    from crypto_bot.risk.manager import RiskManager

    v, r = variant(), candidate()
    other = copy.deepcopy(r)
    other["symbol"] = "OTHERUSDT"
    observe(v, other, 0, False)
    now = observe(v, other, 1)
    v.execute_signals({"OTHERUSDT": {now: bar()}}, now, False)
    assert len(v.ledger.portfolio.positions()) == 1
    if limit == "cash":
        v.ledger.portfolio.reserve(v.ledger.portfolio.view().available_cash)
    else:
        v.settings = replace(
            v.settings,
            **{
                "max_open_positions"
                if limit == "positions"
                else "max_total_exposure_pct": 1,
            },
        )
        v.risk = RiskManager(v.settings)
    observe(v, r, 2, False)
    now = observe(v, r, 3)
    v.execute_signals({s: {now: bar()} for s in [r["symbol"], "OTHERUSDT"]}, now, False)
    e = [e for e in v.events if e["event"] == "FRESH_SETUP_MISSED_CAPACITY"][-1]
    assert e["block_risk_reason"] == (reason,)
    assert len(v.ledger.portfolio.positions()) == 1
    observe(v, r, 4)
    assert not v.pending


def test_shared_market_frame_and_setup_checkpoint_immutable():
    from collections import defaultdict

    from test_replay_companion_isolation import RotatingUniverse, features, score

    from crypto_bot.config.settings import Settings
    from crypto_bot.replay.market_context import frame
    from crypto_bot.replay.universe import CandleHistory

    histories = {
        s: CandleHistory(Tape().hourly(s, START - 524 * HOUR, START + 14 * HOUR))
        for s in ["NEARUSDT", "OPUSDT"]
    }
    shared = frame(
        RotatingUniverse(histories, None, 0),
        Settings(),
        START + 6 * HOUR,
        defaultdict(list),
        score,
        features,
    )
    before = shared.rows()
    v = variant()
    v.signals(shared.rows(), {r["symbol"] for r in before}, START + 6 * HOUR)
    assert shared.rows() == before
    s = FreshSetupAdmission()
    s.observe("X", False, True, START)
    s.observe("X", True, True, START + HOUR)
    s.admit("X", START + HOUR)
    checkpoint = s.checkpoint()
    restored = FreshSetupAdmission(checkpoint)
    for step, setup, observable in [
        (2, False, False),
        (3, True, True),
        (4, False, True),
        (5, True, True),
    ]:
        assert restored.observe(
            "X", setup, observable, START + step * HOUR
        ) == s.observe("X", setup, observable, START + step * HOUR)
        assert restored.checkpoint() == s.checkpoint()
    assert checkpoint["X"]["armed_count"] == 1


def test_held_only_exit_row_cannot_seed_false_or_rearm():
    v, r = variant(), candidate()
    r["auto_eligible"] = False
    observe(v, r, 0, False)
    assert v.fresh_setup.states[r["symbol"]]["current"] is None
    r["auto_eligible"] = True
    observe(v, r, 1)
    assert not v.pending  # First observable TRUE is still not fresh.
    observe(v, r, 2, False)
    observe(v, r, 3)
    assert v.pending
    r["auto_eligible"] = False
    observe(v, r, 4, False)
    r["auto_eligible"] = True
    observe(v, r, 5)
    assert not v.pending  # Holding a symbol outside TopN cannot rearm it.
