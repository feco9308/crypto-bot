# Entry Market Context Research — 2024 EXPLORATION

Read-only elemzés az egyetlen meglévő B anchoron:
`b9275979397440f2aabbe4a89e4f9cc4`, variant 0.
1506 CLOSED trade: 847 ACTIVATED és 659 ORIGINAL_STOP. Nem új replay,
nem új entry filter, nem matched-entry Shadow futtatás.

## Forrás és időbeli megfigyelhetőség

A `research/entry_market_context.py` az alkalmazás telepített csomagján kívül
található. Nem hív ReplayEngine-t, stratégiát, RiskManagert, PortfolioService-t,
executiont vagy workert. Nem hoz létre run rekordot. Közvetlen SQLite
`mode=ro` kapcsolatokkal olvassa a B run adatait és a cache manifestet;
kizárólag az artifact könyvtárba ír JSON-t. HTTP kliens nincs példányosítva.
A HistoricalData osztályból csak a meglévő tiszta ZIP CSV-parser fut.

A B metadata SHA256-pinnelt **eredeti ZIP fájljaiból** olvassa a lezárt 1h
gyertyákat, nem a később esetleg felülírt, összevont candle-cache táblából.
A 2023. decemberi warmup is az anchor része. Hiányzó, sérült vagy módosult
forrás esetén megáll; nincs API vagy candle-close/execution fallback.
Az archive teljes fájljának integritáshash-e nem trading input: a feature
számítás kizárólag az adott döntés előtt lezárult gyertyákat látja.

BTC: ugyanaz a `technical.features`, a replay azonos gördülő **499** lezárt
gyertyás ablakával. EMA-slope: az akkori EMA / a 6 vagy 24 órával korábbi,
akkori saját gördülő ablakból számolt EMA mínusz egy, százalékban.
Return: az utolsó lezárt close / az 1, 4, 24 vagy 168 órával korábbi close
mínusz egy. Az összes source timestamp UTC és szigorúan kisebb a döntésnél.

Breadth: az eredeti HistoricalUniverse rolling-24h-volume Top 50 rangsora,
kiegészítve a B-ben **már megnyitott, még nem lezárt** pozíciók friss
instrumentumaival, ahogy az anchor scanner működött. A pozíciók as-of
állapota kizárólag a már megtörtént tárolt fill eseményekből származik.
Nem a mai Binance universe, nem az összes 693 jelölt, nem csak a watchlist.
Momentum az eredeti `score_momentum`, ugyanazzal a 26 órás history-retentionnel,
20 perces toleranciával és −24h scanner-warmuppal. Market Score nem változik.

Minden breadth mutató saját, friss és nem hiányzó bemenetű nevezőt használ.
A hiányzó delta nem nulla és nem negatív: kimarad az adott mutató nevezőjéből.
Exportálva van a teljes fresh-symbol lista, Top50/held lista, a mutatónkénti
nevező és symbol-lista, valamint a gyertyák forrásideje. ALT = nem BTC, nem ETH,
az eredeti asset_group definíció szerint.

A coin-local belépési adatok mind a B eredeti auditjából maradnak meg.
A rekonstrukció minden belépésnél ellenőrzi a kilenc coin-local változó,
valamint a BTC bullish regime és forrásidő egyezését.

## Statisztika, előre rögzített bucketek

ACTIVATED: `activation_075r_time != null`; ORIGINAL_STOP: `exit_reason ==
ORIGINAL_STOP`. Profitable **szigorúan `realized_r > 0`**, tolerancia nélkül.
A break-even Decimal kerekítési maradékai ezért pozitívnak számíthatnak;
a compact külön jelzi az `abs(R) <= 1e-8` darabszámot.

Külön activation és profitability összehasonlítás: N, missing, mean, median,
lineáris interpolációjú Q25/Q75, pozitív mínusz negatív különbség, pooled
sample-variance Cohen d, növekvő feature-irányú ROC AUC (tie = 0.5).
Az AUC < 0.5 fordított irányú kapcsolat. A ranking `abs(AUC − 0.5)` szerinti
leíró sorrend, nem out-of-sample teljesítmény és nem korrigált sokszoros teszt.

Bucket-határok a forrásban, az elemzés előtt rögzítve; `[lower, upper)`:

| Feature típus | Belső határok |
|---|---|
| BTC return % | −5, −2, 0, 2, 5 |
| BTC EMA slope % | −1, 0, 0.5, 1, 2 |
| BTC EMA-spread / price-to-EMA % | −2, 0, 2, 5, 10 |
| BTC vagy breadth RSI | 40, 50, 60, 70 |
| BTC ATR % | 0.25, 0.5, 1, 2 |
| Breadth arány % | 20, 40, 60, 80 |
| Breadth median score | 40, 50, 60, 70, 80 |
| Breadth median delta | −10, −5, 0, 5, 10 |
| Breadth median ATR % | 1, 2, 3, 5 |
| Breadth symbol count | 40, 50, 51, 54 |

