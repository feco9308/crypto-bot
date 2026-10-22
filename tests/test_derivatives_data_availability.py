"""Bounded official HTTP audit: deterministic metadata, no trading/replay."""

import ast
import gzip
import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from research import derivatives_data_availability as a
from research import institutional_signal_edge as edge


def test_deterministic_exact_mapping_no_guessed_aliases():
    names = {"BTCUSDT", "1000PEPEUSDT"}
    assert a.mapping("BTCUSDT", names)[0] == "BTCUSDT"
    assert a.mapping("PEPEUSDT", names)[0] == ""
    assert a.mapping("MISSINGUSDT", names) == a.mapping("MISSINGUSDT", names)


@pytest.mark.parametrize("family", a.FAMILIES)
def test_archive_urls_deterministic(family):
    freq = "daily" if family == "metrics" else "monthly"
    date = "2024-01-01" if freq == "daily" else "2024-01"
    key = a.key_for(family, "BTCUSDT", date, freq)
    assert key == a.key_for(family, "BTCUSDT", date, freq)
    assert key.startswith("data/futures/um/") and key.endswith(".zip")
    assert "BTCUSDT" in key


@pytest.mark.parametrize(
    "symbol,period",
    [("../BTCUSDT", "2024-01"), ("BTCUSDT", "2026-01"), ("BTCUSDT", "2023-01")],
)
def test_invalid_key_scope_rejected(symbol, period):
    with pytest.raises(ValueError):
        a.key_for("klines_1h", symbol, period)


def test_checksum_validation():
    data = b"bounded official sample"
    expected = hashlib.sha256(data).hexdigest()
    assert a.validate_checksum(data, expected + "  file.zip") == expected
    for bad in ("0" * 64, "invalid"):
        with pytest.raises(ValueError, match="CHECKSUM_MISMATCH"):
            a.validate_checksum(data, bad)


@pytest.mark.parametrize(
    "value",
    [
        1704067200,
        1704067200000,
        1704067200000000,
        "2024-01-01 00:00:00",
        "2024-01-01T00:00:00Z",
    ],
)
def test_timestamp_units_utc(value):
    assert a.stamp(value) == 1704067200000


def test_no_lookahead_spec_boundaries():
    assert a.known_at("funding", 100, 100) is True
    assert a.known_at("funding", 101, 100) is False
    for kind in ("klines", "aggTrades", "mark"):
        assert a.known_at(kind, 100, 100) is False
        assert a.known_at(kind, 99, 100) is True
    assert a.known_at("metrics", 99, 100) is None
    assert a.known_at("metrics", 90, 100, 10) is False
    assert a.known_at("metrics", 89, 100, 10) is True


def test_listing_before_actual_data_is_not_coverage():
    objects = [dict(key=a.key_for("klines_1h", "NEWUSDT", "2024-05"))]
    t = a.stamp("2024-05-01T01:00:00Z")
    assert a.covered(t, objects) is None
    listing = a.stamp("2024-05-20T00:00:00Z")
    assert a.covered(t, objects, (listing, listing + 86400000)) is False
    assert a.covered(listing + 3600000, objects, (listing, listing + 86400000)) is True


def test_missing_archive_not_a_zero_market_value():
    assert a.covered(a.stamp("2024-05-01T01:00:00Z"), []) is False
    # False means no object, not an invented zero OI, funding or volume.
    assert not any(
        k in a.covered.__code__.co_names
        for k in ("fillna", "RiskManager", "place_order")
    )


def test_recent_rest_is_not_historical_even_when_archive_exists():
    assert a.RETENTION["oi"] == "LATEST_1_MONTH"
    for key in ("global_ls", "top_accounts", "top_positions", "taker"):
        assert a.RETENTION[key] == "LATEST_30_DAYS"
    assert "HISTORICAL" in a.RETENTION["funding"]
    assert a.RETENTION["current_oi"] == "CURRENT_ONLY"


def test_snapshot_and_funding_parsing():
    key = a.key_for("metrics", "BTCUSDT", "2024-01-01", "daily")
    rows = [
        ["create_time", "symbol", "sum_open_interest"],
        ["2024-01-01 00:00:00", "BTCUSDT", "10"],
        ["2024-01-01 00:10:00", "BTCUSDT", "11"],
    ]
    result = a.inspect_rows(key, rows)
    assert result["rows"] == 2 and result["missing_intervals"] == 1
    assert result["first_timestamp"] == "2024-01-01T00:00:00Z"
    f = a.inspect_rows(
        a.key_for("fundingRate", "BTCUSDT", "2024-01"),
        [
            ["calc_time", "funding_interval_hours", "last_funding_rate"],
            ["1704067200000", "8", ".001"],
            ["1704096000000", "8", ".002"],
        ],
    )
    assert f["cadence_ms"] == {28800000: 1}


