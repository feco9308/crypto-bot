"""Offline eligibility diagnostic parity and safety; no production DB dependency."""

import hashlib
import json
import sqlite3
import subprocess
import sys
import zipfile
from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from crypto_bot.config.settings import Settings
from crypto_bot.data.base import Candle
from crypto_bot.indicators.technical import features
from crypto_bot.replay.institutional import (
    InstitutionalContext,
    InstitutionalSettings,
    InstitutionalStrategy,
)
from crypto_bot.replay.market_context import frame
from crypto_bot.replay.universe import CandleHistory, HistoricalUniverse
from crypto_bot.scanner.scoring import score
from crypto_bot.trading.domain import D, MarketContext
from research import institutional_eligibility_gate as gate
from research import institutional_signal_edge as edge


def context():
    trends = {
        k: dict(
            ready=True,
            fresh=True,
            observations=499,
            source_timestamp=edge.iso(0),
            ema_fast=100.0,
            ema_slow=90.0,
        )
        for k in ["1h", "4h", "1d"]
    }
    audit = dict(
        coin_trends=trends,
        btc=dict(regime="BULL", risk_multiplier="1", trends={}),
        correlation=dict(enabled=True, blocked=False, pairs=[]),
    )
    return MarketContext(
        1,
        "BTCUSDT",
        edge.at(edge.HOUR),
        D(100),
        D(80),
        D(5),
        D(55),
        D(100),
        D(90),
        D(1),
        D("1.5"),
        "WATCH",
        True,
        dict(spread=0.02, institutional=audit),
    )


def observed(c, s=None):
    s = s or InstitutionalSettings()
    signal = InstitutionalStrategy(s).evaluate(c, False)
    return gate.predicates(c, s, signal)


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("score", "69.999", "SCORE"),
        ("score", "70", None),
        ("delta", "0", "DELTA"),
        ("delta", ".001", None),
        ("delta", None, "DELTA"),
        ("rsi", "39.999", "RSI"),
        ("rsi", "40", None),
        ("rsi", "70", None),
        ("rsi", "70.001", "RSI"),
        ("relative_volume", ".999", "RELATIVE_VOLUME"),
        ("relative_volume", "1", None),
        ("atr", "0", "INITIAL_STOP"),
        ("atr", "51", "INITIAL_STOP"),
    ],
)
def test_production_numeric_boundary_predicate_parity(field, value, expected):
    c = replace(context(), **{field: D(value) if value is not None else None})
    result = observed(c)
    assert result["failure_gates"] == ([expected] if expected else [])
    assert result["final_asset_eligible"] == (expected is None)


@pytest.mark.parametrize("timeframe", ["1h", "4h", "1d"])
@pytest.mark.parametrize("unavailable", [False, True])
def test_exact_production_trend_reason_parity(timeframe, unavailable):
    c = context()
    c.features["institutional"]["coin_trends"][timeframe].update(
        ready=not unavailable, ema_fast=90.0
    )
    result = observed(c)
    assert result["failure_gates"] == [timeframe.upper() + "_TREND"]
    assert result["single_failure"] is True
    assert result["strategy_reasons"] == [
        f"MULTITF_{timeframe.upper()}_"
        + ("UNAVAILABLE" if unavailable else "NOT_BULLISH")
    ]


@pytest.mark.parametrize(
    "regime,multiplier,reason",
    [
        ("UNKNOWN", "0", "BTC_REGIME_UNAVAILABLE"),
        ("BEAR", "0", "BTC_BEAR_REGIME"),
        ("NEUTRAL", "0", "BTC_REGIME_RISK_ZERO"),
    ],
)
def test_btc_source_semantics(regime, multiplier, reason):
    c = context()
    c.features["institutional"]["btc"].update(regime=regime, risk_multiplier=multiplier)
    result = observed(c)
    assert result["failure_gates"] == ["BTC_REGIME"]
    assert result["strategy_reasons"] == [reason]


def test_missing_spread_is_not_invented_gate():
    c = context()
    c.features.pop("spread")
    result = observed(c)
    assert result["spread_pass"] and not result["spread_available"]
    c.features["spread"] = 0.201
    assert observed(c)["failure_gates"] == ["SPREAD"]


