import csv
import io
import zipfile
from array import array
from pathlib import Path
from unittest.mock import Mock

import pytest

from research import derivatives_metrics_semantics as a
from research import metrics_identity_coverage as c

T = a.stamp("2024-06-01T12:05:00Z")


def archive_rows(times, **changes):
    stream = io.StringIO()
    w = csv.DictWriter(stream, fieldnames=["create_time", "symbol", *a.FIELDS])
    w.writeheader()
    for t in times:
        w.writerow(
            dict(
                create_time=a.iso(t),
                symbol="BTCUSDT",
                **{k: "1.00000000" for k in a.FIELDS},
            )
            | changes
        )
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as z:
        z.writestr("metrics.csv", stream.getvalue())
    return result.getvalue()


def test_exact_schema_utc_and_five_minute_alignment():
    rows = a.parse_archive(archive_rows([T, T + a.STEP]), "BTCUSDT")
    assert rows[0]["timestamp_ms"] == T and T % a.STEP == 0
    assert set(a.FIELDS).issubset(rows[0])
    assert a.stamp("2024-06-01 12:05:00") == T


def test_order_duplicates_and_unaligned_labels_are_preserved_for_audit():
    rows = a.parse_archive(archive_rows([T + a.STEP, T, T, T + 1]), "BTCUSDT")
    assert [r["timestamp_ms"] for r in rows] == [T + a.STEP, T, T, T + 1]
    assert rows[1]["out_of_order"] and rows[2]["duplicate_label"]
    assert rows[3]["timestamp_ms"] % a.STEP == 1


@pytest.mark.parametrize(
    "value,valid",
    [
        ("", False),
        (None, False),
        ("0", True),
        ("NaN", False),
        ("Infinity", False),
        ("-1", False),
        ("3.5", True),
    ],
)
def test_missing_nonfinite_negative_and_unbounded_ratio(value, valid):
    assert c.valid_metric("count_long_short_ratio", value) == valid
    assert (a.metric_issue(value) is None) == valid


def test_invalid_values_are_not_silently_zero_or_dropped():
    row = a.parse_archive(archive_rows([T], sum_open_interest="NaN"), "BTCUSDT")[0]
    assert row["sum_open_interest"] == "NaN" and row["invalid_fields"] == [
        "sum_open_interest"
    ]


@pytest.mark.parametrize(
    "symbol", ["../BTCUSDT", "PEPE/1000PEPE", "BTCUSDT?x=1", "btcusdt"]
)
def test_invalid_symbol_mapping_rejected(symbol):
    with pytest.raises(ValueError):
        a.archive_url(symbol, "2024-06-01")


def test_exact_mapping_without_multiplier_guessing():
    assert "/PEPEUSDT/" in a.archive_url("PEPEUSDT", "2024-06-01")
    assert "1000PEPE" not in a.archive_url("PEPEUSDT", "2024-06-01")


@pytest.mark.parametrize(
    "url",
    [
        "https://thirdparty.example/x",
        "https://fapi.binance.com/fapi/v1/order",
        "https://fapi.binance.com/fapi/v2/account",
        "http://data.binance.vision/x",
    ],
)
def test_only_official_public_sources(url):
    with pytest.raises(ValueError):
        a.allowed(url)


def test_checksum_failure_is_fatal():
    ev = Mock()
    ev.get.side_effect = [{"status": 200}, {"status": 200}]
    ev.body.side_effect = [b"0" * 64, b"corrupt"]
    with pytest.raises(ValueError, match="CHECKSUM"):
        a.archive(ev, "BTCUSDT", "2024-06-01")


def test_rest_alignment_keeps_offsets_as_hypotheses_not_lags():
    row = a.parse_archive(archive_rows([T]), "BTCUSDT")[0]
    rest = {
        kind: [
            dict(
                timestamp=T + a.STEP,
                **{field: "1" for k, field in a.FIELDS.values() if k == kind},
            )
        ]
        for kind in a.REST
    }
    result = a.compare([row], rest, "BTCUSDT", "2024-06-01")
    assert all(
        r["comparison"] == "EXACT"
        for r in result
        if r["tested_rest_minus_archive_ms"] == a.STEP
    )
    assert all(
        r["comparison"] == "MISSING_REST_ROW"
        for r in result
        if r["tested_rest_minus_archive_ms"] == 0
    )
    assert a.publication_verdict()["publication_availability"] == "UNKNOWN"
    assert a.publication_verdict()["assumed_publication_lag_ms"] is None


@pytest.mark.parametrize(
    "x,y,expected",
    [
        ("1.0000", "1", "EXACT"),
        ("1.23456", "1.2346", "ROUNDING_COMPATIBLE_NOT_PROVEN"),
        ("1", "2", "VALUE_DIFFERENCE"),
        ("", "0", "MISSING_VALUE"),
    ],
)
def test_decimal_comparison(x, y, expected):
    assert a.value_match(x, y) == expected


