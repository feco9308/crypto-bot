"""Independent fixed quotes prove candle/signal and execution price separation."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
import requests
from conftest import NOW
from sqlalchemy import select
from test_paper_core import context, seed

from crypto_bot.execution.paper import PaperExecutionService
from crypto_bot.portfolio.service import PortfolioService
from crypto_bot.storage.migrate import upgrade_database
from crypto_bot.storage.models import Decision, ScannerRun, Snapshot
from crypto_bot.storage.paper_models import (
    PaperFill,
    PaperOrder,
    PaperPosition,
    RiskRecord,
    SignalRecord,
    SymbolTradePermission,
)
from crypto_bot.trading.config import PaperSettings
from crypto_bot.trading.domain import D
from crypto_bot.trading.engine import PaperEngine
from crypto_bot.trading.quotes import BinancePublicQuotes, MarketQuote
from crypto_bot.trading.repository import PaperRepository
from crypto_bot.trading.strategy import ReferenceStrategy
from crypto_bot.web.app import create_app


class FixedQuotes:
    def __init__(self, bid=101, ask=102, when=NOW):
        self.quotes = {"BTCUSDT": MarketQuote("BTCUSDT", bid, ask, when, "FIXED_TEST")}
        self.calls = []

    def get_quotes(self, symbols):
        self.calls.append(set(symbols))
        return {
            symbol: quote for symbol, quote in self.quotes.items() if symbol in symbols
        }

    def set(self, bid, ask=None, when=NOW, symbol="BTCUSDT"):
        self.quotes[symbol] = MarketQuote(
            symbol, bid, bid if ask is None else ask, when, "FIXED_TEST"
        )


@pytest.fixture
def pipeline(database, settings):
    upgrade_database(database)
    seed(database)
    repo = PaperRepository(database, PaperSettings())
    repo.initialize_account(NOW)
    repo.set_enabled(True, NOW)
    provider = FixedQuotes()
    return repo, PaperEngine(repo, settings, quote_provider=provider), provider


def test_fill_risk_and_mark_use_quotes_but_signal_keeps_closed_candle(
    pipeline, database
):
    repo, engine, quotes = pipeline
    with database.session() as s:
        snapshot = s.scalar(select(Snapshot))
        before = (
            snapshot.features.copy(),
            snapshot.total_score,
            snapshot.score_momentum.copy(),
        )
    engine.run_once(NOW)
    with database.session() as s:
        signal, order, fill, position, risk = [
            s.scalar(select(model))
            for model in (
                SignalRecord,
                PaperOrder,
                PaperFill,
                PaperPosition,
                RiskRecord,
            )
        ]
        assert signal.entry_reference == D(100) and signal.stop_reference == D(98)
        assert order.reference_price == D(102)
        assert fill.price == D("102.051")
        assert fill.slippage == pytest.approx(
            D(".051") * fill.quantity, abs=D(".000000000002")
        )
        assert position.current_price == D(101) and position.marked_at == NOW
        assert risk.execution_quote["ask"] == "102" and order.quote["bid"] == "101"
        assert risk.stop_distance_pct == pytest.approx(
            (D("102.051") - D(98)) / D("102.051") * 100, abs=D(".000000000002")
        )
        assert (snapshot := s.scalar(select(Snapshot))).features == before[0]
        assert (
            snapshot.total_score == before[1] and snapshot.score_momentum == before[2]
        )
        pid = position.id
    later = NOW + timedelta(seconds=15)
    quotes.set(110, 111, later)
    result = engine.run_once(later)  # no new scanner snapshot
    assert result["processed"] == 0 and D(repo.status()["unrealized_pnl"]) > 0
    with database.session() as s:
        assert s.get(PaperPosition, pid).current_price == D(110)
    engine.manual_close(pid, later)
    with database.session() as s:
        assert s.get(PaperPosition, pid).exit_price == D("109.945")


@pytest.mark.parametrize(
    "bid,ask", [(0, 1), (2, 1), (-1, 1), (1, "NaN"), ("Infinity", 1)]
)
def test_invalid_quote_refused(bid, ask):
    with pytest.raises((ValueError, ArithmeticError)):
        MarketQuote("BTCUSDT", bid, ask, NOW)


@pytest.mark.parametrize("offset", [-11, 1])
def test_stale_or_future_quote_does_not_fill(pipeline, database, offset):
    repo, engine, provider = pipeline
    provider.set(101, 102, NOW + timedelta(seconds=offset))
    engine.run_once(NOW)
    assert repo.status()["open_positions"] == 0
    with database.session() as s:
        assert s.scalar(select(PaperOrder)).reference_price == 0
        assert "quote" in s.scalar(select(RiskRecord)).reasons[0]


def test_quote_failure_never_falls_back_to_candle(pipeline, database):
    repo, engine, provider = pipeline
    provider.quotes = {}
    result = engine.run_once(NOW)
    assert result["missing_quotes"] == ["BTCUSDT"]
    assert repo.status()["cash_balance"] == "1000.000000000000"
    assert repo.status()["open_positions"] == 0
    with database.session() as s:
        assert s.scalar(select(PaperOrder)).status == "REJECTED"
        assert s.scalar(select(PaperFill)) is None


@pytest.mark.parametrize("reason,bid", [("STOP", 97), ("TAKE_PROFIT", 105)])
def test_quote_protection_works_while_off_with_stale_1h_candle(
    pipeline, database, reason, bid
):
    repo, engine, provider = pipeline
    if reason == "TAKE_PROFIT":
        repo.settings = replace(repo.settings, take_profit_r_multiple=D(2))
        engine = PaperEngine(repo, engine.scanner_settings, quote_provider=provider)
    engine.run_once(NOW)
    repo.set_enabled(False, NOW)
    database.set_override("BTCUSDT", "IGNORE")
    later = NOW + timedelta(hours=2)
    provider.set(bid, bid + 1, later)
    result = engine.run_once(later)
    assert result["fresh_markets"] == 0 and result["orders"] == 1
    with database.session() as s:
        position = s.scalar(select(PaperPosition))
        assert position.status == "CLOSED" and position.exit_reason == reason
        assert position.exit_price == D(bid) * D(".9995")


def test_manual_close_uses_quote_even_without_fresh_strategy_data(pipeline, database):
    repo, engine, provider = pipeline
    engine.run_once(NOW)
    with database.session() as s:
        pid = s.scalar(select(PaperPosition.id))
    later = NOW + timedelta(hours=2)
    provider.set(108, 109, later)
    engine.manual_close(pid, later)
    with database.session() as s:
        assert s.get(PaperPosition, pid).exit_reason == "MANUAL"
        assert s.get(PaperPosition, pid).exit_price == D("107.946")


def test_missing_quote_keeps_last_quote_mark_and_blocks_new_buy(pipeline, database):
    repo, engine, provider = pipeline
    engine.run_once(NOW)
    later = NOW + timedelta(minutes=5)
    seed(database, later, price=500)  # closed candle must not overwrite 101 quote mark
    seed(database, later, price=100, symbol="ETHUSDT")
    provider.quotes = {}
    provider.set(101, 102, later, "ETHUSDT")
    engine.run_once(later)
    with database.session() as s:
        position = s.scalar(select(PaperPosition))
        assert position.current_price == 101 and position.marked_at == NOW
        risk = s.scalars(select(RiskRecord).order_by(RiskRecord.id.desc())).first()
        assert not risk.approved and "portfolio marks" in risk.reasons[0]
    assert repo.status()["open_positions"] == 1


@pytest.mark.parametrize(
    "override,auto,manual,expected",
    [
        ("WATCH", False, False, 0),
        ("PINNED", False, False, 0),
        ("AUTO", True, False, 1),
        ("WATCH", True, False, 1),
        ("WATCH", False, True, 1),
        ("PINNED", False, True, 1),
        ("IGNORE", True, True, 0),
    ],
)
def test_manual_observation_and_auto_eligibility(
    pipeline, database, override, auto, manual, expected
):
    repo, engine, _ = pipeline
    database.set_override("BTCUSDT", override)
    with database.session.begin() as s:
        s.scalar(select(Decision)).algorithm_watch = auto
    if manual:
        repo.set_manual_trade_enabled("BTCUSDT", True, NOW)
    engine.run_once(NOW)
    assert repo.status()["open_positions"] == expected
    assert database.overrides()["BTCUSDT"] == override


def test_require_watch_false_cannot_bypass_manual_permission():
    c = context(algorithm_watch=False, watch_state="PINNED")
    assert (
        ReferenceStrategy(PaperSettings(require_watch=False)).evaluate(c).action
        == "HOLD"
    )
    assert (
        ReferenceStrategy(PaperSettings())
        .evaluate(replace(c, manual_trade_enabled=True))
        .action
        == "BUY"
    )


def test_custom_strategy_cannot_bypass_permission_gate(pipeline, database):
    repo, engine, _ = pipeline
    with database.session.begin() as s:
        s.scalar(select(Decision)).algorithm_watch = False

    class AlwaysBuy:
        def evaluate(self, ctx, has_position):
            return ReferenceStrategy(PaperSettings()).evaluate(
                replace(ctx, algorithm_watch=True)
            )

    engine.strategy = AlwaysBuy()
    engine.run_once(NOW)
    assert repo.status()["open_positions"] == 0
    with database.session() as s:
        assert "permission" in s.scalar(select(RiskRecord)).reasons[0]


def test_strategy_ignores_non_1h_snapshots(pipeline, database):
    repo, engine, _ = pipeline
    with database.session.begin() as s:
        s.scalar(select(ScannerRun)).timeframe = "5m"
        s.scalar(select(Snapshot)).timeframe = "5m"
    assert engine.run_once(NOW)["processed"] == 0
    assert repo.status()["open_positions"] == 0


def test_fetch_does_not_hold_write_lock(pipeline, database):
    _, engine, provider = pipeline
    original = provider.get_quotes

    def get(symbols):
        with database.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA busy_timeout=100")
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            connection.rollback()
        return original(symbols)

    provider.get_quotes = get
    assert engine.run_once(NOW)["orders"] == 1


def pending(pipeline, database):
    repo, engine, provider = pipeline
    with repo.transaction() as s:
        portfolio = PortfolioService(s)
        c = engine.contexts(s, NOW)[0]
        sig = engine.strategy.evaluate(c)
        record = SignalRecord(
            snapshot_id=c.snapshot_id,
            timestamp=NOW,
            symbol=c.symbol,
            action="BUY",
            signal_strength=sig.signal_strength,
            entry_reference=sig.entry_reference,
            stop_reference=sig.stop_reference,
            strategy_name=sig.strategy_name,
            reasons=list(sig.reasons),
            configuration={},
        )
        s.add(record)
        s.flush()
        risk = engine.risk.evaluate(
            sig, portfolio.view(), True, entry_price=provider.quotes[c.symbol].ask
        )
        row = RiskRecord(
            signal_id=record.id,
            approved=risk.approved,
            risk_amount=risk.risk_amount,
            position_notional=risk.position_notional,
            quantity=risk.quantity,
            stop_distance_pct=risk.stop_distance_pct,
            reasons=list(risk.reasons),
            portfolio={},
        )
        s.add(row)
        s.flush()
        order = PaperExecutionService(
            s, portfolio, repo.settings, quotes=provider.quotes
        ).create_order(record.id, row.id, NOW)
        assert order.status == "CREATED"
        return order.id


def test_pending_fill_rechecks_quote_age_and_releases_cash(pipeline, database):
    repo, _, provider = pipeline
    oid = pending(pipeline, database)
    with repo.transaction() as s:
        order = PaperExecutionService(
            s, PortfolioService(s), repo.settings, provider.quotes
        ).fill_order(oid, NOW + timedelta(seconds=11))
        assert order.status == "REJECTED"
    assert D(repo.status()["reserved_cash"]) == 0


def test_pending_fill_rechecks_current_ask_and_risk(pipeline, database):
    repo, _, provider = pipeline
    oid = pending(pipeline, database)
    provider.set(110, 111)
    with repo.transaction() as s:
        order = PaperExecutionService(
            s, PortfolioService(s), repo.settings, provider.quotes
        ).fill_order(oid, NOW)
        assert order.status == "REJECTED" and "Risk limits" in order.reason
    assert D(repo.status()["reserved_cash"]) == 0


def test_pending_permission_revoked_and_reset_preserves_permission(pipeline, database):
    repo, _, _ = pipeline
    with database.session.begin() as s:
        s.scalar(select(Decision)).algorithm_watch = False
    repo.set_manual_trade_enabled("BTCUSDT", True, NOW)
    oid = pending(pipeline, database)
    repo.set_manual_trade_enabled("BTCUSDT", False, NOW)
    with database.session() as s:
        assert s.get(PaperOrder, oid).status == "CANCELLED"
        assert not s.get(SymbolTradePermission, "BTCUSDT").manual_trade_enabled
    assert D(repo.status()["reserved_cash"]) == 0
    repo.set_manual_trade_enabled("BTCUSDT", True, NOW)
    repo.set_enabled(False, NOW)
    repo.reset(True, NOW)
    with database.session() as s:
        assert s.get(SymbolTradePermission, "BTCUSDT").manual_trade_enabled


def test_permission_web_csrf_and_observation_unchanged(pipeline, database, settings):
    repo, _, _ = pipeline
    database.set_override("BTCUSDT", "PINNED")
    app = create_app(settings, database)
    client = app.test_client()
    try:
        assert b"Manual trading permissions" in client.get("/paper").data
        assert (
            client.post(
                "/paper/permissions", data={"symbol": "BTCUSDT", "enabled": "ON"}
            ).status_code
            == 403
        )
        with client.session_transaction() as s:
            csrf = s["csrf"]
        assert (
            client.post(
                "/paper/permissions",
                data={"csrf": csrf, "symbol": "BTCUSDT", "enabled": "ON"},
            ).status_code
            == 302
        )
        assert repo.permissions(NOW)[0]["manual_trade_enabled"]
        assert database.overrides()["BTCUSDT"] == "PINNED"
        assert (
            client.post(
                "/paper/permissions",
                data={"csrf": csrf, "symbol": "UNKNOWN", "enabled": "ON"},
            ).status_code
            == 400
        )
    finally:
        app.extensions["web_heartbeat"].close()


class Http:
    def __init__(self, payload, status=200, headers=None):
        self.response = requests.Response()
        self.response.status_code = status
        self.response._content = json.dumps(payload).encode()
        self.response.headers.update(headers or {})
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


def test_public_bookticker_get_only_validates_partial_rows():
    http = Http(
        [
            {"symbol": "BTCUSDT", "bidPrice": "101", "askPrice": "102"},
            {"symbol": "ETHUSDT", "bidPrice": "NaN", "askPrice": "100"},
        ]
    )
    provider = BinancePublicQuotes(
        "https://api.binance.com", session=http, clock=lambda: NOW
    )
    result = provider.get_quotes({"BTCUSDT", "ETHUSDT"})
    assert list(result) == ["BTCUSDT"] and result["BTCUSDT"].received_at == NOW
    args, kwargs = http.calls[0]
    assert args == ("https://api.binance.com/api/v3/ticker/bookTicker",)
    assert set(kwargs) == {"params", "timeout"} and set(kwargs["params"]) == {"symbols"}
    assert json.loads(kwargs["params"]["symbols"]) == ["BTCUSDT", "ETHUSDT"]


def test_rate_limit_cooldown_avoids_repeat_requests():
    http = Http({}, 429, {"Retry-After": "60"})
    provider = BinancePublicQuotes(
        "https://api.binance.com", session=http, clock=lambda: NOW
    )
    assert provider.get_quotes({"BTCUSDT"}) == {}
    assert provider.get_quotes({"BTCUSDT"}) == {} and len(http.calls) == 1


def test_quote_request_exception_is_safe():
    class Offline:
        def get(self, *args, **kwargs):
            raise requests.Timeout("offline")

    assert (
        BinancePublicQuotes("https://api.binance.com", session=Offline()).get_quotes(
            {"BTCUSDT"}
        )
        == {}
    )


def test_migration_adopts_old_paper_rows_without_touching_scanner(database):
    from alembic import command

    from crypto_bot.storage.migrate import migration_config

    seed(database)
    database.set_override("BTCUSDT", "PINNED")
    with database.engine.begin() as conn:
        command.upgrade(migration_config(conn), "0002_paper")
        conn.exec_driver_sql(
            "INSERT INTO strategy_signals (id,timestamp,symbol,action,signal_strength,entry_reference,strategy_name,reasons,configuration) VALUES (1,?, 'BTCUSDT','HOLD','0.5','100','old_strategy','[\"old reason\"]','{}')",
            (NOW.isoformat(sep=" "),),
        )
    with database.session() as s:
        before = s.scalar(select(Snapshot)).features.copy()
    upgrade_database(database)
    upgrade_database(database)
    with database.session() as s:
        assert s.get(SignalRecord, 1).reasons == ["old reason"]
        assert s.scalar(select(Snapshot)).features == before
        assert s.get(SymbolTradePermission, "BTCUSDT") is None
    assert database.overrides()["BTCUSDT"] == "PINNED"
    with database.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"


def test_fill_permission_recheck_cannot_be_bypassed_by_old_approval(pipeline, database):
    repo, _, provider = pipeline
    with database.session.begin() as s:
        s.scalar(select(Decision)).algorithm_watch = False
    repo.set_manual_trade_enabled("BTCUSDT", True, NOW)
    oid = pending(pipeline, database)
    # Simulate permission revoked independently after risk approval.
    with database.session.begin() as s:
        s.get(SymbolTradePermission, "BTCUSDT").manual_trade_enabled = False
    with repo.transaction() as s:
        order = PaperExecutionService(
            s, PortfolioService(s), repo.settings, provider.quotes
        ).fill_order(oid, NOW)
        assert order.status == "REJECTED" and "permission" in order.reason
    assert D(repo.status()["reserved_cash"]) == 0


def test_quote_has_already_passed_strategy_stop_or_target(pipeline, database):
    repo, engine, provider = pipeline
    repo.settings = replace(repo.settings, take_profit_r_multiple=D(2))
    engine = PaperEngine(repo, engine.scanner_settings, quote_provider=provider)
    provider.set(105, 106)
    engine.run_once(NOW)
    with database.session() as s:
        assert "take profit" in s.scalar(select(RiskRecord)).reasons[0]
    assert repo.status()["open_positions"] == 0


def test_manual_deny_keeps_automatic_eligibility(pipeline, database):
    repo, engine, _ = pipeline
    repo.set_manual_trade_enabled("BTCUSDT", False, NOW)
    engine.run_once(NOW)
    assert repo.status()["open_positions"] == 1


def test_quote_provider_payload_without_requested_symbol_does_not_fill(pipeline):
    repo, engine, provider = pipeline
    provider.quotes = {"BTCUSDT": MarketQuote("ETHUSDT", 101, 102, NOW)}
    engine.run_once(NOW)
    assert repo.status()["open_positions"] == 0
