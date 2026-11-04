"""Frozen strategy and bookkeeping regressions using synthetic daily candles."""

import copy
import json
import zipfile
from dataclasses import replace
from decimal import Decimal as D
from decimal import localcontext
from pathlib import Path

import pytest

from research import orthogonal_strategy_class as b


def fixture(days=6):
    t = b.source.stamp("2024-01-01")
    data = {s: {} for s in b.source.SYMBOLS}
    for s in data:
        for k in range(-40, days):
            day = t + k * b.DAY
            data[s][day] = b.Candle(day, day + b.DAY - 1, D(100), D(101), D(99), D(100))
    return t, data


def manual(t, states):
    result = {}
    previous = {s: False for s in b.source.SYMBOLS}
    for i, flags in enumerate(states):
        day = t + i * b.DAY
        result[day] = {}
        for s, flag in zip(b.source.SYMBOLS, flags):
            result[day][s] = dict(
                signal_id=f"{s}:{day}",
                active_after=flag,
                source_close_ms=day - 1,
                known_at_ms=day,
                source_close=b.source.iso(day - 1),
                known_at=b.source.iso(day),
                action="BUY"
                if flag and not previous[s]
                else "SELL"
                if previous[s] and not flag
                else "HOLD",
            )
            previous[s] = flag
    return result


def fills(result):
    return [r for r in result["events"] if r["event"] == "EXECUTED_NEXT_DAILY_OPEN"]


def test_exact_frozen_scope_and_no_grid():
    assert b.STRATEGIES == {"S1": "BREAKOUT", "S2": "MEAN_REVERSION"}
    assert b.PORTFOLIOS == {
        "BTC_ONLY": ("BTCUSDT",),
        "ETH_ONLY": ("ETHUSDT",),
        "EQUAL_WEIGHT": ("BTCUSDT", "ETHUSDT"),
    }
    assert b.PERIODS == {
        2024: ("2024-01-01", "2025-01-01"),
        2025: ("2025-02-01", "2026-01-01"),
    }
    assert b.MIN_EPISODES == 5


@pytest.mark.parametrize("strategy", ["S1", "S2"])
def test_completed_candle_only(strategy):
    t, data = fixture()
    day = t - b.DAY
    data["BTCUSDT"][day] = replace(data["BTCUSDT"][day], end=t)
    with pytest.raises(ValueError, match="UNCOMPLETED"):
        b.observation(strategy, data["BTCUSDT"], day, False, t)


def test_missing_window_fails_without_fallback():
    t, data = fixture()
    del data["BTCUSDT"][t - 20 * b.DAY]
    with pytest.raises(ValueError):
        b.observation("S1", data["BTCUSDT"], t - b.DAY, False, t)


def donchian():
    t, data = fixture()
    hist = data["BTCUSDT"]
    day = t - b.DAY
    for k in range(1, 22):
        d = day - k * b.DAY
        hist[d] = replace(hist[d], high=D(100), low=D(80), close=D(90))
    return t, hist, day


def test_donchian_prior20_high_and_decision_high_excluded():
    t, h, day = donchian()
    h[day - 20 * b.DAY] = replace(h[day - 20 * b.DAY], high=D(150))
    h[day - 21 * b.DAY] = replace(h[day - 21 * b.DAY], high=D(999))
    h[day] = replace(h[day], close=D(151), high=D(1000))
    result = b.observation("S1", h, day, False, t)
    assert result["action"] == "BUY" and D(result["prior20_high"]) == 150
    h[day] = replace(h[day], close=D(150))
    assert b.observation("S1", h, day, False, t)["action"] == "HOLD"


def test_donchian_prior10_low_and_decision_low_excluded():
    t, h, day = donchian()
    h[day - 10 * b.DAY] = replace(h[day - 10 * b.DAY], low=D(60))
    h[day - 11 * b.DAY] = replace(h[day - 11 * b.DAY], low=D(1))
    h[day] = replace(h[day], close=D(59), low=D(".1"))
    row = b.observation("S1", h, day, True, t)
    assert row["action"] == "SELL" and D(row["prior10_low"]) == 60
    h[day] = replace(h[day], close=D(60))
    assert b.observation("S1", h, day, True, t)["action"] == "HOLD"


