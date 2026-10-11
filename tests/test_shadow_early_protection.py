"""Fixed research exit protection; production engine/registries unchanged."""

from copy import deepcopy

import pytest

from crypto_bot.replay.clock import ms
from crypto_bot.replay.registry import EXITS
from crypto_bot.trading.config import PaperSettings
from crypto_bot.trading.domain import D
from research.shadow_early_protection import (
    MINUTE,
    NAMES,
    EarlyProtection,
    category,
    paired,
    parity,
    position,
    simulate,
    summarize,
)

ENTRY = {
    "shadow_entry_id": "anchor:0:1",
    "symbol": "SOLUSDT",
    "entry_time": "2024-01-01T00:00:00Z",
    "entry_fill_price": "100",
    "original_stop_price": "98",
    "quantity": "5",
    "notional": "500",
    "entry_fee": "0.5",
    "entry_slippage": "0.25",
    "risk_amount": "10",
    "atr": 1,
}


def settings():
    return PaperSettings(paper_fee_pct=D("0.1"), paper_slippage_pct=D("0.05"))


@pytest.mark.parametrize("label,activation", [("B", "101"), ("C", "101.5")])
def test_only_original_stop_before_predeclared_activation(label, activation):
    model = EarlyProtection(ENTRY, settings(), NAMES[label])
    state = model.ratchet(
        model.state, D(activation) - D("0.001"), ms(ENTRY["entry_time"]) + MINUTE
    )
    assert state.stage == 0 and state.stop == D("98")
    assert (
        model.ratchet(state, D(activation), ms(ENTRY["entry_time"]) + 2 * MINUTE).stage
        == 1
    )


@pytest.mark.parametrize("label,target", [("B", 0), ("C", 0.25)])
def test_protection_is_net_of_fee_slippage_and_quantization(label, target):
    p = position(ENTRY, label, settings(), "RESEARCH_QUARANTINE_STALE")
    p.adaptive.state = p.adaptive.ratchet(
        p.adaptive.state, p.adaptive.thresholds[0], ms(ENTRY["entry_time"]) + MINUTE
    )
    p.close(p.adaptive.state.stop, ms(ENTRY["entry_time"]) + 2 * MINUTE, "TEST", "TEST")
    assert target <= p.result["r_multiple"] < target + 1e-9
    assert p.adaptive.audit(p.result)["exit_stage"] == (
        "BREAK_EVEN_050R" if label == "B" else "POSITIVE_LOCK_075R_NET025R"
    )


@pytest.mark.parametrize("label", ["B", "C"])
def test_same_one_r_two_r_atr_trail_and_monotonic_stop(label):
    model = EarlyProtection(ENTRY, settings(), NAMES[label])
    stop = model.state.stop
    for price in [D("101.5"), D(102), D("101.8"), D(104), D(105), D(103)]:
        model.state = model.ratchet(
            model.state, price, ms(ENTRY["entry_time"]) + MINUTE
        )
        assert model.state.stop >= stop
        stop = model.state.stop
    assert model.state.stop == D("104.50") and model.state.stage == 3


class Prices:
    def __init__(self, values):
        self.values = values

    def candle(self, symbol, timestamp):
        return self.values.get(timestamp)


def candle(offset=0, high=101.1, low=100, close=100.8):
    time = ms(ENTRY["entry_time"]) + offset * MINUTE
    return {
        "time": time,
        "close_time": time + MINUTE - 1,
        "open": 100,
        "high": high,
        "low": low,
        "close": close,
        "interval": "1m",
    }


def config(minutes=3):
    from crypto_bot.replay.clock import at

    return {
        "end": at(ms(ENTRY["entry_time"]) + minutes * MINUTE).isoformat() + "Z",
        "stale_position_policy": "RESEARCH_QUARANTINE_STALE",
    }


def test_early_break_even_rescues_stop_with_existing_intrabar_ambiguity():
    rows = {
        ms(ENTRY["entry_time"]): candle(),
        ms(ENTRY["entry_time"]) + MINUTE: candle(1, high=101.2, low=97, close=99),
    }
    results = simulate(ENTRY, ["A", "B", "C"], settings(), config(), Prices(rows), True)
    assert results[0]["exit_stage"] == "ORIGINAL_STOP"
    assert (
        results[1]["exit_stage"] == "BREAK_EVEN_050R"
        and results[1]["intrabar_ambiguity"]
    )
    assert category(results[1]) == "FLAT" and category(results[0]) == "LOSS"
    assert results[2]["exit_stage"] == "ORIGINAL_STOP"
    comparison = paired([results[0]], [results[1]])
    assert comparison["rescue"]["count"] == 1
    assert comparison["rescue"]["gained_delta_r"] == pytest.approx(
        comparison["net_delta_r"]
    )


