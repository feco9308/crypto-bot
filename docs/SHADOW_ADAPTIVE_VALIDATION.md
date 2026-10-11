# Adaptive Peak Lock v1 — 2024 Shadow validáció

**EXPLORATION. MATCHED-ENTRY EXIT COMPARISON. NOT STATISTICALLY VALIDATED.**

Az új policy kizárólag Shadow Researchben elérhető. Nincs production promotion,
optimizer, paper/strategy/risk/score/scanner/execution módosítás.

## Telepítés és futás

- Fejlesztési branch: `feature/shadow-adaptive-peak-lock`.
- Production branch: `feature/trading-core-paper`.
- Előző production HEAD: `d2234d8`; implementáció: `cfb2554`.
- Anchor: `04c2ef76ceac41199a922ae55480d16b`, variant 0.
- Shadow run: `8807bf4c690c4ebaa30c1a295a029071`, COMPLETED.
- Oldal: `/replay/shadow-exit/8807bf4c690c4ebaa30c1a295a029071`.
- 2024-01-01 → 2025-01-01, változatlan anchor config és 433 BUY.
- Policy-k: baseline_v1, fixed_tp 1.5%, r_tp 0.5R, trailing_pct 1.5/0.5%,
  adaptive_peak_lock_v1 rögzített szabályokkal.
- Conservative OHLC, strict no-lookahead, RESEARCH_QUARANTINE_STALE.
- 5 policy × 433 entry = 2165 outcome; engine idő: 91.614800 másodperc.

## Izoláció és regresszió

Baseline parity: **0 mismatch**. Mind a 430 lezárt anchor trade exit timestampje,
fillje és realizált PnL-je egyezik; az eredeti három open trade továbbra is open.
Az előző négy shadow policy **minden outcome-ja teljesen azonos** az előző
részletes JSON-exportban tárolttal. Nem csak a summary-t hasonlítottuk össze.

Az anchor trades/orders/fills/audit/equity tábláinak darabszáma és SHA256 hash-e
változatlan; a paper state és `.env` hash változatlan. Nincs scanner DB migration,
paper reset, audit-visszaírás vagy normal replay registry változtatás.

Teszt: **443 passed in 92.30s**, ebből 14 új adaptive teszt, a korábbi browser
tesztek kiegészítve az új policy kiválasztásával és activation cohort panellel.
`ruff check crypto_bot tests`: sikeres. Production browser: 1440px / 390px,
5 summary sor, 10 pairwise sor, 3 cohort sor, nincs JS-hiba vagy oldal-kilógás.
Screenshotok: `shadow-result-1440.png`, `shadow-result-390.png`.

Restart: csak web és idle replay worker. Scanner PID 1333 / paper PID 1334
változatlan. Web PID 20407, replay worker PID 20403; mind aktív. A sikeres futás
worker invocation journaljában nincs exception. További restart nem kell.

## Adaptive eredmény — minden teljes adaptive outcome

| Mutató | Érték |
| --- | ---: |
| Canonical entry | 433 |
| Closed | 433 |
| Open | 0 |
| Stale | 0 |
| Incomplete | 0 |
| Win rate % | 38.337182 |
| Mean return % | -0.811200 |
| Median return % | 0.000000 |
| Normalized profit factor | 0.706238 |
| Expectancy R | -0.124901 |
| Median R | 0.000000 |
| Mean MFE % | 4.272171 |
| Mean giveback % | 4.823003 |
| Mean giveback R | 0.822737 |
| Ambiguous intrabar outcome | 37 |

A median return és median R a ledger kerekítési pontosságában break-even (≈0).
A 433 adaptive trade-ből 166 érdemi nyereség, 65 flat, 202 veszteség. A win/loss
besorolás 0.000001 százalékpontos toleranciát használ a kerekítési break-even
eredményeknél; ez megegyezik a pairwise tie toleranciával. Az eredeti pontos
PnL, return és mean értékeket az export megőrzi. A régi policy-k summary
számítása változatlan. PF a normalizált nettó return-ök aránya, flatok nélkül.

