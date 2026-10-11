"""Fixed-data matched-entry regressions; never call Binance or a trading API."""

import copy
import json
import os

import pytest
from test_replay import START, Synthetic, config, row
from test_replay_integration import Stops, token, web_case  # noqa: F401

from crypto_bot.replay.clock import at
from crypto_bot.replay.config import ConfigValidationError, settings_for
from crypto_bot.replay.engine import ReplayEngine, Variant
from crypto_bot.replay.exits import observe, policy
from crypto_bot.replay.registry import EXITS
from crypto_bot.replay.shadow_config import validate
from crypto_bot.replay.shadow_engine import HOUR, MINUTE, ShadowEngine, ShadowPosition
from crypto_bot.replay.shadow_metrics import analysis, export
from crypto_bot.replay.shadow_store import ShadowStore, canonical_entries
from crypto_bot.trading.domain import D, q


def entry(symbol="BTCUSDT", ident="entry-1"):
    return dict(
        shadow_entry_id=ident,
        symbol=symbol,
        entry_time=at(START).isoformat() + "Z",
        entry_fill_price="100",
        original_stop_price="98",
        risk_amount="2",
        quantity="1",
        notional="100",
        entry_fee="0.1",
        entry_slippage="0.05",
        atr=1,
        atr_pct=1,
        entry_score=80,
        delta_1h=1,
        delta_4h=4,
        delta_24h=5,
        rsi=55,
        relative_volume=1.5,
        momentum=2,
        range_position=0.7,
        btc_regime="BULLISH",
        asset_group="BTC",
    )


def definitions():
    return [
        dict(id=k, name=k, exit_policy=k, exit_parameters=dict(v))
        for k, v in EXITS.items.items()
    ]


def bar(t=START, o=100, h=101, lo=99, c=100):
    return dict(
        time=t,
        close_time=t + MINUTE - 1,
        open=o,
        high=h,
        low=lo,
        close=c,
        interval="1m",
    )


def job(entries=None, policies=None):
    cfg = config(end="2025-01-01T02:00:00Z")
    return dict(
        config=dict(
            anchor_run_id="a" * 32, anchor_variant=0, policies=policies or definitions()
        ),
        source=dict(
            config=cfg,
            variant=cfg["variants"][0],
            entries=entries or [entry()],
            metadata={},
        ),
        metadata={},
    )


class Fixed(Synthetic):
    def minutes(self, symbol, start, end, interval="1m"):
        return {t: bar(t, h=103, lo=97) for t in range(start, end, MINUTE)}


@pytest.mark.parametrize("definition", definitions(), ids=list(EXITS.items))
def test_every_registry_policy_receives_identical_entry_and_reuses_exit_path(
    definition,
):
    e = entry()
    p = ShadowPosition(
        e,
        definition,
        settings_for(config()["variants"][0]),
        "RESEARCH_QUARANTINE_STALE",
    )
    candle = bar(h=104, lo=97)
    old = copy.deepcopy(p.p)
    hit, ambiguous, _ = observe(
        candle,
        old,
        policy(definition["exit_policy"], definition["exit_parameters"]),
        True,
    )
    p.bar(candle, START + MINUTE, True)
    result = p.export(START + MINUTE)
    assert result["entry_fill_price"] == e["entry_fill_price"]
    assert result["original_stop"] == e["original_stop_price"]
    assert result["entry_atr"] == e["atr"]
    assert result["canonical_notional"] == e["notional"]
    assert result["entry_fee"] == e["entry_fee"]
    assert result["exit_reason"] == hit[1]
    assert result["intrabar_ambiguity"] == ambiguous


def test_fee_slippage_return_and_canonical_risk_normalization():
    p = ShadowPosition(
        entry(),
        definitions()[0],
        settings_for(config()["variants"][0]),
        "STRICT_FRESH_MARKS",
    )
    p.close(D(105), START + MINUTE, "STRATEGY", "TEST")
    r = p.export(START + MINUTE)
    price = q(D(105) * D(".9995"))
    fee = q(price * D(".001"))
    pnl = q(price - fee - D("100.1"))
    assert D(r["exit_price"]) == price and D(r["exit_fee"]) == fee
    assert D(r["realized_pnl"]) == pnl
    assert D(r["slippage"]) == D(".05") + D(105) - price
    assert r["return_pct"] == pytest.approx(float(pnl / D("100.1") * 100))
    assert r["r_multiple"] == pytest.approx(float(pnl / D(2)))


