# Market Scanner — adatmodell és számítások

[A fő README](../README.md) tartalmazza a telepítést és üzemeltetést.
A scanner számításai a paper core hozzáadásával nem változtak.

## Scanner alapbeállítások

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

