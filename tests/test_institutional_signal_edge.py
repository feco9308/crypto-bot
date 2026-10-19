"""Deterministic offline research tests; no replay worker or exchange clients."""

import csv
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest

from crypto_bot.data.base import Candle
from crypto_bot.replay.institutional import InstitutionalContext
from crypto_bot.replay.universe import CandleHistory
from research import institutional_signal_edge as edge


def history(bars):
    return CandleHistory(
        [
            (Candle(t, t + edge.MINUTE - 1, o, hi, lo, c, 1), 100)
            for t, o, hi, lo, c in bars
        ]
    )


def label(bars, end=None):
    end = (
        end if end is not None else (bars[-1][0] + edge.MINUTE if bars else edge.MINUTE)
    )
    return edge.FutureLabels(history(bars), end).get(0, 100.0, 98.0)


def test_database_mode_ro_and_query_only(tmp_path):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE source(value)")
    before = path.read_bytes()
    with edge.read_only(path) as db:
        assert db.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO source VALUES(1)")
        db.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO source VALUES(1)")
    assert path.read_bytes() == before


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError), edge.read_only(path):
        pass
    assert not path.exists()


def test_fingerprint_includes_source_rows_and_run_inventory(tmp_path):
    path = tmp_path / "test.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE replay_runs(id,status)")
        db.execute("CREATE TABLE replay_trades(id,run_id,data)")
        db.execute("INSERT INTO replay_runs VALUES('r','COMPLETED')")
        db.execute("INSERT INTO replay_trades VALUES(1,'r','original')")
    with edge.read_only(path) as db:
        a = edge.source_fingerprint(db, ["r"])
        assert a == edge.source_fingerprint(db, ["r"])
    with sqlite3.connect(path) as db:
        db.execute("UPDATE replay_trades SET data='changed'")
    with edge.read_only(path) as db:
        assert edge.source_fingerprint(db, ["r"]) != a


def test_no_network_enforced_in_subprocess():
    code = "import sys,socket; from research.institutional_signal_edge import no_network; sys.addaudithook(no_network); socket.create_connection(('127.0.0.1',9))"
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert p.returncode != 0
    assert "NETWORK_FORBIDDEN_OFFLINE_RESEARCH" in p.stderr


def test_network_hook_denies_dns_and_connect_without_request():
    for event in (
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.gethostbyname",
    ):
        with pytest.raises(RuntimeError, match="NETWORK_FORBIDDEN"):
            edge.no_network(event, ())


def archive(tmp_path, contents, sha=None):
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as z:
        z.writestr("data.csv", contents)
    raw = blob.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    (tmp_path / "objects").mkdir(exist_ok=True)
    (tmp_path / "objects" / (digest + ".zip")).write_bytes(raw)
    key = "data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-01.zip"
    return edge.Archives(tmp_path, {key: sha or digest}), key, digest


def test_pinned_archive_only_and_hash_validated(tmp_path):
    provider, key, sha = archive(
        tmp_path, "1704067200000,100,102,99,101,1,1704067259999,100\n"
    )
    h = provider.history("BTCUSDT", "1m", 1704067200000, 1704067260000)
    assert len(h) == 1 and h[0][0].close == 101
    assert provider.used == {key: sha}
    with pytest.raises(ValueError, match="UNPINNED"):
        provider.object(key + "unlisted", sha)
    (tmp_path / "objects" / (sha + ".zip")).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA mismatch"):
        provider.object(key, sha)


def test_missing_pin_never_reads_unpinned_available_archive(tmp_path):
    provider, key, _ = archive(
        tmp_path, "1704067200000,100,102,99,101,1,1704067259999,100\n"
    )
    missing = edge.Archives(tmp_path, {key: None})
    assert len(missing.history("BTCUSDT", "1m", 1704067200000, 1704067260000)) == 0
    assert missing.used[key] is None


def test_future_label_separate_and_conservative_same_minute():
    result = label([(0, 100, 110, 97, 101)])
    assert result["label_type"] == "POST_HOC_FUTURE_LABEL"
    assert result["hit_1r_before_stop"] is False
    assert result["stop_before_1r"] is True
    assert result["time_to_stop_hours"] == 1 / 60


def test_gap_up_target_is_known_before_later_stop():
    result = label([(0, 104, 110, 97, 101)])
    assert result["hit_1r_before_stop"] is True
    assert result["hit_2r_before_stop"] is True
    assert result["hit_5r_before_stop"] is False
    assert result["time_to_1r_hours"] == 0
    assert result["stop_before_1r"] is False


def test_gap_down_stops_at_open_not_later_high():
    result = label([(0, 97, 130, 96, 100)])
    assert result["hit_1r_before_stop"] is False
    assert result["time_to_stop_hours"] == 0


