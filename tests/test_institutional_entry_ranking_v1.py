"""Ordering research: unchanged eligibility, real risk/ledger, fixed synthetic data."""

import copy
import json
from dataclasses import replace
from decimal import localcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_institutional_entry_v1 import GOLDEN
from test_institutional_entry_v3 import candidate, institutional_context
from test_replay_companion_isolation import (
    START,
    Tape,
    controlled,  # noqa: F401
    core,
)
from test_shadow_exit import bar

from crypto_bot.replay import stale
from crypto_bot.replay.config import (
    ConfigValidationError,
    config_hash,
    input_config,
    validate,
)
from crypto_bot.replay.engine import ReplayEngine, Variant
from crypto_bot.replay.entry_ranking import ranked
from crypto_bot.replay.models import ReplayStore

ROOT = Path(__file__).parents[1]
ARTIFACT = (
    ROOT / "docs/replay/institutional-entry-ranking-v1-2024-exploration.config.json"
)
PROOF = ROOT / "tests/fixtures/institutional-entry-ranking-control-51e1137.json"
H = 3600000
S = "market_score_desc_v1"
R = "acceleration_efficiency_v1"


def row(symbol, score=80, delta=20, volume=2):
    r = candidate()
    r["symbol"], r["total_score"] = symbol, score
    r["momentum"]["delta_4h"] = delta
    r["features"]["relative_volume"] = volume
    return r


def variant(policy=R, max_positions=3):
    raw = json.loads(ARTIFACT.read_text())
    v = raw["variants"][0] | dict(entry_ranking=policy)
    v["risk_parameters"] = v["risk_parameters"] | dict(max_open_positions=max_positions)
    raw["variants"] = [v]
    result = Variant(validate(raw)["variants"][0], START, stale.QUARANTINE)
    result.research_context = SimpleNamespace(
        context=lambda *args: institutional_context()
    )
    result.context_isolated = True
    return result


def signals(v, rows, now=START):
    before = copy.deepcopy(rows)
    ledger = copy.deepcopy(v.ledger.checkpoint())
    v.signals(rows, {r["symbol"] for r in rows}, now)
    assert rows == before
    assert v.ledger.checkpoint() == ledger
    return [s.symbol for s, r in v.pending if s.action == "BUY"]


def execute(v, rows, now=START):
    v.execute_signals({r["symbol"]: {now: bar()} for r in rows}, now, False)


def test_score_order_and_exact_ties():
    rows = [row("ZUSDT", 90), row("AUSDT", 90), row("BUSDT", 80)]
    assert signals(variant("OFF"), rows) == ["AUSDT", "ZUSDT", "BUSDT"]
    assert signals(variant(S), list(reversed(rows))) == ["AUSDT", "ZUSDT", "BUSDT"]


def test_efficiency_score_and_symbol_tie_breaks():
    rows = [
        row("ZUSDT", 80, 20, 2),
        row("AUSDT", 80, 15, "1.5"),
        row("BUSDT", 90, 20, 2),
        row("CUSDT", 70, 30, 2),
    ]
    assert signals(variant(R), rows) == ["CUSDT", "BUSDT", "AUSDT", "ZUSDT"]


def test_exact_rational_comparison_not_global_decimal_precision():
    v = variant()
    signals(v, [row("AUSDT"), row("ZUSDT")])
    pending = copy.deepcopy(v.pending)
    for s, r in pending:
        r["momentum"]["delta_4h"] = (
            "1" if s.symbol == "AUSDT" else "1." + "0" * 60 + "1"
        )
        r["features"]["relative_volume"] = "3"
    for precision in [6, 28, 70]:
        with localcontext() as ctx:
            ctx.prec = precision
            result = ranked(pending, R)
            assert [s.symbol for s, r in result] == ["ZUSDT", "AUSDT"]
            assert result[0][1]["_entry_ranking"]["entry_ranking_value"].startswith(
                "0.3333333333"
            )


