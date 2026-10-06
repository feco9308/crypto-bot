# Paper Trading Analysis / Read-only Export API

**NO LIVE TRADING · NO STRATEGY CHANGE · NO SCORE CHANGE · READ-ONLY ANALYSIS ONLY**

A réteg a meglévő paper döntések utólagos, leíró elemzésére szolgál. Nem optimalizál
stratégiát, nem ad új BUY/SELL jelzést vagy mesterséges good/bad entry minősítést.
Scanner, score, selection, strategy threshold, risk, execution és portfolio kód
változatlan. Nincs migration, új tábla, scanner history írás vagy tick archiválás.

## Oldal és endpointok

| GET útvonal | Tartalom |
| --- | --- |
| `/paper/analysis` | Portfolio Summary, Trade Statistics, Score vs Outcome, Entry Timing, Recent Trades, Download JSON/CSV |
| `/api/paper/analysis/trades` | Lapozott, strukturált audit és historical metrikák |
| `/api/paper/analysis/summary` | Cache-elt, háttérben számított statisztikák és score bucketek |
| `/api/paper/analysis/export.json` | Letölthető JSON metadata + trades |
| `/api/paper/analysis/export.csv` | Letölthető CSV; egy trade/sor, UTF-8 BOM, CRLF |
| `/api/paper/analysis/meta` | CSV-hez külön metadata |

Csak GET/HEAD/OPTIONS. POST/DELETE/PUT nem engedélyezett; nincs ON/OFF, close,
permission, reset vagy config változtatás ezen a rétegen keresztül.

Közös opcionális query paraméterek:

- `status=OPEN|CLOSED`, `symbol=BTCUSDT`, `strategy=watchlist_reference_v1`.
- `from=2026-10-05T00:00:00Z`, `to=2026-10-06T00:00:00Z`: **entry_time szerinti**, inclusive UTC szűrés.
- Naive ISO dátum/idő is UTC-ként értendő; explicit offset UTC-re konvertálódik.
- `limit=500` default, maximum 1000; `offset=0` default.
- Ismeretlen, duplikált query paraméter, invalid dátum/pagination HTTP 400. URL token nem támogatott.

Trades válasz: `trades`, `total`, `limit`, `offset`, `next_offset`, `read_only`.
A summary az összes szűrt trade-et aggregálja, nem csak az első lapot.
**Export default az összes szűrt trade**, nem csak 500 sor. Explicit limit/offset
esetén egy lap kerül a fájlba, JSON meta `export_scope=PAGE` jelöléssel.
CSV meta ugyanazon szűrő/pagination paraméterekkel kérhető.

## Export mezők

Pénz/quantity/fee értékek az eredeti exact Decimal adatból **JSON stringként**
kerülnek exportba. A leíró %/statistical/historical OHLC metrikák JSON számok.

- Azonosítás: `position_id`, `symbol`, `status`, `strategy_name`, `signal_id`,
  `exit_signal_id`, `entry_order_id`, `exit_order_id`.
- Idő/méret: `entry_time`, `exit_time`, `duration_seconds`, `entry_ms`, `exit_ms`,
  `quantity`, `notional`, `stop_price`, `take_profit_price`.
- Entry: `strategy_reference_price`, `entry_bid`, `entry_ask`,
  `entry_quote_source`, `entry_quote_received_at`, `entry_fill_price`,
  `entry_slippage`, `entry_fee`, `entry_reason`.
- Exit: `exit_reference_price`, `exit_bid`, `exit_ask`, `exit_quote_source`,
  `exit_fill_price`, `exit_slippage`, `exit_fee`, `exit_reason`,
  `exit_strategy_reasons`, `exit_trigger_price`, `exit_quote_bid`, `exit_quote_ask`,
  `exit_quote_received_at`, `stop_threshold`, `take_profit_threshold`.
- Könyvelés: `realized_pnl`, `unrealized_pnl`, `total_fees`, `return_pct`.
  Return = könyvelt PnL / (entry notional + entry fee) × 100;
  OPEN esetén unrealized, CLOSED esetén realized PnL. A fee nincs másodszor levonva.
