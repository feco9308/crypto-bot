# First milestone validation

Date: 2026-10-05. GitHub baseline: de95639. Branch: feature/market-scanner-v2. Python 3.12.3 on the supplied Linux workspace.

## Automated checks

- 49 pytest cases passed, including all five timeframes. Offline tests use FakeProvider / mocked HTTP responses, never live Binance.
- Fresh separate virtualenv: installed requirements-dev.txt and editable project; all 49 tests passed there too; pip check reported no broken requirements.
- Ruff checks for import organization, undefined names and standard Python errors passed; new Python files formatted consistently.
- Python compileall, JavaScript syntax check and git diff --check passed.
- Built distribution wheel and confirmed all four templates and both static files are packaged.
- systemd-analyze verify passed on copies with executable/working paths adjusted to this checkout. The original /opt/crypto-bot sample cannot be fully verified before installation there. No services were installed or enabled on this machine.

## Public market data smoke test

Unauthenticated GET-only scan against Binance: 3720 discovered instruments, 71 passed automatic filters, TOP 50 selected, 49 successfully scored and persisted. HYPEUSDT had fewer than the required 400 closed warmup candles; run marked partial and remaining instruments continued. Five algorithm watch decisions created. No order endpoint, credentials or trading calls used. Smoke database stored under /tmp, outside the repository.

## Browser check

Two Gunicorn workers with a shared local test session key. Headless Chromium/Playwright validated:

- 49 dashboard rows, symbol search and descending score sort.
- PINNED form submit, appearance in highlighted section and persistence after reload.
- Instrument detail page, score/price canvas rendering, no JavaScript page errors.
- 390px mobile viewport without page overflow; wide data table scrolls within its container.

Original port 6000 was blocked by Chromium as unsafe; changed new defaults/deployment/docs to 8000. A hidden form label positioning bug caused page overflow and was fixed by anchoring labels in the table container. Temporary browser dependencies, screenshots and smoke data are not committed.

## Remaining scope limits

No broker/order execution, paper trades, risk engine, ML or profit optimization. PostgreSQL deployment not exercised. Only schema v1 initialization exists; future schema changes need explicit migrations. No data retention, backup tooling, per-user authentication, distributed scanner lock or full historical candle/spread archive. Run one scanner per database; use local/SSH access or an authenticated TLS proxy. Interrupted running rows are retained for diagnosis. Missing/incompatible score history yields null deltas. Stable/leveraged classifications are maintained configurable lists/heuristics. IGNORE filters the effective auto watchlist without rewriting original algorithm decisions, so the effective list can have fewer than N entries.