def bollinger_equality():
    t, data = fixture()
    h = data["BTCUSDT"]
    day = t - b.DAY
    deviations = [4, 4, -4, 2, -2, 1, -1, 1, -1] + [0] * 10
    for k, dev in enumerate(deviations, 1):
        d = day - k * b.DAY
        h[d] = replace(h[d], close=D(100 + dev))
    h[day] = replace(h[day], close=D(96))
    return t, h, day


def test_bollinger_exact_mean_sample_std_and_strict_entry():
    t, h, day = bollinger_equality()
    row = b.observation("S2", h, day, False, t)
    assert D(row["sma20"]) == 100
    assert D(row["sample_std20"]) == 2  # sum squared deviations 76 / (20-1)
    assert D(row["lower_band"]) == 96
    assert row["action"] == "HOLD" and row["entry_condition"] is False
    h[day] = replace(h[day], close=D("95.999"))
    assert b.observation("S2", h, day, False, t)["action"] == "BUY"
    # Twenty-first close is not included in the frozen 20-bar window.
    h[day - 20 * b.DAY] = replace(h[day - 20 * b.DAY], close=D("1e9"))
    assert b.observation("S2", h, day, False, t)["action"] == "BUY"


def test_bollinger_exit_greater_equal_mean_and_no_extra_exit():
    t, data = fixture()
    day = t - b.DAY
    h = data["BTCUSDT"]
    assert b.observation("S2", h, day, True, t)["action"] == "SELL"
    h[day] = replace(h[day], close=D(99))
    assert b.observation("S2", h, day, True, t)["action"] == "HOLD"


def test_next_open_fills_never_decision_close():
    t, data = fixture()
    sig = manual(t, [(True, False), (False, False)])
    data["BTCUSDT"][t] = replace(data["BTCUSDT"][t], open=D(123), close=D(80))
    data["BTCUSDT"][t + b.DAY] = replace(data["BTCUSDT"][t + b.DAY], open=D(77))
    result = b.simulate(data, sig, "S1", "BTC_ONLY", t, t + 2 * b.DAY)
    assert [(r["side"], D(r["fill_price"])) for r in fills(result)] == [
        ("BUY", D(123)),
        ("SELL", D(77)),
    ]
    assert all(
        b.source.stamp(r["source_close"]) < b.source.stamp(r["timestamp"])
        for r in fills(result)
    )


@pytest.mark.parametrize("field", ["source_close_ms", "known_at_ms"])
def test_same_bar_or_future_signal_cannot_execute(field):
    t, data = fixture()
    sig = manual(t, [(True, False)])
    sig[t]["BTCUSDT"][field] = t + b.DAY
    with pytest.raises(ValueError, match="SAME_BAR"):
        b.simulate(data, sig, "S1", "BTC_ONLY", t, t + b.DAY)


def test_fee_slippage_each_executed_side_no_debt():
    t, data = fixture()
    sig = manual(t, [(True, False), (False, False)])
    r = b.simulate(
        data, sig, "S1", "BTC_ONLY", t, t + 2 * b.DAY, b.source.FEE, b.source.SLIP
    )
    buy, sell = fills(r)
    assert D(buy["fill_price"]) == D("100.05") and D(sell["fill_price"]) == D("99.95")
    for fill in (buy, sell):
        with localcontext() as ctx:
            ctx.prec = 45
            assert D(fill["fee"]) == D(fill["notional"]) * D(".001")
        assert D(fill["cash_after_allocation"]) >= 0
        assert float(fill["slippage"]) == pytest.approx(
            float(D(fill["quantity"]) * D(".05"))
        )
    expected = 1000 * 0.9995 * 0.999 / (1.0005 * 1.001)
    assert r["metrics"]["ending_equity"] == pytest.approx(expected)
    assert r["metrics"]["completed_position_episodes"] == 1
    assert r["episodes"][0]["realized_pnl"] == pytest.approx(expected - 1000)