@pytest.mark.parametrize(
    "key,value",
    [
        ("delta_4h", None),
        ("delta_4h", "NaN"),
        ("delta_4h", "Infinity"),
        ("delta_4h", "-Infinity"),
        ("relative_volume", None),
        ("relative_volume", "NaN"),
        ("relative_volume", float("nan")),
        ("relative_volume", "Infinity"),
        ("relative_volume", "-Infinity"),
        ("relative_volume", 0),
        ("relative_volume", -1),
    ],
)
def test_invalid_ranking_features_do_not_remove_valid_buy(key, value):
    v = variant()
    signals(v, [row("BADUSDT", 90), row("GOODUSDT", 80)])
    pending = copy.deepcopy(v.pending)
    for s, r in pending:
        if s.symbol == "BADUSDT":
            (r["momentum"] if key == "delta_4h" else r["features"])[key] = value
    result = ranked(pending, R)
    assert [s.symbol for s, r in result] == ["GOODUSDT", "BADUSDT"]
    audit = result[-1][1]["_ranking_event"]
    assert not audit["ranking_value_available"] and audit["ranking_value"] is None
    json.dumps(audit, default=str, allow_nan=False)
    assert {id(s) for s, r in pending} == {id(s) for s, r in result}


def test_missing_keys_and_all_unavailable_sort_by_score_then_symbol():
    v = variant()
    signals(v, [row("ZUSDT", 90), row("AUSDT", 90), row("BUSDT", 80)])
    for s, r in v.pending:
        r["momentum"].pop("delta_4h")
        r["features"].pop("relative_volume")
    result = ranked(v.pending, R)
    assert [s.symbol for s, r in result] == ["AUSDT", "ZUSDT", "BUSDT"]
    assert all(not r["_ranking_event"]["ranking_value_available"] for s, r in result)


def test_already_accepted_buy_with_unavailable_rank_still_reaches_fill():
    v = variant()
    signals(v, [row("BADUSDT", 90), row("GOODUSDT", 80)])
    for s, r in v.pending:
        if s.symbol == "BADUSDT":
            r["momentum"]["delta_4h"] = None
    v.pending = ranked(v.pending, R)
    execute(v, [row("BADUSDT"), row("GOODUSDT")])
    assert [f["symbol"] for f in v.execution.fills] == ["GOODUSDT", "BADUSDT"]


def test_higher_rank_gets_slot_full_list_reaches_real_risk():
    v = variant(max_positions=1)
    rows = [row("SCOREUSDT", 90, 2), row("FASTUSDT", 80, 20)]
    assert signals(v, rows) == ["FASTUSDT", "SCOREUSDT"]
    assert len(v.pending) == 2
    execute(v, rows)
    assert [f["symbol"] for f in v.execution.fills] == ["FASTUSDT"]
    rejected = [
        e
        for e in v.events
        if e["event"] == "RISK_DECISION" and not e["decision"]["approved"]
    ]
    assert rejected[0]["symbol"] == "SCOREUSDT"
    assert rejected[0]["entry_rank"] == 2
    assert rejected[0]["decision"]["reasons"] == ("max open positions reached",)


def test_rank_one_risk_rejection_does_not_truncate_following_candidates():
    v = variant(max_positions=1)
    rows = [row("FIRSTUSDT", 80, 30), row("SECONDUSDT", 80, 20)]
    signals(v, rows)
    first = bar()
    first["open"] = 97  # Below the unchanged signal stop of 98.
    v.execute_signals(
        {"FIRSTUSDT": {START: first}, "SECONDUSDT": {START: bar()}}, START, False
    )
    assert [f["symbol"] for f in v.execution.fills] == ["SECONDUSDT"]
    e = next(
        e
        for e in v.events
        if e["event"] == "RISK_DECISION" and e["symbol"] == "FIRSTUSDT"
    )
    assert (
        e["decision"]["reasons"] == ("invalid stop distance",) and e["entry_rank"] == 1
    )


def test_correlation_rejection_then_next_rank_can_fill():
    v = variant(max_positions=2)
    v.research_context = SimpleNamespace(
        context=lambda symbol, now, settings, positions: institutional_context(
            blocked=symbol == "YUSDT" and any(p.symbol == "XUSDT" for p in positions)
        )
    )
    rows = [row("ZUSDT", 80, 10), row("YUSDT", 80, 20), row("XUSDT", 80, 30)]
    assert signals(v, rows) == ["XUSDT", "YUSDT", "ZUSDT"]
    execute(v, rows)
    assert [f["symbol"] for f in v.execution.fills] == ["XUSDT", "ZUSDT"]
    e = next(e for e in v.events if e["event"] == "RISK_REJECTED")
    assert (
        e["symbol"] == "YUSDT"
        and e["entry_rank"] == 2
        and e["reasons"] == ["CORRELATION_LIMIT"]
    )


