"""Evidence-only timestamp audit. No DB, outcomes, feature extraction or engines."""

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

BASE = "d09247b99e57e165c5cebb246a5e10a2c73dcd63"
NAME = "derivatives-metrics-semantics-v1"
STEP = 300000
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
REST = {
    "oi": "/futures/data/openInterestHist",
    "global": "/futures/data/globalLongShortAccountRatio",
    "top_accounts": "/futures/data/topLongShortAccountRatio",
    "top_positions": "/futures/data/topLongShortPositionRatio",
    "taker": "/futures/data/takerlongshortRatio",
}
FIELDS = {
    "sum_open_interest": ("oi", "sumOpenInterest"),
    "sum_open_interest_value": ("oi", "sumOpenInterestValue"),
    "count_long_short_ratio": ("global", "longShortRatio"),
    "count_toptrader_long_short_ratio": ("top_accounts", "longShortRatio"),
    "sum_toptrader_long_short_ratio": ("top_positions", "longShortRatio"),
    "sum_taker_long_short_vol_ratio": ("taker", "buySellRatio"),
}
DOC = "https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data"
HISTORICAL_DAYS = (
    "2024-01-01",
    "2024-06-01",
    "2024-12-31",
    "2025-02-01",
    "2025-06-01",
    "2025-12-31",
)


def encoded(value):
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def now_ms():
    return time.time_ns() // 1000000


def iso(t):
    return (
        (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=t))
        .isoformat()
        .replace("+00:00", "Z")
    )


