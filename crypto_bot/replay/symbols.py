"""Archive identifiers may have one-character or Unicode asset names."""


def valid_symbol(symbol):
    return (
        isinstance(symbol, str)
        and symbol.endswith("USDT")
        and 1 <= len(symbol[:-4]) <= 30
        and symbol[:-4].isalnum()
        and symbol == symbol.upper()
    )
