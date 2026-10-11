# Production reboot ellenőrzés — 2026-10-11

## Kiinduló állapot

A checkout tiszta volt: `feature/trading-core-paper`,
`0639a33b1612fb18e87de41a003431a98e647675`. A GitHub production és
stale-quarantine branch ugyanitt állt; nem volt újabb, deployra szánt remote
verzió. A blocked-entry observability, quarantine és config import/export
változásai megmaradtak. Nem történt pull, reset vagy régebbi verzió visszaállítása.

A vizsgálat kezdetén **mind a négy szolgáltatás active/running és enabled volt**:
scanner PID 1359, web PID 1361, paper PID 1360, replay worker PID 1362.
Valós felhasználó: `ndvi`, UID 1000; `Linger=yes` már engedélyezve volt.
Mind a négy az aktuális boot indulásakor elindult, `NRestarts=0` értékkel.
A bootnapló 2026-10-10 07:28:27 UTC indulást rögzít; ugyanott az óra később
visszalépett. Az ellenőrzéskor az NTP már aktív, az óra szinkronizált.
Az autostart elmaradása a jelenlegi boot naplójából nem igazolható.

A backend `0.0.0.0:8000` címen figyelt; `/health` HTTP 200, status ok.
`https://crypto.vagilak.hu/health` a LAN-proxy 192.168.1.17 címéről
HTTP 401-et és `WWW-Authenticate: Basic` fejlécet adott. Ez az OpenResty/Nginx
Proxy Manager hozzáférésvédelme, nem alkalmazásleállás vagy DNS-hiba.
A proxy konfigurációját és jelszóvédelmét nem módosítottuk.

## Igazolt paper adatlekérési hiba

A paper process működött, de minden public quote-hiány miatt DEGRADED volt.
A szimbólumlistában `牛来USDT` szerepelt. A korábbi `json.dumps` ezt literal
Unicode-escape formában küldte. Binance bookTicker HTTP 400 / -1100
„Illegal characters” hibával a teljes batch-et elutasította.

Azonos public GET kérésekkel igazoltuk:

- compact ASCII lista BTCUSDT/ETHUSDT: HTTP 200;
- compact BTCUSDT/牛来USDT lista `ensure_ascii=True`: HTTP 400;
- ugyanaz `ensure_ascii=False`: HTTP 200, mindkét quote megérkezett.

Javítás: kizárólag a request paraméter UTF-8 JSON szerializálása változott a
`crypto_bot/trading/quotes.py` fájlban. Nincs más endpoint, API key, signed
request, fallback, orderküldés vagy strategy/risk/portfolio változtatás.
Mock regressziós teszt ellenőrzi a compact JSON-t és a GET-only hívást.

## Autostart és restart

A meglévő négy user unitot használjuk. Mindegyikben:

```ini
[Unit]
StartLimitIntervalSec=0

[Service]
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

A korábbi RestartSec 15 helyett 5. A végtelen időtávú újrapróbálás boot/hálózati
indulási hibánál megakadályozza a start-limit miatti tartós letiltást.
Az operatori stop továbbra is stop marad; nem indokolta semmi a Restart=always-t.

A replay user unitból eltávolítottuk a rendszerszintű hálózatot ténylegesen nem
szinkronizáló `network-online.target` függőséget. A scanner normál ciklusai
retry-znek, a paper quote-hibánál friss ár nélkül elutasít és újrapróbálkozik,
a webnek nem kell Binance az induláshoz. A replay worker helyi DB-vel indul,
hálózati feladat hibáját külön run hibaként jelenti. A restart policy egy
process-indulási hibát újrapróbál, egy FAILED replay jobot nem futtat újra.
Nincs sleep hack vagy külön hálózatfigyelő process.

Az abszolút WorkingDirectory/ExecStart/EnvironmentFile utak léteznek; a
systemd user manager ndvi identitását öröklik. A replay worker abszolút
REPLAY_DATA_DIR-ja a production data könyvtár. A `.env` változatlan maradt.
A `default.target` enable-symlinkek és linger miatt interaktív SSH/login nem kell.
`systemd-analyze --user verify` hibamentes; daemon-reload és enable --now
mind a négy unitra megtörtént. A futó scanner adatrögzítésének megőrzésére
nincs szükségtelen scanner restart. A quote javítás betöltéséhez csak paper
restart szükséges; a már futó web és idle replay worker kódja nem változott.

## Ellenőrzés és adatok

Az indulás előtti státusz/paper/DB-count bizonyítékok:
`artifacts/reboot-recovery-2026-10-11/before.json` és `*.before.txt`.
Az újraindítás utáni bizonyíték az ugyanott tárolt `after.json`.
Ezek Gitből kizárt helyi auditfájlok.

Nincs DB migration, reset, env-módosítás, replay cache vagy eredménytörlés.
Paper generation 1, induló cash/equity 948.211234726629 USDT,
realized PnL -51.788765273371 USDT; nyitott pozíció 0. A meglévő drawdown-limit
megmarad: a friss quote visszatérése nem oldja fel a normál risk tiltást.
A korábbi hét replay run, 21 variant és a befejezett 2024 research run megmarad.

A teljes karbantartott tesztcsomag futtatásakor kiderült, hogy a Chromiumhoz
szükséges korábbi `/tmp` könyvtár rebootkor eltűnt. A tesztfüggőségeket most
tartósan, adminjog nélkül bontottuk ki ide:
`~/.local/lib/crypto-bot-browser/`. Ez nem production runtime függőség.

```bash
PYTHONPATH=/home/ndvi/crypto-bot-recovery \
LD_LIBRARY_PATH=/home/ndvi/.local/lib/crypto-bot-browser/usr/lib/x86_64-linux-gnu \
/home/ndvi/crypto-bot/.venv/bin/pytest -q
```

A teljes karbantartott tesztcsomag **396 passed** (86 másodperc), beleértve
a Unicode public GET regressziót és a desktop/mobile browser teszteket.
`ruff check crypto_bot tests`, `bash -n scripts/crypto-status` és mind a négy
user unit systemd verify ellenőrzése sikeres.

## Egyparancsos diagnosztika

```bash
crypto-status
```

Forrás: `scripts/crypto-status`; felhasználói symlink:
`~/.local/bin/crypto-status`. Megmutatja a valós usert/lingert, mind a négy unit
állapotát, PID-jét, enabled állapotát, restart policy-jét, indulási idejét,
restart-számát, rövid journal figyelmeztetéseit és a helyi health választ.
Nem ír adatbázisba és nem állít le/indít szolgáltatást.

Nincs automatikus szerver-reboot; az autostart feltételeit logikailag ellenőriztük.

## Deploy utáni állapot

A javítás commitja `7ccf194`, a production branch
`feature/trading-core-paper`. A checkoutot helyi `merge --ff-only` frissítette;
a GitHubon nem volt behozandó újabb commit. Mind a négy unit enabled/running,
linger továbbra is yes. Scanner PID **1359**, web PID **1361**, paper PID
**19043**, replay worker PID **1362**. Csak a paper PID változott (1360-ról).
Az érintetlen modulokat futtató scanner/web/replay megtartotta eredeti processét;
startup Git-metaadatuk ezért még a korábbi 0639a33-at jelezheti.

A paper friss ciklusai már RUNNING állapotúak, új quote warning/exception nincs.
A régi last_error mező történeti adatként megmarad. Trading ON, paper generation,
cash/equity, realized PnL és a 0 nyitott pozíció változatlan.
Mindkét SQLite `PRAGMA quick_check` eredménye ok, a `.env` SHA-256 változatlan,
a meglévő paper/history/replay rekordszámok nem csökkentek. A scanner újabb
sikeres ciklust írt; a replay worker API-n READY, friss heartbeat és PID 1362,
mind a hét korábbi replay job és eredmény látható, a 2024 research run COMPLETED.
Backend health HTTP 200; a publikus proxy továbbra is Basic Authot kér.

Módosított repo-fájlok: README, ez a riport, `crypto_bot/trading/quotes.py`,
`tests/test_paper_quotes_permissions.py`, `scripts/crypto-status`, a négy
`deploy/user/crypto-*.service` minta. A négy telepített user unit ezekkel frissült,
az eredeti unitok auditkönyvtárba mentve. Új `~/.local/bin/crypto-status` symlink
és tartós browser tesztkönyvtár készült. Environment/config, production adatbázis
séma, stratégia, score, risk sizing és live végrehajtás nem változott.

## Valódi reboot próba és bootkori órahiba

A felhasználó ezután valóban újraindította a szervert. Az új bootban mind a
négy service **5,4 másodperccel boot után** indult, új PID-ekkel:
scanner 1333, paper 1334, web 1335, replay worker 1336. Mind enabled/running,
linger yes, NRestarts 0. Az autostartot ezzel tényleges reboot is igazolta.

A paper kezdeti DEGRADED és a scanner ERROR állapota más okból keletkezett:
a gép órája bootkor két órával előrébb járt. A timesyncd napló először
13:04 UTC-t mutatott, majd az első NTP szinkron **2026-10-11 11:05:00 UTC**
időre állította vissza. Előtte a scanner minden gyertyát stale-nek minősített,
a paper „No fresh closed 1h scanner observations” figyelmeztetést adott.
Ez nem a Unicode quote-hiba visszatérése. A helyes órával a paper már magától
RUNNING lett; a scanner a következő normál ciklusban újra próbálkozik.
Az RTC-beállítás/hypervisor oldal pontos oka nem bizonyított; nem állítottuk
át a hardverórát vagy a rendszerszintű NTP-konfigurációt.

A következő boot védelmére mind a négy user unit kapott:

```ini
ExecStartPre=/usr/lib/systemd/systemd-time-wait-sync
TimeoutStartSec=120
```

Ez a systemd beépített, eseményalapú szinkron-várakozója, nem sleep.
A telepített upstream system unit megjegyzése szerint maga a program nem
igényel CAP_SYS_TIME-ot. Felhasználóként, majd átmeneti user service-ként is
kipróbáltuk: sikeres, exit 0. A systemd unit verify mind a négy mintára sikeres.
Ha még nincs szinkron, az ExecStart nem fut; timeout esetén az on-failure/5s
policy korlátlanul újrapróbálja. A szinkronhoz a meglévő, aktív timesyncd kell.
Az általános időkapu más NTP implementációra nem lett tesztelve.

A minták és telepített unitok frissültek daemon-reloaddal. A már működő
process-eket nem restartoltuk. Az új időkapu valódi boot alatti kivárását ezért
egy későbbi reboot igazolhatja; újabb rebootot nem végeztünk automatikusan.
Az aktuális process-ek korai started_at/last_error időbélyegei a hibás órából
származó történeti metaadatok maradnak, nem írtuk át őket.

Reboot után a paper generation/ON/cash/equity/PnL/pozíciószám változatlan,
mind a hét replay run és 52701 replay fill megmaradt; research run COMPLETED,
worker READY és friss heartbeat. `.env` SHA-256 változatlan. A bizonyíték:
`artifacts/reboot-recovery-2026-10-11/reboot-proof.json`.
Alkalmazáskód, score, strategy, risk és ledger logika ehhez nem változott;
csak a négy unitminta és az üzemeltetési leírás módosult.