def test_no_fill_rank_one_still_allows_rank_two():
    v = variant(max_positions=1)
    rows = [row("FIRSTUSDT", 80, 30), row("SECONDUSDT", 80, 20)]
    signals(v, rows)
    v.execute_signals({"SECONDUSDT": {START: bar()}}, START, False)
    assert [f["symbol"] for f in v.execution.fills] == ["SECONDUSDT"]
    e = next(e for e in v.events if e["event"] == "NO_FILL")
    assert e["entry_rank"] == 1 and e["entry_candidate_pool_size"] == 2


def test_sell_bypass_and_before_buy_with_existing_held_exit_row():
    v = variant(max_positions=1)
    held = row("HELDUSDT")
    signals(v, [held], START - H)
    execute(v, [held], START - H)
    v.events = []
    held["total_score"] = 1
    held["auto_eligible"] = False
    rows = [row("SCOREUSDT", 90, 2), held, row("FASTUSDT", 80, 20)]
    signals(v, rows)
    assert [s.action for s, r in v.pending] == ["SELL", "BUY", "BUY"]
    assert [
        e["symbol"] for e in v.events if e["event"] == "ENTRY_CANDIDATE_RANKED"
    ] == ["FASTUSDT", "SCOREUSDT"]
    execute(v, rows)
    assert [(f["symbol"], f["side"]) for f in v.execution.fills[-2:]] == [
        ("HELDUSDT", "SELL"),
        ("FASTUSDT", "BUY"),
    ]
    assert all(
        "entry_rank" not in e
        for e in v.events
        if e.get("action") == "SELL" or e.get("side") == "SELL"
    )


def test_defensive_held_only_buy_not_ranked_or_dropped():
    v = variant()
    signals(v, [row("AUSDT"), row("BUSDT")])
    pending = copy.deepcopy(v.pending)
    for s, r in pending:
        r.pop("_entry_ranking")
        r.pop("_ranking_event")
        if s.symbol == "AUSDT":
            r["auto_eligible"] = False
    result = ranked(pending, R)
    assert len(result) == 2
    held = next(r for s, r in result if s.symbol == "AUSDT")
    assert "_entry_ranking" not in held
    assert result[0][1]["_ranking_event"]["candidate_pool_size"] == 1


def test_rank_audit_fields_and_fill_linkage_without_financial_record_changes():
    rows = [row("AUSDT", 80, 20, 2), row("BUSDT", 90, 15, "1.5")]
    v = variant()
    signals(v, rows)
    first = next(e for e in v.events if e["event"] == "ENTRY_CANDIDATE_RANKED")
    assert {
        k: first[k]
        for k in [
            "symbol",
            "ranking_policy",
            "rank",
            "candidate_pool_size",
            "ranking_value",
            "ranking_value_available",
            "market_score",
            "delta_4h",
            "relative_volume",
        ]
    } == dict(
        symbol="BUSDT",
        ranking_policy=R,
        rank=1,
        candidate_pool_size=2,
        ranking_value="10",
        ranking_value_available=True,
        market_score="90",
        delta_4h="15",
        relative_volume="1.5",
    )
    assert first["original_strategy_decision"]["action"] == "BUY"
    execute(v, rows)
    fill = next(e for e in v.events if e["event"] == "ENTRY_FILL")
    assert fill["entry_candidate_id"] == first["entry_candidate_id"]
    assert (
        fill["position_id"] == first["position_id"]
        and fill["order_id"] == first["order_id"]
    )
    assert all("entry_rank" not in f for f in v.execution.fills)


def test_off_path_never_invokes_ranking_and_keeps_events_checkpoint(monkeypatch):
    import crypto_bot.replay.engine as engine

    monkeypatch.setattr(engine, "ranked", lambda *a: pytest.fail("OFF invoked ranking"))
    v = variant("OFF")
    signals(v, [row("ZUSDT"), row("AUSDT")])
    execute(v, [row("ZUSDT"), row("AUSDT")])
    assert not any(e["event"] == "ENTRY_CANDIDATE_RANKED" for e in v.events)
    assert "entry_ranking" not in v.checkpoint()


