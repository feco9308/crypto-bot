"""Frozen diagnostic correctness: fixed arrays, no network/replay/production writes."""

import hashlib
import io
import math
import zipfile
from array import array
from copy import deepcopy

import pytest

from research import funding_premium as f
from research import funding_premium_stats as s

T = f.ms("2024-06-02T08:00:00Z")


def bars(hours=24, end=T, value=0.001):
    times = list(range(end - hours * f.HOUR, end, f.HOUR))
    return [
        array("q", times),
        array("q", [t + f.HOUR - 1 for t in times]),
        array("d", [value] * hours),
    ]


def events(rows):
    return [array("q", [t for t, _ in rows]), array("d", [v for _, v in rows])]


def calculate(funding=None, premium=None, traded=None, mark=None, index=None, t=T):
    return f.values_at(
        bars(value=100) if traded is None else traded,
        funding,
        bars() if premium is None else premium,
        bars(value=101) if mark is None else mark,
        bars(value=100) if index is None else index,
        t,
    )


@pytest.mark.parametrize("family", f.FAMILIES)
def test_exact_symbol_key_only(family):
    assert "/PEPEUSDT/" in f.key_for(family, "PEPEUSDT", "2024-06")
    assert "1000PEPE" not in f.key_for(family, "PEPEUSDT", "2024-06")


@pytest.mark.parametrize(
    "symbol", ["../BTCUSDT", "BTC/USDT", "btcusdt", "BONKUSDT?x=1"]
)
def test_no_alias_or_path_guessing(symbol):
    with pytest.raises(ValueError):
        f.key_for("fundingRate", symbol, "2024-06")


@pytest.mark.parametrize(
    "family",
    ["aggTrades", "trades", "metrics", "openInterest", "longShortRatio", "liquidation"],
)
def test_other_data_families_forbidden(family):
    with pytest.raises(ValueError):
        f.key_for(family, "BTCUSDT", "2024-06")


@pytest.mark.parametrize("period", ["2026-01", "2023-06", "2025-13", "2024-00"])
def test_out_of_scope_archive_rejected(period):
    with pytest.raises(ValueError):
        f.key_for("fundingRate", "BTCUSDT", period)


@pytest.mark.parametrize("offset,expected", [(-1, 0.002), (0, 0.002), (1, 0.001)])
def test_original_millisecond_boundary(offset, expected):
    funding = events([(T - f.HOUR, 0.001), (T + offset, 0.002)])
    values, _, meta = calculate(funding)
    assert values["funding_rate_last"] == expected
    assert meta["funding_last_event_ms"] <= T


def test_exact_open_left_closed_right_window_and_no_fixed_cadence():
    funding = events(
        [
            (T - f.DAY, 0.99),
            (T - f.DAY + 1, 0.001),
            (T - 3 * f.HOUR - 3, -0.002),
            (T, 0.004),
            (T + 1, 0.9),
        ]
    )
    values, reasons, meta = calculate(funding)
    assert values["funding_rate_last"] == 0.004
    assert values["funding_sum_24h"] == pytest.approx(0.003)
    assert meta["funding_event_count_24h"] == 3
    assert reasons["funding_sum_24h"] == "AVAILABLE"


@pytest.mark.parametrize("offset", [-f.DAY, -f.DAY - 1, 1])
def test_no_recent_event_is_unavailable_not_zero(offset):
    values, _, _ = calculate(events([(T + offset, 0.001)]))
    assert math.isnan(values["funding_rate_last"]) and math.isnan(
        values["funding_sum_24h"]
    )


def test_real_zero_funding_is_available():
    values, reasons, _ = calculate(events([(T, 0)]))
    assert values["funding_rate_last"] == values["funding_sum_24h"] == 0
    assert reasons["funding_rate_last"] == "AVAILABLE"


def test_front_like_funding_after_traded_coverage_cannot_extend_any_feature():
    # Later funding / premium / mark / index exist, but the previous traded bar does not.
    old = bars(end=T - f.HOUR, value=100)
    values, reasons, meta = calculate(events([(T, 0.001)]), traded=old)
    assert all(math.isnan(values[k]) for k in f.FEATURES)
    assert set(reasons.values()) == {"INACTIVE_TRADED_FUTURES_MARKET"}
    assert meta["traded_futures_active_at_t"] == 0