def test_no_future_candle_can_modify_already_closed_exit():
    time = ms(ENTRY["entry_time"])
    rows = {time: candle(high=100.5, low=97, close=99)}
    original = simulate(
        ENTRY, ["A", "B", "C"], settings(), config(), Prices(rows), True
    )
    rows[time + MINUTE] = candle(1, high=100000, low=0.01, close=999)
    assert (
        simulate(ENTRY, ["A", "B", "C"], settings(), config(), Prices(rows), True)
        == original
    )


def test_policy_order_does_not_change_outcomes_or_canonical_entry():
    before = deepcopy(ENTRY)
    time = ms(ENTRY["entry_time"])
    rows = {time: candle(high=103, low=97, close=102)}
    one = simulate(ENTRY, ["A", "B", "C"], settings(), config(), Prices(rows), True)
    two = simulate(ENTRY, ["C", "B", "A"], settings(), config(), Prices(rows), True)
    assert {r["policy_id"]: r for r in one} == {r["policy_id"]: r for r in two}
    assert ENTRY == before


def test_stale_does_not_fabricate_retroactive_or_year_end_exit():
    rows = simulate(ENTRY, ["A", "B", "C"], settings(), config(), Prices({}), True)
    assert all(r["status"] == "OPEN_STALE" and r["exit_time"] is None for r in rows)
    assert all(r["last_valid_mark_price"] == "100" for r in rows)


def test_fresh_data_after_gap_resumes_but_remains_incomplete():
    time = ms(ENTRY["entry_time"])
    rows = simulate(
        ENTRY,
        ["B"],
        settings(),
        config(),
        Prices({time + MINUTE: candle(1, high=100.5, low=97, close=99)}),
        True,
    )
    assert rows[0]["status"] == "INCOMPLETE_DATA" and rows[0]["resumed_after_stale"]
    assert rows[0]["exit_time"] is not None and not rows[0]["comparison_eligible"]


def test_existing_conservative_optimistic_path_selection_is_used():
    rows = {ms(ENTRY["entry_time"]): candle(high=105, low=97, close=104)}
    conservative = simulate(ENTRY, ["B"], settings(), config(), Prices(rows), True)[0]
    optimistic = simulate(ENTRY, ["B"], settings(), config(), Prices(rows), False)[0]
    assert conservative["intrabar_ambiguity"] and optimistic["intrabar_ambiguity"]
    assert conservative["realized_r"] < optimistic["realized_r"]


def test_gap_can_fill_below_net_lock_without_inventing_protected_price():
    p = position(ENTRY, "C", settings(), "RESEARCH_QUARANTINE_STALE")
    p.adaptive.state = p.adaptive.ratchet(
        p.adaptive.state, D("101.5"), ms(ENTRY["entry_time"]) + MINUTE
    )
    bar = candle(1, high=100.1, low=99, close=99.5)
    p.bar(bar, ms(ENTRY["entry_time"]) + 2 * MINUTE, True)
    assert p.result["exit_reference_price"] == "100"
    assert p.result["r_multiple"] < 0.25


def test_net_effect_decomposition_and_winner_damage_reconcile():
    def row(i, stage, r, time):
        return {
            "shadow_entry_id": str(i),
            "comparison_eligible": True,
            "status": "CLOSED",
            "return_pct": r,
            "realized_r": r,
            "exit_stage": stage,
            "exit_time": time,
        }

    a = [
        row(1, "ORIGINAL_STOP", -1, "2024-01-02"),
        row(2, "BREAK_EVEN_075R", 0, "2024-01-02"),
        row(3, "PEAK_TRAIL_1R", 2, "2024-01-02"),
    ]
    b = [
        row(1, "BREAK_EVEN_050R", 0, "2024-01-01"),
        row(2, "LOCK", 0.25, "2024-01-01"),
        row(3, "BREAK_EVEN_050R", 0, "2024-01-01"),
    ]
    result = paired(a, b)
    assert result["net_delta_r"] == -0.75 and result["decomposition_residual_r"] == 0
    assert result["winner_damage"]["control_r_thresholds"]["1.0"]["0.5"]["count"] == 1
    assert (
        result["net_effect_decomposition"]["truncated_future_winners"]["delta_r"] == -2
    )


