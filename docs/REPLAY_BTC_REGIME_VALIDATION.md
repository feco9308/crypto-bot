# BTC BULLISH entry gate — 2024 EXPLORATION validáció

**NOT STATISTICALLY VALIDATED. NO PRODUCTION PROMOTION.**

## Branch / kód

Fejlesztési branch: `feature/replay-btc-regime-gate`. Implementáció: `591c7dc`.
Production branch: `feature/trading-core-paper`. A production web/replay deployment
frissült; production paper, ReferenceStrategy, scanner/score/RiskManager,
PortfolioService és live/paper execution kódja nem változott.

459 sikeres teszt (92.61s), 16 új gate/adapter teszttel. Ruff tiszta. A UI
gate import/export roundtrip böngészőből is ellenőrizve, új implicit futás nélkül.

## Módszer

Négy **független normál ReplayEngine** futás; teljes cash/positions/exposure/
sizing/risk/execution pipeline. Nem Shadow filtering. Az éves archívumokat a
korábbi anchor rögzített revízióival használtuk, candidate set 693 symbol,
azonos historical Top 50 kiválasztás. A közös többvariánsos held-symbol-history
coupling elkerülésére mindegyik változat saját scanner-history-t is használ.

2024-01-01 → 2025-01-01, EXPLORATION, daily loss/drawdown guard 100%,
RESEARCH_QUARANTINE_STALE, conservative intrabar, strict no-lookahead,
fallback_5m=false. Az anchor variant 0 minden más entry/risk paramétere azonos.

A: gate OFF / fixed_tp 1.5%. B: BULLISH_ONLY / fixed_tp 1.5%.
C: BULLISH_ONLY / r_tp 0.5R. D: BULLISH_ONLY / adaptive_peak_lock_v1.

Az új D replay-only adapter ugyanazt az adaptive Shadow modellt használja,
valós replay sizing/fee/slippage mellett, checkpointban perzisztált stop/peak
állapottal. Stop-only policy; production regisztráció nem történt. A gate
SELL/stop/TP kilépéseket soha nem blokkol.

## BTC regime és no-lookahead

Forrás a már meglévő `crypto_bot/replay/engine.py` BTCUSDT scanner-row értéke:
EMA50 > EMA200 → BULLISH, egyébként BEARISH, hiányzó sor → UNKNOWN.
Ugyanez került a korábbi canonical entryk BTC breakdownjába. Nincs új EMA
definíció vagy külön market-data feed. A már lezárt, friss BTC candle source
timestampje szerepel a döntési auditban. A gate future/missing/stale source-ra
fail closed. Minden gated BUY forrástimestampjét ellenőriztük: strictly past,
legfeljebb 1h régi. Minden gate-blokk eredeti döntése BUY; teljes elutasítási
breakdown perzisztált auditból ellenőrizve, számlálók nem vesztek el.

## Futások

| Variant | Run ID |
| --- | --- |
| A | `03e9e8c433e740ad86d0ef5a9969c1a2` |
| B | `dbc72bea14324608880c129d82fe9a60` |
| C | `bd0605611d804d44bed10ea0003dcc41` |
| D | `e79eb00e28eb408f8d0d38df3fb17540` |

## Portfolio eredmények

| Mutató | A | B | C | D |
| --- | ---: | ---: | ---: | ---: |
| Initial balance USDT | 1000.000000 | 1000.000000 | 1000.000000 | 1000.000000 |
| Final equity USDT | 165.520238 | 312.493166 | 392.717693 | 473.551165 |
| Return % | -83.447976 | -68.750683 | -60.728231 | -52.644883 |
| Realized PnL USDT | -834.479762 | -687.506834 | -607.282307 | -526.448835 |
| Unrealized PnL USDT | 0.000000 | 0.000000 | 0.000000 | 0.000000 |
| Closed trades | 4871.000000 | 3395.000000 | 2623.000000 | 1607.000000 |
| Open trades | 0.000000 | 0.000000 | 0.000000 | 0.000000 |
| Win rate % | 72.982960 | 73.284242 | 65.611895 | 41.257001 |
| Profit factor (actual PnL) | 0.708788 | 0.738263 | 0.796874 | 0.798960 |
| Expectancy USDT/trade | -0.171316 | -0.202506 | -0.231522 | -0.327597 |
| Average R | -0.074219 | -0.068955 | -0.071009 | -0.091666 |
| Median R | 0.210755 | 0.216367 | 0.398633 | 0.000000 |
| Max drawdown % | 83.909081 | 68.900431 | 60.882663 | 53.998886 |
| Average hold seconds | 13333.894478 | 13210.957290 | 18477.026306 | 32265.625389 |
| Total fees USDT | 461.869280 | 437.785506 | 397.992794 | 261.963261 |
| Total slippage USDT | 230.935063 | 218.893224 | 198.996739 | 130.981782 |
| BTC gate blocks | 0.000000 | 3432.000000 | 3431.000000 | 3406.000000 |
| Stale positions | 0.000000 | 0.000000 | 0.000000 | 0.000000 |

Az adaptive break-even kerekítési töredéke flatnak számít (0.000001 százalékpont
tolerancia), mint a Shadow elemzésben. Más policy-k statisztikája változatlan.
Final equity tartalmazza az esetleges nyitott pozíciók utolsó markját; stale
esetben ez nem garantált likvidálható érték. Export warningokat meg kell tartani.