def test_gates_before_pool_and_no_future_snapshot_use():
    v = variant()
    v.config["entry_filter"] = {"min_delta_4h": "15"}
    rs = [row("PASSUSDT", 80, 20), row("GATEUSDT", 90, 10), row("HOLDUSDT", 1, 100)]
    rs[0]["future_snapshot"] = {"delta_4h": 10000, "relative_volume": 0.001}
    rs[1]["future_snapshot"] = {"delta_4h": 10000, "relative_volume": 0.001}
    assert signals(v, rs) == ["PASSUSDT"]
    e = next(e for e in v.events if e["event"] == "ENTRY_CANDIDATE_RANKED")
    assert e["candidate_pool_size"] == 1 and e["ranking_value"] == "10"


def test_quarantine_keeps_cash_marks_reservations_and_risk_view():
    v = variant()
    a = row("HELDUSDT")
    signals(v, [a])
    execute(v, [a])
    v.fresh_positions({}, START + 60000)
    assert stale.state(v.ledger.portfolio.positions()[0])["quarantined"]
    ledger = copy.deepcopy(v.ledger.checkpoint())
    view = v.decision_view()
    signals(v, [row("NEWUSDT")], START + H)
    assert v.ledger.checkpoint() == ledger and v.decision_view() == view
    assert view.available_cash <= v.ledger.portfolio.view().available_cash


def small_run(
    path, names, stop=None, off=False, missing=False, provider=None, competing=False
):
    raw = json.loads(GOLDEN.read_text())["public_config"]
    base = raw["variants"][0]
    if competing:
        base["risk_parameters"]["max_open_positions"] = 1
    raw["variants"] = [
        base
        | dict(name=n)
        | (
            {"entry_ranking": S}
            if n == "S"
            else {"entry_ranking": R}
            if n == "R"
            else {"entry_ranking": "OFF"}
            if off
            else {}
        )
        for n in names
    ]
    if missing:
        raw["variants"][0].pop("entry_filter", None)
    store = ReplayStore(path / "replay.db")
    rid = store.create(validate(raw))

    def check():
        cp = store.checkpoint(rid)
        if stop and cp and cp["next_time"] >= stop:
            raise InterruptedError("fixed fixture checkpoint")

    try:
        ReplayEngine(store, provider or Tape(), check).run(rid)
    except InterruptedError:
        pass
    return store, rid


def audit(store, rid, index):
    with store.connection() as db:
        return [
            json.loads(r[0])
            for r in db.execute(
                "SELECT data FROM replay_audit_events WHERE run_id=? AND variant=? ORDER BY id",
                (rid, index),
            )
        ]


def legacy_audit(events):
    return [
        {
            k: v
            for k, v in e.items()
            if not k.startswith("entry_ranking_")
            and k
            not in ("entry_rank", "entry_candidate_id", "entry_candidate_pool_size")
        }
        for e in events
        if e["event"] != "ENTRY_CANDIDATE_RANKED"
    ]


def test_legacy_and_score_control_bit_exact_against_independent_base(request, tmp_path):
    request.getfixturevalue("controlled")
    proof = json.loads(PROOF.read_text())
    for mode in ["missing", "off", "no_filter"]:
        st, rid = small_run(
            tmp_path / mode, "A", off=mode == "off", missing=mode == "no_filter"
        )
        assert core(st, rid, 0) == proof["core"]
        assert audit(st, rid, 0) == proof["events"]
        assert st.checkpoint(rid)["variants"][0] == proof["checkpoint"]
        assert st.get(rid)["variants"][0]["summary"] == proof["metrics"]
    st, rid = small_run(tmp_path / "AS", "AS")
    assert core(st, rid, 0) == core(st, rid, 1) == proof["core"]
    assert (
        st.get(rid)["variants"][0]["summary"] == st.get(rid)["variants"][1]["summary"]
    )
    assert (
        st.checkpoint(rid)["variants"][0]
        == st.checkpoint(rid)["variants"][1]
        == proof["checkpoint"]
    )
    assert legacy_audit(audit(st, rid, 1)) == audit(st, rid, 0) == proof["events"]