def stamp(t):
    value = datetime.fromisoformat(t.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    if delta.microseconds % 1000:
        raise ValueError("SUB_MILLISECOND_TIMESTAMP_NOT_SUPPORTED")
    return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000


def allowed(url):
    p = urlsplit(url)
    if p.scheme != "https" or p.username or p.password or p.fragment:
        raise ValueError("FORBIDDEN_URL")
    if p.netloc == "fapi.binance.com" and p.path in {*REST.values(), "/fapi/v1/time"}:
        permitted = (
            set()
            if p.path == "/fapi/v1/time"
            else {"symbol", "period", "limit", "startTime", "endTime"}
        )
        if set(parse_qs(p.query, keep_blank_values=True)) - permitted:
            raise ValueError("AUTHENTICATED_OR_UNAPPROVED_PARAMETER")
        return
    if url == DOC:
        return
    if (
        p.netloc == "data.binance.vision"
        and not p.query
        and re.fullmatch(
            r"/data/futures/um/daily/metrics/([A-Z0-9]+USDT)/\1-metrics-20\d\d-\d\d-\d\d\.zip(?:\.CHECKSUM)?",
            p.path,
        )
    ):
        return
    raise ValueError("ONLY_APPROVED_OFFICIAL_SOURCES")


class Evidence:
    def __init__(self, root, capture=False):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.capture = capture
        self.used = set()
        self.requests = 0
        self.session = requests.Session()
        self.session.trust_env = False

    def get(self, url, tag="stable"):
        allowed(url)
        spec = dict(url=url, tag=tag)
        token = sha(encoded(spec))
        path = self.root / (token + ".json.gz")
        if path.exists():
            record = json.loads(gzip.decompress(path.read_bytes()))
            assert (
                record["request"] == spec
                and sha(bytes.fromhex(record["body_hex"])) == record["sha256"]
            )
        else:
            if not self.capture:
                raise ValueError("OFFLINE_EVIDENCE_MISSING " + url)
            self.requests += 1
            if self.requests > 250:
                raise RuntimeError("REQUEST_BUDGET")
            start = now_ms()
            mono = time.monotonic()
            with self.session.get(
                url,
                timeout=25,
                allow_redirects=False,
                stream=True,
                headers={"Cache-Control": "no-cache"},
            ) as response:
                data = b""
                for chunk in response.iter_content(65536):
                    data += chunk
                    if len(data) > 8_000_000:
                        raise RuntimeError("BODY_CAP")
                record = dict(
                    request=spec,
                    status=response.status_code,
                    request_started_ms=start,
                    response_received_ms=now_ms(),
                    monotonic_duration_ms=(time.monotonic() - mono) * 1000,
                    sha256=sha(data),
                    body_hex=data.hex(),
                    headers={
                        k: v
                        for k, v in response.headers.items()
                        if k.lower()
                        in (
                            "date",
                            "last-modified",
                            "etag",
                            "content-type",
                            "age",
                            "cache-control",
                        )
                    },
                )
            path.write_bytes(gzip.compress(encoded(record), mtime=0))
            if record["status"] in (418, 429, 503):
                raise RuntimeError("RATE_LIMIT_STOP_NO_RETRY")
        self.used.add(token)
        return record

    def body(self, record):
        return bytes.fromhex(record["body_hex"])


def archive_url(symbol, day):
    if not re.fullmatch(r"[A-Z0-9]+USDT", symbol) or not re.fullmatch(
        r"20\d\d-\d\d-\d\d", day
    ):
        raise ValueError("INVALID_SYMBOL_DATE")
    return f"https://data.binance.vision/data/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{day}.zip"


def archive(ev, symbol, day):
    url = archive_url(symbol, day)
    check = ev.get(url + ".CHECKSUM")
    if check["status"] == 404:
        return None, dict(
            symbol=symbol, day=day, url=url, status="MISSING", checksum_http_status=404
        )
    if check["status"] != 200:
        raise ValueError("CHECKSUM_HTTP_ERROR")
    expected = ev.body(check).decode().split()[0].lower()
    record = ev.get(url)
    if (
        record["status"] != 200
        or not re.fullmatch("[0-9a-f]{64}", expected)
        or sha(ev.body(record)) != expected
    ):
        raise ValueError("CHECKSUM_MISMATCH")
    data = parse_archive(ev.body(record), symbol)
    return data, dict(
        symbol=symbol,
        day=day,
        url=url,
        status="CHECKSUM_VALIDATED",
        sha256=expected,
        bytes=len(ev.body(record)),
        first_create_time=iso(min(r["timestamp_ms"] for r in data)),
        last_create_time=iso(max(r["timestamp_ms"] for r in data)),
        first_physical_row_label=data[0]["create_time"],
        last_physical_row_label=data[-1]["create_time"],
        out_of_order_rows=sum(r["out_of_order"] for r in data),
        duplicate_rows=sum(r["duplicate_label"] for r in data),
        schema_header=[
            k
            for k in data[0]
            if k
            not in {
                "timestamp_ms",
                "row_index",
                "duplicate_label",
                "out_of_order",
                "invalid_fields",
            }
        ],
        rows=len(data),
        original_label_dates=sorted({r["create_time"][:10] for r in data}),
        archive_response_received_ms=record["response_received_ms"],
        headers=record["headers"],
    )


def parse_archive(blob, symbol):
    result = []
    seen = set()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        if len(z.namelist()) != 1:
            raise ValueError("UNEXPECTED_ZIP_MEMBERS")
        for row in csv.DictReader(
            io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8-sig")
        ):
            if not {"create_time", "symbol", *FIELDS}.issubset(row):
                raise ValueError("METRICS_SCHEMA")
            t = stamp(row["create_time"])
            if row["symbol"] != symbol:
                raise ValueError("WRONG_SYMBOL")
            # Preserve archive order and duplicates: both are audit findings.
            duplicate = t in seen
            out_of_order = bool(result and t < result[-1]["timestamp_ms"])
            seen.add(t)
            invalid_fields = [field for field in FIELDS if metric_issue(row.get(field))]
            result.append(
                row
                | dict(
                    timestamp_ms=t,
                    row_index=len(result),
                    duplicate_label=duplicate,
                    out_of_order=out_of_order,
                    invalid_fields=invalid_fields,
                )
            )
    if not result:
        raise ValueError("EMPTY_ARCHIVE")
    return result


def metric_issue(value):
    if value in (None, ""):
        return "MISSING"
    try:
        number = Decimal(value)
    except Exception:
        return "INVALID_NUMBER"
    if not number.is_finite():
        return "NONFINITE"
    if number < 0:
        return "NEGATIVE"
    return None


def rest(ev, kind, symbol, start=None, end=None, limit=500, tag="stable"):
    params = dict(symbol=symbol, period="5m", limit=limit)
    if start is not None:
        params["startTime"] = start
    if end is not None:
        params["endTime"] = end
    record = ev.get(
        "https://fapi.binance.com" + REST[kind] + "?" + urlencode(params), tag
    )
    value = json.loads(ev.body(record)) if record["status"] == 200 else None
    if not isinstance(value, list):
        return None, record
    if any(
        "timestamp" not in r or ("symbol" in r and r["symbol"] != symbol) for r in value
    ):
        raise ValueError("REST_SCHEMA_OR_SYMBOL")
    if len({int(r["timestamp"]) for r in value}) != len(value):
        raise ValueError("DUPLICATE_REST_TIMESTAMP")
    return value, record


def value_match(a, b):
    if a in ("", None) or b in ("", None):
        return "MISSING_VALUE"
    x, y = Decimal(str(a)), Decimal(str(b))
    if not x.is_finite() or not y.is_finite():
        return "INVALID_VALUE"
    if x == y:
        return "EXACT"
    quantum = Decimal(1).scaleb(max(x.as_tuple().exponent, y.as_tuple().exponent))
    if x.quantize(quantum, rounding=ROUND_HALF_UP) == y.quantize(
        quantum, rounding=ROUND_HALF_UP
    ):
        return "ROUNDING_COMPATIBLE_NOT_PROVEN"
    return "VALUE_DIFFERENCE"


def compare(archive_rows, responses, symbol, day):
    records = []
    for row in archive_rows:
        t = row["timestamp_ms"]
        for field, (kind, key) in FIELDS.items():
            values = responses.get(kind)
            indexed = (
                {int(r["timestamp"]): r for r in values} if values is not None else {}
            )
            for offset in (-STEP, 0, STEP):
                r = indexed.get(t + offset)
                status = (
                    "REST_UNAVAILABLE"
                    if values is None
                    else "MISSING_REST_ROW"
                    if r is None
                    else value_match(row[field], r.get(key))
                )
                records.append(
                    dict(
                        symbol=symbol,
                        archive_day=day,
                        create_time=row["create_time"],
                        archive_timestamp_ms=t,
                        archive_field=field,
                        rest_endpoint=REST[kind],
                        rest_field=key,
                        tested_rest_minus_archive_ms=offset,
                        rest_timestamp_ms=int(r["timestamp"]) if r else None,
                        archive_value=row[field],
                        rest_value=r.get(key) if r else None,
                        comparison=status,
                        lexical_equal=row[field] == r.get(key) if r else False,
                    )
                )
    return records


def publication_verdict(semantic=None, publication=None):
    # Value equality, cadence, retrospective availability, or a chosen lag are not publication evidence.
    return dict(
        create_time_semantics=semantic or "UNKNOWN",
        publication_availability=publication or "UNKNOWN",
        decision_time_use_2024_2025="NOT_PROVEN_NO_GO"
        if semantic is None or publication is None
        else "REQUIRES_SEPARATE_REVIEW",
        assumed_publication_lag_ms=None,
    )


def write_csv(path, rows):
    fields = sorted({k for r in rows for k in r})
    with path.open("w", newline="") as stream:
        w = csv.DictWriter(stream, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def observe(ev, out):
    start = now_ms()
    boundary = (start // STEP + 1) * STEP
    samples = []
    for offset in (-2000, 2000, 20000, 60000):
        target = boundary + offset
        while now_ms() < target:
            time.sleep(min(0.25, (target - now_ms()) / 1000))
        for kind in REST:
            rows, r = rest(
                ev, kind, "BTCUSDT", limit=5, tag=f"boundary-{boundary}-{offset}-{kind}"
            )
            samples.append(
                dict(
                    endpoint=REST[kind],
                    scheduled_offset_ms=offset,
                    boundary_ms=boundary,
                    request_started_ms=r["request_started_ms"],
                    response_received_ms=r["response_received_ms"],
                    status=r["status"],
                    reported_timestamps=[v["timestamp"] for v in rows]
                    if rows is not None
                    else None,
                    evidence_sha256=r["sha256"],
                )
            )
        print("BOUNDARY_OBSERVED", iso(boundary), offset, flush=True)
    (out / "live-boundary-observations.json").write_bytes(encoded(samples))
    return samples


def package(out, ev):
    proofs = {
        token: dict(sha256=sha((ev.root / (token + ".json.gz")).read_bytes()))
        for token in sorted(ev.used)
    }
    (out / "evidence-index.json").write_bytes(encoded(proofs))
    files = {
        p.name: dict(sha256=sha(p.read_bytes()), bytes=p.stat().st_size)
        for p in sorted(out.iterdir())
        if p.is_file() and p.suffix != ".zip" and p.name != "manifest.json"
    }
    (out / "manifest.json").write_bytes(encoded(dict(base=BASE, files=files)))
    path = out / (NAME + ".zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted([*files, "manifest.json"]):
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, (out / name).read_bytes())
        for token in sorted(ev.used):
            info = zipfile.ZipInfo(
                "evidence/" + token + ".json.gz", (2020, 1, 1, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, (ev.root / (token + ".json.gz")).read_bytes())
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--capture", action="store_true")
    p.add_argument("--observe-boundary", action="store_true")
    args = p.parse_args()
    out = args.root.resolve() / "artifacts" / NAME
    out.mkdir(parents=True, exist_ok=True)
    ev = Evidence(out / "evidence", args.capture)
    doc = ev.get(DOC)
    parser = Text()
    parser.feed(ev.body(doc).decode())
    text = " ".join(" ".join(parser.parts).split())
    (out / "official-rest-docs.txt").write_text(text)
    server = ev.get("https://fapi.binance.com/fapi/v1/time")
    clock = json.loads(ev.body(server))
    server_day = datetime.fromtimestamp(clock["serverTime"] / 1000, timezone.utc).date()
    fresh = []
    all_archives = []
    comparisons = []
    retrieval = []
    for delta in range(1, 8):
        day = (server_day - timedelta(days=delta)).isoformat()
        data, meta = archive(ev, "BTCUSDT", day)
        all_archives.append(meta)
        if data is not None:
            fresh.append(day)
        if len(fresh) == 2:
            break
    if len(fresh) < 2:
        raise RuntimeError("INSUFFICIENT_FRESH_ARCHIVE_OVERLAP")
    print("FRESH_OVERLAP_DAYS", fresh, flush=True)
    for symbol in SYMBOLS:
        for day in fresh:
            data, meta = archive(ev, symbol, day)
            all_archives.append(meta)
            if data is None:
                continue
            start = stamp(day + "T00:00:00Z")
            responses = {}
            for kind in REST:
                response, r = rest(
                    ev, kind, symbol, start - STEP, start + 86400000 + STEP
                )
                responses[kind] = response
                retrieval.append(
                    dict(
                        symbol=symbol,
                        day=day,
                        endpoint=REST[kind],
                        status=r["status"],
                        rows=len(response) if response is not None else None,
                        min_timestamp=min(
                            (int(v["timestamp"]) for v in response), default=None
                        )
                        if response is not None
                        else None,
                        max_timestamp=max(
                            (int(v["timestamp"]) for v in response), default=None
                        )
                        if response is not None
                        else None,
                    )
                )
            comparisons.extend(compare(data, responses, symbol, day))
        print("COMPARED", symbol, flush=True)
    for symbol in SYMBOLS:
        for day in HISTORICAL_DAYS:
            _, meta = archive(ev, symbol, day)
            all_archives.append(meta)
    for kind in REST:
        for day in ("2024-06-01", "2025-06-01"):
            start = stamp(day + "T00:00:00Z")
            rows, r = rest(ev, kind, "BTCUSDT", start, start + STEP, limit=5)
            retrieval.append(
                dict(
                    symbol="BTCUSDT",
                    day=day,
                    endpoint=REST[kind],
                    status=r["status"],
                    rows=len(rows) if rows is not None else None,
                    role="HISTORICAL_RETENTION_CONTROL",
                )
            )
    live_path = out / "live-boundary-observations.json"
    if live_path.exists():
        live = json.loads(live_path.read_text())
        # Replay captured evidence, never repeat a completed observation window.
        for sample in live:
            kind = next(k for k, path in REST.items() if path == sample["endpoint"])
            tag = f"boundary-{sample['boundary_ms']}-{sample['scheduled_offset_ms']}-{kind}"
            _, record = rest(ev, kind, "BTCUSDT", limit=5, tag=tag)
            if record["sha256"] != sample["evidence_sha256"]:
                raise ValueError("BOUNDARY_EVIDENCE_CHANGED")
    else:
        live = observe(ev, out) if args.observe_boundary else []
    write_csv(out / "archive-rest-field-comparison.csv", comparisons)
    write_csv(out / "rest-retrieval.csv", retrieval)
    write_csv(
        out / "boundary-triplet-comparison.csv",
        [
            r
            for r in comparisons
            if r["create_time"][11:16] in ("12:00", "12:05", "12:10")
        ],
    )
    grouped = defaultdict(Counter)
    for r in comparisons:
        grouped[(r["archive_field"], r["tested_rest_minus_archive_ms"])][
            r["comparison"]
        ] += 1
    counts = [
        dict(archive_field=k[0], rest_minus_archive_ms=k[1], **dict(v))
        for k, v in sorted(grouped.items())
    ]
    write_csv(out / "field-alignment-summary.csv", counts)
    all_archives = list({r["url"]: r for r in all_archives}.values())
    (out / "archive-manifest.json").write_bytes(encoded(all_archives))
    verdict = publication_verdict()
    summary = dict(
        base=BASE,
        **verdict,
        fresh_days=fresh,
        symbols=list(SYMBOLS),
        field_alignment=counts,
        observations_only=True,
        live_boundary_observed=bool(live),
        outcomes_read=False,
        production_db_access=False,
        production_runtime_changed=False,
        replay_started=False,
        deployment=False,
        historical_extrapolation="CURRENT_MATCHES_DO_NOT_ESTABLISH_2024_2025_CONTRACT_OR_PUBLICATION_TIME",
        archive_read_at="CAPTURE_RESPONSE_RECEIPT_ONLY; NOT_HISTORICAL_ROW_AVAILABILITY",
        semantics_warning="REST_PERIOD_LABEL_IS_NOT_PUBLICATION_TIME; ARCHIVE_CREATE_TIME_HAS_NO_VERIFIED_CONTRACT",
    )
    (out / "summary.json").write_bytes(encoded(summary))
    report = [
        "# Derivatives Metrics Timestamp Semantics Audit v1",
        "",
        "No predictive research, outcomes, strategy, replay, database access or deployment.",
        "",
        "2024/2025 create_time_semantics: **UNKNOWN**. Publication availability: **UNKNOWN**. Decision-time use: **NOT_PROVEN_NO_GO**. No assumed lag.",
        "",
        "## Evidence limits",
        "",
        "The current official REST schema describes OI and ratio timestamps as period-end and taker timestamps as period-start. These are labels, not first-publication times. The archive create_time contract and its historical version are not specified by those REST descriptions. Contemporary value alignment is empirical evidence for the sampled files only; it does not prove a 2024/2025 contract or a historical release SLA.",
        "Rows spaced five minutes apart are not treated as proof of end/start/snapshot semantics. Daily archive filename dates are not used to rewrite row timestamps. Local first receipt of a live response is an observation bound, not a universal publication lag. Missing REST rows are kept separate from request failure and retention unavailability.",
        "Only official documentation and public, unauthenticated Binance GETs were used. Community issues and third-party datasets were not used as evidence.",
        "",
        "Fresh overlap days: "
        + ", ".join(fresh)
        + ". Symbols: "
        + ", ".join(SYMBOLS)
        + ".",
        "",
        "## Field comparison",
        "",
        "| Archive field | REST timestamp minus create_time ms | Counts |",
        "|---|---:|---|",
    ]
    for c in counts:
        report.append(
            "| "
            + c["archive_field"]
            + " | "
            + str(c["rest_minus_archive_ms"])
            + " | "
            + json.dumps(
                {
                    k: v
                    for k, v in c.items()
                    if k not in ("archive_field", "rest_minus_archive_ms")
                },
                sort_keys=True,
            )
            + " |"
        )
    report += [
        "",
        "## Exact boundary interpretation",
        "",
        "The noon triplets 12:00 / 12:05 / 12:10 are exported with original row labels and all predefined -5m/0/+5m REST comparisons. Even exact agreement at a label cannot prove the row was publicly known at that instant. For an archive row create_time=12:05, historical known_at remains UNKNOWN; no invented 5/10/15-minute lag is applied.",
        "2024/2025 representative daily files are checksum validated and structurally parsed, but contemporary REST retention prevents direct overlapping retrieval at those historical dates. Live boundary polling, if captured, describes today only and cannot repair that missing historical publication evidence.",
        "",
        "The complete specification is handled together with exact identity coverage and per-field readiness in the final handoff.",
    ]
    (out / "report.md").write_text("\n".join(report) + "\n")
    path = package(out, ev)
    print("DONE", verdict, sha(path.read_bytes()), flush=True)


if __name__ == "__main__":
    main()
