"""Fixed-price, network-free tests of the frozen research harness."""

import copy
import csv
import io
import json
import zipfile
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest

from research import low_turnover_benchmark as b


def fixture(days=4):
    start = b.stamp("2024-01-01")
    data = {s: {} for s in b.SYMBOLS}
    for s in b.SYMBOLS:
        for n in range(-200, days):
            t = start + n * b.DAY
            close = D("101") if n == -1 else D("100")
            data[s][t] = b.Bar(t, t + b.DAY - 1, D("100"), close)
    return start, data


def orders(result):
    return [r for r in result["events"] if r["event"] == "EXECUTED_NEXT_OPEN"]


def test_frozen_universe_periods_and_no_search():
    assert b.BENCHMARKS == ("B0", "B1", "B2", "T1", "T2")
    assert b.SMA_DAYS == 200
    assert b.SYMBOLS == ("BTCUSDT", "ETHUSDT")
    assert b.PERIODS == {
        2024: ("2024-01-01", "2025-01-01"),
        2025: ("2025-02-01", "2026-01-01"),
    }


def test_sma200_exact_inclusive_last_200_not_201():
    start, data = fixture()
    assert b.active(data, "BTCUSDT", start) is True
    t = start - 201 * b.DAY
    data["BTCUSDT"][t] = b.Bar(t, t + b.DAY - 1, D("1e9"), D("1e9"))
    assert b.active(data, "BTCUSDT", start) is True
    t = start - b.DAY
    data["BTCUSDT"][t] = replace(data["BTCUSDT"][t], close=D("100"))
    assert b.active(data, "BTCUSDT", start) is False


def test_daily_must_be_completed_before_signal():
    start, data = fixture()
    t = start - b.DAY
    data["BTCUSDT"][t] = replace(data["BTCUSDT"][t], end=start)
    assert b.active(data, "BTCUSDT", start) is None
    assert orders(b.run(data, "T1", start, start + b.DAY)) == []


def test_missing_history_not_a_false_signal_or_fill():
    start, data = fixture()
    del data["BTCUSDT"][start - 100 * b.DAY]
    assert b.target("T1", data, start) is None
    assert orders(b.run(data, "T1", start, start + b.DAY)) == []


def test_next_open_execution_never_decision_close():
    start, data = fixture()
    data["BTCUSDT"][start] = replace(
        data["BTCUSDT"][start], open=D("123"), close=D("80")
    )
    nxt = start + b.DAY
    data["BTCUSDT"][nxt] = replace(data["BTCUSDT"][nxt], open=D("77"))
    trade = orders(b.run(data, "T1", start, start + 2 * b.DAY))
    assert [(x["side"], x["fill_price"]) for x in trade] == [
        ("BUY", 123.0),
        ("SELL", 77.0),
    ]
    assert trade[0]["decision_data_close"] == b.iso(start - 1)
    assert trade[1]["decision_data_close"] == b.iso(nxt - 1)
    assert all(x["timestamp"] > x["decision_data_close"] for x in trade)


def test_current_and_future_closes_cannot_change_current_order():
    start, data = fixture()
    before = orders(b.run(data, "T1", start, start + b.DAY))
    data["BTCUSDT"][start] = replace(data["BTCUSDT"][start], close=D("1000000"))
    data["BTCUSDT"][start + b.DAY] = replace(
        data["BTCUSDT"][start + b.DAY], open=D("9"), close=D("7")
    )
    assert orders(b.run(data, "T1", start, start + b.DAY)) == before


def test_no_next_candle_means_no_exit_fill():
    start, data = fixture(days=1)
    data["BTCUSDT"][start] = replace(data["BTCUSDT"][start], close=D("80"))
    r = b.run(data, "T1", start, start + b.DAY, b.FEE, b.SLIP)
    assert [x["side"] for x in orders(r)] == ["BUY"]
    assert r["metrics"]["exits"] == 0
    assert r["metrics"]["terminal_open_assets"] == 1
    assert any(
        e["event"] == "TERMINAL_LIQUIDATION_COST_ESTIMATE_NOT_A_FILL"
        for e in r["events"]
    )


