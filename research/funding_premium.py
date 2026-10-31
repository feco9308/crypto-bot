"""Frozen NEGATIVE funding/premium exploration. Offline by default; never a replay."""

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import re
import threading
import zipfile
from array import array
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

from research import funding_premium_stats as s

BASE = "f679c290d28a6f57782a267473363304548254f3"
NAME = "funding-premium-v1"
HOUR = 3600000
DAY = 24 * HOUR
EXPECTED_DIRECTION = -1
FEATURES = (
    "funding_rate_last",
    "funding_sum_24h",
    "premium_close_1h",
    "premium_mean_24h",
    "mark_index_basis_1h",
)
FAMILIES = ("fundingRate", "premiumIndexKlines", "markPriceKlines", "indexPriceKlines")
CONTROLS = ("market_score", "delta_4h", "relative_volume")
FLOW_CONTROL = "taker_buy_share_4h"
PREFIX = "POST_HOC_FUTURE_LABEL_1H."
OUTCOMES = tuple(
    PREFIX + f"{k}_{h}h"
    for h in (24, 72, 168)
    for k in ("close_return_pct", "mfe_atr", "mae_atr")
)
PRIMARY = tuple(PREFIX + f"close_return_pct_{h}h" for h in (24, 72, 168)) + tuple(
    PREFIX + f"mfe_atr_{h}h" for h in (72, 168)
)
SECONDARY = (
    "POST_HOC_FUTURE_LABEL.hit_5r_before_stop",
    "POST_HOC_FUTURE_LABEL.stop_before_1r",
)
RUNS = ("31423539f64e4650840df5b6b3fe9a26", "f9d41057d61e4714b61df69d320620e0")
ARCHIVE = "https://data.binance.vision/"
NAN = float("nan")
WINNERS = (
    ("FETUSDT", "2024-02-24T12:00:00Z"),
    ("ETHUSDT", "2024-02-20T16:00:00Z"),
    ("PEOPLEUSDT", "2024-05-31T01:00:00Z"),
)


def ms(value):
    return int(
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .replace(tzinfo=timezone.utc)
        .timestamp()
        * 1000
    )


