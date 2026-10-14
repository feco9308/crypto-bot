"""Frozen acceleration/volume hypotheses; no historical research execution."""

import copy
import json
from collections import defaultdict
from pathlib import Path

import pytest
from test_institutional_entry_v1 import BUY_TIME, GOLDEN
from test_replay_btc_gate import btc_row
from test_replay_companion_isolation import (
    START,
    RotatingUniverse,
    Tape,
    controlled,  # noqa: F401
    core,
    features,
    score,
)  # noqa: F401
from test_replay_entry_filter import variant
from test_shadow_exit import bar

from crypto_bot.config.settings import Settings
from crypto_bot.replay.config import ConfigValidationError, input_config, validate
from crypto_bot.replay.engine import ReplayEngine
from crypto_bot.replay.market_context import frame
from crypto_bot.replay.models import ReplayStore, dump
from crypto_bot.replay.rejections import from_audit
from crypto_bot.replay.research import ENTRY_FILTER_REASONS, entry_filter_reasons
from crypto_bot.replay.universe import CandleHistory

ACCEL = {"min_delta_4h": "15.0"}
J = {"max_relative_volume": "2.0"}
K = ACCEL | J
ARTIFACT = (
    Path(__file__).parents[1]
    / "docs/replay/institutional-entry-v2-2023-validation.config.json"
)


@pytest.mark.parametrize("volume,blocked", [("2.000", False), ("2.001", True)])
def test_volume_boundary_and_buy_pipeline(volume, blocked):
    r = btc_row()
    r["features"]["relative_volume"] = volume
    before = copy.deepcopy(r)
    v = variant(J)
    v.config["btc_regime_entry_gate"] = "OFF"
    wallet = copy.deepcopy(v.ledger.checkpoint())
    v.signals([r], {r["symbol"]}, BUY_TIME)
    assert bool(v.pending) != blocked
    assert r == before and wallet == v.ledger.checkpoint()
    event = v.events[-1] if blocked else v.events[0]
    assert event["event"] == (
        "RESEARCH_ENTRY_FILTER_BLOCK" if blocked else "RESEARCH_ENTRY_FILTER_PASS"
    )
    assert event["relative_volume"] == volume
    assert event["filter_thresholds"]["max_relative_volume"] == "2.0"
    assert event["original_strategy_decision"]["action"] == "BUY"
    assert all(key in event for key in ("symbol", "timestamp", "score", "delta_4h"))
    if blocked:
        assert event["filter_reasons"] == ["ENTRY_RELATIVE_VOLUME_ABOVE_MAX"]


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", "-Infinity"])
def test_missing_nonfinite_volume_fails_closed_only_when_constrained(value):
    r = btc_row()
    r["features"]["relative_volume"] = value
    assert entry_filter_reasons(r, J) == ["ENTRY_RELATIVE_VOLUME_UNAVAILABLE"]
    assert entry_filter_reasons(r, {}) == []


@pytest.mark.parametrize(
    "delta,volume,reasons",
    [
        ("15.0", "2.0", []),
        ("14.999", "2.0", ["ENTRY_DELTA_4H_BELOW_MIN"]),
        ("15.0", "2.001", ["ENTRY_RELATIVE_VOLUME_ABOVE_MAX"]),
        (
            "14.999",
            "2.001",
            ["ENTRY_DELTA_4H_BELOW_MIN", "ENTRY_RELATIVE_VOLUME_ABOVE_MAX"],
        ),
    ],
)
def test_combined_frozen_thresholds(delta, volume, reasons):
    r = btc_row()
    r["momentum"]["delta_4h"] = delta
    r["features"]["relative_volume"] = volume
    assert entry_filter_reasons(r, K) == reasons


