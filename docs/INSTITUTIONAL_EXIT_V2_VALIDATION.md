# Institutional Exit v2 validation

Base production commit: `6a17ea4b6b079c9844669d6c31e8afcd841839ac`.
Development branch: `feature/institutional-exit-research-v2`, isolated worktree
`/home/ndvi/crypto-bot-exit-v2`. Production checkout remains on the base commit.
No deployment, service restart, production DB write/migration/reset, or new
historical replay was performed.

## Checks

- Full maintained suite: **611 passed in 104.58 seconds** (includes browser tests).
- New research coverage: **27 tests**, including desktop/mobile import/export.
- `ruff check crypto_bot tests research`: PASS.
- `git diff --check`: PASS.
- `ruff check .`: four pre-existing I001 errors in `backtest.py`,
  `legacy/backtest.py`, `legacy/dashboard.py`, `legacy/trading_bot.py`. Unchanged.
- Baseline and old adaptive trade/cash/order/fill fixtures match the unchanged
  production code exactly. Both controls have actual filled and closed trades.
- All five variants generate actual positions in a synthetic end-to-end replay.
- Shared backend validates the prepared five-variant config and its export/import
  round-trip. Import/export triggers no run creation on desktop or mobile.
- Entry AND risk parameter dictionaries equal the original institutional C
  config exactly for all five variants.
- No differences in production trading, RiskManager, scanner, indicators,
  ReferenceStrategy, institutional entry predicate, existing exit implementations,
  storage models or database migrations.
- No external historical data was used for tests or parameter selection.
  Existing synthetic test fixtures retain their existing calendar labels; the
  new five-variant end-to-end fixture uses a simulated 2024 interval.

Config SHA256 (existing backend config_hash of validated immutable snapshot):
`21b77f8a530516c5b36146a20731bc2e73c9ad3543a623039e3e8d511651f560`.

## Files

New policy/state module: `crypto_bot/replay/exit_v2.py`.

Replay integration changes: `config.py`, `engine.py`, `research.py`,
`institutional_metrics.py`, `exporter.py`, `rejections.py`, `models.py`
(all under `crypto_bot/replay`). `models.py` only expands the existing audit
reporting query; it does not change the schema.

Replay builder: `crypto_bot/web/static/replay.js`,
`crypto_bot/web/templates/replay.html`.

New tests/fixtures: `tests/test_exit_v2.py`, `tests/test_exit_v2_browser.py`,
`tests/exit_v2_control_fixture.py`,
`tests/fixtures/exit-v2-controls-6a17ea4.json`.

Documentation/config: `README.md`, `docs/INSTITUTIONAL_EXIT_RESEARCH_V2.md`,
this validation report and
`docs/replay/institutional-exit-v2-2024.config.json`.

## Limits

No annual v2 results are claimed. The ready config is the deliverable for a
later explicitly authorized deployment/start. Market gaps can lose money even
with a net break-even stop; fills are never fabricated at the stop. New exits
preserve strategy SELL, whereas the old adaptive control retains its original
stop-only behavior. This distinction is included in B/D interpretation.

Stale handling and original OHLC ambiguity rules remain unchanged. Different
portfolio runs can have different entries: large-winner diagnostics are
unmatched-portfolio proxies, not matched-entry causal evidence. No winner is
selected or promoted. NO LIVE TRADING.