def test_candidates_never_enter_production_or_normal_replay_registry():
    from crypto_bot.replay.research import RESEARCH_EXITS

    assert NAMES["B"] not in EXITS.items and NAMES["C"] not in EXITS.items
    assert NAMES["B"] not in RESEARCH_EXITS and NAMES["C"] not in RESEARCH_EXITS
    with pytest.raises(ValueError, match="predeclared"):
        EarlyProtection(ENTRY, settings(), "arbitrary_optimizer_policy")


def test_positive_control_be_rounding_dust_is_in_damage_count():
    a = {
        "shadow_entry_id": "1",
        "comparison_eligible": True,
        "status": "CLOSED",
        "return_pct": 1e-12,
        "realized_r": 1e-12,
        "exit_stage": "BREAK_EVEN_075R",
        "exit_time": "2024-01-02",
    }
    b = a | {"return_pct": -0.02, "realized_r": -0.02, "exit_time": "2024-01-01"}
    result = paired([a], [b])
    assert result["winner_damage"]["count"] == 1
    assert result["winner_damage"]["nonflat_winner_damage_count"] == 0
    assert result["net_effect_decomposition"]["other_exit_changes"]["delta_r"] < 0


def test_same_minute_damage_is_in_thresholds_but_not_claimed_as_earlier():
    a = {
        "shadow_entry_id": "1",
        "comparison_eligible": True,
        "status": "CLOSED",
        "return_pct": 1.5,
        "realized_r": 1.5,
        "exit_stage": "PEAK_TRAIL_1R",
        "exit_time": "2024-01-02",
    }
    b = a | {"return_pct": 0.25, "realized_r": 0.25}
    result = paired([a], [b])
    assert result["winner_damage"]["control_r_thresholds"]["1.0"]["0.5"]["count"] == 1
    assert result["winner_damage"]["premature_exit_count"] == 0
    assert result["winner_damage"]["same_minute_damage_count"] == 1
    assert result["winner_damage"]["same_minute_damage_lost_r"] == 1.25


def test_control_parity_rejects_duplicate_outcomes_and_changed_fill():
    row = simulate(
        ENTRY,
        ["A"],
        settings(),
        config(),
        Prices({ms(ENTRY["entry_time"]): candle(high=100.5, low=97, close=99)}),
        True,
    )[0]
    rows, trades = [], []
    for pid in range(1, 1507):
        rows.append(row | {"shadow_entry_id": f"anchor:0:{pid}"})
        trades.append(row | {"position_id": pid})
    assert parity(rows, trades)["passed"]
    rows[-1] = rows[-2]
    assert not parity(rows, trades)["passed"]
    rows[-1] = row | {"shadow_entry_id": "anchor:0:1506", "exit_price": "999"}
    proof = parity(rows, trades)
    assert not proof["passed"] and any(
        m["field"] == "exit_price" for m in proof["mismatches"]
    )


def test_mfe_cohorts_use_canonical_control_and_monthly_deltas_reconcile():
    time = ms(ENTRY["entry_time"])
    rows = simulate(
        ENTRY,
        ["A", "B", "C"],
        settings(),
        config(),
        Prices({time: candle(high=100.5, low=97, close=99)}),
        True,
    )
    for row in rows:
        row["highest_r"] = 99  # Must not enter retrospective cohort selection.
    result = summarize(rows, [{"position_id": 1, "highest_r": 0.6}])
    cohorts = {c["control_highest_r_minimum"]: c["n"] for c in result["mfe_cohorts"]}
    assert cohorts[0.5] == 1 and cohorts[0.7] == 0
    for label in ("B", "C"):
        months = [
            v["comparisons"][label]["net_delta_r"]
            for k, v in result["time_robustness"].items()
            if k.startswith("2024-")
        ]
        assert sum(months) == pytest.approx(result["comparisons"][label]["net_delta_r"])
