# Market Intelligence / Market Scanner

Moduláris, API-kulcs nélkül működő Binance Spot piaci megfigyelő. **NO LIVE TRADING IMPLEMENTED. Valódi ordert nem küld; a paper réteg szimulált ordert könyvel.** Nincs futures, margin, leverage vagy ML-predikció. A Market Score heurisztikus rangsorolási érték, nem hozam- vagy nyerési valószínűség.

Az első mérföldkő: automatikus USDT market discovery, indikátorok, magyarázható scoring, historikus tárolás, score momentum, automatikus piacválasztás és webes manuális kontroll. Az [audit és refaktorálási terv](docs/AUDIT.md) a GitHub `de95639` alapállapotát dokumentálja. Az eredeti szerver, config.py, CSV és API-kulcs nem szükséges.

## Friss Ubuntu telepítés

Ubuntu 24.04+, Python 3.11 vagy újabb:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
# A repository tetszőleges könyvtárba klónozható.
git clone https://github.com/feco9308/crypto-bot.git
cd crypto-bot
git switch feature/trading-core-paper
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m pip install -e .
cp .env.example .env
market-scanner init-db
pytest -q
```

A feature branch csak akkor klónozható a GitHubról, ha az implementáció commitjai fel vannak pusholva. Helyi checkoutból a `git clone` és `git switch` kihagyható. A `.env`, SQLite-adatbázisok és sidecar fájljaik, logok és virtualenv gitignore-olva vannak. A `pyproject.toml` a csomag és CLI belépési pontok forrása; `requirements.txt` és `requirements-dev.txt` rögzíti az itt tesztelt dependency verziókat. Production környezetben `pip install -r requirements.txt` és `pip install -e .` elegendő. Nem használunk meglévő virtualenvet.

## Indítás

Külön terminálokban, ugyanabból a repository gyökérből, aktivált virtualenvvel:

```bash
# Egyetlen scanner-futás (hiba / nulla eredmény esetén exit 1)
market-scanner scan --once
# Folyamatos scanner, alapértelmezetten futás után 300 másodperc szünet
market-scanner scan
# Development dashboard
market-scanner web
```

A fejlesztői és production webserver alapértelmezetten `0.0.0.0:8000` címen figyel, így LAN-ról is elérhető: `http://SZERVER_LAN_IP:8000`. Helyben a `http://127.0.0.1:8000` cím is működik. Kompatibilis belépési pontok: `python trading_bot.py` és `python dashboard.py`. A webserver nem indít scannert; több Gunicorn worker sem sokszorozza meg a Binance-lekéréseket. **Egy adatbázishoz egy scanner folyamatot indíts.** SIGINT/SIGTERM után az aktuális futás befejeződik, a várakozás megszakad.

Az időbélyegek UTC-ben jelennek meg és UTC-ben tárolódnak. Ár: utolsó lezárt gyertya záróára; spread és 24h quote volume: aktuális ticker-pillanatkép. A kettő eltérő időablakot reprezentál, ezért nem tekintendő tick-pontosságú szinkron snapshotnak.

## Konfiguráció

A `Settings` validálja az értékeket. Sorrend: beépített alapértékek → opcionális JSON-fájl → környezeti változók (a `.env` csak a még nem beállított environment értékeket tölti be).

```bash
cp config/scanner.example.json config/scanner.json
# .env-be:
# SCANNER_CONFIG=config/scanner.json
# SCANNER_WEIGHTS={"trend":25,"momentum":20,"volume":20,"volatility":15,"liquidity":20}
```

A webes bind külön, a scanner konfigurációjától függetlenül állítható `.env`-ből vagy környezeti változókból:

```dotenv
WEB_HOST=0.0.0.0
WEB_PORT=8000
```

Ezek a defaultok a `market-scanner web`, `python dashboard.py` és a Gunicorn konfiguráció esetén is érvényesek. A fejlesztői CLI `--host` és `--port` kapcsolói felülírják az environment értékeket, például `market-scanner web --host 0.0.0.0 --port 8080`. A bind módosítása után indítsd újra a webfolyamatot.

Minden scanner-beállítás felülírható `SCANNER_` + a mező nagybetűs nevével. Így a `scanner_interval` változó neve `SCANNER_SCANNER_INTERVAL`. Listákat és dictionaryket JSON-ként adj meg. A teljes mezőkészlet: [settings.py](crypto_bot/config/settings.py).

