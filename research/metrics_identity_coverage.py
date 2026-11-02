"""Exact identity-only metrics coverage; no outcome fields are materialized."""

import argparse
import csv
import hashlib
import io
import json
import threading
import zipfile
from array import array
from bisect import bisect_left
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from research import derivatives_metrics_semantics as audit

IDENTITY_COLUMNS = ("symbol", "timestamp", "current_buy_eligible")
HOUR = 3600000


def project_identity(path):
    """RFC4180 projection: skipped field payloads are never buffered or decoded.

    The parser only inspects quote/delimiter bytes in nonidentity fields, like a
    column projection. Outcome values never become Python strings/numbers.
    """
    with path.open("rb") as stream:
        header = next(csv.reader([stream.readline().decode("utf-8")]))
        wanted = {header.index(name): name for name in IDENTITY_COLUMNS}
        col = 0
        quoted = False
        after_quote = False
        value = bytearray()
        row = {}

        def finish():
            nonlocal value
            if col in wanted:
                row[wanted[col]] = value.decode("utf-8")
            value = bytearray()

        for chunk in iter(lambda: stream.read(65536), b""):
            for c in chunk:
                if c == 34:
                    if quoted:
                        quoted = False
                        after_quote = True
                    elif after_quote:
                        if col in wanted:
                            value.append(c)
                        quoted = True
                        after_quote = False
                    else:
                        quoted = True
                elif not quoted and c == 44:
                    finish()
                    col += 1
                    after_quote = False
                elif not quoted and c in (10, 13):
                    if c == 13:
                        continue
                    finish()
                    if row:
                        if set(row) != set(IDENTITY_COLUMNS):
                            raise ValueError("IDENTITY_SCHEMA")
                        yield row
                    col = 0
                    row = {}
                    after_quote = False
                elif col in wanted:
                    value.append(c)
        if quoted:
            raise ValueError("UNTERMINATED_CSV_QUOTE")
        if row or value:
            finish()
            yield row


def identities(root):
    source = root / "artifacts/cross-sectional-market-structure-v1"
    manifest = json.loads((source / "manifest.json").read_text())
    rows = []
    proof = {}
    for year, expected in ((2024, (175680, 4915)), (2025, (160320, 2069))):
        name = f"market-structure-dataset-{year}.csv"
        path = source / name
        digest = hashlib.file_digest(path.open("rb"), "sha256").hexdigest()
        if digest != manifest["files"][name]["sha256"]:
            raise ValueError("IDENTITY_SOURCE_MANIFEST_CHANGED")
        proof[str(path.relative_to(root))] = digest
        n = b = 0
        seen = set()
        for r in project_identity(path):
            symbol, t = r["symbol"], audit.stamp(r["timestamp"])
            if (symbol, t) in seen:
                raise ValueError("DUPLICATE_IDENTITY")
            seen.add((symbol, t))
            if r["current_buy_eligible"] not in ("0.0", "1.0"):
                raise ValueError("INVALID_BUY_IDENTITY")
            buy = r["current_buy_eligible"] == "1.0"
            n += 1
            b += buy
            rows.append((year, symbol, t, buy))
        if (n, b) != expected:
            raise ValueError("IDENTITY_COUNT_CHANGED")
    return rows, proof


def traded_history(root):
    manifest = json.loads(
        (root / "artifacts/futures-taker-flow-v1/archive-manifest.json").read_text()
    )
    histories = defaultdict(lambda: array("q"))
    verified = 0
    for pin in sorted(manifest["records"], key=lambda r: r["key"]):
        if pin["status"] != "COMPLETE":
            continue
        path = (
            root
            / "data/research-cache/futures-usdm-1h-v1/objects"
            / (pin["sha256"] + ".zip")
        )
        data = path.read_bytes()
        if audit.sha(data) != pin["sha256"]:
            raise ValueError("TRADED_CHECKSUM")
        symbol = pin["symbol"]
        opens = histories[symbol]
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for row in csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]))):
                if not row[0].isdigit():
                    continue
                t, close = int(row[0]), int(row[6])
                if t % HOUR or close != t + HOUR - 1 or opens and t <= opens[-1]:
                    raise ValueError("TRADED_INTERVAL_OR_DUPLICATE")
                opens.append(t)
        verified += 1
    return histories, verified


def active(hist, symbol, t):
    opens = hist.get(symbol)
    if opens is None:
        return False
    pos = bisect_left(opens, t - HOUR)
    return pos < len(opens) and opens[pos] == t - HOUR


def valid_metric(field, value):
    if value in ("", None):
        return False
    try:
        x = Decimal(value)
    except InvalidOperation:
        return False
    # Long/short ratios are unbounded ratios, not fractions: >1 is valid.
    return x.is_finite() and x >= 0


class ArchiveEvidence(audit.Evidence):
    """Separate bounded archive-only cache, independent sessions per worker."""

    def __init__(self, root, capture):
        super().__init__(root, capture)
        self.local = threading.local()
        self.guard = threading.Lock()
        self.stop = threading.Event()

    def get(self, url, tag="stable"):
        if not url.startswith("https://data.binance.vision/"):
            raise ValueError("ARCHIVE_ONLY")
        if self.stop.is_set():
            raise RuntimeError("CAPTURE_STOPPED")
        if not hasattr(self.local, "client"):
            self.local.client = audit.Evidence(self.root, self.capture)
        client = self.local.client
        # Each thread's hard budget plus a global call cap bound the capture.
        client.requests = 0
        with self.guard:
            self.requests += 1
            if self.requests > 100000:
                raise RuntimeError("GLOBAL_ARCHIVE_REQUEST_BUDGET")
        try:
            r = client.get(url, tag)
        except Exception:
            self.stop.set()
            raise
        with self.guard:
            self.used.update(client.used)
        return r


