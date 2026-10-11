"""Adaptive policy is shadow-only, deterministic, with audited observed paths."""

import copy
from dataclasses import replace

import pytest
from test_replay import START, config
from test_replay_integration import Stops, token, web_case  # noqa: F401
from test_shadow_exit import bar, completed_source, definitions, entry, job

from crypto_bot.replay.config import ConfigValidationError, settings_for, variant
from crypto_bot.replay.registry import EXITS
from crypto_bot.replay.shadow_adaptive import NAME
from crypto_bot.replay.shadow_config import SHADOW_EXITS, validate
from crypto_bot.replay.shadow_engine import MINUTE, ShadowEngine, ShadowPosition
from crypto_bot.replay.shadow_metrics import analysis, export
from crypto_bot.trading.domain import D


def definition():
    return dict(id=NAME, name=NAME, exit_policy=NAME, exit_parameters={})


def position(e=None):
    return ShadowPosition(
        e or entry(),
        definition(),
        settings_for(config()["variants"][0]),
        "RESEARCH_QUARANTINE_STALE",
    )


def observe(p, i, o, h, lo, c=None, conservative=True):
    t = START + i * MINUTE
    p.bar(bar(t, o, h, lo, o if c is None else c), t + MINUTE, conservative)


def test_initial_stop_only_before_075r():
    p = position()
    observe(p, 0, 100, 101.49, 99)
    assert p.p.stop_price == 98
    assert p.adaptive.state.stage == 0
    observe(p, 1, 99, 100, 97)
    assert p.result["exit_reference_price"] == "98"
    assert p.result["exit_reason"] == "ORIGINAL_STOP"


def test_break_even_activation_and_costs():
    p = position()
    observe(p, 0, 101.6, 101.7, 101.4)
    assert p.adaptive.state.stage == 1
    assert p.p.stop_price == p.adaptive.break_even
    observe(p, 1, 101, 101.1, 100)
    assert p.result["return_pct"] >= 0
    assert abs(p.result["return_pct"]) < 1e-8
    out = p.export(START + 2 * MINUTE)
    assert out["activation_075r_time"] and out["activation_1r_time"] is None
    assert out["exit_stage"] == "BREAK_EVEN_075R"
    assert out["giveback_pct"] > 0


def test_1r_2r_tightening_new_high_monotone_and_retracement():
    p = position()
    observe(p, 0, 101.6, 101.7, 101.4)
    observe(p, 1, 102.2, 102.4, 102.1)
    assert p.p.stop_price == D("101.65")
    observe(p, 2, 104.2, 104.4, 104.1)
    assert p.p.stop_price == D("103.9")
    observe(p, 3, 105, 105.1, 104.7)
    assert p.p.stop_price == D("104.6")
    observe(p, 4, 104.9, 105, 104.8)
    assert p.p.stop_price == D("104.6")
    observe(p, 5, 104.8, 104.9, 104)
    assert D(p.result["exit_reference_price"]) == D("104.6")
    out = p.export(START + 6 * MINUTE)
    assert (
        out["activation_075r_time"]
        < out["activation_1r_time"]
        < out["activation_2r_time"]
    )
    assert out["highest_price_since_entry"] == "105.1"
    assert out["highest_r"] == pytest.approx(2.55)
    assert out["exit_stage"] == "TIGHT_PEAK_TRAIL_2R"
    assert out["giveback_r"] == pytest.approx(
        (out["highest_profit_pct"] - out["realized_return_pct"]) * 100.1 / 100 / 2
    )


def test_threshold_uses_canonical_risk_quantity_not_atr_or_original_stop():
    e = entry()
    e.update(quantity="2", notional="200", entry_fee="0.2", risk_amount="8")
    p = position(e)
    assert p.adaptive.thresholds == (D(103), D(104), D(108))
    observe(p, 0, 102, 102.99, 101)
    assert p.p.stop_price == 98


@pytest.mark.parametrize("fee,slip", [(0, 0), (0.1, 0.05), (0.5, 0.3)])
def test_break_even_exact_accounting_does_not_double_charge_entry_slip(fee, slip):
    p = position()
    settings = replace(
        p.settings, paper_fee_pct=D(str(fee)), paper_slippage_pct=D(str(slip))
    )
    p = ShadowPosition(entry(), definition(), settings, "STRICT_FRESH_MARKS")
    p.close(p.adaptive.break_even, START + MINUTE, "TEST", "TEST")
    assert D(p.result["realized_pnl"]) >= 0
    assert D(p.result["realized_pnl"]) < D("0.00000000001")


def test_intrabar_selected_path_only_and_no_future_high_after_stop():
    conservative, optimistic = position(), position()
    for p, c in [(conservative, True), (optimistic, False)]:
        observe(p, 0, 100, 105, 97, conservative=c)
    assert conservative.result["exit_reason"] == "ORIGINAL_STOP"
    assert conservative.adaptive.state.highest == 100
    assert conservative.adaptive.state.times == (None, None, None)
    assert conservative.ambiguous and optimistic.ambiguous
    assert optimistic.result["return_pct"] > conservative.result["return_pct"]
    assert optimistic.adaptive.state.stage == 3


