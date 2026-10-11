"""Fixed scanner snapshots: research bounds cannot change production strategy."""

import copy
import json

import pytest
from test_replay import START, Synthetic, config
from test_replay_btc_gate import btc_row
from test_shadow_exit import bar

from crypto_bot.replay.config import ConfigValidationError, input_config, validate
from crypto_bot.replay.engine import ReplayEngine, Variant
from crypto_bot.replay.models import ReplayStore, dump
from crypto_bot.replay.rejections import from_audit
from crypto_bot.replay.research import entry_filter_reasons


def variant(bounds=None):
    raw = {
        "start": "2025-01-01",
        "end": "2025-01-02",
        "variants": [
            {"entry_filter": bounds or {}, "btc_regime_entry_gate": "BULLISH_ONLY"}
        ],
    }
    return Variant(validate(raw)["variants"][0], START)


@pytest.mark.parametrize(
    "score,delta,bounds,expected",
    [
        (79.999, 7, {"score_min": 80}, ["SCORE_BELOW_MIN"]),
        (80, 7, {"score_min": 80}, []),
        (89.999, 7, {"score_max": 90}, []),
        (90, 7, {"score_max": 90}, ["SCORE_ABOVE_MAX"]),
        (85, 4.999, {"delta4h_min": 5}, ["DELTA4H_BELOW_MIN"]),
        (85, 5, {"delta4h_min": 5}, []),
        (85, 9.999, {"delta4h_max": 10}, []),
        (85, 10, {"delta4h_max": 10}, ["DELTA4H_ABOVE_MAX"]),
        (
            80,
            5,
            {"score_min": 80, "score_max": 90, "delta4h_min": 5, "delta4h_max": 10},
            [],
        ),
        (
            90,
            10,
            {"score_max": 90, "delta4h_max": 10},
            ["SCORE_ABOVE_MAX", "DELTA4H_ABOVE_MAX"],
        ),
        (79, 7, {"score_min": 80, "delta4h_min": 5}, ["SCORE_BELOW_MIN"]),
        (85, 4, {"score_min": 80, "delta4h_min": 5}, ["DELTA4H_BELOW_MIN"]),
    ],
)
def test_exact_bounds_and_combined_and(score, delta, bounds, expected):
    r = btc_row()
    r["total_score"] = score
    r["momentum"]["delta_4h"] = delta
    assert entry_filter_reasons(r, bounds) == expected
    v = variant(bounds)
    v.signals([r], {r["symbol"]}, START)
    assert bool(v.pending) == (not expected)
    v.execute_signals({r["symbol"]: {START: bar()}}, START, False)
    assert len(v.execution.fills) == (0 if expected else 1)
    if expected:
        assert not v.execution.orders
        audit = v.events[0]
        assert audit["event"] == "RESEARCH_ENTRY_FILTER_BLOCK"
        assert audit["timestamp"] == "2025-01-01T00:00:00Z"
        assert audit["symbol"] == r["symbol"] and audit["delta_4h"] == delta
        assert float(audit["score"]) == score
        assert audit["filter_reasons"] == expected
        assert audit["original_strategy_decision"]["action"] == "BUY"


def test_unavailable_is_only_blocked_when_constrained():
    r = btc_row()
    r["momentum"]["delta_4h"] = None
    assert entry_filter_reasons(r, {}) == []
    assert entry_filter_reasons(r, {"delta4h_min": 5}) == ["DELTA4H_UNAVAILABLE"]


def test_off_legacy_backward_compatibility():
    a = variant()
    b = variant()
    a.config.pop("entry_filter")  # Actual old immutable variant snapshot.
    r = btc_row()
    for v in (a, b):
        v.signals([r], {r["symbol"]}, START)
        v.execute_signals({r["symbol"]: {START: bar()}}, START, False)
    assert a.ledger.checkpoint() == b.ledger.checkpoint()
    assert a.execution.fills == b.execution.fills and a.events == b.events


