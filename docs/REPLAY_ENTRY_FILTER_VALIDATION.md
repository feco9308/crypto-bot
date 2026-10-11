# Score / delta4h entry filter — 2024 EXPLORATION

**NOT STATISTICALLY VALIDATED. NO PRODUCTION PROMOTION.**

Branch: `feature/replay-entry-score-delta-filter`. Implementáció: `e6cbefe`.
Teljes maintained suite: 484 sikeres teszt, ebből 25 új regresszió. Ruff tiszta.
Desktop/mobile négyszereplős config import/export roundtrip pontos; nem indított implicit replayt.

Négy független normál ReplayEngine futás, saját history/cash/positions/exposure/sizing/risk/execution. Nem utólagos trade subset vagy Shadow filtering.
A korábbi BTC-gate D (`e79eb00e28eb408f8d0d38df3fb17540`) összes effektív entry/risk/exit paraméterét és archivumrevízióit használtuk.
2024-01-01 → 2025-01-01, initial 1000 USDT, BULLISH_ONLY + adaptive_peak_lock_v1, historical Top 50 a teljes fagyasztott 693-symbol universe-ből,
daily-loss/drawdown guard 100%, RESEARCH_QUARANTINE_STALE, conservative intrabar, strict no-lookahead, fallback_5m=false.
A unrestricted; B score [80,90); C delta4h [5,10); D mindkettő (AND). Nincs keresés vagy utólagos paraméterhangolás.

Filter csak a változatlan strategy BUY és a meglévő BTC gate után fut, RiskManager/execution előtt. SELL/exit nem blokkolható.
Csak az aktuális scanner sor total_score és momentum.delta_4h értékét olvassa; nincs új score/momentum/regime definíció vagy future feed.
Minden gated entry BTC forrása strictly past, legfeljebb 1h régi; minden entry és minden filter audit megfelel a határoknak.

## Control parity

A sikeres, **exact** parity után indult B/C/D. Trade count, final equity, realized PnL, max drawdown, fee, slippage, eredeti blocked reasons/categories,
win rate/PF/R/exposure is pontosan azonos. **Az összes trade rekord és a teljes equity curve is egyezik** a korábbi D-vel.
Proof: `artifacts/replay-entry-filter-2024/control-parity.json`. Új, null-default filter config és zero-count reporting mezők nem jelentenek trading változást.

## Run ID-k

| Variant | Run ID |
| --- | --- |
| A | `1538f0360f214d70bd4b1930f47c657c` |
| B | `b9275979397440f2aabbe4a89e4f9cc4` |
| C | `f234a04ddcc441bf8c10fd3eb9ed0b4f` |
| D | `0606fca360884d9a903eb9b5052ebb74` |

## Annual portfolio metrics

| Mutató | A CONTROL | B SCORE | C DELTA4H | D COMBINED |
| --- | ---: | ---: | ---: | ---: |
| Initial USDT | 1000.0000 | 1000.0000 | 1000.0000 | 1000.0000 |
| Final equity USDT | 473.5512 | 560.6003 | 656.8222 | 716.6033 |
| Return % | -52.6449 | -43.9400 | -34.3178 | -28.3397 |
| Realized PnL USDT | -526.4488 | -439.3997 | -343.1778 | -283.3967 |
| Closed trades | 1607.0000 | 1506.0000 | 991.0000 | 626.0000 |
| Open trades | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| Stale positions | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| Win rate % | 41.2570 | 42.0983 | 41.8769 | 41.2141 |
| PF | 0.7990 | 0.8236 | 0.7993 | 0.7591 |
| Expectancy USDT/trade | -0.3276 | -0.2918 | -0.3463 | -0.4527 |
| Avg R | -0.0917 | -0.0745 | -0.0847 | -0.1054 |
| Median R | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| Max DD % | 53.9989 | 45.0662 | 36.5792 | 30.8081 |
| Avg hold seconds | 32265.6254 | 30451.1952 | 33259.9798 | 32876.7412 |
| Fees USDT | 261.9633 | 261.0378 | 175.0259 | 111.9125 |
| Slippage USDT | 130.9818 | 130.5191 | 87.5130 | 55.9563 |
| Average exposure % | 17.6790 | 16.1618 | 11.7673 | 6.9757 |
| Max exposure % | 50.5600 | 50.7182 | 50.4118 | 50.3358 |

