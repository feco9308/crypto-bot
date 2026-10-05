import json
from pathlib import Path

WATCHLIST_PATH = Path(__file__).with_name("watchlist.json")

def load_watchlist():
    """
    Visszaad:
      - enabled symbol lista
      - a teljes nyers config (ha később kell auto_trade, stb.)
    """
    with open(WATCHLIST_PATH, "r") as f:
        data = json.load(f)

    symbols = [
        item["symbol"]
        for item in data.get("symbols", [])
        if item.get("enabled", True)
    ]

    return symbols, data