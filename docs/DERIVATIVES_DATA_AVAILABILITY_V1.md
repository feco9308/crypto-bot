# Derivatives Historical Data Availability Audit v1

Base: `1d6094750cd0c0dc11534c5e82cb030da3ac7c3d`.
Branch: `research/derivatives-data-availability-v1`.

This is an availability audit. It does not calculate predictive features, outcomes,
strategies, rankings, entries, risk or portfolio results. There is no deployment,
replay, migration or production runtime change.

## Scope and reproduction

Inputs are the manifest-verified identity/eligibility columns of the completed
Cross-Sectional Market Structure Diagnostic:

| Cohort | Period, UTC | ALL Top20 | BUY eligible |
|---|---|---:|---:|
| 2024 EXPLORATION | 2024-01-01 to 2025-01-01 | 175,680 | 4,915 |
| 2025 EXPLORATION | 2025-02-01 to 2026-01-01 | 160,320 | 2,069 |

There are 394 distinct spot symbols across the two cohorts. No 2023 research
population or 2026 historical market records are requested. An archive directory
listing can contain today's names and filename metadata outside the cohort;
only 2024/2025 objects are eligible for this audit's data samples and coverage.
Outcome columns are not used.

Run from this isolated worktree with the existing project Python environment:

```bash
# Explicit initial bounded official HTTP capture:
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python -m research.derivatives_data_availability --root /home/ndvi/crypto-bot --capture

# Reproduce using captured evidence only; missing evidence fails, never downloads:
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python -m research.derivatives_data_availability --root /home/ndvi/crypto-bot
```

The default root output is `artifacts/derivatives-data-availability-v1/`. HTTP
capture uses only GET against allowlisted official sources. Futures requests are
restricted to named public market-data routes. No account/order endpoints, API
keys, trading permissions or authenticated clients are used. SQLite source access
is exclusively `mode=ro` plus `PRAGMA query_only=ON`. Relevant source tables and
the run-ID inventory are fingerprinted before/after. Production data and the
existing replay cache are never written.

## Official sources

