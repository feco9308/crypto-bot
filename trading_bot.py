from binance.spot import Spot
import pandas as pd
import ta
from config import API_KEY, API_SECRET
from watchlist import load_watchlist
from pathlib import Path
from datetime import datetime

# ---- History beállítások ----
HISTORY_INTERVAL = "5m"        # 5 perces gyertyák
HISTORY_DAYS = 1               # hány napnyi adat
HISTORY_LIMIT = 12 * 24 * HISTORY_DAYS  # 12 candle/óra * 24 óra = 288 / nap (<=1000)

LOG_PATH = Path(__file__).with_name("signals_log.csv")

client = Spot(api_key=API_KEY, api_secret=API_SECRET)


def get_data(symbol="BTCUSDC",
             interval: str = HISTORY_INTERVAL,
             limit: int = HISTORY_LIMIT):
    klines = client.klines(symbol, interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "open_time", "o", "h", "l", "c", "v", "close_time",
        "qav", "trades", "taker_base", "taker_quote", "ignore"
    ])
    df["c"] = df["c"].astype(float)
    return df


def _clean_series(series: pd.Series):
    """NaN -> None (JSON-ben null lesz, nem NaN)"""
    return [
        float(v) if pd.notna(v) else None
        for v in series
    ]


def get_signal(symbol="BTCUSDC",
               interval: str = HISTORY_INTERVAL,
               limit: int = HISTORY_LIMIT):
    df = get_data(symbol=symbol, interval=interval, limit=limit)

    # Indikátorok
    df["ema9"] = df["c"].ewm(span=9).mean()
    df["ema21"] = df["c"].ewm(span=21).mean()
    df["rsi"] = ta.momentum.RSIIndicator(df["c"]).rsi()

    cur = df.iloc[-1]

    price = float(cur["c"])
    rsi = float(cur["rsi"])
    ema9 = float(cur["ema9"])
    ema21 = float(cur["ema21"])

    # --- 1) RSI alap jelzés ---
    if rsi < 30:
        rsi_signal = "BUY"
    elif rsi > 70:
        rsi_signal = "SELL"
    else:
        rsi_signal = "WAIT"

    # --- 2) Kombinált RSI + EMA jelzés ---
    # BUY: túladott (RSI<30) ÉS rövid EMA (9) a hosszú fölé tör -> emelkedő trend indul
    # SELL: túlvett (RSI>70) ÉS rövid EMA (9) a hosszú alá tör -> eső trend indul
    if rsi < 30 and ema9 > ema21:
        combined_signal = "BUY"
    elif rsi > 70 and ema9 < ema21:
        combined_signal = "SELL"
    else:
        combined_signal = "WAIT"

    # 📈 History (kb. 1 nap, NaN-ek kipucolva)
    hist = df.copy()
    hist_times = pd.to_datetime(hist["open_time"], unit="ms").dt.strftime("%H:%M")

    history = {
        "times": hist_times.tolist(),
        "prices": _clean_series(hist["c"]),
        "ema9": _clean_series(hist["ema9"]),
        "ema21": _clean_series(hist["ema21"]),
        "rsi": _clean_series(hist["rsi"]),
    }

    return {
        "symbol": symbol,
        "price": price,
        "rsi": rsi,
        "ema9": ema9,
        "ema21": ema21,
        "signal": combined_signal,   # KOMBINÁLT jelzés (RSI+EMA)
        "signal_rsi": rsi_signal,    # csak RSI jelzés
        "history": history,
    }


def _log_signals(results):
    """Egyszerű log CSV-be: idő,symbol,price,rsi,ema9,ema21,signal_rsi,signal_combined"""
    try:
        now = datetime.utcnow().isoformat()
        header_needed = not LOG_PATH.exists()
        with LOG_PATH.open("a", encoding="utf-8") as f:
            if header_needed:
                f.write("timestamp,symbol,price,rsi,ema9,ema21,signal_rsi,signal_combined\n")
            for r in results:
                f.write(
                    f"{now},{r['symbol']},{r['price']},"
                    f"{r['rsi']},{r['ema9']},{r['ema21']},"
                    f"{r['signal_rsi']},{r['signal']}\n"
                )
    except Exception as e:
        print("Logolási hiba:", e)


def get_all_signals(interval: str = HISTORY_INTERVAL,
                    limit: int = HISTORY_LIMIT):
    symbols, raw_cfg = load_watchlist()
    results = []
    for sym in symbols:
        try:
            s = get_signal(symbol=sym, interval=interval, limit=limit)
            results.append(s)
        except Exception as e:
            print(f"Hiba {sym} jelzésénél:", e)

    if results:
        _log_signals(results)

    return results


if __name__ == "__main__":
    import time
    while True:
        try:
            print(get_all_signals())
        except Exception as e:
            print("Hiba:", e)
        time.sleep(10)