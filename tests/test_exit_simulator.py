"""Deterministic counterfactual tests. No production prices or trading API calls."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from conftest import NOW
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from test_paper_analysis import Immediate, analysis_case, trades  # noqa: F401
from test_paper_trade_audit import audit  # noqa: F401

from crypto_bot.storage.database import Database
from crypto_bot.web.analysis_metrics import actual_exit_metrics
from crypto_bot.web.exit_simulator import (
    State,
    aggregate,
    interior,
    ratchet,
    scenarios,
    simulate,
)
from crypto_bot.web.trade_audit import milliseconds

D = Decimal
START = milliseconds(NOW)


@pytest.fixture
def database(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/simulator.db")
    db.initialize()
    yield db
    db.engine.dispose()


def trade(exit_price="97", stop="95", minutes=5, symbol="TESTUSDT"):
    fee = D(exit_price) * D(".001")
    pnl = D(exit_price) - fee - D("100.1")
    return dict(
        position_id=1,
        symbol=symbol,
        status="CLOSED",
        entry_ms=START,
        exit_ms=START + minutes * 60000,
        entry_time=NOW.isoformat() + "Z",
        exit_time=(NOW + timedelta(minutes=minutes)).isoformat() + "Z",
        entry_fill_price="100",
        quantity="1",
        notional="100",
        entry_fee="0.1",
        exit_fill_price=exit_price,
        exit_fee=str(fee),
        exit_slippage="0",
        exit_bid=exit_price,
        exit_reason="STRATEGY",
        realized_pnl=str(pnl),
        return_pct=float(pnl / D("100.1") * 100),
        stop_price=stop,
        paper_config_snapshot={"paper_fee_pct": "0.1", "paper_slippage_pct": "0.05"},
    )


def candle(minute, o=100, h=100, lo=100, c=100):
    return dict(time=START + minute * 60000, open=o, high=h, low=lo, close=c)


def path(first=None, minutes=5):
    return [first or candle(0)] + [candle(i) for i in range(1, minutes)]


def scenario(name, data=None, t=None):
    return simulate(t or trade(), data or path(), "1m", name)[0]


def test_baseline_is_exact_recorded_result():
    t = trade()
    r = scenario("BASELINE", t=t)
    assert r["realized_pnl"] == t["realized_pnl"]
    assert r["fill_price"] == t["exit_fill_price"]
    assert r["return_pct"] == t["return_pct"]
    assert not r["triggered"] and not r["intrabar_ambiguity"]
    assert r["conservative_result"] == r["optimistic_result"]


@pytest.mark.parametrize("name,expected", [("TP_2PCT", "102"), ("TP_0.5R", "102.5")])
def test_fixed_and_r_multiple_tp(name, expected):
    r = scenario(name, path(candle(0, h=104, lo=99, c=103)))
    assert r["triggered"] and D(r["trigger_price"]) == D(expected)
    assert r["trigger_time_utc"].endswith("Z")
    assert r["data_source"] == "BINANCE_PUBLIC_KLINES_APPROXIMATION"
    assert r["data_timeframe"] == "1m"


@pytest.mark.parametrize(
    "buffer,expected", [("0", "100"), ("0.1", "100.1"), ("0.2", "100.2")]
)
def test_break_even_and_buffers(buffer, expected):
    data = [candle(0, h=104, lo=99, c=103)] + [
        candle(i, o=103, h=103, lo=103, c=103) for i in range(1, 5)
    ]
    r = scenario(f"BE_0.5R_BUFFER_{buffer}PCT", data)
    assert r["intrabar_ambiguity"]
    assert D(r["optimistic_result"]["trigger_price"]) == D(expected)
    assert r["conservative_result"]["trigger_reason"] == "ACTUAL_EXIT_FALLBACK"
    assert r["conservative_result"]["return_pct"] < r["optimistic_result"]["return_pct"]


def test_trailing_not_active_before_threshold():
    r = scenario("TRAIL_2PCT_1PCT", path(candle(0, h=101, lo=99, c=100)))
    assert not r["triggered"] and not r["intrabar_ambiguity"]
    assert r["trigger_reason"] == "ACTUAL_EXIT_FALLBACK"


def test_trailing_activation_and_partial_peak_bound():
    r = scenario("TRAIL_2PCT_1PCT", path(candle(0, h=105, lo=99, c=100)))
    assert r["intrabar_ambiguity"]
    assert D(r["conservative_result"]["trigger_price"]) == D("100.98")
    assert D(r["optimistic_result"]["trigger_price"]) == D("103.95")


def test_trailing_partial_peak_touch_above_activation_floor():
    # A smaller rally then LOW is possible before the eventual HIGH: stop can
    # first be touched at 103, not just the full-high trailing level 108.9.
    data = [candle(0, o=104, h=110, lo=103, c=109)] + [
        candle(i, o=109, h=109, lo=109, c=109) for i in range(1, 5)
    ]
    r = scenario("TRAIL_2PCT_1PCT", data, trade(exit_price="109"))
    assert r["intrabar_ambiguity"]
    assert D(r["conservative_result"]["trigger_price"]) == D("103")
    assert r["optimistic_result"]["trigger_reason"] == "ACTUAL_EXIT_FALLBACK"
    assert D(r["optimistic_result"]["trigger_price"]) == D("109")


def test_trailing_ratchet_never_moves_down():
    model = next(s for s in scenarios() if s.name == "TRAIL_2PCT_1PCT")
    state = ratchet(State(D(95), D(100)), D(105), model, D(100), D(5), D(102))
    assert state.stop == D("103.95")
    assert ratchet(state, D(103), model, D(100), D(5), D(102)).stop == state.stop


def test_r_based_trailing():
    r = scenario("TRAIL_1R_0.5R", path(candle(0, h=108, lo=99, c=100)))
    assert r["intrabar_ambiguity"]
    assert D(r["conservative_result"]["trigger_price"]) == D("102.5")
    assert D(r["optimistic_result"]["trigger_price"]) == D("105.5")


def test_profit_lock():
    r = scenario("LOCK_2PCT_1PCT", path(candle(0, h=103, lo=99, c=100)))
    assert r["triggered"] and D(r["trigger_price"]) == D(101)


def test_original_stop_wins_before_later_tp():
    data = path(candle(0, h=100, lo=94, c=96))
    data[1] = candle(1, o=96, h=106, lo=96, c=105)
    r = scenario("TP_2PCT", data)
    assert r["trigger_reason"] == "ORIGINAL_STOP"
    assert D(r["trigger_price"]) == D(95)
    assert not r["intrabar_ambiguity"]


def test_strategy_fallback_wins_when_overlay_never_triggered():
    t = trade(exit_price="101")
    r = scenario("TP_5PCT", t=t)
    assert r["trigger_reason"] == "ACTUAL_EXIT_FALLBACK"
    assert r["realized_pnl"] == t["realized_pnl"]
    assert r["trigger_time_utc"] == t["exit_time"]


def test_fee_and_sell_slippage_exact_accounting():
    r = scenario("TP_2PCT", path(candle(0, h=103, lo=99, c=103)))
    assert D(r["fill_price"]) == D("101.949")
    assert D(r["fee"]) == D("0.101949")
    assert D(r["slippage"]) == D("0.051")
    assert D(r["realized_pnl"]) == D("1.747051")
    assert r["return_pct"] == pytest.approx(1.747051 / 100.1 * 100)


def test_tp_stop_same_candle_conservative_and_optimistic():
    r = scenario("TP_2PCT", path(candle(0, h=103, lo=94, c=100)))
    assert r["intrabar_ambiguity"]
    assert r["conservative_result"]["trigger_reason"] == "ORIGINAL_STOP"
    assert D(r["conservative_result"]["trigger_price"]) == D(95)
    assert D(r["optimistic_result"]["trigger_price"]) == D(102)
    assert r["return_pct"] == r["conservative_result"]["return_pct"]


def test_stop_open_gap_is_not_idealized_stop_fill():
    r = scenario("TP_2PCT", path(candle(0, o=93, h=103, lo=92, c=100)))
    assert D(r["trigger_price"]) == D(93)
    assert not r["intrabar_ambiguity"]


def test_missing_data_and_missing_config_do_not_invent_results():
    t = trade()
    records = simulate(t, [], "1m")
    assert records[0]["usable"] and records[0]["realized_pnl"] == t["realized_pnl"]
    assert all(not r["usable"] and r["return_pct"] is None for r in records[1:])
    t["paper_config_snapshot"] = {}
    r = scenario("TP_2PCT", t=t)
    assert not r["usable"] and "CONFIGURATION" in r["unavailable_reason"]


def test_boundary_and_gap_candles_excluded():
    t = trade()
    t["entry_ms"] += 15000
    t["exit_ms"] -= 15000
    assert [c["time"] for c in interior(t, path(), "1m")] == [
        START + i * 60000 for i in (1, 2, 3)
    ]
    assert interior(t, [candle(1), candle(3)], "1m") is None


@pytest.mark.parametrize(
    "symbol,mfe,loss",
    [("AVAXUSDT", 5.21, 1.77), ("QNTUSDT", 2.72, 4.73), ("NEARUSDT", 2.41, 5.19)],
)
def test_synthetic_profit_giveback_patterns(symbol, mfe, loss):
    t = trade(str(100 - loss), stop="90", symbol=symbol)
    data = path(candle(0, h=100 + mfe, lo=99, c=100 + mfe))
    r = scenario("TP_2PCT", data, t)
    assert r["return_pct"] > 0 and t["return_pct"] < 0
    metrics = actual_exit_metrics(t, {"mfe_pct": mfe, "mae_pct": -1})
    assert metrics["profit_giveback_pct"] == pytest.approx(mfe - t["return_pct"])
    assert metrics["mfe_capture_ratio"] < 0
    assert metrics["mfe_capture_status"] == "NEGATIVE_FINAL_RETURN"


def test_synthetic_tao_low_mfe_does_not_magically_improve_entry():
    t = trade("97.5", stop="95", symbol="TAOUSDT")
    data = path(candle(0, h=100.3, lo=99, c=100))
    for r in simulate(t, data, "1m"):
        assert r["return_pct"] == t["return_pct"]
        assert not r["triggered"]


def test_mae_including_exit_keeps_candle_mae_separate():
    t = trade("97.5")
    hist = {"mfe_pct": 0.3, "mae_pct": -1}
    copy = deepcopy(hist)
    metric = actual_exit_metrics(t, hist)
    assert hist == copy
    assert metric["mae_including_exit_pct"] == pytest.approx(-2.5)
    assert metric["mfe_to_final_return_pct"] == pytest.approx(t["return_pct"] - 0.3)
    t["exit_bid"] = None
    assert actual_exit_metrics(t, hist)["mae_including_exit_pct"] is None


def test_summary_counts_and_no_fake_drawdown():
    t = trade()
    record = dict(
        mfe_pct=3, scenarios=simulate(t, path(candle(0, h=103, lo=94, c=100)), "1m")
    )
    s = aggregate([record], "TP_2PCT")
    row = s["scenarios"][0]
    assert (
        row["trade_count"]
        == row["usable_trade_count"]
        == row["ambiguous_trade_count"]
        == 1
    )
    assert row["loss_rate"] == 100 and row["win_rate"] == 0
    assert D(row["total_pnl"]) < 0
    assert s["max_drawdown_proxy"] is None and not s["statistically_validated"]
    assert s["result_basis"] == "CONSERVATIVE"


@pytest.fixture
def simulator_case(request, database):
    (client, repo, engine, pid, fake), analysis, http = request.getfixturevalue(
        "analysis_case"
    )
    simulator = client.application.extensions["paper_exit_simulator"]
    simulator.executor.shutdown(wait=False, cancel_futures=True)
    simulator.executor = Immediate()
    database.test_quotes = {"BTCUSDT": 97}
    database.test_quote_time = NOW + timedelta(minutes=30)
    engine.manual_close(pid, NOW + timedelta(minutes=30))
    return client, repo, engine, pid, fake, analysis, http, simulator


def simulated(client, query=""):
    url = "/api/paper/analysis/exit-simulator" + query
    for _ in range(3):
        result = client.get(url).json
    return result


def test_read_only_get_api_and_filters(simulator_case):
    client, repo, engine, pid, fake, analysis, http, simulator = simulator_case
    before = repo.status()
    engine.strategy.evaluate = Mock(
        side_effect=AssertionError("No strategy evaluation")
    )
    r = simulated(client, f"?position_id={pid}&scenario=TP_2PCT")
    assert r["total"] == 1 and r["read_only"] and r["counterfactual_only"]
    assert len(r["trades"][0]["scenarios"]) == 1
    assert r["trades"][0]["scenarios"][0]["data_timeframe"] == "1m"
    assert repo.status() == before
    with analysis.database.session() as db:
        with pytest.raises(OperationalError, match="readonly"):
            db.execute(text("UPDATE paper_account SET enabled=0"))
    for method in ("post", "put", "patch", "delete"):
        assert (
            getattr(client, method)("/api/paper/analysis/exit-simulator").status_code
            == 405
        )
        assert (
            getattr(client, method)(
                "/api/paper/analysis/exit-simulator/summary"
            ).status_code
            == 405
        )
    assert all(c.args[0].endswith("/api/v3/klines") for c in http.get.call_args_list)
    http.post.assert_not_called()
    engine.strategy.evaluate.assert_not_called()


@pytest.mark.parametrize(
    "query",
    [
        "?position_id=0",
        "?position_id=abc",
        "?scenario=BUY",
        "?reset=1",
        "?symbol=A&symbol=B",
        "?from=oops",
    ],
)
def test_invalid_simulator_parameters(simulator_case, query):
    assert (
        simulator_case[0].get("/api/paper/analysis/exit-simulator" + query).status_code
        == 400
    )


def test_cache_reuse_all_scenarios_no_extra_candle_fetch(simulator_case):
    client, _, _, pid, _, analysis, http, sim = simulator_case
    r = simulated(client, f"?position_id={pid}")
    assert len(r["trades"][0]["scenarios"]) == 49
    count = http.get.call_count
    simulated(client, f"?position_id={pid}&scenario=TP_2PCT")
    client.get("/api/paper/analysis/trades")
    assert http.get.call_count == count
    assert len(sim.cache) == 1


def test_five_minute_fallback_shared_with_analysis(simulator_case):
    client, _, _, pid, _, analysis, http, sim = simulator_case
    original = http.get.side_effect

    def get(url, params, **kwargs):
        if params["interval"] == "1m":
            raise ValueError("1m temporarily unavailable")
        return original(url, params, **kwargs)

    http.get.side_effect = get
    r = simulated(client, f"?position_id={pid}")["trades"][0]
    assert r["simulation_status"] == "READY"
    assert r["scenarios"][1]["data_timeframe"] == "5m"
    assert r["scenarios"][1]["data_source"] == "BINANCE_PUBLIC_KLINES_APPROXIMATION"


def test_missing_historical_data_keeps_baseline_only(simulator_case):
    client, _, _, pid, _, analysis, http, sim = simulator_case
    http.get.side_effect = ValueError("Offline")
    r = simulated(client, f"?position_id={pid}")["trades"][0]
    assert r["simulation_status"] == "UNAVAILABLE"
    assert r["scenarios"][0]["scenario_name"] == "BASELINE"
    assert r["mfe_pct"] is None


def test_summary_api_closed_only_and_mobile_markup(simulator_case):
    client, repo, engine, pid, fake, analysis, http, sim = simulator_case
    simulated(client)
    url = "/api/paper/analysis/exit-simulator/summary"
    client.get(url)
    result = client.get(url).json
    assert result["sample_size"] == 1 and result["aggregation_status"] == "READY"
    assert result["scenarios"][0]["scenario_name"] == "BASELINE"
    assert result["scenarios"][0]["usable_trade_count"] == 1
    for url, title in [
        ("/paper/analysis", "EXIT STRATEGY SIMULATOR"),
        (f"/paper/trades/{pid}", "WHAT-IF EXIT ANALYSIS"),
    ]:
        html = client.get(url).get_data(as_text=True)
        assert title in html and "analysis-recent" in html
        assert "NOT STATISTICALLY VALIDATED" in html
        assert "BEST STRATEGY" not in html
    assert "simulator-marker" in client.get(f"/paper/trades/{pid}").get_data(
        as_text=True
    )


def test_entry_drift_in_json_and_csv_export(simulator_case):
    client, _, _, pid, _, analysis, http, sim = simulator_case
    t = trades(client)[0]
    assert t["entry_quote_drift_pct"] == 0
    assert t["entry_quote_drift_atr"] == 0
    exported = client.get("/api/paper/analysis/export.json").json["trades"][0]
    assert exported["entry_quote_drift_pct"] == t["entry_quote_drift_pct"]
    assert "mae_including_exit_pct" in exported
    assert "entry_quote_drift_atr" in client.get("/api/paper/analysis/export.csv").text


@pytest.mark.parametrize("atr,expected", [(2, 1), (None, None)])
def test_positive_entry_quote_drift_and_missing_atr(
    simulator_case, database, atr, expected
):
    from crypto_bot.storage.models import Snapshot
    from crypto_bot.storage.paper_models import PaperOrder, PaperPosition, SignalRecord

    client, _, _, pid, *_ = simulator_case
    with database.session() as db:
        p = db.get(PaperPosition, pid)
        signal = db.get(SignalRecord, p.signal_id)
        snap = db.get(Snapshot, signal.snapshot_id)
        if atr is None:
            signal.snapshot_id = None
        else:
            snap.atr = atr
        order = db.get(PaperOrder, p.entry_order_id)
        order.quote = order.quote | {"ask": "102"}
        db.commit()
    t = trades(client)[0]
    assert t["entry_quote_drift_pct"] == 2
    assert t["entry_quote_drift_atr"] == expected


def test_empty_or_open_only_simulator(request):
    client = request.getfixturevalue("analysis_case")[0][0]
    result = simulated(client)
    assert result["total"] == 0 and result["trades"] == []


def test_simulation_does_not_mutate_input_trade_or_path():
    t, data = trade(), path(candle(0, h=106, lo=94, c=100))
    before = deepcopy((t, data))
    simulate(t, data, "1m")
    assert (t, data) == before
