from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import NOW, FixtureQuotes, market
from sqlalchemy import func, select

from crypto_bot.config.settings import Settings
from crypto_bot.execution.paper import PaperExecutionService
from crypto_bot.portfolio.service import PortfolioService
from crypto_bot.risk.manager import RiskManager
from crypto_bot.storage.database import Database
from crypto_bot.storage.migrate import upgrade_database
from crypto_bot.storage.models import SchemaVersion, Snapshot
from crypto_bot.storage.paper_models import (
    PaperAccount,
    PaperFill,
    PaperOrder,
    PaperPosition,
    RiskRecord,
    SignalRecord,
)
from crypto_bot.trading.config import PaperSettings
from crypto_bot.trading.domain import (
    ZERO,
    D,
    MarketContext,
    PortfolioView,
    PositionView,
    StrategySignal,
)
from crypto_bot.trading.engine import PaperEngine
from crypto_bot.trading.repository import PaperRepository
from crypto_bot.trading.strategy import ReferenceStrategy

pytestmark = pytest.mark.usefixtures("paper_quote_fixture")


def signal(action="BUY", symbol="BTCUSDT", price="100", stop="98"):
    return StrategySignal(
        symbol,
        action,
        D(".72"),
        D(price),
        D(stop) if stop else None,
        NOW,
        "reference",
        ("test reason",),
    )


def view(
    positions=(), cash="1000", equity="1000", daily="0", peak="1000", reserved="0"
):
    return PortfolioView(
        D(equity),
        D(cash),
        D(reserved),
        positions,
        ZERO,
        ZERO,
        D(daily),
        D("1000"),
        D(peak),
        ZERO,
    )


def pos(symbol="ETHUSDT", quantity="1", price="100"):
    return PositionView(
        1, symbol, D(quantity), D(price), D(price), D("98"), ZERO, "test"
    )


def seed(
    db,
    when=NOW,
    price=100,
    symbol="BTCUSDT",
    score=82,
    delta=17,
    rsi=60,
    fast=99,
    slow=95,
):
    s = Settings(database_url="sqlite:///:memory:")
    run = db.start_run(s, when)
    features = dict(
        price=price,
        rsi=rsi,
        ema50=fast,
        ema200=slow,
        atr=1,
        atr_pct=1,
        relative_volume=1.5,
        spread=0.01,
        momentum=2,
        volume=100,
        average_volume=80,
        price_change_pct=3,
        range_position=0.8,
        volatility=1,
        quote_volume=100000000,
        trend="bullish",
    )
    rows = [
        dict(
            symbol=symbol,
            total_score=score,
            components=dict(
                trend=25, momentum=17, volume=16, volatility=12, liquidity=12
            ),
            features=features,
            reasons=["fixture"],
            momentum=dict(delta_4h=delta),
            score_version="heuristic-v1",
        )
    ]
    db.finish_run(
        run,
        [market(symbol=symbol, base=symbol.removesuffix("USDT"))],
        rows,
        {symbol: "fixture selection"},
        {},
        1,
    )
    db.test_quotes = getattr(db, "test_quotes", {}) | {symbol: price}
    db.test_quote_time = when
    return run


@pytest.fixture
def paper(database, settings):
    upgrade_database(database)
    cfg = PaperSettings()
    repo = PaperRepository(database, cfg)
    repo.initialize_account(NOW)
    return repo, PaperEngine(repo, settings)


@pytest.mark.parametrize("action", ["BUY", "SELL", "HOLD"])
def test_strategy_signal_model(action):
    assert signal(action).action == action


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "SHORT"},
        {"signal_strength": D("1.01")},
        {"entry_reference": ZERO},
        {"signal_strength": Decimal("NaN")},
        {"reasons": ()},
    ],
)
def test_invalid_signal_model(kwargs):
    with pytest.raises(ValueError):
        replace(signal(), **kwargs)


def context(**changes):
    base = MarketContext(
        1,
        "BTCUSDT",
        NOW,
        D(100),
        D(82),
        D(17),
        D(60),
        D(99),
        D(95),
        D(1),
        D("1.5"),
        "WATCH",
        True,
    )
    return replace(base, **changes)


