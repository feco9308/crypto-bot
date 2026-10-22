"""Three frozen futures flow features, read-only exploration; never a replay.

First explicit --download captures required official 1h ZIPs; default is offline.
"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import re
import statistics as st
import threading
import zipfile
from array import array
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import requests

from research import cross_sectional_market_structure as cross
from research import derivatives_data_availability as audit
from research import institutional_signal_edge as edge

BASE = "bea87374a167fc27409c883a08707e92151db5ae"
NAME = "futures-taker-flow-v1"
HOUR = edge.HOUR
FLOW = ("taker_buy_share_1h", "taker_buy_share_4h", "taker_buy_share_24h")
WINDOWS = (1, 4, 24)
CONTROLS = cross.CONTROLS
OUTCOMES = tuple(
    cross.LABEL_PREFIX + f"{k}_{h}h"
    for h in (24, 72, 168)
    for k in ("close_return_pct", "mfe_atr", "mae_atr")
)
PRIMARY = cross.PRIMARY_STATS
SECONDARY = (cross.SECONDARY[2], cross.SECONDARY[3])
NAN = float("nan")
MIN_N, MIN_CLIFF, MIN_RHO = 30, 0.147, 0.05
WINNERS = (
    ("FETUSDT", "2024-02-24T12:00:00Z"),
    ("ETHUSDT", "2024-02-20T16:00:00Z"),
    ("PEOPLEUSDT", "2024-05-31T01:00:00Z"),
)


def key_for(symbol, month):
    if not re.fullmatch("[A-Z0-9]+USDT", symbol) or not re.fullmatch(
        r"(202[45]-\d\d|2023-12)", month
    ):
        raise ValueError("Invalid symbol/month or out-of-scope archive")
    return f"data/futures/um/monthly/klines/{symbol}/1h/{symbol}-1h-{month}.zip"


def months_needed(symbol, t):
    return {key_for(symbol, edge.at(t - h * HOUR).strftime("%Y-%m")) for h in (1, 24)}


def official_url(key):
    if not re.fullmatch(
        r"data/futures/um/monthly/klines/([A-Z0-9]+USDT)/1h/\1-1h-(202[45]-\d\d|2023-12)\.zip",
        key,
    ):
        raise ValueError("ONLY_OFFICIAL_USDM_1H_ALLOWED")
    return audit.ARCHIVE + key


class Cache:
    """Content-addressed research-only files. Never touches the replay cache/DB."""

    def __init__(self, root, download=False):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        (root / "objects").mkdir(exist_ok=True)
        (root / "keys").mkdir(exist_ok=True)
        self.download = download
        self.local = threading.local()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.network_bytes = 0
        self.requests = 0

    def metadata_path(self, key):
        official_url(key)
        return self.root / "keys" / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def read(self, key):
        path = self.metadata_path(key)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        if data["key"] != key or data["url"] != official_url(key):
            raise ValueError("Cache key mismatch")
        if data["status"] == "COMPLETE":
            blob = (self.root / "objects" / (data["sha256"] + ".zip")).read_bytes()
            audit.validate_checksum(blob, data["sha256"])
            if len(blob) != data["bytes"]:
                raise ValueError("Cache size mismatch")
        return data

    def save(self, key, blob, expected, source):
        digest = audit.validate_checksum(blob, expected)
        obj = self.root / "objects" / (digest + ".zip")
        if obj.exists() and cross.sha(obj) != digest:
            raise ValueError("Pinned object corrupted")
        obj.write_bytes(blob)
        data = dict(
            key=key,
            url=official_url(key),
            checksum_url=official_url(key) + ".CHECKSUM",
            status="COMPLETE",
            sha256=digest,
            bytes=len(blob),
            source=source,
            symbol=key.split("/")[5],
            interval="1h",
        )
        self.metadata_path(key).write_bytes(edge.encoded(data))
        return data

    def get(self, url, cap):
        p = urlsplit(url)
        key = p.path.lstrip("/").removesuffix(".CHECKSUM")
        if url not in (official_url(key), official_url(key) + ".CHECKSUM"):
            raise ValueError("Non archive endpoint")
        if self.stop.is_set():
            raise RuntimeError("DOWNLOAD_STOPPED")
        if not self.download:
            raise ValueError("Missing pinned archive, offline mode")
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
        with self.lock:
            self.requests += 1
            if self.requests > 8000:
                self.stop.set()
                raise RuntimeError("REQUEST_BUDGET")
        try:
            with self.local.session.get(url, timeout=40, stream=True) as response:
                if response.status_code in (418, 429, 503):
                    self.stop.set()
                    raise RuntimeError("RATE_LIMIT_STOP_NO_RETRY")
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                blob = b""
                for chunk in response.iter_content(65536):
                    if len(blob) + len(chunk) > cap:
                        self.stop.set()
                        raise ValueError("1H_FILE_SIZE_CAP")
                    blob += chunk
                with self.lock:
                    self.network_bytes += len(blob)
                    if self.network_bytes > 300_000_000:
                        self.stop.set()
                        raise RuntimeError("DOWNLOAD_BYTE_BUDGET")
                return blob
        except requests.RequestException:
            self.stop.set()
            raise

    def fetch(self, key):
        old = self.read(key)
        if old is not None:
            return old
        check = self.get(official_url(key) + ".CHECKSUM", 4096)
        if check is None:
            data = dict(
                key=key,
                url=official_url(key),
                status="MISSING",
                bytes=0,
                symbol=key.split("/")[5],
                interval="1h",
                reason="OFFICIAL_CHECKSUM_HTTP_404_NOT_ZERO_DATA",
            )
            self.metadata_path(key).write_bytes(edge.encoded(data))
            return data
        expected = check.decode().split()[0]
        if not re.fullmatch("[0-9a-fA-F]{64}", expected):
            raise ValueError("Invalid official checksum")
        blob = self.get(official_url(key), 250000)
        if blob is None:
            raise ValueError("ZIP missing despite checksum")
        return self.save(key, blob, expected, "OFFICIAL_PUBLIC_ARCHIVE_CHECKSUM_GET")

    def import_audit(self, prior, needed):
        validation = json.loads((prior / "source-validation.json").read_text())
        manifest = json.loads((prior / "manifest.json").read_text())
        pins = {
            r["key"]: r["sha256"]
            for r in source_rows(prior / "sample-validation.csv")
            if r["status"] == "VERIFIED_ROWS" and r["checksum_verified"] == "True"
        }
        count = 0
        for token in validation["used_evidence"]:
            path = prior / "evidence" / (token + ".json.gz")
            if (
                cross.sha(path)
                != manifest["files"][str(path.relative_to(prior))]["sha256"]
            ):
                raise ValueError("Audit evidence manifest mismatch")
            row = json.loads(gzip.decompress(path.read_bytes()))
            url = row["request"]["url"]
            if not url.startswith(audit.ARCHIVE):
                continue
            key = url.removeprefix(audit.ARCHIVE)
            if (
                key not in needed
                or row["status"] != 200
                or row["request"]["headers"]
                or row["truncated"]
            ):
                continue
            if self.read(key):
                continue
            blob = bytes.fromhex(row["body_hex"])
            if hashlib.sha256(blob).hexdigest() != row["sha256"]:
                raise ValueError("Audit sample corrupted")
            if key not in pins or pins[key] != row["sha256"]:
                raise ValueError("Audit sample official checksum not pinned")
            # Audit manifest and sample validation already prove the official .CHECKSUM.
            self.save(
                key,
                blob,
                row["sha256"],
                "MANIFEST_VERIFIED_CHECKSUM_VALIDATED_AVAILABILITY_AUDIT_SAMPLE",
            )
            count += 1
        return count


def parse_archive(blob, key):
    """Validate all fields needed for this audit, before any bar is usable."""
    columns = [array("q"), array("q"), array("d"), array("d")]
    seen = set()
    count = 0
    zero = 0
    month = key[-11:-4]
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        if len(z.namelist()) != 1:
            raise ValueError("Unexpected ZIP members")
        for row in csv.reader(
            io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8-sig")
        ):
            if not row or not row[0].isdigit():
                continue
            if len(row) < 12:
                raise ValueError("Missing futures kline columns")
            op, close = audit.stamp(row[0]), audit.stamp(row[6])
            if op in seen:
                raise ValueError("DUPLICATE_CANDLE")
            seen.add(op)
            if (
                edge.at(op).strftime("%Y-%m") != month
                or op % HOUR
                or close != op + HOUR - 1
            ):
                raise ValueError("INVALID_HOURLY_INTERVAL")
            if columns[0] and op <= columns[0][-1]:
                raise ValueError("UNORDERED_CANDLE")
            values = [float(row[i]) for i in (1, 2, 3, 4, 5, 7, 8, 9, 10)]
            if not all(math.isfinite(v) for v in values):
                raise ValueError("NONFINITE_FIELD")
            o, hi, lo, c, v, q, n, tb, tq = values
            if (
                min(o, hi, lo, c) <= 0
                or min(v, q, n, tb, tq) < 0
                or n != int(n)
                or tb > v
                or tq > q
                or not lo <= min(o, c) <= max(o, c) <= hi
            ):
                raise ValueError("INVALID_OHLC_OR_VOLUME")
            for col, value in zip(columns, (op, close, q, tq)):
                col.append(value)
            count += 1
            zero += int(q == 0)
    if not count:
        raise ValueError("EMPTY_ARCHIVE")
    missing = sum(
        max(0, (b - a) // HOUR - 1) for a, b in zip(columns[0], columns[0][1:])
    )
    return columns, dict(
        row_count=count,
        first_timestamp=edge.iso(columns[0][0]),
        last_timestamp=edge.iso(columns[0][-1]),
        first_close_time=edge.iso(columns[1][0]),
        last_close_time=edge.iso(columns[1][-1]),
        missing_hours=missing,
        zero_quote_volume_rows=zero,
        duplicate_candles=0,
    )


def ingest(cache, records):
    histories = defaultdict(lambda: [array("q"), array("q"), array("d"), array("d")])
    manifest = []
    for record in sorted(records, key=lambda r: r["key"]):
        row = dict(record)
        if row["status"] == "COMPLETE":
            blob = (cache.root / "objects" / (row["sha256"] + ".zip")).read_bytes()
            audit.validate_checksum(blob, row["sha256"])
            try:
                columns, quality = parse_archive(blob, row["key"])
                row.update(quality)
                row["parse_status"] = "VALIDATED"
                target = histories[row["symbol"]]
                if target[0] and columns[0][0] <= target[0][-1]:
                    raise ValueError("DUPLICATE_CROSS_ARCHIVE_CANDLE")
                for a, b in zip(target, columns):
                    a.extend(b)
            except ValueError as error:
                row["parse_status"] = "REJECTED"
                row["parse_error"] = str(error)
        else:
            row["parse_status"] = "UNAVAILABLE"
        manifest.append(row)
    return dict(histories), manifest


def window(history, t, hours):
    opens, closes, quote, taker = history
    end = bisect_left(opens, t)
    start = end - hours
    if start < 0:
        return None, "INSUFFICIENT_CONTIGUOUS_HISTORY"
    for j in range(start, end):
        expected = t - (end - j) * HOUR
        if opens[j] != expected:
            return None, "MISSING_OR_STALE_HOUR"
        if closes[j] >= t:
            return None, "CLOSE_TIME_NOT_STRICTLY_PAST"
        if closes[j] != opens[j] + HOUR - 1:
            return None, "INVALID_INTERVAL"
    return (start, end), "AVAILABLE"


def flow_value(history, t, hours):
    indices, reason = window(history, t, hours)
    if indices is None:
        return NAN, reason
    lo, hi = indices
    den = sum(history[2][lo:hi])
    if den <= 0:
        return NAN, "ZERO_WINDOW_QUOTE_VOLUME"
    return sum(history[3][lo:hi]) / den, "AVAILABLE"


def source_rows(path):
    with path.open(newline="") as f:
        yield from csv.DictReader(f)


def verify_sources(root):
    prior = root / "artifacts/cross-sectional-market-structure-v1"
    availability = root / "artifacts/derivatives-data-availability-v1"
    hashes = {}
    for directory, names in (
        (
            prior,
            (
                "market-structure-dataset-2024.csv",
                "market-structure-dataset-2025.csv",
                "summary.json",
                "source-validation.json",
            ),
        ),
        (
            availability,
            (
                "archive-layout.json",
                "source-validation.json",
                "sample-validation.csv",
                "archive-objects.csv",
            ),
        ),
    ):
        manifest = json.loads((directory / "manifest.json").read_text())
        for name in names:
            digest = cross.sha(directory / name)
            if digest != manifest["files"][name]["sha256"]:
                raise ValueError("Prior source manifest mismatch " + name)
            hashes[str((directory / name).relative_to(root))] = digest
    layout = json.loads((availability / "archive-layout.json").read_text())
    archive_symbols = set(layout["archive_symbols"])
    needed = set()
    mapping = []
    for year, total, buy in ((2024, 175680, 4915), (2025, 160320, 2069)):
        counts = Counter()
        buys = Counter()
        n = b = 0
        for row in source_rows(prior / f"market-structure-dataset-{year}.csv"):
            s = row["symbol"]
            t = edge.ms(row["timestamp"])
            counts[s] += 1
            buys[s] += row["current_buy_eligible"] == "1.0"
            n += 1
            b += row["current_buy_eligible"] == "1.0"
            if s in archive_symbols:
                needed.update(months_needed(s, t))
        if (n, b) != (total, buy):
            raise ValueError("Unexpected canonical population")
        for s, count in sorted(counts.items()):
            mapping.append(
                dict(
                    year=year,
                    spot_symbol=s,
                    futures_symbol=s if s in archive_symbols else "",
                    mapping="EXACT_ID_CANDIDATE"
                    if s in archive_symbols
                    else "NO_EXACT_MAPPING",
                    top20_rows=count,
                    buy_rows=buys[s],
                    alias_guessing=False,
                )
            )
    return prior, availability, archive_symbols, sorted(needed), mapping, hashes


def load_table(prior, year, histories, mapped):
    table = cross.Table()
    for raw in source_rows(prior / f"market-structure-dataset-{year}.csv"):
        s = raw["symbol"]
        t = edge.ms(raw["timestamp"])
        if s not in mapped:
            indices, reason = None, "NO_EXACT_MAPPING"
        elif s not in histories:
            indices, reason = None, "NO_VALID_ARCHIVE_DATA"
        else:
            indices, reason = window(histories[s], t, 1)
        row = dict(
            symbol=s,
            timestamp=raw["timestamp"],
            timestamp_ms=t,
            year=year,
            symbol_day_id=raw["symbol_day_id"],
            symbol_day_first=float(raw["symbol_day_first"]),
            eligibility_episode_id=raw["eligibility_episode_id"],
            setup_episode_first=float(raw["setup_episode_first"]),
            current_buy_eligible=float(raw["current_buy_eligible"]),
            futures_1h_available=float(indices is not None),
            futures_availability_reason=reason,
            futures_source_close_timestamp=edge.iso(histories[s][1][indices[1] - 1])
            if indices
            else "",
            **{k: cross.number(raw[k]) for k in CONTROLS + OUTCOMES + SECONDARY},
        )
        table.add(row)
    return table


def coverage_rows(table, year):
    result = []
    for name, indices in (
        ("ALL_TOP20", range(table.size)),
        (
            "CURRENT_BUY_ELIGIBLE",
            [i for i, v in enumerate(table.numeric["current_buy_eligible"]) if v],
        ),
    ):
        indices = list(indices)
        available = [i for i in indices if table.numeric["futures_1h_available"][i]]
        days = defaultdict(list)
        for i in indices:
            days[table.text["symbol_day_id"][i]].append(i)
        any_days = sum(
            any(table.numeric["futures_1h_available"][i] for i in rows)
            for rows in days.values()
        )
        full_days = sum(
            all(table.numeric["futures_1h_available"][i] for i in rows)
            for rows in days.values()
        )
        result.append(
            dict(
                year=year,
                population=name,
                total_rows=len(indices),
                available_rows=len(available),
                coverage_pct=len(available) / len(indices) * 100,
                total_unique_symbols=len({table.text["symbol"][i] for i in indices}),
                available_unique_symbols=len(
                    {table.text["symbol"][i] for i in available}
                ),
                total_symbol_days=len(days),
                symbol_days_any_available=any_days,
                symbol_days_all_observed_rows_available=full_days,
                symbol_day_any_coverage_pct=any_days / len(days) * 100,
                symbol_day_complete_coverage_pct=full_days / len(days) * 100,
                unavailable_reasons=dict(
                    Counter(
                        table.text["futures_availability_reason"][i]
                        for i in indices
                        if not table.numeric["futures_1h_available"][i]
                    )
                ),
            )
        )
    return result


def coverage_go(rows):
    return len(rows) == 4 and all(r["coverage_pct"] >= 80 for r in rows)


def selection_analysis(table, year):
    rows = []
    for population, indices in (
        ("ALL_TOP20", range(table.size)),
        (
            "BUY_ELIGIBLE",
            [i for i, v in enumerate(table.numeric["current_buy_eligible"]) if v],
        ),
    ):
        indices = list(indices)
        for outcome in (
            cross.LABEL_PREFIX + "mfe_atr_72h",
            cross.LABEL_PREFIX + "mfe_atr_168h",
            cross.LABEL_PREFIX + "close_return_pct_72h",
            cross.LABEL_PREFIX + "close_return_pct_168h",
        ):
            groups = [
                [
                    table.numeric[outcome][i]
                    for i in indices
                    if bool(table.numeric["futures_1h_available"][i]) == state
                    and math.isfinite(table.numeric[outcome][i])
                ]
                for state in (True, False)
            ]
            a, b = groups
            auc = edge.auc(a, b)
            rows.append(
                dict(
                    year=year,
                    population=population,
                    outcome=outcome,
                    available_total=sum(
                        table.numeric["futures_1h_available"][i] for i in indices
                    ),
                    unavailable_total=sum(
                        not table.numeric["futures_1h_available"][i] for i in indices
                    ),
                    available_observed=len(a),
                    unavailable_observed=len(b),
                    available_median=cross.median(a),
                    unavailable_median=cross.median(b),
                    auc=auc,
                    cliffs_delta=2 * auc - 1 if auc is not None else None,
                    role="SELECTION_BIAS_ONLY_UNWEIGHTED",
                )
            )
    return rows


def add_flow(table, histories):
    for feature in FLOW:
        table.numeric[feature] = array("d", [NAN]) * table.size
    for feature in FLOW:
        table.text[feature + "_status"] = [""] * table.size
    for i in range(table.size):
        for feature, h in zip(FLOW, WINDOWS):
            if not table.numeric["futures_1h_available"][i]:
                value, reason = NAN, table.text["futures_availability_reason"][i]
            else:
                value, reason = flow_value(
                    histories[table.text["symbol"][i]],
                    int(table.numeric["timestamp_ms"][i]),
                    h,
                )
            table.numeric[feature][i] = value
            table.text[feature + "_status"][i] = reason


def populations(table):
    return {
        k: [i for i in indices if table.numeric["futures_1h_available"][i]]
        for k, indices in cross.scopes(table).items()
    }


def row_digest(table, indices):
    h = hashlib.sha256()
    for i in indices:
        h.update(
            (table.text["symbol"][i] + "/" + table.text["timestamp"][i] + "\n").encode()
        )
    return h.hexdigest()


def effect(table, indices, feature, outcome):
    good = [i for i in indices if math.isfinite(table.numeric[feature][i])]
    values = [table.numeric[feature][i] for i in good]
    ranks = cross.percentile(values)
    pairs = [i for i in good if math.isfinite(table.numeric[outcome][i])]
    top = [
        i
        for i, p in zip(good, ranks)
        if p >= 0.8 and math.isfinite(table.numeric[outcome][i])
    ]
    bottom = [
        i
        for i, p in zip(good, ranks)
        if p <= 0.2 and math.isfinite(table.numeric[outcome][i])
    ]
    a = [table.numeric[outcome][i] for i in top]
    b = [table.numeric[outcome][i] for i in bottom]
    auc = edge.auc(a, b)
    result = dict(
        feature=feature,
        outcome=outcome,
        population_count=len(indices),
        feature_count=len(good),
        paired_count=len(pairs),
        paired_row_sha256=row_digest(table, pairs),
        spearman=edge.spearman(
            [table.numeric[feature][i] for i in pairs],
            [table.numeric[outcome][i] for i in pairs],
        ),
        top_count=len(a),
        bottom_count=len(b),
        top_median=cross.median(a),
        bottom_median=cross.median(b),
        median_difference=st.median(a) - st.median(b) if a and b else None,
        auc=auc,
        cliffs_delta=2 * auc - 1 if auc is not None else None,
    )
    return result, good, ranks, top, bottom


def leave_group_effect(table, top, bottom, outcome, group):
    """Exact leave-one-group U using fixed full-population quintile memberships."""
    a = sorted(table.numeric[outcome][i] for i in top)
    b = sorted(table.numeric[outcome][i] for i in bottom)
    if not a or not b:
        return []
    total = edge.auc(a, b) * len(a) * len(b)
    ga, gb = defaultdict(list), defaultdict(list)
    for i in top:
        ga[group(i)].append(table.numeric[outcome][i])
    for i in bottom:
        gb[group(i)].append(table.numeric[outcome][i])
    result = []
    for label in sorted(ga.keys() | gb.keys()):
        x, y = ga[label], gb[label]
        n, m = len(a) - len(x), len(b) - len(y)
        removed_a = sum(
            bisect_left(b, v) + (bisect_right(b, v) - bisect_left(b, v)) * 0.5
            for v in x
        )
        removed_b = sum(
            len(a) - bisect_right(a, v) + (bisect_right(a, v) - bisect_left(a, v)) * 0.5
            for v in y
        )
        both = (edge.auc(x, y) * len(x) * len(y)) if x and y else 0
        u = total - removed_a - removed_b + both
        result.append(
            dict(
                removed_group=label,
                top_count=n,
                bottom_count=m,
                removed_top_count=len(x),
                removed_bottom_count=len(y),
                cliffs_delta=2 * u / (n * m) - 1 if n and m else None,
            )
        )
    return result


def describe(table, year):
    stats, deciles, secondary, controls, concentration = [], [], [], [], []
    for population, indices in populations(table).items():
        print(year, population, len(indices), flush=True)
        for feature in FLOW + CONTROLS:
            good = [i for i in indices if math.isfinite(table.numeric[feature][i])]
            labels = cross.deciles([table.numeric[feature][i] for i in good])
            groups = defaultdict(list)
            for i, d in zip(good, labels):
                groups[d].append(i)
            curve = []
            for d in range(1, 11):
                row = dict(
                    year=year,
                    population=population,
                    feature=feature,
                    decile=d,
                    count=len(groups[d]),
                )
                for outcome in OUTCOMES:
                    vals = [
                        table.numeric[outcome][i]
                        for i in groups[d]
                        if math.isfinite(table.numeric[outcome][i])
                    ]
                    row["median_" + outcome] = cross.median(vals)
                    row["observed_" + outcome] = len(vals)
                deciles.append(row)
                curve.append(row)
            for outcome in PRIMARY:
                row, _, _, top, bottom = effect(table, indices, feature, outcome)
                stats.append(
                    row
                    | dict(
                        year=year,
                        population=population,
                        **cross.curve_summary([r["median_" + outcome] for r in curve]),
                    )
                )
                if (
                    feature in FLOW
                    and population in ("ALL_TOP20", "BUY_ELIGIBLE")
                    and outcome.endswith(("mfe_atr_72h", "mfe_atr_168h"))
                ):
                    for name, fn in (
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
                        for r in leave_group_effect(table, top, bottom, outcome, fn):
                            concentration.append(
                                r
                                | dict(
                                    year=year,
                                    population=population,
                                    feature=feature,
                                    outcome=outcome,
                                    group_type=name,
                                    full_cliffs_delta=row["cliffs_delta"],
                                )
                            )
            if feature in FLOW:
                for label in SECONDARY:
                    a = [
                        table.numeric[feature][i]
                        for i in good
                        if table.numeric[label][i] == 1
                    ]
                    b = [
                        table.numeric[feature][i]
                        for i in good
                        if table.numeric[label][i] == 0
                    ]
                    auc = edge.auc(a, b)
                    secondary.append(
                        dict(
                            year=year,
                            population=population,
                            feature=feature,
                            outcome=label,
                            role="SECONDARY_ONLY",
                            positive_count=len(a),
                            negative_count=len(b),
                            known_coverage=(len(a) + len(b)) / len(indices)
                            if indices
                            else None,
                            auc=auc,
                            cliffs_delta=2 * auc - 1 if auc is not None else None,
                        )
                    )
        for feature in FLOW:
            matched = [
                i
                for i in indices
                if all(
                    math.isfinite(table.numeric[f][i]) for f in (feature,) + CONTROLS
                )
            ]
            for f in (feature,) + CONTROLS:
                for outcome in PRIMARY:
                    row, *_ = effect(table, matched, f, outcome)
                    controls.append(
                        row
                        | dict(
                            year=year,
                            population=population,
                            matched_flow_feature=feature,
                            role="FLOW_MATCHED" if f == feature else "CONTROL_MATCHED",
                            matched_input_row_sha256=row_digest(table, matched),
                        )
                    )
    return stats, deciles, secondary, controls, concentration


def screen(feature, stats, secondary, concentration):
    indexed = {
        (r["year"], r["population"], r["outcome"]): r
        for r in stats
        if r["feature"] == feature
    }
    main = [
        indexed.get((year, "ALL_TOP20", cross.LABEL_PREFIX + f"mfe_atr_{h}h"))
        for year in (2024, 2025)
        for h in (72, 168)
    ]
    direction = 1 if main and main[0] and (main[0]["cliffs_delta"] or 0) > 0 else -1
    main_pass = all(
        r
        and min(r["top_count"], r["bottom_count"]) >= MIN_N
        and r["cliffs_delta"] is not None
        and r["cliffs_delta"] * direction >= MIN_CLIFF
        and r["spearman"] is not None
        and r["spearman"] * direction >= MIN_RHO
        for r in main
    )
    sensitivity = [
        indexed.get((year, p, cross.LABEL_PREFIX + f"mfe_atr_{h}h"))
        for year in (2024, 2025)
        for p in ("SYMBOL_DAY_FIRST", "BUY_ELIGIBLE", "BUY_SETUP_EPISODE_FIRST")
        for h in (72, 168)
    ]
    sensitivity_pass = all(
        r
        and min(r["top_count"], r["bottom_count"]) >= MIN_N
        and r["cliffs_delta"] is not None
        and r["cliffs_delta"] * direction >= 0
        and r["spearman"] is not None
        and r["spearman"] * direction >= 0
        for r in sensitivity
    )
    groups = [
        r
        for r in concentration
        if r["feature"] == feature and r["population"] == "ALL_TOP20"
    ]
    group_pass = bool(groups) and all(
        min(r["top_count"], r["bottom_count"]) >= MIN_N
        and r["cliffs_delta"] is not None
        and r["cliffs_delta"] * direction >= abs(r["full_cliffs_delta"] or 0) * 0.5
        for r in groups
    )
    primary_pass = bool(main_pass and sensitivity_pass and group_pass)
    contrary = [
        r
        for r in secondary
        if r["feature"] == feature
        and min(r["positive_count"], r["negative_count"]) >= MIN_N
        and r["cliffs_delta"] is not None
        and r["cliffs_delta"] * direction * (-1 if r["outcome"] == SECONDARY[1] else 1)
        <= -MIN_CLIFF
    ]
    strength = min(
        [abs(r["cliffs_delta"]) for r in main if r and r["cliffs_delta"] is not None],
        default=0,
    )
    return dict(
        feature=feature,
        direction=direction,
        main_all_year_horizon_pass=main_pass,
        dependency_and_buy_pass=sensitivity_pass,
        leave_symbol_quarter_pass=group_pass,
        primary_pass=primary_pass,
        secondary_strong_opposite=bool(contrary),
        secondary_contradiction_cells=len(contrary),
        minimum_all_abs_cliff=strength,
        candidate_eligible=primary_pass and not contrary,
    )


def classify(checks, stats):
    qualified = sorted(
        (r for r in checks if r["candidate_eligible"]),
        key=lambda r: (-r["minimum_all_abs_cliff"], r["feature"]),
    )
    interesting = any(
        r["feature"] in FLOW
        and r["cliffs_delta"] is not None
        and abs(r["cliffs_delta"]) >= MIN_CLIFF
        for r in stats
    )
    return (
        "FUTURES_TAKER_FLOW_SIGNAL_FOUND"
        if qualified
        else "FUTURES_TAKER_FLOW_SIGNAL_WEAK_OR_UNSTABLE"
        if interesting
        else "FUTURES_TAKER_FLOW_SIGNAL_NOT_FOUND",
        qualified[0]["feature"] if qualified else "NONE",
    )


def anatomy(table, year):
    result = []
    for symbol, timestamp in WINNERS:
        if not timestamp.startswith(str(year)):
            continue
        indices = [
            i
            for i in range(table.size)
            if table.text["symbol"][i] == symbol
            and table.text["timestamp"][i] == timestamp
        ]
        for feature in FLOW:
            row = dict(
                symbol=symbol,
                timestamp=timestamp,
                feature=feature,
                role="POST_HOC_KNOWN_WINNER_EXAMPLE_NOT_RULE",
            )
            if not indices:
                result.append(row | dict(status="NOT_IN_CANONICAL_TOP20"))
                continue
            i = indices[0]
            value = table.numeric[feature][i]
            row.update(
                value=value if math.isfinite(value) else None,
                status=table.text[feature + "_status"][i],
                source_close_timestamp=table.text["futures_source_close_timestamp"][i],
            )
            for population, pool in populations(table).items():
                if population not in ("ALL_TOP20", "BUY_ELIGIBLE"):
                    continue
                vals = sorted(
                    table.numeric[feature][j]
                    for j in pool
                    if math.isfinite(table.numeric[feature][j])
                )
                lo, hi = bisect_left(vals, value), bisect_right(vals, value)
                row[population + "_percentile"] = (
                    ((lo + 1 + hi) / 2 - 1) / (len(vals) - 1)
                    if len(vals) > 1 and math.isfinite(value)
                    else None
                )
                row[population + "_reference_count"] = len(vals)
            result.append(row)
    return result


def write_rows(out, name, rows):
    cross.write_rows(out / name, rows)


def methodology():
    return dict(
        base=BASE,
        role="READ_ONLY_EXPLORATION",
        features=FLOW,
        windows=WINDOWS,
        mapping="Exact spot identifier in verified official USD-M archive only; never aliases/rebrands/multipliers.",
        ingest="Only necessary 1h calendar months covering t-24h..t-1h. 2023-12 source warmup only. No 2026 data. Every ZIP checksum-verified, content SHA-pinned, all rows parsed. Zero volume not missing archive. Invalid/duplicate archives rejected.",
        availability="1h available iff exactly the t-1h valid bar exists, no duplicate, closes at open+1h-1ms and close<t. Independent 4h/24h contiguous-window feature availability is reported; missing/zero denominator => unavailable, never zero share.",
        formula="SUM(taker_buy_quote_volume)/SUM(quote_volume), never average shares. Exactly 1/4/24 completed contiguous hours. No current/future bar; denominator>0. A zero-volume hour is valid in a longer positive-total window.",
        outcomes="Copied unchanged POST_HOC_FUTURE_LABEL_1H spot labels, never futures forward labels. Existing 1m +5R/stop labels are secondary only. No new outcome computation.",
        fair_controls="Base futures-available controls plus per-flow common subset with flow AND all three controls finite. Flow and controls recomputed on identical input/paired outcome rows; SHA identifiers exported.",
        dependency="Inherited canonical symbol-day-first and BUY eligibility-episode-first flags BEFORE availability filtering. No replacement first-available row or recomputed episode. Leave-one-symbol/UTC-quarter U statistics use fixed full-population quintile membership, no rebinning.",
        frozen_statistics=dict(
            deciles=10,
            quintiles="top percentile>=.8 / bottom<=.2; average ties, equal values never split; empty bins explicit",
            minimum_count_per_extreme=MIN_N,
            nontrivial_cliff=MIN_CLIFF,
            minimum_main_rho=MIN_RHO,
            primary_screen="Both years and 72/168h ALL MFE ATR Cliff>=.147 and same-direction rho>=.05, n>=30 per extreme. ALL symbol-day, BUY, BUY episode-first same direction in Cliff/rho with adequate counts. Every ALL leave-one-symbol/quarter must retain n>=30 per extreme and >=50% of full effect with its direction; an inadequately observed removal cannot establish robustness. Secondary cannot promote, only veto if binary Cliff>=.147 strongly opposite.",
            classification="A only surviving primary screen without secondary contradiction; B any nontrivial primary local Cliff but no survivor; C otherwise. At most one by weakest ALL cell magnitude, name tie-break. No cutoff/interaction/model/grid search.",
        ),
        limitations="Univariate association, not conditional incremental information, profit, causality or independent-sample inference. Nonrandom mapping/availability and overlapping outcomes; missingness compared unweighted, never corrected. Both years EXPLORATION.",
        no_lookahead="Every input open is in [t-Nh,t), every close<t. Outputs and known-winner examples never enter feature calculation.",
        safety=dict(
            production_db="mode=ro; PRAGMA query_only=ON",
            network="Only official data.binance.vision monthly USD-M 1h ZIP/CHECKSUM, explicit --download; default offline",
            new_replay=False,
            production_db_writes=0,
            production_behavior_changed=False,
            deployment=False,
        ),
    )


def package(out):
    files = sorted(
        p
        for p in out.iterdir()
        if p.suffix in (".json", ".csv", ".md") and p.name != "manifest.json"
    )
    manifest = dict(
        base=BASE,
        script_sha256=cross.sha(__file__),
        files={
            p.name: dict(bytes=p.stat().st_size, sha256=cross.sha(p)) for p in files
        },
    )
    (out / "manifest.json").write_bytes(edge.encoded(manifest))
    target = out / (NAME + ".zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(files + [out / "manifest.json"]):
            info = zipfile.ZipInfo(p.name, (2024, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with p.open("rb") as src, z.open(info, "w", force_zip64=True) as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    dst.write(chunk)
    return target


def report(out, summary, stats):
    lines = [
        "# Futures Taker Flow Diagnostic v1",
        "",
        "READ-ONLY EXPLORATION. No strategy, feature promotion, replay or production change.",
        "",
        f"Category: {summary['category']}",
        f"hypothesis_candidate_feature: {summary['hypothesis_candidate_feature']}",
        "",
        "## Exact source coverage",
        "",
        "```json",
        json.dumps(summary["coverage"], indent=2),
        "```",
        "",
        "## Primary feature evidence",
        "",
        "Each cell: top-bottom quintile Cliff for 72h / 168h spot MFE ATR. Not trade returns.",
        "",
        "| Feature | 2024 ALL | 2025 ALL | 2024 BUY | 2025 BUY |",
        "|---|---|---|---|---|",
    ]
    index = {(r["year"], r["population"], r["feature"], r["outcome"]): r for r in stats}
    if stats:
        for feature in FLOW + CONTROLS:
            cells = []
            for y, p in (
                (2024, "ALL_TOP20"),
                (2025, "ALL_TOP20"),
                (2024, "BUY_ELIGIBLE"),
                (2025, "BUY_ELIGIBLE"),
            ):
                vals = [
                    index[y, p, feature, cross.LABEL_PREFIX + f"mfe_atr_{h}h"][
                        "cliffs_delta"
                    ]
                    for h in (72, 168)
                ]
                cells.append(
                    " / ".join("NA" if v is None else f"{v:+.4f}" for v in vals)
                )
            lines.append("| " + " | ".join([feature] + cells) + " |")
    lines += [
        "",
        "## Interpretation",
        "",
        "Read coverage-selection-analysis before the flow effects: availability is not a randomized sample, and nothing is weighted/corrected. Fair controls are also exported on identical per-flow rows, with row hashes and paired counts. A 24h feature missing history does not silently substitute a shorter window.",
        "",
        "Dependency and concentration screens are mandatory; thousands of overlapping hourly rows are not independent evidence. Univariate effects do not establish information incremental to controls or net trading profit. Secondary labels cannot establish a primary signal. Known winners are post-hoc examples only.",
        "",
        "Source archive, checksum URLs and SHA pins are in archive-manifest.json; cache ZIPs are excluded from Git and the export bundle. 2023 is warmup only, 2026 not used.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    out = root / "artifacts" / NAME
    out.mkdir(parents=True, exist_ok=True)
    prior, availability, mapped, needed, mapping, inputs = verify_sources(root)
    (out / "methodology.json").write_bytes(edge.encoded(methodology()))
    with edge.read_only(root / "data/replay.db") as db:
        before = edge.source_fingerprint(db, list(edge.RUNS.values()))
    cache = Cache(root / "data/research-cache/futures-usdm-1h-v1", args.download)
    print(
        "Needed archives",
        len(needed),
        "reuse audited samples",
        cache.import_audit(availability, set(needed)),
        flush=True,
    )
    listed = {
        r["key"]
        for r in source_rows(availability / "archive-objects.csv")
        if r["family"] == "klines_1h"
    }
    for key in needed:
        if "-2023-12.zip" not in key and key not in listed and cache.read(key) is None:
            cache.metadata_path(key).write_bytes(
                edge.encoded(
                    dict(
                        key=key,
                        url=official_url(key),
                        status="MISSING",
                        bytes=0,
                        symbol=key.split("/")[5],
                        interval="1h",
                        reason="PINNED_AUDIT_LISTING_ABSENT_NOT_ZERO_DATA",
                    )
                )
            )
    records = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for i, r in enumerate(pool.map(cache.fetch, needed)):
            records.append(r)
            if i % 100 == 0:
                print("Cached", i, "/", len(needed), flush=True)
    histories, archive_manifest = ingest(cache, records)
    (out / "archive-manifest.json").write_bytes(
        edge.encoded(dict(base=BASE, records=archive_manifest))
    )
    write_rows(out, "symbol-mapping.csv", mapping)
    tables = {year: load_table(prior, year, histories, mapped) for year in (2024, 2025)}
    coverage = sum((coverage_rows(t, y) for y, t in tables.items()), [])
    write_rows(out, "exact-futures-coverage.csv", coverage)
    go = coverage_go(coverage)
    print("COVERAGE_GATE", go, json.dumps(coverage), flush=True)
    (
        all_stats,
        all_deciles,
        all_secondary,
        all_controls,
        all_concentration,
        selection,
        winners,
    ) = [], [], [], [], [], [], []
    if go:
        # Required selection check completed before any flow feature is evaluated.
        for year, table in tables.items():
            selection += selection_analysis(table, year)
        write_rows(out, "coverage-selection-analysis.csv", selection)
        for year, table in tables.items():
            add_flow(table, histories)
            table.write(out / f"futures-flow-dataset-{year}.csv")
            stats, deciles, secondary, controls, concentration = describe(table, year)
            all_stats += stats
            all_deciles += deciles
            all_secondary += secondary
            all_controls += controls
            all_concentration += concentration
            winners += anatomy(table, year)
        checks = [
            screen(feature, all_stats, all_secondary, all_concentration)
            for feature in FLOW
        ]
        category, candidate = classify(checks, all_stats)
        for name, rows in (
            ("feature-statistics.csv", all_stats),
            ("feature-deciles.csv", all_deciles),
            ("control-comparison.csv", all_controls),
            ("cross-year-consistency.csv", checks),
            (
                "dependency-sensitivity.csv",
                [
                    r
                    for r in all_stats
                    if r["population"] not in ("ALL_TOP20", "BUY_ELIGIBLE")
                ],
            ),
            ("one-minute-consistency.csv", all_secondary),
            ("concentration-sensitivity.csv", all_concentration),
            ("winner-anatomy.csv", winners),
        ):
            write_rows(out, name, rows)
    else:
        category, candidate, checks = "COVERAGE_STOP", "NONE", []
    summary = dict(
        base=BASE,
        coverage_gate=go,
        coverage=coverage,
        category=category,
        hypothesis_candidate_feature=candidate,
        feature_checks=checks,
        feature_available_counts={
            y: {f: sum(math.isfinite(v) for v in t.numeric[f]) for f in FLOW}
            for y, t in tables.items()
        }
        if go
        else {},
        source_role="2024_AND_2025_EXPLORATION",
        selection_analysis=selection,
        winner_anatomy=winners,
        archive_status_counts=dict(
            Counter(r["status"] + "/" + r["parse_status"] for r in archive_manifest)
        ),
        caution="Univariate descriptive results, not conditional edge, probability, profit, validation or a strategy. No inference of independent hourly observations.",
    )
    (out / "summary.json").write_bytes(edge.encoded(summary))
    report(out, summary, all_stats)
    with edge.read_only(root / "data/replay.db") as db:
        after = edge.source_fingerprint(db, list(edge.RUNS.values()))
    if before != after:
        raise ValueError("Source DB/run inventory changed")
    for relative, digest in inputs.items():
        if cross.sha(root / relative) != digest:
            raise ValueError("Source artifact changed")
    for record in records:
        cache.read(record["key"])
    validation = dict(
        before=before,
        after=after,
        source_fingerprint_unchanged=True,
        input_sha256=inputs,
        read_only_uri=True,
        query_only=True,
        production_db_writes=0,
        new_replay=False,
        production_behavior_changed=False,
        deployment=False,
        network_scope="Only official monthly USD-M 1h archives/checksums",
        archive_manifest_sha256=cross.sha(out / "archive-manifest.json"),
        cache_key_set_sha256=edge.digest(needed),
        coverage_gate=go,
        coverage_rows=coverage,
    )
    (out / "source-validation.json").write_bytes(edge.encoded(validation))
    bundle = package(out)
    print(
        "DONE",
        category,
        candidate,
        bundle,
        bundle.stat().st_size,
        cross.sha(bundle),
        flush=True,
    )


if __name__ == "__main__":
    main()
