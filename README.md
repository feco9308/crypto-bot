# 🪙 Crypto Bot – RSI + EMA Trading Signal Dashboard

Egyszerű Binance Spot alapú **RSI + EMA** indikátoros jelző bot,  
webes **Flask dashboarddal** és **backtest** funkcióval.

> ⚠️ **Figyelem**  
> A bot jelenleg **NEM kereskedik automatikusan** – csak **jelzést ad** (BUY / SELL / WAIT).  
> Éles pénzzel való automatizálás előtt mindig legyen **alapos backtest + paper trading**!

---

## 📸 Dashboard előnézet



![asd](https://github.com/feco9308/crypto-bot/blob/main/image.png)



---

## 🧩 Fő funkciók

- Binance **Spot** API-ról valós idejű adatok (pl. `BTCUSDC`, `ETHUSDC`, `BNBUSDC`, `SOLUSDC`, `XRPUSDC`)
- Technikai indikátorok:
  - **RSI (Relative Strength Index)**
  - **EMA9 / EMA21** (Exponenciális mozgóátlagok)
- Jelzések:
  - **RSI-only** jelzés (RSI < 30 → BUY, RSI > 70 → SELL, különben WAIT)
  - **RSI+EMA kombinált** jelzés (RSI + trend együtt)
- Webes dashboard (Flask):
  - 24h history grafikon (1 perces adatok)
  - Ár + EMA9 + EMA21 + RSI + RSI BUY/SELL pontok
  - Coin táblázat: aktuális ár, RSI, EMA-k, RSI jelzés, RSI+EMA jelzés
- Backtest:
  - `signals_log.csv` alapján visszatesztelhető stratégia (RSI-only / combined)
- Szolgáltatásként futtatható (**systemd**), hogy reboot után is automatikusan induljon.

---

## 🧱 Architektúra

- `trading_bot.py`
  - Binance Spot API hívások
  - RSI + EMA9 + EMA21 számítása
  - jelzés logika (RSI-only + RSI+EMA)
  - logolás: `signals_log.csv`
- `dashboard.py`
  - Flask app
  - REST API endpointok (`/api/signal`, `/api/all_signals`)
  - HTML + JavaScript alapú dashboard (grafikon + táblázat)
- `backtest.py`
  - `signals_log.csv` feldolgozása
  - szimulált kereskedés (BUY/SELL jelzések alapján)
  - eredmény: PnL, winrate, trade statisztikák

---

## 📌 Követelmények

- Linux (Ubuntu ajánlott, de mással is működhet)
- Python **3.11**
- Binance account + **Spot API kulcs**
  - legjobb, ha **READ-ONLY** vagy kis tesztösszeggel használod

---

## ⚙ Konfiguráció – `config.py`

Hozz létre egy `config.py` fájlt a projekt gyökerében:

```python
API_KEY = "IDE_ÍRD_A_BINANCE_API_KEYT"
API_SECRET = "IDE_ÍRD_A_BINANCE_SECRETET"

# Melyik párokat figyelje a dashboard
SYMBOLS = ["BTCUSDC", "ETHUSDC", "BNBUSDC", "SOLUSDC", "XRPUSDC"]

# Jelzés log fájl
LOG_PATH = "signals_log.csv"

# Frissítési idő (másodperc)
REFRESH_SECONDS = 5
```

> 🔒 Ezt a fájlt **SOHA NE töltsd fel** nyilvános repóba!

---

## 🛠 Telepítés

```bash
# Repo klónozás
git clone https://github.com/feco9308/crypto-bot.git
cd crypto-bot

# Python virtuális környezet
python3.11 -m venv venv311
source venv311/bin/activate

# Csomagok telepítése
pip install --upgrade pip
pip install flask pandas ta binance-connector
# vagy:
# pip install -r requirements.txt
```

---

## 🚀 Quick Start – Dashboard indítása fejlesztői módban

```bash
cd crypto-bot
source venv311/bin/activate
python dashboard.py
```

Alapértelmezett elérés böngészőből:

| Hely       | URL                     |
|-----------|--------------------------|
| Lokálisan | http://127.0.0.1:6000    |
| Hálózaton | http://SZERVER_IP:6000   |

---

## 🔁 Dashboard futtatása systemd szolgáltatásként

Így a dashboard automatikusan indul reboot után, és háttérben fut.

### 1️⃣ Unit fájl létrehozása

```bash
sudo nano /etc/systemd/system/crypto-dashboard.service
```

Tartalom (saját userre/útra igazítsd):

```ini
[Unit]
Description=Crypto Bot Flask Dashboard
After=network.target

[Service]
User="user"
Group="user"
WorkingDirectory=/home/"user"/binance
Environment="PATH=/home/"user"/binance/venv311/bin"
ExecStart=/home/"user"/binance/venv311/bin/python dashboard.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 2️⃣ Engedélyezés és indítás

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-dashboard.service
systemctl status crypto-dashboard.service
```

Log figyelés:

```bash
journalctl -u crypto-dashboard.service -f
```

---

## 📈 Backtest használata

A bot a jelzéseket egy `signals_log.csv` fájlba logolja.  
Erre épít a `backtest.py` script.

### Példa futtatás

```bash
cd crypto-bot
source venv311/bin/activate

# RSI + EMA kombinált stratégia
python backtest.py --symbol BTCUSDC --balance 1000 --fee 0.001 --signal-type combined

# RSI-only stratégia (csak RSI alapján számolt jel)
python backtest.py --symbol BTCUSDC --balance 1000 --fee 0.001 --signal-type rsi
```

### Paraméterek

- `--symbol` – Binance spot pár (pl. `BTCUSDC`)
- `--balance` – kezdő USDC egyenleg
- `--fee` – jutalék egy irányban (0.001 = 0.1%)
- `--signal-type` – `combined` vagy `rsi`

---

## 📐 Indikátor logika – röviden

**RSI szint:**

- **RSI < 30** → túladott zóna → potenciális **BUY**
- **RSI > 70** → túlvett zóna → potenciális **SELL**
- 30–70 között → semleges / várakozás (WAIT)

**EMA-k:**

- **EMA9 > EMA21** → inkább **emelkedő trend**
- **EMA9 < EMA21** → inkább **csökkenő trend**

**Kombinált RSI+EMA jelzés:**

- **BUY**, ha:
  - RSI < 30 és
  - EMA9 > EMA21 (azaz az indikátor túladott, de a trend felfelé fordul)
- **SELL**, ha:
  - RSI > 70 és
  - EMA9 < EMA21 (túlvett + gyengülő trend)

Ez a kombináció általában **kevesebb fals jelzést** ad, mint az önmagában használt RSI.

---

## 🔐 Biztonság

- Binance API kulcs:
  - csak **Spot** jogosultság
  - lehetőség szerint **IP-limit**
  - kis tőke, teszteléshez
- API Key / Secret **soha ne kerüljön GitHubra**
- Ha publikus repóban használod, tedd a `config.py`-t `.gitignore`-ba.

---

## 🧪 Ajánlott lépések automatizálás előtt

1. **Backtest** több hónapnyi adaton
2. **Paper trading** (csak jelzést figyelsz, manual trade)
3. Csak ezután érdemes gondolkodni:
   - automata order küldésen
   - valódi nagyobb tőkével való futtatáson

---

## 📜 Licence

A projekt jelenleg személyes / oktatási célú.  
Ha később publikus licence (pl. MIT) kerül rá, azt itt fogod látni.

---

## 🤝 Közreműködés

Issue-k, ötletek, pull requestek jöhetnek:

- GitHub: https://github.com/feco9308/crypto-bot

Ha hasznosnak találod a projektet, dobj egy ⭐-t a repóra! 🙂