def test_reference_strategy_configurable_and_no_probability():
    strategy = ReferenceStrategy(PaperSettings())
    out = strategy.evaluate(context())
    assert out.action == "BUY" and out.stop_reference == D(98)
    assert out.signal_strength == D(".82")
    assert strategy.evaluate(context(watch_state="IGNORE")).action == "HOLD"
    assert strategy.evaluate(context(delta=None)).action == "HOLD"
    assert strategy.evaluate(context(score=D(40)), True).action == "SELL"
    assert strategy.evaluate(context(), True).action == "HOLD"
    out = ReferenceStrategy(
        PaperSettings(require_delta=False, take_profit_r_multiple=D(2))
    ).evaluate(context(delta=None))
    assert out.take_profit_price == D(104)


def test_risk_example_and_costs():
    cfg = PaperSettings(
        max_total_exposure_pct=D(100),
        max_symbol_exposure_pct=D(100),
        paper_fee_pct=ZERO,
        paper_slippage_pct=ZERO,
    )
    result = RiskManager(cfg).evaluate(signal(), view())
    assert (
        result.approved
        and result.risk_amount == D(5)
        and result.position_notional == D(250)
        and result.quantity == D("2.5")
    )
    costly = RiskManager(
        replace(cfg, paper_fee_pct=D(".1"), paper_slippage_pct=D(".05"))
    ).evaluate(signal(), view())
    assert costly.quantity < result.quantity and costly.risk_amount <= D(5)


def test_risk_default_symbol_cap():
    result = RiskManager(PaperSettings()).evaluate(signal(), view())
    assert result.approved and result.position_notional <= D(200)
    assert result.position_notional * (1 + D(".001")) <= D(200)


@pytest.mark.parametrize(
    "portfolio,expected",
    [
        (view(positions=(pos(), pos("SOLUSDT"), pos("ADAUSDT"))), "max open"),
        (view(daily="-20"), "daily loss"),
        (view(equity="950"), "drawdown"),
        (view(positions=(pos("BTCUSDT"),)), "duplicate"),
        (view(cash="0"), "insufficient"),
        (view(positions=(pos(quantity="5"),)), "total exposure"),
    ],
)
def test_risk_limits(portfolio, expected):
    out = RiskManager(PaperSettings()).evaluate(signal(), portfolio)
    assert not out.approved and expected in out.reasons[0]


def test_risk_minimum_and_disabled_and_sell_exit():
    manager = RiskManager(PaperSettings())
    assert not manager.evaluate(signal(), view(), False).approved
    assert not manager.evaluate(signal(), view(cash="1")).approved
    assert not manager.evaluate(signal(stop="101"), view()).approved
    assert not manager.evaluate(signal("HOLD"), view()).approved
    assert manager.evaluate(
        signal("SELL"),
        view(positions=(pos("BTCUSDT"),), daily="-100", equity="500"),
        False,
    ).approved
    assert not manager.evaluate(signal("SELL"), view()).approved


def test_pipeline_fee_slippage_cash_pnl_and_audit(paper, database):
    repo, engine = paper
    repo.set_enabled(True, NOW)
    seed(database)
    assert engine.run_once(NOW)["orders"] == 1
    with database.session() as s:
        position = s.scalar(select(PaperPosition))
        assert position.entry_price == D("100.05")
        assert position.entry_fee == pytest.approx(
            position.notional * D(".001"), abs=D("0.000000000002")
        )
        assert s.scalar(select(PaperFill)).order_id == position.entry_order_id
        assert s.get(SignalRecord, position.signal_id).snapshot_id is not None
        assert s.scalar(select(RiskRecord)).approved
        pid = position.id
        quantity = position.quantity
        entry_cost = position.notional + position.entry_fee
    status = repo.status()
    assert D(status["cash_balance"]) == D(1000) - entry_cost
    assert D(status["total_equity"]) == pytest.approx(
        D(1000) + D(status["unrealized_pnl"]), abs=D("0.000000000002")
    )
    seed(database, NOW + timedelta(minutes=5), price=110)
    engine.run_once(NOW + timedelta(minutes=5))
    assert D(repo.status()["unrealized_pnl"]) > 0
    repo.set_enabled(False)
    engine.manual_close(pid, NOW + timedelta(minutes=5))
    with database.session() as s:
        p = s.get(PaperPosition, pid)
        assert (
            p.status == "CLOSED"
            and p.exit_reason == "MANUAL"
            and p.exit_price == D("109.945")
        )
        expected = (p.exit_price - p.entry_price) * quantity - p.entry_fee - p.exit_fee
        assert p.realized_pnl == pytest.approx(expected, abs=D("0.000000000002"))
    status = repo.status()
    assert status["open_positions"] == 0
    assert D(status["cash_balance"]) == D(1000) + D(status["realized_pnl"])
    assert D(status["reserved_cash"]) == 0
    assert D(status["fees_paid"]) > 0


