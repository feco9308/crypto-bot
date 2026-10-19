"""Deterministic offline calculations; no replay or production mutation."""

import ast
import copy
import hashlib
import json
import math
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from crypto_bot.data.base import Candle
from crypto_bot.replay.universe import CandleHistory
from research import cross_sectional_market_structure as m
from research import institutional_signal_edge as edge


def history(count=300):
    return CandleHistory(
        [
            (
                Candle(
                    i * m.HOUR,
                    (i + 1) * m.HOUR - 1,
                    100 + i,
                    102 + i,
                    99 + i,
                    101 + i,
                    1,
                ),
                100,
            )
            for i in range(count)
        ]
    )


def cohort(n=20):
    table = m.Table()
    for i in range(n):
        row = dict(
            symbol=f"COIN{i}",
            timestamp_ms=0,
            top20_rank=i + 1,
            reference=100 + i,
            ema50=105,
            ema200=100,
            ret_4h=i - 10,
            ret_24h=i,
            **{key: i for key in m.OUTCOMES},
        )
        table.add(row)
    return table


def test_average_rank_ties_and_order():
    assert m.percentile([2, 1, 2, 3]) == [0.5, 0, 0.5, 1]
    assert m.percentile([2, 2]) == [0.5, 0.5]
    assert m.percentile([1]) == [0.5]
    assert math.isnan(m.percentile([float("nan")])[0])
    values = [4, 2, 1, 2]
    assert m.percentile(values)[::-1] == m.percentile(values[::-1])


