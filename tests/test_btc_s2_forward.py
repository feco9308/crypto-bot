"""Frozen S2 synthetic-clock regressions; no network or historical 2026 outcomes."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import localcontext
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select, text

from crypto_bot.config.settings import Settings
from crypto_bot.storage.migrate import upgrade_database
from crypto_bot.storage.paper_models import (
    ForwardDailyDecision,
    PaperAccount,
    PaperFill,
    PaperGeneration,
    PaperOrder,
    PaperPosition,
    RiskRecord,
    SignalRecord,
)
from crypto_bot.trading.config import PaperSettings
from crypto_bot.trading.domain import D, q
from crypto_bot.trading.engine import PaperEngine
from crypto_bot.trading.forward import S2ForwardEngine
from crypto_bot.trading.forward_daily import (
    DAY_MS,
    BinanceBTCDaily,
    DailyCandle,
    DailyDataUnavailable,
    completed_window,
    millis,
    s2_observation,
)
from crypto_bot.trading.profiles import S2_PROFILE, S2_STRATEGY, ForwardSafetyError
from crypto_bot.trading.quotes import MarketQuote
from crypto_bot.trading.repository import PaperRepository
from crypto_bot.web.app import create_app
from crypto_bot.web.trade_audit import quote_display

ACTIVATION = datetime(2030, 1, 21, 12)
PROVENANCE = {
    "2026_final_oos_pristine": False,
    "2026_final_oos_abandoned_for_forward_paper": True,
    "reason": "USER_CHOSE_REALTIME_FORWARD_PAPER",
}


def window(now, closes=None):
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    closes = closes or [100] * 19 + [50]
    return tuple(
        DailyCandle(
            millis(end - timedelta(days=20 - i)),
            millis(end - timedelta(days=19 - i)) - 1,
            D(c),
            D(c),
            D(c),
            D(c),
        )
        for i, c in enumerate(closes)
    )


def raw(bars):
    return [
        [
            b.open_ms,
            str(b.open),
            str(b.high),
            str(b.low),
            str(b.close),
            "1",
            b.close_ms,
            "1",
            1,
            "1",
            "1",
            "0",
        ]
        for b in bars
    ]


@pytest.fixture
def forward(database):
    upgrade_database(database)
    cfg = PaperSettings(execution_profile=S2_PROFILE, strategy_name=S2_STRATEGY)
    repo = PaperRepository(database, cfg)
    repo.initialize_account(ACTIVATION - timedelta(days=3))
    # Existing account remains auditably archived, not deleted.
    with repo.transaction() as s:
        a = s.get(PaperAccount, 1)
        a.configuration = PaperSettings().public_dict()
        a.cash_balance = D("948.21")
    activation = repo.activate_s2(PROVENANCE, True, ACTIVATION)
    clock = Mock(return_value=ACTIVATION + timedelta(days=1))
    daily = Mock()
    daily.get_window.side_effect = lambda now: window(now)
    quotes = Mock()
    quotes.get_quotes.side_effect = lambda _: {
        "BTCUSDT": MarketQuote("BTCUSDT", D(89), D(90), clock(), "FIXED_PUBLIC")
    }
    engine = S2ForwardEngine(repo, Settings(), daily, quotes, clock)
    return repo, engine, clock, daily, quotes, activation


def count(repo, model):
    with repo.database.session() as s:
        return s.scalar(select(func.count()).select_from(model))


def test_frozen_sma_sample_std_and_strict_edges():
    bars = window(ACTIVATION, list(range(1, 21)))
    out = s2_observation(bars, False)
    with localcontext() as c:
        c.prec = 45
        assert D(out["sma20"]) == D("10.5")
        assert D(out["std20"]) == (D(665) / 19).sqrt()
        assert D(out["lower_band"]) == D("10.5") - 2 * (D(665) / 19).sqrt()
    equal = window(ACTIVATION, [100] * 20)
    assert s2_observation(equal, False)["action"] == "HOLD"  # close == lower
    assert s2_observation(equal, True)["action"] == "SELL"  # close == SMA
    assert s2_observation(window(ACTIVATION), False)["action"] == "BUY"
    assert s2_observation(window(ACTIVATION), True)["action"] == "HOLD"


def test_daily_provider_completed_only_public_get():
    now = ACTIVATION
    bars = window(now)
    current = DailyCandle(
        millis(now.replace(hour=0)),
        millis(now.replace(hour=0)) + DAY_MS - 1,
        D(1),
        D(1),
        D(1),
        D(1),
    )
    session = Mock()
    session.get.return_value.json.return_value = raw(bars + (current,))
    provider = BinanceBTCDaily("https://api.binance.com", session=session)
    assert provider.get_window(now) == bars
    args, kw = session.get.call_args
    assert args == ("https://api.binance.com/api/v3/klines",)
    assert kw["params"] == {"symbol": "BTCUSDT", "interval": "1d", "limit": 22}
    assert kw["allow_redirects"] is False
    assert not session.post.called
    assert session.trust_env is False


@pytest.mark.parametrize(
    "damage", ["duplicate", "gap", "alignment", "negative", "nan", "too_short", "stale"]
)
def test_invalid_daily_data_fail_closed(damage):
    data = raw(window(ACTIVATION))
    if damage == "duplicate":
        data[-1] = data[-2]
    if damage == "gap":
        del data[2]
    if damage == "alignment":
        data[-1][0] += 1
    if damage == "negative":
        data[-1][1] = "-1"
    if damage == "nan":
        data[-1][4] = "NaN"
    if damage == "too_short":
        data = data[1:]
    if damage == "stale":
        data = raw(window(ACTIVATION - timedelta(days=1)))
    with pytest.raises(DailyDataUnavailable):
        completed_window(data, ACTIVATION)


def test_activation_warmup_no_backfill_and_new_close_only(forward):
    repo, engine, clock, daily, quotes, activation = forward
    clock.return_value = ACTIVATION
    assert engine.run_once()["processed"] == 0
    assert count(repo, SignalRecord) == count(repo, PaperOrder) == 0
    assert not quotes.get_quotes.called
    assert activation["starting_state"] == "FLAT"
    assert activation["starting_equity"] == "1000"
    assert repo.status()["forward"]["latest_action"] is None
    clock.return_value += timedelta(days=1)
    assert engine.run_once()["orders"] == 1
    assert count(repo, ForwardDailyDecision) == count(repo, SignalRecord) == 1
    with repo.database.session() as s:
        d = s.scalar(select(ForwardDailyDecision))
        assert d.source_close_ms > millis(ACTIVATION)
        assert d.decision_time == clock()
        assert d.state_before == "FLAT" and d.state_after == "LONG"
        assert d.execution_audit["source_candle_close_time"].endswith("Z")
        assert len(list(s.scalars(select(PaperGeneration)))) == 2
        assert (
            s.get(PaperGeneration, 1).account_before["cash_balance"]
            == "948.210000000000"
        )


def test_full_equity_fresh_ask_bid_costs_no_stop_and_full_exit(forward, monkeypatch):
    repo, engine, clock, daily, quotes, _ = forward
    monkeypatch.setattr(
        "crypto_bot.risk.manager.RiskManager.evaluate",
        Mock(side_effect=AssertionError("S2 must not size with RiskManager")),
    )
    engine.run_once()
    with repo.database.session() as s:
        pos = s.scalar(select(PaperPosition))
        fill = s.scalar(select(PaperFill))
        risk = s.scalar(select(RiskRecord))
        quantity = pos.quantity
        assert fill.price == D("90.045")  # ask plus slippage
        assert fill.fee == q(fill.notional * D(".001"))
        assert fill.slippage == q((fill.price - D(90)) * quantity)
        assert pos.stop_price is None and pos.take_profit_price is None
        assert risk.approved and risk.risk_amount == risk.stop_distance_pct == 0
        assert risk.reasons == ["Frozen S2 full-equity paper allocation; no stop"]
        assert 0 <= s.get(PaperAccount, 1).cash_balance < D(".000000001")
        assert quote_display(pos, {"bid": "89"})["distance_to_stop_pct"] is None
    # Same completed candle cannot produce another signal, even with reconstructed engine.
    restarted = S2ForwardEngine(repo, Settings(), daily, quotes, clock)
    assert restarted.run_once()["orders"] == 0
    assert count(repo, SignalRecord) == count(repo, PaperOrder) == 1
    clock.return_value += timedelta(days=1)
    daily.get_window.side_effect = lambda now: window(now, [100] * 20)
    assert restarted.run_once()["orders"] == 1
    with repo.database.session() as s:
        fills = list(s.scalars(select(PaperFill).order_by(PaperFill.id)))
        pos = s.scalar(select(PaperPosition))
        assert fills[-1].quantity == quantity
        assert fills[-1].price == D("88.9555")  # bid minus slippage
        assert fills[-1].fee == q(fills[-1].notional * D(".001"))
        assert pos.status == "CLOSED" and pos.exit_reason == "STRATEGY"
        assert not repo.status()["open_positions"]
        assert s.get(PaperAccount, 1).cash_balance >= 0


def test_missing_quote_retry_single_signal_and_committed_decision(forward):
    repo, engine, clock, _, quotes, _ = forward
    quotes.get_quotes.return_value = {}

    def missing(_):
        assert count(repo, SignalRecord) == 1  # persisted before quote request
        return {}

    quotes.get_quotes.side_effect = missing
    assert engine.run_once()["orders"] == 0
    assert engine.run_once()["orders"] == 0
    assert count(repo, SignalRecord) == 1
    assert count(repo, PaperFill) == 0
    quotes.get_quotes.side_effect = lambda _: {
        "BTCUSDT": MarketQuote("BTCUSDT", D(89), D(90), clock(), "FIXED")
    }
    assert engine.run_once()["orders"] == 1
    assert count(repo, SignalRecord) == count(repo, PaperFill) == 1


def test_unfilled_signal_superseded_no_retroactive_fill(forward):
    repo, engine, clock, _, quotes, _ = forward
    quotes.get_quotes.side_effect = lambda _: {}
    engine.run_once()
    clock.return_value += timedelta(days=1)
    engine.run_once()
    with repo.database.session() as s:
        decisions = list(
            s.scalars(select(ForwardDailyDecision).order_by(ForwardDailyDecision.id))
        )
        assert [x.status for x in decisions] == ["MISSED_SUPERSEDED", "RETRYABLE"]
    assert count(repo, PaperFill) == 0


@pytest.mark.parametrize(
    "violation",
    [
        "stale_quote",
        "prior_quote",
        "negative_cash",
        "unexpected_stop",
        "wrong_strategy",
    ],
)
def test_safety_halt_persisted_no_repair_trade(forward, violation):
    repo, engine, clock, _, quotes, _ = forward
    if violation in {"negative_cash", "unexpected_stop", "wrong_strategy"}:
        if violation != "negative_cash":
            engine.run_once()
        with repo.transaction() as s:
            if violation == "negative_cash":
                s.get(PaperAccount, 1).cash_balance = D(-1)
            elif violation == "unexpected_stop":
                s.scalar(select(PaperPosition)).stop_price = D(1)
            else:
                s.scalar(select(PaperPosition)).strategy_name = "legacy"
    else:
        age = timedelta(seconds=20 if violation == "stale_quote" else 1)
        quotes.get_quotes.side_effect = lambda _: {
            "BTCUSDT": MarketQuote("BTCUSDT", D(89), D(90), clock() - age, "FIXED")
        }
    before = count(repo, PaperOrder)
    with pytest.raises(ForwardSafetyError):
        engine.run_once()
    assert count(repo, PaperOrder) == before
    with repo.database.session() as s:
        assert s.get(PaperGeneration, 2).status == "ERROR"
        assert not s.get(PaperAccount, 1).enabled
    with pytest.raises(ForwardSafetyError):
        engine.run_once()


def test_generation_cannot_reactivate_or_destructively_reset(forward):
    repo, *_ = forward
    with pytest.raises(ValueError):
        repo.activate_s2(PROVENANCE, True, ACTIVATION)
    with pytest.raises(ValueError):
        repo.reset(True)


def test_old_engine_detached(forward):
    repo, *_ = forward
    legacy = PaperEngine(PaperRepository(repo.database, PaperSettings()), Settings())
    with pytest.raises(RuntimeError, match="detached"):
        legacy.run_once(ACTIVATION)
    assert count(repo, SignalRecord) == 0


def test_default_reference_configuration_unchanged():
    assert "execution_profile" not in PaperSettings().public_dict()
    assert (
        replace(
            PaperSettings(), execution_profile=S2_PROFILE, strategy_name=S2_STRATEGY
        ).public_dict()["execution_profile"]
        == S2_PROFILE
    )


def test_ui_daily_status_and_no_stop_view(forward, monkeypatch):
    repo, engine, _, _, _, _ = forward
    engine.run_once()
    monkeypatch.setattr(PaperSettings, "load", lambda: repo.settings)
    app = create_app(
        Settings(database_url=str(repo.database.engine.url)), repo.database
    )
    response = app.test_client().get("/paper")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    for value in [
        "s2-forward-status",
        S2_STRATEGY,
        S2_PROFILE,
        "SMA20",
        "STD20",
        "Activated UTC",
        "LONG",
        "no stop",
    ]:
        assert value in html
    assert count(repo, PaperOrder) == 1
    app.extensions["web_heartbeat"].close()


def test_migration_nullable_and_foreign_keys_on(database):
    upgrade_database(database)
    with database.engine.connect() as c:
        assert c.execute(text("PRAGMA foreign_keys")).scalar() == 1
        cols = c.execute(text("PRAGMA table_info(paper_positions)")).mappings()
        assert next(x for x in cols if x["name"] == "stop_price")["notnull"] == 0
    upgrade_database(database)  # idempotent


def test_migration_retains_existing_stop_position_fill_and_audit(database):
    from alembic import command
    from conftest import NOW, FixtureQuotes
    from test_paper_core import seed

    from crypto_bot.storage.migrate import migration_config

    with database.engine.begin() as c:
        command.upgrade(migration_config(c), "0003_quotes_permissions")
    repo = PaperRepository(database, PaperSettings())
    repo.initialize_account(NOW)
    repo.set_enabled(True, NOW)
    seed(database)
    PaperEngine(repo, Settings(), quote_provider=FixtureQuotes(database)).run_once(NOW)
    tables = [
        "paper_account",
        "strategy_signals",
        "risk_decisions",
        "paper_orders",
        "paper_positions",
        "paper_fills",
        "paper_portfolio_snapshots",
    ]

    def rows():
        with database.engine.connect() as c:
            return {
                t: c.execute(text("SELECT * FROM " + t + " ORDER BY id")).all()
                for t in tables
            }

    before = rows()
    assert len(before["paper_positions"]) == len(before["paper_fills"]) == 1
    upgrade_database(database)
    assert rows() == before
    with database.engine.connect() as c:
        assert c.execute(text("PRAGMA foreign_key_check")).first() is None
    # Activation must refuse inherited position, even when new entries are OFF.
    repo.set_enabled(False, NOW)
    s2 = PaperRepository(
        database, PaperSettings(execution_profile=S2_PROFILE, strategy_name=S2_STRATEGY)
    )
    with pytest.raises(ValueError, match="no open position"):
        s2.activate_s2(PROVENANCE, True, NOW)
    assert len(rows()["paper_positions"]) == 1


def test_paper_off_never_backfills_old_buy_on_enable(forward):
    repo, engine, _, _, _, _ = forward
    repo.set_enabled(False)
    engine.run_once()
    repo.set_enabled(True)
    assert engine.run_once()["orders"] == 0
    with repo.database.session() as s:
        assert s.scalar(select(ForwardDailyDecision)).status == "MISSED_DISABLED"
    assert count(repo, SignalRecord) == 1


def test_disabled_s2_still_exits_full_position(forward):
    repo, engine, clock, daily, _, _ = forward
    engine.run_once()
    repo.set_enabled(False)
    clock.return_value += timedelta(days=1)
    daily.get_window.side_effect = lambda now: window(now, [100] * 20)
    assert engine.run_once()["orders"] == 1
    assert repo.status()["open_positions"] == 0


def test_s2_never_stop_protects_even_on_crash_price(forward):
    repo, engine, _, _, quotes, _ = forward
    engine.run_once()
    quotes.get_quotes.side_effect = lambda _: {
        "BTCUSDT": MarketQuote("BTCUSDT", D(1), D(2), engine.clock(), "FIXED")
    }
    engine.run_once()
    assert count(repo, PaperOrder) == 1
    assert repo.status()["open_positions"] == 1


def test_public_full_equity_sizing_cash_safe_across_prices():
    from crypto_bot.execution.full_equity import full_equity_quantity

    cfg = PaperSettings(execution_profile=S2_PROFILE, strategy_name=S2_STRATEGY)
    for cash in ["1000", "948.211234726629", "0.1", "100000000"]:
        for ask in ["0.01", "1", "90", "73111.987654321", "1000000"]:
            qty, price, notion, fee = full_equity_quantity(D(cash), D(ask), cfg)
            assert qty > 0 and notion + fee <= D(cash)
            assert price >= D(ask)


def test_s2_ui_browser_desktop_mobile(forward, monkeypatch):
    from pathlib import Path
    from urllib.parse import urlsplit

    playwright = pytest.importorskip("playwright.sync_api")
    repo, engine, _, _, _, _ = forward
    engine.run_once()
    monkeypatch.setattr(PaperSettings, "load", lambda: repo.settings)
    app = create_app(Settings(), repo.database)
    client = app.test_client()
    destination = Path("artifacts/btc-s2-forward-paper-v1")
    destination.mkdir(parents=True, exist_ok=True)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for width in [1440, 390]:
                page = browser.new_page(viewport={"width": width, "height": 960})

                def respond(route):
                    response = client.get(urlsplit(route.request.url).path)
                    route.fulfill(
                        status=response.status_code,
                        body=response.data,
                        content_type=response.content_type,
                    )

                page.route("http://s2.test/**", respond)
                page.goto("http://s2.test/paper")
                assert page.locator("#s2-forward-status").is_visible()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                page.screenshot(
                    path=str(destination / f"paper-{width}.png"), full_page=True
                )
                page.close()
        finally:
            browser.close()
            app.extensions["web_heartbeat"].close()


def test_stale_daily_window_not_caught_up(forward):
    repo, engine, clock, daily, _, _ = forward
    daily.get_window.side_effect = lambda _: window(clock() - timedelta(days=1))
    result = engine.run_once()
    assert result["warning"] and result["processed"] == 0
    assert count(repo, SignalRecord) == count(repo, PaperOrder) == 0