@pytest.mark.parametrize("conservative", [True, False])
def test_ambiguous_bar_uses_existing_intrabar_policy(conservative):
    d = next(d for d in definitions() if d["id"] == "fixed_tp")
    p = ShadowPosition(
        entry(), d, settings_for(config()["variants"][0]), "STRICT_FRESH_MARKS"
    )
    p.bar(bar(h=104, lo=97), START + MINUTE, conservative)
    assert p.ambiguous
    assert p.result["exit_reason"] == ("ORIGINAL_STOP" if conservative else "FIXED_TP")


def test_no_future_or_preentry_candle_and_no_entry_time_exit():
    p = ShadowPosition(
        entry(),
        definitions()[0],
        settings_for(config()["variants"][0]),
        "STRICT_FRESH_MARKS",
    )
    with pytest.raises(ValueError):
        p.bar(bar(), START, True)
    with pytest.raises(ValueError):
        p.bar(bar(START - MINUTE), START + MINUTE, True)
    with pytest.raises(ValueError):
        p.close(101, START, "STRATEGY", "TEST")
    assert p.result is None


def test_stale_resume_does_not_fill_or_force_close_and_is_incomplete():
    p = ShadowPosition(
        entry("FRONTUSDT"),
        definitions()[0],
        settings_for(config()["variants"][0]),
        "RESEARCH_QUARANTINE_STALE",
    )
    p.fresh(False, START + MINUTE)
    assert p.export(START + HOUR)["shadow_status"] == "SHADOW_OPEN_STALE"
    with pytest.raises(ValueError):
        p.close(100, START + HOUR, "FORCED", "TEST")
    p.fresh(True, START + HOUR)
    assert p.result is None and stale_events(p) == [
        "POSITION_STALE",
        "POSITION_RESUMED",
    ]
    p.bar(bar(START + HOUR, h=101, lo=97), START + HOUR + MINUTE, True)
    assert p.export(START + HOUR + MINUTE)["status"] == "INCOMPLETE_DATA"
    assert not p.export(START + HOUR + MINUTE)["comparison_eligible"]


def stale_events(p):
    return [e["event"] for e in p.events]


def test_stale_symbol_does_not_stop_other_symbols_and_no_future_coverage_exit():
    class Gaps(Fixed):
        def minutes(self, symbol, start, end, interval="1m"):
            if symbol == "FRONTUSDT":
                return {}
            return super().minutes(symbol, start, end, interval)

    j = job([entry("FRONTUSDT", "front"), entry("BTCUSDT", "btc")], [definitions()[0]])
    result = ShadowEngine(Gaps()).run(j)
    front, btc = result["outcomes"]
    assert front["status"] == "OPEN_STALE" and front["exit_time"] is None
    assert front["last_valid_mark_price"] == "100"
    assert btc["status"] == "CLOSED"
    assert result["incomplete_entry_count"] == 1
    assert result["metadata"]["strict_no_lookahead"]


def test_policy_order_determinism_and_no_shadow_equity():
    j = job()
    first = ShadowEngine(Fixed()).run(j)
    j["config"]["policies"].reverse()
    second = ShadowEngine(Fixed()).run(j)

    def sort(r):
        return sorted(r["outcomes"], key=lambda x: x["policy_id"])

    assert sort(first) == sort(second)
    assert first["policy_summaries"] == second["policy_summaries"]
    assert first["pairwise"] == second["pairwise"]
    assert "final_equity" not in json.dumps(export(first, compact=True))


def test_incomplete_outcomes_are_not_paired_wins_losses():
    e = entry()
    defs = definitions()[:2]
    rows = []
    for d in defs:
        p = ShadowPosition(
            e, d, settings_for(config()["variants"][0]), "RESEARCH_QUARANTINE_STALE"
        )
        if d == defs[0]:
            p.fresh(False, START + MINUTE)
        else:
            p.close(105, START + MINUTE, "TEST", "TEST")
        rows.append(p.export(START + MINUTE))
    result = analysis([e], rows)
    pair = result["pairwise"][0]
    assert pair["matched_entries"] == 0 and pair["incomplete"] == 1
    assert pair["a_wins"] == pair["b_wins"] == pair["ties"] == 0
    assert result["common_comparable_entry_count"] == 0
    assert result["policy_summaries"][defs[0]["id"]]["win_rate"] is None


def test_shadow_cannot_affect_anchor_cash_slots_or_next_buy():
    v = Variant(config()["variants"][0], START)
    v.signals([row()], {"BTCUSDT"}, START)
    v.execute_signals({"BTCUSDT": {START: bar()}}, START, False)
    before = copy.deepcopy(v.checkpoint())
    e = entry()
    for d in definitions():
        p = ShadowPosition(e, d, v.settings, "STRICT_FRESH_MARKS")
        p.bar(bar(h=104, lo=97), START + MINUTE, True)
    assert v.checkpoint() == before
    control = Variant(config()["variants"][0], START)
    control.ledger.restore(json.loads(json.dumps(before["ledger"], default=str)))
    control.execution.next_order = before["next_order"]
    r = row()
    r["symbol"] = "ETHUSDT"
    for candidate in (v, control):
        candidate.signals([r], {"ETHUSDT"}, START + HOUR)
        candidate.execute_signals(
            {
                "BTCUSDT": {START + HOUR: bar(START + HOUR)},
                "ETHUSDT": {START + HOUR: bar(START + HOUR)},
            },
            START + HOUR,
            False,
        )
    assert v.execution.fills[-1] == control.execution.fills[-1]