def test_warmup_and_outside_cohort_never_performance():
    start, data = fixture()
    for bench in b.BENCHMARKS:
        r = b.run(data, bench, start, start + 4 * b.DAY)
        assert len(r["daily"]) == 4
        assert all(e["timestamp"] >= b.iso(start) for e in r["events"])
        assert all(e["timestamp"] < b.iso(start + 4 * b.DAY) for e in r["events"])


@pytest.mark.parametrize("month", ["2023-05", "2026-01", "2022-12"])
def test_forbidden_archive_dates(month):
    with pytest.raises(ValueError):
        b.archive_url("BTCUSDT", month)


def test_exact_official_archive_only_no_futures_or_orders(tmp_path):
    with pytest.raises(ValueError, match="ONLY_OFFICIAL"):
        b.fetch(tmp_path, "https://fapi.binance.com/fapi/v1/order", True, None)
    with pytest.raises(ValueError):
        b.archive_url("SOLUSDT", "2024-01")


def test_fee_and_slippage_per_side_and_no_borrowing():
    cash, qty = D(1000), {s: D(0) for s in b.SYMBOLS}
    prices = {s: D(100) for s in b.SYMBOLS}
    cash, buy = b.reallocate(cash, qty, prices, {"BTCUSDT": D(1)}, b.FEE, b.SLIP)
    entry_q = qty["BTCUSDT"]
    assert buy[0]["fill_price"] == 100.05
    assert buy[0]["fee"] == pytest.approx(buy[0]["notional"] * 0.001)
    assert buy[0]["slippage"] == pytest.approx(float(entry_q * D(".05")))
    cash, sell = b.reallocate(cash, qty, prices, {}, b.FEE, b.SLIP)
    assert sell[0]["fill_price"] == 99.95
    assert sell[0]["fee"] == pytest.approx(sell[0]["notional"] * 0.001)
    expected = 1000 * 0.9995 * 0.999 / (1.0005 * 1.001)
    assert float(cash) == pytest.approx(expected)
    assert all(q == 0 for q in qty.values())


@pytest.mark.parametrize("bench,assets", [("B0", 1), ("B1", 1), ("B2", 2)])
def test_buy_hold_entry_and_estimated_exit_costs(bench, assets):
    start, data = fixture()
    r = b.run(data, bench, start, start + 4 * b.DAY, b.FEE, b.SLIP)
    m = r["metrics"]
    assert m["entries"] == assets and m["exits"] == 0
    assert m["transitions"] == 1
    expected = 1000 * 0.9995 * 0.999 / (1.0005 * 1.001)
    assert m["ending_equity"] == pytest.approx(expected)
    assert m["total_fees"] > 1.99
    assert m["total_modeled_slippage"] > 0.99


def test_t2_active_set_allocation_and_only_state_change_rebalance():
    start, data = fixture()
    assert b.target("T2", data, start) == {s: D(".5") for s in b.SYMBOLS}
    r = b.run(data, "B2", start, start + 4 * b.DAY)
    assert r["metrics"]["entries"] == 2 and r["metrics"]["exits"] == 0
    # Make all completed closes rise, so TRUE stays TRUE while prices drift.
    for s in b.SYMBOLS:
        for i in range(4):
            t = start + i * b.DAY
            data[s][t] = replace(data[s][t], close=D(110 + i), open=D(110 + i))
    data["BTCUSDT"][start + b.DAY] = replace(
        data["BTCUSDT"][start + b.DAY], open=D(200)
    )
    r = b.run(data, "T2", start, start + 4 * b.DAY)
    assert len(orders(r)) == 2
    # Only BTC active -> allocate its entire cash budget, not 50% cash.
    for k in range(1, 201):
        t = start - k * b.DAY
        data["ETHUSDT"][t] = replace(data["ETHUSDT"][t], close=D(100))
    assert b.target("T2", data, start) == {"BTCUSDT": D(1)}