## Elsődleges A vs B összehasonlítás

Azonos Fixed TP mellett csak a gate tér el (az account és a scanner-history
független). B−A final equity különbség: 146.972928 USDT.
Return különbség: 14.697293 százalékpont.
Max drawdown B−A: -15.008650 százalékpont.
Ez kizárólag a 2024 EXPLORATION időszakban megfigyelt eredmény; nincs production
promotion, optimalizálás, coin-router, ATR filter vagy EMA-paraméter keresés.

A gate 1476-tal csökkentette a lezárt trade-ek számát. Mind a négy portfolio
veszteséges, profit factorjuk 1 alatti és expectancy-jük negatív. Az A/B
javulás kisebb veszteséget jelent, nem igazoltan profitábilis stratégiát.
Mindegyik futás végén 0 OPEN és 0 OPEN_STALE pozíció maradt; nem kellett
év végi kényszerzárás. D 157 intrabar-ambiguous trade-je a kért conservative
OHLC szabályok szerint lett kezelve.

## Blocked BUY reasons

| Variant | BEARISH gate | UNKNOWN gate | STALE gate | Teljes gate |
| --- | ---: | ---: | ---: | ---: |
| A | 0 | 0 | 0 | 0 |
| B | 3432 | 0 | 0 | 3432 |
| C | 3431 | 0 | 0 | 3431 |
| D | 3406 | 0 | 0 | 3406 |

Teljes tényleges elutasítási kategóriák (nem csak gate):

- A: `{"below_minimum_order_or_balance_exposure_room": 88, "max_positions": 7192, "missing_execution_minute": 1}`.
- B: `{"below_minimum_order_or_balance_exposure_room": 2, "btc_regime_entry_block": 3432, "max_positions": 5868, "missing_execution_minute": 1}`.
- C: `{"btc_regime_entry_block": 3431, "max_positions": 6460, "missing_execution_minute": 1}`.
- D: `{"btc_regime_entry_block": 3406, "max_positions": 7127, "missing_execution_minute": 1}`.

A gate opportunity-t számol, a RiskManager előtt: egy blokkolt BUY nem feltétlenül
kapott volna risk approvalt gate nélkül. Nem állítjuk, hogy minden blokkból
trade lett volna. A hagyományos `blocked_entries.other` összesítést megtartjuk,
de a részletes kategória külön azonosítja a BTC blokkokat.

## Operáció és integritás

Csak web/replay research worker műveletek történtek; scanner PID 1333 és paper
PID 1334 változatlan. Az izolált, termináltól független systemd research service
legfeljebb két ReplayEngine jobot futtatott párhuzamosan. A végén visszaindította
a normál replay workert. Nincs production migration/reset/setting change,
historical audit rewrite vagy source anchor változtatás. Az anchor trades,
orders, fills, audit és equity táblák darabszáma és hash-e változatlan.
.env hash változatlan; a production paper állapotot nem írta a research.

Mind a négy HTTP compact export summary-ja és variant-konfigurációja egyezik
a helyi exporttal. A compare API négy azonos periódusú, stale warning nélküli
eredményt adott. A böngészős compare oldal négy sort renderelt JS hiba nélkül;
képernyőkép: `artifacts/replay-btc-gate-2024/comparison-browser.png`.
A research service sikeresen befejeződött, a normál replay worker aktív
(ellenőrzött PID 27729). Web PID 26372, scanner PID 1333 és paper PID 1334.
Eredmény- és dokumentációs lezáráshoz további restart nem szükséges.

## Fájlok

Új: `crypto_bot/replay/research.py`, `tests/test_replay_btc_gate.py`,
`docs/REPLAY_BTC_REGIME_GATE.md`, ez a validációs riport.

Replay módosítások: `config.py`, `engine.py`, `rejections.py`, `models.py`,
`metrics.py`, `exporter.py`. Web UI: `web/replay.py`, `web/static/replay.js`,
`web/templates/replay.html`. Adaptive korábbi teszt/dokumentáció frissítése
a kifejezetten engedélyezett új research elérhetőségre.

## Exportok

Pontos root: `/home/ndvi/crypto-bot/artifacts/replay-btc-gate-2024/`.

- `A.compact.json` — SHA256 `a21be050b20db014235134be8be6a90d040cd8d64d7bd6a81bead4b4b787275d`.
- `B.compact.json` — SHA256 `21c5f357177e5c48cf1742e7021494833101b2b6cb259774de845648040d8e73`.
- `C.compact.json` — SHA256 `bcb5a332e4cd9b5f15a32de7a84dfa41b98e394af4c05fb72acc972830b6f63c`.
- `D.compact.json` — SHA256 `fa1468529c13fcd9fa14864746a11a00fda9aa1aeec7cf35cef44816378e0fce`.
- `comparison.compact.json` — SHA256 `9a6efa1044eb7dd41277fc3d97988856db440c2d537b4c8c8e3da74124b2d20a`.

A négy per-variant compact export és az összesített comparison export minden
kért portfolio metrikát, konfigurációt, archivum hash-eket és blocked reason
breakdownt tartalmaz. A harness, a konfigurációk, a run ID-k, a validátor és
a validációs bizonyítékok ugyanebben a könyvtárban maradnak.
