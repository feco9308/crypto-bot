"""Display rounding only; raw ledger and export projection must retain precision."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext

import pytest
from test_btc_s2_forward import forward  # noqa: F401 -- shared isolated fixture

from crypto_bot.config.settings import Settings
from crypto_bot.storage.paper_models import PaperGeneration
from crypto_bot.trading.config import PaperSettings
from crypto_bot.web.analysis_data import project, trade_query
from crypto_bot.web.app import create_app
from crypto_bot.web.paper_formatting import (
    btc_qty,
    money,
    percent,
    price,
    quantity,
    utc_datetime,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (Decimal("1000.000000000000"), "1 000,00"),
        (Decimal("0E-12"), "0,00"),
        (Decimal("-0E-8"), "0,00"),
        (Decimal("-0.004"), "0,00"),
        (Decimal("-12.345"), "-12,35"),
        (Decimal("77453.860000"), "77 453,86"),
        (Decimal("80407.336"), "80 407,34"),
        (1000, "1 000,00"),
        (12.345, "12,35"),
        ("2099.37767978588309433941301499725699376691702", "2 099,38"),
        (None, "—"),
        ("NaN", "—"),
        (float("inf"), "—"),
        ("invalid", "—"),
    ],
)
def test_money_and_price(value, expected):
    assert money(value) == price(value) == expected


@pytest.mark.parametrize("value", [Decimal("0E-12"), Decimal("-0.0001"), 0, "0"])
def test_percent_zero(value):
    assert percent(value) == "0,00%"


def test_money_currency_and_percent_half_up():
    assert money(Decimal("1000.000000000000"), "USDT") == "1 000,00 USDT"
    assert money(Decimal("-12.345"), "USDT") == "-12,35 USDT"
    assert percent(Decimal("4.265")) == "4,27%"
    assert percent(Decimal("-12.345")) == "-12,35%"


@pytest.mark.parametrize(
    "value,expected",
    [
        (Decimal("0.01234567"), "0,01234567 BTC"),
        (Decimal("0.012345678912"), "0,01234568 BTC"),
        (Decimal("1.230000000000"), "1,23 BTC"),
        (Decimal("0E-8"), "0,00 BTC"),
        (1, "1 BTC"),
        (None, "—"),
    ],
)
def test_btc_quantity(value, expected):
    assert btc_qty(value) == expected
    assert quantity(Decimal("0.01234000")) == "0,01234"


@pytest.mark.parametrize(
    "formatter", [money, price, quantity, btc_qty, percent, utc_datetime]
)
def test_none(formatter):
    assert formatter(None) == "—"


@pytest.mark.parametrize(
    "value,expected",
    [
        (datetime(2026, 11, 4, 9, 38, 14, 421873), "2026-11-04 09:38:14 UTC"),
        (
            datetime(2026, 11, 4, 9, 38, 14, tzinfo=timezone.utc),
            "2026-11-04 09:38:14 UTC",
        ),
        ("2026-11-04T09:38:14.421873Z", "2026-11-04 09:38:14 UTC"),
        ("2026-11-03T23:59:59.999000Z", "2026-11-03 23:59:59 UTC"),
        ("2026-11-04T11:38:14.421873+02:00", "2026-11-04 09:38:14 UTC"),
        ("bad date", "—"),
    ],
)
def test_utc_timestamps(value, expected):
    assert utc_datetime(value) == expected


def test_decimal_and_timestamp_inputs_and_global_context_unchanged():
    number = Decimal("2099.37767978588309433941301499725699376691702")
    stamp = datetime(2026, 11, 4, 9, 38, 14, 421873)
    original_number, original_stamp = number.as_tuple(), stamp.isoformat()
    before = getcontext().copy()
    for formatter in [money, price, quantity, btc_qty, percent]:
        formatter(number)
    utc_datetime(stamp)
    assert number.as_tuple() == original_number and stamp.isoformat() == original_stamp
    after = getcontext()
    assert (before.prec, before.rounding, before.flags) == (
        after.prec,
        after.rounding,
        after.flags,
    )


def test_s2_card_portfolio_open_history_and_raw_exports(request, monkeypatch):
    repo, engine, clock, daily, _, _ = request.getfixturevalue("forward")
    clock.return_value += timedelta(microseconds=123456)
    engine.run_once()
    with repo.transaction() as session:
        generation = session.get(PaperGeneration, 2)
        generation.latest_indicators = generation.latest_indicators | {
            "close": "77453.860000",
            "sma20": "80407.33600000",
            "std20": "2099.37767978588309433941301499725699376691702",
            "lower_band": "76208.5806404282338113211739700054860124661660",
        }
    monkeypatch.setattr(PaperSettings, "load", lambda: repo.settings)
    app = create_app(Settings(), repo.database)
    client = app.test_client()

    def raw_trade():
        with repo.database.session() as session:
            return project(session.execute(trade_query()).first(), clock())

    before = raw_trade()
    account_before = repo.status()
    html = client.get("/paper").get_data(as_text=True)
    for value in [
        "77 453,86",
        "80 407,34",
        "2 099,38",
        "76 208,58",
        "90,05",
        "89,00",
        "11,09446387 BTC",
        "999,00 USDT",
        "0,00 USDT",
        "2030-01-22 12:00:00 UTC",
    ]:
        assert value in html, html[
            html.find('data-label="Quantity"') : html.find('data-label="Quantity"')
            + 160
        ]
    assert "2099.377679785883094" not in html
    assert "0E-12" not in html and "T12:00:00.123456Z" not in html
    assert before == raw_trade() and account_before == repo.status()
    assert before["entry_fill_price"] == "90.045000000000"
    assert before["entry_time"].endswith(".123456Z")
    assert before["quantity"] == "11.094463868077"
    # API JSON uses the unchanged export projection, without template filters.
    with app.app_context():
        from flask import jsonify

        assert jsonify(before).json["entry_fill_price"] == "90.045000000000"
        assert jsonify(before).json["entry_time"].endswith(".123456Z")
    clock.return_value += timedelta(days=1)
    from test_btc_s2_forward import window

    daily.get_window.side_effect = lambda now: window(now, [100] * 20)
    engine.run_once()
    closed_before = raw_trade()
    html = client.get("/paper").get_data(as_text=True)
    assert "88,96" in html and "2030-01-23 12:00:00 UTC" in html
    assert closed_before == raw_trade()
    app.extensions["web_heartbeat"].close()


def test_flat_portfolio_integer_fields(request, monkeypatch):
    repo, *_ = request.getfixturevalue("forward")
    monkeypatch.setattr(PaperSettings, "load", lambda: repo.settings)
    app = create_app(Settings(), repo.database)
    html = app.test_client().get("/paper").get_data(as_text=True)
    assert "<dt>Generation</dt><dd>2</dd>" in html
    assert "<dt>Open Positions</dt><dd>0</dd>" in html
    assert "1 000,00 USDT" in html and "0,00%" in html
    assert "1000.000000000000" not in html and "0E-12" not in html
    app.extensions["web_heartbeat"].close()