def test_active_market_required_and_front_metrics_cannot_extend_lifetime():
    hist = {"FRONTUSDT": array("q", [T - c.HOUR])}
    assert c.active(hist, "FRONTUSDT", T)
    assert not c.active(hist, "FRONTUSDT", T + c.HOUR)
    rows = [(2024, "FRONTUSDT", T + c.HOUR, True)]
    metrics = {("FRONTUSDT", T + c.HOUR): [{field: "1" for field in a.FIELDS}]}
    result = c.summarize(rows, hist, metrics)
    assert all(r["valid_unambiguous_value_in_active_market"] == 0 for r in result)
    assert all(r["decision_time_coverage_pct"] is None for r in result)


def test_identity_projection_never_materializes_or_decodes_outcomes(tmp_path):
    path = tmp_path / "source.csv"
    path.write_bytes(
        b"symbol,MFE,MAE,forward_return,trade_pnl,timestamp,current_buy_eligible\r\n"
        b'BTCUSDT,"\xff,\xfe\n""garbage""",INVALID,DO_NOT_READ,SECRET,2024-06-01T12:05:00Z,1.0\r\n'
    )
    assert list(c.project_identity(path)) == [
        dict(
            symbol="BTCUSDT",
            timestamp="2024-06-01T12:05:00Z",
            current_buy_eligible="1.0",
        )
    ]
    assert c.IDENTITY_COLUMNS == ("symbol", "timestamp", "current_buy_eligible")


def test_deterministic_raw_coverage_unknown_is_not_ready():
    rows = [(2024, "BTCUSDT", T, True)]
    hist = {"BTCUSDT": array("q", [T - c.HOUR])}
    observations = {("BTCUSDT", T): [{field: "0" for field in a.FIELDS}]}
    first = c.summarize(rows, hist, observations)
    assert first == c.summarize(rows, hist, observations)
    assert all(
        r["raw_active_label_coverage_pct"] == 100 for r in first if r["year"] == 2024
    )
    assert all(r["decision_time_coverage_pct"] is None for r in first)
    assert a.publication_verdict()["decision_time_use_2024_2025"] == "NOT_PROVEN_NO_GO"


def test_no_database_or_engine_dependency():
    for path in (Path(a.__file__), Path(c.__file__)):
        source = path.read_text()
        assert "import sqlite3" not in source and "crypto_bot." not in source
        assert "ReplayEngine" not in source and "PaperExecution" not in source


def test_evidence_offline_and_deterministic_artifacts(tmp_path):
    ev = a.Evidence(tmp_path / "evidence")
    with pytest.raises(ValueError, match="OFFLINE"):
        ev.get("https://fapi.binance.com/fapi/v1/time")
    (tmp_path / "summary.json").write_bytes(a.encoded(a.publication_verdict()))
    path = a.package(tmp_path, ev)
    before = path.read_bytes()
    assert a.package(tmp_path, ev).read_bytes() == before


@pytest.mark.parametrize("lag", [None, 300000, 600000, 900000])
def test_unknown_semantics_and_arbitrary_lag_never_ready(lag):
    from research.metrics_semantics_finalize import readiness

    assert readiness(lag_ms=lag) == "SEMANTICS_AMBIGUOUS"
    assert (
        readiness(True, True, lag_ms=300000, lag_proven=False) == "SEMANTICS_AMBIGUOUS"
    )


def test_taker_ambiguity_does_not_block_independently_proven_oi():
    from research.metrics_semantics_finalize import classify, readiness

    fields = [
        dict(
            field="sum_open_interest",
            status=readiness(True, True),
            coverage_confirmed=True,
            data_quality_approved=True,
        ),
        dict(field="sum_taker_long_short_vol_ratio", status=readiness()),
    ]
    assert classify(fields) == ("METRICS_SEMANTICS_PARTIALLY_READY", "OPEN_INTEREST")


def test_no_key_or_signature_parameters():
    with pytest.raises(ValueError):
        a.allowed("https://fapi.binance.com/futures/data/openInterestHist?signature=x")


def test_byte_projection_quotes_newlines_and_eof_without_outcome_decode(tmp_path):
    p = tmp_path / "ids.csv"
    p.write_bytes(
        b'symbol,timestamp,current_buy_eligible,MFE\n"BTCUSDT","2024-06-01T12:05:00Z",1.0,"\xff\n""secret"""'
    )
    assert list(c.project_identity(p)) == [
        {
            "symbol": "BTCUSDT",
            "timestamp": "2024-06-01T12:05:00Z",
            "current_buy_eligible": "1.0",
        }
    ]


def test_timestamp_offsets_and_milliseconds_are_not_rounded():
    assert a.stamp("2024-06-01T13:05:00.001+01:00") == T + 1
    assert a.stamp(a.iso(T + 1)) == T + 1
    with pytest.raises(ValueError, match="SUB_MILLISECOND"):
        a.stamp("2024-06-01T12:05:00.000001Z")


def test_candidate_requires_coverage_and_quality_even_if_semantics_proven():
    from research.metrics_semantics_finalize import classify

    fields = [
        dict(field="sum_open_interest", status="READY_FOR_DECISION_TIME_RESEARCH")
    ]
    assert classify(fields)[1] == "NONE"
    fields[0]["coverage_confirmed"] = True
    assert classify(fields)[1] == "NONE"
