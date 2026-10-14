"""Frozen entry hypotheses: closed scanner fields, inclusive bounds and parity."""

import copy
import json
from collections import defaultdict
from pathlib import Path

import pytest
from test_replay import START as BUY_TIME
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
from crypto_bot.replay.research import ENTRY_FILTER_FIELDS, entry_filter_reasons
from crypto_bot.replay.universe import CandleHistory

F = {"min_delta_4h": "5.0"}
G = {"min_atr_pct": "1.0", "max_atr_pct": "3.0"}
H = {"max_range_position": "0.50"}
GOLDEN = Path(__file__).parent / "fixtures/institutional-entry-control-be08107.json"


@pytest.mark.parametrize(
    "key,value,bounds,reasons",
    [
        ("delta_4h", "4.999", F, ["ENTRY_DELTA_4H_BELOW_MIN"]),
        ("delta_4h", "5.000", F, []),
        ("atr_pct", "0.999", G, ["ENTRY_ATR_PCT_BELOW_MIN"]),
        ("atr_pct", "1.000", G, []),
        ("atr_pct", "3.000", G, []),
        ("atr_pct", "3.001", G, ["ENTRY_ATR_PCT_ABOVE_MAX"]),
        ("range_position", "0.500", H, []),
        ("range_position", "0.501", H, ["ENTRY_RANGE_POSITION_ABOVE_MAX"]),
    ],
)
def test_frozen_boundaries_and_pipeline(key, value, bounds, reasons):
    r = btc_row()
    (r["momentum"] if key == "delta_4h" else r["features"])[key] = value
    before = copy.deepcopy(r)
    v = variant(bounds)
    wallet = copy.deepcopy(v.ledger.checkpoint())
    v.signals([r], {r["symbol"]}, BUY_TIME)
    assert entry_filter_reasons(r, bounds) == reasons
    assert r == before and v.ledger.checkpoint() == wallet
    assert bool(v.pending) == (not reasons)
    if reasons:
        event = v.events[-1]
        assert event["filter_reasons"] == reasons
        assert event["original_strategy_decision"]["action"] == "BUY"
        assert event["filter_thresholds"][next(iter(bounds))] == next(
            iter(bounds.values())
        )
        for field in [
            "score",
            "delta_4h",
            "atr_pct",
            "range_position",
            "symbol",
            "timestamp",
        ]:
            assert field in event
    else:
        assert v.events[0]["event"] == "RESEARCH_ENTRY_FILTER_PASS"


@pytest.mark.parametrize(
    "key,bounds,reason",
    [
        ("delta_4h", F, "ENTRY_DELTA_4H_UNAVAILABLE"),
        ("atr_pct", G, "ENTRY_ATR_PCT_UNAVAILABLE"),
        ("range_position", H, "ENTRY_RANGE_POSITION_UNAVAILABLE"),
    ],
)
@pytest.mark.parametrize("invalid", [None, "NaN", "Infinity"])
def test_missing_nonfinite_fail_closed_only_when_constrained(
    key, bounds, reason, invalid
):
    r = btc_row()
    (r["momentum"] if key == "delta_4h" else r["features"])[key] = invalid
    assert entry_filter_reasons(r, bounds) == [reason]
    assert entry_filter_reasons(r, {}) == []


@pytest.mark.parametrize("bounds", [F, G, H])
def test_sell_bypasses_every_entry_filter(bounds):
    v = variant(bounds)
    r = btc_row()
    r["momentum"]["delta_4h"] = 5
    r["features"].update(atr_pct=1, range_position=0.5)
    now = 1735689600000
    v.signals([r], {r["symbol"]}, now)
    v.execute_signals({r["symbol"]: {now: bar()}}, now, False)
    assert v.execution.fills[-1]["side"] == "BUY"
    r["total_score"] = 1
    r["momentum"]["delta_4h"] = 4
    r["features"].update(atr_pct=0.999, range_position=0.501)
    before = len(v.events)
    v.signals([r], {r["symbol"]}, now)
    assert v.pending[0][0].action == "SELL"
    assert not any(
        e["event"].startswith("RESEARCH_ENTRY_FILTER") for e in v.events[before:]
    )
    v.execute_signals({r["symbol"]: {now: bar()}}, now, False)
    assert v.execution.fills[-1]["side"] == "SELL"


