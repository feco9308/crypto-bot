# Paper Trading Core — stratégia, risk és könyvelés

**NO LIVE TRADING IMPLEMENTED.** [Telepítés és vezérlés](../README.md).

## Architektúra és audit

Persistált scanner snapshot → `StrategyEngine` → `StrategySignal` → `RiskManager` → `PortfolioService` cash-foglalás → `ExecutionService` → `PaperExecutionService` → fill/ledger → adatbázis.

- `trading/domain.py`: közös, Decimal-alapú signal/risk/portfolio objektumok. BUY/SELL/HOLD; strength 0–1 relatív erősség, nem valószínűség.
- `trading/strategy.py`: cserélhető, tiszta stratégia; nincs API vagy orderküldés.
- `risk/manager.py`: tiszta engedélyezés és méretezés; nem küld ordert.
- `portfolio/service.py`: belső könyvelés; nem kommunikál tőzsdével és nem küld ordert.
- `execution/base.py`: place_order/cancel_order/get_order/get_positions interfész. `execution/paper.py`: kizárólag helyi paper végrehajtás. Későbbi adapter az orchestration execution_factory-jával cserélhető, a strategy és risk réteg átírása nélkül.
- `trading/engine.py`: az adatbázisban már szereplő friss snapshotokat használja; nem indít Binance market data hívást.

Minden signal a scanner snapshothoz kapcsolódik, a risk decision a signalhoz, az order a signal/riskhez, a fill az orderhez és positionhöz. Az auditoldal megmutatja a score-t, indikátorokat, paramétereket, döntéskori portfóliót, risk indoklást, fillt, fee/slippage költséget és zárást. A reset ezeket a paper rekordokat is törli; reset előtt exportálj/ments, ha meg akarod őrizni őket.

## Referencia-stratégia

Entry feltételek: effective WATCH/PINNED, score ≥70, elérhető Δ4h ≥0, EMAfast > EMAslow, RSI 40–70, relative volume ≥1. Stop = reference price − 2×ATR. Exit: score <45 vagy bearish EMA, application stop, opcionális take profit vagy manual close. Hiányzó momentum nem kitalált nulla: alapból HOLD. A `require_delta=false` csak kifejezett konfigurációval kapcsolható ki.

Minden küszöb konfigurálható `PAPER_CONFIG` JSON-fájllal vagy `PAPER_<FIELD>` environment változóval. Példa: `config/paper.example.json`; az env felülírja a JSON-t. A konfiguráció minden signal mellett persistál. Opcionális take profit: `take_profit_r_multiple`, alapból 0 = kikapcsolva. Az alkalmazás nem optimalizál a profitra.

## Risk és portfolio

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