Mindkét szélen nyitott tartomány. Nincs boundary search. A megjelenítéshez a
legerősebb négy származtatott BTC-strength és négy breadth változó szerepel;
az abszolút BTC price/EMA szintek is benne vannak a teljes összehasonlításban
és a modellekben, de árszint-bucket nem lenne stabil 2024 egészére.

Időbeli robustness: **entry decision** szerinti UTC Q1–Q4 és mind a 12 hónap.
Havi minimum 30 trade; a B korábbi portfolio havi PnL riportja ezzel szemben
exit hónap szerint csoportosít. Ezért a havi darabszámok eltérhetnek.

## Prediktív diagnosztika

Feature set: kilenc coin-local; 15 BTC + 16 breadth; együtt 40 változó.
Nincs outcome, exit, activation, risk sizing vagy jövőbeli feature a modellekben.

Három expanding-window teszt: Q1 → Q2; Q1–Q2 → Q3; Q1–Q3 → Q4.
A tréningből kimarad minden trade, amelynek exit ideje nem korábbi a teszt
kezdeténél: a tanítási címkéje akkor még nem biztosan megfigyelhető.
Azonos döntési timestamphez tartozó trade-ek azonos oldalra kerülnek.
Median imputation és standard scaling kizárólag a tréninghalmazon fitelődik.

Rögzített modellek: LogisticRegression C=1, lbfgs, max_iter=2000;
DecisionTreeClassifier max_depth=3, min_samples_leaf=30, random_state=0.
Nincs grid search, nincs küszöbválasztás, nincs egészéves ranking alapján
model-feature selection, nincs BUY jel vagy mentett trading modell.
Az AUC-k számtani foldátlaga szerepel, a három fold külön is megmarad.