- Snapshot: `snapshot_id`, `snapshot_timestamp`, `market_score`,
  `delta_1h`, `delta_4h`, `delta_24h`, `trend`, `ema50`, `ema200`, `rsi`, `atr`,
  `atr_pct`, `relative_volume`, `momentum`, `spread`, `quote_volume`,
  `volatility`, `range_position`, `trend_score`, `momentum_score`, `volume_score`,
  `volatility_score`, `liquidity_score`, `score_version`, `scanner_timeframe`.
- Strategy/Risk: `strategy_action`, `signal_strength`, `strategy_reasons`,
  `risk_approved`, `risk_amount`, `risk_position_notional`, `risk_quantity`,
  `risk_stop_distance_pct`, `risk_reasons`, `portfolio_equity_at_decision`,
  `available_cash_at_decision`, `current_exposure_at_decision`,
  `open_positions_count_at_decision`.
- Jogosultság az eredeti döntéskor: `algorithm_watch`, `manual_trade_enabled`,
  `entry_eligible`, `watch_override`.
- `paper_config_snapshot`: az entry signal public paper konfigurációjának allowlistelt másolata.

A snapshot és signal/risk adatok tárolt értékek; a score/indikátorokat nem számítjuk
újra. Available cash = mentett cash − reserved, exposure a mentett pozíciólista
quantity × current_price összege; ezek read-only megjelenítési projekciók.
Hiányzó snapshot, quote vagy audit mező null marad. Execution auditban nincs
candle-close fallback. Slippage a quote assetben elszámolt költség, nem százalék.
STOP/TAKE_PROFIT trigger bidből származik; STRATEGY konkrét eredeti reasons,
MANUAL az eredeti kézi zárás signalja szerint jelenik meg.

## MFE / MAE és entry timing

Forrás: [Binance public historical klines](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints),
kizárólag `/api/v3/klines` GET, API key/signed kérés nélkül.

CLOSED LONG trade-eknél:

- `mfe_pct = max(0, (highest / entry_fill − 1) × 100)`.
- `mae_pct = min(0, (lowest / entry_fill − 1) × 100)`.
- `mfe_price`, `mae_price`: a tényleges megfigyelt legmagasabb/legkisebb candle ár.
- `time_to_mfe_seconds`, `time_to_mae_seconds`: entrytől a kiválasztott candle
  **open time**-jáig; az első maximum/minimum, ha több azonos van.
- `excursion_timeframe=1m|5m`, `excursion_source=BINANCE_PUBLIC_KLINES`.
- `extrema_time_precision=CANDLE_OPEN_TIME`,
  `boundary_policy=FULLY_CONTAINED_CANDLES_ONLY`.

**Ez candle-alapú, konzervatív közelítés, nem tick- vagy bid-alapú teljes útvonal.**
Csak teljesen entry→exit intervallumba eső, már lezárt gyertyákat használunk.
A belépés percének entry előtti, illetve kilépés percének exit utáni magas/alacsony
árát nem tulajdonítjuk a trade-nek. A határpercek kihagyása miatt az igazi MFE/MAE
nagyobb is lehet. Egy percnél rövidebb trade-nél nem feltétlenül van használható
belső gyertya. Extrém ár időpontja OHLC-ből nem állapítható meg tick-pontossággal.

Minden szükséges belső candle-nek folytonosan rendelkezésre kell állnia.
Hiányzó belső candle esetén az adott teljes ablak metrikái nullok;
nem állítunk részleges adatsorra teljes MFE/MAE értéket.

Timing mind OPEN, mind CLOSED trade-nél (akár exit után is, az entry timinghoz):

- `price_change_15m_after_entry_pct`, `...30m...`, `...60m...`.
- `lowest_price_first_15m`, `highest_price_first_15m`; ugyanez 30m/60m.
- `entry_to_next_30m_low_pct`, `entry_to_next_30m_high_pct`; ugyanez 60m.
- `timing_price_15m_observed_at_utc`; ugyanez 30m/60m, a használt tényleges close időpontja.