## Exit diagnostics

| Mutató | A CONTROL | B SCORE | C DELTA4H | D COMBINED |
| --- | ---: | ---: | ---: | ---: |
| ORIGINAL_STOP | 718.0000 | 659.0000 | 447.0000 | 288.0000 |
| ADAPTIVE_PEAK_LOCK | 889.0000 | 847.0000 | 544.0000 | 338.0000 |
| Adaptive activated count | 889.0000 | 847.0000 | 544.0000 | 338.0000 |
| Adaptive activation rate % | 55.3205 | 56.2417 | 54.8940 | 53.9936 |
| ORIGINAL_STOP rate % | 44.6795 | 43.7583 | 45.1060 | 46.0064 |
| Avg R ADAPTIVE_PEAK_LOCK | 0.6420 | 0.6456 | 0.6673 | 0.6568 |
| Avg R ORIGINAL_STOP | -1.0000 | -1.0000 | -1.0000 | -1.0000 |

Activation rate: activation_075r_time jelen van / closed trades. Open/stale nem számít bele a closed-rate nevezőbe. Az exit count ettől külön mérőszám.

## BUY block diagnostics

| Mutató | A CONTROL | B SCORE | C DELTA4H | D COMBINED |
| --- | ---: | ---: | ---: | ---: |
| BTC_REGIME_ENTRY_BLOCK | 3406.0000 | 3418.0000 | 3430.0000 | 3441.0000 |
| RESEARCH_ENTRY_FILTER_BLOCK | 0.0000 | 4164.0000 | 8082.0000 | 9087.0000 |
| max_positions | 7127.0000 | 3245.0000 | 398.0000 | 88.0000 |
| Legacy other (incl BTC/filter) | 3407.0000 | 7582.0000 | 11512.0000 | 12528.0000 |
| Other residual | 1.0000 | 0.0000 | 0.0000 | 0.0000 |
| SCORE_BELOW_MIN | 0.0000 | 2412.0000 | 0.0000 | 2604.0000 |
| SCORE_ABOVE_MAX | 0.0000 | 1752.0000 | 0.0000 | 1878.0000 |
| DELTA4H_BELOW_MIN | 0.0000 | 0.0000 | 864.0000 | 916.0000 |
| DELTA4H_ABOVE_MAX | 0.0000 | 0.0000 | 7218.0000 | 7440.0000 |
| SCORE_UNAVAILABLE | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| DELTA4H_UNAVAILABLE | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

Egy opportunity több határt sérthet, így a részokok összege meghaladhatja a filter-block countot. BTC gate előbb fut; annak blokkjait nem számítjuk filter-blocknak.
Other residual: minden részletes nem-BTC/nem-filter/nem-max_positions blokk. A backward-compatible legacy blocked_entries.other a gate/filter okokat is tartalmazza.

- A actual reasons: `{"BTC_REGIME_ENTRY_BLOCK": 3406, "MISSING_1M_EXECUTION_CANDLE; fallback disabled": 1, "max open positions reached": 7127}`.
- B actual reasons: `{"BTC_REGIME_ENTRY_BLOCK": 3418, "RESEARCH_ENTRY_FILTER_BLOCK": 4164, "max open positions reached": 3245}`.
- C actual reasons: `{"BTC_REGIME_ENTRY_BLOCK": 3430, "RESEARCH_ENTRY_FILTER_BLOCK": 8082, "max open positions reached": 398}`.
- D actual reasons: `{"BTC_REGIME_ENTRY_BLOCK": 3441, "RESEARCH_ENTRY_FILTER_BLOCK": 9087, "max open positions reached": 88}`.