def build_needed(rows, hist):
    needed = defaultdict(set)
    for _, symbol, t, _ in rows:
        if not active(hist, symbol, t):
            continue
        day = audit.datetime.fromtimestamp(t / 1000, audit.timezone.utc).date()
        for d in (day - timedelta(days=1), day):
            needed[symbol, d.isoformat()].add(t)
    return needed


def summarize(rows, hist, observed):
    result = []
    for year in (2024, 2025):
        for pop in ("ALL", "BUY_TIMESTAMP_IDENTITY"):
            indices = [r for r in rows if r[0] == year and (pop == "ALL" or r[3])]
            for field in audit.FIELDS:
                count = present = ambiguous = 0
                for _, symbol, t, _ in indices:
                    if not active(hist, symbol, t):
                        continue
                    matches = observed.get((symbol, t), [])
                    present += bool(matches)
                    vals = {
                        Decimal(r[field])
                        for r in matches
                        if valid_metric(field, r.get(field))
                    }
                    if len(vals) == 1 and all(
                        valid_metric(field, r.get(field)) for r in matches
                    ):
                        count += 1
                    elif matches:
                        ambiguous += 1
                result.append(
                    dict(
                        year=year,
                        population=pop,
                        field=field,
                        total_identity_rows=len(indices),
                        active_market_rows=sum(
                            active(hist, r[1], r[2]) for r in indices
                        ),
                        exact_label_present_in_active_market=present,
                        valid_unambiguous_value_in_active_market=count,
                        raw_active_label_coverage_pct=count / len(indices) * 100
                        if indices
                        else None,
                        ambiguous_or_invalid_label_rows=ambiguous,
                        decision_time_coverage_pct=None,
                        decision_time_semantics="UNKNOWN",
                        label_lookup="EXACT_CREATE_TIME_EQUAL_TO_IDENTITY_TIMESTAMP; NOT_A_KNOWN_AT_CLAIM",
                    )
                )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    out = root / "artifacts" / audit.NAME / "coverage-stage"
    out.mkdir(parents=True, exist_ok=True)
    rows, proof = identities(root)
    hist, verified = traded_history(root)
    needed = build_needed(rows, hist)
    print(
        "IDENTITY_ONLY",
        len(rows),
        "TRADED_VERIFIED",
        verified,
        "NEEDED_DAILY_ARCHIVES",
        len(needed),
        flush=True,
    )
    ev = ArchiveEvidence(
        root / "data/research-cache/metrics-semantics-daily-v1/evidence", args.capture
    )
    observed = defaultdict(list)
    manifest = []
    quality = []

    def fetch(key):
        symbol, day = key
        data, meta = audit.archive(ev, symbol, day)
        selected = []
        if data:
            counts = Counter(r["timestamp_ms"] for r in data)
            quality_row = dict(
                symbol=symbol,
                day=day,
                rows=len(data),
                out_of_order_rows=sum(r["out_of_order"] for r in data),
                duplicate_labels=sum(n - 1 for n in counts.values()),
                unaligned_labels=sum(r["timestamp_ms"] % audit.STEP != 0 for r in data),
                gaps_in_sorted_unique_labels=sum(
                    max(0, (b - a) // audit.STEP - 1)
                    for a, b in zip(sorted(counts), sorted(counts)[1:])
                ),
                invalid_fields=sum(
                    not valid_metric(field, r.get(field))
                    for field in audit.FIELDS
                    for r in data
                ),
            )
            selected = [r for r in data if r["timestamp_ms"] in needed[key]]
        else:
            quality_row = dict(symbol=symbol, day=day, status="ARCHIVE_MISSING")
        return meta, quality_row, selected

    with ThreadPoolExecutor(max_workers=6) as pool:
        for i, (meta, dq, data) in enumerate(pool.map(fetch, sorted(needed))):
            manifest.append(meta)
            quality.append(dq)
            for r in data:
                observed[r["symbol"], r["timestamp_ms"]].append(r)
            if i % 200 == 0:
                print("DAILY_CACHE", i, "/", len(needed), flush=True)
    audit.write_csv(out / "exact-coverage.csv", summarize(rows, hist, observed))
    audit.write_csv(out / "historical-data-quality.csv", quality)
    (out / "historical-archive-manifest.json").write_bytes(audit.encoded(manifest))
    maps = []
    for year in (2024, 2025):
        for symbol in sorted({r[1] for r in rows if r[0] == year}):
            maps.append(
                dict(
                    year=year,
                    spot_symbol=symbol,
                    exact_futures_symbol=symbol if symbol in hist else "",
                    exact_mapping_only=True,
                    alias_guessing=False,
                )
            )
    audit.write_csv(out / "symbol-mapping.csv", maps)
    (out / "identity-source-validation.json").write_bytes(
        audit.encoded(
            dict(
                source_sha256=proof,
                identity_columns_materialized=list(IDENTITY_COLUMNS),
                outcome_columns_materialized=[],
                identities=len(rows),
                traded_archives_verified=verified,
                production_db_access=False,
                decision_time_coverage="UNKNOWN; RAW_LABEL_COVERAGE_IS_NOT_USABLE_FEATURE_COVERAGE",
            )
        )
    )
    print("EXACT_IDENTITY_COVERAGE_COMPLETE", flush=True)


if __name__ == "__main__":
    main()