A return az adott horizont előtt befejeződő utolsó teljes candle close-ból
származik, az entry fillhez képest. Nem interpolálunk a másodperc pontosságú
horizontra: az eltérés 1m, illetve fallbacknél 5m alatt lehet, és a tényleges
használt időpontot exportáljuk. Az ablak interior candle-jeinek high/low-ja adja
az entry quality nyers metrikáit. Ha a horizont még jövőbeli, nincs elég adat vagy
rés van a candle sorban, az adott ablak null. OPEN trade MFE/MAE-ja null marad.

## Cache, háttérmunka és kiesés

- Workerönként elkülönített bounded **memória-cache**, nem a production SQLite-ban.
- Historical key: symbol + timeframe + time range; TTL **1 óra**, max **128 lap**,
  laponként legfeljebb 1000 candle. Nincs új tick archívum.
- Preferált 1m. Több mint 10 000 perces tartománynál 5m; 1m kiesésekor szintén 5m fallback.
- Max 20 000 candle / trade, max 21 public oldal / kör, 30s oldalsorozat határ,
  public request timeout 5s. Nagyon hosszú történet UNAVAILABLE lehet, nem csendesen levágott MFE.
- Historical worker: **1 thread**, max **32 queued/running job**, legfeljebb
  egy historical oldal/másodperc/worker, a scannerrel közös public API terhelés korlátozására.
- Derived trade cache: max **512 trade/range eredmény**; READY CLOSED TTL 1 óra,
  OPEN/PARTIAL/UNAVAILABLE TTL 60s.
- Aggregation worker: külön **1 thread**, max **4 pending** szűrő; max **16 összesítés**, TTL 30s.
- Az analysis request nem vár a Binance-ra vagy a teljes aggregációra.
- `aggregation_status=PENDING|READY|UNAVAILABLE` külön jelzi az összesítést.
- Trade `analysis_data_status=PENDING|READY|PARTIAL|UNAVAILABLE`.
- Historical kiesésnél az érintett mezők nullok; a DB audit és export működik.
- 418/429 után Retry-After alapú backoff; nincs request retry-vihar.
- Cache process újraindításkor elveszik, és a két Gunicorn worker saját cache-t tart.
  Sok trade esetén több kör szükséges; coverage/sample count mindig látható.

Kis adatmintánál **Insufficient sample size**. Threshold 30 CLOSED trade, explicit
`sample_small=true` a kis bucketekben. Ez operátori figyelmeztetés, nem a statisztikai
szignifikancia bizonyítéka nagyobb mintán sem.

Gross profit = pozitív **könyvelt, fee utáni** PnL-ek összege; gross loss = negatív
könyvelt PnL-ek abszolút összege. Net = ezek különbsége. Fee külön is exportálva,
nem vonjuk le újra. Profit factor nincs veszteség esetén null,
`profit_factor_status=NO_LOSSES|UNDEFINED`; nem exportálunk JSON Infinity-t.
Win rate és PnL/holding/return/MFE átlagok CLOSED trade-eken alapulnak.
MFE/MAE átlag kizárólag rendelkezésre álló, teljes belső candle-adaton;
`mfe_sample_count`, `mae_sample_count` és státusz darabszámok jelzik a coverage-et.

Score bucketek: <50, 50–59, 60–69, 70–79, 80–89, 90–100, szükség esetén UNKNOWN.
A bucket trade_count OPEN-t is tartalmaz, az outcome mutatók CLOSED-on alapulnak.
A current equity / max drawdown **teljes account**, utolsó mentett portfolio
snapshot szerint; nem az entry-date szűrésből újraszámolt drawdown.

## Metadata, export és security

JSON metadata: application_version, git_commit, git_branch, database_schema_version,
score_version és scanner_timeframe (tényleges, szűrt entry snapshotokból vett listák),
generated_at_utc, trade_count, paper_config (public account snapshot), filters,
export_scope, money_encoding és consistency. Trade-enként külön config/snapshot
és score version is rendelkezésre áll, így eltérő konfigurációjú trade-ek követhetők.

Az export kezdetén a szűrt pozíció-ID lista rögzül; új position nem kerül be
félúton. Sorok lapokban streamelődnek, egy lap rövid SELECT olvasás után bezárja
DB kapcsolatát. Nincs hosszú SQLite write transaction vagy minden sort egy JSON
memóriablokkba gyűjtő export. Audit/mark értékek lapok között változhatnak, ha az
engine közben frissít: `CAPTURED_POSITION_IDS_WITH_PER_BATCH_AUDIT` ezt jelzi;
nem állítunk az egész letöltésre egyetlen időpontra fagyasztott portfolio állapotot.