- [Binance public archive specification](https://github.com/binance/binance-public-data)
- [Official archive browser](https://data.binance.vision/)
- [Official S3 directory metadata](https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=data/futures/um/)
- [USD-M market-data REST specification](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)

Exact request URLs, response status, capture timestamp, selected headers, body SHA
and bounded response bodies are retained in `evidence/*.json.gz`. The artifact
manifest hashes those evidence files too. A rebuild validates cached body hashes.
No third-party market dataset is used.

## Mapping and evidence levels

1. A spot identifier is an exact-name mapping candidate only when the official
   USD-M archive contains that identifier. Current exchangeInfo is not historical
   listing evidence. Dated delivery contracts are not silently substituted.
2. A monthly/daily filename proves an object is listed, not that a particular
   decision-time bar exists. All displayed object-presence coverage is an upper
   estimate, not strict observed row coverage.
3. Up to two small 1h boundary files per candidate instrument establish actual
   first/last observed timestamps in the audit period. Each file is <=80 KB and
   checksum verified. This does not download full symbol histories. A first
   observed timestamp is not automatically the legal listing instant, and a last
   observed timestamp alone does not prove delisting rather than a data gap.
4. Verified contiguous hourly sample spans provide a separate lower bound on
   actual 1h decision-row coverage. Between-file records remain unverified. The
   upper estimate is clipped by confirmed instrument boundary data when present;
   it never fills a pre-listing month to its start.
5. Rebrands and multiplier identifiers such as `PEPEUSDT`/`1000PEPEUSDT` are not
   guessed or stitched. Potential multiplier names found in official directory
   metadata are listed separately, unresolved, and excluded from strict mappings.
   This intentionally limits coverage; future identity normalization requires
   explicit official contract/underlying evidence and units.

The representative sample additionally covers BTC, ETH, FET, PEOPLE, FRONT, NOT,
WIF, BIO, PENGU and PNUT where historical objects exist. These include old markets,
a terminated name and later listings. Complete representative funding/price/
metrics ZIPs are capped at 1 MB each; 1m/5m kline checks use small daily files.
Large aggTrades/trades files are never bulk-read: at most their first 65,536 bytes
are requested. If Range is ignored, the full response body is not read.

## Families and semantics

**Traded futures klines:** monthly 1h/5m/1m inventory. Samples inspect OHLC, volume,
quote volume, trade count, taker-buy base/quote fields and numeric consistency.
A later taker-flow study could use those official raw columns, but no such
calculation is performed here. Decision-time rule: `close_time < decision`.

**aggTrades/trades:** monthly inventory, plus daily range controls. Buyer-maker
flags indicate aggressor side: buyer-maker true is aggressive sell; false is
aggressive buy. Duplicate timestamps can be valid simultaneous trades; duplicate
IDs and missing IDs are reported separately. Trade inactivity is not a missing
fixed interval. Prefix-only samples do not validate whole-ZIP SHA, last timestamp
or full-day continuity. They are never presented as full-file validation. The
last trade used at a later decision must satisfy `trade_time < decision`.

**Funding:** official monthly `fundingRate` archive and bounded historical public
REST controls for both years. Funding event time is UTC milliseconds in the
inspected format; measured cadence is exported, not universally assumed to be
8h. Missing pre-listing events are not zero funding. Event-time boundary:
`fundingTime <= decision`; a future scheduled rate is not an observed event.

**Mark/index/premium:** official roots are `markPriceKlines`, `indexPriceKlines`
and `premiumIndexKlines`; the inspected root is not named `premiumPriceKlines`.
Both 1h and 5m are inventoried. These are price-reference series, not real traded
volume series. A later basis/premium study must align closed bars strictly before
the decision. No basis or predictive feature is constructed here.

**Open interest and long/short:** REST retention is only the latest month for OI
statistics and latest 30 days for the ratio endpoints. Current `openInterest`
is a current snapshot, not historical evidence. Those REST routes alone cannot
supply the target years. However, the official **daily metrics archive exists**
and its samples contain `sum_open_interest`, `sum_open_interest_value`, global
account ratio, top-trader account ratio and top-trader position ratio. Historical
archive availability and recent-only REST are distinct facts.

**Taker metric:** recent-only REST is separated from the archived
`sum_taker_long_short_vol_ratio` and from reconstructable raw kline/trade columns.
The archived ratio alone does not supply absolute aggressive buy/sell quantities
or establish identical interval semantics to the REST response.

**Liquidations:** absence is evaluated against the enumerated official USD-M
daily/monthly roots. An authenticated account's force-order history is not a
public historical market-liquidation archive. No third-party replacement is used.
The absence finding is limited to the inspected official sources.

## No-lookahead and quality limitations

UTC timestamps are parsed explicitly (seconds/milliseconds/microseconds detected
from magnitude; timezone offsets converted). Sample records must match the named
archive date/month. The audit reports duplicates, ordering, missing regular bars,
funding cadence and schema columns. Missing archives/data are UNKNOWN observations,
never zero flow, zero funding or zero OI.

For metrics, `create_time` labels a snapshot but does not prove its original
publication time or the taker ratio's interval convention. Decision-time usability
therefore remains UNKNOWN without a pinned publication lag/interval specification.
Even a prior-snapshot convention needs justification; exact timestamp matching
alone is insufficient. This is an explicit readiness blocker, not a solved gate.

Binance documents archive revisions. URL and ETag are not immutable data versions;
full ZIP SHA256/.CHECKSUM and captured bytes are required. A present-day archive
can be reproducible while its original point-in-time publication vintage remains
unproven. `LastModified` is archive-upload metadata, not a market event timestamp.

## Coverage, classification and storage

`symbol-coverage.csv` reports year/symbol/family mapping, object counts, row
upper estimates, sample-verified lower rows, and actual sample first/last times.
`data-family-matrix.csv` adds ALL/BUY/symbol-day estimates and classifications.

Historical archive families remain `PARTIAL_OR_COVERAGE_LIMITED` until strict
per-row gap/mapping/publication validation is complete. REST recent-only status is
recorded separately; it does not make an existing historical archive unavailable.
No A readiness claim is made merely from filenames.

Storage is the sum of listed compressed objects for the selected exact-mapped
symbols: whole 2024/2025 monthly objects for monthly families, and only months
intersecting the cohort for daily metrics. It is not the size of the full exchange.
Different granularities overlap; OI/ratio/taker metrics share the same metrics
files and must not be counted three times. Uncompressed size is not extrapolated
from tiny nonrandom samples. No full historical dataset is downloaded.

At most one `next_research_candidate` is selected on data availability alone:
`FUTURES_TAKER_FLOW` only if both years' kline ALL/BUY presence estimates exceed
70% and required raw fields are verified; otherwise NONE. This descriptive
availability criterion does not optimize a trading parameter, establish predictive
edge, promote a strategy or approve immediate feature use. Strict mapping/gaps
remain prerequisites. OI/positioning additionally have unresolved publication-time
semantics.

## Export and tests

The ZIP includes the report, summary, methodology, matrix, symbol coverage, sample
validation, layout, retention, storage, source validation, archive object inventory,
REST controls, raw bounded HTTP evidence and a file-SHA manifest. ZIP member order,
timestamps and permissions are fixed. Captured timestamps are immutable input
provenance, not regenerated export timestamps.

Targeted tests exercise exact mapping, pre-data coverage rejection, official URL
names, pagination guards, checksums, UTC units/boundaries, recent-only retention,
unknown values, no trading endpoints/replay creation/DB writes, bounded Range and
boundary samples, cached-evidence integrity, deterministic report and ZIP. Full
maintained tests, Ruff and `git diff --check` are required before push.

## Completed audit evidence

394 cohort symbols; 345 exact archive-directory candidates. Of those, 342 have
2024/2025 1h price records verified in bounded samples. The other mappings remain
unresolved or have no relevant-period files. Multiplier candidates are listed in
`archive-layout.json`, not included by assumption.

Archive-presence upper estimates, within the exact-ID plan and confirmed
instrument bounds (not measured full candle/snapshot coverage):

| Family | 2024 ALL / BUY % | 2025 ALL / BUY % | Classification |
|---|---:|---:|---|
| Traded klines 1h/5m/1m | 87.27 / 85.33 | 93.59 / 95.99 | B |
| aggTrades | 86.87 / 84.78 | 93.45 / 95.99 | B |
| trades | 86.87 / 84.78 | 93.45 / 95.99 | B |
| Funding | 87.27 / 85.33 | 93.57 / 95.99 | B |
| Mark 1h | 87.27 / 85.33 | 93.59 / 95.99 | B |
| Index 1h | 87.35 / 85.41 | 93.74 / 95.99 | B |
| Premium 1h | 86.94 / 84.78 | 93.53 / 95.99 | B |
| Metrics: OI/positioning/taker ratio | 86.91 / 84.78 | 93.49 / 95.99 | B |
| Liquidation archive in inspected USD-M roots | Not found | Not found | C |

B = PARTIAL_OR_COVERAGE_LIMITED, C = NOT_AVAILABLE_FOR_2024_2025 in the inspected
sources. REST OI/ratio/taker routes separately retain their recent-only/D status.
These percentages are upper estimates for the audited exact-name file plan,
not an upper bound on every possible future mapping/normalization solution.
Alias exclusion can understate what would become available with verified mapping.

The bounded 1h files directly cover 10.7565% / 8.7619% of ALL rows and
11.6582% / 0% of BUY rows in 2024/2025. In particular, **0% sampled 2025 BUY
coverage is not missing whole-year data**: first/last boundary samples do not
intersect those eligible rows. This demonstrates why the much higher filename
presence must not be called measured row coverage. Strict per-row coverage
remains UNKNOWN until ingestion/gap validation.

There are 808 checksum-verified regular complete samples and 16 bounded trade
prefix samples. Additional same-event funding alignment controls are in their
own file, not counted in the regular sample-validation subset. Matrix earliest/
latest timestamps refer to that regular sample subset; REST and alignment controls
provide additional timestamps in separate evidence files.

For BTC/ETH/FET/PEOPLE, all eight year/symbol funding REST controls return actual
historical events. 28 same-event comparisons against checksum-verified June
archives match exact timestamps. Twelve events are 1–2ms after the nominal hour;
the archive preserves those offsets. Use the original timestamp, not a rounded
hour: an event at t+1ms is not available at t. These checks do not prove original
network publication latency or immutable first-published archive vintage.

FRONT's last confirmed traded 1h archive row is 2024-09-11 09:00 UTC, while the
funding REST returns three June-2025 FRONT records with a fixed .45 mark price in
this control. `cross-family-anomalies.json` preserves the discrepancy. No API bug,
active market, or fillable price is inferred from it; funding events do not extend
instrument coverage beyond the confirmed price archive.

Compressed listed-file storage plan, both years combined:

- Traded klines: 1h 192.86 MB; 5m 1.98 GB; 1m 8.91 GB.
- aggTrades 355.50 GB; trades 727.02 GB.
- Funding 6.46 MB; mark 1h 102.17 MB; index 1h 109.05 MB;
  premium 1h 88.41 MB.
- Selected metrics months 516.31 MB, shared by OI/ratios/taker fields.

Decimal units; selected exact-mapped file plan, not exchange-wide size. These are
metadata sums, **not downloads**. Full sample ZIP payload is approximately 20.5 MB;
no large full aggTrades/trades file was read. Metadata/HTTP evidence is separate.

`next_research_candidate = FUTURES_TAKER_FLOW`, based solely on official raw
columns, substantial two-year archive presence, reproducible checksums, explicit
closed-candle timing and much lower storage cost via klines than full trade data.
It is conditional on strict mapping/row/gap validation; no predictive edge is
claimed and no feature or strategy is implemented.

Validation: **51 targeted tests PASS; 932 full maintained tests PASS** in 233.81s.
Maintained Ruff and `git diff --check` PASS. Before/after source fingerprints and
all source CSV hashes match; no production database write or replay creation.
Independent export QA checks 7,033 symbol/year/family records, all evidence body
hashes, population totals and coverage bounds. It also validates 465,376 sampled
candle open/close time pairs, with zero invalid interval bounds. ZIP CRC/member
SHA and deterministic repeated packaging are checked separately.

Capture budgets apply to new transfers per explicit capture invocation; saved
responses are reused when continuing the audit. The final used HTTP evidence is
128,378,519 response-body bytes, predominantly directory XML, across 7,422 distinct
request records. Complete archive sample ZIP payload totals about 20.5 MB; large
trade files contributed bounded prefixes only. This is not a full history ingest.