def test_funding_cannot_activate_market_with_no_traded_history():
    values, _, _ = f.values_at(
        None, events([(T, 0.01)]), bars(), bars(value=100), bars(value=100), T
    )
    assert all(math.isnan(v) for v in values.values())


def test_premium_current_future_bar_excluded_and_input_unchanged():
    premium = bars(25, end=T + f.HOUR)
    premium[2][-1] = 999
    untouched = deepcopy(premium)
    values, _, meta = calculate(premium=premium)
    assert values["premium_close_1h"] == 0.001
    assert values["premium_mean_24h"] == pytest.approx(0.001)
    assert meta["premium_source_close_ms"] < T
    assert premium == untouched


def test_premium_close_equal_to_decision_is_not_available():
    premium = bars()
    premium[1][-1] = T
    values, _, _ = calculate(premium=premium)
    assert math.isnan(values["premium_close_1h"])
    assert math.isnan(values["premium_mean_24h"])


def test_24h_premium_needs_exact_contiguous_window():
    premium = bars(25)
    for col in premium:
        del col[-12]
    values, _, _ = calculate(premium=premium)
    assert values["premium_close_1h"] == 0.001
    assert math.isnan(values["premium_mean_24h"])


def test_premium_mean_is_arithmetic_mean_of_closes():
    premium = bars()
    premium[2] = array("d", range(24))
    values, _, _ = calculate(premium=premium)
    assert values["premium_mean_24h"] == 11.5
    assert values["premium_close_1h"] == 23


def test_basis_requires_same_immediate_previous_hour():
    values, _, _ = calculate(index=bars(end=T - f.HOUR, value=100))
    assert math.isnan(values["mark_index_basis_1h"])
    values, _, meta = calculate()
    assert values["mark_index_basis_1h"] == pytest.approx(0.01)
    assert meta["mark_source_close_ms"] == meta["index_source_close_ms"] == T - 1


def test_zero_index_cannot_create_basis():
    values, _, _ = calculate(index=bars(value=0))
    assert math.isnan(values["mark_index_basis_1h"])


def blob(text):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("sample.csv", text)
    return data.getvalue()


def test_parse_funding_preserves_irregular_milliseconds():
    data = blob(
        f"calc_time,funding_interval_hours,last_funding_rate\n{T},8,0.001\n{T + 1},4,-0.002\n"
    )
    cols, meta = f.parse_archive(data, f.key_for("fundingRate", "BTCUSDT", "2024-06"))
    assert list(cols[0]) == [T, T + 1]
    assert meta["row_count"] == 2


@pytest.mark.parametrize("times", [(T, T), (T, T - 1)])
def test_duplicate_unordered_funding_rejected(times):
    data = blob("".join(f"{t},8,.001\n" for t in times))
    with pytest.raises(ValueError, match="DUPLICATE_OR_UNORDERED"):
        f.parse_archive(data, f.key_for("fundingRate", "BTCUSDT", "2024-06"))


def test_archive_checksum_cache_pin_and_offline(tmp_path):
    cache = f.Cache(tmp_path)
    key = f.key_for("fundingRate", "BTCUSDT", "2024-06")
    data = blob(f"{T},8,.001\n")
    with pytest.raises(ValueError, match="CHECKSUM"):
        cache.save(key, data, "0" * 64, "TEST")
    pin = hashlib.sha256(data).hexdigest()
    cache.save(key, data, pin, "TEST")
    assert cache.fetch(key)["sha256"] == pin
    (tmp_path / "objects" / (pin + ".zip")).write_bytes(b"bad")
    with pytest.raises(ValueError, match="CHECKSUM"):
        cache.read(key)


def test_network_cannot_reach_api_or_nonapproved_families(tmp_path):
    cache = f.Cache(tmp_path, download=True)
    for url in (
        "https://fapi.binance.com/fapi/v1/order",
        "https://data.binance.vision/data/futures/um/monthly/aggTrades/BTCUSDT/x.zip",
    ):
        with pytest.raises(ValueError):
            cache.get(url, 100)
    assert cache.requests == 0


