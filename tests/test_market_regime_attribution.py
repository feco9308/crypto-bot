"""Synthetic, deterministic tests; never fetch data or rerun a benchmark."""

import copy
import json
import math
import statistics
import zipfile
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest

from research import market_regime_attribution as a


def history(days=5):
    t = a.stamp("2024-01-01")
    data = {}
    for i in range(-201, days):
        x = t + i * a.DAY
        data[x] = a.source.Bar(x, x + a.DAY - 1, D(100), D(100))
    return t, data


def test_exact_inclusive_sma200_and_equality_cash():
    t, data = history()
    above, dist, sma = a.state_at(data, t, t + a.DAY)
    assert above is False and sma == 100 and dist == 0
    data[t] = replace(data[t], close=D(200))
    above, dist, sma = a.state_at(data, t, t + a.DAY)
    assert above is True and sma == D("100.5")
    data[t - 200 * a.DAY] = replace(data[t - 200 * a.DAY], close=D("1e12"))
    assert a.state_at(data, t, t + a.DAY) == (above, dist, sma)


@pytest.mark.parametrize("future_end", [0, 1])
def test_completed_daily_only(future_end):
    t, data = history()
    data[t] = replace(data[t], end=t + a.DAY + future_end)
    with pytest.raises(ValueError, match="NOT_COMPLETED"):
        a.daily_row(data, t, t)


def test_incomplete_history_fails_instead_of_zero():
    t, data = history()
    del data[t - 100 * a.DAY]
    with pytest.raises(ValueError, match="MISSING"):
        a.state_at(data, t, t + a.DAY)


def test_future_prices_do_not_change_observed_metrics():
    t, data = history()
    data[t] = replace(data[t], close=D(101))
    row = a.daily_row(data, t, t)
    data[t + a.DAY] = replace(data[t + a.DAY], close=D("1e12"))
    assert a.daily_row(data, t, t) == row
    assert a.stamp(row["timestamp"]) < a.stamp(row["known_at"])


def test_crossing_detection_no_same_close_future_leakage():
    t, data = history()
    for i, cl in enumerate([101, 102, 99, 101]):
        x = t + i * a.DAY
        data[x] = replace(data[x], close=D(cl))
    rows = [a.daily_row(data, t + i * a.DAY, t) for i in range(4)]
    assert [r["below_to_above"] for r in rows] == [True, False, False, True]
    assert [r["above_to_below"] for r in rows] == [False, False, True, False]
    assert rows == [a.daily_row(data, t + i * a.DAY, t) for i in range(4)]


def test_consecutive_spells_are_clipped_censored_and_deterministic():
    t, _ = history()
    flags = [True, True, False, False, False, True]
    rows = [
        dict(
            day=a.source.iso(t + i * a.DAY),
            above_sma200=flag,
            previous_above_sma200=flags[i - 1] if i else True,
        )
        for i, flag in enumerate(flags)
    ]
    spans = a.runs(rows)
    assert [s["days"] for s in spans] == [2, 3, 1]
    assert spans[0]["left_censored"] is True
    assert [s["right_censored"] for s in spans] == [False, False, True]
    assert a.runs(rows) == spans
    rows[0]["previous_above_sma200"] = False
    assert a.runs(rows)[0]["left_censored"] is False


def test_rv20_rv60_sample_daily_simple_return_definition():
    t, data = history()
    prices = [D(100)]
    returns = [D(".01") if k % 3 else D("-.02") for k in range(60)]
    for ret in returns:
        prices.append(prices[-1] * (1 + ret))
    for k, price in enumerate(prices):
        x = t + (k - 60) * a.DAY
        data[x] = replace(data[x], close=price)
    row = a.daily_row(data, t, t)
    assert row["rv20"] == pytest.approx(
        statistics.stdev(map(float, returns[-20:])) * math.sqrt(365), abs=1e-13
    )
    assert row["rv60"] == pytest.approx(
        statistics.stdev(map(float, returns)) * math.sqrt(365), abs=1e-13
    )
    assert row["daily_close_return"] == pytest.approx(float(returns[-1]))


