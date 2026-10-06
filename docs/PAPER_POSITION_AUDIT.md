# Paper Trade / Position Audit

NO LIVE TRADING IMPLEMENTED. A fejlesztés kizárólag a webes megjelenítési réteget
és tesztjeit bővíti. Nem változtatja meg a scannert, Market Score-t, Strategy-t,
Risk Managert, Executiont vagy Portfolio könyvelést. Nincs új migration.

## Oldalak és endpointok

| GET útvonal | Tartalom |
| --- | --- |
| `/paper/trades/<position_id>` | Közös OPEN / CLOSED pozíciórészletek |
| `/api/paper/trades/<position_id>/chart?interval=5m` | Public OHLC vizualizáció, overlay metadata |
| `/api/paper/trades/<position_id>/scores` | Valós scanner snapshot ID, UTC, score, Δ4h |
| `/api/paper/trades/<position_id>/quote` | OPEN pozíció public bidje és indikatív megjelenítési metrikái |

A `/paper` Open Positions és Trade History sorai a részletoldalra navigálnak.
A natív Details link billentyűzettel és JavaScript nélkül is használható.
Close gomb, link, kijelölés vagy módosító billentyűs kattintás nem indít sornavigációt.
A Signal History meglévő `/paper/signals/<signal_id>` auditja megmarad;
a pozícióoldal entry/exit linkjei és a signal oldal visszalinkje összekötik a nézeteket.

## Audit adatok

Összefoglaló: státusz, strategy, entry/exit idő, duration, quantity, notional,
entry/current/exit ár, nettó realizált és tárolt unrealized PnL, összes fee,
return % (könyvelt PnL / (entry notional + entry fee)), stop, TP és kapcsolódó ID-k.

A belépési audit a kapcsolt scanner snapshotot, score komponenseket, Δ1h/4h/24h-t,
indikátorokat, mentett belépési jogosultságot, signal reasons-t, konfigurációt,
risk döntést és akkori portfolio adatokat mutatja. A risk budget az akkori equity
és a mentett risk % megjelenítési szorzata; az approved risk amount az eredeti rekord.
Exposure az akkori mentett pozíciólistából, available cash a mentett cash/reserved
értékekből származik. Ez nem új Risk Manager döntés.

**STRATEGY REFERENCE PRICE** és **EXECUTION QUOTE / FILL PRICE** külön blokk.
A risk és order public quote metadata, bid/ask, receipt time, fill, fee és
slippage visszakereshető a kapcsolt rekordokban. Slippage a quote assetben
elszámolt költség. Hiányzó audit quote esetén `not recorded` jelenik meg;
execution auditban nincs candle-close fallback.

CLOSED pozíciónál külön exit audit jelenik meg. STRATEGY esetén az eredeti
signal reason szó szerint olvasható (például score threshold, bearish EMA).
STOP / TAKE_PROFIT esetén a tárolt bid, stop/TP küszöb és `bid ≤ stop`, illetve
`bid ≥ TP` feltétel látható. MANUAL esetén a kézi zárás és eredeti quote/fill szerepel.

A timeline időrendben mutatja a tárolt snapshot, signal, risk, order, fill,
position open/close és utolsó mark eseményeket UTC-ben. A RiskRecord nem tárol
külön timestampet: a kapcsolt signal időpontját használjuk, ezt megjelöljük.
Az összes köztes mark nincs pozíciószinten archiválva; nem találunk ki eseményeket.
Egy STOP/MANUAL exit a régi entry snapshotra is hivatkozhat: annak tényleges,
régebbi timestampje látható, nem nevezünk egy régi snapshotot új mérésnek.

## Vizualizáció, elkülönítés és korlátok

A webhez tartozó `PublicChartData` kizárólag public HTTP GET-et küld a
[`/api/v3/klines` és `/api/v3/ticker/bookTicker` endpointokra](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints).
Nem kér API keyt, nincs signed kérés, order endpoint vagy trading client.
A Strategy és scanner nem importálja ezt a modult. A vizualizáció eredménye nem kerül
az adatbázisba; az executionhöz használt quote-ok meglévő audit tárolása változatlan.

A függőség nélküli canvas candlestick chart alapértelmezése 5m, kapcsolók:
1m, 5m, 15m, 1h. Entry előtt két órától a teljes pozícióidőszakon át,
CLOSED esetén exit után legfeljebb egy óráig, OPEN esetén aktuális UTC-ig tart.
BUY/SELL marker, entry/stop/TP vonal, zoom, pan és OHLC tooltip támogatott.
EMA overlay nem része ennek a verziónak (opcionális specifikációs elem).
A chart nyitott gyertyát is mutathat, kizárólag vizualizációként.

