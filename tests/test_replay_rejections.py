"""Rejection observability must not change fills, guards or production decisions."""

import json
from copy import deepcopy
from dataclasses import asdict
from unittest.mock import patch

import pytest
from test_replay import START, Synthetic, config, row
from test_replay_integration import store, web_case  # noqa: F401

from crypto_bot.replay.clock import at
from crypto_bot.replay.engine import ReplayEngine, Variant
from crypto_bot.replay.exporter import payload
from crypto_bot.replay.rejections import details, from_audit, legacy_group
from crypto_bot.risk.manager import RiskManager
from crypto_bot.trading.domain import D, StrategySignal


@pytest.mark.parametrize(
    "reason,category",
    [
        ("daily loss limit reached", "daily_loss"),
        ("drawdown limit reached", "drawdown"),
        ("total exposure limit reached", "total_exposure"),
        ("symbol exposure limit reached", "symbol_exposure"),
        ("max open positions reached", "max_positions"),
        ("invalid stop distance", "invalid_stop_distance"),
        ("duplicate open position; pyramiding disabled", "duplicate_position"),
        ("insufficient free balance", "insufficient_free_balance"),
        (
            "sized order below minimum value or insufficient free balance/exposure room",
            "below_minimum_order_or_balance_exposure_room",
        ),
        ("nonpositive portfolio equity", "nonpositive_equity"),
        ("take profit already reached at current quote", "take_profit_already_reached"),
        ("paper trading OFF", "trading_disabled"),
        ("HOLD; no order requested", "hold_no_order"),
        ("no open long position", "no_open_position"),
        ("Fresh portfolio marks unavailable", "missing_portfolio_marks"),
        ("MISSING_1M_EXECUTION_CANDLE; fallback disabled", "missing_execution_minute"),
        ("MISSING_EXECUTION_CANDLE", "missing_execution_candle"),
        ("new unknown reason", "other"),
    ],
)
def test_exact_reason_classification_preserves_unknown_strings(reason, category):
    result = details({reason: 3})
    assert result["blocked_entry_reasons"] == {reason: 3}
    assert result["blocked_entry_categories"] == {category: 3}
    assert result["blocked_entry_attempts"] == 3


def test_legacy_aggregates_stay_compatible_without_masking_minimum_value():
    reason = (
        "sized order below minimum value or insufficient free balance/exposure room"
    )
    assert legacy_group(reason) == "exposure"
    assert details({reason: 92})["blocked_entry_categories"] == {
        "below_minimum_order_or_balance_exposure_room": 92
    }


def candle(time=START, **changes):
    return (
        dict(
            time=time,
            close_time=time + 59999,
            open=100,
            high=100.5,
            low=99.5,
            close=100,
            interval="1m",
        )
        | changes
    )


def buy(v, symbol="BTCUSDT", now=START, datasets=None):
    v.signals([row() | dict(symbol=symbol)], {symbol}, now)
    v.execute_signals(
        datasets if datasets is not None else {symbol: {now: candle(now)}}, now, False
    )


@pytest.mark.parametrize("policy", ["baseline_v1", "fixed_tp", "r_tp", "trailing_pct"])
def test_identical_entry_and_rejection_accounting_for_every_exit_policy(policy):
    v = Variant(config(variants=[dict(exit_policy=policy)])["variants"][0], START)
    buy(v)
    assert len(v.execution.fills) == 1
    assert v.execution.fills[0]["quantity"] == "1.997003496253"
    buy(v, "ETHUSDT", START + 3600000, {})
    buy(v, "ETHUSDT", START + 7200000)  # BTC's mark is missing, not ETH's.
    assert v.blocked["other"] == 2
    assert dict(v.blocked_reasons) == {
        "MISSING_1M_EXECUTION_CANDLE; fallback disabled": 1,
        "Fresh portfolio marks unavailable": 1,
    }
    assert v.events[-1]["missing_mark_symbols"] == ["BTCUSDT"]
    assert v.events[-1]["action"] == "BUY"
    assert len(v.ledger.portfolio.positions()) == 1


def test_baseline_retains_unpriced_position_and_resumes_exits_when_data_returns():
    v = Variant(config()["variants"][0], START)
    buy(v)
    position = v.ledger.portfolio.positions()[0]
    v.bar("BTCUSDT", candle(high=104, low=99.5), START + 60000, True)
    assert not v.closed and v.trade(position, START + 3600000)["status"] == "OPEN"
    buy(v, "ETHUSDT", START + 3600000)
    assert v.blocked["other"] == 1 and not v.closed
    assert position.original_stop == D(98)
    now = START + 7200000
    rows = [row() | dict(total_score=40), row() | dict(symbol="ETHUSDT")]
    v.signals(rows, {"ETHUSDT"}, now)
    v.execute_signals(
        {s: {now: candle(now)} for s in ("BTCUSDT", "ETHUSDT")}, now, False
    )
    assert len(v.closed) == 1 and v.closed[0]["exit_reason"] == "STRATEGY"
    assert [p.symbol for p in v.ledger.portfolio.positions()] == ["ETHUSDT"]
    assert len([f for f in v.execution.fills if f["side"] == "BUY"]) == len(
        v.closed
    ) + len(v.ledger.portfolio.positions())
    assert v.blocked["other"] == 1