def test_post_cost_target_weights_minimal_trades_deterministic():
    q = {"BTCUSDT": D(3), "ETHUSDT": D(0)}
    prices = {"BTCUSDT": D(100), "ETHUSDT": D(10)}
    weights = {s: D(".5") for s in b.SYMBOLS}
    before = copy.deepcopy(q)
    cash, events = b.reallocate(D(700), q, prices, weights, b.FEE, b.SLIP)
    assert all(e["side"] == "BUY" for e in events)  # no sell-all / rebuy tax
    assert abs(q["BTCUSDT"] * 100 - q["ETHUSDT"] * 10) < D("1e-20")
    assert cash >= 0
    other = before.copy()
    assert b.reallocate(D(700), other, prices, weights, b.FEE, b.SLIP) == (cash, events)
    assert other == q


def test_shared_source_is_immutable_and_order_independent():
    start, data = fixture()
    before = copy.deepcopy(data)
    a = {k: b.run(data, k, start, start + 4 * b.DAY) for k in b.BENCHMARKS}
    z = {k: b.run(data, k, start, start + 4 * b.DAY) for k in reversed(b.BENCHMARKS)}
    assert a == z and data == before


def test_leave_month_is_deterministic_return_deletion():
    daily = [
        {"month": "2024-01", "return": 0.1},
        {"month": "2024-02", "return": -0.05},
        {"month": "2024-03", "return": 0.02},
    ]
    r = b.leave_month(daily)
    assert r == b.leave_month(daily)
    assert r[0]["ending_equity"] == pytest.approx(1000 * 0.95 * 1.02)
    assert r[1]["ending_equity"] == pytest.approx(1000 * 1.1 * 1.02)


def test_deterministic_package_and_manifest(tmp_path):
    (tmp_path / "summary.json").write_bytes(b.encoded({"value": 1}))
    z = b.package(tmp_path)
    first = z.read_bytes()
    assert b.package(tmp_path).read_bytes() == first
    with zipfile.ZipFile(z) as f:
        m = json.loads(f.read("manifest.json"))
        assert b.sha(f.read("summary.json")) == m["files"]["summary.json"]["sha256"]
        assert f.testzip() is None


