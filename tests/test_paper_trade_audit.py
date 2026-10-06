"""Position audit and visualization tests; all market data is fixed, no live API."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from conftest import NOW, FixtureQuotes
from sqlalchemy import select
from test_paper_core import seed

from crypto_bot.storage.migrate import upgrade_database
from crypto_bot.storage.models import Snapshot
from crypto_bot.storage.paper_models import ExtensionBase, PaperPosition, SignalRecord
from crypto_bot.trading.config import PaperSettings
from crypto_bot.trading.engine import PaperEngine
from crypto_bot.trading.repository import PaperRepository
from crypto_bot.web.app import create_app
from crypto_bot.web.chart_data import ChartUnavailable, PublicChartData
from crypto_bot.web.trade_audit import milliseconds


@pytest.fixture
def audit(database, settings, monkeypatch):
    monkeypatch.setattr(
        "crypto_bot.web.trade_audit.utcnow", lambda: NOW + timedelta(hours=2)
    )
    upgrade_database(database)
    repo = PaperRepository(database, PaperSettings(take_profit_r_multiple=Decimal(2)))
    repo.initialize_account(NOW)
    repo.set_enabled(True, NOW)
    seed(database)
    engine = PaperEngine(repo, settings, quote_provider=FixtureQuotes(database))
    engine.run_once(NOW)
    with database.session() as db:
        pid = db.scalar(select(PaperPosition.id))
    app = create_app(settings, database)
    app.config["TESTING"] = True
    fake = Mock()
    fake.candles.return_value = dict(
        candles=[dict(time=milliseconds(NOW), open=100, high=102, low=99, close=101)],
        next_start=None,
    )
    fake.quote.return_value = dict(
        symbol="BTCUSDT",
        bid="101",
        ask="102",
        source="FIXED_PUBLIC_TEST",
        received_at="2026-10-05T12:00:00Z",
    )
    app.extensions["paper_chart_data"] = fake
    yield app.test_client(), repo, engine, pid, fake
    app.extensions["web_heartbeat"].close()


def test_open_detail_summary_links_and_read_only(audit, database, monkeypatch):
    client, repo, engine, pid, fake = audit
    before = repo.status()
    monkeypatch.setattr(
        engine.strategy,
        "evaluate",
        Mock(side_effect=AssertionError("Visualization must not evaluate strategy")),
    )
    response = client.get(f"/paper/trades/{pid}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    for text in [
        "OPEN",
        "WHY DID WE BUY?",
        "TRADE / POSITION SUMMARY",
        "STRATEGY REFERENCE PRICE",
        "EXECUTION QUOTE / FILL PRICE",
        "Algorithm watch at decision",
        "EVENT TIMELINE",
        "Entry Fill",
        "Position Open",
        "Current public bid",
        "Signal #1",
    ]:
        assert text in html
    assert "/paper/signals/1" in html
    assert f"/paper/trades/{pid}" in client.get("/paper").get_data(as_text=True)
    assert f'data-position-url="/paper/trades/{pid}"' in client.get("/paper").get_data(
        as_text=True
    )
    fake.candles.assert_not_called()
    fake.quote.assert_not_called()
    assert repo.status() == before


@pytest.mark.parametrize(
    "reason,price,score,expected",
    [
        ("STOP", 97, 82, "current bid ≤ stop price"),
        ("TAKE_PROFIT", 105, 82, "current bid ≥ take-profit price"),
        ("STRATEGY", 101, 40, "below exit threshold"),
        ("MANUAL", 101, 82, "explicit paper position close requested"),
    ],
)
def test_closed_detail_exit_explanations(
    audit, database, reason, price, score, expected
):
    client, repo, engine, pid, fake = audit
    later = NOW + timedelta(minutes=5)
    seed(database, later, price=price, score=score)
    if reason == "MANUAL":
        engine.manual_close(pid, later)
    else:
        engine.run_once(later)
    with database.session() as db:
        position = db.get(PaperPosition, pid)
        assert position.exit_reason == reason
    html = client.get(f"/paper/trades/{pid}").get_data(as_text=True)
    assert "CLOSED" in html and f"WHY DID WE SELL? · {reason}" in html
    assert expected in html
    assert "Exit Fill" in html and "Position Closed" in html
    with database.session() as db:
        exit_signal = db.scalar(
            select(SignalRecord).where(SignalRecord.action == "SELL")
        )
        assert f"/paper/signals/{exit_signal.id}" in html
    assert f"/paper/trades/{pid}" in client.get("/paper").get_data(as_text=True)
    assert client.get(f"/api/paper/trades/{pid}/quote").status_code == 409
    fake.quote.assert_not_called()


@pytest.mark.parametrize("interval", ["1m", "5m", "15m", "1h"])
def test_chart_endpoint_and_strategy_isolation(audit, database, interval):
    client, repo, engine, pid, fake = audit
    with database.session() as db:
        before = {
            table.name: list(db.execute(table.select()))
            for table in ExtensionBase.metadata.sorted_tables
            if not table.name.startswith(("service_", "system_"))
        }
        features = db.scalar(select(Snapshot)).features.copy()
    response = client.get(f"/api/paper/trades/{pid}/chart?interval={interval}")
    assert response.status_code == 200
    assert response.json["visualization_only"]
    assert response.json["range"]["start"] == milliseconds(NOW - timedelta(hours=2))
    assert response.json["overlays"]["entry_time"] == "2026-10-05T12:00:00Z"
    assert fake.candles.call_args.args[1] == interval
    assert client.get(f"/api/paper/trades/{pid}/quote").json["unrealized_pnl"]
    with database.session() as db:
        after = {
            table.name: list(db.execute(table.select()))
            for table in ExtensionBase.metadata.sorted_tables
            if table.name in before
        }
        assert before == after
        assert db.scalar(select(Snapshot)).features == features
    # Public visualization price 101 must not contaminate closed strategy price 100.
    with database.session() as db:
        assert engine.contexts(db, NOW)[0].price == Decimal(100)


def test_scores_are_exact_snapshots_no_fabrication(audit, database):
    client, _, _, pid, fake = audit
    seed(database, NOW + timedelta(minutes=5), score=40, delta=-12)
    data = client.get(f"/api/paper/trades/{pid}/scores").json
    assert data["source"] == "scanner_snapshots"
    assert [(p["score"], p["delta_4h"]) for p in data["points"]] == [
        (82, 17),
        (40, -12),
    ]
    assert len(data["points"]) == 2
    fake.candles.assert_not_called()
    fake.quote.assert_not_called()


def test_missing_public_data_audit_still_usable(audit):
    client, _, _, pid, fake = audit
    fake.candles.side_effect = ChartUnavailable("Public data unavailable")
    fake.quote.side_effect = ChartUnavailable("Public bid unavailable")
    assert client.get(f"/api/paper/trades/{pid}/chart").status_code == 503
    assert client.get(f"/api/paper/trades/{pid}/quote").status_code == 503
    assert client.get(f"/paper/trades/{pid}").status_code == 200
    fake.candles.side_effect = None
    fake.candles.return_value = dict(candles=[], next_start=None)
    assert client.get(f"/api/paper/trades/{pid}/chart").json["candles"] == []


@pytest.mark.parametrize(
    "suffix",
    [
        "/chart?interval=1d",
        "/chart?start=0",
        "/chart?start=x",
        "/chart?end=999999999999999",
    ],
)
def test_invalid_chart_request(audit, suffix):
    client, _, _, pid, fake = audit
    assert client.get(f"/api/paper/trades/{pid}" + suffix).status_code == 400
    fake.candles.assert_not_called()


@pytest.mark.parametrize(
    "path",
    [
        "/paper/trades/99999",
        "/api/paper/trades/99999/chart",
        "/api/paper/trades/99999/scores",
        "/api/paper/trades/99999/quote",
    ],
)
def test_missing_position(audit, path):
    assert audit[0].get(path).status_code == 404


def test_mobile_cards_and_native_details_links(audit):
    html = audit[0].get("/paper").get_data(as_text=True)
    assert 'class="paper-position-list"' in html
    assert 'data-label="Symbol"' in html and "· Details</a>" in html
    assert 'data-label="Close"' in html
    assert "audit-metrics" in audit[0].get(f"/paper/trades/{audit[3]}").get_data(
        as_text=True
    )


def test_legacy_missing_quotes_and_zero_cost(audit, database):
    client, repo, engine, pid, fake = audit
    from crypto_bot.storage.paper_models import PaperOrder

    with database.session.begin() as db:
        position = db.get(PaperPosition, pid)
        position.notional = position.entry_fee = Decimal(0)
        db.get(PaperOrder, position.entry_order_id).quote = None
    response = client.get(f"/paper/trades/{pid}")
    assert response.status_code == 200
    assert b"Execution quote metadata was not recorded" in response.data


def response(data, code=200):
    result = Mock(status_code=code, headers={})
    result.json.return_value = data
    return result


def test_public_get_only_pagination_cache_and_quote():
    session = Mock()
    start = milliseconds(NOW - timedelta(hours=2))
    session.get.side_effect = [
        response([[start, "100", "102", "99", "101", "1", start + 299999]]),
        response(dict(symbol="BTCUSDT", bidPrice="100", askPrice="101")),
    ]
    provider = PublicChartData("https://api.binance.com", session=session)
    data = provider.candles("BTCUSDT", "5m", start, start + 1001 * 300000)
    assert data["next_start"] == start + 1000 * 300000
    assert provider.candles("BTCUSDT", "5m", start, start + 1001 * 300000) == data
    quote = provider.quote("BTCUSDT")
    assert provider.quote("BTCUSDT") == quote
    assert session.get.call_count == 2
    for call in session.get.call_args_list:
        assert call.args[0] in [
            "https://api.binance.com/api/v3/klines",
            "https://api.binance.com/api/v3/ticker/bookTicker",
        ]
        assert "headers" not in call.kwargs and "signature" not in call.kwargs["params"]
        assert call.kwargs["allow_redirects"] is False
    session.post.assert_not_called()
    session.delete.assert_not_called()


@pytest.mark.parametrize(
    "payload", [{}, [[0, "NaN", "2", "1", "1"]], [[0, "2", "1", "3", "2"]]]
)
def test_invalid_public_chart_payload(payload):
    session = Mock()
    session.get.return_value = response(payload)
    with pytest.raises(ChartUnavailable):
        PublicChartData("https://api.binance.com", session=session).candles(
            "BTCUSDT", "5m", 0, 300000
        )


def test_rate_limit_backoff_and_bounded_cache():
    session = Mock()
    session.get.return_value = response([], 429)
    provider = PublicChartData("https://api.binance.com", session=session)
    for _ in range(2):
        with pytest.raises(ChartUnavailable):
            provider.candles("BTCUSDT", "5m", 0, 300000)
    assert session.get.call_count == 1
    provider.retry_at = 0
    session.get.return_value = response([])
    for i in range(140):
        provider.candles("BTCUSDT", "5m", i * 300000, (i + 1) * 300000)
    assert len(provider.cache) == 128