def test_missing_first_minute_unknown_not_false():
    result = label([(edge.MINUTE, 100, 150, 90, 100)])
    assert result["hit_1r_before_stop"] is None
    assert result["stop_before_1r"] is None
    assert result["censor_reason"] == "MISSING_MINUTE_CENSORED"


def test_missing_middle_minute_never_crosses_gap():
    result = label([(0, 100, 101, 99, 100), (2 * edge.MINUTE, 100, 150, 90, 100)])
    assert result["hit_1r_before_stop"] is None
    assert result["stop_before_1r"] is None
    assert result["horizon_6h_status"] == "RUN_END_CENSORED"


def test_known_hit_preserved_but_unreached_barrier_unknown_on_gap():
    result = label([(0, 100, 104, 99, 102), (2 * edge.MINUTE, 100, 150, 90, 100)])
    assert result["hit_1r_before_stop"] is True
    assert result["hit_2r_before_stop"] is True
    assert result["hit_5r_before_stop"] is None
    assert result["stop_before_1r"] is False


def test_end_censored_no_fabricated_stop_or_fill():
    result = label([(0, 100, 101, 99, 100)])
    assert result["hit_1r_before_stop"] is None
    assert result["stop_time"] is None
    assert result["censor_reason"] == "RUN_END_CENSORED"
    assert not any("fill" in k or "order" in k for k in result)


def test_horizon_extrema_and_mae_sign():
    bars = [(i * edge.MINUTE, 100, 104, 99, 100) for i in range(360)]
    result = label(bars)
    assert result["mfe_6h_r"] == 2
    assert result["mae_6h_r"] == -0.5
    assert result["horizon_6h_status"] == "OBSERVED"
    assert result["mfe_24h_r"] is None


def test_extrema_and_first_crossing_match_brute_force():
    values = [4, 1, 9, 3, 5, 0, 10]
    for maximum in (True, False):
        tree = edge.Extrema(values, maximum)
        for lo in range(len(values)):
            for hi in range(lo + 1, len(values) + 1):
                assert tree.query(lo, hi) == (max if maximum else min)(values[lo:hi])
                for threshold in range(11):
                    expected = next(
                        (
                            i
                            for i in range(lo, hi)
                            if (
                                values[i] >= threshold
                                if maximum
                                else values[i] <= threshold
                            )
                        ),
                        None,
                    )
                    assert tree.first(lo, hi, threshold) == expected


def test_future_high_cannot_change_earlier_barrier_outcome():
    earlier = [(0, 100, 101, 97, 99)]
    a = label(earlier)
    b = label(earlier + [(edge.MINUTE, 100, 500, 99, 101)])
    for k in (
        "hit_1r_before_stop",
        "hit_5r_before_stop",
        "stop_before_1r",
        "stop_time",
    ):
        assert a[k] == b[k]


def test_closed_htf_only_no_lookahead():
    candles = [
        (
            Candle(
                i * edge.HOUR,
                (i + 1) * edge.HOUR - 1,
                100 + i,
                101 + i,
                99 + i,
                100 + i,
                1,
            ),
            100,
        )
        for i in range(80)
    ]
    now = 40 * edge.HOUR
    context = InstitutionalContext({"BTCUSDT": CandleHistory(candles)})
    original = context.trend("BTCUSDT", 4, now, 2, 3)
    changed = candles[:40] + [
        (Candle(c.open_time, c.close_time, 10000, 10001, 9999, 10000, 1), v)
        for c, v in candles[40:]
    ]
    alternative = InstitutionalContext({"BTCUSDT": CandleHistory(changed)})
    assert alternative.trend("BTCUSDT", 4, now, 2, 3) == original
    assert edge.ms(original["source_timestamp"]) < now
    # At partial 4h bucket the latest source is the prior complete bucket.
    partial = context.trend("BTCUSDT", 4, now + edge.HOUR, 2, 3)
    assert partial["source_timestamp"] == original["source_timestamp"]


def test_authoritative_candidate_population_2024_audit_parity():
    events = [
        dict(
            event="ENTRY_CANDIDATE_RANKED",
            timestamp="2024-01-01T00:00:00Z",
            symbol=s,
            original_strategy_decision=dict(action="BUY"),
        )
        for s in ("AUSDT", "BUSDT")
    ]
    source = dict(
        year=2024,
        events=events + [dict(event="STRATEGY_SIGNAL", action="BUY")],
        event_counts={"ENTRY_CANDIDATE_RANKED": 2},
    )
    assert edge.candidate_events(source) == events
    source["events"].append(events[0])
    source["event_counts"]["ENTRY_CANDIDATE_RANKED"] = 3
    with pytest.raises(ValueError, match="Duplicate"):
        edge.candidate_events(source)