## Controlhoz képest (B−A, C−A, D−A)

| Mutató | B−A | C−A | D−A |
| --- | ---: | ---: | ---: |
| Final equity USDT | 87.0492 | 183.2710 | 243.0521 |
| Return pp | 8.7049 | 18.3271 | 24.3052 |
| PF | 0.0246 | 0.0003 | -0.0398 |
| Avg R | 0.0171 | 0.0069 | -0.0137 |
| Max DD pp | -8.9327 | -17.4197 | -23.1908 |
| Trade count | -101.0000 | -616.0000 | -981.0000 |
| Fee+slippage USDT | -1.3881 | -130.4061 | -225.0763 |
| Activation-rate pp | 0.9212 | -0.4264 | -1.3269 |

D COMBINED az előre rögzített közös hipotézis; nem nevezzük automatikusan best strategy-nak. Nézd együtt a trade countot, havi eredményeket, drawdownt és költséget, ne csak az éves equityt.

## Cost diagnostics

| Mutató | A CONTROL | B SCORE | C DELTA4H | D COMBINED |
| --- | ---: | ---: | ---: | ---: |
| Fees USDT | 261.9633 | 261.0378 | 175.0259 | 111.9125 |
| Slippage USDT | 130.9818 | 130.5191 | 87.5130 | 55.9563 |
| Total transaction cost USDT | 392.9450 | 391.5569 | 262.5390 | 167.8687 |
| Cost / initial balance % | 39.2945 | 39.1557 | 26.2539 | 16.7869 |
| Gross realized profit estimate USDT | 2316.2089 | 2273.6955 | 1514.0567 | 984.7521 |
| Cost / gross profit estimate % | 16.9650 | 17.2212 | 17.3401 | 17.0468 |

Gross estimate = sum(max(realized_pnl + trade fees + trade slippage, 0)) a lezárt trade-eken. Approximate pre-cost positive profits; nem mesterséges no-fee portfolio rerun.
A slippage már a fill árban van, a nettó PnL-ből nem vonjuk le újra. Nyitott trade-ek díjai miatt a teljes cost és a closed-trade gross estimate nevezője eltérhet; open count külön látszik.

## 2024-04 külön ellenőrzés

| Mutató | A CONTROL | B SCORE | C DELTA4H | D COMBINED |
| --- | ---: | ---: | ---: | ---: |
| Trades | 84.0000 | 76.0000 | 50.0000 | 32.0000 |
| Realized net PnL USDT | -149.5441 | -116.1437 | -78.4326 | -47.7301 |
| Equity change USDT | -149.6167 | -111.4937 | -78.4326 | -47.7301 |
| PF | 0.3209 | 0.4102 | 0.3158 | 0.3551 |
| Expectancy USDT | -1.7803 | -1.5282 | -1.5687 | -1.4916 |
| Avg R | -0.4277 | -0.3572 | -0.4101 | -0.3617 |
| Monthly DD % | 16.6362 | 12.2930 | 10.1492 | 6.8957 |

## A havi robusztusság

