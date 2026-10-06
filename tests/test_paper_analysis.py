"""Deterministic analysis/export tests. Public market-data HTTP is always mocked."""

import csv
import io
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from conftest import NOW
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from test_paper_core import seed
from test_paper_trade_audit import audit  # noqa: F401 -- pytest fixture

from crypto_bot.storage.database import Database
from crypto_bot.storage.models import Base
from crypto_bot.storage.paper_models import (
    ExtensionBase,
    PaperAccount,
    SignalRecord,
)
from crypto_bot.web.analysis import csv_value
from crypto_bot.web.analysis_history import AnalysisHistory, HistoricalData
from crypto_bot.web.analysis_metrics import historical_metrics, summary
from crypto_bot.web.trade_audit import milliseconds


class Immediate:
    def submit(self, fn, *args):
        fn(*args)

    def shutdown(self, **kwargs):
        pass


class Queued(Immediate):
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))


@pytest.fixture
def database(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/analysis.db")
    db.initialize()
    yield db
    db.engine.dispose()


def raw_rows(start, end, interval="1m"):
    step = {"1m": 60000, "5m": 300000}[interval]
    result = []
    for t in range(start, end + 1, step):
        minute = (t - milliseconds(NOW)) // 60000
        close = 100 + minute * 0.1
        result.append(
            [
                t,
                100,
                max(110 if minute == 10 else close + 1, close, 100),
                90 if minute == 20 else min(99, close - 1),
                close,
            ]
        )
    return result


@pytest.fixture
def analysis_case(request, monkeypatch):
    case = request.getfixturevalue("audit")
    client, repo, engine, pid, fake = case
    monkeypatch.setattr(
        "crypto_bot.web.analysis.utcnow", lambda: NOW + timedelta(hours=2)
    )
    session = Mock()

    def get(url, params, **kwargs):
        assert url.endswith("/api/v3/klines")
        response = Mock(status_code=200, headers={})
        response.json.return_value = raw_rows(
            params["startTime"], params["endTime"], params["interval"]
        )
        return response

    session.get.side_effect = get
    service = client.application.extensions["paper_analysis"]
    service.history.close()
    service.history = AnalysisHistory(
        HistoricalData("https://api.binance.com", session=session), executor=Immediate()
    )
    service.aggregate_executor.shutdown(wait=False, cancel_futures=True)
    service.aggregate_executor = Immediate()
    yield case, service, session
    service.close()


def trades(client):
    client.get("/api/paper/analysis/trades")
    return client.get("/api/paper/analysis/trades").json["trades"]


def test_open_trade_fields_and_stored_snapshot(analysis_case):
    (client, repo, engine, pid, _), service, http = analysis_case
    t = trades(client)[0]
    assert t["status"] == "OPEN" and t["exit_time"] is None
    assert t["mfe_pct"] is None
    assert t["market_score"] == 82 and t["delta_4h"] == 17
    assert t["trend_score"] == 25 and t["rsi"] == 60
    assert t["entry_bid"] == "100" and t["entry_ask"] == "100"
    assert Decimal(t["entry_fill_price"]) == Decimal("100.05")
    assert t["strategy_reference_price"] != t["entry_fill_price"]
    assert (
        t["algorithm_watch"] and t["entry_eligible"] and not t["manual_trade_enabled"]
    )
    assert (
        t["risk_approved"] and t["portfolio_equity_at_decision"] == "1000.000000000000"
    )
    assert t["current_exposure_at_decision"] == "0"
    assert t["analysis_data_status"] == "READY"
    assert t["price_change_60m_after_entry_pct"] is not None
    http.post.assert_not_called()
    http.delete.assert_not_called()
    assert all(
        call.args[0] == "https://api.binance.com/api/v3/klines"
        for call in http.get.call_args_list
    )


@pytest.mark.parametrize(
    "reason,price,score",
    [
        ("STOP", 97, 82),
        ("TAKE_PROFIT", 105, 82),
        ("STRATEGY", 101, 40),
        ("MANUAL", 101, 82),
    ],
)
def test_closed_exit_audit(analysis_case, database, reason, price, score):
    (client, repo, engine, pid, _), service, http = analysis_case
    later = NOW + timedelta(minutes=30)
    seed(database, later, price=price, score=score)
    if reason == "MANUAL":
        engine.manual_close(pid, later)
    else:
        engine.run_once(later)
    t = trades(client)[0]
    assert t["status"] == "CLOSED" and t["exit_reason"] == reason
    assert t["exit_quote_bid"] == str(price)
    assert t["exit_quote_received_at"] == "2026-10-05T12:30:00Z"
    assert t["exit_strategy_reasons"] and t["duration_seconds"] == 1800
    assert t["mfe_price"] == 110 and t["mae_price"] == 90
    assert t["mfe_pct"] > 0 and t["mae_pct"] < 0
    if reason == "STOP":
        assert t["stop_threshold"] is not None
    if reason == "TAKE_PROFIT":
        assert t["take_profit_threshold"] is not None
    if reason == "STRATEGY":
        assert any("below exit threshold" in x for x in t["exit_strategy_reasons"])


def test_summary_winner_loser_profit_factor_and_buckets():
    base = dict(
        status="CLOSED",
        duration_seconds=120,
        return_pct=1,
        market_score=82,
        mfe_pct=4,
        mae_pct=-2,
        total_fees="0.2",
    )
    data = summary(
        [
            base | dict(realized_pnl="10"),
            base | dict(realized_pnl="-5", return_pct=-0.5),
        ]
    )
    assert data["total_trades"] == 2 and data["winners"] == data["losers"] == 1
    assert (
        data["win_rate"] == 50
        and data["gross_profit"] == 10
        and data["gross_loss"] == 5
    )
    assert data["net_realized_pnl"] == 5 and data["profit_factor"] == 2
    assert data["average_trade_pnl"] == data["median_trade_pnl"] == 2.5
    assert data["sample_message"] == "Insufficient sample size"
    bucket = next(x for x in data["score_buckets"] if x["bucket"] == "80-89")
    assert bucket["trade_count"] == 2 and bucket["sample_small"]


def test_summary_endpoint_cached_no_http_wait(analysis_case):
    (client, *_), service, http = analysis_case
    trades(client)
    data = client.get("/api/paper/analysis/summary").json
    assert data["aggregation_status"] == "READY"
    assert data["total_trades"] == 1 and data["open_trades"] == 1
    assert data["net_realized_pnl"] == 0 and data["current_equity"]
    queued = Queued()
    service.aggregate_executor = queued
    service.aggregate_cache.clear()
    assert (
        client.get("/api/paper/analysis/summary").json["aggregation_status"]
        == "PENDING"
    )
    assert len(queued.jobs) == 1
    client.get("/api/paper/analysis/summary")
    assert len(queued.jobs) == 1


def test_precise_mfe_mae_and_timing():
    candles = [
        dict(
            time=r[0],
            open=float(r[1]),
            high=float(r[2]),
            low=float(r[3]),
            close=float(r[4]),
        )
        for r in raw_rows(milliseconds(NOW), milliseconds(NOW + timedelta(hours=1)))
    ]
    trade = dict(
        entry_ms=milliseconds(NOW),
        exit_ms=milliseconds(NOW + timedelta(minutes=30)),
        entry_fill_price="100",
    )
    result = historical_metrics(
        trade, candles, "1m", milliseconds(NOW + timedelta(hours=2))
    )
    assert result["mfe_pct"] == pytest.approx(10) and result[
        "mae_pct"
    ] == pytest.approx(-10)
    assert (
        result["time_to_mfe_seconds"] == 600 and result["time_to_mae_seconds"] == 1200
    )
    for minutes, expected in [(15, 1.4), (30, 2.9), (60, 5.9)]:
        assert result[f"price_change_{minutes}m_after_entry_pct"] == pytest.approx(
            expected
        )
    assert result["entry_to_next_30m_low_pct"] == pytest.approx(-10)
    assert result["analysis_data_status"] == "READY"
    # One missing interior candle invalidates complete excursion and 30/60m data.
    partial = historical_metrics(
        trade,
        [c for c in candles if c["time"] != milliseconds(NOW + timedelta(minutes=20))],
        "1m",
        milliseconds(NOW + timedelta(hours=2)),
    )
    assert (
        partial["mfe_pct"] is None
        and partial["price_change_30m_after_entry_pct"] is None
    )
    assert (
        partial["price_change_15m_after_entry_pct"] is not None
        and partial["analysis_data_status"] == "PARTIAL"
    )
    future = historical_metrics(
        trade, candles, "1m", milliseconds(NOW + timedelta(minutes=16))
    )
    assert future["price_change_30m_after_entry_pct"] is None


def test_boundary_candles_not_attributed_to_trade():
    start = milliseconds(NOW) + 30000
    trade = dict(entry_ms=start, exit_ms=start + 120000, entry_fill_price="100")
    candles = [
        dict(time=milliseconds(NOW), high=999, low=1, close=100),
        dict(time=milliseconds(NOW) + 60000, high=105, low=95, close=100),
        dict(time=milliseconds(NOW) + 120000, high=888, low=2, close=100),
    ]
    result = historical_metrics(trade, candles, "1m", start + 3600000)
    assert result["mfe_price"] == 105 and result["mae_price"] == 95


@pytest.mark.parametrize(
    "query",
    [
        "status=CLOSED",
        "symbol=ETHUSDT",
        "strategy=no-such",
        "from=2026-10-06",
        "to=2026-10-04",
        "offset=1",
    ],
)
def test_filters_empty(analysis_case, query):
    client = analysis_case[0][0]
    response = client.get("/api/paper/analysis/trades?" + query)
    assert response.status_code == 200 and response.json["trades"] == []


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=1001",
        "offset=-1",
        "status=LIVE",
        "from=x",
        "from=2026-10-06&to=2026-10-01",
        "enabled=ON",
        "token=secret",
        "status=OPEN&status=CLOSED",
    ],
)
def test_invalid_query(analysis_case, query):
    assert (
        analysis_case[0][0].get("/api/paper/analysis/trades?" + query).status_code
        == 400
    )