def test_exact_equal_weight_and_one_active_gets_full_budget():
    t, data = fixture()
    sig = manual(t, [(True, True)])
    r = b.simulate(
        data, sig, "S1", "EQUAL_WEIGHT", t, t + b.DAY, b.source.FEE, b.source.SLIP
    )
    trade = fills(r)
    assert len(trade) == 2
    assert abs(D(trade[0]["quantity"]) * 100 - D(trade[1]["quantity"]) * 100) < D(
        "1e-25"
    )
    assert all(D(x["cash_after_allocation"]) >= 0 for x in trade)
    weights = b.weights_for(manual(t, [(False, True)])[t], b.source.SYMBOLS)
    assert weights == {"ETHUSDT": D(1)}


def test_rebalance_only_active_set_changes_and_partial_sale_is_same_episode():
    t, data = fixture()
    sig = manual(
        t, [(True, False), (True, True), (True, True), (False, True), (False, False)]
    )
    data["BTCUSDT"][t + 2 * b.DAY] = replace(
        data["BTCUSDT"][t + 2 * b.DAY], open=D(200), close=D(200)
    )
    r = b.simulate(
        data, sig, "S1", "EQUAL_WEIGHT", t, t + 5 * b.DAY, b.source.FEE, b.source.SLIP
    )
    assert not any(x["timestamp"] == b.source.iso(t + 2 * b.DAY) for x in fills(r))
    assert r["metrics"]["transitions"] == 4
    btc = [ep for ep in r["episodes"] if ep["symbol"] == "BTCUSDT"]
    assert len(btc) == 1 and btc[0]["holding_days"] == 3
    assert btc[0]["sell_fills"] == 2
    assert len(r["episodes"]) == 2 and all(
        ep["status"] == "CLOSED" for ep in r["episodes"]
    )


def test_terminal_cost_is_estimate_not_a_sell_or_completed_episode():
    t, data = fixture()
    sig = manual(t, [(True, False)])
    r = b.simulate(
        data, sig, "S2", "BTC_ONLY", t, t + b.DAY, b.source.FEE, b.source.SLIP
    )
    assert [x["side"] for x in fills(r)] == ["BUY"]
    assert r["metrics"]["completed_position_episodes"] == 0
    assert r["metrics"]["win_rate"] is None
    assert r["metrics"]["average_holding_days"] is None
    assert (
        r["episodes"][0]["status"] == "OPEN_AT_END"
        and r["episodes"][0]["realized_pnl"] is None
    )
    terminal = r["events"][-1]
    assert terminal["event"] == "TERMINAL_COST_ESTIMATE_NOT_A_FILL"
    assert D(terminal["fee"]) > 0 and D(terminal["slippage"]) > 0
    assert r["metrics"]["ending_equity"] == pytest.approx(
        1000 * 0.9995 * 0.999 / (1.0005 * 1.001)
    )


def test_flat_cohort_has_no_fake_episode_or_cost():
    t, data = fixture()
    sig = manual(t, [(False, False)] * 3)
    r = b.simulate(
        data, sig, "S2", "EQUAL_WEIGHT", t, t + 3 * b.DAY, b.source.FEE, b.source.SLIP
    )
    assert r["episodes"] == [] and fills(r) == []
    assert r["metrics"]["ending_equity"] == 1000 and r["metrics"]["fee"] == 0


