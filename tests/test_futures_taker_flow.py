"""Deterministic, synthetic sources only; never Binance or production DB."""

import ast
import hashlib
import io
import math
import sqlite3
import zipfile
from pathlib import Path

import pytest

from research import futures_taker_flow as f
from research import institutional_signal_edge as edge

T = edge.ms("2024-01-01T00:00:00Z")


def blob(hours=30, duplicate=False, gap=None, q=None, b=None):
    rows = []
    for i in range(hours):
        if i == gap:
            continue
        t = T + i * f.HOUR
        quote = q[i] if q else 100
        buy = b[i] if b else 40
        row = [t, 1, 1, 1, 1, quote, t + f.HOUR - 1, quote, 1, buy, buy, 0]
        rows.append(",".join(map(str, row)))
        if i == 0 and duplicate:
            rows.append(rows[-1])
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        z.writestr("file.csv", "\n".join(rows) + "\n")
    return stream.getvalue()


def history(n=30, q=None, b=None):
    h, _ = f.parse_archive(blob(n, q=q, b=b), f.key_for("BTCUSDT", "2024-01"))
    return h


def table():
    t = f.cross.Table()
    for i in range(40):
        t.add(
            dict(
                symbol="BTCUSDT" if i % 2 else "ETHUSDT",
                timestamp=edge.iso(T + i * f.HOUR),
                timestamp_ms=T + i * f.HOUR,
                symbol_day_id=str(i // 24),
                symbol_day_first=float(i % 24 == 0),
                setup_episode_first=float(i % 3 == 0),
                current_buy_eligible=float(i % 2 == 0),
                futures_1h_available=float(i % 7 != 0),
                futures_availability_reason="AVAILABLE" if i % 7 else "MISSING",
                **{k: float(i) for k in f.CONTROLS},
                **{k: float(i % 13) for k in f.OUTCOMES},
                **{k: float(i % 2) for k in f.SECONDARY},
            )
        )
    return t


@pytest.mark.parametrize("symbol", ["BTCUSDT", "1000SATSUSDT", "FETUSDT"])
def test_exact_archive_url(symbol):
    key = f.key_for(symbol, "2024-01")
    assert f.official_url(key) == f.audit.ARCHIVE + key
    assert "/" + symbol + "/1h/" + symbol + "-1h-" in key


@pytest.mark.parametrize(
    "symbol,month",
    [("../BTCUSDT", "2024-01"), ("BTCUSDT", "2026-01"), ("BTCUSDT", "2023-11")],
)
def test_invalid_alias_or_future_url_rejected(symbol, month):
    with pytest.raises(ValueError):
        f.key_for(symbol, month)


@pytest.mark.parametrize(
    "key",
    [
        "data/futures/um/monthly/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01.zip",
        "data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-01.zip",
        "data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip",
    ],
)
def test_non_1h_data_families_rejected(key):
    with pytest.raises(ValueError):
        f.official_url(key)


def test_no_alias_guessing_in_source_mapping():
    source = Path(f.__file__).read_text()
    assert '"1000" + symbol' not in source
    assert "if s in archive_symbols" in source
    assert f.key_for("PEPEUSDT", "2024-01") != f.key_for("1000PEPEUSDT", "2024-01")


def test_checksum_pinned_cache_and_corruption_rejected(tmp_path):
    cache = f.Cache(tmp_path)
    key = f.key_for("BTCUSDT", "2024-01")
    data = blob()
    digest = hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError, match="CHECKSUM"):
        cache.save(key, data, "0" * 64, "test")
    row = cache.save(key, data, digest, "verified_fixture")
    assert cache.read(key) == row
    (cache.root / "objects" / (digest + ".zip")).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="CHECKSUM"):
        cache.read(key)


def test_default_cache_never_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(
        f.requests.Session, "get", lambda *a, **k: pytest.fail("Network forbidden")
    )
    cache = f.Cache(tmp_path)
    with pytest.raises(ValueError, match="offline"):
        cache.fetch(f.key_for("BTCUSDT", "2024-01"))


def test_duplicate_rejects_archive_and_is_never_deduplicated_silently():
    with pytest.raises(ValueError, match="DUPLICATE"):
        f.parse_archive(blob(duplicate=True), f.key_for("BTCUSDT", "2024-01"))


