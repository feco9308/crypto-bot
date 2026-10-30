"""Shared default candidate competition contract; no eligibility or risk logic."""

from crypto_bot.trading.domain import MarketContext

CANDIDATE_ORDER_POLICY = "SCORE_DESC_SYMBOL_ASC_V1"


def candidate_order_key(context: MarketContext):
    """Order decision-time candidates by scanner score, then exact symbol ID."""
    return -context.score, context.symbol
