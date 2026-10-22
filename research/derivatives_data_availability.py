"""Bounded official-source availability audit. No features, outcomes or replay.

Live metadata capture is explicit (--capture). Rebuilds reuse saved HTTP evidence.
"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import re
import struct
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests

from research import institutional_signal_edge as edge

BASE = "1d6094750cd0c0dc11534c5e82cb030da3ac7c3d"
NAME = "derivatives-data-availability-v1"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
ARCHIVE = "https://data.binance.vision/"
DOC = "https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data"
FAMILIES = (
    "klines_1h",
    "klines_5m",
    "klines_1m",
    "aggTrades",
    "trades",
    "fundingRate",
    "markPriceKlines_1h",
    "markPriceKlines_5m",
    "indexPriceKlines_1h",
    "indexPriceKlines_5m",
    "premiumIndexKlines_1h",
    "premiumIndexKlines_5m",
    "metrics",
)
SAMPLES = (
    "BTCUSDT",
    "ETHUSDT",
    "FETUSDT",
    "PEOPLEUSDT",
    "FRONTUSDT",
    "NOTUSDT",
    "WIFUSDT",
    "BIOUSDT",
    "PENGUUSDT",
    "PNUTUSDT",
)
REST = {
    "funding": "/fapi/v1/fundingRate",
    "oi": "/futures/data/openInterestHist",
    "global_ls": "/futures/data/globalLongShortAccountRatio",
    "top_accounts": "/futures/data/topLongShortAccountRatio",
    "top_positions": "/futures/data/topLongShortPositionRatio",
    "taker": "/futures/data/takerlongshortRatio",
    "current_oi": "/fapi/v1/openInterest",
}
RETENTION = {
    "oi": "LATEST_1_MONTH",
    "global_ls": "LATEST_30_DAYS",
    "top_accounts": "LATEST_30_DAYS",
    "top_positions": "LATEST_30_DAYS",
    "taker": "LATEST_30_DAYS",
    "current_oi": "CURRENT_ONLY",
    "funding": "HISTORICAL_START_END_SUPPORTED_NO_FIXED_RECENT_ONLY_LIMIT_DOCUMENTED",
}
NS = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}


def stamp(value):
    """UTC ISO or numeric seconds/ms/us; do not copy Spot-2025 units to futures."""
    text = str(value)
    if re.fullmatch(r"\d+(\.\d+)?", text):
        n = float(text)
        return int(n / 1000 if n >= 1e14 else n if n >= 1e11 else n * 1000)
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def iso(t):
    return (
        datetime.fromtimestamp(t / 1000, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
        if t is not None
        else ""
    )


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def key_for(family, symbol, period, frequency="monthly"):
    if not re.fullmatch("[A-Z0-9]+USDT", symbol):
        raise ValueError("Invalid symbol")
    if not re.fullmatch(r"202[45]-\d\d(?:-\d\d)?", period):
        raise ValueError("2024/2025 only")
    parts = family.split("_")
    base = parts[0]
    interval = parts[1] if len(parts) > 1 else None
    return (
        f"data/futures/um/{frequency}/{base}/{symbol}/"
        + (interval + "/" if interval else "")
        + symbol
        + "-"
        + (interval + "-" if interval else base + "-")
        + period
        + ".zip"
    )


def validate_checksum(blob, text):
    expected = text.split()[0].lower()
    if (
        not re.fullmatch("[0-9a-f]{64}", expected)
        or hashlib.sha256(blob).hexdigest() != expected
    ):
        raise ValueError("CHECKSUM_MISMATCH")
    return expected


def known_at(kind, event, decision, publication_lag_ms=None):
    """Boundary SPECIFICATION only; no feature extraction.

    Metrics publication time is unverified: UNKNOWN unless a later study pins lag.
    """
    if kind == "metrics" and publication_lag_ms is None:
        return None
    return (
        event + (publication_lag_ms or 0) <= decision
        if kind == "funding"
        else event + (publication_lag_ms or 0) < decision
    )


class Evidence:
    def __init__(self, out, capture=False):
        self.root = out / "evidence"
        self.root.mkdir(exist_ok=True)
        self.capture = capture
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.local = threading.local()
        self.total = 0
        self.requests = 0
        self.used = set()

    def get(self, url, headers=None, limit=2_000_000):
        if self.stop.is_set():
            raise RuntimeError("CAPTURE_STOPPED")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc not in {
            "s3-ap-northeast-1.amazonaws.com",
            "data.binance.vision",
            "fapi.binance.com",
            "raw.githubusercontent.com",
            "developers.binance.com",
            "api.github.com",
        }:
            raise ValueError("NON_OFFICIAL_SOURCE")
        if (
            parsed.netloc == "fapi.binance.com"
            and parsed.path not in REST.values()
            and parsed.path != "/fapi/v1/exchangeInfo"
        ):
            raise ValueError("NON_MARKET_ENDPOINT")
        spec = dict(url=url, headers=headers or {})
        token = edge.digest(spec)
        path = self.root / (token + ".json.gz")
        with self.lock:
            if path.exists():
                data = json.loads(gzip.decompress(path.read_bytes()))
                if (
                    data["request"] != spec
                    or hashlib.sha256(bytes.fromhex(data["body_hex"])).hexdigest()
                    != data["sha256"]
                ):
                    raise ValueError("Evidence mismatch")
                self.used.add(token)
                return data
        if not self.capture:
            raise ValueError("Missing captured evidence: " + url)
        with self.lock:
            self.requests += 1
            if self.requests > 7000:
                self.stop.set()
                raise ValueError("REQUEST_BUDGET")
        # GET only, no keys, no retries; small controlled metadata concurrency.
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
        with self.local.session.get(
            url, headers=headers, timeout=40, stream=True
        ) as response:
            if response.status_code in (418, 429, 503):
                self.stop.set()
                raise RuntimeError("RATE_LIMIT_STOP " + url)
            if headers and "Range" in headers and response.status_code != 206:
                body = b""
                truncated = True
            else:
                body = b""
                truncated = False
                for block in response.iter_content(65536):
                    if len(body) + len(block) > limit:
                        truncated = True
                        break
                    body += block
            data = dict(
                request=spec,
                status=response.status_code,
                headers={
                    k: v
                    for k, v in response.headers.items()
                    if k.lower()
                    in {
                        "content-length",
                        "content-range",
                        "last-modified",
                        "etag",
                        "content-type",
                    }
                },
                captured_at=datetime.now(timezone.utc).isoformat(),
                body_hex=body.hex(),
                truncated=truncated,
                sha256=hashlib.sha256(body).hexdigest(),
            )
        with self.lock:
            self.total += len(body)
            if self.total > 100_000_000:
                self.stop.set()
                raise RuntimeError("TOTAL_TRANSFER_BUDGET")
            path.write_bytes(gzip.compress(edge.encoded(data), mtime=0))
            self.used.add(token)
        time.sleep(0.05)
        return data

    def listing(self, prefix, marker="", max_keys=1000, delimiter=None):
        params = {"prefix": prefix, "max-keys": max_keys}
        if marker:
            params["marker"] = marker
        if delimiter:
            params["delimiter"] = delimiter
        data = self.get(S3 + "?" + urlencode(params))
        if data["status"] != 200 or data["truncated"]:
            raise ValueError("Listing unavailable")
        root = ET.fromstring(bytes.fromhex(data["body_hex"]))
        rows = []
        for r in root.findall("s:Contents", NS):
            rows.append(
                dict(
                    key=r.findtext("s:Key", namespaces=NS),
                    bytes=int(r.findtext("s:Size", namespaces=NS)),
                    etag=r.findtext("s:ETag", namespaces=NS),
                    last_modified=r.findtext("s:LastModified", namespaces=NS),
                )
            )
        prefixes = [r.text for r in root.findall("s:CommonPrefixes/s:Prefix", NS)]
        return rows, prefixes, root.findtext("s:IsTruncated", namespaces=NS) == "true"

    def sample(self, key):
        response = self.get(ARCHIVE + key, limit=1_000_000)
        if response["status"] != 200 or response["truncated"]:
            return dict(
                key=key,
                status="UNAVAILABLE_OR_OVER_SAMPLE_CAP",
                http=response["status"],
            )
        checksum = self.get(ARCHIVE + key + ".CHECKSUM", limit=4096)
        if checksum["status"] != 200:
            return dict(key=key, status="NO_CHECKSUM")
        blob = bytes.fromhex(response["body_hex"])
        digest = validate_checksum(blob, bytes.fromhex(checksum["body_hex"]).decode())
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            raw = z.read(z.namelist()[0]).decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(raw)))
        return inspect_rows(key, rows) | dict(
            checksum_verified=True, sha256=digest, compressed_bytes=len(blob)
        )


def inspect_rows(key, rows):
    header = rows[0] if rows and not rows[0][0].replace(".", "", 1).isdigit() else []
    data = rows[1:] if header else rows
    family = key.split("/")[4]
    interval = key.split("/")[6] if "Klines" in family or family == "klines" else ""
    if not data:
        return dict(key=key, status="EMPTY_UNKNOWN")
    time_index = 5 if family == "aggTrades" else 4 if family == "trades" else 0
    times = [stamp(r[time_index]) for r in data]
    step = {"1h": 3600000, "5m": 300000, "1m": 60000}.get(
        interval, 300000 if family == "metrics" else None
    )
    expected_period = re.search(r"-(202[45]-\d\d(?:-\d\d)?)\.zip$", key)
    if expected_period and any(
        not iso(t).startswith(expected_period[1]) for t in times
    ):
        raise ValueError("ARCHIVE_TIMESTAMP_OUTSIDE_NAMED_PERIOD")
    differences = [b - a for a, b in zip(times, times[1:])]
    if family == "fundingRate":
        cadence = dict(Counter(differences))
    else:
        cadence = {}
    fields = header or (
        {
            "aggTrades": [
                "agg_trade_id",
                "price",
                "quantity",
                "first_trade_id",
                "last_trade_id",
                "transact_time",
                "is_buyer_maker",
            ],
            "trades": [
                "trade_id",
                "price",
                "quantity",
                "quote_quantity",
                "time",
                "is_buyer_maker",
            ],
        }.get(family, [])
    )
    result = dict(
        key=key,
        status="VERIFIED_ROWS",
        family=family,
        interval=interval,
        rows=len(data),
        first_timestamp=iso(min(times)),
        last_timestamp=iso(max(times)),
        first_ms=min(times),
        last_ms=max(times),
        duplicate_timestamps=len(times) - len(set(times)),
        out_of_order=sum(d < 0 for d in differences),
        missing_intervals=sum(max(0, d // step - 1) for d in differences)
        if step
        else None,
        irregular_intervals=sum(d != step for d in differences) if step else None,
        cadence_ms=cadence,
        columns=fields,
        first_row=data[0],
        last_row=data[-1],
    )
    if family == "klines":
        result["field_count_min"] = min(map(len, data))
        result["required_kline_columns_present"] = result["field_count_min"] >= 11
        anomalies = 0
        for row in data:
            try:
                values = [float(row[i]) for i in (1, 2, 3, 4, 5, 7, 8, 9, 10)]
                if (
                    not all(math.isfinite(v) for v in values)
                    or min(values) < 0
                    or not float(row[3])
                    <= min(float(row[1]), float(row[4]))
                    <= max(float(row[1]), float(row[4]))
                    <= float(row[2])
                ):
                    anomalies += 1
            except (ValueError, IndexError):
                anomalies += 1
        result["invalid_ohlc_or_volume_rows"] = anomalies
        result["last_close_ms"] = stamp(data[-1][6])
        result["first_close_ms"] = stamp(data[0][6])
    if family == "metrics":
        result["empty_metric_values"] = sum(
            v.lower() in ("", "nan", "null") for row in data for v in row[2:]
        )
    if family in ("aggTrades", "trades"):
        result["buyer_maker_present"] = all(
            r[6 if family == "aggTrades" else 5].lower() in ("true", "false")
            for r in data
        )
        ids = [int(r[0]) for r in data]
        result["duplicate_ids"] = len(ids) - len(set(ids))
        result["id_gaps"] = sum(b - a > 1 for a, b in zip(ids, ids[1:]))
    return result


def inventory(ev, family, symbol, periods=None):
    base, *interval = family.split("_")
    frequency = "daily" if base == "metrics" else "monthly"
    prefix = f"data/futures/um/{frequency}/{base}/{symbol}/" + (
        interval[0] + "/" if interval else ""
    )
    stem = symbol + "-" + (interval[0] + "-" if interval else base + "-")
    rows = []
    if frequency == "daily":
        for period in periods if periods is not None else ("2024", "2025"):
            more, _, truncated = ev.listing(prefix + stem + str(period))
            if truncated:
                raise ValueError("Unexpected daily year pagination")
            rows += more
    else:
        more, _, truncated = ev.listing(
            prefix, marker=prefix + stem + "2024", max_keys=100
        )
        rows += [r for r in more if re.search(r"-202[45]-", r["key"])]
        if truncated and (
            not more or prefix + stem + "2025-12.zip.CHECKSUM" > more[-1]["key"]
        ):
            raise ValueError("Unresolved archive pagination")
    checksums = {r["key"][:-9] for r in rows if r["key"].endswith(".CHECKSUM")}
    return [
        r | dict(family=family, symbol=symbol, checksum_available=r["key"] in checksums)
        for r in rows
        if r["key"].endswith(".zip")
    ]


def load_population(root):
    prior = root / "artifacts/cross-sectional-market-structure-v1"
    manifest = json.loads((prior / "manifest.json").read_text())
    population = {}
    hashes = {}
    for year, n, buy in ((2024, 175680, 4915), (2025, 160320, 2069)):
        path = prior / f"market-structure-dataset-{year}.csv"
        if sha(path) != manifest["files"][path.name]["sha256"]:
            raise ValueError("Source CSV checksum changed")
        hashes[year] = sha(path)
        # Only identity/eligibility. Never read outcome values into this audit.
        rows = []
        with path.open() as f:
            for row in csv.DictReader(f):
                rows.append(
                    (
                        row["symbol"],
                        stamp(row["timestamp"]),
                        row["current_buy_eligible"] == "1.0",
                    )
                )
        if len(rows) != n or sum(r[2] for r in rows) != buy:
            raise ValueError("Unexpected population")
        population[year] = rows
    return population, hashes


def mapping(symbol, archive_symbols, current=None):
    # Exact identifiers only: no rebrand stitching, multiplication or fuzzy mapping.
    if symbol not in archive_symbols:
        return "", "UNMAPPED_NO_EXACT_ARCHIVE_SYMBOL"
    return symbol, "EXACT_ID_ARCHIVE_EVIDENCE_REQUIRED_PER_TIMESTAMP"


def covered(t, objects, bounds=None):
    """Archive-presence upper bound; bounds are authoritative when parsed.

    Monthly presence alone is NOT proof of intra-month rows or past listing.
    """
    when = datetime.fromtimestamp((t - 3600000) / 1000, timezone.utc)
    date = when.strftime("%Y-%m-%d")
    month = date[:7]
    keys = [
        r
        for r in objects
        if r["key"].endswith("-" + month + ".zip")
        or r["key"].endswith("-" + date + ".zip")
    ]
    if not keys:
        return False
    if bounds:
        lo, hi = bounds
        return lo <= t - 3600000 <= hi
    return None  # UNKNOWN intrafile coverage, never convert to a zero observation


def write_rows(path, rows):
    keys = sorted({k for r in rows for k in r})
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in rows:
            w.writerow(
                {
                    k: json.dumps(v, sort_keys=True)
                    if isinstance(v, (dict, list))
                    else v
                    for k, v in row.items()
                }
            )


def package(out):
    files = sorted(
        p
        for p in out.rglob("*")
        if p.is_file() and p.suffix != ".zip" and p.name != "manifest.json"
    )
    manifest = dict(
        base=BASE,
        script_sha256=sha(__file__),
        files={
            str(p.relative_to(out)): dict(bytes=p.stat().st_size, sha256=sha(p))
            for p in files
        },
    )
    (out / "manifest.json").write_bytes(edge.encoded(manifest))
    target = out / (NAME + ".zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(files + [out / "manifest.json"]):
            info = zipfile.ZipInfo(str(p.relative_to(out)), (2024, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, p.read_bytes())
    return target


def boundary_samples(ev, index):
    """Two small 1h files per mapped instrument, never a full history download.

    Confirm first/last observed archive rows within 2024/2025. Missing intermediate
    records remain unknown, and a delist is not inferred merely from data ending.
    """
    jobs = []
    for (family, symbol), objects in sorted(index.items()):
        if family != "klines_1h" or not objects:
            continue
        ordered = sorted(objects, key=lambda r: r["key"])
        for obj in {
            ordered[0]["key"]: ordered[0],
            ordered[-1]["key"]: ordered[-1],
        }.values():
            if obj["bytes"] <= 80000:
                jobs.append(obj["key"])
    result = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for i, row in enumerate(pool.map(ev.sample, jobs)):
            result.append(row)
            if i % 100 == 0:
                print("Instrument boundary samples", i, "/", len(jobs), flush=True)
    return result


def compare_funding_times(archive_rows, rest_rows):
    """Compare timestamps/rates only, never create a signal or backfill an event."""
    nominal = {stamp(r[0]): r for r in archive_rows if r and r[0].isdigit()}
    comparisons = []
    for event in rest_rows:
        actual = int(event["fundingTime"])
        hour = actual // 3600000 * 3600000
        row = nominal.get(actual) or nominal.get(hour)
        comparisons.append(
            dict(
                actual_rest_event_ms=actual,
                archive_calc_ms=stamp(row[0]) if row else None,
                timestamp_offset_ms=actual - stamp(row[0]) if row else None,
                event_offset_from_hour_ms=actual - hour,
                rest_rate=event["fundingRate"],
                archive_rate=row[2] if row else None,
                archive_exact_event_time_safe=bool(row and actual == stamp(row[0])),
            )
        )
    return comparisons


def funding_alignment(ev, out):
    controls = json.loads((out / "rest-control-samples.json").read_text())
    result = []
    for symbol in SAMPLES[:4]:
        for year in (2024, 2025):
            key = key_for("fundingRate", symbol, f"{year}-06")
            sample = ev.sample(key)
            if sample["status"] != "VERIFIED_ROWS":
                result.append(
                    dict(symbol=symbol, year=year, status="UNCONFIRMED", key=key)
                )
                continue
            response = ev.get(ARCHIVE + key, limit=1000000)
            with zipfile.ZipFile(io.BytesIO(bytes.fromhex(response["body_hex"]))) as z:
                rows = list(
                    csv.reader(io.StringIO(z.read(z.namelist()[0]).decode("utf-8-sig")))
                )
            rest = next(
                r
                for r in controls
                if r["symbol"] == symbol
                and r["year"] == year
                and r["endpoint"] == "funding"
            )
            comparisons = compare_funding_times(rows, json.loads(rest["body"]))
            result.append(
                dict(
                    symbol=symbol,
                    year=year,
                    key=key,
                    status="CHECKSUM_VERIFIED_ALIGNMENT_CONTROL",
                    comparisons=comparisons,
                )
            )
    (out / "funding-time-alignment.json").write_bytes(edge.encoded(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    out = root / "artifacts" / NAME
    out.mkdir(parents=True, exist_ok=True)
    ev = Evidence(out, args.capture)
    population, hashes = load_population(root)
    symbols = sorted({r[0] for rows in population.values() for r in rows})
    with edge.read_only(root / "data/replay.db") as db:
        before = edge.source_fingerprint(db, list(edge.RUNS.values()))
    layout = {}
    for freq in ("monthly", "daily"):
        _, prefixes, _ = ev.listing(f"data/futures/um/{freq}/", delimiter="/")
        layout[freq] = prefixes
    archive_symbols = set()
    marker = ""
    while True:
        _, prefixes, truncated = ev.listing(
            "data/futures/um/monthly/klines/", marker=marker, delimiter="/"
        )
        archive_symbols.update(p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes)
        if not truncated:
            break
        if not prefixes or prefixes[-1] <= marker:
            raise ValueError("Invalid symbol pagination")
        marker = prefixes[-1]
    maps = {s: mapping(s, archive_symbols) for s in symbols}
    mapped = sorted({m[0] for m in maps.values() if m[0]})
    print("Population symbols", len(symbols), "exact mappings", len(mapped), flush=True)
    all_objects = []
    cohort_months = defaultdict(set)
    for rows in population.values():
        for symbol, timestamp, buy in rows:
            month = iso(timestamp - 3600000)[:7]
            if month.startswith(("2024", "2025")):
                cohort_months[symbol].add(month)
    jobs = [
        (f, s, sorted(cohort_months[s]) if f == "metrics" else None)
        for f in FAMILIES
        for s in mapped
    ]
    with ThreadPoolExecutor(max_workers=3) as pool:
        for i, result in enumerate(pool.map(lambda j: inventory(ev, *j), jobs)):
            all_objects += result
            if i % 100 == 0:
                print(
                    "Inventories",
                    i,
                    "/",
                    len(jobs),
                    "objects",
                    len(all_objects),
                    flush=True,
                )
    write_rows(out / "archive-objects.csv", all_objects)
    layout["source"] = S3
    layout["unresolved_multiplier_candidates"] = {
        s: [
            candidate
            for candidate in ("1000" + s, "1000000" + s)
            if candidate in archive_symbols
        ]
        for s in symbols
        if not maps[s][0]
    }
    layout["premium_name"] = (
        "Official inspected root is premiumIndexKlines, not premiumPriceKlines"
    )
    layout["archive_symbols"] = sorted(archive_symbols)
    layout["mapping_policy"] = (
        "Exact only; alias candidates are unresolved and excluded from strict estimates; today exchangeInfo never establishes historical listing."
    )
    (out / "archive-layout.json").write_bytes(edge.encoded(layout))
    index = defaultdict(list)
    for obj in all_objects:
        index[(obj["family"], obj["symbol"])].append(obj)
    samples = boundary_samples(ev, index)
    # Bounded representative full files, all checksum-verified. First and last
    # object months validate listing/delisting boundaries only in this sample.
    for s in SAMPLES:
        if s not in mapped:
            continue
        for year in (2024, 2025):
            for family in (
                "klines_1h",
                "fundingRate",
                "markPriceKlines_1h",
                "indexPriceKlines_1h",
                "premiumIndexKlines_1h",
                "metrics",
            ):
                objs = sorted(
                    (r for r in index[family, s] if f"-{year}-" in r["key"]),
                    key=lambda r: r["key"],
                )
                if not objs:
                    continue
                chosen = [objs[0]] + (
                    [objs[-1]] if family == "klines_1h" and len(objs) > 1 else []
                )
                for obj in chosen:
                    if obj["bytes"] <= 1_000_000:
                        samples.append(ev.sample(obj["key"]))
            for interval in ("1m", "5m"):
                if s not in SAMPLES[:4]:
                    continue
                key = key_for("klines_" + interval, s, f"{year}-06-01", "daily")
                samples.append(ev.sample(key))
    # Large trade archives: only first 64KiB, never download ignored Range bodies.
    for s in SAMPLES[:4]:
        for year in (2024, 2025):
            for family in ("aggTrades", "trades"):
                key = key_for(family, s, f"{year}-06-01", "daily")
                response = ev.get(
                    ARCHIVE + key, headers={"Range": "bytes=0-65535"}, limit=65536
                )
                row = dict(key=key, status="RANGE_UNAVAILABLE", http=response["status"])
                if response["status"] == 206:
                    blob = bytes.fromhex(response["body_hex"])
                    if blob[:4] == b"PK\x03\x04":
                        method = struct.unpack_from("<H", blob, 8)[0]
                        name, extra = struct.unpack_from("<HH", blob, 26)
                        payload = blob[30 + name + extra :]
                        raw = (
                            zlib.decompressobj(-15).decompress(payload, 1_000_000)
                            if method == 8
                            else payload
                        )
                        lines = raw.decode("utf-8-sig", errors="replace").splitlines()[
                            :-1
                        ]
                        row = inspect_rows(key, list(csv.reader(lines))) | dict(
                            status="PREFIX_RANGE_ONLY",
                            checksum_verified=False,
                            range_bytes=len(blob),
                            warning="Not complete ZIP; whole-file SHA not validated; no last timestamp/whole-day continuity claim",
                        )
                samples.append(row)
    samples = list({r["key"]: r for r in samples}.values())
    write_rows(out / "sample-validation.csv", samples)
    rest_samples = []
    for s in SAMPLES:
        for year in (2024, 2025):
            start = stamp(f"{year}-06-01T00:00:00Z")
            url = (
                "https://fapi.binance.com"
                + REST["funding"]
                + "?"
                + urlencode(
                    dict(symbol=s, startTime=start, endTime=start + 86400000, limit=10)
                )
            )
            response = ev.get(url, limit=20000)
            rest_samples.append(
                dict(
                    symbol=s,
                    year=year,
                    endpoint="funding",
                    status=response["status"],
                    body=bytes.fromhex(response["body_hex"]).decode(errors="replace"),
                )
            )
    for endpoint, path in REST.items():
        if endpoint in ("funding", "current_oi"):
            continue
        for year in (2024, 2025):
            start = stamp(f"{year}-06-01T00:00:00Z")
            response = ev.get(
                "https://fapi.binance.com"
                + path
                + "?"
                + urlencode(
                    dict(
                        symbol="BTCUSDT",
                        period="5m",
                        startTime=start,
                        endTime=start + 86400000,
                        limit=2,
                    )
                ),
                limit=20000,
            )
            rest_samples.append(
                dict(
                    symbol="BTCUSDT",
                    year=year,
                    endpoint=endpoint,
                    status=response["status"],
                    body=bytes.fromhex(response["body_hex"]).decode(errors="replace"),
                )
            )
    confirmed_rest = []
    for row in rest_samples:
        if row["endpoint"] != "funding" or row["status"] != 200:
            continue
        data = json.loads(row["body"])
        if isinstance(data, list) and data:
            start = stamp(f"{row['year']}-06-01T00:00:00Z")
            if not all(
                start <= int(x["fundingTime"]) <= start + 86400000 for x in data
            ):
                raise ValueError("REST_FUNDING_IGNORED_HISTORICAL_WINDOW")
            confirmed_rest.append((row["symbol"], row["year"]))
    if not all(
        (symbol, year) in confirmed_rest
        for symbol in SAMPLES[:4]
        for year in (2024, 2025)
    ):
        raise ValueError("Required historical funding controls unconfirmed")
    (out / "rest-control-samples.json").write_bytes(edge.encoded(rest_samples))
    funding_alignment(ev, out)
    docs = {}
    for name, url in {
        "public_data_readme": "https://raw.githubusercontent.com/binance/binance-public-data/master/README.md",
        "market_data_docs": DOC,
    }.items():
        response = ev.get(url, limit=2_000_000)
        docs[name] = dict(url=url, sha256=response["sha256"], status=response["status"])
    retention = {
        k: dict(
            endpoint=REST[k],
            retention=val,
            rest_classification="RECENT_ONLY"
            if k != "funding"
            else "HISTORICAL_PUBLIC_MARKET_DATA",
            source=DOC,
            historical_rest_status="REST_NOT_HISTORICALLY_AVAILABLE"
            if k not in ("funding",)
            else "CONFIRMED_BY_SMALL_CONTROLS",
            archive_alternative="daily/metrics"
            if k in ("oi", "global_ls", "top_accounts", "top_positions", "taker")
            else "monthly/fundingRate"
            if k == "funding"
            else None,
        )
        for k, val in RETENTION.items()
    }
    (out / "retention-limits.json").write_bytes(edge.encoded(retention))
    assemble(out, population, maps, index, samples, layout)
    with edge.read_only(root / "data/replay.db") as db:
        after = edge.source_fingerprint(db, list(edge.RUNS.values()))
    if before != after:
        raise ValueError("Source tables/run inventory changed")
    if any(
        sha(
            root
            / "artifacts/cross-sectional-market-structure-v1"
            / f"market-structure-dataset-{y}.csv"
        )
        != h
        for y, h in hashes.items()
    ):
        raise ValueError("Source CSV changed")
    (out / "source-validation.json").write_bytes(
        edge.encoded(
            dict(
                before=before,
                after=after,
                source_fingerprint_unchanged=True,
                input_sha256=hashes,
                read_only_uri=True,
                query_only=True,
                database_writes=0,
                new_replay=False,
                production_behavior_changed=False,
                deployment=False,
                docs=docs,
                network="Official GET only; bounded metadata/samples, never authenticated or order endpoints",
                evidence_record_count=len(ev.used),
                captured_response_bytes=sum(
                    len(
                        bytes.fromhex(
                            json.loads(
                                gzip.decompress(
                                    (ev.root / (token + ".json.gz")).read_bytes()
                                )
                            )["body_hex"]
                        )
                    )
                    for token in ev.used
                ),
                used_evidence=sorted(ev.used),
            )
        )
    )
    target = package(out)
    print("DONE", target, sha(target), flush=True)


def assemble(out, population, maps, index, samples, layout):
    sample_index = defaultdict(list)
    for s in samples:
        if s["status"] == "VERIFIED_ROWS":
            sample_index[s["key"].split("/")[5]].append(s)
    instrument_bounds = {}
    verified_spans = defaultdict(list)
    for symbol, evidence in sample_index.items():
        bars = [
            r for r in evidence if r["family"] == "klines" and r["interval"] == "1h"
        ]
        if bars:
            instrument_bounds[symbol] = (
                min(r["first_ms"] for r in bars),
                max(r["last_ms"] for r in bars),
            )
            verified_spans[symbol] = [
                (r["first_ms"], r["last_ms"])
                for r in bars
                if r["missing_intervals"] == 0
                and r["irregular_intervals"] == 0
                and r["duplicate_timestamps"] == 0
            ]
    funding_post_price_coverage = []
    controls_path = out / "rest-control-samples.json"
    if controls_path.exists():
        for control in json.loads(controls_path.read_text()):
            if (
                control["endpoint"] != "funding"
                or control["status"] != 200
                or control["symbol"] not in instrument_bounds
            ):
                continue
            events = json.loads(control["body"])
            last = instrument_bounds[control["symbol"]][1]
            outside = [r for r in events if int(r["fundingTime"]) > last + 3600000]
            if outside:
                funding_post_price_coverage.append(
                    dict(
                        symbol=control["symbol"],
                        year=control["year"],
                        last_confirmed_trade_kline=iso(last),
                        events=outside,
                        interpretation="Funding events exceed confirmed traded-price archive coverage; neither live contract nor fillable market inferred. No backfill.",
                    )
                )
    (out / "cross-family-anomalies.json").write_bytes(
        edge.encoded(funding_post_price_coverage)
    )
    matrix = []
    symbol_rows = []
    storage = {}
    for family in FAMILIES:
        family_stats = {}
        for year, rows in population.items():
            by_symbol = defaultdict(list)
            for s, t, buy in rows:
                by_symbol[s].append((t, buy))
            total = Counter()
            listed_symbols = 0
            days = set()
            covered_days = set()
            for s, observations in sorted(by_symbol.items()):
                fs, reason = maps[s]
                objects = index[family, fs] if fs else []
                eligible = [o for o in objects if f"-{year}-" in o["key"]]
                if eligible:
                    listed_symbols += 1
                local = Counter()
                for t, buy in observations:
                    day = iso(t)[:10]
                    days.add((s, day))
                    presence = covered(t, objects)
                    within_bounds = (
                        fs not in instrument_bounds
                        or instrument_bounds[fs][0]
                        <= t - 3600000
                        <= instrument_bounds[fs][1]
                    )
                    if family == "klines_1h" and any(
                        lo <= t - 3600000 <= hi for lo, hi in verified_spans[fs]
                    ):
                        total["sample_verified_rows"] += 1
                        local["sample_verified_rows"] += 1
                        if buy:
                            total["sample_verified_buy_rows"] += 1
                    if presence is None and within_bounds:
                        total["monthly_or_daily_presence_upper"] += 1
                        local["presence_upper"] += 1
                        covered_days.add((s, day))
                        if buy:
                            total["buy_presence_upper"] += 1
                            local["buy_presence_upper"] += 1
                    else:
                        total["no_matching_object"] += 1
                    total["all"] += 1
                    total["buy"] += int(buy)
                evidence = [
                    r
                    for r in sample_index[fs]
                    if r["family"] == family.split("_")[0]
                    and f"-{year}-" in r["key"]
                    and ("_" not in family or r["interval"] == family.split("_")[1])
                ]
                symbol_rows.append(
                    dict(
                        year=year,
                        family=family,
                        spot_symbol=s,
                        futures_symbol=fs,
                        mapping_status=reason,
                        top20_rows=len(observations),
                        buy_rows=sum(b for t, b in observations),
                        archive_objects=len(eligible),
                        instrument_first_confirmed_data=iso(
                            instrument_bounds.get(fs, (None, None))[0]
                        ),
                        instrument_last_confirmed_data=iso(
                            instrument_bounds.get(fs, (None, None))[1]
                        ),
                        sampled_verified_1h_rows=local["sample_verified_rows"]
                        if family == "klines_1h"
                        else "UNKNOWN",
                        listing_or_delisting_instant="UNKNOWN; first/last archive data are not legal listing/delist announcement timestamps",
                        archive_presence_upper_rows=local["presence_upper"],
                        buy_presence_upper_rows=local["buy_presence_upper"],
                        earliest_confirmed_timestamp=min(
                            (e["first_timestamp"] for e in evidence), default=""
                        ),
                        latest_confirmed_timestamp=max(
                            (e["last_timestamp"] for e in evidence), default=""
                        ),
                        historical_row_coverage="UNKNOWN_NOT_ALL_FILES_PARSED",
                        listing_evidence="Sample actual rows only; archive filenames elsewhere only upper bound",
                        alias_policy="No heuristic/rebrand stitching",
                        sampled_duplicate_timestamps=sum(
                            e["duplicate_timestamps"] for e in evidence
                        ),
                        sampled_missing_intervals=sum(
                            e["missing_intervals"] or 0 for e in evidence
                        ),
                    )
                )
            size = sum(
                o["bytes"]
                for (f, s), objs in index.items()
                if f == family
                for o in objs
                if f"-{year}-" in o["key"]
            )
            family_stats[year] = dict(
                all_rows=total["all"],
                buy_rows=total["buy"],
                symbol_count=len(by_symbol),
                symbols_with_archive=listed_symbols,
                top20_presence_upper_pct=100
                * total["monthly_or_daily_presence_upper"]
                / total["all"],
                buy_presence_upper_pct=100 * total["buy_presence_upper"] / total["buy"],
                symbol_day_presence_upper_pct=100 * len(covered_days) / len(days),
                sample_verified_1h_row_lower_pct=100
                * total["sample_verified_rows"]
                / total["all"]
                if family == "klines_1h"
                else None,
                sample_verified_1h_buy_lower_pct=100
                * total["sample_verified_buy_rows"]
                / total["buy"]
                if family == "klines_1h"
                else None,
                strict_actual_row_coverage="UNKNOWN_REQUIRES_FULL_ROW_INGESTION",
                compressed_archive_bytes=size,
            )
            confirmed = [
                e
                for e in samples
                if e["status"] == "VERIFIED_ROWS"
                and e["family"] == family.split("_")[0]
                and f"-{year}-" in e["key"]
                and ("_" not in family or e["interval"] == family.split("_")[1])
            ]
            matrix.append(
                dict(
                    year=year,
                    family=family,
                    availability="PARTIAL" if listed_symbols else "NO",
                    classification="PARTIAL_OR_COVERAGE_LIMITED"
                    if listed_symbols
                    else "NOT_AVAILABLE_FOR_2024_2025",
                    official_source=ARCHIVE + "?prefix=data/futures/um/",
                    granularity=family.rsplit("_", 1)[-1]
                    if "_" in family
                    else "5m snapshots"
                    if family == "metrics"
                    else "events",
                    earliest_confirmed_timestamp=min(
                        (e["first_timestamp"] for e in confirmed), default=""
                    ),
                    latest_confirmed_timestamp=max(
                        (e["last_timestamp"] for e in confirmed), default=""
                    ),
                    checksum="Listed .CHECKSUM; verified only complete small samples",
                    offline_reconstruction="YES_IF_EXACT_RAW_BYTES_AND_CHECKSUM_PINNED",
                    limitations="Presence upper bounds, not actual timestamp coverage; exact-name mappings only; no implicit listing-date fill; publication-time lag unknown for metrics",
                    **family_stats[year],
                )
            )
        storage[family] = family_stats
    # Metrics columns are separately classified from REST retention, not discarded.
    for family, columns in {
        "open_interest": ["sum_open_interest", "sum_open_interest_value"],
        "long_short_ratios": [
            "count_long_short_ratio",
            "count_toptrader_long_short_ratio",
            "sum_toptrader_long_short_ratio",
        ],
        "taker_metric": ["sum_taker_long_short_vol_ratio"],
    }.items():
        for year in (2024, 2025):
            base = next(
                r for r in matrix if r["family"] == "metrics" and r["year"] == year
            )
            matrix.append(
                base
                | dict(
                    family=family,
                    archive_fields=columns,
                    rest_status="REST_NOT_HISTORICALLY_AVAILABLE",
                    limitations="Official metrics archive exists; verify snapshot publication lag and interval convention before decision-time use. Taker archive ratio does not itself give absolute buy/sell volumes.",
                )
            )
    for year in (2024, 2025):
        matrix.append(
            dict(
                year=year,
                family="liquidations",
                availability="NO",
                classification="NOT_AVAILABLE_FOR_2024_2025",
                official_source=S3,
                limitations="No liquidation family in inspected official USD-M monthly/daily roots; authenticated personal force-orders are not a public historical market dataset. No third-party source used.",
            )
        )
    write_rows(out / "symbol-coverage.csv", symbol_rows)
    write_rows(out / "data-family-matrix.csv", matrix)
    (out / "estimated-storage.json").write_bytes(
        edge.encoded(
            dict(
                scope="SUM of listed compressed archives for exact-mapped cohort symbols in 2024/2025. Metrics: only months intersecting canonical cohort hours. Other families: all listed months in both years. Separate intervals overlap; metrics-derived families share files. Excludes aliases; not entire exchange size.",
                families=storage,
                uncompressed_estimate="NOT_EXTRAPOLATED_FROM_TINY_NONRANDOM_SAMPLES",
            )
        )
    )
    methodology = dict(
        base=BASE,
        scope="Availability only; no outcome/feature/model/strategy/replay analysis",
        population="Manifest-verified Top20 identity and BUY flags only; no future outcome used",
        mapping="Exact official archive identifiers only. Same name is a candidate, not historical coverage. No automatic 1000x, rename or rebrand stitching. Such cases remain unresolved, explicitly limiting coverage.",
        coverage="Archive object presence is an UPPER BOUND for each prior closed-hour decision row, NOT actual coverage. No filename proves listing within a month, no first-month backfill. Only sampled actual timestamps are authoritative first/last confirmation. Missing archive is UNKNOWN, not zero market observation.",
        ready="All families with archives are B until per-timestamp ingestion and gap/publication audit; candidate may be nominated on substantial reproducible archive coverage, never on predictive results.",
        no_lookahead=dict(
            klines="close_time < decision",
            aggTrades="trade timestamp < decision; isBuyerMaker=true means aggressive sell, false aggressive buy",
            funding="Unrounded actual fundingTime/calc_time <= decision. Same-event timestamp alignment is checked in funding-time-alignment controls, including millisecond offsets from scheduled hours. Never floor timestamps to a nominal hour or use next scheduled rate.",
            metrics="Create_time is snapshot label, not demonstrated publication time. Until publication lag/interval end semantics are pinned, usable-at-decision is UNKNOWN. Conservative prior interval plus documented lag would require a separate study.",
            mark_index_premium="completed bar close_time < decision; mark/index/premium bars are price references, not traded volume",
        ),
        revision="Binance documents archive revisions. Save URL, capture time, LastModified, SHA256/checksum and bytes. ETag is not SHA256. Historical capture cannot prove original real-time publication vintage.",
        limits=dict(
            concurrent_requests=3,
            single_sample_cap_bytes=1000000,
            instrument_boundary_file_cap_bytes=80000,
            max_boundary_files_per_instrument=2,
            trade_range_bytes=65536,
            total_new_response_cap_bytes=100000000,
            no_retry_on_rate_limit=True,
            no_bulk_history=True,
        ),
        source_docs=[DOC, "https://github.com/binance/binance-public-data"],
    )
    (out / "methodology.json").write_bytes(edge.encoded(methodology))
    # Strict data-availability choice: use raw taker fields in official klines;
    # ratios/metrics carry unresolved historical publication-time semantics.
    k = [r for r in matrix if r["family"] == "klines_1h"]
    candidate = (
        "FUTURES_TAKER_FLOW"
        if len(k) == 2
        and all(
            r["top20_presence_upper_pct"] >= 70 and r["buy_presence_upper_pct"] >= 70
            for r in k
        )
        and any(e.get("required_kline_columns_present") for e in samples)
        else "NONE"
    )
    summary = dict(
        base=BASE,
        next_research_candidate=candidate,
        selection_basis="DATA_QUALITY_COVERAGE_REPRODUCIBILITY_ONLY; official kline columns and closed-time semantics; NOT predictive edge. Candidate conditional on strict symbol-mapping and full row/gap verification.",
        matrix=matrix,
        population={
            y: dict(
                top20_rows=len(r),
                buy_rows=sum(x[2] for x in r),
                symbols=len({x[0] for x in r}),
            )
            for y, r in population.items()
        },
        exact_mapping_symbols=sum(bool(v[0]) for v in maps.values()),
        unresolved_spot_symbols=sorted(s for s, v in maps.items() if not v[0]),
        sample_count=len(samples),
        cross_family_anomalies=funding_post_price_coverage,
        classification_note="B classifications are deliberate: metadata presence estimates must not be mistaken for proven candle/snapshot row coverage.",
    )
    (out / "summary.json").write_bytes(edge.encoded(summary))
    lines = [
        "# Derivatives Historical Data Availability Audit v1",
        "",
        "No features, outcome research, strategy, replay or deployment.",
        "",
        "## Coverage",
        "",
        "All percentages below are archive-presence UPPER BOUNDS, not proven actual decision-time coverage. Exact-name mappings only; unresolved aliases excluded. First/last sampled rows are in sample-validation and symbol-coverage.",
        "",
        "| Family | Year | ALL upper % | BUY upper % | Classification |",
        "|---|---:|---:|---:|---|",
    ]
    for r in matrix:
        lines.append(
            f"| {r['family']} | {r['year']} | {r.get('top20_presence_upper_pct', 'N/A')} | {r.get('buy_presence_upper_pct', 'N/A')} | {r['classification']} |"
        )
    lines += [
        "",
        "## Principal findings",
        "",
        "Official daily metrics contains historical OI and global/top account/top position long-short ratios, plus taker ratio. Short REST retention does not erase these official archive datasets. Publication lag and interval-label semantics remain unverified; snapshots are not ready to use at an hourly decision merely because their timestamp matches it.",
        "",
        "Funding: public REST 2024/2025 controls are recorded, and monthly official archives are inventoried. Funding cadence is measured in actual samples, not assumed universally 8h.",
        "",
        "Trades/aggTrades: large files are metadata-only and bounded ZIP prefixes. Such prefixes cannot validate whole-file checksum, end timestamp or whole-day continuity. isBuyerMaker supports aggressor side; simultaneous trades may share timestamps, so duplicate IDs matter more than duplicate times.",
        "",
        "No official USD-M liquidation family was found in both enumerated roots. This is an audit of the inspected official archive, not proof that no Binance product could ever offer such data.",
        "",
        "## Funding timing / source contradictions",
        "",
        "See funding-time-alignment.json for checksum-verified same-month archive vs REST event-time controls. Controls compare exact archive calc_time with actual REST fundingTime, preserving positive millisecond offsets from the nominal hour. An event at t+1ms is not known at t; do not floor or truncate either source timestamp. Exact event alignment in samples does not establish original network publication latency. cross-family-anomalies.json records funding responses beyond confirmed traded-price coverage; those events do not prove an active fillable contract.",
        "",
        "## Mapping and gaps",
        "",
        "Today exchangeInfo is never historical listing proof. Exact spot/futures names are merely mapping candidates until actual records exist. Listing/delisting exact instants for unsampled symbols are UNKNOWN; archive filenames must not expand partial months to full coverage. Absence and unavailable HTTP data never become zero flow/OI. Rebrands and 1000x contracts need separate official identity evidence; no guessed continuity.",
        "",
        "## Next research candidate",
        "",
        candidate,
        "",
        summary["selection_basis"],
        "",
        "## Provenance and limits",
        "",
        "Raw response evidence includes URL, status, capture time, headers and body SHA. Full samples are checksum verified. Metadata and small samples only; all listing counts and source populations are reproducible from saved evidence without network. No future outcome columns are used. The data-family-matrix distinguishes recent-only REST from historical metrics archives. REST control errors are evidence, not silently normalized to no market data.",
        "",
        "See methodology.json for timestamp boundaries, transfer limits and coverage definitions; estimated-storage.json gives exact listed compressed sizes, not a speculative raw-data extrapolation.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