def test_single_rsi_failure_not_double_counted_and_multi_failure_separate():
    c = replace(context(), rsi=D(71))
    result = observed(c)
    assert result["failure_count"] == 1
    assert result["single_failure_gate"] == "RSI"
    assert result["single_failure_population"] == "ONLY_RSI_HIGH_FAIL"
    c = replace(c, score=D(69))
    result = observed(c)
    assert result["failure_count"] == 2
    assert result["single_failure"] is False
    assert result["single_failure_population"] is None
    assert (
        result["canonical_failure_combination"] == "RSI_OUTSIDE_RANGE|SCORE_BELOW_MIN"
    )


def test_delta_disabled_still_rejects_present_nonpositive_source_rule():
    settings = replace(InstitutionalSettings(), require_delta=False)
    assert observed(replace(context(), delta=None), settings)["delta_pass"] is True
    assert observed(replace(context(), delta=D(0)), settings)["delta_pass"] is False


def test_watch_gate_and_context_availability_are_distinct():
    c = replace(context(), algorithm_watch=False, watch_state="AUTO")
    result = observed(c)
    assert result["failure_gates"] == ["WATCH"]
    assert result["institutional_context_available"] is True
    assert "institutional_context_available" not in gate.PASS_FIELD.values()


def test_original_buy_and_hold_stop_expression_same_without_fabrication():
    s = InstitutionalSettings()
    c = context()
    signal = InstitutionalStrategy(s).evaluate(c, False)
    assert gate.reference_stop(c, s) == signal.stop_reference == D(98)
    assert gate.reference_stop(replace(c, score=D(10)), s) == D(98)
    assert gate.reference_stop(replace(c, atr=D(51)), s) is None


def hhistory(n=600, volume=1000000):
    return CandleHistory(
        [
            (
                Candle(
                    i * edge.HOUR,
                    (i + 1) * edge.HOUR - 1,
                    100 + i * 0.01,
                    101 + i * 0.01,
                    99 + i * 0.01,
                    100 + i * 0.01,
                    1000,
                ),
                volume,
            )
            for i in range(n)
        ]
    )


def test_canonical_context_identical_to_source_closed_htf_and_no_lookahead():
    histories = {s: hhistory() for s in ["BTCUSDT", "ETHUSDT"]}
    start = 192 * edge.HOUR
    s = replace(InstitutionalSettings(), ema_fast_period=2, ema_slow_period=3)
    canonical = gate.CanonicalContext(histories, start)
    direct = InstitutionalContext(histories, start)
    for hour in range(200, 225):
        now = hour * edge.HOUR
        got = canonical.context("ETHUSDT", now, s, [])
        assert got == direct.context("ETHUSDT", now, s, [])
        assert got["correlation"]["pairs"] == []
        for t in list(got["coin_trends"].values()) + list(
            got["btc"]["trends"].values()
        ):
            if t["source_timestamp"]:
                assert edge.ms(t["source_timestamp"]) < now
    changed = deepcopy(histories)
    for h in changed.values():
        for column in h.columns[2:6]:
            for i in range(225, len(column)):
                column[i] *= 10
    future = gate.CanonicalContext(changed, start)
    assert future.context("ETHUSDT", 224 * edge.HOUR, s, []) == canonical.context(
        "ETHUSDT", 224 * edge.HOUR, s, []
    )


def test_top20_deterministic_no_held_only_rows_or_future_market_input():
    settings = replace(Settings(), number_of_markets=20)
    histories = {f"COIN{i}USDT": hhistory(volume=(i + 1) * 1000000) for i in range(22)}
    histories["HELDUSDT"] = hhistory(volume=1)
    universe = HistoricalUniverse(histories, settings, 0.02)
    now = 450 * edge.HOUR
    markets = universe.ranked(now)
    assert len(markets) == 20 and all(m.symbol != "HELDUSDT" for m in markets)
    a = frame(
        universe, settings, now, defaultdict(list), score, features, markets=markets
    )
    assert all(
        r["auto_eligible"] and r["features"]["candle_time"] < now for r in a.rows()
    )
    changed = deepcopy(histories)
    for h in changed.values():
        for i in range(450, len(h)):
            h.columns[7][i] *= 100
    b = HistoricalUniverse(changed, settings, 0.02)
    assert [m.symbol for m in b.ranked(now)] == [m.symbol for m in markets]
    assert (
        a.rows_json
        == frame(
            universe, settings, now, defaultdict(list), score, features, markets=markets
        ).rows_json
    )