A közös ötféle összehasonlítás mintája 430. Az adaptive közös mintán: win rate
38.139535%, mean return -0.841913%, PF 0.697228, expectancy -0.130198R. A fenti teljes adaptive summary 433-as mintáját nem keverjük ezzel.

## Pairwise — A mindig adaptive

| B | Matched | A wins | B wins | Ties | Incomplete | Mean return Δ (százalékpont) | Median return Δ | Mean R Δ |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 430 | 175 | 80 | 175 | 3 | 0.600291 | 0.000000 | -0.014334 |
| fixed_tp | 433 | 158 | 172 | 103 | 0 | -0.440562 | 0.000000 | -0.054410 |
| r_tp | 433 | 167 | 119 | 147 | 0 | -0.210008 | 0.000000 | -0.002908 |
| trailing_pct | 433 | 158 | 171 | 104 | 0 | -0.194873 | 0.000000 | -0.010128 |

## BTC / ATR bontás — teljes adaptive minták

| Csoport | N | Win % | Mean return % | Median return % | PF | Expectancy R | Median R | MFE % | Giveback % | Giveback R | Sample |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| BTC BEARISH | 267 | 32.209738 | -1.276936 | -2.242380 | 0.605418 | -0.230190 | -1.000000 | 4.255373 | 5.271983 | 0.866387 | LARGER SAMPLE |
| BTC BULLISH | 166 | 48.192771 | -0.062094 | 0.000000 | 0.968919 | 0.044450 | 0.000000 | 4.299189 | 4.100847 | 0.752529 | LARGER SAMPLE |
| ATR 0-1% | 32 | 53.125000 | -0.043356 | 0.503433 | 0.933771 | 0.016395 | 0.534147 | 1.264238 | 1.054737 | 0.617671 | SMALL SAMPLE |
| ATR 1-2% | 95 | 32.631579 | -0.804455 | -2.469253 | 0.551820 | -0.230100 | -1.000000 | 2.473161 | 3.021740 | 0.856892 | SMALL SAMPLE |
| ATR 2-3% | 171 | 40.935673 | -0.321716 | 0.000000 | 0.855303 | -0.062052 | 0.000000 | 4.103629 | 4.165398 | 0.802899 | LARGER SAMPLE |
| ATR 3-5% | 104 | 34.615385 | -1.103584 | 0.000000 | 0.693568 | -0.142434 | 0.000000 | 5.766020 | 6.605505 | 0.861274 | LARGER SAMPLE |
| ATR >5% | 31 | 38.709677 | -3.343638 | -10.803088 | 0.584526 | -0.236230 | -1.000000 | 8.808304 | 11.880247 | 0.909896 | SMALL SAMPLE |

## Adaptive saját MFE / activation cohortjai

| Küszöb | Elérte | Nyereség | Flat | Veszteség | Incomplete | Profitból visszaadott | Mean giveback % | Mean giveback R |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.75R | 231 | 166 | 65 | 0 | 0 | 231 | 2.627184 | 0.474369 |
| 1.0R | 166 | 166 | 0 | 0 | 0 | 166 | 1.951803 | 0.345535 |
| 2.0R | 3 | 3 | 0 | 0 | 0 | 3 | 1.443262 | 0.238097 |

A cohortok egymásba ágyazottak; nem összeadandók. A csúcs csak az adott adaptive
pozíció kilépéséig megfigyelt árútból számítódik, későbbi high nem kerül bele.
Mind a 231 aktivált pozíció adott vissza valamennyit a nettó profitcsúcsából;
166 nyereséget tartott meg, 65 break-even lett, 0 veszteség ezen a mintán.

## Baseline profitból veszteségbe fordult trade-ek megmentése

Ez **külön baseline cohort**: a baseline saját MFE-je elérte a küszöböt, de
teljes, lezárt trade-ként nettó veszteség lett. Ugyanazon canonical entry
adaptive eredményével hasonlítjuk össze. Nem az adaptive fenti activation
darabszámainak részhalmaza; az adaptive korábban is lezárhatott.