@pytest.mark.parametrize("direction", [1, -1])
def test_efficiency60_exact_monotone_up_and_down(direction):
    t, data = history()
    for k in range(61):
        x = t + (k - 60) * a.DAY
        data[x] = replace(data[x], close=D(200 + direction * k))
    assert a.daily_row(data, t, t)["efficiency60"] == 1


def test_efficiency60_roundtrip_zero_and_flat_undefined():
    t, data = history()
    assert a.daily_row(data, t, t)["efficiency60"] is None
    for k in range(61):
        x = t + (k - 60) * a.DAY
        data[x] = replace(data[x], close=D(100 + 10 * (k % 2)))
    assert a.daily_row(data, t, t)["efficiency60"] == 0


def test_type7_percentiles_and_missing_stay_missing():
    assert a.quantile([1, 2, 3, 4], 0.25) == 1.75
    assert a.quantile([1, 2, 3, 4], 0.5) == 2.5
    assert a.quantile([1, 2, 3, 4], 0.75) == 3.25
    assert a.quantile([None], 0.5) is None
    with pytest.raises(ValueError):
        a.quantile([1, float("nan")], 0.5)


def test_pearson_spearman_average_ties_and_null():
    assert a.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1)
    assert a.pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1)
    assert a.ranks([10, 20, 20, 40]) == [1.0, 2.5, 2.5, 4.0]
    assert a.pearson([1, 1], [1, 2]) is None
    assert a.pearson([1], [1]) is None


def test_monthly_aggregation_uses_initial_open_then_prior_month_close():
    rows = []
    for month, close, down, up in [
        ("2024-01", 110, False, True),
        ("2024-02", 99, True, False),
    ]:
        rows.append(
            dict(
                month=month,
                close=close,
                distance_sma200=0.1,
                above_to_below=down,
                below_to_above=up,
                rv20=0.5,
                efficiency60=0.2,
            )
        )
    result = a.monthly(rows, 100)
    assert result[0]["return_pct"] == pytest.approx(10)
    assert result[1]["return_pct"] == pytest.approx(-10)
    assert [r["sma_crossings"] for r in result] == [1, 1]
    assert a.monthly(rows, 100) == result


def synthetic_full_data():
    first = a.stamp("2023-06-01")
    end = a.stamp("2026-01-01")
    return {
        s: {
            t: a.source.Bar(t, t + a.DAY - 1, D(100), D(100 + (i % 7)))
            for i, t in enumerate(range(first, end, a.DAY))
        }
        for s in a.SYMBOLS
    }


def test_cohorts_exclude_2023_and_2026_outcomes_and_inputs_immutable():
    data = synthetic_full_data()
    before = copy.deepcopy(data)
    tables = a.analyze_market(data)
    assert len(tables["daily"]) == 1400
    assert len(tables["monthly"]) == 46
    assert {r["year"] for r in tables["daily"]} == {2024, 2025}
    assert min(r["day"] for r in tables["daily"]) == "2024-01-01T00:00:00Z"
    assert max(r["day"] for r in tables["daily"]) == "2025-12-31T00:00:00Z"
    assert not any(r["month"] == "2025-01" for r in tables["monthly"])
    assert all(
        r["lag1_pairs"] == (365 if r["year"] == 2024 else 333) for r in tables["serial"]
    )
    assert data == before


def test_only_frozen_families_no_parameter_search():
    assert a.FAMILIES == (
        "buy_hold",
        "sma200_state",
        "sma200_distance",
        "RV20_RV60",
        "efficiency60",
        "serial_lag1",
        "monthly_regime",
    )
    assert a.PERIODS == {
        2024: ("2024-01-01", "2025-01-01"),
        2025: ("2025-02-01", "2026-01-01"),
    }


def sample_events(t):
    return [
        dict(
            event="EXECUTED_NEXT_OPEN",
            timestamp=a.source.iso(t),
            symbol="BTCUSDT",
            side="BUY",
            quantity=1,
            notional=100,
            fee=1,
            reference_price=100,
        ),
        dict(
            event="EXECUTED_NEXT_OPEN",
            timestamp=a.source.iso(t + a.DAY),
            symbol="BTCUSDT",
            side="SELL",
            quantity=1,
            notional=120,
            fee=1,
            reference_price=120,
        ),
    ]