| Exit month | Trades | Realized PnL USDT | PF | Expectancy USDT | Avg R | Monthly DD % | Equity change USDT |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2024-01 | 119.0000 | -39.9864 | 0.8440 | -0.3360 | -0.0693 | 5.2444 | -39.9864 |
| 2024-02 | 208.0000 | -24.5301 | 0.9374 | -0.1179 | -0.0259 | 8.8766 | -24.5301 |
| 2024-03 | 158.0000 | -9.3348 | 0.9702 | -0.0591 | -0.0102 | 6.4251 | -9.2622 |
| 2024-04 | 84.0000 | -149.5441 | 0.3209 | -1.7803 | -0.4277 | 16.6362 | -149.6167 |
| 2024-05 | 165.0000 | -36.1015 | 0.8647 | -0.2188 | -0.0553 | 6.8491 | -37.2346 |
| 2024-06 | 52.0000 | -42.5983 | 0.5666 | -0.8192 | -0.2157 | 8.1768 | -41.4652 |
| 2024-07 | 127.0000 | -49.3640 | 0.7538 | -0.3887 | -0.1120 | 9.8701 | -49.3640 |
| 2024-08 | 77.0000 | -31.2737 | 0.7175 | -0.4062 | -0.1165 | 6.9335 | -31.2737 |
| 2024-09 | 139.0000 | 23.4980 | 1.1615 | 0.1691 | 0.0529 | 3.1369 | 22.0360 |
| 2024-10 | 148.0000 | -105.0050 | 0.5414 | -0.7095 | -0.2449 | 17.2778 | -103.5430 |
| 2024-11 | 209.0000 | 18.3143 | 1.0870 | 0.0876 | 0.0358 | 9.3271 | 14.7938 |
| 2024-12 | 121.0000 | -80.5234 | 0.5423 | -0.6655 | -0.2578 | 17.3795 | -77.0029 |

## B havi robusztusság

| Exit month | Trades | Realized PnL USDT | PF | Expectancy USDT | Avg R | Monthly DD % | Equity change USDT |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2024-01 | 113.0000 | -89.7690 | 0.6729 | -0.7944 | -0.1637 | 10.5763 | -89.7690 |
| 2024-02 | 208.0000 | -4.9706 | 0.9862 | -0.0239 | -0.0106 | 5.8080 | -7.2252 |
| 2024-03 | 157.0000 | 17.9598 | 1.0643 | 0.1144 | 0.0265 | 6.0688 | 15.5645 |
| 2024-04 | 76.0000 | -116.1437 | 0.4102 | -1.5282 | -0.3572 | 12.2930 | -111.4937 |
| 2024-05 | 144.0000 | -50.4250 | 0.7958 | -0.3502 | -0.0900 | 9.0757 | -51.2507 |
| 2024-06 | 43.0000 | -17.5657 | 0.7639 | -0.4085 | -0.1018 | 5.0360 | -16.7400 |
| 2024-07 | 127.0000 | -42.3805 | 0.7895 | -0.3337 | -0.0895 | 10.9872 | -42.3805 |
| 2024-08 | 76.0000 | -70.4072 | 0.4692 | -0.9264 | -0.2781 | 10.4929 | -70.4072 |
| 2024-09 | 132.0000 | 29.8328 | 1.1981 | 0.2260 | 0.0818 | 3.0901 | 25.8025 |
| 2024-10 | 133.0000 | -63.0501 | 0.6832 | -0.4741 | -0.1468 | 10.5447 | -59.0198 |
| 2024-11 | 186.0000 | 37.8245 | 1.1950 | 0.2034 | 0.0701 | 4.4990 | 36.2561 |
| 2024-12 | 111.0000 | -70.3050 | 0.6089 | -0.6334 | -0.2074 | 11.8859 | -68.7366 |

## C havi robusztusság

