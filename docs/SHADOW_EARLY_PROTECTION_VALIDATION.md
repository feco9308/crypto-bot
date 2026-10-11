# 2024 B Shadow Early Protection — EXPLORATION

Anchor: `b9275979397440f2aabbe4a89e4f9cc4`, variant 0. Pontosan 1506 meglévő FILLED BUY. Új entry, filter vagy portfolio replay nincs.

A: `adaptive_peak_lock_v1` (control). B: `adaptive_peak_lock_050be`. C: `adaptive_peak_lock_075_lock025`.

A kontroll conservative eredménye **1506/1506, 0 mismatch**: exit timestamp/fill/reason/stage/PnL/R/fee/slippage. Candidate-ek csak a sikeres kontroll után indultak.

## Változtatási határ és végrehajtás

A candidate osztály a `research/shadow_early_protection.py` fájlban van, az installált application package-en kívül. Nem regisztráltuk productionbe, normál Replay exitként vagy webes trading policy-ként. A meglévő `AdaptivePeakLock.observe/walk/ratchet` és `ShadowPosition.bar/close/fresh/export` kód fut változatlanul.

B kizárólag az első raw MFE-R activationt változtatja 0.75-ről 0.50-re. C activationja marad 0.75R; első protective stopja nettó 0.25 × canonical initial risk nyereséget céloz. Az 1R/2R ATR peak trail és a monotonic ratchet mindháromnál ugyanaz.

A net protection ára: `(canonical_notional + entry_fee + target_R * canonical_risk + existing_rounding_guard) / quantity / (1-exit_fee_rate) / (1-exit_slippage_rate)`, a meglévő ceiling/12-decimal guarddal. A canonical fill már tartalmazza a belépési slippage-et: nem számoljuk kétszer. A BUY quantity/notional/risk/original-stop változatlan.

A protection cél nem garantált gap-fill: stop alá nyitó következő gyertya esetén a meglévő szabály szerinti opening-price fill történik. Ezt teszt védi.

Csak SHA256-ellenőrzött, már cache-elt Binance 1m ZIP-ek olvasása; SQLite mode=ro. 575 használt 1m objektum, **0 supplemental forrás**, 0 HTTP hívás. Nincs új market data letöltés, order API vagy trading kulcs.

Mind conservative, mind optimistic a meglévő lokális OHLC path-enumerationt használja, az ismeretlen high/low sorrendet ambiguityként auditálva. Nem feltételezi automatikusan a high-first sorrendet. A gyertya csak close után figyelhető meg. A két mód lokális intrabar érzékenység, nem minden lehetséges globális árút matematikai korlátja.

Stale/gap: ugyanaz a Shadow quarantine és incomplete flag; nincs invented fill, retroaktív last-price exit vagy év végi force close. Minden outcome CLOSED; legkésőbbi conservative exit: 2024-12-19 15:25 UTC.

## Conservative fő eredmény

WIN/FLAT/LOSS mindháromnál azonos existing adaptive tolerance szerint: flat, ha `abs(return_pct) <= 0.000001`. A szigorúan pozitív R-ből származó kerekítési maradékokat nem értelmezzük érdemi winnek.

| Mutató | A control | B 0.50R BE | C 0.75R → net 0.25R |
|---|---:|---:|---:|
| Closed | 1506 | 1506 | 1506 |
| Open | 0 | 0 | 0 |
| Stale | 0 | 0 | 0 |
| Incomplete | 0 | 0 | 0 |
| Win | 634 | 501 | 847 |
| Flat | 213 | 483 | 0 |
| Loss | 659 | 522 | 659 |
| Mean R | -0.07451615 | -0.06255178 | -0.07616965 |
| Median R | 0.00000000 | 0.00000000 | 0.25000000 |
| Total R | -112.22132592 | -94.20297502 | -114.71149715 |
| PF_R | 0.82971133 | 0.81815180 | 0.82593266 |
| Mean return % | -0.26978561 | -0.23082810 | -0.30097496 |
| Normalized mean PnL / 1000 entry cost | -2.69785615 | -2.30828100 | -3.00974957 |
| Normalized sum PnL / 1000 entry cost | -4062.97135476 | -3476.27118164 | -4532.68285013 |
| Canonical actual PnL USDT | -439.399654888308 | -345.849091886636 | -456.291957931916 |
| Fee USDT | 261.037821158777 | 261.131465365988 | 261.020911946533 |
| Slippage USDT | 130.519099147642 | 130.565945199899 | 130.510652914356 |
| Mean hold seconds | 30451.19521912 | 23078.20717131 | 29143.94422311 |
| Ambiguity count | 130 | 108 | 122 |

**Ez matched-entry counterfactual, nem portfolio equity.** A canonical notionalok/riskek az eredeti B wallet-pályán alakultak ki; a korábbi candidate exit miatt felszabaduló tőkét nem fektettük újra. A normalized PnL független, 1000 entry-costra skálázott trade-ek összege/átlaga.

## Stage contribution

