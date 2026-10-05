"""Exact ledger persistence: SQLite TEXT, PostgreSQL NUMERIC, Decimal in Python."""

from decimal import Decimal

from sqlalchemy import Numeric, Text
from sqlalchemy.types import TypeDecorator

from crypto_bot.trading.domain import q


class ExactDecimal(TypeDecorator):
    impl = Numeric
    cache_ok = True

    def __init__(self):
        super().__init__(precision=30, scale=12)

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(
            Text() if dialect.name == "sqlite" else Numeric(30, 12)
        )

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = q(value)
        return format(value, "f") if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(str(value))