def test_unranked_source_includes_pre_risk_rejections_no_fill_but_not_sell():
    events = [
        dict(event=kind, timestamp=f"2025-02-01T0{i}:00:00Z", symbol="BTCUSDT", **extra)
        for i, (kind, extra) in enumerate(
            [
                ("STRATEGY_SIGNAL", dict(action="BUY")),
                ("RISK_REJECTED", dict(reasons=["CORRELATION_LIMIT"])),
                ("NO_FILL", dict(action="BUY")),
                ("STRATEGY_SIGNAL", dict(action="SELL")),
            ]
        )
    ]
    assert len(edge.candidate_events(dict(year=2025, events=events))) == 3


def test_deterministic_episode_grouping_gap_breaks_episode():
    rows = [
        dict(year=2024, symbol="BTCUSDT", timestamp=edge.iso(i * edge.HOUR))
        for i in (0, 1, 3, 4)
    ]
    a = edge.episodes(deepcopy(rows))
    b = edge.episodes(list(reversed(deepcopy(rows))))
    assert {r["timestamp"]: r for r in a} == {r["timestamp"]: r for r in b}
    assert [r["setup_episode_ordinal"] for r in a] == [1, 2, 1, 2]
    assert a[0]["setup_episode_id"] == a[1]["setup_episode_id"]
    assert a[1]["setup_episode_id"] != a[2]["setup_episode_id"]


def test_spearman_auc_cliff_ties_and_constant():
    assert edge.spearman([1, 2, 2, 3], [2, 4, 4, 6]) == pytest.approx(1)
    assert edge.spearman([1, 1, 1], [1, 2, 3]) is None
    assert edge.auc([2, 3], [1, 2]) == 0.875
    assert edge.auc([1], [1]) == 0.5
    assert edge.auc([], [1]) is None


def test_decile_ties_deterministic_no_outcome_threshold_search():
    rows = [
        dict(
            symbol="BTCUSDT",
            symbol_day_id="d",
            setup_episode_id="e",
            market_score=70,
            POST_HOC_FUTURE_LABEL=dict(hit_5r_before_stop=bool(i % 2)),
            ACTUAL_TRADE_LABEL=dict(actual_trade=False),
        )
        for i in range(20)
    ]
    original = deepcopy(rows)
    result = edge.deciles(rows, "market_score")
    assert len(result) == 10
    assert result[0]["candidate_count"] == 20
    assert sum(r["candidate_count"] for r in result[1:]) == 0
    assert rows == original
    for r in rows:
        r["POST_HOC_FUTURE_LABEL"]["hit_5r_before_stop"] = not r[
            "POST_HOC_FUTURE_LABEL"
        ]["hit_5r_before_stop"]
    assert [r["lower_quantile_edge"] for r in edge.deciles(rows, "market_score")] == [
        r["lower_quantile_edge"] for r in result
    ]


def test_deterministic_encoding_and_future_feature_separation():
    row = dict(
        timestamp="2024-01-01T00:00:00Z",
        market_score=80,
        institutional_context={},
        POST_HOC_FUTURE_LABEL=dict(label_type="POST_HOC_FUTURE_LABEL", mfe_72h_r=10),
        ACTUAL_TRADE_LABEL=dict(actual_trade=True, realized_r=2),
    )
    flat = edge.flatten_candidate(row)
    assert "POST_HOC_FUTURE_LABEL.mfe_72h_r" in flat
    assert "ACTUAL_TRADE_LABEL.realized_r" in flat
    assert not any("POST_HOC" in k or "realized" in k for k in edge.FEATURES)
    assert edge.csv_bytes([flat]) == edge.csv_bytes(
        [dict(reversed(list(flat.items())))]
    )
    assert edge.encoded(row) == edge.encoded(dict(reversed(list(row.items()))))


def test_portfolio_descriptive_context_excludes_same_timestamp_fills():
    event = dict(timestamp="2024-01-01T00:00:00Z", symbol="BTCUSDT")
    fill = event | dict(
        event="ENTRY_FILL", position_id=1, quantity="1", fill_price="100", fee=".1"
    )
    after = dict(timestamp="2024-01-01T01:00:00Z", symbol="ETHUSDT")
    state = edge.portfolio_before([event, after], [fill], 1000)
    assert state[(event["symbol"], event["timestamp"])] == (1000, [])
    cash, positions = state[(after["symbol"], after["timestamp"])]
    assert cash == 899.9
    assert [p.symbol for p in positions] == ["BTCUSDT"]


def test_research_not_imported_by_runtime():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "crypto_bot").rglob("*.py"):
        assert "institutional_signal_edge" not in path.read_text()


def test_institutional_public_snapshot_decimals_without_env():
    original = edge.InstitutionalSettings()
    snapshot = original.public_dict()
    restored = edge.institutional_settings(snapshot)
    assert restored == original
    assert restored.btc_neutral_risk_multiplier == edge.Decimal(".5")
    assert snapshot == original.public_dict()