def test_current_future_candle_cannot_change_earlier_signal_or_fill():
    t, data = fixture()
    day = t - b.DAY
    data["BTCUSDT"][day] = replace(data["BTCUSDT"][day], close=D(200), high=D(200))
    sig = b.schedule(data, "S1", t, t + b.DAY)
    original = b.simulate(data, sig, "S1", "BTC_ONLY", t, t + b.DAY)
    data["BTCUSDT"][t] = replace(
        data["BTCUSDT"][t], high=D("1e8"), low=D(1), close=D(1000)
    )
    assert b.schedule(data, "S1", t, t + b.DAY) == sig
    changed = b.simulate(data, sig, "S1", "BTC_ONLY", t, t + b.DAY)
    assert fills(original) == fills(changed)


def test_signals_and_inputs_independent_of_portfolio_cost_and_order():
    t, data = fixture()
    before = copy.deepcopy(data)
    sig = b.schedule(data, "S1", t, t + 4 * b.DAY)
    original = copy.deepcopy(sig)
    forward = {
        k: b.simulate(data, sig, "S1", k, t, t + 4 * b.DAY) for k in b.PORTFOLIOS
    }
    reverse = {
        k: b.simulate(data, sig, "S1", k, t, t + 4 * b.DAY)
        for k in reversed(b.PORTFOLIOS)
    }
    assert forward == reverse and sig == original and data == before
    for k in b.PORTFOLIOS:
        b.simulate(data, sig, "S1", k, t, t + 4 * b.DAY, b.source.FEE, b.source.SLIP)
    assert sig == original


@pytest.mark.parametrize(
    "start,end", [("2023-12-31", "2024-01-02"), ("2025-12-31", "2026-01-02")]
)
def test_2023_performance_and_2026_data_forbidden(start, end):
    with pytest.raises(ValueError, match="FROZEN_SCOPE"):
        b.schedule({}, "S1", b.source.stamp(start), b.source.stamp(end))
    with pytest.raises(ValueError, match="FROZEN_SCOPE"):
        b.simulate({}, {}, "S1", "BTC_ONLY", b.source.stamp(start), b.source.stamp(end))


def test_warmup_can_supply_first_signal_but_has_no_trade_pnl():
    t, data = fixture()
    data["BTCUSDT"][t - b.DAY] = replace(
        data["BTCUSDT"][t - b.DAY], close=D(110), high=D(110)
    )
    sig = b.schedule(data, "S1", t, t + 2 * b.DAY)
    r = b.simulate(data, sig, "S1", "BTC_ONLY", t, t + 2 * b.DAY)
    assert sig[t]["BTCUSDT"]["decision_day"].startswith("2023-12-31")
    assert all(b.source.stamp(x["timestamp"]) >= t for x in r["events"])
    assert all(b.source.stamp(x["day"]) >= t for x in r["daily"])


def test_leave_month_unchanged_returns_deterministic():
    returns = [
        dict(month="2024-01", **{"return": 0.1}),
        dict(month="2024-02", **{"return": -0.05}),
        dict(month="2024-03", **{"return": 0.02}),
    ]
    result = b.source.leave_month(returns)
    assert result == b.source.leave_month(returns)
    assert result[0]["ending_equity"] == pytest.approx(1000 * 0.95 * 1.02)


def screen_rows():
    return [
        dict(
            year=year,
            strategy=s,
            portfolio=p,
            gross_return_pct=2,
            net_return_pct=1,
            completed_position_episodes=5,
            positive_leave_month_runs=12 if year == 2024 else 11,
            leave_month_count=12 if year == 2024 else 11,
        )
        for year in (2024, 2025)
        for s in b.STRATEGIES
        for p in b.PORTFOLIOS
    ]


def test_multiple_classes_no_posthoc_selection():
    r = b.screen(screen_rows())
    assert r["final_classification"] == "ORTHOGONAL_CLASS_SIGNAL_FOUND"
    assert r["exploration_candidate"] == "NONE"
    assert r["clean_validation_eligible"] == "YES"
    assert r["candidate_note"] == "MULTIPLE_CLASSES_REQUIRE_PREREGISTRATION"


