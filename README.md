Crypto Bot – RSI + EMA + Web Dashboard

Egyszerű Binance alapú RSI + EMA jelző bot, valós idejű grafikonos webes felülettel.

⚠️ Figyelem: a bot jelenleg NEM kereskedik automatikusan.
Csak jelzést ad → BUY / SELL / WAIT
Éles kereskedés előtt kötelező: backtest + paper trading!

🚀 Funkciók

✔ Binance valós idejű spot árak
✔ RSI, EMA9, EMA21 technikai indikátorok
✔ Jelzés logolás (CSV)
✔ 1 napos történelmi grafikon (1 perces adatok)
✔ Kattintható coin kiválasztás
✔ Kombinált jelzések: RSI + EMA keresztezés együtt
✔ Flask alapú WebUI
✔ Backtest támogatás log alapján

📌 Jelzés logika összefoglaló
Jelzés típusa	Logika
RSI BUY	RSI < 30
RSI SELL	RSI > 70
WAIT	30–70 között
RSI+EMA BUY	RSI BUY + EMA9 > EMA21
RSI+EMA SELL	RSI SELL + EMA9 < EMA21

📌 A WebUI grafikonon RSI BUY/SELL pontok is jelölve vannak.

🧠 Grafikon értelmezése

Ár + EMA9 + EMA21 = trend

RSI (jobb tengely) = túlvett/túladott

RSI 30 = vételi zóna

RSI 70 = eladási zóna

RSI+EMA jel = biztosabb, kevesebb fake jel

🛠️ Telepítés
1️⃣ Repository klónozás
git clone https://github.com/feco9308/crypto-bot.git
cd crypto-bot

2️⃣ Python virtuális környezet
python3.11 -m venv venv311
source venv311/bin/activate

3️⃣ Csomagok telepítése
pip install --upgrade pip
pip install flask pandas ta binance-connector
# vagy:
# pip install -r requirements.txt

🔑 API kulcs konfigurálása

Hozd létre a config.py fájlt:

API_KEY = "IDE_ÍRD_A_BINANCE_API_KEYT"
API_SECRET = "IDE_ÍRD_A_BINANCE_SECRETET"


⚠️ Javasolt csak Spot-restricted és read-only kulcsot használni tesztelés idején!

▶ Dashboard futtatása (fejlesztői mód)
cd crypto-bot
source venv311/bin/activate
python dashboard.py


Elérés böngészőből:

Hely	URL
Lokálisan	http://127.0.0.1:6000

Hálózaton	http://SzerverIP:6000

Automatikus frissítés 5 mp-ként 📡

🏃 Dashboard futtatása systemd service-ként

Így reboot után is automatikusan indul.

sudo nano /etc/systemd/system/crypto-dashboard.service


Tartalom (módosítsd a saját user/útvonal szerint):

[Unit]
Description=Crypto Bot Flask Dashboard
After=network.target

[Service]
User=feco93
Group=feco93
WorkingDirectory=/home/feco93/binance
Environment="PATH=/home/feco93/binance/venv311/bin"
ExecStart=/home/feco93/binance/venv311/bin/python dashboard.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target


Aktiválás:

sudo systemctl daemon-reload
sudo systemctl enable --now crypto-dashboard.service
systemctl status crypto-dashboard.service
journalctl -u crypto-dashboard.service -f

📊 Backtest használata

A bot minden jelzést logol a signals_log.csv fájlba.
Ez alapján visszatesztelhető a stratégia:

python backtest.py --symbol BTCUSDC --balance 1000 --fee 0.001 --signal-type combined

🎛 Paraméterek
Paraméter	Jelentés	Példa
--symbol	Pár	BTCUSDC
--balance	Kezdő tőke	1000
--fee	Jutalék	0.001 = 0.1%
--signal-type	Stratégia	combined vagy rsi
🧪 Teendő automatizált kereskedés előtt

☑ Backtest legalább több hónapnyi adaton
☑ Paper trading több héten át
☑ Stop-Loss & Take-Profit logika kialakítása
☑ Kockázatkezelési szabályok meghatározása

⚠️ Jogi nyilatkozat

Ez a projekt nem pénzügyi tanácsadás!
A kriptokereskedés magas kockázatú.
Mindenki csak saját felelősségére használja!