@pytest.mark.parametrize("bounds", [ACCEL, J, K])
def test_sell_bypasses_volume_and_acceleration(bounds):
    r = btc_row()
    r["momentum"]["delta_4h"] = 15
    r["features"]["relative_volume"] = 2
    v = variant(bounds)
    v.config["btc_regime_entry_gate"] = "OFF"
    v.signals([r], {r["symbol"]}, BUY_TIME)
    v.execute_signals({r["symbol"]: {BUY_TIME: bar()}}, BUY_TIME, False)
    r["total_score"] = 1
    r["momentum"]["delta_4h"] = 0
    r["features"]["relative_volume"] = 3
    before = len(v.events)
    v.signals([r], {r["symbol"]}, BUY_TIME + 60000)
    assert v.pending[0][0].action == "SELL"
    assert not any(
        e["event"].startswith("RESEARCH_ENTRY_FILTER") for e in v.events[before:]
    )
    v.execute_signals({r["symbol"]: {BUY_TIME + 60000: bar()}}, BUY_TIME + 60000, False)
    assert v.execution.fills[-1]["side"] == "SELL"


@pytest.mark.parametrize("value", [-1, "NaN", "Infinity"])
def test_invalid_volume_config(value):
    with pytest.raises(ConfigValidationError) as exc:
        variant({"max_relative_volume": value})
    assert (
        exc.value.errors[0]["field"] == "variants[0].entry_filter.max_relative_volume"
    )


def test_config_roundtrip_and_old_configs_unchanged():
    raw = json.loads(ARTIFACT.read_text())
    cfg = validate(raw)
    assert cfg["dataset_role"] == "VALIDATION"
    assert (cfg["start"], cfg["end"]) == (
        "2023-01-01T00:00:00Z",
        "2024-01-01T00:00:00Z",
    )
    assert (
        cfg["universe_size"] == 20
        and cfg["market_context_version"] == "ISOLATED_TOPN_V1"
    )
    assert "market_context_version" not in raw
    assert cfg["stale_position_policy"] == "RESEARCH_QUARANTINE_STALE"
    assert not cfg["fallback_5m"] and cfg["conservative"]
    assert [v["entry_filter"] for v in raw["variants"]] == [{}, ACCEL, J, K]
    assert validate(input_config(cfg)) == cfg
    old = json.loads(
        (
            ARTIFACT.parent / "institutional-entry-v1-2025-validation.config.json"
        ).read_text()
    )
    for v in cfg["variants"]:
        assert (
            v["strategy"] == "institutional_style_v1"
            and v["exit_policy"] == "baseline_v1"
        )
        for key in [
            "strategy_parameters",
            "risk_parameters",
            "exit_parameters",
            "btc_regime_entry_gate",
        ]:
            assert v[key] == old["variants"][0][key]
    for oldraw in [old, json.loads(GOLDEN.read_text())["public_config"]]:
        oldcfg = validate(oldraw)
        neutral = copy.deepcopy(oldraw)
        for v in neutral["variants"]:
            v.setdefault("entry_filter", {})["max_relative_volume"] = None
        assert validate(neutral) == oldcfg
        assert all(
            "max_relative_volume" not in v["entry_filter"] for v in oldcfg["variants"]
        )


def test_volume_diagnostics_persist_and_legacy_audit_unchanged(tmp_path):
    v = variant(J)
    v.config["btc_regime_entry_gate"] = "OFF"
    r = btc_row()
    r["features"]["relative_volume"] = 2
    v.signals([r], {r["symbol"]}, BUY_TIME)
    r["features"]["relative_volume"] = 3
    v.signals([r], {r["symbol"]}, BUY_TIME + 3600000)
    details = from_audit(v.events, v.blocked)
    d = details["entry_filter_diagnostics"]
    assert (
        d["attempted_buy_count"],
        d["passed_filter_count"],
        d["blocked_filter_count"],
        d["block_rate_pct"],
    ) == (2, 1, 1, 50)
    assert d["reason_counts"]["ENTRY_RELATIVE_VOLUME_ABOVE_MAX"] == 1
    assert d["reason_counts"]["ENTRY_RELATIVE_VOLUME_UNAVAILABLE"] == 0
    store = ReplayStore(tmp_path / "replay.db")
    rid = store.create(validate(json.loads(ARTIFACT.read_text())))
    with store.connection() as db:
        for e in v.events:
            db.execute(
                "insert into replay_audit_events(run_id,variant,data) values(?,?,?)",
                (rid, 0, dump(e)),
            )
    assert store.blocked_entry_details(rid, 0, v.blocked) == details
    old = variant({"min_delta_4h": "5.0"})
    old.config["btc_regime_entry_gate"] = "OFF"
    r["momentum"]["delta_4h"] = 5
    old.signals([r], {r["symbol"]}, BUY_TIME)
    assert "relative_volume" not in old.events[0]
    assert set(
        from_audit(old.events, old.blocked)["entry_filter_diagnostics"]["reason_counts"]
    ) == set(ENTRY_FILTER_REASONS)