A szerver 1000 gyertyás lapokban szolgál ki (`next_start`, majd `start` és `end`
query paraméter). Nincs csendes, legutóbbi 1000 gyertyára vágás. A böngésző legfeljebb
50 lapot tölt egy körben; ennél hosszabb tartománynál látható figyelmeztetést ad,
és nagyobb timeframe választandó. A cache workerönként legfeljebb 128 bejegyzés,
klines TTL 30s, public quote TTL 15s. 418/429 válasz után backoff aktív.
Hiányzó, hibás vagy timeoutos upstream adat 503; az audit továbbra is használható.

A score-chart pontokat mutat a saját DB snapshotjaiból, az eredeti UTC időben,
entry/exit markerrel. Hiányzó Δ4h üres marad; nincs interpoláció vagy kitalált score.
A snapshotokat külön adat-táblázatban is felsoroljuk.

OPEN nézetben public bidből származó **indikatív**, nem könyvelt unrealized PnL,
notional exposure és stop/TP távolság jelenik meg. A summary eredeti könyvelt PnL-ja
és utolsó ledger markja külön látható. `received_at` a local receipt time, nem
Binance exchange update time. Hibánál a bid/metrikák `unavailable`, nincs fallback.
Aktív böngészőlapon a chart/score/bid 30 másodpercenként frissül; a price chart csak
a már betöltött history végét frissíti. A summary/frissen lezárt státusz új oldalletöltéskor frissül.

Mobilon a két pozíciótábla feliratozott kártyává alakul, a detail is kártyás.
A többi széles adat-tábla saját konténerében görgethető. A CSP `script-src 'self'`
változatlan, nincs CDN vagy külső script.

## Tesztelés

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q

# Opcionális tényleges Chromium tesztek:
.venv/bin/pip install -e '.[test,browser-test]'
.venv/bin/playwright install --with-deps chromium
.venv/bin/pytest -q tests/test_paper_trade_browser.py
```

A browser extra nélkül a böngészős modul skip, a 160 unit/integration teszt fut.
Browser extrával és Chromium rendszerfüggőségekkel összesen 164 teszt.
Minden új teszt fixed/mock adatot és elkülönített DB-t használ, élő Binance nélkül.

A böngészős tesztek 1440 px desktop és 390 px mobil méreten ellenőrzik:
row click, Close gomb elkülönítése, Details, entry/exit linkek, timeframe/zoom,
oldalirányú kilógás, chart adatkimaradás és kizárólag local GET kérések.
Képernyőképeket a gitből kizárt `artifacts/` könyvtárba írnak.

## Új fájlok

- `crypto_bot/web/trade_audit.py`: read-only audit projekció, timeline, score query.
- `crypto_bot/web/chart_data.py`: izolált public GET kliens, lapozás/cache/backoff.
- `crypto_bot/web/templates/paper_trade.html`: OPEN/CLOSED detail nézet.
- `crypto_bot/web/static/trade_audit.js`: candlestick/score chart, public bid UI.
- `tests/test_paper_trade_audit.py`: 26 unit/integration teszt.
- `tests/test_paper_trade_browser.py`: 4 tényleges böngészős teszt.
- `docs/PAPER_POSITION_AUDIT.md`: ez a dokumentáció.

Módosított: web route regisztráció, `/paper` és signal audit template,
shared CSS/row click JS, web modul docstring, README, opcionális browser-test extra
és generált artifact gitignore. Scanner / trading / risk / execution / portfolio /
storage model / migration kód nem változik.

## Validáció — 2026-10-06

- Teljes pytest csomag: **164 passed** (134 meglévő + 26 új unit/integration + 4 browser).
- Ruff és mindkét érintett JS fájl `node --check`: sikeres.
- Chromium desktop 1440 px / mobile 390 px: nincs vízszintes oldal-kilógás vagy JS page error.
- Public adatkimaradás esetén az audit továbbra is olvasható, a bid unavailable.
- A visualization GET teszt a paper üzleti táblák összes sorát változatlannak találta;
  a strategy context továbbra is az eredeti lezárt snapshot árát kapja.
- Nincs schema migration, új quote archívum vagy trading API.

Rögzített fixture-adatokból készült képernyőképek:

- [Mobil summary](screenshots/paper-audit-summary-mobile.png)
- [Desktop price/score chart](screenshots/paper-audit-charts-desktop.png)
- [Mobil paper pozíciókártyák](screenshots/paper-positions-mobile.png)