@pytest.mark.parametrize("policy", ["fixed_tp", "r_tp", "trailing_pct"])
def test_overlay_can_close_before_data_loss_without_bypassing_fresh_mark_guard(policy):
    v = Variant(config(variants=[dict(exit_policy=policy)])["variants"][0], START)
    buy(v)
    v.bar("BTCUSDT", candle(high=104, low=99.5, close=100), START + 60000, True)
    assert len(v.closed) == 1
    assert v.closed[0]["exit_reason"] in ("FIXED_TP", "R_TP", "TRAILING")
    buy(v, "ETHUSDT", START + 3600000)
    assert [p.symbol for p in v.ledger.portfolio.positions()] == ["ETHUSDT"]
    assert v.blocked["other"] == 0


def test_hold_sell_and_duplicate_are_not_confused_with_new_buy_attempts():
    v = Variant(config()["variants"][0], START)
    buy(v)
    buy(v)  # Existing position produces HOLD, not another risk rejection.
    assert len(v.execution.fills) == 1 and not sum(v.blocked.values())
    signal = StrategySignal(
        "BTCUSDT", "BUY", D(1), D(100), D(98), at(START), "test", ("synthetic signal",)
    )
    v.pending = [(signal, row())]
    v.execute_signals({"BTCUSDT": {START: candle()}}, START, False)
    assert v.blocked_reasons["duplicate open position; pyramiding disabled"] == 1
    rejected_before = deepcopy(v.blocked)
    v.pending = [
        (
            StrategySignal(
                "ETHUSDT",
                "SELL",
                D(1),
                D(100),
                None,
                at(START),
                "test",
                ("synthetic signal",),
            ),
            row(),
        )
    ]
    v.execute_signals({}, START, False)  # Missing SELL execution is not a blocked BUY.
    v.pending = [
        (
            StrategySignal(
                "ETHUSDT",
                "SELL",
                D(1),
                D(100),
                None,
                at(START),
                "test",
                ("synthetic signal",),
            ),
            row(),
        )
    ]
    v.execute_signals({"ETHUSDT": {START: candle()}}, START, False)
    assert v.blocked == rejected_before
    assert v.events[-1]["decision"]["reasons"] == ["no open long position"] or v.events[
        -1
    ]["decision"]["reasons"] == ("no open long position",)


def old_events():
    return [
        dict(event="ENTRY_FILL", symbol="BTCUSDT"),
        dict(
            event="NO_FILL",
            symbol="BTCUSDT",
            reason="MISSING_1M_EXECUTION_CANDLE; fallback disabled",
        ),  # SELL
        dict(
            event="RISK_REJECTED",
            symbol="ETHUSDT",
            reasons=["Fresh portfolio marks unavailable"],
        ),
        dict(
            event="NO_FILL",
            symbol="ETHUSDT",
            reason="MISSING_1M_EXECUTION_CANDLE; fallback disabled",
        ),  # BUY
        dict(
            event="RISK_DECISION", symbol="ETHUSDT", reasons=["invalid stop distance"]
        ),
        dict(
            event="RISK_DECISION", symbol="ETHUSDT", reasons=["no open long position"]
        ),
        dict(event="EXIT_FILL", symbol="BTCUSDT"),
        dict(
            event="NO_FILL",
            symbol="BTCUSDT",
            reason="MISSING_1M_EXECUTION_CANDLE; fallback disabled",
        ),  # BUY again
    ]


def test_legacy_audit_infers_actions_and_reports_incomplete_data():
    result = from_audit(old_events(), dict(other=4))
    assert result["blocked_entry_reasons"] == {
        "Fresh portfolio marks unavailable": 1,
        "MISSING_1M_EXECUTION_CANDLE; fallback disabled": 2,
        "invalid stop distance": 1,
    }
    assert result["blocked_entry_breakdown_complete"]
    result = from_audit(old_events(), dict(other=5))
    assert not result["blocked_entry_breakdown_complete"]
    assert result["blocked_entry_unresolved_attempts"] == 1
    assert not from_audit(old_events(), dict(max_positions=4))[
        "blocked_entry_breakdown_complete"
    ]


def save_legacy(db):
    run = db.create(config())
    for event in old_events():
        value = deepcopy(event)
        if value["event"] == "RISK_DECISION":
            value["decision"] = dict(approved=False, reasons=value.pop("reasons"))
        with db.connection() as sql:
            sql.execute(
                "INSERT INTO replay_audit_events(run_id,variant,data) VALUES(?,0,?)",
                (run, json.dumps(value)),
            )
    db.complete(
        run, [dict(metrics=dict(blocked_entries=dict(other=4)), breakdowns={})], {}
    )
    return run