def test_missing_hour_detected():
    h, meta = f.parse_archive(blob(gap=10), f.key_for("BTCUSDT", "2024-01"))
    assert meta["missing_hours"] == 1
    assert f.window(h, T + 11 * f.HOUR, 1)[0] is None
    assert f.flow_value(h, T + 12 * f.HOUR, 4)[1] == "MISSING_OR_STALE_HOUR"


@pytest.mark.parametrize("window", [1, 4, 24])
def test_current_and_future_candles_cannot_leak(window):
    h = history(48)
    t = T + 24 * f.HOUR
    before = f.flow_value(h, t, window)
    for i in range(24, 48):
        h[2][i] = 1e20
        h[3][i] = 1e20
    assert f.flow_value(h, t, window) == before
    assert h[1][23] < t


def test_close_time_equal_decision_rejected():
    h = history()
    t = T + 24 * f.HOUR
    h[1][23] = t
    assert f.window(h, t, 1) == (None, "CLOSE_TIME_NOT_STRICTLY_PAST")


@pytest.mark.parametrize("hours", [4, 24])
def test_rolling_window_requires_every_hour(hours):
    h = history()
    i = 24 - hours
    del h[0][i]
    del h[1][i]
    del h[2][i]
    del h[3][i]
    assert math.isnan(f.flow_value(h, T + 24 * f.HOUR, hours)[0])


def test_sum_numerator_over_sum_denominator_not_mean_shares():
    h = history(4, q=[1, 1, 1, 97], b=[1, 1, 1, 0])
    value, _ = f.flow_value(h, T + 4 * f.HOUR, 4)
    assert value == 0.03 and value != 0.75


def test_zero_quote_denominator_unavailable_but_not_a_missing_bar():
    h = history(4, q=[0] * 4, b=[0] * 4)
    assert f.window(h, T + 4 * f.HOUR, 1)[0] is not None
    assert math.isnan(f.flow_value(h, T + 4 * f.HOUR, 1)[0])
    assert f.flow_value(h, T + 4 * f.HOUR, 4)[1] == "ZERO_WINDOW_QUOTE_VOLUME"
    h = history(4, q=[0, 0, 0, 10], b=[0, 0, 0, 5])
    assert f.flow_value(h, T + 4 * f.HOUR, 4)[0] == 0.5


def test_complete_interval_validation():
    data = blob(1)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        row = z.read("file.csv").decode().strip().split(",")
    row[6] = str(T + f.HOUR)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        z.writestr("file.csv", ",".join(row))
    with pytest.raises(ValueError, match="INVALID_HOURLY"):
        f.parse_archive(stream.getvalue(), f.key_for("BTCUSDT", "2024-01"))


def test_coverage_gate_all_four_inclusive_boundary():
    assert f.coverage_go([dict(coverage_pct=80)] * 4)
    assert not f.coverage_go([dict(coverage_pct=80)] * 3)
    assert not f.coverage_go([dict(coverage_pct=80)] * 3 + [dict(coverage_pct=79.999)])


def test_deterministic_exact_coverage_and_original_episode_flags():
    t = table()
    first = f.coverage_rows(t, 2024)
    assert first == f.coverage_rows(t, 2024)
    assert first[0]["total_rows"] == 40 and first[0]["available_rows"] == 34
    scopes = f.populations(t)
    assert scopes["SYMBOL_DAY_FIRST"] == [24]
    assert all(
        t.numeric["setup_episode_first"][i] for i in scopes["BUY_SETUP_EPISODE_FIRST"]
    )


def test_outcomes_and_future_highs_are_not_feature_inputs():
    t = table()
    outcomes = {k: list(t.numeric[k]) for k in f.OUTCOMES + f.SECONDARY}
    f.add_flow(t, {"BTCUSDT": history(48), "ETHUSDT": history(48)})
    assert outcomes == {k: list(t.numeric[k]) for k in f.OUTCOMES + f.SECONDARY}
    assert all(k.startswith("POST_HOC_FUTURE_LABEL_1H.") for k in f.OUTCOMES)
    assert len(f.FLOW) == 3 and f.WINDOWS == (1, 4, 24)