def sample_zip(month, duplicate=False):
    start = b.stamp(month + "-01")
    unit = 1000 if month >= "2025-01" else 1
    end = b.stamp("2025-02-01" if month == "2025-01" else "2024-02-01")
    stream = io.StringIO()
    writer = csv.writer(stream)
    for t in range(start, end, b.DAY):
        writer.writerow(
            [
                t * unit,
                "100",
                "110",
                "90",
                "101",
                "5",
                (t + b.DAY) * unit - 1,
                "500",
                10,
                "2",
                "200",
                0,
            ]
        )
    if duplicate:
        writer.writerow(
            [
                start * unit,
                "100",
                "110",
                "90",
                "101",
                "5",
                (start + b.DAY) * unit - 1,
                "500",
                10,
                "2",
                "200",
                0,
            ]
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as f:
        f.writestr("data.csv", stream.getvalue())
    return buf.getvalue()


@pytest.mark.parametrize("month", ["2024-01", "2025-01"])
def test_official_schema_ms_microseconds_and_complete_daily_bars(month):
    rows = b.parse_zip(sample_zip(month), month)
    assert len(rows) == 31
    assert rows[0].start == b.stamp(month + "-01")
    assert rows[0].end == rows[0].start + b.DAY - 1
    with pytest.raises(ValueError):
        b.parse_zip(sample_zip(month, True), month)


def test_offline_cache_integrity(tmp_path):
    url = b.archive_url("BTCUSDT", "2024-01")
    path = tmp_path / (b.sha(url.encode()) + ".json")
    r = dict(url=url, sha256=b.sha(b"abc"), body_hex=b"abc".hex())
    path.write_bytes(b.encoded(r))
    assert b.fetch(tmp_path, url, False, None) == r
    r["body_hex"] = b"bad".hex()
    path.write_bytes(b.encoded(r))
    with pytest.raises(ValueError, match="CACHE_CORRUPTION"):
        b.fetch(tmp_path, url, False, None)


def test_no_production_or_database_import():
    text = Path(b.__file__).read_text()
    assert "import crypto_bot" not in text and "from crypto_bot" not in text
    assert "import sqlite" not in text and "import sqlalchemy" not in text


@pytest.mark.parametrize(
    "returns,robust,category",
    [
        ([1, 1], True, "LOW_TURNOVER_EDGE_PRESENT"),
        ([1, -1], False, "LOW_TURNOVER_RESULT_MIXED"),
        ([-1, -1], False, "LOW_TURNOVER_EDGE_NOT_FOUND"),
        ([1, 1], False, "LOW_TURNOVER_RESULT_MIXED"),
    ],
)
def test_classification_no_optimization(returns, robust, category):
    rows = [
        dict(
            benchmark=k,
            net_return_pct=v,
            gross_return_pct=v,
            positive_leave_months=12 if robust else 10,
            leave_month_count=12,
        )
        for k in ("T1", "T2")
        for v in returns
    ]
    assert b.classify(rows)[0] == category


@pytest.mark.parametrize(
    "start,end", [("2023-12-31", "2024-01-02"), ("2025-12-31", "2026-01-02")]
)
def test_forbidden_outcome_periods(start, end):
    with pytest.raises(ValueError, match="INVALID_FROZEN_RUN"):
        b.run({}, "B0", b.stamp(start), b.stamp(end))


def test_missing_cohort_day_fails_without_a_close_fill():
    start, data = fixture()
    del data["BTCUSDT"][start + b.DAY]
    with pytest.raises(ValueError, match="MISSING_OR_INCOMPLETE"):
        b.run(data, "T1", start, start + 2 * b.DAY)


def test_all_cash_zero_volatility_sharpe_undefined():
    start, data = fixture()
    for s in b.SYMBOLS:
        for t, bar in list(data[s].items()):
            data[s][t] = replace(bar, close=D(100))
    r = b.run(data, "T2", start, start + 4 * b.DAY, b.FEE, b.SLIP)
    assert r["metrics"]["ending_equity"] == 1000
    assert r["metrics"]["total_fees"] == 0
    assert r["metrics"]["time_in_market_pct"] == 0
    assert r["metrics"]["descriptive_sharpe_rf0"] is None


def test_known_buyhold_mark_drawdown_and_initial_equity():
    start, data = fixture(days=3)
    for i, close in enumerate([110, 88, 99]):
        t = start + i * b.DAY
        data["BTCUSDT"][t] = replace(data["BTCUSDT"][t], close=D(close))
    r = b.run(data, "B0", start, start + 3 * b.DAY)
    assert [x["equity"] for x in r["daily"]] == [1100, 880, 990]
    assert r["metrics"]["max_drawdown_pct"] == pytest.approx(20)
    assert r["metrics"]["return_pct"] == pytest.approx(-1)


def test_changed_btc_only_to_both_t2_target_allocation():
    qty = {"BTCUSDT": D(10), "ETHUSDT": D(0)}
    cash, events = b.reallocate(
        D(0),
        qty,
        {"BTCUSDT": D(100), "ETHUSDT": D(10)},
        {s: D(".5") for s in b.SYMBOLS},
        b.FEE,
        b.SLIP,
    )
    assert [e["side"] for e in events] == ["SELL", "BUY"]
    assert cash >= 0
    assert abs(qty["BTCUSDT"] * 100 - qty["ETHUSDT"] * 10) < D("1e-20")


def test_no_missing_reference_can_be_called_comparable(tmp_path):
    assert all(
        r["status"] == "NOT DIRECTLY COMPARABLE" for r in b.current_references(tmp_path)
    )