def test_known_buy_record_reproduced_without_portfolio():
    c = context()
    s = InstitutionalSettings()
    sig = InstitutionalStrategy(s).evaluate(c, False)
    row = dict(
        symbol=c.symbol,
        features=dict(
            price=100,
            atr=1,
            atr_pct=1,
            rsi=55,
            relative_volume=1.5,
            range_position=0.5,
            quote_volume=100000000,
            volatility=1,
            spread=0.02,
            momentum=1,
            candle_time=edge.HOUR - 1,
        ),
        momentum=dict(delta_1h=1, delta_4h=5, delta_24h=10),
        total_score=80,
        components=dict(trend=25),
        auto_eligible=True,
    )
    got = gate.row_record(
        row, {"BTCUSDT": "source watch"}, edge.HOUR, c, s, sig, 2024, 1, {}
    )
    assert got["final_asset_eligible"] and got["original_stop_reference"] == 98
    assert got["strategy_action_without_portfolio"] == "BUY"
    assert not any("POST_HOC" in k for k in got)
    assert got["row_scope"] == "TOP20_SHARED_ONLY"


def test_episode_grouping_state_change_and_gap_deterministic():
    state = {}
    rows = []
    for hour, eligible in [
        (0, False),
        (1, False),
        (2, True),
        (3, True),
        (4, False),
        (6, False),
    ]:
        rows.append(
            gate.episode(
                dict(
                    year=2024,
                    symbol="BTCUSDT",
                    timestamp=edge.iso(hour * edge.HOUR),
                    final_asset_eligible=eligible,
                ),
                state,
            )
        )
    assert [r["eligibility_episode_ordinal"] for r in rows] == [1, 2, 1, 2, 1, 1]
    assert len({r["eligibility_episode_id"] for r in rows}) == 4


def test_future_labels_are_prior_implementation_and_censored_not_false():
    history = CandleHistory([(Candle(60000, 119999, 100, 200, 90, 100, 1), 100)])
    result = edge.FutureLabels(history, 120000).get(0, 100, 98)
    assert result["label_type"] == "POST_HOC_FUTURE_LABEL"
    assert result["hit_5r_before_stop"] is None
    assert result["stop_before_1r"] is None
    history = CandleHistory([(Candle(0, 59999, 100, 120, 97, 100, 1), 100)])
    result = edge.FutureLabels(history, 60000).get(0, 100, 98)
    assert result["stop_before_1r"] is True and result["hit_5r_before_stop"] is False


def test_read_only_source_fingerprint_and_no_new_run(tmp_path):
    p = tmp_path / "source.db"
    with sqlite3.connect(p) as db:
        db.execute("create table replay_runs(id,status)")
        db.execute("insert into replay_runs values('r','COMPLETED')")
    raw = p.read_bytes()
    with edge.read_only(p) as db:
        before = edge.source_fingerprint(db, ["r"])
        with pytest.raises(sqlite3.OperationalError):
            db.execute("insert into replay_runs values('new','QUEUED')")
        assert db.execute("pragma query_only").fetchone() == (1,)
        assert edge.source_fingerprint(db, ["r"]) == before
    assert p.read_bytes() == raw


def test_no_network_diagnostic_process():
    code = "import sys,socket; from research.institutional_signal_edge import no_network; sys.addaudithook(no_network); socket.getaddrinfo('example.com',443)"
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert p.returncode and "NETWORK_FORBIDDEN_OFFLINE_RESEARCH" in p.stderr


def test_deterministic_zip_and_no_database_access(tmp_path, monkeypatch):
    (tmp_path / "summary.json").write_text("{}\n")
    (tmp_path / "report.md").write_text("fixed\n")

    def fail(*args, **kwargs):
        raise AssertionError("No source action from export")

    monkeypatch.setattr(sqlite3, "connect", fail)
    a = gate.package(tmp_path)
    raw = a.read_bytes()
    assert gate.package(tmp_path).read_bytes() == raw
    with zipfile.ZipFile(a) as z:
        assert z.testzip() is None
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["base"] == gate.BASE
        for name, item in manifest["files"].items():
            assert hashlib.sha256(z.read(name)).hexdigest() == item["sha256"]