def test_disabled_and_duplicate_snapshot_position(paper, database):
    repo, engine = paper
    seed(database)
    engine.run_once(NOW)
    assert repo.status()["open_positions"] == 0
    with database.session() as s:
        assert s.scalar(select(PaperOrder)).status == "REJECTED"
    repo.set_enabled(True)
    engine.run_once(NOW)
    assert repo.status()["open_positions"] == 0  # processed OFF snapshot cannot replay
    seed(database, NOW + timedelta(minutes=5))
    engine.run_once(NOW + timedelta(minutes=5))
    assert repo.status()["open_positions"] == 1
    result = engine.run_once(NOW + timedelta(minutes=5))
    assert result["processed"] == 0 and result["orders"] == 0
    seed(database, NOW + timedelta(minutes=10))
    engine.run_once(NOW + timedelta(minutes=10))
    assert repo.status()["open_positions"] == 1


@pytest.mark.parametrize(
    "reason,price", [("STOP", 97), ("TAKE_PROFIT", 105), ("STRATEGY", 100)]
)
def test_protective_and_strategy_exits_while_off(paper, database, reason, price):
    repo, engine = paper
    if reason == "TAKE_PROFIT":
        repo.settings = replace(repo.settings, take_profit_r_multiple=D(2))
        engine = PaperEngine(repo, engine.scanner_settings)
    repo.set_enabled(True)
    seed(database)
    engine.run_once(NOW)
    repo.set_enabled(False)
    seed(
        database,
        NOW + timedelta(minutes=5),
        price=price,
        score=40 if reason == "STRATEGY" else 82,
    )
    engine.run_once(NOW + timedelta(minutes=5))
    with database.session() as s:
        p = s.scalar(select(PaperPosition))
        assert p.status == "CLOSED" and p.exit_reason == reason
    assert repo.status()["open_positions"] == 0


