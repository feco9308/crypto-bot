# Derivatives Metrics Timestamp Semantics Audit v1

Base: `d09247b99e57e165c5cebb246a5e10a2c73dcd63`.
Branch: `research/derivatives-metrics-semantics-v1`.

This is a data-quality and timestamp-contract audit. It does not use predictive
outcomes, score/rank candidates, construct an entry filter, fit a model, run a
replay, change a strategy/risk parameter or modify deployed runtime code.

## Three clocks, kept separate

1. The timestamp or period label attached to a market measurement.
2. The time a public REST response actually made that measurement observable
   to this client.
3. The publication / Last-Modified time of a daily ZIP object.

None of these is silently substituted for another. In particular, a ZIP's
Last-Modified header does not establish when an underlying observation was
known during live trading. A row labeled 12:05 is not assumed to have been
published at 12:05. A first client receipt is only an observation bound for that
response, not a historical provider-wide publication SLA.

## Official evidence scope

Only `data.binance.vision` archives, official Binance REST documentation and
unauthenticated public Binance market-data GETs are captured. Account/order
paths, API key/signature parameters, redirects and nonofficial hosts are rejected.
The HTTP session does not load credentials or proxy authentication from the
user environment. No authenticated request or trading key is used.

Current documentation snapshot:

https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data

The current schema calls OI, global and top-trader ratio timestamps period-end,
and the taker timestamp period-start. This does not define the archive
`create_time` field or prove that the current documentation describes the
2024/2025 archive contract. Historical REST controls for June 2024 and June 2025
are retained alongside the current documented recent-only retention limit.

Community issues, external datasets and third-party APIs are not evidence in
this audit. No conclusion about an archive contract is inferred from five-minute
spacing or physical row order.

## Archive schema and quality policy

The expected CSV columns are:

```text
create_time
symbol
sum_open_interest
sum_open_interest_value
count_toptrader_long_short_ratio
sum_toptrader_long_short_ratio
count_long_short_ratio
sum_taker_long_short_vol_ratio
```

Original string values and original row order are retained. Duplicate labels,
order inversions, nonaligned labels, missing labels, schema variants, missing
values, nonfinite numbers and negatives are audited. Missing is not zero.
OI/OI value must be nonnegative. Long/short ratios are unbounded ratios;
values above one are valid and must not be mistaken for invalid fractions.

Naive `create_time` strings are interpreted as UTC for the explicit comparison.
An explicit UTC offset is respected. Integer datetime arithmetic preserves every
millisecond; unsupported submillisecond labels are rejected rather than rounded
onto a five-minute boundary. This parsing convention is not a publication-time
claim or evidence of a historical archive contract.

Every complete ZIP is verified against its official SHA256 checksum. Response
bytes, URL, HTTP status, Date, Last-Modified, SHA256, local request/receipt times
and monotonic request duration are pinned. The default execution mode is offline;
only explicit `--capture` may fetch missing objects. Rate-limit responses stop
capture, without a retry loop. Complete cached evidence is reused and verified.

## Fresh overlap and boundary observations

The frozen current overlap uses BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT and XRPUSDT on
2026-10-30 and 2026-10-31. There are 2,880 archive rows. The newer daily ZIP was
not available at the time of collection; that 404 is retained, not refreshed to
create a different experiment after the fact.

For each of six archive fields the audit compares public REST values at the
original label minus five minutes, the same label, and plus five minutes. These
are fixed alignment hypotheses, not selected publication lags. Decimal equality
is exact numeric equality; a separate rounding-compatible classification uses
the coarser literal precision and ROUND_HALF_UP. Compatibility is not proof of
which rounding rule the publisher used. All raw values and timestamp differences
remain available for inspection.

The 12:00 / 12:05 / 12:10 noon triplets are exported explicitly. A short prospective
BTC boundary observation at 2026-11-02 06:15 UTC retains queries scheduled just
before and after the boundary, with authoritative actual request/receipt times.
Different endpoint observation times are not collapsed into one availability
instant. Completed observation windows are replayed from captured evidence and
are never silently re-polled during an offline audit run.

Observed OI and OI value match all 2,880 contemporary rows at REST timestamp
`create_time + 5 minutes`. This association is not automatically projected onto
2024/2025 and is not a proved five-minute publication lag. The account ratios
have additional value differences; no inversion or other undocumented transform
is fitted to make them match.

## Exact historical coverage without outcome reads

The canonical identity populations are preserved:

* 2024: 175,680 ALL / 4,915 BUY identities.
* 2025: 160,320 ALL / 2,069 BUY identities; this existing source starts February 1.

A byte-level CSV column projection buffers and decodes only `symbol`,
`timestamp`, `current_buy_eligible`. In skipped fields it only recognizes CSV
quote/delimiter bytes; no MFE, MAE, return, +5R, stop or PnL value is decoded or
materialized. A regression fixture with invalid UTF-8 and quoted newlines inside
an outcome field proves that the skipped payload is not decoded. Whole-file
hashing is opaque source-integrity verification, not outcome analysis.

