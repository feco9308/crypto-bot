"""Research-only diagnostic regressions; no network or production database."""

import json
import sqlite3
from copy import deepcopy

import pytest

from crypto_bot.data.base import Candle
from crypto_bot.replay.universe import CandleHistory
from research.portfolio_slot_opportunity import (
    HOUR,
    MINUTE,
    Archives,
    DecisionFeatures,
    candidate_future,
    collect,
    effect,
    peak_state,
    read_only,
    remaining_label,
    span,
    stats,
)


def history(bars):
    return CandleHistory(
        [(Candle(t, t + MINUTE - 1, o, h, lo, c, 1), 1) for t, o, h, lo, c in bars]
    )


def test_sqlite_read_only_and_query_only(tmp_path):
    p = tmp_path / "test.db"
    with sqlite3.connect(p) as db:
        db.execute("create table source(value)")
    with read_only(p) as db:
        assert db.execute("pragma query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            db.execute("insert into source values(1)")
        assert db.total_changes == 0


def test_missing_minute_is_unavailable_not_false_or_fabricated_fill():
    result = candidate_future(history([]), 0, 100, 90, HOUR)
    assert set(result["reached_before_stop"].values()) == {None}
    assert result["censor_reason"] == "MISSING_PINNED_1M"
    assert not result["stop_before_1r"]
    assert result["horizons"]["6"]["mfe_r"] is None
    assert "fill_price" not in result


def test_stop_wins_ambiguous_same_minute_high():
    result = candidate_future(history([(0, 100, 160, 89, 110)]), 0, 100, 90, MINUTE)
    assert result["reached_before_stop"]["1"] is False
    assert result["reached_before_stop"]["5"] is False
    assert result["stop_before_1r"]


def test_target_before_later_stop_and_open_gap_before_low():
    result = candidate_future(history([(0, 120, 140, 80, 100)]), 0, 100, 90, MINUTE)
    assert result["reached_before_stop"]["1"] is True
    assert result["reached_before_stop"]["2"] is True
    assert result["reached_before_stop"]["5"] is False
    assert not result["stop_before_1r"]


def test_known_positive_remains_known_after_coverage_gap():
    result = candidate_future(history([(0, 100, 111, 99, 105)]), 0, 100, 90, 3 * MINUTE)
    assert result["reached_before_stop"]["1"] is True
    assert result["reached_before_stop"]["2"] is None
    assert result["censor_reason"] == "MISSING_PINNED_1M"


def test_end_of_run_is_censored_no_forced_last_price_exit():
    result = candidate_future(history([(0, 100, 109, 99, 105)]), 0, 100, 90, MINUTE)
    assert result["censor_reason"] == "RUN_END"
    assert result["reached_before_stop"]["1"] is None
    assert not result["original_stop_observed"]


def test_invalid_stop_distance_is_explicit():
    assert (
        candidate_future(history([]), 0, 100, 101, HOUR)["unavailable"]
        == "INVALID_REFERENCE_STOP_DISTANCE"
    )


def test_forward_windows_raw_excursions_separate_from_stopped_barrier():
    bars = [
        (n * MINUTE, 100, 120 if n else 101, 80 if not n else 95, 100)
        for n in range(360)
    ]
    result = candidate_future(history(bars), 0, 100, 90, 6 * HOUR)
    assert result["stop_before_1r"]
    assert result["horizons"]["6"] == dict(
        available=True, mfe_r=2.0, mae_r=-2.0, unavailable_reason=None
    )
    assert not result["horizons"]["24"]["available"]


def test_current_peak_never_uses_current_or_future_candle_high():
    a = history([(0, 100, 105, 99, 103), (MINUTE, 103, 10000, 1, 500)])
    observed = peak_state(a, 0, MINUTE, 100, 104, 1, 10)
    assert observed["highest_observed_price"] == 105
    assert observed["current_mfe_r"] == 0.5
    assert observed["current_drawdown_from_peak_r"] == 0.1
    assert (
        peak_state(history([(0, 100, 105, 99, 103)]), 0, MINUTE, 100, 104, 1, 10)
        == observed
    )


def test_past_gap_does_not_use_final_trade_peak_as_fallback():
    result = peak_state(history([]), 0, MINUTE, 100, 105, 1, 10)
    assert result["current_mfe_r"] is None
    assert result["highest_observed_price"] is None


def test_incumbent_remaining_r_and_conservative_stop_bar():
    t = dict(
        status="CLOSED",
        exit_time="1970-01-01T00:02:00Z",
        risk_amount="10",
        quantity="1",
        realized_pnl="-11",
        r_multiple=-1.1,
        exit_reason="STOP",
    )
    h = history([(0, 110, 120, 105, 110), (MINUTE, 109, 9999, 89, 90)])
    result = remaining_label(h, t, 0, 110, 9, 115)
    assert result["actual_remaining_realized_r"] == -2
    assert result["future_additional_mfe_r"] == 1
    assert result["remaining_holding_hours"] == 2 / 60
    assert result["label_type"] == "POST_HOC_FUTURE_LABEL"


def test_actual_open_incumbent_has_no_eventual_exit_label():
    result = remaining_label(
        history([]), dict(status="OPEN", exit_reason=None), 0, 100, 0, 100
    )
    assert not result["available"]
    assert "actual_remaining_realized_r" not in result


def test_span_requires_full_closed_coverage():
    h = history([(0, 1, 2, 1, 1), (2 * MINUTE, 1, 2, 1, 1)])
    assert span(h, 0, MINUTE)[2]
    assert not span(h, 0, 3 * MINUTE)[2]


def test_population_is_final_buy_risk_max_positions_only():
    rank = dict(event="ENTRY_CANDIDATE_RANKED", entry_candidate_id="id")
    decision = dict(
        event="RISK_DECISION",
        entry_candidate_id="id",
        action="BUY",
        decision=dict(approved=False, reasons=["max open positions reached"]),
    )
    events = [
        rank,
        decision,
        dict(decision, action="SELL"),
        dict(decision, decision=dict(approved=False, reasons=["CORRELATION_LIMIT"])),
        dict(decision, decision=dict(approved=True, reasons=[])),
    ]
    old = deepcopy(events)
    assert collect(events)[1] == [(rank, decision)]
    assert events == old


def test_stats_and_effect_sizes_order_invariant_and_tie_aware():
    assert stats([1, 2, 3, 4, None]) == dict(
        count=4, median=2.5, q1=1.75, q3=3.25, iqr=1.5
    )
    assert effect([1, 1], [1, 1])["cliffs_delta"] == 0
    assert effect([4, 5], [1, 2])["cliffs_delta"] == 1
    assert effect([4, 5], [1, 2])["iqr_overlap_ratio"] == 0
    assert effect([5, 4], [2, 1]) == effect([4, 5], [1, 2])


def test_closed_hourly_features_independent_of_future_candles():
    now = 600 * HOUR
    rows = [
        (
            Candle(
                n * HOUR,
                (n + 1) * HOUR - 1,
                100 + n * 0.01,
                101 + n * 0.01,
                99 + n * 0.01,
                100 + n * 0.01,
                10000,
            ),
            1e7,
        )
        for n in range(603)
    ]
    changed = deepcopy(rows)
    for n in range(600, 603):
        changed[n] = (
            Candle(n * HOUR, (n + 1) * HOUR - 1, 1, 100000, 1, 100000, 1e8),
            1e15,
        )
    cfg = dict(
        scanner_settings=dict(number_of_markets=1),
        spread_approximation_pct="0.02",
        start="1970-01-26T00:00:00Z",
        end="1970-01-26T03:00:00Z",
    )
    a = DecisionFeatures({"BTCUSDT": CandleHistory(rows)}, cfg)
    b = DecisionFeatures({"BTCUSDT": CandleHistory(changed)}, cfg)
    assert a.get("BTCUSDT", now) == b.get("BTCUSDT", now)
    assert a.get("BTCUSDT", now)["source_timestamp"] < "1970-01-26T00:00:00Z"


def test_archives_never_fall_back_to_unpinned_sqlite_rows(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    with sqlite3.connect(cache / "candles.sqlite") as d:
        d.execute("create table objects(key,status,sha256)")
    source = Archives(cache, {}, 0, HOUR)
    assert len(source.history("BTCUSDT", "1m")) == 0
    assert source.used == {}


def test_source_has_no_run_or_order_creation_runtime_import():
    from research import portfolio_slot_opportunity as module

    assert not hasattr(module, "ReplayEngine")
    assert not hasattr(module, "PortfolioService")
    assert not hasattr(module, "RiskManager")
    assert not hasattr(module, "PaperExecutionService")
    assert json.loads(module.encoded({"future": None})) == {"future": None}


def test_original_stop_alias_excludes_post_exit_high():
    t = dict(
        status="CLOSED",
        exit_time="1970-01-01T00:01:00Z",
        risk_amount="10",
        quantity="1",
        realized_pnl="-10",
        r_multiple=-1.0,
        exit_reason="ORIGINAL_STOP",
    )
    result = remaining_label(history([(0, 100, 9999, 89, 95)]), t, 0, 100, 0, 100)
    assert result["future_additional_mfe_r"] == 0