def test_existing_transition_episodes_reconcile_without_any_new_orders():
    t, data = history()
    data[t + a.DAY] = replace(data[t + a.DAY], open=D(120))
    events = sample_events(t)
    before = copy.deepcopy(events)
    ep, pos = a.episode_attribution(
        events, {s: data for s in a.SYMBOLS}, t, t + 3 * a.DAY, 1018
    )
    assert [r["pnl"] for r in ep] == [19, -1]
    assert [r["invested_after_transition"] for r in ep] == [True, False]
    assert sum(r["pnl"] for r in ep) == 18
    assert pos == [
        dict(
            symbol="BTCUSDT",
            start=a.source.iso(t),
            end=a.source.iso(t + a.DAY),
            days=1.0,
            status="CLOSED",
        )
    ]
    assert events == before


def test_open_position_duration_censored_no_forced_exit():
    t, data = history()
    events = sample_events(t)[:1]
    ep, pos = a.episode_attribution(
        events, {s: data for s in a.SYMBOLS}, t, t + 3 * a.DAY, 999
    )
    assert len(ep) == 1 and pos[0]["status"] == "OPEN_AT_END_CENSORED"
    assert pos[0]["days"] == 3
    assert events[0]["side"] == "BUY" and len(events) == 1


def test_partial_position_adjustments_keep_original_start():
    t, data = history()
    events = sample_events(t)[:1]
    events.append(
        dict(events[0], timestamp=a.source.iso(t + a.DAY), quantity=0.5, notional=50)
    )
    ep, pos = a.episode_attribution(
        events, {s: data for s in a.SYMBOLS}, t, t + 3 * a.DAY, 998
    )
    assert len(pos) == 1 and pos[0]["days"] == 3
    assert len(ep) == 2


def test_bad_source_price_or_outside_cohort_fill_rejected():
    t, data = history()
    events = sample_events(t)
    with pytest.raises(ValueError, match="OPEN_MISMATCH"):
        a.episode_attribution(
            events, {s: data for s in a.SYMBOLS}, t, t + 3 * a.DAY, 1018
        )
    with pytest.raises(ValueError, match="OUT_OF_COHORT"):
        a.episode_attribution(
            events, {s: data for s in a.SYMBOLS}, t + a.DAY, t + 3 * a.DAY, 1018
        )


def test_fingerprint_and_manifest_validation(tmp_path):
    (tmp_path / "summary.json").write_bytes(a.source.encoded({"candidate": "NONE"}))
    bundle = a.package(tmp_path)
    digest = a.source.sha(bundle.read_bytes())
    assert a.validate_bundle(bundle, digest)["base"] == a.BASE
    with pytest.raises(ValueError, match="FINGERPRINT"):
        a.validate_bundle(bundle, "0" * 64)


def test_deterministic_artifacts_and_no_nan(tmp_path):
    (tmp_path / "summary.json").write_bytes(
        a.source.encoded({"strategy_candidate": "NONE"})
    )
    p = a.package(tmp_path)
    blob = p.read_bytes()
    assert a.package(tmp_path).read_bytes() == blob
    with zipfile.ZipFile(p) as z:
        assert z.testzip() is None
        manifest = json.loads(z.read("manifest.json"))
        assert (
            a.source.sha(z.read("summary.json"))
            == manifest["files"]["summary.json"]["sha256"]
        )
    with pytest.raises(ValueError):
        a.source.encoded({"x": float("nan")})