Ez összhangban van a scikit-learn dokumentált
[időrendi validációs és train-only preprocessing elveivel](https://scikit-learn.org/stable/modules/cross_validation.html#time-series-split),
az átfedő trade-labelök purgingja külön alkalmazásspecifikus védelem.

## Reprodukció és export

A kutatási dependency-k külön venvben vannak, nem a production `.venv`-ben:

```bash
python3 -m venv /home/ndvi/.cache/crypto-bot-context-venv
/home/ndvi/.cache/crypto-bot-context-venv/bin/pip install -r research/requirements.txt
PYTHONPATH=/home/ndvi/crypto-bot-shadow OPENBLAS_NUM_THREADS=1 \
  /home/ndvi/.cache/crypto-bot-context-venv/bin/python -m research.entry_market_context \
  --root /home/ndvi/crypto-bot
```

Már exportált kontextusokból csak a statisztika újraszámolásához:
`--analyze-existing-detail`. Ez sem hoz létre replayt.

Kért artifactok:

- `/home/ndvi/crypto-bot/artifacts/entry-market-context-2024/entry-market-context-2024.detailed.json`
- `/home/ndvi/crypto-bot/artifacts/entry-market-context-2024/entry-market-context-2024.compact.json`

A detailed megőrzi az összes eredeti trade-adatot, az összes feature-t,
source-audit hivatkozását és a pinned archive revisionöket.
A compact tartalmazza a módszertant, proofot, táblákat, rankinget, bucketeket,
havi/negyedéves robustness-t, időrendi AUC-kat és missing-summaryt.

Production kód, config, run, auditrekord és service változtatás nincs.
Új scanner/paper/web/replay-worker restart nem szükséges.
NO LIVE TRADING IMPLEMENTED. Kizárólag 2024 EXPLORATION.

## Elkészült eredmények

Mind az **1506** eredeti trade megmaradt: **847 ACTIVATED**, **659 ORIGINAL_STOP**.
A strict `R > 0` profitable csoport szintén pontosan a 847 ACTIVATED trade;
203 trade-nél `abs(R) <= 1e-8`, tehát break-even kerekítési maradék szerepel.
Az activation/profitability összehasonlítások ezért ezen az anchoron azonosak.

40 feature elkészült: 9 eredeti coin-local, 15 BTC, 16 breadth.
**Minden új BTC/breadth feature teljes, hiányzó érték nélkül.** A meglévő
coin-local delta_1h 6, delta_24h 131 trade-nél hiányzik; nincs nullával pótlás
a leíró statisztikában. A prediktív modell kizárólag train-only median imputationt használ.

No-lookahead / integritás:

- **13 554** coin-local parity ellenőrzés, **0 mismatch**.
- BTC regime/source timestamp: **0 mismatch**.
- **4874** SHA256-ellenőrzött, elérhető 1h archive objektum.
- Minden exportált candle-source timestamp szigorúan korábbi a döntésnél.
- B run összes releváns DB-táblája előtte/utána azonos sor- és tartalomhash.
- Későbbi candle, snapshot és Q4 modell-feature változtatási regressziók sikeresek.

### Egészéves és negyedéves kapcsolatok

Az új market-context változók közül a legerősebb éves különbségek is kicsik:

| Feature | ACTIVATED mean | ORIGINAL_STOP mean | AUC | Cohen d |
|---|---:|---:|---:|---:|
| ALT bullish EMA breadth % | 76.084 | 73.966 | 0.5232 | 0.0980 |
| Összes bullish EMA breadth % | 76.582 | 74.539 | 0.5230 | 0.0961 |
| BTC price vs EMA200 % | 3.144 | 2.974 | 0.5208 | 0.0561 |
| BTC EMA200 slope 24h % | 0.797 | 0.755 | 0.5183 | 0.0623 |
| BTC EMA spread % | 2.342 | 2.216 | 0.5164 | 0.0673 |

Az AUC iránya a növekvő feature értéké, nem utólag átfordított „best AUC”.

| Feature AUC | Q1 | Q2 | Q3 | Q4 |
|---|---:|---:|---:|---:|
| ALT bullish breadth | 0.5050 | 0.5027 | 0.5075 | 0.5340 |
| BTC price vs EMA200 | 0.5053 | 0.5595 | 0.5324 | 0.4914 |
| BTC EMA200 slope 24h | 0.5204 | 0.5363 | 0.5081 | 0.4923 |
| Breadth median RSI | 0.4229 | 0.5497 | 0.5109 | 0.4607 |

ALT breadth ugyanabba az irányba mutat, de Q1–Q3-ban gyakorlatilag chance.
Több BTC/RSI kapcsolat irányt vált; nincs erős, negyedéveken át stabil szeparáció.
A teljes feature-táblák és a rögzített bucketek a compact exportban szerepelnek.

Entry hónap szerinti kiemelt időszakok:

| Hónap | N | Activation % | Avg R | R PF | Mean ALT bullish % | Mean BTC EMA spread % |
|---|---:|---:|---:|---:|---:|---:|
| Április | 73 | 39.73 | −0.3575 | 0.4069 | 60.25 | 1.104 |
| Szeptember | 134 | 63.43 | +0.0656 | 1.1795 | 83.03 | 1.901 |
| November | 189 | 65.08 | +0.0625 | 1.1790 | 83.73 | 4.010 |

Az eltérő havi market context látható, de a havi aggregált kapcsolat nem
bizonyít stabil, egyedi belépési döntéshez használható hatást.

### Chronological predictive diagnostic

| Feature set / modell | Q2 AUC | Q3 AUC | Q4 AUC | Mean AUC |
|---|---:|---:|---:|---:|
| Coin-local / logistic | 0.4603 | 0.4805 | 0.5559 | **0.4989** |
| Market-context / logistic | 0.4624 | 0.4307 | 0.5189 | **0.4707** |
| Combined / logistic | 0.4570 | 0.4403 | 0.5358 | **0.4777** |
| Coin-local / shallow tree | 0.5033 | 0.5000 | 0.5219 | **0.5084** |
| Market-context / shallow tree | 0.4909 | 0.4880 | 0.4961 | **0.4917** |
| Combined / shallow tree | 0.4909 | 0.4880 | 0.4961 | **0.4917** |

A két tree market/combined eredmény azonos: a fix, konzervatív fa ebben a
diagnosztikában nem választott eltérő, hasznos coin-local splitet.
Az AUC < 0.5 értékeket nem fordítottuk át, és nem választottunk jobb modellt
az éves eredmények alapján. Nincs stabil időrendi prediktív javulás.

### Validáció és változtatási határ

- Teljes maintained suite: **502 passed**, 92.37 s; ebből 18 új invariáns teszt.
- Külön izolált modelltesztek: **2 passed**; összesen 20 új teszteset.
- `ruff check crypto_bot tests research`: sikeres.
- A globális `ruff check .` négy meglévő legacy import-sorrend hibát jelez,
  ugyanígy a változatlan production checkoutban; ezeket nem módosítottuk.
- A böngészős teljes suite-hoz a meglévő helyi Chromium shared-library
  könyvtárútvonal szükséges; első próbálkozás után ezzel minden teszt átment.
- Modellek külön research venvben: NumPy 2.2.6, SciPy 1.15.3,
  scikit-learn 1.7.2. Az újabb NumPy wheel nem támogatta a szerver CPU-ját;
  a kompatibilis verziók rögzítve vannak, production dependency nem változott.
- Production HEAD változatlan: `5ea5d09`; research kód commit: `ed08a72`.
- Scanner PID 1333, paper PID 1334, web PID 31948, replay-worker PID 35145:
  változatlan, active. Egyik szolgáltatást sem restartoltuk.

Következtetés: ezen az egyetlen 2024 EXPLORATION anchoron a piac állapota
havi szinten eltérő, de a kért változók nem mutattak stabil out-of-time
előrejelző információt az ACTIVATED vs ORIGINAL_STOP célra. Új entry filter,
threshold, optimizer vagy production promotion nem készült.
