"""Confirmatory adapter and screen tests: synthetic prices only, no clean outcomes."""

import copy
import json
import zipfile
from dataclasses import replace
from decimal import Decimal as D
from decimal import localcontext
from pathlib import Path

import pytest

from research import mean_reversion_clean_validation as c


def data_fixture(days=365):
    h = {}
    for t in range(c.source.stamp("2022-12-01"), c.START + days * c.DAY, c.DAY):
        h[t] = c.source.Bar(t, t + c.DAY - 1, D(100), D(100))
    return {c.BTC: h}


def signals(t, states):
    return {
        t + i * c.DAY: {
            c.BTC: dict(
                signal_id=str(i),
                source_close=c.source.iso(t + i * c.DAY - 1),
                source_close_ms=t + i * c.DAY - 1,
                known_at_ms=t + i * c.DAY,
                active_after=flag,
                action="BUY" if flag else "SELL",
            )
        }
        for i, flag in enumerate(states)
    }


def engine(data, sig, start, end, net=True):
    with localcontext() as ctx:
        ctx.prec = 45
        return c.bind_btc_accounting()(
            data,
            sig,
            "S2",
            "BTC_ONLY",
            start,
            end,
            c.source.FEE if net else D(0),
            c.source.SLIP if net else D(0),
        )


def twelve(value=1):
    return [
        dict(omitted_month=f"2023-{m:02d}", net_return_pct=value) for m in range(1, 13)
    ]


def test_exact_fixed_2023_cohort_and_btc_scope():
    assert c.START == c.source.stamp("2023-01-01") and c.END == c.source.stamp(
        "2024-01-01"
    )
    assert c.MONTHS == ("2022-12",) + tuple(f"2023-{m:02d}" for m in range(1, 13))
    assert c.BTC == "BTCUSDT"
    assert len(c.btc_signals(data_fixture())) == 365


@pytest.mark.parametrize("month", ["2022-11", "2024-01", "2025-01", "2026-01"])
def test_forbidden_data_months(month):
    with pytest.raises(ValueError, match="ONLY_BTC"):
        c.archive_url(month)


def test_foreign_api_forbidden_before_request(tmp_path):
    with pytest.raises(ValueError, match="FROZEN_ARCHIVE"):
        c.cached_response(
            tmp_path, "https://fapi.binance.com/fapi/v1/order", True, None
        )


def test_same_frozen_code_objects_btc_namespace_only_and_no_global_mutation():
    original = c.source.SYMBOLS
    bound = c.bind_btc_accounting()
    assert bound.__code__ is c.frozen._simulate.__code__
    assert bound.__globals__["source"].SYMBOLS == (c.BTC,)
    allocator = bound.__globals__["source"].reallocate
    assert allocator.__code__ is c.source.reallocate.__code__
    assert (
        allocator.__globals__["_reallocate"].__code__ is c.source._reallocate.__code__
    )
    assert allocator.__globals__["SYMBOLS"] == (c.BTC,)
    assert c.source.SYMBOLS == original == ("BTCUSDT", "ETHUSDT")


def equality_fixture():
    data = data_fixture()
    day = c.START - c.DAY
    h = data[c.BTC]
    prior = [4, 4, -4, 2, -2, 1, -1, 1, -1] + [0] * 10
    for k, dev in enumerate(prior, 1):
        t = day - k * c.DAY
        h[t] = replace(h[t], close=D(100 + dev))
    h[day] = replace(h[day], close=D(96))
    return data, day


def test_exact_sma20_sample_ddof1_lower_band_and_entry_equality():
    data, day = equality_fixture()
    row = c.frozen.observation("S2", data[c.BTC], day, False, c.START)
    assert (
        D(row["sma20"]) == 100
        and D(row["sample_std20"]) == 2
        and D(row["lower_band"]) == 96
    )
    assert row["active_after"] is False
    data[c.BTC][day] = replace(data[c.BTC][day], close=D("95.999"))
    assert (
        c.frozen.observation("S2", data[c.BTC], day, False, c.START)["active_after"]
        is True
    )


def test_exit_greater_equal_mean_and_equality_exits():
    data = data_fixture()
    day = c.START - c.DAY
    assert (
        c.frozen.observation("S2", data[c.BTC], day, True, c.START)["action"] == "SELL"
    )
    data[c.BTC][day] = replace(data[c.BTC][day], close=D(99))
    assert (
        c.frozen.observation("S2", data[c.BTC], day, True, c.START)["action"] == "HOLD"
    )


def test_completed_candle_only():
    data = data_fixture()
    day = c.START - c.DAY
    data[c.BTC][day] = replace(data[c.BTC][day], end=c.START)
    with pytest.raises(ValueError, match="UNCOMPLETED"):
        c.btc_signals(data)