def test_buy_block_preserves_cash_slot_and_later_buy_sell():
    v = variant({"score_min": 80, "score_max": 90})
    r = btc_row()
    r["total_score"] = 90
    before = copy.deepcopy(v.ledger.checkpoint())
    v.signals([r], {r["symbol"]}, START)
    assert not v.pending and v.ledger.checkpoint() == before
    later = START + 3600000
    r = btc_row(source=later - 1)
    r["total_score"] = 85
    v.signals([r], {r["symbol"]}, later)
    v.execute_signals({r["symbol"]: {later: bar(later)}}, later, False)
    assert len(v.execution.fills) == 1
    r["total_score"] = 1
    v.signals([r], {r["symbol"]}, later)
    assert v.pending[0][0].action == "SELL"
    v.execute_signals({r["symbol"]: {later: bar(later)}}, later, False)
    assert v.execution.fills[-1]["side"] == "SELL"


def test_filter_uses_current_delta4h_not_strategy_delta_window_or_future():
    v = variant({"delta4h_min": 5})
    r = btc_row()
    r["momentum"]["delta_4h"] = 4
    r["future_snapshot"] = {"delta_4h": 8, "total_score": 85}
    v.signals([r], {r["symbol"]}, START)
    assert not v.pending and v.events[0]["filter_reasons"] == ["DELTA4H_BELOW_MIN"]


@pytest.mark.parametrize(
    "bounds",
    [
        {"score_min": "NaN"},
        {"delta4h_max": "infinity"},
        {"score_min": 90, "score_max": 80},
        {"delta4h_min": 5, "delta4h_max": 5},
        {"score_max": 101},
        {"unknown": 1},
    ],
)
def test_schema_invalid_bounds_have_field_errors(bounds):
    with pytest.raises(ConfigValidationError) as exc:
        variant(bounds)
    assert all(
        e["field"].startswith("variants[0].entry_filter.") for e in exc.value.errors
    )


def test_roundtrip_and_legacy_config():
    cfg = config()
    cfg["variants"][0]["entry_filter"] = {
        "score_min": "80",
        "score_max": "90",
        "delta4h_min": "5",
        "delta4h_max": "10",
    }
    normalized = validate(input_config(cfg))
    assert validate(input_config(normalized)) == normalized
    cfg["variants"][0].pop("entry_filter")
    assert all(
        v is None for v in input_config(cfg)["variants"][0]["entry_filter"].values()
    )


def test_multi_reason_audit_counter_persists_without_double_count(tmp_path):
    v = variant({"score_max": 85, "delta4h_max": 5})
    r = btc_row()
    r["total_score"] = 85
    r["momentum"]["delta_4h"] = 5
    v.signals([r], {r["symbol"]}, START)
    store = ReplayStore(tmp_path / "replay.db")
    ident = store.create(config())
    with store.connection() as db:
        db.execute(
            "INSERT INTO replay_audit_events(run_id,variant,data) VALUES(?,?,?)",
            (ident, 0, dump(v.events[-1])),
        )
    result = store.blocked_entry_details(ident, 0, v.blocked)
    assert result["research_entry_filter_block_count"] == 1
    assert result["research_entry_filter_blocks"]["SCORE_ABOVE_MAX"] == 1
    assert result["research_entry_filter_blocks"]["DELTA4H_ABOVE_MAX"] == 1
    assert result["blocked_entry_breakdown_complete"]
    assert result == from_audit(v.events, v.blocked)


def test_full_replay_legacy_off_control_parity(tmp_path):
    store = ReplayStore(tmp_path / "replay.db")
    cfg = config()
    cfg["variants"][0]["exit_policy"] = "adaptive_peak_lock_v1"
    cfg["variants"][0]["exit_parameters"] = {}
    legacy = copy.deepcopy(cfg)
    legacy["variants"][0].pop("entry_filter", None)
    old, new = store.create(legacy), store.create(validate(input_config(cfg)))
    for ident in (old, new):
        ReplayEngine(store, Synthetic()).run(ident)
    results = [store.get(i)["variants"][0]["summary"]["metrics"] for i in (old, new)]
    assert results[0] == results[1]
    with store.connection() as db:
        trades = [
            [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT data FROM replay_trades WHERE run_id=? AND variant=0 ORDER BY position_id",
                    (ident,),
                )
            ]
            for ident in (old, new)
        ]
    assert trades[0] == trades[1]