def small_run(path, names, stop=None, neutral=False, missing=False):
    raw = json.loads(GOLDEN.read_text())["public_config"]
    base = raw["variants"][0]
    definitions = {"A": {}, "I": ACCEL, "J": J, "K": K}
    raw["variants"] = [base | dict(name=n, entry_filter=definitions[n]) for n in names]
    if neutral:
        raw["variants"][0]["entry_filter"] = {"max_relative_volume": None}
    if missing:
        raw["variants"][0].pop("entry_filter")
    store = ReplayStore(path / "replay.db")
    rid = store.create(validate(raw))

    def check():
        cp = store.checkpoint(rid)
        if stop and cp and cp["next_time"] >= stop:
            raise InterruptedError("fixture checkpoint pause")

    try:
        ReplayEngine(store, Tape(), check).run(rid)
    except InterruptedError:
        pass
    return store, rid


def test_control_companion_order_and_checkpoint_bit_exact(request, tmp_path):
    request.getfixturevalue("controlled")
    golden = json.loads(GOLDEN.read_text())["core"]
    for mode in ["default", "neutral", "missing"]:
        store, rid = small_run(
            tmp_path / mode, ["A"], neutral=mode == "neutral", missing=mode == "missing"
        )
        assert core(store, rid, 0) == golden
    for names in ["AIJK", "KJIA"]:
        store, rid = small_run(tmp_path / names, list(names))
        assert core(store, rid, names.index("A")) == golden
    full, a = small_run(tmp_path / "full", list("AIJK"))
    resumed, b = small_run(tmp_path / "resumed", list("AIJK"), stop=START + 8 * 3600000)
    ReplayEngine(resumed, Tape()).run(b)
    for i in range(4):
        assert core(full, a, i) == core(resumed, b, i)
        assert (
            full.get(a)["variants"][i]["summary"]
            == resumed.get(b)["variants"][i]["summary"]
        )


def test_future_row_ignored_and_stale_wallet_preserved():
    from crypto_bot.replay import stale

    v = variant(J)
    v.config["btc_regime_entry_gate"] = "OFF"
    v.stale_policy = stale.QUARANTINE
    r = btc_row()
    r["features"]["relative_volume"] = 2
    v.signals([r], {r["symbol"]}, BUY_TIME)
    v.execute_signals({r["symbol"]: {BUY_TIME: bar()}}, BUY_TIME, False)
    v.fresh_positions({}, BUY_TIME + 60000)
    assert stale.state(v.ledger.portfolio.positions()[0])["stale"]
    before = copy.deepcopy(v.ledger.checkpoint())
    risk = v.decision_view()
    row = btc_row()
    row["symbol"] = "ETHUSDT"
    row["features"]["relative_volume"] = 3
    row["future_snapshot"] = {"relative_volume": 1}
    original = copy.deepcopy(row)
    v.signals([row], {"ETHUSDT"}, BUY_TIME + 60000)
    assert not v.pending and row == original
    assert v.events[-1]["filter_reasons"] == ["ENTRY_RELATIVE_VOLUME_ABOVE_MAX"]
    assert before == v.ledger.checkpoint() and risk == v.decision_view()


def test_volume_filter_keeps_shared_market_frame_immutable():
    histories = {
        symbol: CandleHistory(
            Tape().hourly(symbol, START - 524 * 3600000, START + 14 * 3600000)
        )
        for symbol in ["NEARUSDT", "OPUSDT"]
    }
    shared = frame(
        RotatingUniverse(histories, None, 0),
        Settings(),
        START + 6 * 3600000,
        defaultdict(list),
        score,
        features,
    )
    before = shared.rows()
    candidate = shared.rows()[0]
    candidate["features"]["relative_volume"] = 2.001
    candidate["future_snapshot"] = {"relative_volume": 1}
    assert entry_filter_reasons(candidate, J) == ["ENTRY_RELATIVE_VOLUME_ABOVE_MAX"]
    assert shared.rows() == before