def completed_source(db):
    run = db.create(config(end="2025-01-01T04:00:00Z"))
    db.claim("test")
    ReplayEngine(db, Stops()).run(run)
    return run


def test_web_validate_start_exports_and_production_isolation(request):
    client, source, production = request.getfixturevalue("web_case")
    ident = completed_source(source)
    raw = dict(
        anchor_run_id=ident,
        anchor_variant=0,
        policies=[{"id": "baseline", "exit_policy": "baseline_v1"}],
    )
    hdr = token(client)
    assert client.get("/replay/shadow-exit").status_code == 200
    assert (
        client.post(
            "/api/replay/shadow/config/validate", json=raw, headers=hdr
        ).status_code
        == 200
    )
    path = source.path.parent / "shadow-exit.db"
    store = ShadowStore(path)
    assert store.jobs() == []  # Validation never starts a job.
    assert client.post("/api/replay/shadow/runs", json=raw).status_code == 403
    response = client.post("/api/replay/shadow/runs", json=raw, headers=hdr)
    assert response.status_code == 202
    run = response.json["run_id"]
    job_value = store.get(run, True)
    before = source.get(ident)
    result = ShadowEngine(Stops()).run(job_value)
    store.update(run, status="COMPLETED", result=result)
    assert source.get(ident) == before
    assert len(result["canonical_entries"]) == len(canonical_entries(source, ident, 0))
    assert client.get("/replay/shadow-exit/" + run).status_code == 200
    for kind in ("compact.json", "json"):
        response = client.get(f"/api/replay/shadow/runs/{run}/export.{kind}")
        assert response.status_code == 200
        assert response.json["metadata"]["portfolio_performance"] is False
    with production.engine.connect() as c:
        assert (
            c.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE 'shadow_%'"
            ).scalar()
            == 0
        )


@pytest.mark.parametrize(
    "bad",
    [
        dict(anchor_variant=99),
        dict(policies=[{"exit_policy": "unknown"}]),
        dict(
            policies=[{"exit_policy": "fixed_tp", "exit_parameters": {"tp_pct": "NaN"}}]
        ),
        dict(policies=[]),
        dict(policies=[{"id": "same"}, {"id": "same"}]),
    ],
)
def test_invalid_shadow_config_uses_shared_validator(request, bad):
    _, source, _ = request.getfixturevalue("web_case")
    ident = completed_source(source)
    raw = dict(anchor_run_id=ident, policies=[{}]) | bad
    with pytest.raises(ConfigValidationError):
        validate(raw, source)


def test_shadow_storage_rejects_production_alias(tmp_path):
    production = tmp_path / "market.db"
    production.write_bytes(b"untouched")
    os.link(production, tmp_path / "shadow-exit.db")
    with pytest.raises(ValueError):
        ShadowStore(tmp_path / "shadow-exit.db")
    assert production.read_bytes() == b"untouched"


def test_completed_shadow_baseline_matches_normal_replay_actual_exits(request):
    _, source, _ = request.getfixturevalue("web_case")
    run = completed_source(source)
    cfg = validate(
        dict(
            anchor_run_id=run, policies=[{"id": "base", "exit_policy": "baseline_v1"}]
        ),
        source,
    )
    store = ShadowStore(source.path.parent / "shadow-exit.db")
    ident = store.create(cfg, source)
    result = ShadowEngine(Stops()).run(store.get(ident, True))
    original, _ = source.data(run)
    lookup = {t["position_id"]: t for t in original}
    for e, out in zip(result["canonical_entries"], result["outcomes"]):
        t = lookup[e["anchor_position_id"]]
        assert out["exit_time"] == t["exit_time"]
        assert out["exit_price"] == t["exit_price"]
        assert out["realized_pnl"] == t["realized_pnl"]


def test_restart_requeues_only_shadow_job_and_source_is_immutable(request):
    _, source, _ = request.getfixturevalue("web_case")
    run = completed_source(source)
    store = ShadowStore(source.path.parent / "shadow-exit.db")
    ident = store.create(
        validate(dict(anchor_run_id=run, policies=[{}]), source), source
    )
    before = source.get(run)
    assert store.claim() == ident
    store.recover()
    assert store.get(ident)["status"] == "QUEUED"
    assert source.get(run) == before