def test_aggtrade_timestamp_duplicates_are_not_duplicate_trades():
    rows = [
        ["id", "price", "qty", "first", "last", "time", "is_buyer_maker"],
        ["1", "10", "1", "1", "1", "1704067200000", "true"],
        ["2", "10", "1", "2", "2", "1704067200000", "false"],
    ]
    r = a.inspect_rows(a.key_for("aggTrades", "BTCUSDT", "2024-01"), rows)
    assert r["duplicate_timestamps"] == 1 and r["duplicate_ids"] == 0
    assert r["buyer_maker_present"] is True


def test_no_capture_is_offline_and_cached_evidence_is_hashed(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network not permitted")

    monkeypatch.setattr(a.requests.Session, "get", forbidden)
    ev = a.Evidence(tmp_path)
    url = a.ARCHIVE + "data/futures/um/example"
    with pytest.raises(ValueError, match="Missing captured evidence"):
        ev.get(url)
    spec = dict(url=url, headers={})
    data = dict(
        request=spec,
        body_hex=b"hello".hex(),
        sha256=hashlib.sha256(b"hello").hexdigest(),
    )
    path = ev.root / (edge.digest(spec) + ".json.gz")
    path.write_bytes(gzip.compress(edge.encoded(data), mtime=0))
    assert ev.get(url) == data
    data["body_hex"] = b"tampered".hex()
    path.write_bytes(gzip.compress(edge.encoded(data), mtime=0))
    with pytest.raises(ValueError, match="Evidence mismatch"):
        ev.get(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://third-party.example/data",
        "http://data.binance.vision/data",
        "https://fapi.binance.com/fapi/v1/order",
        "https://fapi.binance.com/fapi/v1/account",
    ],
)
def test_nonofficial_or_trading_endpoints_rejected(tmp_path, url):
    with pytest.raises(ValueError):
        a.Evidence(tmp_path, True).get(url)


def test_rate_limit_stops_capture_no_retry(tmp_path, monkeypatch):
    class Response:
        status_code = 429

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    calls = []

    def get(*args, **kwargs):
        calls.append(args)
        return Response()

    monkeypatch.setattr(a.requests.Session, "get", get)
    ev = a.Evidence(tmp_path, True)
    with pytest.raises(RuntimeError, match="RATE_LIMIT_STOP"):
        ev.get(a.S3)
    with pytest.raises(RuntimeError, match="CAPTURE_STOPPED"):
        ev.get(a.S3)
    assert len(calls) == 1


def test_ignored_range_never_reads_bulk_body(tmp_path, monkeypatch):
    class Response:
        status_code = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_content(self, *args):
            raise AssertionError("Bulk read forbidden")

    monkeypatch.setattr(a.requests.Session, "get", lambda *args, **kwargs: Response())
    ev = a.Evidence(tmp_path, True)
    result = ev.get(a.ARCHIVE + "example.zip", headers={"Range": "bytes=0-65535"})
    assert result["truncated"] and not result["body_hex"]


def test_read_only_db_no_write_or_replay_creation(tmp_path):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE replay_runs (id TEXT)")
        db.execute("INSERT INTO replay_runs VALUES ('old')")
    digest = a.sha(path)
    with edge.read_only(path) as db:
        assert db.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO replay_runs VALUES ('new')")
    assert a.sha(path) == digest


def test_deterministic_report_and_zip(tmp_path):
    rows = [dict(family="funding", year=2024, coverage="UNKNOWN")]
    a.write_rows(tmp_path / "sample.csv", rows)
    first = (tmp_path / "sample.csv").read_bytes()
    a.write_rows(tmp_path / "sample.csv", rows)
    assert (tmp_path / "sample.csv").read_bytes() == first
    (tmp_path / "summary.json").write_bytes(edge.encoded(dict(candidate="NONE")))
    z = a.package(tmp_path)
    before = z.read_bytes()
    assert a.package(tmp_path).read_bytes() == before
    with zipfile.ZipFile(z) as bundle:
        assert bundle.namelist() == sorted(bundle.namelist())
        manifest = json.loads(bundle.read("manifest.json"))
        for name, record in manifest["files"].items():
            assert hashlib.sha256(bundle.read(name)).hexdigest() == record["sha256"]