def test_existing_results_and_compact_exports_enriched_without_rewriting(request):
    db = request.getfixturevalue("store")
    run = save_legacy(db)
    with db.connection() as sql:
        before = [tuple(r) for r in sql.execute("SELECT * FROM replay_variants")]
        audit_before = [
            tuple(r) for r in sql.execute("SELECT * FROM replay_audit_events")
        ]
    for compact in (False, True):
        metrics = payload(db, run, compact=compact)["summary"]
        assert metrics["blocked_entries"] == dict(other=4)
        assert metrics["blocked_entry_categories"]["missing_portfolio_marks"] == 1
        assert metrics["blocked_entry_attempts"] == 4
        assert metrics["blocked_entry_breakdown_complete"]
    with db.connection() as sql:
        assert [
            tuple(r) for r in sql.execute("SELECT * FROM replay_variants")
        ] == before
        assert [
            tuple(r) for r in sql.execute("SELECT * FROM replay_audit_events")
        ] == audit_before


def test_results_api_returns_legacy_reason_breakdown(request):
    client, db, _ = request.getfixturevalue("web_case")
    run = save_legacy(db)
    response = client.get(f"/api/replay/runs/{run}/results")
    assert response.status_code == 200
    assert (
        response.json["summary"]["metrics"]["blocked_entry_categories"][
            "missing_portfolio_marks"
        ]
        == 1
    )
    assert client.get(f"/replay/runs/{run}").status_code == 200


def test_reporting_disabled_and_enabled_runs_have_same_trades_equity_and_risk(tmp_path):
    from crypto_bot.replay.models import ReplayStore

    results = []
    for reporting in (False, True):
        db = ReplayStore(tmp_path / str(reporting) / "replay.db")
        run = db.create(config())
        if reporting:
            ReplayEngine(db, Synthetic()).run(run)
        else:
            with patch.object(
                Variant, "block_entry", lambda self, signal, reasons: None
            ):
                ReplayEngine(db, Synthetic()).run(run)
        results.append(db.data(run))
    assert results[0] == results[1]


def test_replay_uses_unchanged_production_risk_decision():
    v = Variant(config()["variants"][0], START)
    v.signals([row()], {"BTCUSDT"}, START)
    signal, _ = v.pending[0]
    direct = RiskManager(v.settings).evaluate(
        signal, v.ledger.portfolio.view(), True, entry_price=D(100)
    )
    with patch.object(v.risk, "evaluate", wraps=v.risk.evaluate) as evaluate:
        v.execute_signals({"BTCUSDT": {START: candle()}}, START, False)
    assert evaluate.call_count == 1
    assert v.events[1]["decision"] == asdict(direct)
    assert v.execution.fills[0]["quantity"] == str(direct.quantity)


def test_invalid_execution_stop_is_visible_but_sizing_is_unchanged():
    v = Variant(config()["variants"][0], START)
    buy(v, datasets={"BTCUSDT": {START: candle(open=97)}})
    assert not v.execution.fills
    assert v.events[-1]["decision"]["reasons"] == ("invalid stop distance",)
    assert dict(v.blocked_reasons) == {"invalid stop distance": 1}
    assert v.blocked["other"] == 1


def test_old_checkpoint_resumes_with_complete_reasons(tmp_path):
    from crypto_bot.replay.models import ReplayStore

    results = []

    class Restart(Exception):
        pass

    for restart in (False, True):
        db = ReplayStore(tmp_path / str(restart) / "replay.db")
        run = db.create(
            config(variants=[dict(risk_parameters=dict(max_open_positions=1))])
        )
        if restart:

            def check():
                if db.get(run)["progress"].get("processed_timestamps", 0) >= 12:
                    raise Restart()

            with pytest.raises(Restart):
                ReplayEngine(db, Synthetic(), check).run(run)
            checkpoint = db.checkpoint(run)
            for variant in checkpoint["variants"]:
                variant.pop("blocked_reasons")
            db.update(run, checkpoint=checkpoint)
        ReplayEngine(db, Synthetic()).run(run)
        summary = db.get(run)["variants"][0]["summary"]["metrics"]
        assert summary["blocked_entry_attempts"] > 0
        assert summary["blocked_entry_breakdown_complete"]
        assert "max open positions reached" in summary["blocked_entry_reasons"]
        assert (
            db.checkpoint(run)["variants"][0]["blocked_reasons"]
            == summary["blocked_entry_reasons"]
        )
        results.append((db.data(run), summary))
    assert results[0] == results[1]


def test_instrumented_exit_processing_still_requires_closed_candle():
    v = Variant(config()["variants"][0], START)
    buy(v)
    before = deepcopy(v.ledger.checkpoint())
    with pytest.raises(ValueError, match="CLOSED"):
        v.bar("BTCUSDT", candle(low=90), START, True)
    assert v.ledger.checkpoint() == before
    assert not v.closed
    assert not sum(v.blocked.values())