CSV listák/config JSON stringként, UTF-8 BOM-mal, CRLF sorvéggel. Szöveges formula
cellák escapelve, valódi negatív számok változatlanok. JSON money exact string.
Példa (a meglévő dashboard proxy-auth környezetben):

```text
https://crypto.vagilak.hu/paper/analysis
https://crypto.vagilak.hu/api/paper/analysis/export.json?status=CLOSED
https://crypto.vagilak.hu/api/paper/analysis/export.csv?symbol=BTCUSDT
https://crypto.vagilak.hu/api/paper/analysis/meta?symbol=BTCUSDT
```

A production Nginx Proxy Manager auth **változatlan**, az új URL-ek ugyanazon
védett dashboard host alatt vannak. Nincs token URL-ben, külön publikus host,
portnyitás vagy proxy ACL módosítás. A LAN backend korábbi hozzáférési modellje
változatlan; internetes elérés a meglévő hitelesített proxy-n át történik.

Az analysis külön SQLite **mode=ro** poolt használ, nem inicializál/migrál DB-t.
Export és metadata explicit allowlist, public config mezők; nincs secret key,
API key, password, cookie, CSRF, database URL vagy sensitive filesystem path.
Ismeretlen beágyazott numeric/quote adatok nem kerülnek ki. Credential-szerű
szövegek/pathok redactálva. API response nem hoz létre CSRF session cookie-t.

## Új fájlok és tesztek

- `crypto_bot/web/analysis.py`: routes, cache-elt háttérösszesítés, streamed JSON/CSV.
- `crypto_bot/web/analysis_readonly.py`: külön read-only SQLite pool.
- `crypto_bot/web/analysis_data.py`: allowlistelt audit mezőmapping és scrub.
- `crypto_bot/web/analysis_metrics.py`: tiszta historical/statisztikai függvények.
- `crypto_bot/web/analysis_history.py`: TTL/cache, bounded background work, public data.
- `crypto_bot/web/static/paper_analysis.js`: read-only UI frissítés/download/filter linkek.
- `crypto_bot/web/templates/paper_analysis.html`: responsive oldal.
- `tests/test_paper_analysis.py`: determinisztikus unit/integration tesztek, mock HTTP.
- `docs/PAPER_ANALYSIS.md`: ez a dokumentáció.

A meglévő browser tesztmodul két analysis/export tesztet kapott. Desktop 1440 és
mobil 390 px: letöltött JSON tartalom, CSV BOM, Details link, nincs oldal-kilógás
vagy JS error, minden böngészős kérés local GET.

```bash
# Unit/integration; browser extra nélkül a browser modul skip:
.venv/bin/pytest -q
# Browser extra és Chromium rendszerfüggőségek:
.venv/bin/pip install -e '.[test,browser-test]'
.venv/bin/playwright install --with-deps chromium
.venv/bin/pytest -q tests/test_paper_analysis.py tests/test_paper_trade_browser.py
```

A production ellenőrzés nem követeli Binance elérhetőségét: az analysis audit,
summary/export és auth működése ettől független, historical status lehet UNAVAILABLE.
A scanner/paper PIDs és trading ON/OFF állapot deploy előtt/után ellenőrizve;
csak web restart szükséges. A core diff audit kötelező, nincs migration.

## Fejlesztési validáció — 2026-10-06

- Teljes regression csomag: **197 passed** (164 korábbi + 31 új analysis unit/integration + 2 új browser).
- Browser tests összesen **6**, mind determinisztikus, mock market data / izolált DB.
- Ruff, JS `node --check`, whitespace diff check: sikeres.
- Read-only teszt: az analysis SQLite kapcsolaton az UPDATE ténylegesen `readonly`
  hibával elutasítva; az API-k után a business és scanner táblák sora változatlan.
- A core diff üres a scanner / trading / risk / execution / portfolio / storage könyvtárakban.
- Képek: [mobil](screenshots/paper-analysis-mobile.png), [desktop](screenshots/paper-analysis-desktop.png).