| Exit month | Trades | Realized PnL USDT | PF | Expectancy USDT | Avg R | Monthly DD % | Equity change USDT |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2024-01 | 74.0000 | -54.8729 | 0.6858 | -0.7415 | -0.1619 | 6.0354 | -54.8729 |
| 2024-02 | 133.0000 | -75.1275 | 0.7344 | -0.5649 | -0.1237 | 9.8531 | -75.1275 |
| 2024-03 | 104.0000 | -38.6618 | 0.8112 | -0.3717 | -0.0798 | 8.7044 | -38.6618 |
| 2024-04 | 50.0000 | -78.4326 | 0.3158 | -1.5687 | -0.4101 | 10.1492 | -78.4326 |
| 2024-05 | 92.0000 | -3.8263 | 0.9720 | -0.0416 | -0.0113 | 4.3148 | -4.9637 |
| 2024-06 | 27.0000 | -10.2893 | 0.7995 | -0.3811 | -0.1104 | 3.7550 | -9.1519 |
| 2024-07 | 78.0000 | -10.4804 | 0.9181 | -0.1344 | -0.0293 | 8.5252 | -10.4804 |
| 2024-08 | 45.0000 | -25.5808 | 0.6532 | -0.5685 | -0.1609 | 4.9919 | -25.5808 |
| 2024-09 | 101.0000 | 19.5434 | 1.1653 | 0.1935 | 0.0510 | 4.7842 | 17.2172 |
| 2024-10 | 104.0000 | -50.5766 | 0.6953 | -0.4863 | -0.1469 | 9.0205 | -48.7767 |
| 2024-11 | 100.0000 | -7.7169 | 0.9433 | -0.0772 | -0.0139 | 6.9287 | -7.0015 |
| 2024-12 | 83.0000 | -7.1560 | 0.9418 | -0.0862 | -0.0201 | 4.6881 | -7.3451 |

## D havi robusztusság

| Exit month | Trades | Realized PnL USDT | PF | Expectancy USDT | Avg R | Monthly DD % | Equity change USDT |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2024-01 | 39.0000 | -43.6851 | 0.5715 | -1.1201 | -0.2337 | 5.2426 | -43.6851 |
| 2024-02 | 84.0000 | -64.3385 | 0.6673 | -0.7659 | -0.1580 | 9.6802 | -64.3385 |
| 2024-03 | 74.0000 | -31.6452 | 0.7847 | -0.4276 | -0.0948 | 5.8956 | -31.6452 |
| 2024-04 | 32.0000 | -47.7301 | 0.3551 | -1.4916 | -0.3617 | 6.8957 | -47.7301 |
| 2024-05 | 71.0000 | -3.4746 | 0.9714 | -0.0489 | -0.0096 | 4.1493 | -3.4746 |
| 2024-06 | 15.0000 | -16.9630 | 0.5338 | -1.1309 | -0.2803 | 3.1265 | -16.9630 |
| 2024-07 | 43.0000 | -4.0593 | 0.9397 | -0.0944 | -0.0122 | 3.6711 | -4.0593 |
| 2024-08 | 28.0000 | -31.5666 | 0.4865 | -1.1274 | -0.2963 | 4.7033 | -31.5666 |
| 2024-09 | 55.0000 | 19.7091 | 1.2980 | 0.3583 | 0.0816 | 3.6944 | 16.1492 |
| 2024-10 | 57.0000 | -39.1003 | 0.6361 | -0.6860 | -0.1920 | 6.8568 | -35.5404 |
| 2024-11 | 75.0000 | -13.2008 | 0.8789 | -0.1760 | -0.0422 | 7.3973 | -13.7400 |
| 2024-12 | 53.0000 | -7.3424 | 0.9194 | -0.1385 | -0.0317 | 4.1926 | -6.8031 |

Trade metrikák exit month szerint; portfolio equity change külön oszlop, nyitott pozíciók markjai miatt eltérhet a realized PnL-től.
Monthly DD a meglévő HOURLY_EQUITY_OBSERVATIONS felbontáson, nem minute-precise. Rövid hónapos elemszámokat külön óvatosan kell értelmezni.

## Production / operáció / integritás

A kontrollhoz képest B equity +87.05 USDT, C +183.27 USDT, D +243.05 USDT.
Mind a négy eredmény veszteséges, PF < 1 és expectancy < 0.
D COMBINED kisebb összvesztesége alacsonyabb exposure és turnover mellett
keletkezett: 626 trade az 1607 helyett, átlagos exposure 6.98% a 17.68% helyett,
transaction cost 167.87 USDT a 392.95 helyett. A trade-minőség proxyi azonban
nem javultak: PF 0.799 → 0.759, avg R −0.0917 → −0.1054,
adaptive activation 55.32% → 53.99%. Nem következik ebből jobb belépési edge.