def test_deterministic_deciles_ties_not_split():
    assert m.deciles(list(range(20))) == [i // 2 + 1 for i in range(20)]
    assert m.deciles([1] * 20) == [6] * 20
    assert m.deciles([float("nan")]) == [0]


@pytest.mark.parametrize("hours", [4, 24])
def test_closed_candle_return_no_lookahead(hours):
    h = history()
    t = 30 * m.HOUR
    before = m.return_feature(h, t, hours)
    assert before[0] == pytest.approx((130 / (130 - hours) - 1) * 100)
    assert edge.ms(before[1]) < t
    for col in h.columns[2:]:
        for i in range(30, len(h)):
            col[i] *= 10000
    assert m.return_feature(h, t, hours) == before


@pytest.mark.parametrize("kind", ["missing", "stale", "unclosed"])
def test_return_gap_stale_or_unclosed_unavailable(kind):
    h = history(40)
    if kind == "missing":
        h.columns[0][10] += 1
    elif kind == "stale":
        h.columns[0][29] -= 1
    else:
        h.columns[1][29] = 30 * m.HOUR
    assert math.isnan(m.return_feature(h, 30 * m.HOUR, 24)[0])


def test_btc_return_independent_of_future_and_coin():
    btc = history()
    coin = history()
    stamp = 30 * m.HOUR
    excess = m.return_feature(coin, stamp, 4)[0] - m.return_feature(btc, stamp, 4)[0]
    btc.columns[5][30] = 9999999
    assert (
        excess
        == m.return_feature(coin, stamp, 4)[0] - m.return_feature(btc, stamp, 4)[0]
    )
    assert math.isnan(m.return_feature(history(1), stamp, 4)[0])


def test_breadth_exact_membership_values_and_common_context():
    t = cohort()
    market = m.cohort_features(t)
    assert market.size == 1
    assert market.numeric["breadth_positive_4h"][0] == 9 / 20
    assert market.numeric["breadth_positive_24h"][0] == 19 / 20
    assert market.numeric["breadth_above_ema50"][0] == 14 / 20
    assert market.numeric["breadth_bullish_ema"][0] == 1
    assert market.numeric["top20_median_return_4h"][0] == -0.5
    assert market.numeric["top20_return_dispersion_24h"][0] == pytest.approx(
        math.sqrt(33.25)
    )
    for feature in m.BREADTH:
        assert len(set(t.numeric[feature])) == 1
    assert t.numeric["cs_rank_return_4h_pct"][0] == 0
    assert t.numeric["cs_rank_return_4h_pct"][-1] == 1
    assert t.numeric["cs_excess_vs_top20_median_4h"][0] == -9.5
    assert market.numeric[m.OUTCOMES[0]][0] == 9.5


@pytest.mark.parametrize("n", [19, 21])
def test_held_only_or_missing_member_rejected(n):
    with pytest.raises(ValueError, match="Top20"):
        m.cohort_features(cohort(n))


def test_duplicate_symbol_or_bad_rank_rejected():
    t = cohort()
    t.text["symbol"][1] = t.text["symbol"][0]
    with pytest.raises(ValueError):
        m.cohort_features(t)
    t = cohort()
    t.numeric["top20_rank"][1] = 1
    with pytest.raises(ValueError):
        m.cohort_features(t)


def test_partial_cohort_is_not_silently_rescaled():
    t = cohort()
    t.numeric["ret_4h"][0] = float("nan")
    market = m.cohort_features(t)
    assert math.isnan(market.numeric["breadth_positive_4h"][0])
    assert all(math.isnan(v) for v in t.numeric["cs_rank_return_4h_pct"])
    assert math.isfinite(market.numeric["breadth_positive_24h"][0])


def test_market_outcome_requires_full_fixed_membership():
    t = cohort()
    t.numeric[m.OUTCOMES[0]][0] = float("nan")
    market = m.cohort_features(t)
    assert math.isnan(market.numeric[m.OUTCOMES[0]][0])
    assert market.numeric[m.OUTCOMES[0] + "_member_coverage"][0] == 0.95


def test_primary_future_only_correct_windows_and_atr():
    h = history()
    t = 30 * m.HOUR
    result = m.primary_labels(h, t, 300 * m.HOUR, 130, 2)
    key = m.LABEL_PREFIX
    assert result[key + "status_24h"] == "COMPLETE"
    assert result[key + "close_return_pct_24h"] == pytest.approx((154 / 130 - 1) * 100)
    assert result[key + "mfe_atr_24h"] == 12.5
    assert result[key + "mae_atr_24h"] == -0.5
    # Decision feature candle and bars strictly outside future window do not leak.
    h.columns[3][29] = 1e9
    h.columns[3][198] = 1e9
    assert m.primary_labels(h, t, 300 * m.HOUR, 130, 2) == result


def test_horizon_at_end_complete_beyond_end_censored():
    key = m.LABEL_PREFIX
    result = m.primary_labels(history(), 30 * m.HOUR, 54 * m.HOUR, 130, 2)
    assert result[key + "status_24h"] == "COMPLETE"
    assert result[key + "status_72h"] == "RUN_END_CENSORED"
    assert math.isnan(result[key + "mfe_atr_72h"])
    assert math.isnan(result[key + "close_return_pct_72h"])


def test_missing_hour_no_shortened_label_or_synthetic_exit():
    h = history()
    h.columns[0][50] += 1
    result = m.primary_labels(h, 30 * m.HOUR, 300 * m.HOUR, 130, 2)
    assert result[m.LABEL_PREFIX + "status_24h"] == "MISSING_HOUR_CENSORED"
    assert math.isnan(result[m.LABEL_PREFIX + "mfe_atr_24h"])
    assert not any("exit" in k or "fill" in k for k in result)


def test_incomplete_future_candle_censors():
    h = history()
    h.columns[1][53] = 54 * m.HOUR
    result = m.primary_labels(h, 30 * m.HOUR, 300 * m.HOUR, 130, 2)
    assert result[m.LABEL_PREFIX + "status_24h"] == "MISSING_HOUR_CENSORED"


def test_atr_is_fixed_decision_input_and_invalid_only_removes_atr_labels():
    h = history()
    a = m.primary_labels(h, 30 * m.HOUR, 300 * m.HOUR, 130, 2)
    b = m.primary_labels(h, 30 * m.HOUR, 300 * m.HOUR, 130, 4)
    assert a[m.LABEL_PREFIX + "mfe_atr_72h"] == 2 * b[m.LABEL_PREFIX + "mfe_atr_72h"]
    c = m.primary_labels(h, 30 * m.HOUR, 300 * m.HOUR, 130, float("nan"))
    assert math.isnan(c[m.LABEL_PREFIX + "mfe_atr_24h"])
    assert math.isfinite(c[m.LABEL_PREFIX + "close_return_pct_24h"])


def test_primary_labels_do_not_use_1m_secondary():
    before = m.primary_labels(history(), 30 * m.HOUR, 300 * m.HOUR, 130, 2)
    # There is no minute archive argument or imported minute label in calculation.
    assert all(k.startswith("POST_HOC_FUTURE_LABEL_1H.") for k in before)
    assert m.bool_number("") != m.bool_number("")  # UNKNOWN is nan, not false
    with pytest.raises(ValueError):
        m.bool_number("UNKNOWN_AS_FALSE")


def test_symbol_day_episode_scopes_use_frozen_previous_flags():
    t = m.Table()
    for daily, buy, ep in [(1, 1, 1), (0, 1, 0), (1, 0, 1), (0, 1, 1)]:
        t.add(
            dict(
                symbol_day_first=daily, current_buy_eligible=buy, setup_episode_first=ep
            )
        )
    scopes = m.scopes(t)
    assert scopes["ALL_TOP20"] == [0, 1, 2, 3]
    assert scopes["BUY_SYMBOL_DAY_FIRST"] == [0]
    assert scopes["BUY_SETUP_EPISODE_FIRST"] == [0, 3]


def test_read_only_db_rejects_all_writes_and_no_replay_created(tmp_path):
    path = tmp_path / "test.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE replay_runs (id TEXT)")
        db.execute("INSERT INTO replay_runs VALUES ('old')")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with edge.read_only(path) as db:
        assert db.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO replay_runs VALUES ('new')")
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM replay_runs")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_source_fingerprint_detects_changes(tmp_path):
    path = tmp_path / "test.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE replay_runs (id TEXT, data TEXT)")
        db.execute("INSERT INTO replay_runs VALUES ('old', 'a')")
    with edge.read_only(path) as db:
        before = edge.source_fingerprint(db, ["old"])
    with edge.read_only(path) as db:
        assert edge.source_fingerprint(db, ["old"]) == before
    with sqlite3.connect(path) as db:
        db.execute("UPDATE replay_runs SET data='changed'")
    with edge.read_only(path) as db:
        assert edge.source_fingerprint(db, ["old"]) != before