@pytest.mark.parametrize(
    "bounds",
    [
        {"min_atr_pct": -1},
        {"max_atr_pct": -1},
        {"max_range_position": 1.01},
        {"min_delta_4h": "NaN"},
        {"max_atr_pct": "Infinity"},
        {"min_atr_pct": 3, "max_atr_pct": 1},
    ],
)
def test_invalid_config_fields(bounds):
    with pytest.raises(ConfigValidationError) as e:
        variant(bounds)
    assert all(
        x["field"].startswith("variants[0].entry_filter.") for x in e.value.errors
    )


def test_roundtrip_neutral_and_validation_artifact():
    cfg = json.loads(GOLDEN.read_text())["public_config"]
    old = validate(cfg)
    neutral = copy.deepcopy(cfg)
    neutral["variants"][0]["entry_filter"] |= ENTRY_FILTER_FIELDS
    assert validate(neutral) == old
    missing = copy.deepcopy(cfg)
    missing["variants"][0].pop("entry_filter")
    assert validate(missing) == old
    for bounds in [F, G, H]:
        raw = copy.deepcopy(cfg)
        raw["variants"][0]["entry_filter"] = bounds
        normalized = validate(raw)
        assert validate(input_config(normalized)) == normalized
    artifact = (
        Path(__file__).parents[1]
        / "docs/replay/institutional-entry-v1-2025-validation.config.json"
    )
    raw = json.loads(artifact.read_text())
    normalized = validate(raw)
    assert normalized["dataset_role"] == "VALIDATION"
    assert (
        normalized["start"] == "2025-02-01T00:00:00Z"
        and normalized["end"] == "2026-01-01T00:00:00Z"
    )
    assert (
        normalized["market_context_version"] == "ISOLATED_TOPN_V1"
        and "market_context_version" not in raw
    )
    assert [v["entry_filter"] for v in raw["variants"]] == [{}, F, G, H]
    for v in normalized["variants"]:
        assert (
            v["strategy"] == "institutional_style_v1"
            and v["exit_policy"] == "baseline_v1"
        )
        assert v["strategy_parameters"] == old["variants"][0]["strategy_parameters"]
        assert v["risk_parameters"] == old["variants"][0]["risk_parameters"]


def test_pass_block_diagnostics_and_persistence(tmp_path):
    v = variant(F)
    v.config["btc_regime_entry_gate"] = "OFF"
    r = btc_row()
    r["momentum"]["delta_4h"] = 5
    now = 1735689600000
    v.signals([r], {r["symbol"]}, now)
    r["momentum"]["delta_4h"] = 4
    v.signals([r], {r["symbol"]}, now + 3600000)
    result = from_audit(v.events, v.blocked)
    d = result["entry_filter_diagnostics"]
    assert (
        d["attempted_buy_count"],
        d["passed_filter_count"],
        d["blocked_filter_count"],
        d["block_rate_pct"],
    ) == (2, 1, 1, 50)
    assert d["reason_counts"]["ENTRY_DELTA_4H_BELOW_MIN"] == 1
    store = ReplayStore(tmp_path / "replay.db")
    rid = store.create(validate(json.loads(GOLDEN.read_text())["public_config"]))
    with store.connection() as db:
        for event in v.events:
            db.execute(
                "insert into replay_audit_events(run_id,variant,data) values(?,?,?)",
                (rid, 0, dump(event)),
            )
    assert store.blocked_entry_details(rid, 0, v.blocked) == result


def small_run(path, names, stop=None, neutral=False, missing=False):
    golden = json.loads(GOLDEN.read_text())
    raw = copy.deepcopy(golden["public_config"])
    base = raw["variants"][0]
    defs = {"A": {}, "F": F, "G": G, "H": H}
    raw["variants"] = [
        base | dict(name=name, entry_filter=defs[name]) for name in names
    ]
    if neutral:
        raw["variants"][0]["entry_filter"] = ENTRY_FILTER_FIELDS.copy()
    cfg = validate(raw)
    if missing:
        cfg["variants"][0].pop("entry_filter")
    store = ReplayStore(path / "replay.db")
    rid = store.create(cfg)

    def check():
        checkpoint = store.checkpoint(rid)
        if stop and checkpoint and checkpoint["next_time"] >= stop:
            raise InterruptedError("test pause")

    try:
        ReplayEngine(store, Tape(), check).run(rid)
    except InterruptedError:
        pass
    return store, rid