def institutional_fixture(tmp_path, data, t):
    folder = tmp_path / "artifacts" / a.source.NAME
    folder.mkdir(parents=True)
    path = tmp_path / "existing.compact.json"
    doc = dict(
        config=dict(start="2024-01-01T00:00:00Z", end="2025-01-01T00:00:00Z"),
        metadata=dict(market_context_version="ISOLATED_TOPN_V1"),
        summary=dict(
            trade_count=2,
            return_pct=-1,
            profit_factor=0.9,
            fees="2",
            slippage_cost="1",
            win_rate=50,
        ),
        trades=[
            dict(
                symbol="BTCUSDT",
                entry_time=a.source.iso(t),
                exit_time=a.source.iso(t + a.DAY),
            ),
            dict(
                symbol="ETHUSDT",
                entry_time=a.source.iso(t + a.DAY),
                exit_time=a.source.iso(t + 2 * a.DAY),
            ),
        ],
    )
    path.write_bytes(a.source.encoded(doc))
    pin = a.source.sha(path.read_bytes())
    ref = dict(
        year=2024,
        source=str(path),
        source_sha256=pin,
        member_sha256=pin,
        run_id="existing",
    )
    (folder / "current-strategy-reference.json").write_bytes(a.source.encoded([ref]))
    return path


def test_institutional_entry_uses_only_preceding_completed_btc_state(tmp_path):
    t, data = history()
    y = t - a.DAY
    data[y] = replace(data[y], close=D(200))
    data[t] = replace(data[t], close=D(50))
    institutional_fixture(tmp_path, data, t)
    rows, entries = a.institutional_reference(tmp_path, {s: data for s in a.SYMBOLS})
    assert [r["btc_state"] for r in entries] == ["ABOVE", "BELOW_EQUAL"]
    assert rows[0]["entries_btc_above"] == 1 and rows[0]["closed_trades"] == 2
    assert all(
        a.stamp(r["btc_source_close"]) < a.stamp(r["entry_time"]) for r in entries
    )
    assert all("realized_pnl" not in r for r in entries)


def test_institutional_modified_source_is_rejected(tmp_path):
    t, data = history()
    path = institutional_fixture(tmp_path, data, t)
    path.write_text("{}")
    with pytest.raises(ValueError, match="SOURCE_CHANGED"):
        a.institutional_reference(tmp_path, {s: data for s in a.SYMBOLS})


def concordance_tables(eff_direction=-1):
    fields = {
        "market": "return_pct",
        "sma_state": "days_above_pct",
        "efficiency": "efficiency60_median",
        "volatility": "rv20_median",
    }
    tables = {k: [] for k in fields}
    for table, field in fields.items():
        for year in (2024, 2025):
            for sym in a.SYMBOLS:
                value = 10 if year == 2024 else 5
                if table == "efficiency" and eff_direction > 0:
                    value = 10 if year == 2024 else 15
                row = dict(year=year, symbol=sym, **{field: value})
                if table == "sma_state":
                    row["crossings_per_365_days"] = 5 if year == 2024 else 10
                tables[table].append(row)
    return tables


def test_classification_requires_independent_family_confirmation():
    category, _ = a.classify(concordance_tables())
    assert category == "TREND_REGIME_SHIFT_CLEAR"
    category, proof = a.classify(concordance_tables(eff_direction=1))
    assert (
        category == "TREND_REGIME_SHIFT_PARTIAL"
        and proof["lower_efficiency60_both"] is False
    )


def test_no_production_strategy_database_or_replay_import():
    text = Path(a.__file__).read_text()
    for banned in [
        "import crypto_bot",
        "from crypto_bot",
        "import sqlite",
        "import sqlalchemy",
    ]:
        assert banned not in text


def test_attribution_cannot_evaluate_benchmark_or_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("REPLAY_OR_NETWORK_FORBIDDEN")

    monkeypatch.setattr(a.source, "run", forbidden)
    monkeypatch.setattr(a.source, "_run", forbidden)
    monkeypatch.setattr(a.source.requests.sessions.Session, "request", forbidden)
    t, data = history()
    events = sample_events(t)[:1]
    rows = a.daily_row(data, t, t)
    assert rows["close"] == 100
    a.episode_attribution(events, {s: data for s in a.SYMBOLS}, t, t + 3 * a.DAY, 999)


def test_timestamp_preserves_milliseconds_and_offset():
    t = a.stamp("2024-01-01T00:00:00.123Z")
    assert t == a.stamp("2024-01-01T01:00:00.123+01:00")
    assert t % 1000 == 123
    with pytest.raises(ValueError):
        a.stamp("2024-01-01T00:00:00.123456Z")