def iso(value):
    return (
        datetime.fromtimestamp(value / 1000, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def month(value):
    return iso(value)[:7]


def source_rows(path):
    with path.open(newline="") as stream:
        yield from csv.DictReader(stream)


def key_for(family, symbol, period):
    if family not in FAMILIES + ("klines",) or not re.fullmatch(
        "[A-Z0-9]+USDT", symbol
    ):
        raise ValueError("EXACT_SYMBOL_OR_FAMILY_REQUIRED")
    if (
        not re.fullmatch(r"(202[45]-\d\d|2023-12)", period)
        or not 1 <= int(period[-2:]) <= 12
    ):
        raise ValueError("OUT_OF_SCOPE_MONTH")
    path = f"data/futures/um/monthly/{family}/{symbol}/"
    return path + (
        f"{symbol}-fundingRate-{period}.zip"
        if family == "fundingRate"
        else f"1h/{symbol}-1h-{period}.zip"
    )


def key_parts(key):
    parts = key.split("/")
    if len(parts) < 7:
        raise ValueError("INVALID_ARCHIVE_KEY")
    family, symbol = parts[4:6]
    period = key[-11:-4]
    if key_for(family, symbol, period) != key:
        raise ValueError("ONLY_OFFICIAL_MONTHLY_USDM_ALLOWED")
    return family, symbol, period


def official_url(key):
    key_parts(key)
    return ARCHIVE + key


def checksum(blob, expected):
    digest = hashlib.sha256(blob).hexdigest()
    if not re.fullmatch("[0-9a-f]{64}", expected.lower()) or digest != expected.lower():
        raise ValueError("CHECKSUM_MISMATCH")
    return digest


class Cache:
    """Research cache with immutable content pins; only approved archive GETs."""

    def __init__(self, root, download=False):
        self.root, self.download = root, download
        (root / "keys").mkdir(parents=True, exist_ok=True)
        (root / "objects").mkdir(exist_ok=True)
        self.stop = threading.Event()
        self.local = threading.local()
        self.lock = threading.Lock()
        self.requests = self.network_bytes = 0

    def path(self, key):
        key_parts(key)
        return self.root / "keys" / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def read(self, key):
        path = self.path(key)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        if data["key"] != key or data["url"] != official_url(key):
            raise ValueError("CACHE_KEY_MISMATCH")
        if data["status"] == "COMPLETE":
            blob = self.blob(data)
            checksum(blob, data["sha256"])
            if len(blob) != data["bytes"]:
                raise ValueError("SIZE_MISMATCH")
        elif data["status"] != "MISSING":
            raise ValueError("INVALID_CACHE_STATUS")
        return data

    def blob(self, data):
        return (self.root / "objects" / (data["sha256"] + ".zip")).read_bytes()

    def missing(self, key, reason):
        family, symbol, _ = key_parts(key)
        data = dict(
            key=key,
            url=official_url(key),
            status="MISSING",
            bytes=0,
            family=family,
            symbol=symbol,
            reason=reason,
        )
        self.path(key).write_bytes(s.encoded(data))
        return data

    def save(self, key, blob, expected, source):
        family, symbol, _ = key_parts(key)
        digest = checksum(blob, expected)
        target = self.root / "objects" / (digest + ".zip")
        if target.exists() and s.sha(target) != digest:
            raise ValueError("PIN_CORRUPTED")
        target.write_bytes(blob)
        data = dict(
            key=key,
            url=official_url(key),
            checksum_url=official_url(key) + ".CHECKSUM",
            status="COMPLETE",
            sha256=digest,
            bytes=len(blob),
            family=family,
            symbol=symbol,
            interval="event" if family == "fundingRate" else "1h",
            source=source,
        )
        self.path(key).write_bytes(s.encoded(data))
        return data

    def get(self, url, cap):
        key = url.removeprefix(ARCHIVE).removesuffix(".CHECKSUM")
        if url not in (official_url(key), official_url(key) + ".CHECKSUM"):
            raise ValueError("NON_ARCHIVE_NETWORK_FORBIDDEN")
        if not self.download:
            raise ValueError("OFFLINE_MISSING_PIN")
        if self.stop.is_set():
            raise RuntimeError("DOWNLOAD_STOPPED")
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
        with self.lock:
            self.requests += 1
            if self.requests > 24000:
                self.stop.set()
                raise RuntimeError("REQUEST_BUDGET")
        try:
            with self.local.session.get(
                url, timeout=45, stream=True, allow_redirects=False
            ) as response:
                if response.status_code in (418, 429, 503):
                    self.stop.set()
                    raise RuntimeError("RATE_LIMIT_STOP_NO_RETRY")
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                if response.status_code != 200:
                    raise ValueError("UNEXPECTED_HTTP_STATUS")
                chunks = []
                size = 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > cap:
                        self.stop.set()
                        raise ValueError("ARCHIVE_SIZE_CAP")
                    chunks.append(chunk)
                with self.lock:
                    self.network_bytes += size
                    if self.network_bytes > 400_000_000:
                        self.stop.set()
                        raise RuntimeError("DOWNLOAD_BYTE_BUDGET")
                return b"".join(chunks)
        except requests.RequestException:
            self.stop.set()
            raise

    def fetch(self, key):
        old = self.read(key)
        if old is not None:
            return old
        check = self.get(official_url(key) + ".CHECKSUM", 4096)
        if check is None:
            return self.missing(key, "OFFICIAL_CHECKSUM_HTTP_404")
        expected = check.decode().split()[0]
        if not re.fullmatch("[0-9a-fA-F]{64}", expected):
            raise ValueError("INVALID_CHECKSUM")
        blob = self.get(official_url(key), 250000)
        if blob is None:
            raise ValueError("ZIP_MISSING_WITH_CHECKSUM")
        return self.save(key, blob, expected, "OFFICIAL_CHECKSUM_GET")

    def import_audit(self, prior, needed):
        manifest = json.loads((prior / "manifest.json").read_text())
        validation = json.loads((prior / "source-validation.json").read_text())
        pins = {
            r["key"]: r["sha256"]
            for r in source_rows(prior / "sample-validation.csv")
            if r["status"] == "VERIFIED_ROWS" and r["checksum_verified"] == "True"
        }
        count = 0
        for token in validation["used_evidence"]:
            path = prior / "evidence" / (token + ".json.gz")
            if s.sha(path) != manifest["files"][str(path.relative_to(prior))]["sha256"]:
                raise ValueError("AUDIT_MANIFEST_MISMATCH")
            row = json.loads(gzip.decompress(path.read_bytes()))
            key = row["request"]["url"].removeprefix(ARCHIVE)
            if (
                key not in needed
                or row["status"] != 200
                or row["request"]["headers"]
                or row["truncated"]
            ):
                continue
            if self.read(key):
                continue
            if key not in pins:
                # Alignment-only evidence is not a full sample-validation pin;
                # fetch and verify its official checksum through the normal path.
                continue
            blob = bytes.fromhex(row["body_hex"])
            if pins.get(key) != row["sha256"]:
                raise ValueError("UNPINNED_AUDIT_SAMPLE")
            self.save(key, blob, row["sha256"], "CHECKSUM_VALIDATED_AUDIT_SAMPLE")
            count += 1
        return count


def parse_archive(blob, key):
    """Funding timestamps are integer milliseconds, never rounded to an hour."""
    family, _, period = key_parts(key)
    columns = (
        [array("q"), array("d")]
        if family == "fundingRate"
        else [array("q"), array("q"), array("d")]
    )
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        if len(archive.namelist()) != 1:
            raise ValueError("UNEXPECTED_ZIP_MEMBERS")
        rows = csv.reader(
            io.TextIOWrapper(archive.open(archive.namelist()[0]), encoding="utf-8-sig")
        )
        for i, row in enumerate(rows):
            if not row:
                raise ValueError("EMPTY_ROW")
            if i == 0 and row[0] in ("calc_time", "open_time"):
                continue
            if not row[0].isdigit():
                raise ValueError("INVALID_TIMESTAMP")
            op = int(row[0])
            if month(op) != period:
                raise ValueError("WRONG_ARCHIVE_MONTH")
            if columns[0] and op <= columns[0][-1]:
                raise ValueError("DUPLICATE_OR_UNORDERED_TIMESTAMP")
            if family == "fundingRate":
                if len(row) != 3:
                    raise ValueError("FUNDING_SCHEMA")
                value = float(row[2])
                if (
                    not math.isfinite(value)
                    or not math.isfinite(float(row[1]))
                    or float(row[1]) <= 0
                ):
                    raise ValueError("INVALID_FUNDING")
                columns[0].append(op)
                columns[1].append(value)
            else:
                if len(row) < 12 or not row[6].isdigit():
                    raise ValueError("KLINE_SCHEMA")
                close = int(row[6])
                value = float(row[4])
                prices = [float(row[j]) for j in (1, 2, 3, 4)]
                if op % HOUR or close != op + HOUR - 1:
                    raise ValueError("INVALID_HOURLY_INTERVAL")
                if not all(map(math.isfinite, prices)):
                    raise ValueError("NONFINITE_PRICE")
                o, hi, lo, c = prices
                if not lo <= min(o, c) <= max(o, c) <= hi:
                    raise ValueError("INVALID_OHLC")
                if family != "premiumIndexKlines" and min(prices) <= 0:
                    raise ValueError("NONPOSITIVE_PRICE")
                columns[0].append(op)
                columns[1].append(close)
                columns[2].append(value)
    if not columns[0]:
        raise ValueError("EMPTY_ARCHIVE")
    return columns, dict(
        row_count=len(columns[0]),
        first_timestamp=iso(columns[0][0]),
        last_timestamp=iso(columns[0][-1]),
        duplicate_timestamps=0,
        missing_hours=sum(
            max(0, (b - a) // HOUR - 1) for a, b in zip(columns[0], columns[0][1:])
        )
        if family != "fundingRate"
        else None,
        last_close_time=iso(columns[1][-1]) if family != "fundingRate" else None,
    )


def ingest(cache, records):
    grouped = defaultdict(list)
    manifest = []
    for record in sorted(records, key=lambda r: r["key"]):
        if record["status"] != "COMPLETE":
            manifest.append(record)
            continue
        cols, validation = parse_archive(cache.blob(record), record["key"])
        family, symbol, _ = key_parts(record["key"])
        grouped[family, symbol].append(cols)
        manifest.append(record | validation | {"parse_status": "VALIDATED"})
    histories = {}
    for key, pieces in grouped.items():
        cols = [array(col.typecode) for col in pieces[0]]
        for piece in pieces:
            if cols[0] and piece[0][0] <= cols[0][-1]:
                raise ValueError("OVERLAPPING_ARCHIVES")
            for out, col in zip(cols, piece):
                out.extend(col)
        histories[key] = cols
    return histories, manifest


def window(history, t, hours=1):
    if history is None:
        return None
    opens, closes, *_ = history
    end = bisect_left(opens, t)
    start = end - hours
    if start < 0 or end <= 0:
        return None
    if any(
        opens[j] != t - (end - j) * HOUR
        or closes[j] >= t
        or closes[j] != opens[j] + HOUR - 1
        for j in range(start, end)
    ):
        return None
    return start, end


def values_at(traded, funding, premium, mark, index, t):
    """Exactly five decision-time features, plus non-predictive source metadata."""
    values = {f: NAN for f in FEATURES}
    reasons = {f: "INACTIVE_TRADED_FUTURES_MARKET" for f in FEATURES}
    meta = dict(
        traded_futures_active_at_t=0.0,
        traded_source_close_ms=NAN,
        funding_event_count_24h=0.0,
        funding_last_event_ms=NAN,
        premium_source_close_ms=NAN,
        premium_window_first_open_ms=NAN,
        basis_source_open_ms=NAN,
        mark_source_close_ms=NAN,
        index_source_close_ms=NAN,
    )
    active = window(traded, t)
    if active is None:
        return values, reasons, meta
    meta.update(
        traded_futures_active_at_t=1.0, traded_source_close_ms=traded[1][active[1] - 1]
    )
    if funding:
        times, rates = funding
        a, b = bisect_right(times, t - DAY), bisect_right(times, t)
        if b > a:
            values["funding_rate_last"] = rates[b - 1]
            values["funding_sum_24h"] = math.fsum(rates[a:b])
            meta.update(
                funding_event_count_24h=float(b - a), funding_last_event_ms=times[b - 1]
            )
    for f in FEATURES[:2]:
        reasons[f] = (
            "AVAILABLE" if math.isfinite(values[f]) else "NO_EVENT_IN_EXACT_24H_WINDOW"
        )
    for feature, hours in (("premium_close_1h", 1), ("premium_mean_24h", 24)):
        w = window(premium, t, hours)
        if w:
            a, b = w
            values[feature] = math.fsum(premium[2][a:b]) / hours
            meta["premium_source_close_ms"] = premium[1][b - 1]
            if hours == 24:
                meta["premium_window_first_open_ms"] = premium[0][a]
        reasons[feature] = "AVAILABLE" if w else "MISSING_CONTIGUOUS_PREMIUM_HOURS"
    mw, iw = window(mark, t), window(index, t)
    if mw and iw:
        m, i = mw[1] - 1, iw[1] - 1
        if mark[0][m] == index[0][i] and index[2][i] > 0:
            values["mark_index_basis_1h"] = mark[2][m] / index[2][i] - 1
            meta.update(
                basis_source_open_ms=mark[0][m],
                mark_source_close_ms=mark[1][m],
                index_source_close_ms=index[1][i],
            )
    reasons["mark_index_basis_1h"] = (
        "AVAILABLE"
        if math.isfinite(values["mark_index_basis_1h"])
        else "MISSING_SAME_HOUR_MARK_INDEX_PAIR"
    )
    return values, reasons, meta


def verify_inputs(root):
    hashes = {}
    directories = {
        "flow": root / "artifacts/futures-taker-flow-v1",
        "cross": root / "artifacts/cross-sectional-market-structure-v1",
        "audit": root / "artifacts/derivatives-data-availability-v1",
    }
    names = {
        "flow": [
            "futures-flow-dataset-2024.csv",
            "futures-flow-dataset-2025.csv",
            "archive-manifest.json",
            "source-validation.json",
        ],
        "cross": [
            "market-structure-dataset-2024.csv",
            "market-structure-dataset-2025.csv",
            "source-validation.json",
        ],
        "audit": [
            "archive-layout.json",
            "archive-objects.csv",
            "sample-validation.csv",
            "source-validation.json",
        ],
    }
    for key, folder in directories.items():
        manifest = json.loads((folder / "manifest.json").read_text())
        for name in names[key]:
            path = folder / name
            digest = s.sha(path)
            if digest != manifest["files"][name]["sha256"]:
                raise ValueError("SOURCE_MANIFEST_MISMATCH " + name)
            hashes[str(path.relative_to(root))] = digest
    return directories, hashes


def capture(root, download):
    directories, hashes = verify_inputs(root)
    out = root / "artifacts" / NAME
    out.mkdir(parents=True, exist_ok=True)
    cache = Cache(root / "data/research-cache/funding-premium-v1", download)
    traded_cache = Cache(root / "data/research-cache/futures-usdm-1h-v1")
    pinned = json.loads((directories["flow"] / "archive-manifest.json").read_text())[
        "records"
    ]
    # Reverify every prior traded archive pin; never fetch/refresh this cache.
    for pin in pinned:
        record = traded_cache.read(pin["key"])
        if record is None or record["status"] != pin["status"]:
            raise ValueError("TRADED_PIN_MISSING")
        if record["status"] == "COMPLETE" and record["sha256"] != pin["sha256"]:
            raise ValueError("TRADED_PIN_MISMATCH")
    traded_hist, traded_manifest = ingest(traded_cache, pinned)
    mapped = set(
        json.loads((directories["audit"] / "archive-layout.json").read_text())[
            "archive_symbols"
        ]
    )
    needed = set()
    mapping = []
    for year in (2024, 2025):
        counts = Counter()
        buys = Counter()
        active_count = 0
        for row in source_rows(
            directories["flow"] / f"futures-flow-dataset-{year}.csv"
        ):
            symbol, t = row["symbol"], ms(row["timestamp"])
            counts[symbol] += 1
            buys[symbol] += row["current_buy_eligible"] == "1.0"
            active = window(traded_hist.get(("klines", symbol)), t) is not None
            if active != (row["futures_1h_available"] == "1.0"):
                raise ValueError("TRADED_ACTIVITY_PARITY_MISMATCH")
            if not active:
                continue
            active_count += 1
            for family in FAMILIES:
                periods = (
                    {month(t - DAY), month(t)}
                    if family == "fundingRate"
                    else {month(t - HOUR), month(t - DAY)}
                    if family == "premiumIndexKlines"
                    else {month(t - HOUR)}
                )
                needed.update(key_for(family, symbol, p) for p in periods)
        mapping.extend(
            dict(
                year=year,
                spot_symbol=symbol,
                futures_symbol=symbol if symbol in mapped else "",
                exact_mapping=symbol in mapped,
                alias_guessing=False,
                top20_rows=n,
                buy_rows=buys[symbol],
            )
            for symbol, n in sorted(counts.items())
        )
        print("ACTIVE_TRADED_PARITY", year, active_count, flush=True)
    listed = {
        r["key"] for r in source_rows(directories["audit"] / "archive-objects.csv")
    }
    print(
        "NEEDED",
        len(needed),
        "AUDIT_REUSED",
        cache.import_audit(directories["audit"], needed),
        flush=True,
    )
    for key in needed:
        if "-2023-12.zip" not in key and key not in listed and cache.read(key) is None:
            cache.missing(key, "PINNED_AUDIT_LISTING_ABSENT")
    records = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for i, record in enumerate(pool.map(cache.fetch, sorted(needed))):
            records.append(record)
            if i % 100 == 0:
                print(
                    "CAPTURE",
                    i,
                    "/",
                    len(needed),
                    "NETWORK_BYTES",
                    cache.network_bytes,
                    flush=True,
                )
    histories, manifest = ingest(cache, records)
    write_rows(out / "symbol-mapping.csv", mapping)
    (out / "archive-manifest.json").write_bytes(
        s.encoded(dict(records=manifest, traded_records=traded_manifest))
    )
    (out / "capture-validation.json").write_bytes(
        s.encoded(
            dict(
                input_sha256=hashes,
                archive_counts=dict(Counter(r["status"] for r in records)),
                traded_pins_reverified=len(pinned),
                new_network_bytes=cache.network_bytes,
                requests=cache.requests,
            )
        )
    )
    print(
        "CAPTURE_COMPLETE",
        len(records),
        dict(Counter(r["status"] for r in records)),
        flush=True,
    )
    return directories, hashes, traded_hist, histories


def write_rows(path, rows):
    rows = list(rows)
    fields = sorted({k for row in rows for k in row})
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--capture-only", action="store_true")
    parser.add_argument("--resume-existing", action="store_true")
    args = parser.parse_args()
    if args.resume_existing:
        if args.download or args.capture_only:
            parser.error("--resume-existing cannot download or capture")
        root = args.root.resolve()
        directories, hashes = verify_inputs(root)
        prior = json.loads(
            (root / "artifacts" / NAME / "capture-validation.json").read_text()
        )
        if prior["input_sha256"] != hashes:
            raise ValueError("RECOVERY_SOURCE_CHANGED")
        analyze(root, directories, hashes, None, None, resume_existing=True)
        return
    state = capture(args.root.resolve(), args.download)
    if not args.capture_only:
        analyze(args.root.resolve(), *state)


def build_table(root, year, directories, traded, histories):
    table = s.Table()
    mapped = set(
        json.loads((directories["audit"] / "archive-layout.json").read_text())[
            "archive_symbols"
        ]
    )
    seen = set()
    for raw in source_rows(directories["flow"] / f"futures-flow-dataset-{year}.csv"):
        symbol, t = raw["symbol"], ms(raw["timestamp"])
        if (symbol, t) in seen:
            raise ValueError("DUPLICATE_CANONICAL_ROW")
        seen.add((symbol, t))
        values, reasons, meta = values_at(
            traded.get(("klines", symbol)),
            *(histories.get((family, symbol)) for family in FAMILIES),
            t,
        )
        if symbol not in mapped:
            reasons = {f: "NO_EXACT_MAPPING" for f in FEATURES}
        row = dict(
            symbol=symbol,
            timestamp=raw["timestamp"],
            timestamp_ms=t,
            year=year,
            symbol_day_id=raw["symbol_day_id"],
            eligibility_episode_id=raw["eligibility_episode_id"],
            **{
                k: s.number(raw[k])
                for k in (
                    "symbol_day_first",
                    "setup_episode_first",
                    "current_buy_eligible",
                )
                + CONTROLS
                + (FLOW_CONTROL,)
                + OUTCOMES
                + SECONDARY
            },
            **values,
            **meta,
            **{f + "_status": reasons[f] for f in FEATURES},
        )
        table.add(row)
    expected = {2024: (175680, 4915), 2025: (160320, 2069)}[year]
    if (table.size, sum(table.numeric["current_buy_eligible"])) != expected:
        raise ValueError("CANONICAL_POPULATION_CHANGED")
    table.write(root / "artifacts" / NAME / f"funding-premium-dataset-{year}.csv")
    return table


def coverage_rows(table, year):
    result = []
    for feature in FEATURES:
        for pop, indices in s.scopes(table).items():
            if pop not in ("ALL_TOP20", "BUY_ELIGIBLE"):
                continue
            available = [i for i in indices if math.isfinite(table.numeric[feature][i])]
            days = defaultdict(list)
            for i in indices:
                days[table.text["symbol_day_id"][i]].append(i)
            result.append(
                dict(
                    year=year,
                    feature=feature,
                    population=pop,
                    total_rows=len(indices),
                    available_rows=len(available),
                    coverage_pct=100 * len(available) / len(indices) if indices else 0,
                    total_unique_symbols=len(
                        {table.text["symbol"][i] for i in indices}
                    ),
                    available_unique_symbols=len(
                        {table.text["symbol"][i] for i in available}
                    ),
                    total_symbol_days=len(days),
                    symbol_days_any_available=sum(
                        any(math.isfinite(table.numeric[feature][i]) for i in values)
                        for values in days.values()
                    ),
                    symbol_days_fully_available=sum(
                        all(math.isfinite(table.numeric[feature][i]) for i in values)
                        for values in days.values()
                    ),
                    unavailable_reasons=json.dumps(
                        dict(
                            Counter(
                                table.text[feature + "_status"][i]
                                for i in indices
                                if not math.isfinite(table.numeric[feature][i])
                            )
                        ),
                        sort_keys=True,
                    ),
                )
            )
    return result


def coverage_gate(rows, feature):
    cells = [r for r in rows if r["feature"] == feature]
    return (
        len(cells) == 4
        and {(r["year"], r["population"]) for r in cells}
        == {(y, p) for y in (2024, 2025) for p in ("ALL_TOP20", "BUY_ELIGIBLE")}
        and all(r["coverage_pct"] >= 80 for r in cells)
    )


def selection_analysis(table, year):
    rows = []
    for feature in FEATURES:
        for population, indices in s.scopes(table).items():
            if population not in ("ALL_TOP20", "BUY_ELIGIBLE"):
                continue
            valid = [i for i in indices if math.isfinite(table.numeric[feature][i])]
            unavailable = [
                i for i in indices if not math.isfinite(table.numeric[feature][i])
            ]
            for h in (72, 168):
                for measure in ("mfe_atr", "close_return_pct"):
                    outcome = PREFIX + f"{measure}_{h}h"
                    a = [
                        table.numeric[outcome][i]
                        for i in valid
                        if math.isfinite(table.numeric[outcome][i])
                    ]
                    b = [
                        table.numeric[outcome][i]
                        for i in unavailable
                        if math.isfinite(table.numeric[outcome][i])
                    ]
                    auc = s.auc(a, b)
                    rows.append(
                        dict(
                            feature=feature,
                            year=year,
                            population=population,
                            outcome=outcome,
                            available_total=len(valid),
                            unavailable_total=len(unavailable),
                            available_observed=len(a),
                            unavailable_observed=len(b),
                            available_median=s.median(a),
                            unavailable_median=s.median(b),
                            cliffs_delta=2 * auc - 1 if auc is not None else None,
                            role="SELECTION_BIAS_ONLY_UNWEIGHTED",
                        )
                    )
    return rows


def paired_outcome_digest(table, indices, outcome):
    h = hashlib.sha256()
    for i in indices:
        v = table.numeric[outcome][i]
        if math.isfinite(v):
            h.update(
                s.encoded([table.text["symbol"][i], table.text["timestamp"][i], v])
            )
    return h.hexdigest()


def statistics_for(table, year, eligible):
    stats, deciles, secondary, controls, concentration = [], [], [], [], []
    cache = {}

    def effect(indices, feature, outcome, need_groups=False):
        token = (s.row_digest(table, indices), feature, outcome)
        if token not in cache or need_groups:
            r, *rest = s.effect(table, indices, feature, outcome)
            r.update(
                input_row_sha256=token[0],
                paired_outcome_sha256=paired_outcome_digest(table, indices, outcome),
            )
            # Cache scalar summaries only, never annual index/rank arrays.
            cache[token] = r
            if need_groups:
                return r, *rest
        return cache[token], None, None, None, None

    for population, indices in s.scopes(table).items():
        print("STATISTICS", year, population, len(indices), flush=True)
        for feature in eligible:
            valid = [i for i in indices if math.isfinite(table.numeric[feature][i])]
            groups = defaultdict(list)
            for i, label in zip(
                valid, s.deciles([table.numeric[feature][i] for i in valid])
            ):
                groups[label].append(i)
            curve = []
            for label in range(1, 11):
                r = dict(
                    year=year,
                    population=population,
                    feature=feature,
                    decile=label,
                    count=len(groups[label]),
                )
                for outcome in OUTCOMES:
                    vals = [
                        table.numeric[outcome][i]
                        for i in groups[label]
                        if math.isfinite(table.numeric[outcome][i])
                    ]
                    r["median_" + outcome] = s.median(vals)
                    r["observed_" + outcome] = len(vals)
                deciles.append(r)
                curve.append(r)
            for outcome in PRIMARY:
                result, _, _, top, bottom = effect(valid, feature, outcome, True)
                stats.append(
                    result
                    | dict(
                        year=year,
                        population=population,
                        expected_direction="NEGATIVE",
                        **s.curve_summary([r["median_" + outcome] for r in curve]),
                    )
                )
                if (
                    population in ("ALL_TOP20", "BUY_ELIGIBLE")
                    and "mfe_atr_" in outcome
                ):
                    for kind, group in (
                        ("SYMBOL", lambda i: table.text["symbol"][i]),
                        (
                            "UTC_CALENDAR_QUARTER",
                            lambda i: (
                                table.text["timestamp"][i][:4]
                                + "Q"
                                + str(
                                    (int(table.text["timestamp"][i][5:7]) - 1) // 3 + 1
                                )
                            ),
                        ),
                    ):
                        for r in s.leave_group_effect(
                            table,
                            top,
                            bottom,
                            outcome,
                            group,
                            all_groups={group(i) for i in valid},
                        ):
                            full = result["cliffs_delta"]
                            delta = r["cliffs_delta"]
                            concentration.append(
                                r
                                | dict(
                                    year=year,
                                    population=population,
                                    feature=feature,
                                    outcome=outcome,
                                    group_type=kind,
                                    full_cliffs_delta=full,
                                    signed_effect_retention=delta / full
                                    if full and delta is not None
                                    else None,
                                    retains_direction_and_half_effect=bool(
                                        full
                                        and delta is not None
                                        and delta / full >= 0.5
                                    ),
                                )
                            )
            for label in SECONDARY:
                a = [
                    table.numeric[feature][i]
                    for i in valid
                    if table.numeric[label][i] == 1
                ]
                b = [
                    table.numeric[feature][i]
                    for i in valid
                    if table.numeric[label][i] == 0
                ]
                auc = s.auc(a, b)
                secondary.append(
                    dict(
                        year=year,
                        population=population,
                        feature=feature,
                        outcome=label,
                        positive_count=len(a),
                        negative_count=len(b),
                        auc=auc,
                        cliffs_delta=2 * auc - 1 if auc is not None else None,
                        role="SECONDARY_ONLY",
                    )
                )
            if population not in ("ALL_TOP20", "BUY_ELIGIBLE"):
                continue
            for name, features in (
                ("SPOT_CONTROLS", (feature,) + CONTROLS),
                ("SPOT_PLUS_TAKER_4H", (feature,) + CONTROLS + (FLOW_CONTROL,)),
            ):
                matched = [
                    i
                    for i in valid
                    if all(math.isfinite(table.numeric[f][i]) for f in features)
                ]
                for f in features:
                    for outcome in PRIMARY:
                        result, *_ = effect(matched, f, outcome)
                        controls.append(
                            result
                            | dict(
                                year=year,
                                population=population,
                                matched_feature=feature,
                                matched_pool=name,
                                role="TARGET_MATCHED"
                                if f == feature
                                else "CONTROL_MATCHED",
                            )
                        )
    return stats, deciles, secondary, controls, concentration


def evidence_screen(feature, stats, secondary, concentration, selection, coverage_pass):
    cells = {
        (r["year"], r["population"], r["outcome"]): r
        for r in stats
        if r["feature"] == feature
    }

    def cell(y, p, h):
        return cells.get((y, p, PREFIX + f"mfe_atr_{h}h"))

    main = [cell(y, "ALL_TOP20", h) for y in (2024, 2025) for h in (72, 168)]
    main_pass = all(
        r
        and min(r["top_count"], r["bottom_count"]) >= 30
        and r["cliffs_delta"] is not None
        and r["cliffs_delta"] <= -0.147
        and r["spearman"] is not None
        and r["spearman"] <= -0.05
        for r in main
    )
    dependency = [
        cell(y, p, h)
        for y in (2024, 2025)
        for p in ("BUY_ELIGIBLE", "SYMBOL_DAY_FIRST", "BUY_SETUP_EPISODE_FIRST")
        for h in (72, 168)
    ]
    dep_pass = all(
        r
        and min(r["top_count"], r["bottom_count"]) >= 30
        and r["cliffs_delta"] is not None
        and r["cliffs_delta"] <= 0
        and r["spearman"] is not None
        and r["spearman"] <= 0
        for r in dependency
    )
    groups = [
        r
        for r in concentration
        if r["feature"] == feature and r["population"] == "ALL_TOP20"
    ]
    group_checks = {
        kind: bool([r for r in groups if r["group_type"] == kind])
        and all(
            min(r["top_count"], r["bottom_count"]) >= 30
            and r["retains_direction_and_half_effect"]
            and r["full_cliffs_delta"] < 0
            for r in groups
            if r["group_type"] == kind
        )
        for kind in ("SYMBOL", "UTC_CALENDAR_QUARTER")
    }
    contrary = [
        r
        for r in secondary
        if r["feature"] == feature
        and min(r["positive_count"], r["negative_count"]) >= 30
        and r["cliffs_delta"] is not None
        and r["cliffs_delta"] * (-1 if r["outcome"] == SECONDARY[1] else 1) >= 0.147
    ]
    # Conservative, predeclared interpretation guard; no weighting or correction.
    selection_flags = [
        r
        for r in selection
        if r["feature"] == feature
        and "mfe_atr_" in r["outcome"]
        and min(r["available_observed"], r["unavailable_observed"]) >= 30
        and r["cliffs_delta"] is not None
        and abs(r["cliffs_delta"]) >= 0.147
    ]
    primary = (
        coverage_pass
        and main_pass
        and dep_pass
        and all(group_checks.values())
        and not selection_flags
    )
    return dict(
        feature=feature,
        expected_direction="NEGATIVE",
        coverage_pass=coverage_pass,
        main_all_year_horizon_pass=main_pass,
        dependency_and_buy_pass=dep_pass,
        leave_symbol_pass=group_checks["SYMBOL"],
        leave_quarter_pass=group_checks["UTC_CALENDAR_QUARTER"],
        selection_bias_interpretation_flag=bool(selection_flags),
        selection_flag_cells=len(selection_flags),
        primary_pass=primary,
        secondary_strong_opposite=bool(contrary),
        candidate_eligible=primary and not contrary,
        minimum_all_abs_cliff=min(
            (
                abs(r["cliffs_delta"])
                for r in main
                if r and r["cliffs_delta"] is not None
            ),
            default=0,
        ),
    )


def classify(checks, stats):
    if not any(c["coverage_pass"] for c in checks):
        return "COVERAGE_STOP", "NONE"
    candidates = sorted(
        (c for c in checks if c["candidate_eligible"]),
        key=lambda c: (-c["minimum_all_abs_cliff"], c["feature"]),
    )
    if candidates:
        return "FUNDING_PREMIUM_SIGNAL_FOUND", candidates[0]["feature"]
    # Unexpected positive effects can only produce B, never a hypothesis candidate.
    interesting = any(
        r["feature"] in FEATURES
        and r["cliffs_delta"] is not None
        and abs(r["cliffs_delta"]) >= 0.147
        for r in stats
    )
    return (
        "FUNDING_PREMIUM_SIGNAL_WEAK_OR_UNSTABLE"
        if interesting
        else "FUNDING_PREMIUM_SIGNAL_NOT_FOUND"
    ), "NONE"


def winner_anatomy(table, year):
    rows = []
    if year != 2024:
        return rows
    scopes = s.scopes(table)
    for symbol, timestamp in WINNERS:
        found = [
            i
            for i in range(table.size)
            if table.text["symbol"][i] == symbol
            and table.text["timestamp"][i] == timestamp
        ]
        if len(found) != 1:
            raise ValueError("WINNER_SOURCE_ROW_MISSING")
        i = found[0]
        for feature in FEATURES:
            v = table.numeric[feature][i]
            row = dict(
                symbol=symbol,
                timestamp=timestamp,
                feature=feature,
                value=v if math.isfinite(v) else None,
                status=table.text[feature + "_status"][i],
                role="POST_HOC_EXAMPLE_NOT_RULE",
            )
            for p in ("ALL_TOP20", "BUY_ELIGIBLE"):
                ref = sorted(
                    table.numeric[feature][j]
                    for j in scopes[p]
                    if math.isfinite(table.numeric[feature][j])
                )
                row[p + "_reference_count"] = len(ref)
                row[p + "_percentile"] = (
                    (
                        (
                            bisect_left(ref, v)
                            + (bisect_right(ref, v) - bisect_left(ref, v) - 1) / 2
                        )
                        / (len(ref) - 1)
                    )
                    if math.isfinite(v) and len(ref) > 1
                    else None
                )
            rows.append(row)
    return rows


def methodology():
    return dict(
        base=BASE,
        role="2024_AND_2025_READ_ONLY_EXPLORATION",
        features=list(FEATURES),
        expected_direction="NEGATIVE",
        outcome_namespace=PREFIX,
        primary_labels="UNCHANGED_PREVIOUSLY_VALIDATED_SPOT_OUTCOMES",
        source_runs=list(RUNS),
        active_market="EXACT_PREVIOUS_TRADED_1H_REQUIRED_FOR_EVERY_FEATURE",
        funding_window="(t-24h,t]; ORIGINAL_INTEGER_MILLISECONDS; AT_LEAST_ONE_EVENT; NO_CADENCE_ASSUMPTION",
        candles="EXACT_PREVIOUS_HOUR; close_time<t; NO_CURRENT_CANDLE",
        premium_mean="24_CONTIGUOUS_CLOSES_ARITHMETIC_MEAN",
        bins="AVERAGE_TIED_RANK_PERCENTILES; FIXED_DECILES_AND_TOP_BOTTOM_QUINTILES; NO_SEARCH",
        dependency="INHERITED_SOURCE_FLAGS_ONLY; NO_NEW_EPISODES",
        evidence=dict(
            cliff=0.147,
            spearman=0.05,
            minimum_extreme_n=30,
            leave_group_retention=0.5,
            primary_direction="NEGATIVE",
            selection_guard="CONSERVATIVE_NO_CANDIDATE_IF_AVAILABLE_VS_UNAVAILABLE_MFE_ABS_CLIFF_GE_0.147_WITH_N_GE_30",
        ),
        leave_group="FIX_FULL_POPULATION_QUINTILE_MEMBERSHIP; DO_NOT_REBIN",
        matched_controls=list(CONTROLS) + ([FLOW_CONTROL]),
        secondary="CONSISTENCY_VETO_ONLY; CANNOT_DRIVE_CATEGORY",
        no_new_features=True,
        no_threshold_optimization=True,
        no_imputation=True,
        no_weighting=True,
        no_2023_research_cohort=True,
        pre_start_history="2023-12_ARCHIVES_ONLY_TO_FORM_FIRST_2024_DECISION_FEATURES",
        no_2026_input=True,
        database_access="mode=ro; PRAGMA query_only=ON",
        production_db_writes=0,
        new_replay=False,
        deployment=False,
    )


def package(out):
    files = {
        p.name: dict(bytes=p.stat().st_size, sha256=s.sha(p))
        for p in sorted(out.iterdir())
        if p.is_file() and p.name not in ("manifest.json", NAME + ".zip")
    }
    (out / "manifest.json").write_bytes(s.encoded(dict(base=BASE, files=files)))
    path = out / (NAME + ".zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in sorted([*files, "manifest.json"]):
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, (out / name).read_bytes())
    return path


def report(out, summary, statistics):
    lines = [
        "# Funding + Premium / Basis Diagnostic v1",
        "",
        "READ-ONLY EXPLORATION. Expected direction: NEGATIVE. No strategy, replay or deployment.",
        "",
        "Category: **"
        + summary["category"]
        + "**. Candidate: **"
        + summary["hypothesis_candidate_feature"]
        + "**.",
        "",
        "## Exact decision-row coverage",
        "",
        "| Feature | Year | Population | Available / total | Coverage |",
        "|---|---|---|---:|---:|",
    ]
    for r in summary["coverage"]:
        lines.append(
            f"| {r['feature']} | {r['year']} | {r['population']} | {r['available_rows']} / {r['total_rows']} | {r['coverage_pct']:.4f}% |"
        )
    lines += [
        "",
        "## Primary MFE results",
        "",
        "Top minus bottom quintile Cliff; higher funding/premium was hypothesized to predict weaker long opportunity. Positive effects are unexpected.",
        "",
        "| Feature | Year | Population | Horizon | Cliff | Spearman |",
        "|---|---|---|---:|---:|---:|",
    ]
    for r in statistics:
        if (
            r["population"] in ("ALL_TOP20", "BUY_ELIGIBLE")
            and "mfe_atr_" in r["outcome"]
        ):
            lines.append(
                f"| {r['feature']} | {r['year']} | {r['population']} | {r['outcome'].split('_')[-1]} | {r['cliffs_delta']} | {r['spearman']} |"
            )
    lines += [
        "",
        "## Evidence checks",
        "",
        "```json",
        json.dumps(summary["feature_checks"], indent=2),
        "```",
        "",
        "## Interpretation limits",
        "",
        "The available subset differs from the exact-mapping unavailable subset. Selection-analysis.csv reports unweighted differences for every feature and both populations/years. No correction or imputation is applied. A conservative, predeclared selection guard prevents candidate promotion when a nontrivial selection difference makes the interpretation uncertain.",
        "All labels, symbol-day flags and BUY episode flags are inherited unchanged. Only these five derivatives features are evaluated. Funding event counts and source timestamps are metadata, never predictors.",
        "Matched controls use identical input rows and paired outcome rows, with exported hashes. The optional taker4h comparison uses its own fully matched pool. These are univariate descriptive associations, not proof of conditional incremental predictive information.",
        "Raw archive objects are outside this export. No funding record extends traded-market coverage. No 2026 input or 2023 research cohort is used.",
        "See source-validation.json and export-validation.json for integrity and safety checks.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n")


def load_existing_table(path):
    """Read completed dataset bytes without rebuilding features or touching cache."""
    text = {"symbol", "timestamp", "symbol_day_id", "eligibility_episode_id"} | {
        f + "_status" for f in FEATURES
    }
    table, seen = s.Table(), set()
    for raw in source_rows(path):
        if not {"symbol", "timestamp", *FEATURES}.issubset(raw):
            raise ValueError("RECOVERY_DATASET_SCHEMA")
        key = raw["symbol"], raw["timestamp"]
        if key in seen:
            raise ValueError("DUPLICATE_CANONICAL_ROW")
        seen.add(key)
        table.add({k: v if k in text else s.number(v) for k, v in raw.items()})
    return table


def verify_existing_rows(path, rows):
    """Require completed stage output to match; never silently overwrite it."""
    buffer = io.StringIO(newline="")
    fields = sorted({k for row in rows for k in row})
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    if path.read_bytes() != buffer.getvalue().encode():
        raise ValueError("RECOVERY_STAGE_MISMATCH: " + path.name)


def analyze(root, directories, hashes, traded, histories, resume_existing=False):
    out = root / "artifacts" / NAME
    if resume_existing:
        if (out / "methodology.json").read_bytes() != s.encoded(methodology()):
            raise ValueError("RECOVERY_METHOD_CHANGED")
    else:
        (out / "methodology.json").write_bytes(s.encoded(methodology()))
    with s.read_only(root / "data/replay.db") as db:
        before = s.source_fingerprint(db, RUNS)
    tables = {}
    for year, counts in ((2024, (175680, 4915)), (2025, (160320, 2069))):
        table = (
            load_existing_table(out / f"funding-premium-dataset-{year}.csv")
            if resume_existing
            else build_table(root, year, directories, traded, histories)
        )
        if (table.size, sum(table.numeric["current_buy_eligible"])) != counts:
            raise ValueError("CANONICAL_POPULATION_CHANGED")
        tables[year] = table
    coverage = [r for year, table in tables.items() for r in coverage_rows(table, year)]
    stage_output = verify_existing_rows if resume_existing else write_rows
    stage_output(out / "exact-coverage.csv", coverage)
    eligible = [f for f in FEATURES if coverage_gate(coverage, f)]
    print("COVERAGE_GO_FEATURES", eligible, flush=True)
    # Explicitly complete and persist selection analysis before predictive statistics.
    selection = [
        r for year, table in tables.items() for r in selection_analysis(table, year)
    ]
    stage_output(out / "selection-analysis.csv", selection)
    collected = [[], [], [], [], []]
    for year, table in tables.items():
        if eligible:
            for target, rows in zip(collected, statistics_for(table, year, eligible)):
                target.extend(rows)
    stats, deciles, secondary, controls, concentration = collected
    checks = [
        evidence_screen(
            f, stats, secondary, concentration, selection, coverage_gate(coverage, f)
        )
        for f in FEATURES
    ]
    category, candidate = classify(checks, stats)
    winners = [r for year, table in tables.items() for r in winner_anatomy(table, year)]
    for name, rows in (
        ("feature-statistics.csv", stats),
        ("feature-deciles.csv", deciles),
        ("one-minute-consistency.csv", secondary),
        ("control-comparison.csv", controls),
        ("concentration-sensitivity.csv", concentration),
        (
            "dependency-sensitivity.csv",
            [r for r in stats if r["population"] not in ("ALL_TOP20", "BUY_ELIGIBLE")],
        ),
        ("cross-year-consistency.csv", checks),
        ("winner-anatomy.csv", winners),
    ):
        write_rows(out / name, rows)
    with s.read_only(root / "data/replay.db") as db:
        after = s.source_fingerprint(db, RUNS)
    unchanged = before == after
    current = {p: s.sha(root / p) for p in hashes}
    if not unchanged or current != hashes:
        raise ValueError("SOURCE_CHANGED")
    validation = dict(
        before=before,
        after=after,
        source_fingerprint_unchanged=unchanged,
        input_sha256=hashes,
        source_files_unchanged=current == hashes,
        database_access="mode=ro; PRAGMA query_only=ON",
        production_db_writes=0,
        new_replay=False,
        deployment=False,
    )
    (out / "source-validation.json").write_bytes(s.encoded(validation))
    summary = dict(
        base=BASE,
        category=category,
        hypothesis_candidate_feature=candidate,
        expected_direction="NEGATIVE",
        feature_checks=checks,
        coverage=coverage,
        selection_analysis=selection,
        winner_anatomy=winners,
        source_role="2024_AND_2025_EXPLORATION",
    )
    (out / "summary.json").write_bytes(s.encoded(summary))
    report(out, summary, stats)
    path = package(out)
    print("DONE", category, candidate, str(path), s.sha(path), flush=True)


if __name__ == "__main__":
    main()
