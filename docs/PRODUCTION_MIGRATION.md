# Kontrollált production migráció — 2026-10-05 (UTC)

Production checkout: `/home/ndvi/crypto-bot`, branch: `feature/trading-core-paper`,
alkalmazásverzió: **0.2.0**. Production SQLite: `/home/ndvi/crypto-bot/data/market.db`.
A scanner, web, paper engine és monitoring ugyanazt az abszolút DB URL-t használja.
**NO LIVE TRADING IMPLEMENTED. Paper trading OFF.**

## Audit és mentések

Korábbi production: `feature/market-scanner-v2`, commit `2d6ccfa`.
Paper fejlesztési kiindulópont: `e98c838`. A production szolgáltatások nem a
`/home/ndvi/crypto-bot-paper` checkoutból futnak. A fejlesztési checkout új branche:
`development/trading-core-paper`; preview engine/web leállítva, DB és fájlok megtartva.

Az auditkor régi scanner PID=12108 (tmux `crypto`), web PID=13425 (:8000);
preview engine PID=19027, Gunicorn master PID=19036 (:8001).
A folyamatok SIGTERM-mel szabályosan leálltak, nem használtunk SIGKILL-t.
A DB-t megnyitó régi process-ek és a portfoglalások eltűntek az átállítás előtt.
A tmux sessionök más tartalmát nem töröltük.

Mentési könyvtár: `/home/ndvi/crypto-bot-backups/20261005-194519-UTC/` (0700).

- `market-before-paper.db`: SQLite backup API-val készült, integritás `ok`.
- `market-before-paper-copy.db`: a konzisztens mentés további fájlmásolata.
- `market-quiescent-before-paper.db`: leállítás utáni végleges, konzisztens mentés;
  a leállításig létrejött további adatokat is tartalmazza, integritás `ok`.
- `production.env`, privát korábbi process environmentek, `pip-freeze.txt`.
- `repository.bundle`: az eredeti helyi Git branchek és commitok mentése;
  `repository-after-migration.bundle`: az üzemeltetési commitokat is tartalmazza.
- `before.json`, `quiescent-before.json`, `after-migration.json`: eredeti táblák
  sorszámai, teljes sorállomány SHA256-je és override-ok.
- `rehearsal.db`: a mentésen kipróbált migráció.
- `ROLLBACK.md`: a leállítás ELŐTT elkészült konkrét visszaállítási terv.
- `migration.log`, `scanner-smoke.log`, `web-checks.json`, `final-status.json`:
  ellenőrzési bizonyítékok. A végleges SHA/PID/időpont a `final-status.json` fájlban van.

GitHub push hitelesítés hiányában sikertelen (`could not read Username`).
Nem történt force push vagy main módosítás; a helyi commitok bundle-ben megmaradtak.
Hitelesítés után: `git push -u origin feature/trading-core-paper`.

## Adatmegőrzés és migráció

| Ellenőrzési pont | Scanner runs | Snapshots | Decisions | Overrides | Instruments |
|---|---:|---:|---:|---:|---:|
| Első, futás közbeni mentés | 95 | 4655 | 4655 | 4 | 3720 |
| Szabályos leállítás utáni mentés | 96 | 4704 | 4704 | 4 | 3720 |
| Migráció után, scanner indítás előtt | 96 | 4704 | 4704 | 4 | 3720 |
| Scanner `--once` próbakör után | 97 | 4753 | 4753 | 4 | 3720 |
| Első systemd scanner ciklus után | 98 | 4802 | 4802 | 4 | 3720 |

Minden régi tábla (schema_version, instruments, scanner_runs, snapshots, decisions,
overrides, override_events) teljes tartalmának SHA256-je pontosan egyezett a
migráció előtt/után. A scanner tovább gyűjt, ezért a későbbi counts növekedhetnek.

Megőrzött override-ok: ADAUSDT=PINNED, AAVEUSDT=WATCH, PENGUUSDT=IGNORE,
NILUSDT=PINNED. A legutóbbi eredeti scanner konfiguráció és az új checkout
`Settings.public_dict()` értéke pontosan egyezett. A score/scanner/indicator/data/config
modulok változatlanok a régi production kódhoz képest.