def test_2022_warmup_position_not_inherited():
    data = data_fixture()
    # Previous warmup dip is not a preexisting held position on the cohort start.
    day = c.START - 2 * c.DAY
    data[c.BTC][day] = replace(data[c.BTC][day], close=D(70))
    sig = c.btc_signals(data)
    assert sig[c.START][c.BTC]["active_before"] is False
    assert sig[c.START][c.BTC]["active_after"] is False
    g = c.primary(data)[1]
    assert all(c.source.stamp(r["timestamp"]) >= c.START for r in g["events"])
    assert all(c.source.stamp(r["day"]) >= c.START for r in g["daily"])


def test_next_open_execution_never_source_close():
    data = data_fixture(2)
    sig = signals(c.START, [True, False])
    data[c.BTC][c.START] = replace(data[c.BTC][c.START], open=D(123), close=D(90))
    data[c.BTC][c.START + c.DAY] = replace(data[c.BTC][c.START + c.DAY], open=D(77))
    result = engine(data, sig, c.START, c.START + 2 * c.DAY, False)
    fills = [r for r in result["events"] if r["event"] == "EXECUTED_NEXT_DAILY_OPEN"]
    assert [D(r["fill_price"]) for r in fills] == [D(123), D(77)]
    assert all(
        c.source.stamp(r["source_close"]) < c.source.stamp(r["timestamp"])
        for r in fills
    )


def test_same_bar_fill_rejected():
    data = data_fixture(1)
    sig = signals(c.START, [True])
    sig[c.START][c.BTC]["source_close_ms"] = c.START
    with pytest.raises(ValueError, match="SAME_BAR"):
        engine(data, sig, c.START, c.START + c.DAY)


def test_fee_slippage_each_side_and_no_debt_short():
    data = data_fixture(2)
    sig = signals(c.START, [True, False])
    r = engine(data, sig, c.START, c.START + 2 * c.DAY)
    fill = [x for x in r["events"] if x["event"] == "EXECUTED_NEXT_DAILY_OPEN"]
    assert D(fill[0]["fill_price"]) == D("100.05") and D(fill[1]["fill_price"]) == D(
        "99.95"
    )
    assert all(
        D(x["cash_after_allocation"]) >= 0 and D(x["quantity"]) > 0 for x in fill
    )
    assert r["metrics"]["ending_equity"] == pytest.approx(
        1000 * 0.9995 * 0.999 / (1.0005 * 1.001)
    )
    assert r["metrics"]["actual_modeled_fee"] > 1.99
    assert r["metrics"]["actual_modeled_slippage"] > 0.99


def test_missing_next_in_cohort_open_no_fill_terminal_estimate_only():
    data = data_fixture(1)
    sig = signals(c.START, [True])
    result = engine(data, sig, c.START, c.START + c.DAY)
    fills = [r for r in result["events"] if r["event"] == "EXECUTED_NEXT_DAILY_OPEN"]
    assert [r["side"] for r in fills] == ["BUY"]
    assert result["metrics"]["completed_position_episodes"] == 0
    assert result["metrics"]["open_position_episodes"] == 1
    assert (
        result["episodes"][0]["status"] == "OPEN_AT_END"
        and result["episodes"][0]["realized_pnl"] is None
    )
    assert result["events"][-1]["event"] == "TERMINAL_COST_ESTIMATE_NOT_A_FILL"


def test_2024_open_cannot_affect_terminal_value():
    data = data_fixture()
    sig = signals(c.START, [False] * 364 + [True])
    first = engine(data, sig, c.START, c.END)
    data[c.BTC][c.END] = c.source.Bar(c.END, c.END + c.DAY - 1, D("1e12"), D("1e12"))
    assert engine(data, sig, c.START, c.END) == first
    assert all(c.source.stamp(r["timestamp"]) < c.END for r in first["events"])


def test_open_episode_audit_has_no_fabricated_exit():
    data = data_fixture(1)
    sig = signals(c.START, [True])
    gross = engine(data, sig, c.START, c.START + c.DAY, False)
    net = engine(data, sig, c.START, c.START + c.DAY)
    row = c.episode_audit(gross, net)[0]
    assert (
        row["censored"] is True
        and row["exit_fill_timestamp"] is None
        and row["exit_fill_price_net"] is None
    )
    assert row["net_pnl"] is None and row["terminal_valuation_net_pnl"] < 0


def test_closed_episode_audit_links_decision_fill_and_pnl():
    data = data_fixture(2)
    sig = signals(c.START, [True, False])
    gross = engine(data, sig, c.START, c.START + 2 * c.DAY, False)
    net = engine(data, sig, c.START, c.START + 2 * c.DAY)
    row = c.episode_audit(gross, net)[0]
    assert row["censored"] is False and row["holding_days"] == 1
    assert row["gross_pnl"] == 0 and row["net_pnl"] < 0
    assert c.source.stamp(row["entry_decision_timestamp"]) < c.source.stamp(
        row["entry_fill_timestamp"]
    )
    assert c.source.stamp(row["exit_decision_timestamp"]) < c.source.stamp(
        row["exit_fill_timestamp"]
    )