def test_control_bit_exact_against_independent_old_code_fixture(request, tmp_path):
    request.getfixturevalue("controlled")
    golden = json.loads(GOLDEN.read_text())
    for mode in ["default", "neutral", "missing"]:
        store, rid = small_run(
            tmp_path / mode, ["A"], neutral=mode == "neutral", missing=mode == "missing"
        )
        assert core(store, rid, 0) == golden["core"]
        assert (
            "entry_filter_diagnostics"
            not in store.get(rid)["variants"][0]["summary"]["metrics"]
        )


def test_filtered_companions_and_order_do_not_change_control(request, tmp_path):
    request.getfixturevalue("controlled")
    golden = json.loads(GOLDEN.read_text())["core"]
    for names in [["A", "F", "G", "H"], ["H", "G", "F", "A"]]:
        store, rid = small_run(tmp_path / "".join(names), names)
        assert core(store, rid, names.index("A")) == golden


def test_checkpoint_resume_filtered_variants_bit_exact(request, tmp_path):
    request.getfixturevalue("controlled")
    full, a = small_run(tmp_path / "full", list("AFGH"))
    resumed, b = small_run(tmp_path / "resumed", list("AFGH"), stop=START + 8 * 3600000)
    ReplayEngine(resumed, Tape()).run(b)
    for i in range(4):
        assert core(full, a, i) == core(resumed, b, i)
        assert (
            full.get(a)["variants"][i]["summary"]
            == resumed.get(b)["variants"][i]["summary"]
        )


def test_filter_cannot_mutate_shared_frame_and_ignores_future():
    histories = {
        s: CandleHistory(Tape().hourly(s, START - 524 * 3600000, START + 14 * 3600000))
        for s in ["NEARUSDT", "OPUSDT"]
    }
    f = frame(
        RotatingUniverse(histories, None, 0),
        Settings(),
        START + 6 * 3600000,
        defaultdict(list),
        score,
        features,
    )
    before = f.rows()
    row = f.rows()[0]
    row["future_snapshot"] = {"atr_pct": 2, "range_position": 0.1, "delta_4h": 999}
    row["features"]["atr_pct"] = 0.999
    assert entry_filter_reasons(row, G) == ["ENTRY_ATR_PCT_BELOW_MIN"]
    assert f.rows() == before


def test_active_filter_with_zero_buy_opportunities_reports_zero(request, tmp_path):
    request.getfixturevalue("controlled")
    raw = json.loads(GOLDEN.read_text())["public_config"]
    raw["end"] = "2024-01-01T04:00:00Z"
    raw["variants"][0]["entry_filter"] = F
    store = ReplayStore(tmp_path / "replay.db")
    rid = store.create(validate(raw))
    ReplayEngine(store, Tape()).run(rid)
    d = store.get(rid)["variants"][0]["summary"]["metrics"]["entry_filter_diagnostics"]
    assert (
        d["attempted_buy_count"]
        == d["passed_filter_count"]
        == d["blocked_filter_count"]
        == 0
    )
    assert d["block_rate_pct"] is None


def test_filter_block_preserves_stale_quarantine_wallet():
    from crypto_bot.replay import stale

    v = variant(G)
    v.stale_policy = stale.QUARANTINE
    r = btc_row()
    r["features"]["atr_pct"] = 1
    now = BUY_TIME
    v.signals([r], {r["symbol"]}, now)
    v.execute_signals({r["symbol"]: {now: bar()}}, now, False)
    v.fresh_positions({}, now + 60000)
    assert stale.state(v.ledger.portfolio.positions()[0])["stale"]
    before = copy.deepcopy(v.ledger.checkpoint())
    risk_before = v.decision_view()
    candidate = btc_row()
    candidate["symbol"] = "ETHUSDT"
    candidate["features"]["atr_pct"] = 0.999
    v.signals([candidate], {"ETHUSDT"}, now + 60000)
    assert not v.pending
    assert v.events[-1]["filter_reasons"] == ["ENTRY_ATR_PCT_BELOW_MIN"]
    assert v.ledger.checkpoint() == before and v.decision_view() == risk_before