| Policy | Exit stage | N | Mean R | Total R contribution |
|---|---|---:|---:|---:|
| A | BREAK_EVEN_075R | 223 | 0.00000005 | 0.00001105 |
| A | ORIGINAL_STOP | 659 | -1.00000974 | -659.00641610 |
| A | PEAK_TRAIL_1R | 610 | 0.85512362 | 521.62541027 |
| A | TIGHT_PEAK_TRAIL_2R | 14 | 1.79711920 | 25.15966885 |
| B | BREAK_EVEN_050R | 508 | -0.00004806 | -0.02441472 |
| B | ORIGINAL_STOP | 518 | -1.00001238 | -518.00641171 |
| B | PEAK_TRAIL_1R | 468 | 0.86021244 | 402.57942164 |
| B | TIGHT_PEAK_TRAIL_2R | 12 | 1.77070248 | 21.24842978 |
| C | ORIGINAL_STOP | 659 | -1.00000974 | -659.00641610 |
| C | PEAK_TRAIL_1R | 527 | 0.85203074 | 449.02019918 |
| C | POSITIVE_LOCK_075R_NET025R | 310 | 0.25000006 | 77.50001782 |
| C | TIGHT_PEAK_TRAIL_2R | 10 | 1.77747019 | 17.77470195 |

## Rescue, winner damage és reconciliált nettó hatás

| Hatás | B ΔR | C ΔR |
|---|---:|---:|
| rescued_original_stops | 141.00000945 | 0.00000000 |
| improved_break_even_exits | 0.00000000 | 55.75000113 |
| truncated_future_winners | -122.96437435 | -56.88350025 |
| other_exit_changes | -0.01728420 | -1.35667211 |
| **Összes ΔR** | **18.01835090** | **-2.49017123** |

B: 141 rescued ORIGINAL_STOP (136 flat, 5 kicsiny, quantization-guard által pozitívnak sorolt eredmény), **+141.00000945R**. 146 strict-positive control eredmény rosszabb: 144 érdemi nyerő és 2 break-even trade. Korábbi exit miatti teljes veszteség **122.98165855R**, ebből érdemi nyerőké 122.96437435R.

C: 0 original-stop rescue. 223 control BE trade javulása **+55.75000113R**. 87 nyerő rosszabb, összesen **58.24017236R** veszteség: 83 szigorúan korábbi exit **56.88350025R**, 4 azonos percű, más intrabar fill **1.35667211R**. Azonos percnél nem állítjuk, hogy az exit ténylegesen előbb történt.

A decomposition disjoint: rescued original stops → improved control BE → korábban levágott nonflat win → egyéb. Így a 2 BE-gap loss és a 4 azonos percű C damage az egyéb kategóriában van. A floating-point residual abszolút értéke < 1e-8R.

Winner damage: control `realized_r > 0`, candidate ΔR < −1e-8; a küszöbtáblák minden damage-et tartalmaznak, a strict-earlier subset külön exportált. Az 1e-8 tolerancia kizárólag rounding-noise megkülönböztetés, nem trading threshold.

| Candidate | Control R > | Lost >0.10R N | Lost >0.25R N | Lost >0.50R N |
|---|---:|---:|---:|---:|
| B | 0.5 | 144 | 144 | 144 |
| B | 1.0 | 28 | 28 | 28 |
| C | 0.5 | 87 | 87 | 51 |
| C | 1.0 | 28 | 28 | 28 |

A teljes stage→outcome és stage→stage transition matrix a compactban, a trade-enként visszakövethető canonical ID-k és minden outcome a detailedben található.

## Kontroll MFE cohortok

Conditioning kizárólag az eredeti B trade `highest_r` értékéből; nem a candidate korábban levágott MFE-jéből. A cohortok egymásba ágyazottak, nem összeadandók.

| Control ORIGINAL_STOP highest R ≥ | N | A total R | B total R | C total R | B rescue N |
|---|---:|---:|---:|---:|---:|
| 0.25 | 341 | -341.000014 | -200.000005 | -341.000014 | 141 |
| 0.4 | 213 | -213.000009 | -72.000000 | -213.000009 | 141 |
| 0.5 | 141 | -141.000004 | 0.000005 | -141.000004 | 141 |
| 0.6 | 75 | -75.000001 | 0.000001 | -75.000001 | 75 |
| 0.7 | 23 | -23.000000 | 0.000000 | -23.000000 | 23 |

C rescue mindegyik cohortban 0, minden A/B/C total és pairwise diagnosztika exportálva.

## Quarterly és havi robustness

UTC **entry month/quarter**, nem exit hónap. Ugyanazok a canonical trade-ek mindhárom policy alatt.