def test_companions_reversed_order_and_checkpoint_all_variants_bit_exact(
    request, tmp_path
):
    request.getfixturevalue("controlled")
    proof = json.loads(PROOF.read_text())
    full, a = small_run(tmp_path / "full", "ASR")
    reverse, b = small_run(tmp_path / "reverse", "RSA")
    resumed, c = small_run(tmp_path / "resumed", "ASR", stop=START + 8 * H)
    ReplayEngine(resumed, Tape()).run(c)
    for label in "ASR":
        i = "ASR".index(label)
        j = "RSA".index(label)
        assert core(full, a, i) == core(reverse, b, j) == core(resumed, c, i)
        assert (
            full.get(a)["variants"][i]["summary"]
            == reverse.get(b)["variants"][j]["summary"]
            == resumed.get(c)["variants"][i]["summary"]
        )
        assert (
            full.checkpoint(a)["variants"][i]
            == reverse.checkpoint(b)["variants"][j]
            == resumed.checkpoint(c)["variants"][i]
        )
        assert audit(full, a, i) == audit(reverse, b, j) == audit(resumed, c, i)
    assert core(full, a, 0) == proof["core"]


def test_shared_market_frame_immutable():
    from collections import defaultdict

    from test_replay_companion_isolation import RotatingUniverse, features, score

    from crypto_bot.config.settings import Settings
    from crypto_bot.replay.market_context import frame
    from crypto_bot.replay.universe import CandleHistory

    histories = {
        s: CandleHistory(Tape().hourly(s, START - 524 * H, START + 14 * H))
        for s in ["NEARUSDT", "OPUSDT"]
    }
    history = defaultdict(list)
    f = frame(
        RotatingUniverse(histories, None, 0),
        Settings(),
        START + 6 * H,
        history,
        score,
        features,
    )
    rows = f.rows()
    before = copy.deepcopy(rows)
    past = copy.deepcopy(history)
    signals(variant(), rows, START + 6 * H)
    assert f.rows() == before and history == past


@pytest.mark.parametrize("value", ["wrong", None, True])
def test_invalid_policy_rejected_per_field(value):
    raw = json.loads(ARTIFACT.read_text())
    raw["variants"][1]["entry_ranking"] = value
    with pytest.raises(ConfigValidationError) as e:
        validate(raw)
    assert e.value.errors[0]["field"] == "variants[1].entry_ranking"


def test_only_institutional_strategy_accepts_active_ranking():
    raw = json.loads(ARTIFACT.read_text())
    raw["variants"] = [dict(strategy="watchlist_reference_v1", entry_ranking=R)]
    with pytest.raises(ConfigValidationError) as e:
        validate(raw)
    assert e.value.errors[0]["field"] == "variants[0].entry_ranking"


def test_old_hashes_and_import_export_roundtrip_and_exact_frozen_config():
    raw = json.loads(ARTIFACT.read_text())
    cfg = validate(raw)
    assert validate(input_config(cfg)) == cfg
    assert (
        cfg["market_context_version"] == "ISOLATED_TOPN_V1"
        and "market_context_version" not in raw
    )
    assert cfg["dataset_role"] == "EXPLORATION" and cfg["universe_size"] == 20
    assert (
        cfg["stale_position_policy"] == stale.QUARANTINE
        and not cfg["fallback_5m"]
        and cfg["conservative"]
    )
    assert (cfg["start"], cfg["end"]) == (
        "2024-01-01T00:00:00Z",
        "2025-01-01T00:00:00Z",
    )
    assert [v.get("entry_ranking", "OFF") for v in cfg["variants"]] == ["OFF", S, R]
    a = cfg["variants"][0]
    for v in cfg["variants"]:
        assert {k: x for k, x in v.items() if k not in ("name", "entry_ranking")} == {
            k: x for k, x in a.items() if k != "name"
        }
        assert v.get("entry_admission", "OFF") == v.get("setup_rearm", "OFF") == "OFF"
        assert all(x is None for x in v["entry_filter"].values())
        assert (
            v["strategy"] == "institutional_style_v1"
            and v["exit_policy"] == "baseline_v1"
        )
    proof = json.loads(PROOF.read_text())
    old = json.loads(GOLDEN.read_text())["public_config"]
    v3 = json.loads(
        (
            ARTIFACT.parent / "institutional-entry-v3-2024-exploration.config.json"
        ).read_text()
    )
    assert config_hash(validate(old)) == proof["old_config_hash"]
    assert config_hash(validate(v3)) == proof["v3_config_hash"]
    for legacy in [old, v3]:
        off = copy.deepcopy(legacy)
        for v in off["variants"]:
            v["entry_ranking"] = "OFF"
        assert validate(off) == validate(legacy)
        assert validate(input_config(validate(legacy))) == validate(legacy)