def test_completed_postentry_candle_required():
    p = position()
    with pytest.raises(ValueError):
        p.bar(bar(h=1000), START, True)
    with pytest.raises(ValueError):
        p.bar(bar(START - MINUTE, h=1000), START + MINUTE, True)
    assert p.adaptive.state.stage == 0


def test_stale_no_forced_exit_and_resume_is_not_fill():
    p = position()
    observe(p, 0, 101.6, 101.7, 101.4)
    stop = p.p.stop_price
    p.fresh(False, START + 2 * MINUTE)
    out = p.export(START + 100 * MINUTE)
    assert out["status"] == "OPEN_STALE" and out["exit_time"] is None
    assert out["final_protective_stop"] == str(stop)
    p.fresh(True, START + 101 * MINUTE)
    assert p.result is None and p.p.stop_price == stop
    observe(p, 102, 101, 101.1, 100)
    assert p.export(START + 103 * MINUTE)["status"] == "INCOMPLETE_DATA"


def test_all_policy_order_unchanged_other_policy_outcomes_and_anchor():
    policies = definitions() + [definition()]
    j = job(policies=policies)
    before = copy.deepcopy(j)
    a = ShadowEngine(Stops()).run(j)
    j2 = copy.deepcopy(j)
    j2["config"]["policies"].reverse()
    b = ShadowEngine(Stops()).run(j2)

    def key(r):
        return (r["shadow_entry_id"], r["policy_id"])

    assert sorted(a["outcomes"], key=key) == sorted(b["outcomes"], key=key)
    assert j == before
    old = ShadowEngine(Stops()).run(job())
    assert [r for r in a["outcomes"] if r["policy"] != NAME] == old["outcomes"]


def test_shadow_only_config_ui_and_normal_replay_rejects(request):
    client, source, _ = request.getfixturevalue("web_case")
    run = completed_source(source)
    cfg = dict(anchor_run_id=run, policies=[definition()])
    assert validate(cfg, source)["policies"][0]["exit_policy"] == NAME
    assert NAME in SHADOW_EXITS and NAME not in EXITS.items
    with pytest.raises(ConfigValidationError):
        variant(dict(exit_policy=NAME), 0)
    cfg["policies"][0]["exit_parameters"] = {"activation_r": "0.1"}
    with pytest.raises(ConfigValidationError):
        validate(cfg, source)
    assert NAME.encode() in client.get("/replay/shadow-exit").data
    cfg["policies"][0]["exit_parameters"] = {}
    assert (
        client.post(
            "/api/replay/shadow/config/validate", json=cfg, headers=token(client)
        ).status_code
        == 200
    )


def test_cohorts_rescue_and_incomplete_exclusion():
    e = entry()
    p = position()
    observe(p, 0, 101.6, 101.7, 101.4)
    observe(p, 1, 101, 101.1, 100)
    adaptive = p.export(START + 2 * MINUTE)
    baseline = copy.deepcopy(adaptive)
    baseline.update(
        policy="baseline_v1", policy_id="baseline", return_pct=-2, mfe_pct=3
    )
    result = analysis([e], [adaptive, baseline])
    cohort = result["adaptive_peak_lock_analysis"]["cohorts"]["0.75"]
    assert cohort["reached_count"] == 1
    assert cohort["baseline_reached_then_lost_count"] == 1
    assert cohort["adaptive_avoided_baseline_loss_count"] == 1
    assert cohort["breakeven_close_count"] == 1
    assert (
        cohort["adaptive_saved_baseline_loss_count"] == 0
    )  # quantized break-even is flat, not a profitable rescue
    adaptive["return_pct"] = 1
    result = analysis([e], [adaptive, baseline])
    assert (
        result["adaptive_peak_lock_analysis"]["cohorts"]["0.75"][
            "adaptive_saved_baseline_loss_count"
        ]
        == 1
    )
    adaptive["comparison_eligible"] = False
    assert (
        analysis([e], [adaptive, baseline])["adaptive_peak_lock_analysis"]["cohorts"][
            "0.75"
        ]["adaptive_saved_baseline_loss_count"]
        == 0
    )
    assert "adaptive_peak_lock_analysis" in export(result, True)


def test_gap_fills_at_open_even_after_profit_protection():
    p = position()
    observe(p, 0, 101.6, 101.7, 101.4)
    observe(p, 1, 99, 100, 98)
    assert p.result["exit_reference_price"] == "99"
    assert p.result["return_pct"] < 0
    assert p.export(START + 2 * MINUTE)["exit_stage"] == "BREAK_EVEN_075R"