def test_exports_json_csv_metadata_and_secrets(analysis_case, database):
    client = analysis_case[0][0]
    with database.session.begin() as db:
        signal = db.scalar(select(SignalRecord))
        signal.configuration = signal.configuration | {
            "api_key": "CANARYKEY",
            "database_url": "sqlite:////SENSITIVE",
        }
        signal.reasons = [
            "test reason",
            "password=CANARYPASSWORD at /home/private/config",
        ]
        account = db.get(PaperAccount, 1)
        account.configuration = account.configuration | {"secret_key": "CANARYSECRET"}
    response = client.get("/api/paper/analysis/export.json")
    assert (
        response.status_code == 200
        and "attachment" in response.headers["Content-Disposition"]
    )
    data = response.json
    assert (
        data["metadata"]["trade_count"] == 1
        and data["metadata"]["database_schema_version"] == "0003_quotes_permissions"
    )
    assert data["metadata"]["score_version"] == ["heuristic-v1"]
    assert (
        data["metadata"]["paper_config"] and data["trades"][0]["paper_config_snapshot"]
    )
    csv_response = client.get("/api/paper/analysis/export.csv")
    assert csv_response.data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(csv_response.data.decode("utf-8-sig"))))
    assert len(rows) == 1 and rows[0]["symbol"] == "BTCUSDT"
    for body in [
        response.data,
        csv_response.data,
        client.get("/api/paper/analysis/meta").data,
    ]:
        for secret in [
            b"CANARYKEY",
            b"CANARYSECRET",
            b"CANARYPASSWORD",
            b"/home/private",
            b"SENSITIVE",
            b"secret_key",
            b"database_url",
        ]:
            assert secret not in body
    assert "Set-Cookie" not in response.headers
    assert csv_value('=HYPERLINK("bad")').startswith("'")
    assert csv_value("-1.25") == "-1.25"