def test_reservation_cancel_and_fill_lifecycle(paper, database):
    repo, engine = paper
    repo.set_enabled(True)
    seed(database)
    with repo.transaction() as s:
        ledger = PortfolioService(s)
        sig = signal()
        record = SignalRecord(
            timestamp=NOW,
            symbol=sig.symbol,
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
        risk = RiskRecord(
            signal_id=record.id,
            approved=True,
            risk_amount=D(2),
            position_notional=D(100),
            quantity=D(1),
            stop_distance_pct=D(2),
            reasons=["fixture"],
            portfolio={},
        )
        s.add(risk)
        s.flush()
        ex = PaperExecutionService(
            s,
            ledger,
            repo.settings,
            quotes=FixtureQuotes(database).get_quotes({"BTCUSDT"}),
        )
        order = ex.create_order(record.id, risk.id, NOW)
        assert order.status == "CREATED"
        assert ledger.view().reserved_cash == D("100.15005")
        assert ledger.view().available_cash == D("899.84995")
        ex.cancel_order(order.id, NOW)
        assert order.status == "CANCELLED" and ledger.view().reserved_cash == 0
        ex.fill_order(order.id, NOW)
        assert ledger.view().equity == 1000  # cancellation never fills


def test_reservation_invariants(paper):
    repo, _ = paper
    with repo.transaction() as s:
        ledger = PortfolioService(s)
        ledger.reserve(D(100))
        assert ledger.view().available_cash == 900
        with pytest.raises(ValueError):
            ledger.reserve(D(901))
        with pytest.raises(ValueError):
            ledger.release(D(101))
        ledger.release(D(100))
        assert ledger.view().available_cash == 1000


def test_reset_preserves_scanner_and_overrides(paper, database):
    repo, engine = paper
    seed(database)
    database.set_override("BTCUSDT", "PINNED")
    repo.set_enabled(True)
    engine.run_once(NOW)
    with pytest.raises(ValueError):
        repo.reset(True, NOW)
    repo.set_enabled(False)
    with pytest.raises(ValueError):
        repo.reset(False, NOW)
    repo.lease("active", NOW, 120)
    with pytest.raises(ValueError):
        repo.reset(True, NOW)
    repo.release_lease("active")
    repo.reset(True, NOW)
    assert (
        D(repo.status()["total_equity"]) == 1000
        and repo.status()["open_positions"] == 0
    )
    with database.session() as s:
        assert s.scalar(select(func.count(Snapshot.id))) == 1
        assert s.scalar(select(func.count(PaperFill.id))) == 0
        assert s.scalar(select(func.count(SignalRecord.id))) == 0
    assert database.overrides()["BTCUSDT"] == "PINNED"


def test_account_lease_and_stale_prices(paper, database):
    repo, engine = paper
    seed(database)
    repo.set_enabled(True)
    repo.lease("one", NOW, 120)
    with pytest.raises(RuntimeError):
        repo.lease("two", NOW, 120)
    with pytest.raises(RuntimeError):
        engine.run_once(NOW, lease_owner="two")
    engine.run_once(NOW, lease_owner="one")
    with database.session() as s:
        pid = s.scalar(select(PaperPosition.id))
    with pytest.raises(ValueError, match="Fresh"):
        engine.manual_close(pid, NOW + timedelta(hours=1))
    repo.release_lease("one")
    assert engine.run_once(NOW + timedelta(hours=1))["fresh_markets"] == 0


def test_day_rollover_and_marked_pnl(paper, database):
    repo, engine = paper
    repo.set_enabled(True)
    seed(database)
    engine.run_once(NOW)
    before = D(repo.status()["total_equity"])
    tomorrow = NOW + timedelta(days=1)
    seed(database, tomorrow, price=101)
    engine.run_once(tomorrow)
    after = repo.status()
    assert D(after["daily_pnl"]) == D(after["total_equity"]) - before


def test_migration_and_reopen(tmp_path):
    path = f"sqlite:///{tmp_path}/ledger.db"
    db = Database(path)
    db.initialize()
    seed(db)
    db.set_override("BTCUSDT", "WATCH")
    before = db.history("BTCUSDT", "1h")[0].features
    upgrade_database(db)
    upgrade_database(db)
    db.initialize()  # old initializer stays valid
    repo = PaperRepository(db, PaperSettings())
    repo.initialize_account(NOW)
    repo.set_enabled(True)
    PaperEngine(repo, Settings()).run_once(NOW)
    db.engine.dispose()
    db = Database(path)
    repo = PaperRepository(db, PaperSettings())
    assert repo.status()["open_positions"] == 1
    assert db.history("BTCUSDT", "1h")[0].features == before
    assert db.overrides()["BTCUSDT"] == "WATCH"
    with db.session() as s:
        assert s.get(SchemaVersion, 1).version == 1
    db.engine.dispose()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pyramiding": True},
        {"paper_slippage_pct": D(100)},
        {"initial_paper_balance": D(-1)},
        {"risk_per_trade_pct": Decimal("NaN")},
        {"max_open_positions": 0},
        {"loop_interval": float("nan")},
    ],
)
def test_invalid_paper_settings(kwargs):
    with pytest.raises(ValueError):
        PaperSettings(**kwargs)


def test_exact_sqlite_decimal_roundtrip(paper, database):
    repo, _ = paper
    precise = D("1000000.123456789123")
    with repo.transaction() as session:
        session.get(PaperAccount, 1).cash_balance = precise
    assert D(repo.status()["cash_balance"]) == precise


def test_drawdown_limit_stays_latched_after_recovery():
    recovered = replace(view(equity="1000"), max_drawdown=D(6))
    result = RiskManager(PaperSettings()).evaluate(signal(), recovered)
    assert not result.approved and "drawdown" in result.reasons[0]


def test_concurrent_cycles_cannot_double_buy(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    db = Database(f"sqlite:///{tmp_path}/concurrent.db")
    upgrade_database(db)
    repo = PaperRepository(db, PaperSettings())
    repo.initialize_account(NOW)
    repo.set_enabled(True)
    seed(db)

    def cycle(_):
        return PaperEngine(repo, Settings()).run_once(NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(cycle, [1, 2]))
    assert sum(o["processed"] for o in outcomes) == 1
    assert repo.status()["open_positions"] == 1
    db.engine.dispose()


def test_new_scanner_can_continue_after_extension_migration(database, settings):
    from conftest import FakeProvider

    from crypto_bot.scanner.scanner import Scanner
    from crypto_bot.storage.models import SchemaVersion

    scanner = Scanner(FakeProvider(), database, settings)
    before = scanner.run_once(NOW)
    upgrade_database(database)
    after = scanner.run_once(NOW)
    assert [r["total_score"] for r in before] == [r["total_score"] for r in after]
    assert len(database.history("BTCUSDT", "1h")) == 2
    with database.session() as session:
        assert session.get(SchemaVersion, 1).version == 1