def test_no_production_engine_model_or_write_import():
    source = Path(a.__file__).read_text()
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {
        "ReplayEngine",
        "RiskManager",
        "PortfolioService",
        "ReplayRepository",
        "GridSearchCV",
        "fit",
    }
    assert "INSERT INTO" not in source
    assert "POST_HOC_FUTURE_LABEL" not in source


def test_exact_official_noninterval_file_names():
    assert (
        a.key_for("fundingRate", "BTCUSDT", "2024-01")
        == "data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip"
    )
    assert (
        a.key_for("metrics", "BTCUSDT", "2024-01-01", "daily")
        == "data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2024-01-01.zip"
    )
    assert a.key_for("aggTrades", "BTCUSDT", "2024-01-01", "daily").endswith(
        "/BTCUSDT-aggTrades-2024-01-01.zip"
    )


def test_inventory_prefix_and_marker_include_family():
    class Fake:
        def __init__(self):
            self.calls = []

        def listing(self, prefix, **kwargs):
            self.calls.append((prefix, kwargs))
            return [], [], False

    fake = Fake()
    a.inventory(fake, "metrics", "BTCUSDT")
    assert fake.calls[0][0].endswith("BTCUSDT-metrics-2024")
    fake = Fake()
    a.inventory(fake, "fundingRate", "BTCUSDT")
    assert fake.calls[0][1]["marker"].endswith("BTCUSDT-fundingRate-2024")


def test_instrument_boundary_samples_are_bounded_and_keep_listing_unknown():
    class Fake:
        def __init__(self):
            self.keys = []

        def sample(self, key):
            self.keys.append(key)
            return dict(key=key, status="VERIFIED_ROWS")

    fake = Fake()
    objects = [
        dict(key=a.key_for("klines_1h", "NEWUSDT", p), bytes=2000)
        for p in ("2024-05", "2024-06", "2025-12")
    ]
    samples = a.boundary_samples(fake, {("klines_1h", "NEWUSDT"): objects})
    assert len(samples) == 2
    assert sorted(fake.keys) == [objects[0]["key"], objects[-1]["key"]]
    fake = Fake()
    objects[0]["bytes"] = 80001
    assert len(a.boundary_samples(fake, {("klines_1h", "NEWUSDT"): objects})) == 1


def test_empty_or_non_1h_boundaries_do_not_download():
    class Fake:
        def sample(self, key):
            raise AssertionError("Not a bounded 1h file")

    assert (
        a.boundary_samples(
            Fake(),
            {
                ("klines_1h", "NEWUSDT"): [],
                ("aggTrades", "BTCUSDT"): [dict(key="large", bytes=999999999)],
            },
        )
        == []
    )


def test_offset_timestamp_is_converted_not_relabelled():
    assert a.stamp("2024-01-01T01:00:00+01:00") == a.stamp("2024-01-01T00:00:00Z")


def test_archive_timestamp_wrong_year_or_units_rejected():
    rows = [["calc_time", "interval", "rate"], ["1735689600000", "8", "0.001"]]
    with pytest.raises(ValueError, match="OUTSIDE_NAMED_PERIOD"):
        a.inspect_rows(a.key_for("fundingRate", "BTCUSDT", "2024-01"), rows)


def test_metrics_listing_only_cohort_months_when_supplied():
    class Fake:
        def __init__(self):
            self.prefixes = []

        def listing(self, prefix, **kwargs):
            self.prefixes.append(prefix)
            return [], [], False

    fake = Fake()
    a.inventory(fake, "metrics", "BTCUSDT", ["2024-03", "2025-07"])
    assert [p.rsplit("-", 2)[-2:] for p in fake.prefixes] == [
        ["2024", "03"],
        ["2025", "07"],
    ]


