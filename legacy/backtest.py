#!/usr/bin/env python3
import pandas as pd
from pathlib import Path

LOG_PATH = Path(__file__).with_name("signals_log.csv")


def backtest(symbol="BTCUSDC", start_balance=1000.0, fee=0.001, signal_type="combined"):
    """
    Egyszerű long-only backtest:
    - BUY jelzésnél mindent megveszünk
    - SELL jelzésnél mindent eladunk
    - fee: egyirányú jutalék (pl. 0.001 = 0.1%)

    signal_type:
      - 'combined' -> RSI+EMA logból (signal oszlop), vagy ha nincs, akkor helyben számoljuk
      - 'rsi'      -> csak RSI alapján számoljuk BUY/SELL/WAIT-et (nem kell külön oszlop)
    Régi / hibás sorokat átugorjuk.
    """

    if not LOG_PATH.exists():
        print(f"Hiba: nem találom a log fájlt: {LOG_PATH}")
        return

    # Vegyes / hibás sorok átugrása
    try:
        df = pd.read_csv(LOG_PATH, on_bad_lines="skip")
    except TypeError:
        df = pd.read_csv(LOG_PATH, error_bad_lines=False, warn_bad_lines=True)

    if "symbol" not in df.columns:
        print("Hiba: a log nem tartalmaz 'symbol' oszlopot.")
        return

    df = df[df["symbol"] == symbol].copy()
    if df.empty:
        print(f"Nincs adat a(z) {symbol} szimbólumra.")
        return

    # Kötelező numerikus oszlopok
    required_num = ["timestamp", "price", "rsi"]
    for col in ["ema9", "ema21"]:
        if signal_type == "combined":
            required_num.append(col)

    missing = [c for c in required_num if c not in df.columns]
    if missing:
        print("Hiányzó oszlopok a logban:", missing)
        return

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["rsi"] = pd.to_numeric(df["rsi"], errors="coerce")

    if "ema9" in df.columns:
        df["ema9"] = pd.to_numeric(df["ema9"], errors="coerce")
    if "ema21" in df.columns:
        df["ema21"] = pd.to_numeric(df["ema21"], errors="coerce")

    df = df.dropna(subset=["timestamp", "price", "rsi"])
    if df.empty:
        print("Minden sor hibás / hiányos volt a logban, nincs mit backtestelni.")
        return

    df.sort_values("timestamp", inplace=True)

    position = 0.0
    balance = start_balance
    entry_price = None
    trades = []

    for _, row in df.iterrows():
        price = float(row["price"])
        rsi = float(row["rsi"])
        ts = row["timestamp"]

        # --- Jel kiszámítása / kiválasztása ---
        if signal_type == "rsi":
            # RSI-only stratégia: <30 BUY, >70 SELL
            if rsi < 30:
                sig = "BUY"
            elif rsi > 70:
                sig = "SELL"
            else:
                sig = "WAIT"

        elif signal_type == "combined":
            # 1) Ha van 'signal' oszlop, használjuk (amit a bot írt)
            if "signal" in df.columns and not pd.isna(row.get("signal", None)):
                sig = str(row["signal"]).upper()
            else:
                # 2) Ha nincs 'signal', számoljuk RSI+EMA alapján
                ema9 = float(row["ema9"]) if not pd.isna(row.get("ema9", None)) else None
                ema21 = float(row["ema21"]) if not pd.isna(row.get("ema21", None)) else None
                if ema9 is None or ema21 is None:
                    sig = "WAIT"
                else:
                    if rsi < 30 and ema9 > ema21:
                        sig = "BUY"
                    elif rsi > 70 and ema9 < ema21:
                        sig = "SELL"
                    else:
                        sig = "WAIT"
        else:
            sig = "WAIT"

        # --- Pozíciókezelés ---
        if sig == "BUY" and position == 0.0:
            qty = balance * (1 - fee) / price
            position = qty
            entry_price = price
            balance = 0.0
            trades.append({
                "type": "BUY",
                "time": ts,
                "price": price,
                "qty": qty
            })

        elif sig == "SELL" and position > 0.0:
            gross = position * price
            net = gross * (1 - fee)
            trade_pnl = net - (entry_price * position)

            trades.append({
                "type": "SELL",
                "time": ts,
                "price": price,
                "qty": position,
                "pnl": trade_pnl
            })

            balance = net
            position = 0.0
            entry_price = None

    # Záró equity (ha maradt pozíció, utolsó áron értékeljük)
    last_price = float(df.iloc[-1]["price"])
    if position > 0.0:
        equity = balance + position * last_price
    else:
        equity = balance

    closed_trades = [t for t in trades if t["type"] == "SELL"]
    num_trades = len(closed_trades)
    win_trades = sum(1 for t in closed_trades if t["pnl"] > 0)
    lose_trades = sum(1 for t in closed_trades if t["pnl"] <= 0)
    total_pnl = equity - start_balance
    total_pnl_pct = (equity / start_balance - 1) * 100 if start_balance > 0 else 0
    winrate = (win_trades / num_trades * 100) if num_trades > 0 else 0

    print("===== BACKTEST EREDMÉNY =====")
    print(f"Szimbólum:           {symbol}")
    print(f"Jel típus:           {signal_type}")
    print(f"Kezdő egyenleg:      {start_balance:.2f} USDC")
    print(f"Záró egyenleg:       {equity:.2f} USDC")
    print(f"Összes PnL:          {total_pnl:.2f} USDC ({total_pnl_pct:.2f} %)")
    print(f"Lezárt kötések:      {num_trades}")
    print(f"Nyertes kötések:     {win_trades}")
    print(f"Vesztes kötések:     {lose_trades}")
    print(f"Winrate:             {winrate:.1f} %")
    print()
    if num_trades > 0:
        print("Első 5 lezárt kötés:")
        for t in closed_trades[:5]:
            print(f" - {t['time']}  SELL @ {t['price']:.2f}, pnl: {t['pnl']:.2f} USDC")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Egyszerű backtest a signals_log.csv alapján.")
    parser.add_argument("--symbol", default="BTCUSDC", help="Melyik szimbólumra fusson a backtest (pl. BTCUSDC)")
    parser.add_argument("--balance", type=float, default=1000.0, help="Kezdő USDC egyenleg")
    parser.add_argument("--fee", type=float, default=0.001, help="Jutalék (pl. 0.001 = 0.1%)")
    parser.add_argument("--signal-type", choices=["combined", "rsi"], default="combined",
                        help="Melyik jel alapján backtesteljünk: combined (RSI+EMA) vagy rsi (csak RSI)")

    args = parser.parse_args()
    backtest(
        symbol=args.symbol,
        start_balance=args.balance,
        fee=args.fee,
        signal_type=args.signal_type
    )