| Beállítás | Alapérték / jelentés |
| --- | --- |
| quote_asset / number_of_markets | USDT / TOP 50 |
| timeframe | 1h; támogatott: 5m, 15m, 1h, 4h, 1d |
| scanner_interval | 300 másodperc, az előző futás végétől |
| minimum_quote_volume | 5 000 000 quote egység / 24h |
| maximum_spread | 0.2 százalékpont; `(ask-bid)/mid × 100` |
| auto_watchlist_size / top_score_size / top_risers_size | 5 / 5 / 5 |
| ema_fast / ema_slow / rsi_period / atr_period | 50 / 200 / 14 / 14 |
| volume_period / range_period / price_momentum_period | 20 / 20 / 4 gyertya |
| candle_limit | 500 (maximum 1000, legalább kétszeres slow EMA warmup + nyitott gyertya) |
| momentum_windows / riser_window | [1,4,24] óra / 4 óra |
| history_tolerance_minutes | 20 perc a keresett időpont előtt |
| request_pause / api_timeout / api_retries | 0.25s / 10s / 3 újrapróbálás |
| request_weight_budget | 1000 / perc, konzervatív lokális keret |
| database_url | sqlite:///data/market.db; relatív út a working directoryhoz |

Az indikátorperiódusok módosításakor az `ema50`/`ema200` history mezőnevek kompatibilitás miatt maradnak; tényleges jelentésük az adott futás tárolt konfigurációjából olvasható ki. A scanner nem optimalizálja a paramétereket profitra.

## Market discovery és adatminőség

A publikus `/api/v3/exchangeInfo`, `/ticker/24hr`, `/ticker/bookTicker` adatokból aktív, Spot-engedélyezett USDT univerzumot építünk. Először kizárjuk a stablecoin alapú párokat, leveraged tokeneket, alacsony quote volume-ot, hibás bid/askot és nagy spreadet; a fennmaradó piacokat 24h quote volume szerint rendezzük, majd TOP N-et választunk. Az IGNORE nem befolyásolja az adatgyűjtést vagy a rangsorokat.

A stable asset lista, kizárt assetek, leveraged suffixek és kivételek konfigurálhatók. A suffix-szűrés heurisztikus; a JUP explicit kivétel, mert normál token. Új tokeneknél a listákat karban kell tartani. WATCH/PINNED piacok a TOP N-en és quote univerzumon kívül is követhetők, de csak aktív Spot piacokról kérünk gyertyákat. Ezek a manuális piacok nem kerülnek automatikus rangsorkiválasztásba, amennyiben az automatikus univerzumnak nem felelnek meg.

Csak lezárt gyertyákat értékelünk. Nem elégséges warmup, hiányzó/duplikált időpont, hibás OHLCV vagy túl régi utolsó gyertya esetén az instrumentum hibát kap. Egy instrumentum hibája nem állítja le a többieket; a futás `partial` lehet. Discovery-hiba vagy nulla pontozott eredmény `failed`. A részleges futás csak sikeres új snapshotokat publikál; a kézi piacok korábbi adatai külön régi adatként megmaradnak. Korábbi sikeres adatok sikertelen futás után is láthatók, régi adat jelöléssel.