def test_all_four_frozen_criteria_required_and_no_mixed():
    g = dict(return_pct=1)
    n = dict(return_pct=1, completed_position_episodes=5)
    good = c.confirmatory_screen(g, n, twelve())
    assert good["clean_validation_result"] == "CLEAN_VALIDATION_PASS"
    assert (
        good["candidate_status"] == "CONFIRMED_ON_2023_CLEAN_COHORT"
        and good["final_oos_eligible"] == "YES"
    )
    for gross, net, lm in [
        (dict(return_pct=0), n, twelve()),
        (g, dict(n, return_pct=0), twelve()),
        (g, dict(n, completed_position_episodes=4), twelve()),
        (g, n, twelve(0)),
    ]:
        bad = c.confirmatory_screen(gross, net, lm)
        assert bad["clean_validation_result"] == "CLEAN_VALIDATION_FAIL"
        assert (
            bad["candidate_status"] == "CLEAN_VALIDATION_FAILED"
            and bad["final_oos_eligible"] == "NO"
        )
        assert bad["2023_clean_cohort_consumed"] == "YES"


def test_open_episodes_do_not_inflate_minimum():
    result = c.confirmatory_screen(
        dict(return_pct=10),
        dict(return_pct=10, completed_position_episodes=4, open_position_episodes=100),
        twelve(),
    )
    assert result["criteria"]["completed_net_episodes_at_least_five"] is False


def test_missing_or_duplicate_leave_month_fails():
    for rows in (twelve()[:-1], twelve()[:-1] + [twelve()[0]]):
        assert (
            c.confirmatory_screen(
                dict(return_pct=1),
                dict(return_pct=1, completed_position_episodes=5),
                rows,
            )["clean_validation_result"]
            == "CLEAN_VALIDATION_FAIL"
        )


def test_leave_month_deterministic_and_no_refit():
    rows = [
        dict(month="2023-01", **{"return": 0.1}),
        dict(month="2023-02", **{"return": -0.05}),
    ]
    before = copy.deepcopy(rows)
    result = c.source.leave_month(rows)
    assert result == c.source.leave_month(rows) and rows == before
    assert result[0]["ending_equity"] == pytest.approx(950)


def test_consumed_marker_is_exclusive_and_persists_on_failure(tmp_path):
    p = tmp_path / "candidate.json"
    d = tmp_path / "data.json"
    p.write_text("{}")
    d.write_text("{}")
    c.claim_once(tmp_path, p, d)
    saved = json.loads((tmp_path / "cohort-consumption.json").read_text())
    assert saved["2023_clean_cohort_consumed"] == "YES"
    with pytest.raises(ValueError, match="ALREADY_CONSUMED"):
        c.claim_once(tmp_path, p, d)
    with pytest.raises(ValueError, match="ALREADY_CONSUMED"):
        c.ensure_unconsumed(tmp_path)


def test_exclusive_provenance_never_overwrites(tmp_path):
    p = tmp_path / "candidate.json"
    c.exclusive_json(p, {"frozen": True})
    with pytest.raises(FileExistsError):
        c.exclusive_json(p, {"frozen": False})
    assert json.loads(p.read_text()) == {"frozen": True}


def test_independent_oracle_matches_synthetic_complete_year_and_uses_no_strategy(
    monkeypatch,
):
    data = data_fixture()
    for i in range(365):
        t = c.START + i * c.DAY
        if i % 40 in (5, 6):
            data[c.BTC][t] = replace(data[c.BTC][t], close=D(80 if i % 40 == 5 else 79))
    sig, gross, net = c.primary(data)

    def forbidden(*args, **kwargs):
        raise AssertionError("SHARED_STRATEGY_AS_ORACLE")

    monkeypatch.setattr(c.frozen, "observation", forbidden)
    monkeypatch.setattr(c.frozen, "_simulate", forbidden)
    proof = c.verify_independent(sig, gross, net, data)
    assert proof["status"] == "PASS" and proof["daily_cash_observations"] == 730


def test_deterministic_artifacts(tmp_path):
    (tmp_path / "summary.json").write_bytes(c.source.encoded({"result": "SYNTHETIC"}))
    p = c.package(tmp_path)
    blob = p.read_bytes()
    assert c.package(tmp_path).read_bytes() == blob
    with zipfile.ZipFile(p) as z:
        assert z.testzip() is None
        pin = json.loads(z.read("manifest.json"))["files"]["summary.json"]["sha256"]
        assert c.source.sha(z.read("summary.json")) == pin


def test_no_production_import_database_or_additional_class():
    text = Path(c.__file__).read_text()
    for banned in (
        "from crypto_bot",
        "import crypto_bot",
        "import sqlite",
        "import sqlalchemy",
    ):
        assert banned not in text
    assert c.SCREEN == dict(
        gross_return_strictly_above=0,
        net_return_strictly_above=0,
        minimum_completed_NET_BTC_episodes=5,
        all_12_unchanged_NET_leave_month_returns_strictly_above=0,
    )