Az Alembic az eredeti adatbázist `0001_scanner` verzióként átvette, majd hozzáadta a
`0002_paper` tábláit. Nincs scanner drop/reset/recreate. A régi `schema_version=1`
kompatibilitási jelző megmaradt; az Alembic aktuális verziója `0002_paper`.
SQLite WAL aktív, SQLAlchemy busy_timeout=30000 ms, foreign_keys=ON.
A meglévő `.venv` megmaradt; requirements-dev és editable 0.2.0 telepítés megtörtént.

Új production paper account: 1000 USDT, OFF, nulla pozíció/fill/fee/PnL.
A preview ledger nem került át. Futás közben BUY jel és REJECTED auditorder
létrejöhet OFF állapotban; ezek trading-disabled elutasítások, nem végrehajtott trade-ek.

## Folyamatok, monitoring és web

Mindhárom szolgáltatás **ndvi felhasználói systemd unit**, engedélyezve:

| Service | Unit | WorkingDirectory | Port |
|---|---|---|---|
| Market Scanner | crypto-market-scanner.service | /home/ndvi/crypto-bot | nincs |
| Gunicorn web (master + két worker) | crypto-web.service | /home/ndvi/crypto-bot | 0.0.0.0:8000 |
| Paper Trading Engine | crypto-paper.service | /home/ndvi/crypto-bot | nincs |

`loginctl show-user ndvi -p Linger` → yes; mindhárom unit enabled és active.
A user manager örökíti az ndvi identitást; User/Group csak a system unit mintákban
szerepel explicit. A felhasználói unitokban ezek külön megadása itt 216/GROUP hibát
okozott; javítás után a szolgáltatások rendben elindultak. Nem fut root Python app.
Restart=on-failure, RestartSec=15, journald logging. Scanner stop timeout=1800s,
web/paper=120s. Nem rebootoltunk.

A system-unit minták network-online függést és filesystem védelmet is tartalmaznak.
Rendszerszintű aktiválás sudo-jelszó hiányában nem történt. Az opcionális, elkészített
`sudo scripts/install-production-systemd.sh` először leállítja/letiltja a user unitokat,
majd telepíti és engedélyezi a system unitokat. Ne legyen két aktív scanner/engine.
A jelenleg működő user unitokhoz mindig `systemctl --user` / `journalctl --user` kell.

A tényleges scanner és paper process rendszeres perzisztált heartbeatet ír.
Scanner részleges run: 49/50, HYPEUSDT — Need at least 400 closed candles.
Ez DEGRADED figyelmeztetés, friss heartbeat és sikeres ciklus mellett, nem ERROR/STALE.
Paper engine RUNNING, trading OFF; web RUNNING; a négy belső komponens READY.
A health összesítés healthy=true, ERROR=0, STALE=0; app 0.2.0, branch és SHA látható.

LAN-on `/`, `/paper`, `/services`, `/health`, `/api/system/status`: **HTTP 200**.
A bindot `ss -ltnp` igazolta: **0.0.0.0:8000**. Port 8001 nincs productionben.
A domain mind az öt útvonalon **HTTP 401**, openresty Basic authentication.
TLS kapcsolat sikeres, de proxyhitelesítés nélkül az upstream tartalom nem ellenőrizhető.
Nginx Proxy Manager konfigurációját nem módosítottuk; hitelesített böngészőből
külön ellenőrizhető a domain mögötti dashboard.

## Validáció és rollback

A fejlesztési, majd frissített production venv-ben is **98 teszt sikeres**.
A tesztek izoláltak; nem resetelik az élő DB-t. A scanner élő `--once` próbaköre 49 új
snapshotot írt, score/history/override működik. System és user unitok
`systemd-analyze verify` ellenőrzése, shell syntax és git diff whitespace ellenőrzése sikeres.
Restart után is OFF és 1000 USDT marad; a végleges ellenőrzés a `final-status.json` fájlban.

Üzemeltetés: lásd README OPERATIONS. Rollback: állítsd le az új unitokat, mentsd az
aktuális DB-t, válts vissza `feature/market-scanner-v2` (`2d6ccfa`) branchre, állítsd vissza
a mentett `.env`-et, telepíts editable módban, indítsd a régi scannert/webet.
Az additív migráció miatt ép DB esetén a kód rollback megtarthatja a frissen gyűjtött
adatokat is. DB-visszaállítás csak indokolt esetben, minden DB writer leállítása és a
jelenlegi DB/WAL/SHM archiválása után a leállítás utáni mentésből; részletek a
mentési könyvtár `ROLLBACK.md` fájljában. A mentések nem kerülnek törlésre.
