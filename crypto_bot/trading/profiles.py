"""Explicit paper-only profiles; reference risk/stop behavior stays the default."""

REFERENCE_PROFILE = "REFERENCE_STOP_RISK_V1"
S2_PROFILE = "FULL_EQUITY_NO_STOP_V1"
S2_STRATEGY = "BTC_BOLLINGER_MR_20_2_FORWARD_V1"
S2_SYMBOL = "BTCUSDT"
SAFETY_EXIT_STATUS = 78


class ForwardSafetyError(RuntimeError):
    """Persistent operator intervention required; never repair with a trade."""