def test_previous_minute_exit_at_decision_timestamp_already_observable():
    start = "2024-01-01T00:00:00Z"
    now = "2024-01-01T01:00:00Z"
    entry = dict(
        event="ENTRY_FILL",
        timestamp=start,
        symbol="BTCUSDT",
        position_id=1,
        quantity="1",
        fill_price="100",
        fee=".1",
    )
    exit_fill = dict(
        event="EXIT_FILL",
        timestamp=now,
        symbol="BTCUSDT",
        position_id=1,
        quantity="1",
        fill_price="102",
        fee=".1",
        candle_close_time=now,
    )
    state = edge.portfolio_before(
        [dict(symbol="ETHUSDT", timestamp=now)], [entry, exit_fill], 1000
    )
    cash, positions = state[("ETHUSDT", now)]
    assert positions == []
    assert cash == pytest.approx(1001.8)


def analysis_rows():
    rows = []
    for i in range(30):
        r = dict(
            year=2024,
            symbol="FETUSDT",
            timestamp=edge.iso(i * 2 * edge.HOUR),
            institutional_context={},
            btc_regime="BULL",
            POST_HOC_FUTURE_LABEL=dict(
                label_type="POST_HOC_FUTURE_LABEL",
                hit_5r_before_stop=i % 2 == 0,
                stop_before_1r=i % 2 == 1,
                mfe_72h_r=float(i),
                mfe_168h_r=float(i),
            ),
            ACTUAL_TRADE_LABEL=dict(actual_trade=True, realized_r=float(i)),
        )
        r.update({key: float(i) for key in edge.FEATURES})
        rows.append(r)
    return edge.episodes(rows)


def test_full_analysis_winner_anatomy_and_group_iqrs(tmp_path):
    rows = analysis_rows()
    original = deepcopy(rows)
    result = edge.analyze({2024: rows}, tmp_path)
    assert result["base_rates"]["2024"]["raw_hourly"]["candidate_count"] == 30
    f = edge.feature_stats(rows, "market_score")
    assert f["positive_q1"] == edge.quantile(list(range(0, 30, 2)), 0.25)
    assert f["positive_missing_rate"] == 0
    anatomy = list(csv.DictReader((tmp_path / "winner-anatomy.csv").open()))
    assert anatomy
    assert "market_score_negative_control_percentile" in anatomy[0]
    assert rows == original


def test_deterministic_zip_and_read_only_packaging(tmp_path, monkeypatch):
    (tmp_path / "report.md").write_text("same research\n")
    (tmp_path / "summary.json").write_text("{}\n")

    def fail(*args, **kwargs):
        raise AssertionError("Packaging must not access DB")

    monkeypatch.setattr(edge, "read_only", fail)
    bundle = edge.package_artifacts(tmp_path)
    first = bundle.read_bytes()
    assert edge.package_artifacts(tmp_path).read_bytes() == first
    with zipfile.ZipFile(bundle) as z:
        assert z.testzip() is None
        assert z.namelist() == sorted(z.namelist())
        manifest = json.loads(z.read("manifest.json"))
        for name, details in manifest["files"].items():
            assert hashlib.sha256(z.read(name)).hexdigest() == details["sha256"]


def test_numerical_constant_spread_does_not_become_pseudo_signal():
    rows = analysis_rows()
    for i, r in enumerate(rows):
        r["spread"] = 0.02 + (i % 3) * 1e-15
    result = edge.feature_stats(rows, "spread")
    assert result["numerical_constant"] is True
    assert result["auc_5r_vs_stop"] is None
    assert result["spearman_72h_mfe"] is None
    assert edge.deciles(rows, "spread")[0]["candidate_count"] == len(rows)


def test_score_monotonicity_reports_mixed_deciles_without_search():
    rows = analysis_rows()
    deciles = [
        dict(year=2024, scope="raw_hourly", **r)
        for r in edge.deciles(rows, "market_score")
    ]
    result = edge.score_monotonicity(deciles)
    r = next(r for r in result if r["measure"] == "hit_5r_before_stop")
    assert r["increasing_steps"] > 0 and r["decreasing_steps"] > 0
    assert r["nondecreasing"] is False and r["nonincreasing"] is False


def test_pre_risk_rejection_reason_is_not_invented_strategy_reason():
    event = dict(event="RISK_REJECTED", reasons=["CORRELATION_LIMIT"])
    result = edge.original_buy_record(event, 100, 98)
    assert result["action"] == "BUY"
    assert result["reasons"] is None
    assert result["pre_execution_rejection_reasons"] == ["CORRELATION_LIMIT"]
    audited = dict(
        original_strategy_decision=dict(action="BUY", reasons=["MULTITF_BULLISH"])
    )
    assert (
        edge.original_buy_record(audited, 100, 98)
        == audited["original_strategy_decision"]
    )