def test_real_report_deterministic_and_never_backfills_listing_month(tmp_path):
    from collections import defaultdict

    t = a.stamp("2024-05-01T01:00:00Z")
    start = a.stamp("2024-05-20T00:00:00Z")
    key = a.key_for("klines_1h", "NEWUSDT", "2024-05")
    sample = dict(
        key=key,
        status="VERIFIED_ROWS",
        family="klines",
        interval="1h",
        first_ms=start,
        last_ms=start + 3600000,
        first_timestamp=a.iso(start),
        last_timestamp=a.iso(start + 3600000),
        missing_intervals=0,
        duplicate_timestamps=0,
        irregular_intervals=0,
        required_kline_columns_present=True,
    )
    population = {
        2024: [("NEWUSDT", t, True)],
        2025: [("NEWUSDT", a.stamp("2025-05-01T01:00:00Z"), True)],
    }
    index = defaultdict(list, {("klines_1h", "NEWUSDT"): [dict(key=key, bytes=1000)]})
    maps = {"NEWUSDT": ("NEWUSDT", "EXACT")}
    a.assemble(tmp_path, population, maps, index, [sample], {})
    first = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["next_research_candidate"] == "NONE"
    row = next(
        r for r in summary["matrix"] if r["family"] == "klines_1h" and r["year"] == 2024
    )
    assert row["top20_presence_upper_pct"] == 0
    assert row["sample_verified_1h_row_lower_pct"] == 0
    a.assemble(tmp_path, population, maps, index, [sample], {})
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == first


def test_funding_nominal_archive_time_is_not_actual_event_time():
    t = 1704067200000
    rows = [
        ["calc_time", "funding_interval_hours", "last_funding_rate"],
        [str(t), "8", "0.0001"],
    ]
    result = a.compare_funding_times(
        rows, [dict(fundingTime=t + 1, fundingRate="0.00010000")]
    )
    assert result[0]["timestamp_offset_ms"] == 1
    assert result[0]["archive_exact_event_time_safe"] is False
    assert a.known_at("funding", t + 1, t) is False
    assert (
        a.compare_funding_times(rows, [dict(fundingTime=t, fundingRate="0.00010000")])[
            0
        ]["archive_exact_event_time_safe"]
        is True
    )


def test_unmatched_funding_event_never_fabricates_archive_row():
    result = a.compare_funding_times(
        [], [dict(fundingTime=1704067200001, fundingRate="0.001")]
    )[0]
    assert result["archive_calc_ms"] is None and result["archive_rate"] is None
    assert result["archive_exact_event_time_safe"] is False


def test_archive_preserved_submillisecond_hour_offset_matches_exact_event():
    t = 1704067200001
    rows = [[str(t), "8", "0.0001"]]
    r = a.compare_funding_times(rows, [dict(fundingTime=t, fundingRate="0.00010000")])[
        0
    ]
    assert r["archive_calc_ms"] == t and r["timestamp_offset_ms"] == 0
    assert (
        r["event_offset_from_hour_ms"] == 1
        and r["archive_exact_event_time_safe"] is True
    )


def test_post_price_archive_funding_does_not_extend_instrument_coverage(tmp_path):
    from collections import defaultdict

    end = a.stamp("2024-09-11T09:00:00Z")
    t = a.stamp("2025-06-01T01:00:00Z")
    key = a.key_for("klines_1h", "FRONTUSDT", "2024-09")
    sample = dict(
        key=key,
        status="VERIFIED_ROWS",
        family="klines",
        interval="1h",
        first_ms=end - 3600000,
        last_ms=end,
        first_timestamp=a.iso(end - 3600000),
        last_timestamp=a.iso(end),
        missing_intervals=0,
        duplicate_timestamps=0,
        irregular_intervals=0,
    )
    (tmp_path / "rest-control-samples.json").write_bytes(
        edge.encoded(
            [
                dict(
                    endpoint="funding",
                    status=200,
                    symbol="FRONTUSDT",
                    year=2025,
                    body=json.dumps([dict(fundingTime=t, fundingRate="0.0001")]),
                )
            ]
        )
    )
    index = defaultdict(
        list,
        {
            ("klines_1h", "FRONTUSDT"): [dict(key=key, bytes=100)],
            ("fundingRate", "FRONTUSDT"): [
                dict(key=a.key_for("fundingRate", "FRONTUSDT", "2025-06"), bytes=100)
            ],
        },
    )
    a.assemble(
        tmp_path,
        {2024: [("FRONTUSDT", end, True)], 2025: [("FRONTUSDT", t, True)]},
        {"FRONTUSDT": ("FRONTUSDT", "EXACT")},
        index,
        [sample],
        {},
    )
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert len(summary["cross_family_anomalies"]) == 1
    row = next(
        r
        for r in summary["matrix"]
        if r["year"] == 2025 and r["family"] == "fundingRate"
    )
    assert row["top20_presence_upper_pct"] == 0