def test_deciles_fixed_ties_deterministic():
    assert f.cross.deciles(list(range(20))) == [i // 2 + 1 for i in range(20)]
    assert len(set(f.cross.deciles([0.5] * 20))) == 1


def test_selection_analysis_does_not_weight_or_change_rows():
    t = table()
    before = {k: list(v) for k, v in t.numeric.items()}
    rows = f.selection_analysis(t, 2024)
    assert len(rows) == 8
    assert before == {k: list(v) for k, v in t.numeric.items()}
    assert all(
        r["available_observed"] + r["unavailable_observed"]
        == (40 if r["population"] == "ALL_TOP20" else 20)
        for r in rows
    )


def test_exact_leave_group_auc_matches_brute_force_with_ties():
    t = f.cross.Table()
    for i, value in enumerate([1, 2, 2, 4, 1, 2, 3, 3]):
        t.add(dict(symbol="A" if i % 3 == 0 else "B", value=value))
    top = [0, 1, 2, 3]
    bottom = [4, 5, 6, 7]
    rows = f.leave_group_effect(t, top, bottom, "value", lambda i: t.text["symbol"][i])
    for row in rows:
        a = [
            t.numeric["value"][i]
            for i in top
            if t.text["symbol"][i] != row["removed_group"]
        ]
        b = [
            t.numeric["value"][i]
            for i in bottom
            if t.text["symbol"][i] != row["removed_group"]
        ]
        expected = 2 * edge.auc(a, b) - 1
        assert row["cliffs_delta"] == pytest.approx(expected)


def test_secondary_cannot_promote_category():
    checks = [
        dict(feature=f.FLOW[0], candidate_eligible=False, minimum_all_abs_cliff=0)
    ]
    assert f.classify(checks, []) == ("FUTURES_TAKER_FLOW_SIGNAL_NOT_FOUND", "NONE")
    secondary = [
        dict(
            feature=f.FLOW[0],
            positive_count=100,
            negative_count=100,
            cliffs_delta=1,
            outcome=f.SECONDARY[0],
        )
    ]
    check = f.screen(f.FLOW[0], [], secondary, [])
    assert not check["primary_pass"] and not check["candidate_eligible"]


def test_actual_statistics_and_matched_controls_identical_row_sets():
    t = table()
    f.add_flow(t, {"BTCUSDT": history(48), "ETHUSDT": history(48)})
    stats, bins, secondary, controls, groups = f.describe(t, 2024)
    assert len(bins) == 5 * 6 * 10
    assert all(r["role"] == "SECONDARY_ONLY" for r in secondary)
    keyed = {}
    for r in controls:
        key = r["population"], r["matched_flow_feature"], r["outcome"]
        keyed.setdefault(key, set()).add(
            (r["paired_count"], r["paired_row_sha256"], r["matched_input_row_sha256"])
        )
    assert all(len(v) == 1 for v in keyed.values())
    assert len(stats) == 5 * 6 * 5


def test_scope_warmup_only_no_2023_or_2026_decision_cohort():
    needed = f.months_needed("BTCUSDT", T)
    assert needed == {f.key_for("BTCUSDT", "2023-12")}
    assert all(
        "2026" not in k
        for k in f.months_needed("BTCUSDT", edge.ms("2025-12-31T23:00:00Z"))
    )


def test_no_production_db_write_or_new_replay(tmp_path):
    path = tmp_path / "test.db"
    with sqlite3.connect(path) as db:
        db.execute("create table replay_runs(id text)")
    digest = f.cross.sha(path)
    with edge.read_only(path) as db:
        assert db.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO replay_runs VALUES ('new')")
    assert f.cross.sha(path) == digest


def test_export_deterministic_zip_and_cache_excluded(tmp_path):
    (tmp_path / "summary.json").write_bytes(edge.encoded(dict(candidate="NONE")))
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "secret.zip").write_bytes(b"not included")
    first = f.package(tmp_path).read_bytes()
    assert f.package(tmp_path).read_bytes() == first
    with zipfile.ZipFile(f.package(tmp_path)) as z:
        assert z.namelist() == sorted(z.namelist()) and not any(
            "cache" in k for k in z.namelist()
        )