def test_no_new_strategy_threshold_search_or_runtime_import():
    text = Path(gate.__file__).read_text()
    assert (
        "RiskManager(" not in text
        and "ReplayEngine(" not in text
        and "MemoryLedger(" not in text
    )
    assert gate.MIN_COUNT == 30 and gate.MIN_CLIFF == 0.147
    root = Path(__file__).resolve().parents[1]
    assert all(
        "institutional_eligibility_gate" not in p.read_text()
        for p in (root / "crypto_bot").rglob("*.py")
    )


def test_bag_unknown_denominators_and_gate_membership():
    row = dict(
        symbol="BTCUSDT",
        symbol_day_id="d",
        eligibility_episode_id="e",
        market_score=80,
        single_failure=True,
        single_failure_gate="RSI",
        single_failure_population="ONLY_RSI_HIGH_FAIL",
        final_asset_eligible=False,
        failure_count=1,
        strategy_reasons=["RSI_OUTSIDE_RANGE"],
        canonical_failure_combination="RSI_OUTSIDE_RANGE",
        POST_HOC_FUTURE_LABEL=dict(label_type="POST_HOC_FUTURE_LABEL"),
    )
    row.update({v: True for v in gate.PASS_FIELD.values()})
    row["rsi_range_pass"] = False
    bag = gate.Bag()
    bag.add(row)
    s = bag.summary()
    assert s["hit_5r_before_stop_unknown"] == 1 and s["hit_5r_before_stop_rate"] is None
    groups = gate.memberships(row)
    assert (
        "ONLY_RSI_FAIL" in groups
        and "ONLY_RSI_HIGH_FAIL" in groups
        and "MULTI_FAILURE" not in groups
    )


def synthetic_comparison(year, scope, harmful=False, adequate=True, balanced=True):
    return dict(
        year=year,
        scope=scope,
        gate="SCORE",
        availability="ALL",
        adequate_sample=adequate,
        balanced_coverage=balanced,
        rate_5r_difference=-0.1 if harmful else 0.1,
        stop_before_1r_difference=0.1 if harmful else -0.1,
        cliffs_delta_mfe72=-0.3 if harmful else 0.3,
        a_count=100,
        b_count=100,
        a={},
        b=dict(unavailable_single_failure_count=0),
    )


def synthetic_analyses(harmful_gates=("SCORE",), balanced=True):
    analyses = {}
    for year in (2024, 2025):
        entries = []
        for scope in ("raw_hourly", "episode_first"):
            for name in gate.GATES:
                row = synthetic_comparison(
                    year,
                    scope,
                    harmful=name in harmful_gates,
                    adequate=name in harmful_gates,
                    balanced=balanced,
                )
                row["gate"] = name
                entries.append(row)
        enrichment = [
            synthetic_comparison(year, s)
            | dict(b_population="ALL_TOP20_ELIGIBLE_DATA_ROWS")
            for s in ("raw_hourly", "episode_first")
        ]
        analyses[year] = dict(single_failures=entries, enrichment=enrichment)
    return analyses


def test_gate_nomination_requires_both_years_episode_support_and_balanced_coverage():
    a = synthetic_analyses()
    result = gate.conclusions(a)
    assert result["ablation_candidate_gate"] == "SCORE"
    assert (
        gate.conclusions(synthetic_analyses(balanced=False))["ablation_candidate_gate"]
        == "NONE"
    )
    a[2025]["single_failures"][0]["adequate_sample"] = False
    assert gate.conclusions(a)["ablation_candidate_gate"] == "NONE"


def test_multiple_harmful_gates_do_not_create_gate_soup():
    result = gate.conclusions(synthetic_analyses(("SCORE", "RELATIVE_VOLUME")))
    assert result["ablation_candidate_gate"] == "NONE"
    assert result["qualified_ablation_gates"] == ["SCORE", "RELATIVE_VOLUME"]


def test_episode_direction_reversal_prevents_gate_nomination():
    a = synthetic_analyses()
    row = next(
        r
        for r in a[2025]["single_failures"]
        if r["gate"] == "SCORE" and r["scope"] == "episode_first"
    )
    row.update(
        rate_5r_difference=0.1, stop_before_1r_difference=-0.1, cliffs_delta_mfe72=0.3
    )
    assert gate.conclusions(a)["ablation_candidate_gate"] == "NONE"