def synthetic_table():
    table = s.Table()
    for i in range(100):
        table.add(
            dict(
                symbol="AAAUSDT" if i < 50 else "BBBUSDT",
                timestamp=f"2024-01-{i // 24 + 1:02}T{i % 24:02}:00:00Z",
                symbol_day_id=str(i // 24),
                current_buy_eligible=1.0,
                symbol_day_first=float(i % 24 == 0),
                setup_episode_first=float(i % 3 == 0),
                **{feature: float(i) for feature in f.FEATURES},
                **{feature + "_status": "AVAILABLE" for feature in f.FEATURES},
                **{o: float(100 - i) for o in f.OUTCOMES},
            )
        )
    return table


def test_deterministic_coverage_and_80pct_inclusive():
    table = synthetic_table()
    a = f.coverage_rows(table, 2024)
    assert a == f.coverage_rows(table, 2024)
    feature = f.FEATURES[0]
    cells = [
        dict(year=y, population=p, feature=feature, coverage_pct=80)
        for y in (2024, 2025)
        for p in ("ALL_TOP20", "BUY_ELIGIBLE")
    ]
    assert f.coverage_gate(cells, feature)
    cells[0]["coverage_pct"] = 79.999
    assert not f.coverage_gate(cells, feature)


def test_feature_set_and_expected_negative_are_frozen():
    assert len(f.FEATURES) == 5 and f.EXPECTED_DIRECTION == -1
    method = f.methodology()
    assert method["expected_direction"] == "NEGATIVE"
    assert method["no_threshold_optimization"] and method["no_new_features"]
    assert all(not key.startswith("POST_HOC") for key in f.FEATURES)


@pytest.mark.parametrize("group_type", ["symbol", "quarter"])
def test_leave_group_fixed_quintiles_matches_bruteforce(group_type):
    table = synthetic_table()
    outcome = f.PREFIX + "mfe_atr_72h"
    _, _, _, top, bottom = s.effect(table, list(range(100)), f.FEATURES[0], outcome)
    group = (
        (lambda i: table.text["symbol"][i])
        if group_type == "symbol"
        else (lambda i: str(i % 4))
    )
    result = s.leave_group_effect(table, top, bottom, outcome, group)
    for row in result:
        a = [table.numeric[outcome][i] for i in top if group(i) != row["removed_group"]]
        b = [
            table.numeric[outcome][i]
            for i in bottom
            if group(i) != row["removed_group"]
        ]
        auc = s.auc(a, b)
        assert (
            row["cliffs_delta"] == pytest.approx(2 * auc - 1)
            if auc is not None
            else row["cliffs_delta"] is None
        )


def test_unexpected_positive_and_secondary_cannot_create_candidate():
    check = dict(feature=f.FEATURES[0], coverage_pass=True, candidate_eligible=False)
    category, candidate = f.classify(
        [check], [dict(feature=f.FEATURES[0], cliffs_delta=0.8)]
    )
    assert category == "FUNDING_PREMIUM_SIGNAL_WEAK_OR_UNSTABLE" and candidate == "NONE"
    assert f.classify([check], [])[0] == "FUNDING_PREMIUM_SIGNAL_NOT_FOUND"
    assert (
        f.classify([check], [dict(feature="SECONDARY_1M", cliffs_delta=-1)])[0]
        == "FUNDING_PREMIUM_SIGNAL_NOT_FOUND"
    )
    screen = f.evidence_screen(f.FEATURES[0], [], [], [], [], True)
    assert not screen["main_all_year_horizon_pass"] and not screen["candidate_eligible"]


def test_positive_main_effect_cannot_pass_negative_screen():
    stats = []
    for y in (2024, 2025):
        for p in (
            "ALL_TOP20",
            "BUY_ELIGIBLE",
            "SYMBOL_DAY_FIRST",
            "BUY_SETUP_EPISODE_FIRST",
        ):
            for h in (72, 168):
                stats.append(
                    dict(
                        year=y,
                        population=p,
                        outcome=f.PREFIX + f"mfe_atr_{h}h",
                        feature=f.FEATURES[0],
                        top_count=100,
                        bottom_count=100,
                        cliffs_delta=0.5,
                        spearman=0.2,
                    )
                )
    result = f.evidence_screen(f.FEATURES[0], stats, [], [], [], True)
    assert not result["main_all_year_horizon_pass"] and not result["candidate_eligible"]


def test_deterministic_deciles_and_zip(tmp_path):
    assert s.deciles([0, 1, 1, 2]) == s.deciles([0, 1, 1, 2])
    assert s.deciles([1] * 10) == [6] * 10
    (tmp_path / "a.json").write_text("{}\n")
    archive = f.package(tmp_path)
    first = archive.read_bytes()
    assert f.package(tmp_path).read_bytes() == first
    with zipfile.ZipFile(archive) as z:
        assert z.namelist() == ["a.json", "manifest.json"]
        assert z.testzip() is None


def test_read_only_database_cannot_write(tmp_path):
    import sqlite3

    path = tmp_path / "test.db"
    with sqlite3.connect(path) as db:
        db.execute("create table test(x)")
    with s.read_only(path) as db:
        assert db.execute("pragma query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            db.execute("insert into test values (1)")


def negative_evidence():
    stats = []
    for y in (2024, 2025):
        for p in (
            "ALL_TOP20",
            "BUY_ELIGIBLE",
            "SYMBOL_DAY_FIRST",
            "BUY_SETUP_EPISODE_FIRST",
        ):
            for h in (72, 168):
                stats.append(
                    dict(
                        year=y,
                        population=p,
                        outcome=f.PREFIX + f"mfe_atr_{h}h",
                        feature=f.FEATURES[0],
                        top_count=100,
                        bottom_count=100,
                        cliffs_delta=-0.3,
                        spearman=-0.1,
                    )
                )
    groups = [
        dict(
            feature=f.FEATURES[0],
            population="ALL_TOP20",
            group_type=k,
            top_count=100,
            bottom_count=100,
            retains_direction_and_half_effect=True,
            full_cliffs_delta=-0.3,
        )
        for k in ("SYMBOL", "UTC_CALENDAR_QUARTER")
    ]
    return stats, groups


def test_frozen_negative_screen_accepts_only_complete_primary_evidence():
    stats, groups = negative_evidence()
    result = f.evidence_screen(f.FEATURES[0], stats, [], groups, [], True)
    assert result["candidate_eligible"]
    assert f.classify([result], stats) == (
        "FUNDING_PREMIUM_SIGNAL_FOUND",
        f.FEATURES[0],
    )
    assert not f.evidence_screen(f.FEATURES[0], stats, [], groups, [], False)[
        "candidate_eligible"
    ]


@pytest.mark.parametrize("kind", ["SYMBOL", "UTC_CALENDAR_QUARTER"])
def test_leave_group_failure_prevents_candidate(kind):
    stats, groups = negative_evidence()
    next(r for r in groups if r["group_type"] == kind)[
        "retains_direction_and_half_effect"
    ] = False
    assert not f.evidence_screen(f.FEATURES[0], stats, [], groups, [], True)[
        "candidate_eligible"
    ]


def test_selection_and_secondary_can_veto_but_never_promote():
    stats, groups = negative_evidence()
    selection = [
        dict(
            feature=f.FEATURES[0],
            outcome=f.PREFIX + "mfe_atr_72h",
            available_observed=100,
            unavailable_observed=100,
            cliffs_delta=0.3,
        )
    ]
    assert not f.evidence_screen(f.FEATURES[0], stats, [], groups, selection, True)[
        "candidate_eligible"
    ]
    secondary = [
        dict(
            feature=f.FEATURES[0],
            outcome=f.SECONDARY[0],
            positive_count=100,
            negative_count=100,
            cliffs_delta=0.3,
        )
    ]
    assert not f.evidence_screen(f.FEATURES[0], stats, secondary, groups, [], True)[
        "candidate_eligible"
    ]
    assert f.evidence_screen(f.FEATURES[0], stats, secondary, groups, [], True)[
        "primary_pass"
    ]


def test_matched_control_hashes_outcomes_and_no_source_mutation():
    table = synthetic_table()
    for name in f.CONTROLS + (f.FLOW_CONTROL,):
        table.numeric[name] = array("d", range(100))
    table.numeric[f.CONTROLS[0]][0] = f.NAN
    for label in f.SECONDARY:
        table.numeric[label] = array("d", [i % 2 for i in range(100)])
    before = deepcopy(table.numeric)
    stats, deciles, secondary, controls, concentration = f.statistics_for(
        table, 2024, [f.FEATURES[0]]
    )
    assert (
        len(stats) == 25
        and len(deciles) == 50
        and len(secondary) == 10
        and concentration
    )
    cells = {}
    for r in controls:
        key = (r["population"], r["matched_pool"], r["outcome"])
        cells.setdefault(key, []).append(r)
    for rows in cells.values():
        assert (
            len(
                {
                    (
                        r["input_row_sha256"],
                        r["paired_row_sha256"],
                        r["paired_outcome_sha256"],
                        r["paired_count"],
                    )
                    for r in rows
                }
            )
            == 1
        )
    for key in before:
        assert all(
            a == b or math.isnan(a) and math.isnan(b)
            for a, b in zip(before[key], table.numeric[key])
        )


def test_parser_candle_boundary_and_negative_premium():
    op = T - f.HOUR

    def candle(close):
        return blob(f"{op},-.002,.001,-.003,-.001,0,{close},0,0,0,0,0\n")

    key = f.key_for("premiumIndexKlines", "BTCUSDT", "2024-06")
    cols, _ = f.parse_archive(candle(T - 1), key)
    assert cols[2][0] == -0.001
    with pytest.raises(ValueError, match="INTERVAL"):
        f.parse_archive(candle(T), key)


def test_leave_group_without_extreme_members_is_explicit_unchanged_effect():
    table = synthetic_table()
    outcome = f.PREFIX + "mfe_atr_72h"
    full, _, _, top, bottom = s.effect(table, list(range(100)), f.FEATURES[0], outcome)

    def group(i):
        return "MIDDLE_ONLY" if 40 <= i < 60 else "OTHER"

    rows = s.leave_group_effect(
        table, top, bottom, outcome, group, {"MIDDLE_ONLY", "OTHER"}
    )
    middle = next(r for r in rows if r["removed_group"] == "MIDDLE_ONLY")
    assert middle["removed_top_count"] == middle["removed_bottom_count"] == 0
    assert middle["cliffs_delta"] == full["cliffs_delta"]


def test_recovery_reads_dataset_without_rebuilding_or_overwriting(tmp_path):
    table = synthetic_table()
    path = tmp_path / "dataset.csv"
    table.write(path)
    before = path.read_bytes()
    recovered = f.load_existing_table(path)
    assert path.read_bytes() == before
    assert recovered.size == table.size
    assert recovered.text == table.text
    assert recovered.numeric == table.numeric


def test_recovery_rejects_duplicates_and_changed_completed_stage(tmp_path):
    table = synthetic_table()
    path = tmp_path / "dataset.csv"
    table.write(path)
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines + [lines[1]]))
    with pytest.raises(ValueError, match="DUPLICATE_CANONICAL"):
        f.load_existing_table(path)
    stage = tmp_path / "stage.csv"
    f.write_rows(stage, [dict(a=1, b="stable")])
    before = stage.read_bytes()
    f.verify_existing_rows(stage, [dict(a=1, b="stable")])
    with pytest.raises(ValueError, match="RECOVERY_STAGE_MISMATCH"):
        f.verify_existing_rows(stage, [dict(a=2, b="changed")])
    assert stage.read_bytes() == before


def test_recovery_cannot_enable_download(monkeypatch, tmp_path):
    import sys
    from unittest.mock import Mock

    capture = Mock()
    monkeypatch.setattr(f, "capture", capture)
    monkeypatch.setattr(
        sys,
        "argv",
        ["diagnostic", "--root", str(tmp_path), "--resume-existing", "--download"],
    )
    with pytest.raises(SystemExit):
        f.main()
    capture.assert_not_called()


def test_front_audit_does_not_claim_cohort_cache_end_is_global_market_end():
    from research.funding_premium_validate import front_audit

    hist = {
        ("klines", "FRONTUSDT"): bars(),
        ("fundingRate", "FRONTUSDT"): [
            array("q", [T - f.HOUR]),
            array("q", [T - f.HOUR]),
            array("d", [0.001]),
        ],
    }
    report = front_audit(hist)
    assert "NOT_GLOBAL_INSTRUMENT_COVERAGE" in report["scope"]
    assert "last_traded_close" not in report
    assert report["last_traded_close_in_required_cache"] == f.iso(T - 1)