API-védelem: timeout, soros lekérések és pacing, lokális súlykeret, Binance használt-súly header, exponenciális retry hálózati/5xx hibákra. 418/429 esetén a Retry-After alapján cooldown indul; a hátralévő instrumentumokra nem küldünk új kérést a cooldown alatt. Az egész gép/IP más alkalmazásainak forgalmát a lokális keret nem tudja szabályozni. [Binance market data dokumentáció](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market), [REST API / limits](https://developers.binance.com/en/docs/products/spot/rest-api).

## Market Score v1

`clip(x) = min(1, max(0, x))`. Öt komponens 0–1-es nyers értékét normalizált súlyokkal adjuk össze:

`component_points = raw_component × weight / sum(weights) × 100`

| Komponens | Súly | Első verzió képlete |
| --- | --- | --- |
| Trend | 25 | Az EMAfast > EMAslow, price > EMAfast, price > EMAslow feltételek teljesülő aránya |
| Momentum | 20 | Átlaga: clip(0.5 + momentum% / (2 × 3)), clip((RSI − 30) / 40), clip(range_position) |
| Relative volume | 20 | clip(relative_volume / 2) |
| Volatility | 15 | clip(1 − abs(ATR% − 2.5) / 2.5) |
| Liquidity | 20 | Átlaga: clip(quote_volume / 100 000 000), clip(1 − spread% / maximum_spread) |

A képlet küszöbei konfigurálhatók. A score bullish struktúrákat részesít előnyben; magas RSI itt momentumjellemző, nem vételi utasítás. Az ATR-komponens közepes volatilitást preferál, a szélsőségeket csökkenti.

EMA: rekurzív exponenciális átlag `alpha=2/(period+1)`; RSI/ATR: Wilder kezdeti egyszerű átlag és simítás. Flat RSI = 50. Volume átlag: az utolsó gyertyát **megelőző** 20 gyertya, relative volume = utolsó volume / átlag. Price momentum: négy gyertyás záróárváltozás %. Range position: `(price − low20)/(high20 − low20)`, lapos tartományban 0.5. Volatility: rövid logika helyett egyszerű százalékos záróárhozamok populációs szórása a range ablakon; az ATR% külön tárolódik. Az aktuális/átlagos volume, 24h price change, quote volume és a teljes feature-készlet szintén mentésre kerül.

Minden snapshot tartalmazza a komponenseket és az ember számára olvasható indoklást, beleértve a rendelkezésre álló score-deltákat. Ugyanezek a tiszta indikátor- és scoring-függvények később historikus gyertyákon is használhatók; a teljes score rekonstruálásához korabeli spread és 24h tickeradat is szükséges.

## Score history és momentum

`previous_score`: a history-ablak legutóbbi korábbi kompatibilis score-ja. Minden W órás ablaknál a `now − W hours` időpont **előtt vagy azzal egy időben** lévő legutóbbi snapshotot keressük, legfeljebb 20 perc eltéréssel:

`delta_Wh = current_score − score_Wh_ago`

Nincs jövőbeli adat, interpoláció vagy hiányzó adatra kitalált nulla. Más timeframe, scoring-verzió vagy eltérő publikus konfiguráció nem összehasonlítható. Új telepítésnél a delták kezdetben üresek. Ritkább scanner-intervallumnál a history tolerance értékét tudatosan igazítani kell. A konfigurációváltozás utáni első összehasonlítható ablakok újra felépülnek; a korábbi history megmarad.

TOP MARKET SCORE = legmagasabb aktuális score. TOP SCORE RISERS = legnagyobb **pozitív**, elérhető delta a konfigurált riser_window alatt, alapból Δ4h. A score momentum külön számítás, nem része az aktuális Market Score-nak.

## Auto Watchlist és manuális kontroll

Az automatikus univerzumból az auto_min_score (50), relative volume (0.5x), ATR% (0.1–10%) küszöböknek megfelelő piacok:

`selection_priority = score + 0.5 × max(0, delta_4h)`

A legmagasabb prioritású N piacot választjuk; azonos értéknél symbol a determinisztikus tie-breaker. Hiányzó delta nem ad bónuszt. A trend, volume, volatility és liquidity a Market Score-on keresztül is részt vesz a kiválasztásban. A döntés és indoka minden sikeresen pontozott piacnál tárolódik.

| User override | Effective state / működés |
| --- | --- |
| AUTO | WATCH, ha az algoritmus kiválasztotta; különben AUTO |
| WATCH | Kézzel figyelt, automatikus döntéstől függetlenül követjük |
| PINNED | Mindig kiemelt manuális piac; sikertelen lekérés esetén is látható |
| IGNORE | Tényleges watchlistből kizárt; score-gyűjtés folytatódik |

Az algoritmikus kiválasztás **független** a user override-tól: IGNORE mellett az algorithm_watch történetileg igaz lehet, effective_state viszont IGNORE. A dashboard AUTO WATCHLIST blokkja a kiválasztott piacokból kiszűri az IGNORE-t, ezért mérete csökkenhet; nem írjuk át a program eredeti döntését. WATCH/PINNED és algoritmikus kiválasztás együttesen is felismerhető.

## Web UI

Hat külön blokk: TOP MARKET SCORE, TOP SCORE RISERS, AUTO WATCHLIST, MANUAL WATCHLIST, PINNED, IGNORED. A táblázat kereshető, oszlopfejlécekkel rendezhető score, delták, relative volume, momentum és volume szerint is. A `Mentés` gomb persistálja a user override-t; algoritmikus döntést nem módosít. A symbol mezőben a discoveryből ismert, TOP N-en kívüli piac is kézzel hozzáadható.

Instrumentumra kattintva: komponensek, indikátorok, deltaértékek, indoklás, státuszok és legutóbbi 1000 history-pont. A saját canvas grafikon közös, tényleges időtengelyen mutatja a score-t és árat eltérő, feliratozott skálákkal. Nagyobb adathiányokat nem köt össze; az alatta lévő táblázat JavaScript nélkül is használható. A felület explicit frissítést használ; nincs Binance-hívás webes GET során.

Read-only JSON endpointok: `/api/markets`, `/api/history/<symbol>`. Override-módosítás: CSRF-védett POST `/override`; GET nem ír üzleti adatot. Ez egyetlen operátorhoz készült lokális dashboard, nincs felhasználói bejelentkezés vagy többfelhasználós jogosultságkezelés.

## Adatbázis és migráció

SQLite WAL módban, foreign key enforcementtel és 30s busy timeouttal. SQLAlchemy ORM/repository boundary. `market-scanner init-db` idempotensen létrehozza a v1 sémát; meglévő táblákat nem alakít át csendben. A `schema_version` ellenőrzi a támogatott verziót. Következő séma-változáskor verziózott migráció szükséges, adatvesztéses újrainicializálás helyett.

Táblák:

- `instruments`: symbol, base/quote asset, aktív és Spot állapot, frissítés ideje.
- `scanner_runs`: UTC időpont, timeframe, konfiguráció, státusz, számlálók, hibák, befejezés.
- `snapshots`: teljes/component score, ár, RSI, EMA50/200, ATR, relative volume, spread, további feature-k, delták, indokok és scoring-verzió.
- `decisions`: futásonkénti algorithm_watch és kiválasztási indok.
- `overrides`: aktuális user override és módosítás ideje.
- `override_events`: AUTO/WATCH/PINNED/IGNORE változások audit trailje.

A futás eredményei és döntései egy tranzakcióban publikálódnak. Megszakított folyamat után a `running` státusz megmaradhat bizonyítékként; az új futás új rekordot készít. Nincs retenció/törlés, backup vagy gyertyaarchívum ebben a verzióban. A history indexelt symbol/timeframe/idő szerint. Konfiguráció és user override újraindítás után a run historyban/override táblában megmarad; aktív konfiguráció forrása továbbra is JSON/env.

PostgreSQL-hez később külön driver telepítése és database_url váltás szükséges, plusz migráció/átköltöztetés. Az ORM nem garantálja a meglévő SQLite-adatok automatikus átvitelét; a PostgreSQL üzemet ebben a mérföldkőben nem teszteltük.

## Production / systemd

A minták `/opt/crypto-bot` útvonalat és `crypto-bot` rendszerfelhasználót használnak; a repository bárhol lehet, az unitokban ezt igazítsd az aktuális abszolút útvonalhoz. Nincs korábbi szerverútvonaltól való függés.

```bash
sudo useradd --system --user-group --home-dir /opt/crypto-bot --shell /usr/sbin/nologin crypto-bot
# A repositoryt /opt/crypto-bot alá helyezd, ott készítsd el a virtualenvet és .env-et.
# Production secret: .env SCANNER_SECRET_KEY értékébe tartós véletlen kulcs:
python3 -c 'import secrets; print(secrets.token_hex(32))'
# Előbb az aktuális checkoutban a telepítési lépések és init-db:
sudo mkdir -p /opt/crypto-bot/data
sudo chown -R crypto-bot:crypto-bot /opt/crypto-bot
sudo chmod 600 /opt/crypto-bot/.env
sudo -u crypto-bot sh -c 'cd /opt/crypto-bot && .venv/bin/market-scanner init-db'
sudo cp /opt/crypto-bot/deploy/crypto-scanner.service /etc/systemd/system/
sudo cp /opt/crypto-bot/deploy/crypto-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-scanner crypto-dashboard
journalctl -u crypto-scanner -f
```

Dashboard production parancs: `.venv/bin/gunicorn --config gunicorn.conf.py 'crypto_bot.web.app:create_app()'`. A `gunicorn.conf.py` alapból `0.0.0.0:8000` címet és két workert használ, a bindot a `WEB_HOST`/`WEB_PORT` értékekből olvassa. Scanner és web külön szolgáltatás. Tartós secret szükséges, hogy több worker ugyanazt a session/CSRF kulcsot használja.

Nginx Proxy Manager másik gépen: a Proxy Host célja `http`, Forward Hostname / IP = a dashboard szerver LAN IP-je, Forward Port = `8000` (vagy a konfigurált `WEB_PORT`). A `0.0.0.0` bind cím, nem a proxy célcíme. Az alkalmazás alapértelmezett LAN-bindjához nincs szükség SSH tunnelre. Nincs szükség Binance credentialsre.

Meglévő systemd telepítés frissítése a dashboard unit újbóli másolása után:

```bash
sudo systemctl daemon-reload
sudo systemctl restart crypto-dashboard
sudo ss -ltnp | grep :8000
```

A Local Address:Port oszlopban `0.0.0.0:8000` legyen. A scanner újraindítása nem szükséges. A systemd minták telepítéskor a saját útvonalakhoz igazítandók.

## Logging és tesztek

JSON logging stdout/stderr felé: SCANNER_START, SCANNER_COMPLETE (jelöltek, scored, errors, TOP SCORE, TOP RISERS), SCANNER_FAILED, INSTRUMENT_ERROR, API_RATE_LIMIT, AUTO_WATCHLIST_CHANGE és USER_ACTION. systemd alatt journald tárolja. Secret és adatbázis-URL nem kerül a konfigurációs snapshotba.

```bash
pytest -q
python -m compileall -q crypto_bot
node --check crypto_bot/web/static/dashboard.js  # opcionális frontend syntax check
```

A tesztek nem használnak élő Binance kapcsolatot: kézzel ellenőrizhető indikátorértékek, normalizáció és szélsőségek, időalapú momentum/no-lookahead, market filterek, kiválasztás, mind a nyolc AUTO/manual prioritáskombináció, részleges hibák, adatbázis újranyitás, CSRF, API retry/timeout/cooldown és dashboard/history integráció.

## Legacy és következő mérföldkő

A `legacy/` könyvtár az eredeti README-t, trading_bot.py-t, dashboard.py-t, backtest.py-t és watchlist fájlokat őrzi. A gyökérben backtest.py, watchlist.py/json és image.png szintén változatlan referencia. A régi backtest csak meglévő CSV-vel és az opcionális `pip install -e '.[legacy]'` függőségekkel használható; új scanner nem gyárt signals_log.csv-t. Az eredeti legacy dashboard/trading_bot továbbra is régi configot igényel, ezeket ne használd az új platform indításához.

A tiszta `crypto_bot/strategies/legacy_rsi_ema.py` megőrzi az eredeti pandas adjusted EMA9/21 és ta RSI14 szemantikát (flat RSI ott 100), valamint RSI-only és combined BUY/SELL/WAIT jelzéseket. Ezek kizárólag összehasonlítható információk, a scanner nem hívja execution célra. A régi backtest optimista fills/fee és CSV-formátum korlátait az audit rögzíti.

Következő mérföldkő: verziózott migrációk, retenció/backup, historikus OHLCV és spread/ticker archiválás, megfigyelési lefedettség és üzemeltetési metrikák, score-emelkedést követő 1h/4h/12h/24h ármozgás elemzése. Ezután külön stratégia- és risk réteg, majd paper trading. Profitoptimalizálás és live execution külön, későbbi feladat.

## Paper Trading Core — v0.2.0

**NO LIVE TRADING IMPLEMENTED.** Kizárólag belső, szimulált long-only account, publikus scanner snapshotokkal. Nem valódi wallet, nincs Binance trading API-kulcs, broker adapter, margin, futures, leverage vagy short. Az RSI/EMA legacy stratégia és a scanner score/selection képletei változatlanok.

A `feature/trading-core-paper` branch a `feature/market-scanner-v2` utódja. Az aktuális fejlesztés külön worktree-ben történt (`/home/ndvi/crypto-bot-paper`); az eredeti gyűjtő (`/home/ndvi/crypto-bot`) futó folyamata és adatbázisa nem lett átállítva. Az eredeti scanner újraindítását és a napi gyűjtés utáni átállítást külön kell elvégezni. Ne indíts egy második scannert ugyanarra a gyűjtésre.

### Architektúra és audit

Persistált scanner snapshot → `StrategyEngine` → `StrategySignal` → `RiskManager` → `PortfolioService` cash-foglalás → `ExecutionService` → `PaperExecutionService` → fill/ledger → adatbázis.

- `trading/domain.py`: közös, Decimal-alapú signal/risk/portfolio objektumok. BUY/SELL/HOLD; strength 0–1 relatív erősség, nem valószínűség.
- `trading/strategy.py`: cserélhető, tiszta stratégia; nincs API vagy orderküldés.
- `risk/manager.py`: tiszta engedélyezés és méretezés; nem küld ordert.
- `portfolio/service.py`: belső könyvelés; nem kommunikál tőzsdével és nem küld ordert.
- `execution/base.py`: place_order/cancel_order/get_order/get_positions interfész. `execution/paper.py`: kizárólag helyi paper végrehajtás. Későbbi adapter az orchestration execution_factory-jával cserélhető, a strategy és risk réteg átírása nélkül.
- `trading/engine.py`: az adatbázisban már szereplő friss snapshotokat használja; nem indít Binance market data hívást.

Minden signal a scanner snapshothoz kapcsolódik, a risk decision a signalhoz, az order a signal/riskhez, a fill az orderhez és positionhöz. Az auditoldal megmutatja a score-t, indikátorokat, paramétereket, döntéskori portfóliót, risk indoklást, fillt, fee/slippage költséget és zárást. A reset ezeket a paper rekordokat is törli; reset előtt exportálj/ments, ha meg akarod őrizni őket.

### Adatvesztés nélküli migráció

Alembic `0001_scanner`: meglévő scanner v1 séma adoptálása; `0002_paper`: új paper/monitoring táblák. A `schema_version` továbbra is **1**, így a régi scanner kompatibilis marad. Az extension verzióját az `alembic_version` tárolja. A migration nem írja át a meglévő score-okat, instrumentumokat vagy override-okat.

A futó napi gyűjtés alatt ebben a munkamenetben nem migráltuk az eredeti adatbázist. Először másolaton ellenőrizd. Aktív SQLite esetén ne egyszerűen a `.db` fájlt másold: WAL miatt SQLite backupot használj, például:

```bash
# Saját abszolút forrás/cél útvonalak; cél új adatbázis legyen.
python3 - <<'PY'
import sqlite3
source = sqlite3.connect('file:/ABS/PATH/market.db?mode=ro', uri=True)
target = sqlite3.connect('/ABS/PATH/market-backup.db')
source.backup(target)
target.close()
source.close()
PY
```

Az új checkoutban/virtualenvben:

```bash
pip install -r requirements-dev.txt
pip install -e .
# .env: SCANNER_DATABASE_URL a migrálni kívánt adatbázisra mutasson.
market-scanner migrate
market-scanner paper init
market-scanner paper status
```

A migráció explicit parancs; a web vagy a scanner indítása önmagában nem migrál. Az account inicializálás idempotens és **OFF** állapotból indul. A paper táblák: paper_account, strategy_signals, risk_decisions, paper_orders, paper_fills, paper_positions, paper_portfolio_snapshots, paper_processed_snapshots. Monitoring: service_status, system_events. Sem a reset, sem a migration nem törli a scanner historyt/manual override-ot.

### Indítás, leállítás, termináltól független futás

```bash
market-scanner paper run        # engine fut, trading kezdetben OFF
market-scanner paper on         # új pozíció nyitható
market-scanner paper off        # új belépés tiltva; engine/stop/manual exit tovább működhet
market-scanner paper status
market-scanner paper run --once # egy auditált ciklus
market-scanner paper close --position 1
```

A paper run foreground módban SIGINT/SIGTERM-mel állítható le. A kill switch OFF állapota adatbázisban megmarad. Az engine tartós account lease-t használ: második engine vagy reset aktív lease mellett elutasított. Hard crash után a lease lejártáig várj (legalább 120s, loop interval alapján hosszabb lehet).

Terminál bezárását túlélő indítás, sudo nélkül:

```bash
./scripts/background.sh start paper
./scripts/background.sh start web
./scripts/background.sh status all
./scripts/background.sh stop paper
./scripts/background.sh stop web
```

A helper setsid + nohup segítségével külön sessiont indít, saját PID-fájlokkal és `data/services/` logokkal. Egy másik checkout folyamatait nem kezeli. A `start scanner` és `start all` **csak a jelenlegi gyűjtés utáni tervezett átállításkor** használható; most ne indítsd a meglévő gyűjtő mellé. A helper nem indít újra gépreboot után; arra systemd ajánlott.

Production: a meglévő scanner/web unit mellé `deploy/crypto-paper.service` került. Az `/opt/crypto-bot` útvonalak és user igazítása, telepítés, migrate és paper init után:

```bash
sudo cp deploy/crypto-paper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-paper
sudo systemctl stop crypto-paper
journalctl -u crypto-paper -f
```

A gyűjtés utáni teljes átállításkor a scanner és dashboard minták ugyanígy telepíthetők. A systemd szolgáltatások a terminál bezárása és reboot után is futtathatók. Ebben a munkamenetben az eredeti scanner/web szolgáltatásokat nem telepítettük újra és nem indítottuk újra.

### Referencia-stratégia

Entry feltételek: effective WATCH/PINNED, score ≥70, elérhető Δ4h ≥0, EMAfast > EMAslow, RSI 40–70, relative volume ≥1. Stop = reference price − 2×ATR. Exit: score <45 vagy bearish EMA, application stop, opcionális take profit vagy manual close. Hiányzó momentum nem kitalált nulla: alapból HOLD. A `require_delta=false` csak kifejezett konfigurációval kapcsolható ki.

Minden küszöb konfigurálható `PAPER_CONFIG` JSON-fájllal vagy `PAPER_<FIELD>` environment változóval. Példa: `config/paper.example.json`; az env felülírja a JSON-t. A konfiguráció minden signal mellett persistál. Opcionális take profit: `take_profit_r_multiple`, alapból 0 = kikapcsolva. Az alkalmazás nem optimalizál a profitra.

### Risk és portfolio

| Paraméter | Default |
| --- | --- |
| initial_paper_balance | 1000 USDT |
| risk_per_trade_pct | 0.5% |
| max_open_positions | 3 |
| max_total_exposure_pct | 50% |
| max_symbol_exposure_pct | 20% |
| daily_loss_limit_pct | 2% |
| max_drawdown_limit_pct | 5% |
| minimum_order_value | 10 quote egység |
| paper_fee_pct / paper_slippage_pct | 0.1% / 0.05% |
| pyramiding | false; true nem támogatott |
| loop_interval / max_snapshot_age_seconds | 15s / 900s |

A `.env.example` tartalmazza az env neveket (például `PAPER_RISK_PER_TRADE_PCT`, `PAPER_PAPER_FEE_PCT`). Egyetlen quote account működik, ezért USDC snapshotot nem értelmezünk USDT-s árként. Quote asset váltás explicit resetet igényel.

Kockázati keret = equity × risk%. Quantity = keret / egységnyi stop-veszteség; belépési slippage, stopnál várható kilépési slippage és mindkét fee beleszámít. Majd cash, total/symbol exposure korlátozza a méretet. Költség nélküli 100→98 stop és 1000 equity mellett 0.5% riskből 250 notional adódik, de a default 20%-os symbol cap ezt legfeljebb 200-ra csökkenti. A méret lefelé kerekített; minimum alatti order elutasított. SELL meglévő pozícióra akkor is engedélyezett, ha a belépési limit vagy OFF blokkolja az új BUY-t.

Cash a foglaláskor nem fogy el, available cash = cash − reserved; fillkor a foglalás felszabadul és a tényleges notional+fee levonódik. SELL-kor notional−fee kerül vissza cashbe. Realized PnL = kilépési nettó bevétel − teljes belépési költség. Equity = cash + nyitott pozíciók megfigyelt piaci értéke; unrealized PnL tartalmazza a belépési fee-t. Fees külön is kimutathatók. Napi PnL a UTC napi equity-változás; a nap első feldolgozásakor a korábbi mark szerinti equity az alap. Drawdown százalék a megfigyelt peak equityhez képest; maximum történetileg tárolódik. A maximum drawdown limit resetig blokkolja a belépést, a napi limit UTC napi átfordulással újraalapozódik.

Market order lifecycle: CREATED → FILLED/CANCELLED/REJECTED. A fill a scanner snapshot záróárához képest adverse slippage-pel történik. Pending BUY cash-t foglalhat; cancel/OFF felszabadítja. Fill előtt ismét ellenőrzünk, így időközben megváltozott limitek vagy pozíció nem írják felül a kontrollokat. Részleges fill/partial SELL nincs ebben a verzióban.

A ledger Decimal értékeket használ, 12 tizedesre lefelé kerekítve; SQLite-ban szövegként tároljuk őket, így a SQLite floating-point affinity nem veszít pontosságot. PostgreSQL-ben NUMERIC a megfelelő típus. Minden paper ciklus és manuális művelet egy sorosított ledger tranzakció. Egy scanner snapshot stratégiai feldolgozása egyszeri; OFF alatt feldolgozott snapshotot ON után sem játszunk vissza. Új belépéshez új snapshot kell. Létező pozícióhoz nincs újabb BUY/pyramiding. Stop/take-profit exit után ugyanabban a ciklusban nincs új belépés.

**Ármodell korlát:** a scanner historikus/lezárt gyertya pillanatképét használjuk, nem tick streamet. Stop nem lát intrabar low-t vagy a két megfigyelés közötti árat; gap és késleltetés miatt a realizált veszteség nagyobb lehet a tervezett risknél. Friss ár hiányában a pozíció nem kap kitalált fillt; manual close is vár a friss snapshotra. A stale threshold ezért számít, és ez a referencia pipeline nem profit-backtest.

### Paper dashboard és reset

`/paper`: portfolio, ENGINE státusz és külön TRADING ON/OFF, open positions, kézi zárás, legutóbbi 200 trade/signal és auditoldalak. A scanner nézet külön megmarad. Az ON/OFF és zárás CSRF-védett POST; GET nem kereskedik. Nincs live kapcsoló. Manuális zárás `exit_reason=MANUAL`. ON csak a paper accountot aktiválja, nem indít OS processt.

Biztonságos reset, CLI-ből:

```bash
market-scanner paper off
# Előbb állítsd le a paper engine-t: Ctrl+C / helper stop / systemctl stop.
market-scanner paper reset --confirm RESET-PAPER
```

Megerősítés nélkül, trading ON mellett vagy aktív engine lease esetén a reset elutasított. Csak a paper positions/orders/fills/signals/risk/snapshots/processed ledger törlődik; a bank konfigurált kezdő tőkével OFF állapotba kerül. Scanner score/history és manuális watchlist megmarad.

### System Overview / monitoring

`/services`: Market Scanner, Web Dashboard, Strategy Engine, Risk Manager, Portfolio / Wallet, Paper Trading Engine, Database. Name/type/status/version/git metadata, started_at, heartbeat, last_success/error, restart count ahol ismert. Belső modulok Componentként szerepelnek, process heartbeat nélkül. Az alkalmazás központi verziója: `crypto_bot/version.py`; package metadata ezt használja.

Heartbeat: `MONITOR_HEARTBEAT_INTERVAL=30`, `MONITOR_STALE_THRESHOLD=120`; háttérszál írja service_statusba. A scanner és paper engine következő indításkor kapja meg. A most futó régi scanner állapota **inferred from scanner runs**, heartbeatje és verziója ismeretlen; a scanner-intervalhoz igazított freshnessből következtetünk, és ezt jelöljük. Részleges instrumentumhiba DEGRADED, discovery/cycle hiba ERROR. A Web Dashboard a worker heartbeatjeit aggregálja; pontos OS restart count nem ismert.

Healthy zöld, paused sárga, degraded narancs, error/stale piros, stopped szürke. A főoldal összesítést és ERROR/STALE esetén figyelmeztetést mutat. Paper engine RUNNING és paper trading OFF egyszerre érvényes állapot. A health nem változtat score-t, selectiont, risk képletet vagy pénzügyi könyvelést.

`/health`: rövid, secret nélküli 200/503 válasz. `/api/system/status`: részletes komponensek/DB health/version/event history. A Database type/name, scanner schema és Alembic revision, connection állapot és legutóbbi ismert üzleti írás látható; credential nincs a válaszban. Az events táblában default maximum 500 rekord marad (`MONITOR_EVENT_RETENTION_COUNT`), UI legutóbbi 30. A paper/score history retenció külön jövőbeli fejlesztés.

A dashboard továbbra is operátori felület, saját login nincs; Nginx Proxy Manageren az operátori hozzáférést az előtte lévő access control biztosítsa. Binance trading credentialre nincs szükség.

### Validáció és következő lépés

A tesztek fixed Decimal értékeket, mock market data-t és külön SQLite adatbázisokat használnak. Scanner regresszió, migráció/persistence, signal, risk sizing/limitek, cash/reserved/PnL, fee/slippage, buy/sell/cancel/reject, stop/TP/manual, OFF, replay/duplicate, reset, rollback, heartbeat/health/event retention és web kontroll ellenőrzése szerepel bennük. Részletes futási eredmény: `docs/PAPER_VALIDATION.md`.

Következő mérföldkő: a napi gyűjtés utáni kontrollált átállítás; paper adatok megfigyelése és ledger reconciliation; pontosabb árstream/intrabar stop modell; historikus OHLCV/spread archiválás, retention/backup és PostgreSQL integration. Live execution továbbra is külön feladat, itt nincs implementálva.