| Baseline MFE | Később baseline veszteség | Adaptive nyereségre mentette | Adaptive nyereség vagy flat | Ebből flat |
| --- | ---: | ---: | ---: | ---: |
| 0.75R | 148 | 98 | 148 | 50 |
| 1.0R | 118 | 98 | 118 | 20 |
| 2.0R | 38 | 28 | 38 | 10 |

## Szabályok és korlátok

R = bruttó árnövekedés × canonical quantity / canonical risk. A break-even stop
a tényleges canonical notional + entry fee és a várható exit fee/slippage
könyvelését fedezi, az entry slippage-et nem számolja kétszer. ATR = entry ATR.
0.75R után BE, 1R után high−0.75ATR, 2R után high−0.50ATR; stop nem csökken.

Giveback = nettó, fee/slippage után likvidálható megfigyelt peak PnL mínusz
nettó realized PnL. Százalékhoz canonical entry cost, R-hez canonical risk a
nevező. MFE/MAE bruttó árút-excursió. Aktiválási idő a lezárt gyertya UTC
megfigyelési ideje, nem kitalált intraminute időpont.

Az adaptive kilépési policy stop-only; a ReferenceStrategy változatlan, a
korábbi policy-k továbbra is megkapják a meglévő strategy exit jelzéseket.
Stop-gap esetén az open fill veszteséges is lehet — erre külön regressziós
teszt van. A 2024-es aktivált cohort 0 vesztesége nem garancia. Nincs future
archive coverage alapján zárás, year-end force-close vagy stale fill. A
baseline FRONT továbbra is OPEN_STALE; az adaptive ugyanazt az entryt korábban
lezárta, ezért annak eredménye nem stale.

Ez anchor-függő matched-entry kutatás; nem önálló portfolio cash/equity backtest.
A mean return és expectancy továbbra is negatív. A pairwise különbségek nem
alapoznak meg optimalizálást vagy production promotiont. A kisebb ATR minták
SMALL SAMPLE; egyik eredmény sem statisztikailag validált.

## Módosított fájlok

Új: `crypto_bot/replay/shadow_adaptive.py`, `tests/test_shadow_adaptive.py`,
`docs/SHADOW_ADAPTIVE_PEAK_LOCK.md`, ez a validációs riport.

Módosított: `shadow_config.py`, `shadow_engine.py`, `shadow_metrics.py`,
`crypto_bot/web/shadow.py`, `web/static/shadow_result.js`,
`web/templates/shadow_result.html`, `tests/test_shadow_browser.py`,
`docs/SHADOW_EXIT_RESEARCH.md`. Az utóbbiak Shadow integráció/audit/UI változások.

Részletes architektúra: [SHADOW_ADAPTIVE_PEAK_LOCK.md](SHADOW_ADAPTIVE_PEAK_LOCK.md).

## Exportok

Pontos könyvtár: `/home/ndvi/crypto-bot/artifacts/shadow-adaptive-2024/`.

- `2024-shadow-adaptive.compact.json` — summaryk/cohortok/párok/bontások/meta.
- `2024-shadow-adaptive.detailed.json` — canonical entryk, outcome-ok, audit.
- `validation.json`, `stale-outcomes.json`, `config.json`, `run.json`,
  `validated-config.json`, `before.json`, a két screenshot.

Export SHA256:

- `2024-shadow-adaptive.compact.json`: `d6e3c65cfe9f9831dca6fc8ddcfe13e977e651163157343c670a324a1225595b`.
- `2024-shadow-adaptive.detailed.json`: `7d61748f4c063cc9be752308359ba0744382289e6326da6f9eb3f04f73a66917`.

A futás az implementációs `cfb2554` commitot rögzíti. A későbbi dokumentációs
commit nem változtatja meg a kódot vagy a futás eredményét.