def test_no_runtime_engine_or_optimization_calls():
    source = Path(f.__file__).read_text()
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {
        "ReplayEngine",
        "RiskManager",
        "PortfolioService",
        "ReplayRepository",
        "GridSearchCV",
        "fit",
    }
    assert "INSERT INTO" not in source
    assert set(f.FLOW) == {
        "taker_buy_share_1h",
        "taker_buy_share_4h",
        "taker_buy_share_24h",
    }


def frozen_evidence():
    feature = f.FLOW[0]
    stats = []
    for year in (2024, 2025):
        for population in (
            "ALL_TOP20",
            "SYMBOL_DAY_FIRST",
            "BUY_ELIGIBLE",
            "BUY_SETUP_EPISODE_FIRST",
        ):
            for h in (72, 168):
                stats.append(
                    dict(
                        year=year,
                        population=population,
                        feature=feature,
                        outcome=f.cross.LABEL_PREFIX + f"mfe_atr_{h}h",
                        top_count=100,
                        bottom_count=100,
                        cliffs_delta=0.2 if population == "ALL_TOP20" else 0.1,
                        spearman=0.1,
                    )
                )
    groups = [
        dict(
            feature=feature,
            population="ALL_TOP20",
            top_count=90,
            bottom_count=90,
            cliffs_delta=0.15,
            full_cliffs_delta=0.2,
        )
    ]
    return stats, groups


def test_frozen_primary_screen_requires_both_years_and_sensitivities():
    stats, groups = frozen_evidence()
    feature = f.FLOW[0]
    assert f.screen(feature, stats, [], groups)["primary_pass"]
    assert (
        f.classify([f.screen(feature, stats, [], groups)], stats)[0]
        == "FUTURES_TAKER_FLOW_SIGNAL_FOUND"
    )
    stats[-1]["cliffs_delta"] = -0.1
    assert not f.screen(feature, stats, [], groups)["candidate_eligible"]


def test_dominant_or_inadequate_leave_out_cannot_be_hidden():
    stats, groups = frozen_evidence()
    groups.append(groups[0] | dict(top_count=2, cliffs_delta=0.3))
    assert not f.screen(f.FLOW[0], stats, [], groups)["leave_symbol_quarter_pass"]
    groups[-1] = groups[0] | dict(cliffs_delta=0.09)
    assert not f.screen(f.FLOW[0], stats, [], groups)["candidate_eligible"]


def test_secondary_strong_opposite_only_vetoes_never_establishes_primary():
    stats, groups = frozen_evidence()
    feature = f.FLOW[0]
    contrary = [
        dict(
            feature=feature,
            positive_count=100,
            negative_count=100,
            outcome=f.SECONDARY[0],
            cliffs_delta=-0.2,
        )
    ]
    check = f.screen(feature, stats, contrary, groups)
    assert check["primary_pass"] and not check["candidate_eligible"]
    assert f.classify([check], stats) == (
        "FUTURES_TAKER_FLOW_SIGNAL_WEAK_OR_UNSTABLE",
        "NONE",
    )


def test_missing_control_values_do_not_change_matched_row_identity():
    t = table()
    t.numeric["delta_4h"][26] = float("nan")
    f.add_flow(t, {"BTCUSDT": history(48), "ETHUSDT": history(48)})
    _, _, _, rows, _ = f.describe(t, 2024)
    grouped = {}
    for row in rows:
        key = row["population"], row["matched_flow_feature"], row["outcome"]
        grouped.setdefault(key, set()).add(
            (row["paired_count"], row["paired_row_sha256"])
        )
    assert all(len(v) == 1 for v in grouped.values())


def test_public_archive_rate_limit_stops_without_retry(tmp_path, monkeypatch):
    class Response:
        status_code = 429

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    calls = []

    def get(*args, **kwargs):
        calls.append(args)
        return Response()

    monkeypatch.setattr(f.requests.Session, "get", get)
    cache = f.Cache(tmp_path, True)
    url = f.official_url(f.key_for("BTCUSDT", "2024-01"))
    with pytest.raises(RuntimeError, match="RATE_LIMIT_STOP"):
        cache.get(url, 1000)
    with pytest.raises(RuntimeError, match="DOWNLOAD_STOPPED"):
        cache.get(url, 1000)
    assert len(calls) == 1