def test_export_pagination_and_consistent_captured_ids(analysis_case, database):
    client = analysis_case[0][0]
    response = client.get("/api/paper/analysis/export.json?offset=1&limit=1")
    assert (
        response.json["metadata"]["trade_count"] == 0 and response.json["trades"] == []
    )
    assert (
        client.get("/api/paper/analysis/export.json").json["metadata"]["export_scope"]
        == "ALL_MATCHING_TRADES"
    )


def test_analysis_api_readonly_and_sqlite_enforced(analysis_case, database):
    client = analysis_case[0][0]
    service = analysis_case[1]

    def snapshot():
        with database.session() as db:
            return {
                t.name: list(db.execute(t.select()))
                for metadata in (Base.metadata, ExtensionBase.metadata)
                for t in metadata.sorted_tables
                if not t.name.startswith(("service_", "system_"))
            }

    before = snapshot()
    for suffix in ["trades", "summary", "export.json", "export.csv", "meta"]:
        path = "/api/paper/analysis/" + suffix
        assert client.get(path).status_code == 200
        assert client.post(path, data={"enabled": "OFF"}).status_code == 405
        assert client.delete(path).status_code == 405
    assert snapshot() == before
    with service.database.session() as db:
        with pytest.raises(OperationalError, match="readonly"):
            db.execute(text("UPDATE paper_account SET enabled=0"))
    assert database.history("BTCUSDT", "1h")[0].features["price"] == 100