| Időszak | N | A total R | B total R | C total R | B ΔR | C ΔR |
|---|---:|---:|---:|---:|---:|---:|
| Q1 | 481 | -17.586753 | -4.374863 | -22.916082 | 13.211889 | -5.329330 |
| Q2 | 260 | -43.431734 | -37.755888 | -43.150065 | 5.675847 | 0.281669 |
| Q3 | 337 | -23.706983 | -25.985296 | -22.363039 | -2.278313 | 1.343944 |
| Q4 | 428 | -27.495856 | -26.086928 | -26.282310 | 1.408928 | 1.213546 |
| 2024-01 | 113 | -18.494556 | -6.413607 | -21.441601 | 12.080948 | -2.947045 |
| 2024-02 | 209 | -2.204784 | -2.923033 | -0.427401 | -0.718248 | 1.777383 |
| 2024-03 | 159 | 3.112587 | 4.961777 | -1.047080 | 1.849189 | -4.159668 |
| 2024-04 | 73 | -26.096112 | -16.088226 | -29.115286 | 10.007887 | -3.019174 |
| 2024-05 | 145 | -12.077068 | -16.980783 | -9.466855 | -4.903716 | 2.610212 |
| 2024-06 | 42 | -5.258554 | -4.686879 | -4.567924 | 0.571675 | 0.690630 |
| 2024-07 | 127 | -11.365077 | -12.705971 | -9.560666 | -1.340895 | 1.804411 |
| 2024-08 | 76 | -21.134958 | -16.145566 | -19.201633 | 4.989392 | 1.933325 |
| 2024-09 | 134 | 8.793051 | 2.866241 | 6.399260 | -5.926810 | -2.393792 |
| 2024-10 | 131 | -17.522031 | -10.585288 | -14.968047 | 6.936743 | 2.553984 |
| 2024-11 | 189 | 11.813298 | 2.877543 | 10.833038 | -8.935755 | -0.980260 |
| 2024-12 | 108 | -21.787123 | -18.379184 | -22.147302 | 3.407940 | -0.360178 |

B: 3/4 quarter, 7/12 hónap pozitív relative ΔR. Január nélkül +5.9374R, április nélkül +8.0105R; a kettő nélkül **−4.0705R**. Tehát nem kizárólag egyetlen hónap adja a javulást, de két hónapra erősen koncentrálódik. A jól teljesítő szeptemberben és novemberben B ront.

C: 3/4 quarter és 6/12 hónap relative ΔR pozitív, de Q1 vesztesége miatt évesen negatív. Nincs hónapszűrés vagy paraméterkeresés; a leave-one-month-out csak leíró robustness diagnosztika.

## Conservative vs optimistic

| Policy | Conservative total R | Optimistic total R | Conservative ΔR vs A | Optimistic ΔR vs A |
|---|---:|---:|---:|---:|
| A | -112.22132592 | -96.43861927 | 0.00000000 | 0.00000000 |
| B | -94.20297502 | -82.59816672 | 18.01835090 | 13.84045255 |
| C | -114.71149715 | -100.30064271 | -2.49017123 | -3.86202344 |

Az irány mindkét módban azonos: B kis javulás, C romlás; mindhárom expectancy negatív. Az optimistic A-t az optimistic candidate-ekkel vetettük össze, nem a conservative A-val.

A/B/C eltérő exit fill darabszáma a két mód között: 127/96/109; eltérő stage: 4/3/8; eltérő exit timestamp: 0/0/0. Teljes optimistic metrics, damage, decomposition és robustness is exportált.

## Validáció, futtatás, export

Full maintained suite: **521 passed**, 91.49 s; ebből **19 új regression teszteset**. `ruff check crypto_bot tests research`: sikeres. A teljes ellenőrzés a validation.json-ban van. Védett production fájl nem változott, nincs migration/reset/audit-record módosítás.

A külön, termináltól független `crypto-shadow-early-protection-research.service` sikeresen lefutott; scanner, paper, web és normál replay-worker nem restartolt. Research policy-k nem szerepelnek sem az EXITS, sem a normál RESEARCH_EXITS registryben.

Új kísérletet ez a kutatási modul indít (kizárólag Shadow):

```bash
PYTHONPATH=/home/ndvi/crypto-bot-shadow .venv/bin/python -m research.shadow_early_protection \
  --root /home/ndvi/crypto-bot
```

A meglévő három befejezett fázis újraszimulálása nélkül csak riportkészítés: `--summarize-existing`. Ez hiányzó fázis esetén megáll, nem indít implicit futást. Már létező fázist eltérő anchor mellett nem ír felül.

Pontos exportok:

- `/home/ndvi/crypto-bot/artifacts/shadow-early-protection-2024/2024-B-shadow-early-protection.compact.json`
- `/home/ndvi/crypto-bot/artifacts/shadow-early-protection-2024/2024-B-shadow-early-protection.detailed.json`
- `/home/ndvi/crypto-bot/artifacts/shadow-early-protection-2024/validation.json`

Következtetés: korábbi BE valóban megment néhány eredeti stopot, de a megmentett R jelentős részét korábbi nyerők levágása elveszi. A fixed 0.25R positive lock javítja a BE-ket, de többet veszít a későbbi nyerőkön. Nincs production promotion és nincs új filter. NO LIVE TRADING IMPLEMENTED. 2024 EXPLORATION.