A realizált PnL szerint A-nak 2/12, B-nek 3/12, C-nek és D-nek 1/12 pozitív
hónapja volt. Április mindegyikben veszteséges maradt; D −47.73 USDT-ra
csökkentette a veszteséget, de PF-je csak 0.355. B januárja, májusa és augusztusa
rosszabb lett a kontrollnál; C/D január–márciusa és novembere sem javult.
Ez nem minden hónapban működő filter-előny, és nem indokol production promotiont.
Mind a négy run végén 0 OPEN / 0 OPEN_STALE pozíció maradt.
Az export alap-breakdownjában a 2025-01 bucket az exclusive end equity-markerének
0-trades/0-PnL csoportja, nem további kereskedési hónap; a riport csak 2024-et mutat.

Az újranyitott sessionben egyik replay sem lett újraindítva: mind a négy meglévő
run már COMPLETED volt. Csak read-only validáció és export-diagnosztika készült.
Mind a négy standard compact payload megegyezik a HTTP export core adataival;
a négyes compare API/böngésző ellenőrzése sikeres, JS hiba nélkül.
Képernyőkép: `artifacts/replay-entry-filter-2024/comparison-browser.png`.
Normál replay worker visszaállt (ellenőrzött PID 35145), scanner/paper PID maradt
1333/1334. A folytatás nem igényelt service restartot.

Production paper, ReferenceStrategy, RiskManager, PortfolioService, scanner/score, shadow adaptive modell és existing exit model nem változott.
Csak Replay Research és web/config/audit/export módosítás. Nincs live execution, reset, migration, .env módosítás vagy történeti rekord átírás.
A korábbi D trades/orders/fills/audits/equity table count és hash változatlan; közös archive revisions egyeznek.
Web restart történt a feature deploymenthez, idle normál replay worker helyett külön systemd research service futott; sikeres befejezés után normál worker visszaindult.
Scanner PID 1333 és paper PID 1334 változatlan; további restart nem szükséges a dokumentációhoz.

## Changed files

Replay: config.py, research.py, engine.py, rejections.py, models.py, exporter.py. Web: static/replay.js, templates/replay.html.
Új tests/test_replay_entry_filter.py; docs/REPLAY_ENTRY_FILTER.md és ez a validációs riport, README link.

## Exportok

A standard compact payload továbbra is tartalmazza metadata/config/variant/summary/breakdowns/trades/open_positions mezőit.
A feltölthető példányok research_analysis mezője read-only exit/cost/block/monthly/control-delta diagnosztikát ad; nem módosítja a tárolt replay summary-t.
Pontos root: `/home/ndvi/crypto-bot/artifacts/replay-entry-filter-2024/`.

- `/home/ndvi/crypto-bot/artifacts/replay-entry-filter-2024/A.compact.json` — SHA256 `b550158c128bcbcebd26cec4c8f0a37ac242d3afb8664035ab323aba0e3ee1c6`.
- `/home/ndvi/crypto-bot/artifacts/replay-entry-filter-2024/B.compact.json` — SHA256 `90c1f1bc512ffeda202803479751081f9782f4064068bf389a5070e836b53cbe`.
- `/home/ndvi/crypto-bot/artifacts/replay-entry-filter-2024/C.compact.json` — SHA256 `af5dfb16ec1cda3ca22f27c53ce3cdb042ed892f6417559823ee348ac61452cf`.
- `/home/ndvi/crypto-bot/artifacts/replay-entry-filter-2024/D.compact.json` — SHA256 `ec2159c062f3891e7111b5e5603d56a1e9ab07dff824378d292f672b1a42c6ef`.
- `/home/ndvi/crypto-bot/artifacts/replay-entry-filter-2024/comparison.compact.json` — SHA256 `52aaf2e34d83a065c93cf0cfdf805d4abff374e2981c74e2ac8af1dd3414106a`.

Control proof, validation.json, futási configok/ID-k/harness és desktop/mobile képernyőképek ugyanebben a könyvtárban.