@pytest.mark.parametrize("episodes", [4, 5])
def test_minimum_completed_episode_guard(episodes):
    rows = screen_rows()
    for r in rows:
        if r["strategy"] == "S1":
            r["net_return_pct"] = -1
        else:
            r["completed_position_episodes"] = episodes
    result = b.screen(rows)
    assert result["clean_validation_eligible"] == ("NO" if episodes == 4 else "YES")
    if episodes == 4:
        assert any(
            "INSUFFICIENT_EPISODES" in reason
            for row in result["portfolio_screen"]
            for reason in row["reasons"]
        )
    else:
        assert result["exploration_candidate"] == "MEAN_REVERSION"


def test_same_portfolio_must_pass_both_years_no_cross_portfolio_cherry_pick():
    rows = screen_rows()
    for r in rows:
        if (
            r["strategy"] == "S1"
            or (r["portfolio"] == "BTC_ONLY" and r["year"] == 2025)
            or (r["portfolio"] != "BTC_ONLY" and r["year"] == 2024)
        ):
            r["net_return_pct"] = -1
    assert b.screen(rows)["clean_validation_eligible"] == "NO"


def test_one_month_dominance_fails_screen():
    rows = screen_rows()
    for r in rows:
        if r["strategy"] == "S1":
            r["net_return_pct"] = -1
        elif r["year"] == 2025:
            r["positive_leave_month_runs"] = 10
    assert b.screen(rows)["clean_validation_eligible"] == "NO"


def test_all_negative_class_not_found():
    rows = screen_rows()
    for r in rows:
        r["net_return_pct"] = -1
    r = b.screen(rows)
    assert r["final_classification"] == "ORTHOGONAL_CLASS_NOT_FOUND"
    assert (
        r["exploration_candidate"] == "NONE" and r["clean_validation_eligible"] == "NO"
    )


def test_episode_ledger_cashflows_partial_buys_sells_and_return():
    ledger = b.EpisodeLedger("test")
    t = b.source.stamp("2024-01-01")
    ledger.fill("BTCUSDT", "BUY", D(2), D(100), D(1), D(".5"), t)
    ledger.fill("BTCUSDT", "SELL", D(1), D(110), D(1), D(".5"), t + b.DAY)
    ledger.fill("BTCUSDT", "BUY", D(1), D(90), D(1), D(".5"), t + 2 * b.DAY)
    ledger.fill("BTCUSDT", "SELL", D(2), D(120), D(1), D(".5"), t + 3 * b.DAY)
    ep = ledger.closed[0]
    assert ep["realized_pnl"] == 56  # net proceeds 348 minus cash entries 292
    assert ep["return_pct"] == pytest.approx(56 / 292 * 100)
    assert ep["holding_days"] == 3 and ep["buy_fills"] == 2 and ep["sell_fills"] == 2
    assert ledger.open == {}


def test_deterministic_zip(tmp_path):
    (tmp_path / "summary.json").write_bytes(b.source.encoded({"eligible": "NO"}))
    p = b.package(tmp_path)
    blob = p.read_bytes()
    assert b.package(tmp_path).read_bytes() == blob
    with zipfile.ZipFile(p) as z:
        assert z.testzip() is None
        manifest = json.loads(z.read("manifest.json"))
        assert (
            b.source.sha(z.read("summary.json"))
            == manifest["files"]["summary.json"]["sha256"]
        )


def test_no_production_strategy_db_or_old_benchmark_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("OLD_RUN_OR_NETWORK_FORBIDDEN")

    monkeypatch.setattr(b.source, "run", forbidden)
    monkeypatch.setattr(b.source, "_run", forbidden)
    monkeypatch.setattr(b.source.requests.sessions.Session, "request", forbidden)
    t, data = fixture()
    sig = b.schedule(data, "S2", t, t + 2 * b.DAY)
    b.simulate(data, sig, "S2", "BTC_ONLY", t, t + 2 * b.DAY)
    text = Path(b.__file__).read_text()
    assert "from crypto_bot" not in text and "import crypto_bot" not in text
    assert "import sqlite" not in text and "import sqlalchemy" not in text