def test_cache_hit_expiry_bound_queue_and_missing_data():
    clock = [0]
    provider = Mock()
    provider.candles.return_value = dict(candles=[], next_start=None)
    history = AnalysisHistory(provider, executor=Immediate(), clock=lambda: clock[0])
    trade = dict(
        position_id=1,
        symbol="BTCUSDT",
        entry_ms=milliseconds(NOW),
        exit_ms=milliseconds(NOW) + 1800000,
        entry_fill_price="100",
    )
    now = milliseconds(NOW) + 7200000
    history.metrics(trade, now)
    assert history.metrics(trade, now)["analysis_data_status"] == "UNAVAILABLE"
    calls = provider.candles.call_count
    history.metrics(trade, now)
    assert provider.candles.call_count == calls
    clock[0] = 61
    history.metrics(trade, now)
    assert provider.candles.call_count > calls
    for i in range(520):
        history.metrics(trade | dict(position_id=i + 2), now)
    assert len(history.results) == 512
    queue = Queued()
    pending = AnalysisHistory(provider, executor=queue)
    for i in range(40):
        pending.metrics(trade | dict(position_id=i), now)
    assert len(queue.jobs) == 32 and len(pending.pending) == 32
    history.close()
    pending.close()


def test_rate_limit_and_historical_cache_ttl():
    http = Mock()
    reply = Mock(status_code=429, headers={"Retry-After": "60"})
    http.get.return_value = reply
    source = HistoricalData("https://api.binance.com", session=http)
    from crypto_bot.web.chart_data import ChartUnavailable

    for _ in range(2):
        with pytest.raises(ChartUnavailable):
            source.candles("BTCUSDT", "1m", 0, 60000)
    assert http.get.call_count == 1
    source.retry_at = 0
    reply.status_code = 200
    reply.json.return_value = []
    source.candles("BTCUSDT", "1m", 0, 60000)
    source.candles("BTCUSDT", "1m", 0, 60000)
    assert http.get.call_count == 2
    key = next(iter(source.cache))
    source.cache[key] = (0, [])
    source.candles("BTCUSDT", "1m", 0, 60000)
    assert http.get.call_count == 3


def test_missing_historical_api_keeps_ui_usable(analysis_case):
    client = analysis_case[0][0]
    service = analysis_case[1]
    from crypto_bot.web.chart_data import ChartUnavailable

    service.history.provider.candles = Mock(
        side_effect=ChartUnavailable("Public data unavailable")
    )
    trades(client)
    data = trades(client)[0]
    assert data["market_score"] == 82 and data["mfe_pct"] is None
    assert data["analysis_data_status"] == "UNAVAILABLE"
    response = client.get("/paper/analysis")
    assert response.status_code == 200 and b"Insufficient sample size" in response.data
    assert b"analysis-recent" in response.data


def test_5m_fallback_and_long_range_are_disclosed(analysis_case, database):
    case, service, http = analysis_case
    client, repo, engine, pid, _ = case
    database.test_quotes = {"BTCUSDT": 101}
    database.test_quote_time = NOW + timedelta(minutes=30)
    engine.manual_close(pid, NOW + timedelta(minutes=30))
    original = http.get.side_effect

    def get(url, params, **kwargs):
        if params["interval"] == "1m":
            return Mock(status_code=503, headers={})
        return original(url, params, **kwargs)

    http.get.side_effect = get
    item = trades(client)[0]
    assert (
        item["analysis_data_status"] == "READY" and item["excursion_timeframe"] == "5m"
    )
    assert item["excursion_source"] == "BINANCE_PUBLIC_KLINES"
    assert item["mae_pct"] < 0 and float(item["realized_pnl"]) > 0
    provider = Mock()
    provider.candles.return_value = dict(
        candles=[dict(time=milliseconds(NOW), high=101, low=99, close=100)],
        next_start=None,
    )
    history = AnalysisHistory(provider, executor=Immediate())
    trade = dict(
        position_id=999,
        symbol="BTCUSDT",
        entry_ms=milliseconds(NOW),
        exit_ms=milliseconds(NOW) + 10001 * 60000,
        entry_fill_price="100",
    )
    history.metrics(trade, trade["exit_ms"] + 3600000)
    assert provider.candles.call_args.args[1] == "5m"
    history.close()