def test_zero_negative_priority_is_valid_and_not_clamped():
    v = variant()
    signals(v, [row("ZEROUSDT"), row("NEGUSDT"), row("BADUSDT")])
    for s, r in v.pending:
        r["momentum"]["delta_4h"] = {"ZEROUSDT": 0, "NEGUSDT": -20, "BADUSDT": None}[
            s.symbol
        ]
    result = ranked(v.pending, R)
    assert [s.symbol for s, r in result] == ["ZEROUSDT", "NEGUSDT", "BADUSDT"]
    assert [r["_ranking_event"]["ranking_value"] for s, r in result] == [
        "0",
        "-10",
        None,
    ]


def test_competing_score_control_rank_changes_only_priority_and_isolation(
    request, monkeypatch, tmp_path
):
    from test_replay_companion_isolation import features as original_features

    import crypto_bot.replay.engine as engine

    request.getfixturevalue("controlled")

    def competing_features(candles, market, settings):
        f = original_features(candles, market, settings)
        hour = (candles[-1].close_time + 1 - START) / H
        f.update(
            test_score=80 + hour * 0.1,
            rsi=55,
            relative_volume=2 if market.symbol == "NEARUSDT" else 1,
        )
        return f

    monkeypatch.setattr(engine, "features", competing_features)
    full, a = small_run(tmp_path / "full", "ASR", competing=True)
    rev, b = small_run(tmp_path / "reverse", "RSA", competing=True)
    solo, c = small_run(tmp_path / "solo", "R", competing=True)
    paused, d = small_run(
        tmp_path / "paused", "ASR", stop=START + 8 * H, competing=True
    )
    ReplayEngine(paused, Tape()).run(d)
    assert core(full, a, 0) == core(full, a, 1)
    assert core(full, a, 0)["fills"][0]["symbol"] == "NEARUSDT"
    assert core(full, a, 2)["fills"][0]["symbol"] == "OPUSDT"
    assert any(
        e["candidate_pool_size"] == 2
        for e in audit(full, a, 1)
        if e["event"] == "ENTRY_CANDIDATE_RANKED"
    )
    assert core(full, a, 2) == core(solo, c, 0)
    for i, label in enumerate("ASR"):
        j = "RSA".index(label)
        assert core(full, a, i) == core(rev, b, j) == core(paused, d, i)
        assert audit(full, a, i) == audit(rev, b, j) == audit(paused, d, i)
        assert full.checkpoint(a)["variants"][i] == paused.checkpoint(d)["variants"][i]
    assert legacy_audit(audit(full, a, 1)) == audit(full, a, 0)


def test_future_candle_changes_cannot_change_ranked_prefix(request, tmp_path):
    request.getfixturevalue("controlled")
    cutoff = START + 8 * H

    class FutureTape(Tape):
        def hourly(self, symbol, start, end):
            return [
                (
                    replace(
                        c,
                        open=c.open * 3,
                        close=c.close * 3,
                        high=c.high * 3,
                        low=c.low * 3,
                        volume=c.volume * 100,
                    ),
                    qv * 100,
                )
                if c.open_time >= cutoff
                else (c, qv)
                for c, qv in super().hourly(symbol, start, end)
            ]

    before, a = small_run(tmp_path / "base", "ASR", stop=cutoff)
    changed, b = small_run(
        tmp_path / "future", "ASR", stop=cutoff, provider=FutureTape()
    )
    for i in range(3):
        assert (
            before.checkpoint(a)["variants"][i] == changed.checkpoint(b)["variants"][i]
        )
        assert audit(before, a, i) == audit(changed, b, i)
        with before.connection() as db:
            orders_before = [
                r[0]
                for r in db.execute(
                    "select data from replay_orders where run_id=? and variant=? order by id",
                    (a, i),
                )
            ]
        with changed.connection() as db:
            orders_after = [
                r[0]
                for r in db.execute(
                    "select data from replay_orders where run_id=? and variant=? order by id",
                    (b, i),
                )
            ]
        assert orders_before == orders_after
