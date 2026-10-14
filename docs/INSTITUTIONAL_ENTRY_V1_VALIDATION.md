# Institutional Entry Research v1 — implementation validation

Research only. NO LIVE TRADING. No deployment or annual replay was performed.

## Recovery

The interrupted implementation was recovered in
`/home/ndvi/crypto-bot-entry-v1`, branch
`feature/institutional-entry-research-v1`, based on
`be08107d11728a3e90c8ec3dbe351616074c5bf7`.
Six tracked files, the validation configuration and three test/fixture files
were already present as uncommitted changes. They were preserved.
The prior targeted log recorded 59 passed tests; the prior full-suite log had
no completion result and was not accepted as a successful full run.
Completion consisted of reviewing the preserved implementation, revalidating
the configuration, completing browser/full-suite verification and documenting
the result. No thresholds were changed.

## Frozen hypotheses and execution boundary

| Variant | Strategy / exit | Entry filter |
| --- | --- | --- |
| A | institutional_style_v1 / baseline_v1 | OFF |
| F | institutional_style_v1 / baseline_v1 | delta_4h >= 5.0 |
| G | institutional_style_v1 / baseline_v1 | 1.0 <= atr_pct <= 3.0 |
| H | institutional_style_v1 / baseline_v1 | range_position <= 0.50 |

The existing replay `entry_filter` schema adds `min_delta_4h`, `min_atr_pct`,
`max_atr_pct`, `max_range_position`. New maxima are inclusive; legacy score
and delta maxima retain their exclusive semantics. There is no combined
F/G/H study variant. Missing/null new settings are neutral and omitted from
canonical configurations, preserving legacy snapshots and hashes.

Only a strategy BUY reaching the existing replay gate is checked, before risk
and execution. SELL and exits bypass it. Values come from the current closed
scanner row: momentum.delta_4h, features.atr_pct and features.range_position.
No feature definition, strategy decision, shared MarketFrame, score, watchlist,
portfolio or risk state is modified. A constrained missing/nonfinite feature
fails closed; unconstrained features do not reject a BUY.

The requested four reason codes are preserved verbatim. Missing constrained
values use ENTRY_DELTA_4H_UNAVAILABLE, ENTRY_ATR_PCT_UNAVAILABLE or
ENTRY_RANGE_POSITION_UNAVAILABLE. Block and pass audits include the symbol,
UTC timestamp, score, all three input features, thresholds and original BUY.
Only active new filters add pass audits. Persisted audit extraction includes
these events, so checkpoints do not lose diagnostic counts.

`entry_filter_diagnostics` reports attempted BUYs, passes, blocks, block rate
percent and per-reason counts. Its scope is BUYs reaching this gate after
existing upstream BTC/rearm gates, before risk/execution; a filter pass does
not imply an execution. This study has the outer BTC gate and rearm OFF.
Existing replay metrics and comparison machinery remain unchanged; no winner
is selected and no strategy recommendation is produced.

## Configuration provenance

`docs/replay/institutional-entry-v1-2025-validation.config.json` is the public
import schema. The backend derives ISOLATED_TOPN_V1; this internal field is
not manually inserted into the public JSON.

- Period: 2025-02-01T00:00:00Z to 2026-01-01T00:00:00Z.
- Dataset: VALIDATION, historical Top20, RESEARCH_QUARANTINE_STALE.
- Conservative intrabar, fallback_5m=false.
- Initial balance 1000; risk 0.5%; max positions 3; exposure 50%/20%; daily
  loss and drawdown guards 100%; fee 0.1%; slippage 0.05%; no pyramiding.
- All other entry/risk/exit settings equal completed isolated 2024 run
  `d3ba304b99cb4d8c918cbdbf6e884afb`, variant A.
- Validated config hash:
  `4ee7e7d346400230a03263dbe70cff0000132ec4eb08257b479af79380d317db`.

Provenance was checked through SQLite mode=ro with query_only=ON and SELECT
only, without constructing a production ReplayStore. January 2025 is excluded
from trading; earlier closed data may still provide historical indicator warmup.
No 2025 outcomes were used for tuning. The original 4824-hour institutional
history requirement and strict timestamp availability remain unchanged.

## Verification scope

The independent `tests/fixtures/institutional-entry-control-be08107.json`
was captured from unchanged be08107 code before implementation. It uses the
actual institutional entry configuration, deterministic synthetic tapes and a
temporary replay database. It contains a nonempty control (one position, two
fills), not an annual rerun or an empty-ledger parity assertion.

New tests check exact trades/orders/fills/timestamps/curve/cash/equity against
that fixture with default, null and missing filters; A with F/G/H companions
in both orders; and checkpoint/resume core plus full summaries for all four.
All eight specified boundary cases, BUY-only behavior, frame immutability,
future-row isolation, stale quarantine state preservation, unavailable fields,
schema errors, diagnostic persistence and zero-opportunity reporting are covered.
Desktop 1440px and mobile 390px browser tests import/export the real artifact,
check all fields, no horizontal overflow and no implicit run creation.

Validation logs and the read-only config provenance report are stored locally
under `artifacts/institutional-entry-v1/` (ignored runtime artifacts).

Recovery verification: targeted suite 61 passed (34 new core cases, two new
browser cases, 25 existing entry-filter cases); complete maintained suite
669 passed in 161.20 seconds. Both viewport cases were rechecked after the
final UI explanation update: two passed. Maintained Ruff and `git diff --check`
passed. Browser tests require the existing local Chromium library directory
in LD_LIBRARY_PATH; the first recovery full-suite attempt omitted it and was
stopped after libasound.so.2 launch failures, then rerun successfully with it.

## Deployment scope

No migration is needed: existing replay audit JSON stores the added metadata.
No production strategy, ReferenceStrategy, scanner/score, RiskManager,
PortfolioService, paper execution or exit policy file was changed. No service
restart is required for this review-only delivery; deployment is not authorized.

Maintained lint command is `ruff check crypto_bot tests research`. Global
`ruff check .` additionally reports four pre-existing I001 errors in
backtest.py and three legacy files; those unrelated files are unchanged.