Only exact symbol identity is accepted. No PEPE/BONK multiplier, alias, rebrand or
contract stitching is performed. Every identity requires the actual immediate
previous completed traded futures 1h bar from the existing checksum-pinned cache.
A later metrics label can never extend the usable instrument history. The guard
is tested with a FRONT-like stale-history case.

Historical daily metrics files are parsed for every required active identity.
Both the filename day and its preceding day are requested, so a boundary label
is not lost by assuming a filename is a market timestamp. The previous day's
file at the 2024 boundary is source context, not a 2023 research cohort.

Coverage columns explicitly distinguish:

* exact label present under the active-market guard;
* valid, unambiguous numeric field value under that guard;
* safe decision-time feature coverage, which remains UNKNOWN until the archive
  timestamp and publication contracts are proven.

The resulting percentage is a **raw active-label coverage**, not permission to
use the metric at that decision. Required-history cache endpoints are not legal
listing/delisting dates. Listing/delisting boundary output states that scope
explicitly, and legal dates stay UNKNOWN when not established by official evidence.

## Field readiness and candidate gate

Each of OI, OI value, global account ratio, top account ratio, top position ratio
and taker ratio is assessed separately. Taker ambiguity does not automatically
block an independently proven OI field. UNKNOWN semantics cannot become READY,
even with perfect contemporaneous equality. A lag only qualifies when both its
contract and publication bound are independently proven; an arbitrary lag is
never supplied as a substitute for evidence.

A next research candidate additionally requires explicit quality and coverage
approval. No predictive outcome is used in that decision. At most one candidate
can be reported, and reporting it never starts predictive research.

The REST taker `buyVol/sellVol` reconstruction is audited separately. An archive
column's numerator, denominator and covered interval are not inferred from its
name or from matches on a subset of rows. If unproved, its status is
`ARCHIVE_TAKER_RATIO_SEMANTICS_NOT_PROVEN`.

## Run and reproduce

Use the isolated worktree; the shared interpreter is not a deployment:

```bash
cd /home/ndvi/crypto-bot-metrics-semantics
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python \
  -m research.derivatives_metrics_semantics --root /home/ndvi/crypto-bot --capture --observe-boundary
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python \
  -m research.metrics_identity_coverage --root /home/ndvi/crypto-bot --capture

# Offline rechecks reuse validated cache objects; no network access is possible.
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python \
  -m research.derivatives_metrics_semantics --root /home/ndvi/crypto-bot
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python \
  -m research.metrics_identity_coverage --root /home/ndvi/crypto-bot
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python \
  -m research.metrics_semantics_finalize --root /home/ndvi/crypto-bot
```

Required coverage uses 18,115 daily files, with six bounded download threads.
The full raw daily cache stays outside Git and outside the ZIP. Sample evidence,
archive pins, coverage, quality/revision checks and the report are bundled.
Revision checks compare overlapping previously checksum-pinned official samples;
no difference there does not prove that archives were never revised elsewhere.

The production database was only inspected read-only for schema/event-name
metadata while finding identity sources (`mode=ro`, `PRAGMA query_only=ON`). The
collectors do not open a database. No production DB write, service restart,
strategy change, replay or deployment is performed. The deployed candidate-order
parity fix remains untouched.

## Completed audit handoff

The existing historical process completed all 18,115 requested daily files;
17,998 archives passed official checksum validation and 117 were missing.
All 17,998 cached archive payloads were independently rehashed after completion.
No second historical coverage process was started during recovery.

The parsed archive population contains 5,168,662 rows, 54,376 order inversions,
87 off-grid timestamps, 202 gaps, zero within-file duplicate labels and 3,424
missing metric cells. The eight files containing off-grid labels were checked
again using integer-millisecond timestamp parsing; existing coverage is unchanged.
Six overlapping prior checksum pins have unchanged bytes; that limited check
does not establish a complete archive revision history.

Final classification: `METRICS_SEMANTICS_AMBIGUOUS`.
All six fields: `SEMANTICS_AMBIGUOUS`.
Next research candidate: `NONE`.
Historical `create_time` semantics and publication availability remain `UNKNOWN`.
The raw OI exact-active-label coverage is 86.7469% / 84.6185% for 2024 ALL / BUY
and 93.4344% / 95.9401% for 2025 ALL / BUY. These are not proven safe
decision-time feature coverage percentages.

Validation: 39 targeted tests and 880 full maintained tests passed;
`ruff check crypto_bot tests research` passed. Artifacts are at
`/home/ndvi/crypto-bot/artifacts/derivatives-metrics-semantics-v1/`, including
`report.md`, `summary.json`, the complete file manifest and
`derivatives-metrics-semantics-v1.zip`. Raw daily cache objects remain outside
Git and the ZIP. Predictive outcome analysis, new replay, production behavior
changes, production DB writes and deployment: none.