def test_full_label_csv_reports_roundtrip_and_no_db_reaggregation(
    tmp_path, monkeypatch
):
    import gzip

    stage = tmp_path / "stage"
    stage.mkdir()
    start = edge.ms("2024-01-01T00:00:00Z")
    state = {}
    rows = []
    for i in range(3):
        row = dict(
            year=2024,
            run_id=edge.RUNS[2024],
            symbol="BTCUSDT",
            timestamp=edge.iso(start + i * edge.HOUR),
            market_score=80 if i == 0 else 69,
            proposed_entry_reference=100.0,
            original_stop_reference=98.0,
            final_asset_eligible=i == 0,
            single_failure=i == 1,
            single_failure_gate="SCORE" if i == 1 else None,
            single_failure_population="ONLY_SCORE_FAIL" if i == 1 else None,
            failure_count=i,
            strategy_reasons=["APPROVED"]
            if i == 0
            else ["SCORE_BELOW_MIN"]
            if i == 1
            else ["SCORE_BELOW_MIN", "RELATIVE_VOLUME_BELOW_MIN"],
            canonical_failure_combination="NONE"
            if i == 0
            else "SCORE_BELOW_MIN"
            if i == 1
            else "RELATIVE_VOLUME_BELOW_MIN|SCORE_BELOW_MIN",
        )
        row.update({v: True for v in gate.PASS_FIELD.values()})
        row.update(
            {
                k: True
                for k in (
                    "delta_available",
                    "trend_1h_available",
                    "trend_4h_available",
                    "trend_1d_available",
                    "btc_regime_available",
                )
            }
        )
        row["score_pass"] = i == 0
        row["relative_volume_pass"] = i != 2
        rows.append(gate.episode(row, state))
    with gzip.open(stage / "BTCUSDT.jsonl.gz", "wt") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    bars = []
    for i in range(3):
        t = start + i * edge.HOUR
        bars.append(
            (
                Candle(
                    t,
                    t + 59999,
                    100,
                    112 if i == 0 else 101,
                    99 if i == 0 else 97,
                    100,
                    1,
                ),
                100,
            )
        )
        bars.append((Candle(t + 60000, t + 119999, 100, 101, 97, 100, 1), 100))

    class Provider:
        def history(self, *args):
            return CandleHistory(bars)

    bags, count = gate.label_year(2024, stage, Provider(), tmp_path)
    assert count == 3
    assert bags[("raw_hourly", "CURRENT_BUY_ELIGIBLE")].n == 1
    assert bags[("episode_first", "MULTI_FAILURE")].n == 0
    rebuilt, n = gate.bags_from_csv(tmp_path / "top20-opportunity-dataset-2024.csv")
    assert n == 3
    for key in bags:
        assert bags[key].summary() == rebuilt[key].summary()
    analyses = {2024: gate.describe(2024, bags)}
    summary = gate.render(
        analyses,
        tmp_path,
        dict(source_runs=edge.RUNS, checks={2024: dict(top20_count=3)}),
    )
    assert summary["category"] == "CURRENT_ELIGIBILITY_MIXED"
    (tmp_path / "source-validation.json").write_bytes(
        edge.encoded(
            dict(
                source_fingerprint_unchanged=True,
                before={"x": 1},
                after={"x": 1},
                database_writes=0,
                checks={2024: dict(top20_count=3)},
            )
        )
    )
    (tmp_path / "methodology.json").write_bytes(edge.encoded(gate.methodology()))
    gate.package(tmp_path)

    def fail(*args, **kwargs):
        raise AssertionError("No source DB access from immutable CSV regeneration")

    monkeypatch.setattr(sqlite3, "connect", fail)
    assert gate.rebuild_reports(tmp_path).exists()
    changed = json.loads((tmp_path / "summary.json").read_text())
    assert changed["analyses"]["2024"] == json.loads(edge.encoded(analyses[2024]))


def test_missing_delta_is_unavailable_even_when_source_reason_is_nonpositive():
    missing = observed(replace(context(), delta=None))
    present = observed(replace(context(), delta=D(-1)))
    assert (
        missing["strategy_reasons"]
        == present["strategy_reasons"]
        == ["SCORE_DELTA_NOT_POSITIVE"]
    )
    assert gate.unavailable_single(missing) is True
    assert gate.unavailable_single(present) is False
