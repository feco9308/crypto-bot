# Market Intelligence / Market Scanner

Moduláris, API-kulcs nélkül működő Binance Spot piaci megfigyelő. **Nem küld ordert, nem kereskedik automatikusan.** Nincs futures, margin, leverage vagy ML-predikció. A Market Score heurisztikus rangsorolási érték, nem hozam- vagy nyerési valószínűség.

Az első mérföldkő: automatikus USDT market discovery, indikátorok, magyarázható scoring, historikus tárolás, score momentum, automatikus piacválasztás és webes manuális kontroll. Az [audit és refaktorálási terv](docs/AUDIT.md) a GitHub `de95639` alapállapotát dokumentálja. Az eredeti szerver, config.py, CSV és API-kulcs nem szükséges.

## Friss Ubuntu telepítés

Ubuntu 24.04+, Python 3.11 vagy újabb:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
# A repository tetszőleges könyvtárba klónozható.
git clone https://github.com/feco9308/crypto-bot.git
cd crypto-bot
git switch feature/market-scanner-v2
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
market-scanner web --host 127.0.0.1 --port 8000
```

Dashboard: `http://127.0.0.1:8000`. Kompatibilis belépési pontok: `python trading_bot.py` és `python dashboard.py`. A webserver nem indít scannert; több Gunicorn worker sem sokszorozza meg a Binance-lekéréseket. **Egy adatbázishoz egy scanner folyamatot indíts.** SIGINT/SIGTERM után az aktuális futás befejeződik, a várakozás megszakad.

Az időbélyegek UTC-ben jelennek meg és UTC-ben tárolódnak. Ár: utolsó lezárt gyertya záróára; spread és 24h quote volume: aktuális ticker-pillanatkép. A kettő eltérő időablakot reprezentál, ezért nem tekintendő tick-pontosságú szinkron snapshotnak.

## Konfiguráció

A `Settings` validálja az értékeket. Sorrend: beépített alapértékek → opcionális JSON-fájl → környezeti változók (a `.env` csak a még nem beállított environment értékeket tölti be).

```bash
cp config/scanner.example.json config/scanner.json
# .env-be:
# SCANNER_CONFIG=config/scanner.json
# SCANNER_WEIGHTS={"trend":25,"momentum":20,"volume":20,"volatility":15,"liquidity":20}
```

Minden beállítás felülírható `SCANNER_` + a mező nagybetűs nevével. Így a `scanner_interval` változó neve `SCANNER_SCANNER_INTERVAL`. Listákat és dictionaryket JSON-ként adj meg. A teljes mezőkészlet: [settings.py](crypto_bot/config/settings.py).

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

Dashboard production parancs: `.venv/bin/gunicorn --workers 2 --bind 127.0.0.1:8000 'crypto_bot.web.app:create_app()'`. Scanner és web külön szolgáltatás. Tartós secret szükséges, hogy több worker ugyanazt a session/CSRF kulcsot használja.

Távoli operátori elérés: `ssh -L 8000:127.0.0.1:8000 USER@SERVER`, majd helyi böngésző. Publikus eléréshez tegyél elé TLS-es, autentikált reverse proxyt; az alkalmazást ne tedd ki szabadon az internetre. Nincs szükség Binance credentialsre. A systemd minták lokális példák; itt nem telepítettünk szolgáltatást a hostra.

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
