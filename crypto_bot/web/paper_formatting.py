"""Hungarian display only. Never quantize domain, ledger, API or export values."""

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

EMPTY = "—"
SEPARATORS = str.maketrans({",": " ", ".": ","})


def _number(value, places, trim=False):
    if value is None:
        return EMPTY
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return EMPTY
        with localcontext() as context:
            context.prec = max(
                28, len(number.as_tuple().digits), number.adjusted() + places + 2
            )
            rounded = number.quantize(
                Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP
            )
            if rounded == 0:
                rounded = abs(rounded)
            text = f"{rounded:,.{places}f}"
            if trim:
                text = text.rstrip("0").rstrip(".") if rounded else "0.00"
            return text.translate(SEPARATORS)
    except (InvalidOperation, ValueError, TypeError):
        return EMPTY


def money(value, currency=None):
    text = _number(value, 2)
    return f"{text} {currency}" if text != EMPTY and currency else text


def price(value):
    return _number(value, 2)


def quantity(value, asset=None):
    text = _number(value, 8, trim=True)
    return f"{text} {asset}" if text != EMPTY and asset else text


def btc_qty(value):
    return quantity(value, "BTC")


def percent(value):
    text = _number(value, 2)
    return text + "%" if text != EMPTY else text


def utc_datetime(value):
    if value is None:
        return EMPTY
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime):
            return EMPTY
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OverflowError):
        return EMPTY


def register_paper_formatters(app):
    # Dedicated names leave the existing scanner/audit `number` filter untouched.
    app.jinja_env.filters.update(
        paper_money=money,
        paper_price=price,
        paper_qty=quantity,
        paper_percent=percent,
        paper_utc=utc_datetime,
    )