@pytest.mark.parametrize(
    "event",
    [
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.gethostbyname",
    ],
)
def test_no_network_audit_hook(event):
    with pytest.raises(RuntimeError, match="NETWORK_FORBIDDEN"):
        edge.no_network(event, ())


def test_actual_network_hook_blocks_before_network_io():
    code = "import sys,socket; from research.institutional_signal_edge import no_network; sys.addaudithook(no_network); socket.getaddrinfo('localhost',80)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "NETWORK_FORBIDDEN" in result.stderr


def test_output_and_zip_deterministic_and_manifest_verified(tmp_path):
    t = cohort()
    t.write(tmp_path / "dataset.csv")
    before = (tmp_path / "dataset.csv").read_bytes()
    t.write(tmp_path / "dataset.csv")
    assert (tmp_path / "dataset.csv").read_bytes() == before
    (tmp_path / "summary.json").write_bytes(edge.encoded({"a": 1}))
    first = m.package(tmp_path).read_bytes()
    assert m.package(tmp_path).read_bytes() == first
    with zipfile.ZipFile(m.package(tmp_path)) as bundle:
        assert bundle.namelist() == sorted(bundle.namelist())
        manifest = json.loads(bundle.read("manifest.json"))
        for name, meta in manifest["files"].items():
            assert hashlib.sha256(bundle.read(name)).hexdigest() == meta["sha256"]
            assert len(bundle.read(name)) == meta["bytes"]


def test_monotonic_curve_reports_not_perfect_requirement():
    assert m.curve_summary([1, 2, 3, 4, 5, 4.9, 7, 8, 9, 10])["monotonic"]
    assert m.curve_summary([1] * 10)["monotonic"] is False
    assert m.curve_summary([None] * 10)["decile_spearman"] is None


def test_stable_screen_cannot_be_created_by_secondary_or_raw_only():
    assert not m.consistent_feature(m.STRENGTH[0], [], [])["stable"]
    raw = [
        dict(
            feature=m.STRENGTH[0],
            year=y,
            population="ALL_TOP20",
            outcome=m.LABEL_PREFIX + f"mfe_atr_{h}h",
            cliffs_delta=0.5,
            spearman=0.5,
            top_quintile_observed=100,
            bottom_quintile_observed=100,
        )
        for y in (2024, 2025)
        for h in (72, 168)
    ]
    assert not m.consistent_feature(m.STRENGTH[0], raw, [])["stable"]


def test_breadth_needs_authoritative_market_sensitivity():
    stats = [
        dict(
            feature=m.BREADTH[0],
            year=y,
            population=p,
            outcome=m.LABEL_PREFIX + f"mfe_atr_{h}h",
            cliffs_delta=0.3,
            spearman=0.2,
            top_quintile_observed=100,
            bottom_quintile_observed=100,
        )
        for y in (2024, 2025)
        for p in (
            "ALL_TOP20",
            "SYMBOL_DAY_FIRST",
            "BUY_ELIGIBLE",
            "BUY_SETUP_EPISODE_FIRST",
        )
        for h in (72, 168)
    ]
    assert not m.consistent_feature(m.BREADTH[0], stats, [])["stable"]
    for y in (2024, 2025):
        for p in ("ONE_ROW_PER_TIMESTAMP", "ONE_MARKET_ROW_PER_DAY"):
            for h in (72, 168):
                stats.append(
                    stats[0]
                    | dict(
                        year=y, population=p, outcome=m.LABEL_PREFIX + f"mfe_atr_{h}h"
                    )
                )
    assert m.consistent_feature(m.BREADTH[0], stats, [])["stable"]
    copy_stats = copy.deepcopy(stats)
    copy_stats[0]["cliffs_delta"] = -0.3
    assert not m.consistent_feature(m.BREADTH[0], copy_stats, [])["stable"]


def test_no_runtime_engine_or_optimization_import_or_call():
    source = Path(m.__file__).read_text()
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {
        "ReplayEngine",
        "RiskManager",
        "PortfolioService",
        "ReplayRepository",
        "requests",
        "grid_search",
        "GridSearchCV",
        "fit",
    }
    assert "INSERT INTO" not in source
    assert "UPDATE replay" not in source
    assert "2026-" not in m.methodology()["decisions"]
    assert len(m.STRENGTH) == 6
    assert len(m.BREADTH) == 7
    assert m.HORIZONS == (24, 72, 168)


def test_safety_metadata_and_feature_family_frozen():
    methods = m.methodology()
    assert methods["safety"]["database_writes"] == 0
    assert methods["safety"]["new_replay"] is False
    assert methods["frozen_screen"]["minimum_abs_cliff"] == 0.147
    assert methods["feature_families"]["current_controls"] == (
        "market_score",
        "delta_4h",
        "relative_volume",
    )
