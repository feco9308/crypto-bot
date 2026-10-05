# Paper Trading Core validation — v0.2.0

Baseline feature/market-scanner-v2: 2d6ccfa. Development worktree: /home/ndvi/crypto-bot-paper, branch feature/trading-core-paper. Date: 2026-10-05. Python 3.12.3.

## Isolation and compatibility

Original scanner PID 12108 and dashboard PID 13425 remained running from /home/ndvi/crypto-bot. Neither was signalled/restarted. Original worktree remained clean on feature/market-scanner-v2. No migration, paper write or config edit was applied to original data/market.db. A read-only SQLite backup captured 4067 snapshots into the isolated worktree's ignored preview database; only that copy was migrated.

`git diff feature/market-scanner-v2 -- crypto_bot/scanner crypto_bot/indicators crypto_bot/data crypto_bot/storage/database.py crypto_bot/storage/models.py crypto_bot/strategies` is empty. Scores, filters, selection and storage initializer remain unchanged. Future CLI startup adds observational heartbeats; existing running collector has not loaded them. Original scanner initialization accepts migrated scanner schema_version=1.

## Tests and build checks

98 deterministic pytest cases passed: all 49 original tests plus 49 new cases. New coverage includes valid/invalid signals, configurable reference strategy, risk sizing/fees/limits, historical drawdown latch, cash/reservations, exact Decimal persistence at million-unit balances, buy/sell and order lifecycle, costs/PnL, stop/TP/strategy/manual exits while OFF, replay/duplicate protection, daily rollover, lease/reset, migration/reopen, concurrent cycles, transaction rollback, scanner continuation after migration, heartbeat/stale/degraded/legacy inference, event retention, sanitized DB errors, health and CSRF-protected web controls.

Tests never access live Binance. Only read-only backup inspected actual scanner data. Ruff, compileall, JavaScript syntax, bash syntax, pip check and git diff --check passed. Wheel contains seven templates, two static assets and both Alembic revisions/environment. systemd-analyze verify passed on sample copies adjusted to this worktree; no original systemd services were installed/restarted.

## Browser and process checks

Headless Chromium, two Gunicorn workers, separate synthetic test database:

- Original scanner table preserved; Paper ON creates no live call.
- One fixed-price paper BUY filled; OFF still allowed MANUAL close.
- Signal/score/risk/order/fill/position audit page rendered.
- Service/component distinction, version and status rendered.
- 390px mobile viewport without document overflow; no JS page errors.

Isolated preview: 0.0.0.0:8001, cloned scanner history, paper trading OFF. Paper engine and web started with background.sh, verified PPID=1 and SID=own PID after launcher exit. This is a copy of history, not a continuously mirrored collector; freshness warnings are expected as the copy ages. APP_PREVIEW banner explicitly identifies this. Runtime .env, databases, PID files and logs ignored; no secrets committed.

## Remaining limits

No live execution adapter or trading credentials. Long-only, one quote/account, no pyramiding, partial fills or partial exits. Application stop observes persisted closed-candle prices and cannot see intrabar lows; stale/gap fills are refused or realized at the observed price, and losses may exceed estimated stop risk. Drawdown uses observed marks. UTC daily boundaries anchor previous marked equity. Price/history/portfolio retention and PostgreSQL runtime remain future work. SQLite exact ledger values stored as TEXT; future SQL analytics must account for their decimal type, not lexical sorting. One engine lease per account; crash recovery waits lease expiry. No login; operator access via reverse proxy controls. Web worker heartbeats aggregate; exact OS restart count unavailable. Original collector heartbeat/version remain unknown until planned next-day adoption.
