"""Research gate consumes the original replay BTC row; no production strategy edits."""

import copy
import json

import pytest
from test_replay import START, config, row
from test_shadow_exit import bar

from crypto_bot.replay.clock import at
from crypto_bot.replay.config import ConfigValidationError, input_config, validate
from crypto_bot.replay.engine import Variant
from crypto_bot.replay.rejections import from_audit
from crypto_bot.replay.research import NAME, adaptive, gate_state
from crypto_bot.trading.domain import D


def variant(gate="OFF", exit_policy="baseline_v1"):
    raw = config()
    raw["variants"] = [{"btc_regime_entry_gate": gate, "exit_policy": exit_policy}]
    # config() already returns derived snapshots; extract only request fields.
    raw = {k: v for k, v in raw.items() if k in ("start", "end", "variants")}
    return Variant(validate(raw)["variants"][0], START)


def btc_row(regime="BULLISH", source=START - 1):
    r = row()
    r.update(
        btc_regime=regime,
        btc_regime_source_timestamp=at(source).isoformat() + "Z"
        if source is not None
        else None,
    )
    return r


@pytest.mark.parametrize(
    "regime,source,state",
    [
        ("BULLISH", START - 1, "BULLISH"),
        ("BEARISH", START - 1, "BEARISH"),
        ("UNKNOWN", None, "UNKNOWN"),
        ("BULLISH", None, "UNKNOWN"),
        ("BULLISH", START, "UNKNOWN"),
        ("BULLISH", START + 1, "UNKNOWN"),
        ("BULLISH", START - 3600001, "STALE"),
    ],
)
def test_gate_freshness_and_no_future_btc_source(regime, source, state):
    assert gate_state(btc_row(regime, source), START) == state


@pytest.mark.parametrize(
    "regime,source",
    [("BEARISH", START - 1), ("UNKNOWN", None), ("BULLISH", START - 3600001)],
)
def test_blocked_buy_audit_and_reporting(regime, source):
    v = variant("BULLISH_ONLY")
    r = btc_row(regime, source)
    assert v.signals([r], {r["symbol"]}, START) == set()
    assert not v.pending and not v.execution.fills
    e = v.events[-1]
    assert e["event"] == "BTC_REGIME_ENTRY_BLOCK"
    assert e["original_strategy_decision"]["action"] == "BUY"
    assert e["strategy_score"] == str(D(r["total_score"]))
    assert e["btc_regime_source_timestamp"] == r["btc_regime_source_timestamp"]
    result = from_audit(v.events, v.blocked)
    assert result["btc_regime_entry_block_count"] == 1
    assert result["blocked_entry_categories"]["btc_regime_entry_block"] == 1
    assert result["blocked_entry_breakdown_complete"]


def test_off_preserves_current_strategy_risk_and_fill():
    a, b = variant(), variant("OFF")
    r = btc_row("BEARISH")
    for v in (a, b):
        v.signals([r], {r["symbol"]}, START)
        v.execute_signals({r["symbol"]: {START: bar()}}, START, False)
    assert a.execution.fills == b.execution.fills
    assert a.ledger.checkpoint() == b.ledger.checkpoint()
    assert a.events == b.events
    assert len(a.execution.fills) == 1


def test_sell_is_not_regime_blocked():
    v = variant("BULLISH_ONLY")
    r = btc_row()
    v.signals([r], {r["symbol"]}, START)
    v.execute_signals({r["symbol"]: {START: bar()}}, START, False)
    r = btc_row("BEARISH", START + 3600000 - 1)
    r["total_score"] = 1
    v.signals([r], {r["symbol"]}, START + 3600000)
    assert len(v.pending) == 1 and v.pending[0][0].action == "SELL"
    v.execute_signals(
        {r["symbol"]: {START + 3600000: bar(START + 3600000)}}, START + 3600000, False
    )
    assert v.execution.fills[-1]["side"] == "SELL"
    assert not any(e["event"] == "BTC_REGIME_ENTRY_BLOCK" for e in v.events)


def test_blocking_leaves_cash_and_slot_for_later_bullish_buy():
    v = variant("BULLISH_ONLY")
    r = btc_row("BEARISH")
    before = copy.deepcopy(v.ledger.checkpoint())
    v.signals([r], {r["symbol"]}, START)
    assert v.ledger.checkpoint() == before
    later = START + 3600000
    r = btc_row("BULLISH", later - 1)
    r["symbol"] = "ETHUSDT"
    v.signals([r], {"ETHUSDT"}, later)
    v.execute_signals({"ETHUSDT": {later: bar(later)}}, later, False)
    assert v.ledger.portfolio.positions()[0].symbol == "ETHUSDT"


def test_config_defaults_and_roundtrip():
    raw = {
        "start": "2024-01-01",
        "end": "2024-01-02",
        "variants": [{"btc_regime_entry_gate": "BULLISH_ONLY"}],
    }
    normalized = validate(raw)
    assert validate(input_config(normalized)) == normalized
    legacy = validate(raw)
    legacy["variants"][0].pop("btc_regime_entry_gate")
    assert input_config(legacy)["variants"][0]["btc_regime_entry_gate"] == "OFF"
    raw["variants"][0]["btc_regime_entry_gate"] = "BAD"
    with pytest.raises(ConfigValidationError):
        validate(raw)


def test_adaptive_exit_replay_uses_same_shadow_model_and_checkpoint_state():
    v = variant("BULLISH_ONLY", NAME)
    r = btc_row()
    v.signals([r], {r["symbol"]}, START)
    v.execute_signals({r["symbol"]: {START: bar()}}, START, False)
    p = v.ledger.portfolio.positions()[0]
    m = adaptive(p, v.settings)
    assert m.quantity == p.quantity
    peak = float(m.thresholds[1]) + float(m.atr) * 0.1
    epsilon = float(m.atr) * 0.001
    v.bar(
        p.symbol,
        bar(START + 60000, peak, peak + epsilon, peak - epsilon, peak),
        START + 120000,
        True,
    )
    checkpoint = json.loads(json.dumps(v.checkpoint(), default=str))
    restored = variant("BULLISH_ONLY", NAME)
    restored.ledger.restore(checkpoint["ledger"])
    restored.execution.next_order = checkpoint["next_order"]
    assert (
        adaptive(restored.ledger.portfolio.positions()[0], v.settings).state
        == adaptive(p, v.settings).state
    )
    for engine in (v, restored):
        engine.bar(
            p.symbol, bar(START + 120000, peak, peak + 0.1, 97), START + 180000, True
        )
    assert json.loads(json.dumps(v.closed[-1], default=str)) == json.loads(
        json.dumps(restored.closed[-1], default=str)
    )
    assert v.closed[-1]["final_protective_stop"] and v.closed[-1]["activation_1r_time"]


def test_gate_audit_persists_and_is_exported_as_known_reason(tmp_path):
    from crypto_bot.replay.models import ReplayStore, dump

    store = ReplayStore(tmp_path / "replay.db")
    ident = store.create(config())
    v = variant("BULLISH_ONLY")
    v.signals([btc_row("BEARISH")], {"BTCUSDT"}, START)
    with store.connection() as db:
        db.execute(
            "INSERT INTO replay_audit_events(run_id,variant,data) VALUES(?,?,?)",
            (ident, 0, dump(v.events[-1])),
        )
    detail = store.blocked_entry_details(ident, 0, v.blocked)
    assert detail["btc_regime_entry_blocks"]["BEARISH"] == 1
    assert detail["blocked_entry_breakdown_complete"]
