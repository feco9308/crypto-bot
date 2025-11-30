# Crypto Bot

Egyszerű Binance alapú **RSI + EMA jelző bot** és hozzá tartozó **Flask webes dashboard**.

> ⚠️ **Figyelem:** a bot jelenleg **NEM kereskedik automatikusan**.  
> Csak **jelzést** ad (BUY / SELL / WAIT).  
> Éles pénzzel való automata kereskedés előtt mindig legyen alapos backtest + paper trading!

---

## Fő funkciók

- Binance Spot API-ról valós idejű adatok (`BTCUSDC`, `ETHUSDC`, stb.)
- **RSI + EMA9 + EMA21** indikátorok számítása
- **Jelzések**:
  - RSI-only jel (`RSI < 30` → BUY, `RSI > 70` → SELL)
  - Kombinált jel (RSI + EMA keresztezés)
- **Flask dashboard**:
  - aktuális ár, RSI, EMA-k
  - múltbéli grafikon (kb. 1 nap, 1 perces gyertyák)
  - jelzések vizuálisan
- **Backtest**:
  - `signals_log.csv` alapján visszatesztelhető a stratégia

---

## Követelmények

- Linux (Ubuntu vagy hasonló)
- Python **3.11** (ajánlott)
- Binance account + Spot API kulcs  
  (read-only vagy kis összegű tesztpénz **erősen ajánlott**)

---

## Telepítés

```bash
# 1) Repo klónozás
git clone https://github.com/feco9308/crypto-bot.git
cd crypto-bot

# 2) Python virtuális környezet
python3.11 -m venv venv311
source venv311/bin/activate

# 3) Csomagok telepítése
pip install --upgrade pip
pip install flask pandas ta binance-connector
# (ha van requirements.txt, akkor inkább:)
# pip install -r requirements.txt



Dashboard futtatása (fejlesztői módban)
cd /az/elérési/utad/crypto-bot
source venv311/bin/activate
python dashboard.py


Alapértelmezés szerint a Flask app:

0.0.0.0:6000-on indul

böngészőből eléred:

lokálisan: http://127.0.0.1:6000

LAN-ról: http://SzerverIP:6000


Dashboard futtatása systemd service-ként

Így a dashboard automatikusan indul reboot után, és háttérben fut.

1. Systemd unit fájl

Hozz létre egy fájlt (rootként):

sudo nano /etc/systemd/system/crypto-dashboard.service


Tartalma (a saját elérési utakkal / userrel):

[Unit]
Description=Crypto Bot Flask Dashboard
After=network.target

[Service]
# A LINUX FELHASZNÁLÓ, AKI ALATT FUT (pl. "User name")
User="User name"
Group="User name"

# A PROJEKT KÖNYVTÁR
WorkingDirectory=/home/"User name"/binance

# A VENV PATH
Environment="PATH=/home/"User name"/binance/venv311/bin"

# Hogyan indítsa el a Flask appot
ExecStart=/home/"User name"/binance/venv311/bin/python dashboard.py

# Ha leáll, induljon újra
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target


Figyelj, hogy a fenti útvonalak egyezzenek a sajátoddal!

2. Systemd újratöltés, indítás
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-dashboard.service

# Állapot ellenőrzése
systemctl status crypto-dashboard.service

# Log nézés (Ctrl+C-vel kilépsz)
journalctl -u crypto-dashboard.service -f


Ezután a dashboard elérhető marad akkor is, ha kilépsz a terminálból.

Backtest használata

A bot a jelzéseket egy signals_log.csv fájlba logolja.
Erre épít a backtest.py.

Példa futtatás:

cd /az/elérési/utad/crypto-bot
source venv311/bin/activate

# RSI + EMA kombinált stratégia
python backtest.py --symbol BTCUSDC --balance 1000 --fee 0.001 --signal-type combined

# RSI-only stratégia (csak RSI alapján számolt jel)
python backtest.py --symbol BTCUSDC --balance 1000 --fee 0.001 --signal-type rsi


Paraméterek:

--symbol – Binance spot pár (pl. BTCUSDC)

--balance – kezdő USDC egyenleg

--fee – jutalék egy irányban (0.001 = 0.1%)

--signal-type – combined vagy rsi

Fontos megjegyzések

A kód oktatási célú, nem pénzügyi tanácsadás.

Éles pénzzel csak akkor kereskedj, ha:

érted a stratégiát,

kipróbáltad backtestben,

és futtattad paper trading módban is.

A Binance API kulcsokat mindig korlátozd (IP-limit, csak Spot, alacsony limit).