def test_strategy_exits_match_normal_anchor_without_entry_history_feedback(request):
    _, source, _ = request.getfixturevalue("web_case")
    run = source.create(config())
    source.claim("normal")
    ReplayEngine(source, Synthetic()).run(run)
    cfg = validate(dict(anchor_run_id=run, policies=[{"id": "baseline"}]), source)
    store = ShadowStore(source.path.parent / "shadow-exit.db")
    ident = store.create(cfg, source)
    result = ShadowEngine(Synthetic()).run(store.get(ident, True))
    original, _ = source.data(run)
    lookup = {t["position_id"]: t for t in original}
    assert result["canonical_entry_count"] > 0
    for e, out in zip(result["canonical_entries"], result["outcomes"]):
        t = lookup[e["anchor_position_id"]]
        assert out["exit_time"] == t["exit_time"]
        assert out["exit_price"] == t["exit_price"]
        if t["status"] == "CLOSED":
            assert out["realized_pnl"] == t["realized_pnl"]


def test_future_archive_rows_cannot_change_current_exit():
    from crypto_bot.data.base import Candle

    class Future(Synthetic):
        def hourly(self, symbol, start, end):
            rows = super().hourly(symbol, start, end)
            rows.append((Candle(end + HOUR, end + 2 * HOUR - 1, 1, 1, 1, 1, 1), 1))
            return rows

    j = job(policies=[definitions()[0]])
    first = ShadowEngine(Synthetic()).run(j)
    second = ShadowEngine(Future()).run(j)
    assert first["outcomes"] == second["outcomes"]


def test_missing_current_minute_cannot_replay_an_older_minute():
    class OneMinute(Fixed):
        def minutes(self, symbol, start, end, interval="1m"):
            return {START: bar(h=101, lo=99)} if start <= START < end else {}

    result = ShadowEngine(OneMinute()).run(job(policies=[definitions()[0]]))
    out = result["outcomes"][0]
    assert out["status"] == "OPEN_STALE"
    assert out["last_valid_mark_time"] == at(START + MINUTE).isoformat() + "Z"
    assert out["exit_time"] is None


def test_pinned_missing_archive_never_refreshes_or_uses_future_coverage(tmp_path):
    from unittest.mock import Mock

    from crypto_bot.replay.cache import CandleCache
    from crypto_bot.replay.shadow_data import ShadowHistoricalData

    cache = CandleCache(tmp_path / "cache")
    key = "data/spot/monthly/klines/FRONTUSDT/1m/FRONTUSDT-1m-2024-09.zip"
    cache.missing(key)
    with cache.connect() as db:
        db.execute("UPDATE objects SET checked_at=0")
    http = Mock()
    provider = ShadowHistoricalData(cache, {key: None}, session=http)
    assert not provider.object("FRONTUSDT", "1m", "monthly", "2024-09")
    http.get.assert_not_called()
    assert provider.used == {key} and provider.missing == 1


def test_pinned_archive_mismatch_fails_instead_of_silent_revision_change(tmp_path):
    from crypto_bot.replay.cache import CandleCache
    from crypto_bot.replay.shadow_data import ShadowHistoricalData

    cache = CandleCache(tmp_path / "cache")
    key = "data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-01.zip"
    cache.put("BTCUSDT", "1m", [], key, "actual-sha")
    provider = ShadowHistoricalData(cache, {key: "anchor-sha"})
    with pytest.raises(ValueError, match="revision unavailable or changed"):
        provider.object("BTCUSDT", "1m", "monthly", "2024-01")


def test_shadow_worker_completes_without_touching_source_and_failure_is_job_local(
    request,
):
    from crypto_bot.replay.cache import CandleCache
    from crypto_bot.replay.shadow_worker import once
    from crypto_bot.replay.worker import ReplayWorker

    _, source, _ = request.getfixturevalue("web_case")
    run = completed_source(source)
    store = ShadowStore(source.path.parent / "shadow-exit.db")
    cfg = validate(dict(anchor_run_id=run, policies=[{}]), source)
    first = store.create(cfg, source)
    worker = ReplayWorker(source, CandleCache(source.path.parent / "cache"), Stops)
    before = source.get(run)
    assert once(worker)
    assert store.get(first)["status"] == "COMPLETED"
    assert source.get(run) == before and worker.active is None

    class Offline(Stops):
        def hourly(self, *args):
            raise TimeoutError("fixed offline test")

    second = store.create(cfg, source)
    worker.provider_factory = Offline
    assert once(worker)  # A failed shadow job does not kill the replay worker.
    assert store.get(second)["status"] == "FAILED"
    assert worker.active is None and source.get(run) == before
