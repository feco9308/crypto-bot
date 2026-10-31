"""Independent export QA: direct raw CSV arithmetic, no feature-builder calls."""

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import zipfile
from array import array
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path

from research import funding_premium as f
from research import funding_premium_stats as s


def independent_histories(root, manifest):
    """Reparse ZIPs independently; compact arrays avoid loading giant dictionaries."""
    histories = {}
    verified = 0
    for group, cache_dir in (
        ("records", "funding-premium-v1"),
        ("traded_records", "futures-usdm-1h-v1"),
    ):
        for record in sorted(manifest[group], key=lambda r: r["key"]):
            if record["status"] != "COMPLETE":
                continue
            path = (
                root
                / "data/research-cache"
                / cache_dir
                / "objects"
                / (record["sha256"] + ".zip")
            )
            blob = path.read_bytes()
            assert hashlib.sha256(blob).hexdigest() == record["sha256"]
            assert len(blob) == record["bytes"]
            family, symbol = record["key"].split("/")[4:6]
            cols = histories.setdefault(
                (family, symbol), [array("q"), array("q"), array("d")]
            )
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                assert len(archive.namelist()) == 1
                rows = csv.reader(
                    io.TextIOWrapper(
                        archive.open(archive.namelist()[0]), encoding="utf-8-sig"
                    )
                )
                count = 0
                for i, row in enumerate(rows):
                    if i == 0 and row[0] in ("calc_time", "open_time"):
                        continue
                    timestamp = int(row[0])
                    assert not cols[0] or timestamp > cols[0][-1]
                    close = timestamp if family == "fundingRate" else int(row[6])
                    if family != "fundingRate":
                        assert (
                            timestamp % f.HOUR == 0 and close == timestamp + f.HOUR - 1
                        )
                    cols[0].append(timestamp)
                    cols[1].append(close)
                    cols[2].append(float(row[2] if family == "fundingRate" else row[4]))
                    count += 1
                assert count == record["row_count"]
            verified += 1
    return histories, verified


def last_hours(hist, t, hours):
    if hist is None:
        return None
    opens, closes, prices = hist
    end = bisect_left(opens, t)
    start = end - hours
    if start < 0:
        return None
    # Ordered, unique, aligned opens validated above; exact count and both
    # endpoints prove all intervening h aligned hours are present.
    if opens[start] != t - hours * f.HOUR or opens[end - 1] != t - f.HOUR:
        return None
    if closes[end - 1] >= t:
        return None
    return start, end


def expected(hist, symbol, t):
    values = {key: None for key in f.FEATURES}
    if last_hours(hist.get(("klines", symbol)), t, 1) is None:
        return values, False
    funding = hist.get(("fundingRate", symbol))
    if funding:
        times, _, rates = funding
        left, right = bisect_right(times, t - f.DAY), bisect_right(times, t)
        if right > left:
            values["funding_rate_last"] = rates[right - 1]
            values["funding_sum_24h"] = math.fsum(rates[left:right])
    premium = hist.get(("premiumIndexKlines", symbol))
    for key, hours in (("premium_close_1h", 1), ("premium_mean_24h", 24)):
        w = last_hours(premium, t, hours)
        if w:
            values[key] = math.fsum(premium[2][w[0] : w[1]]) / hours
    mark, index = (
        hist.get(("markPriceKlines", symbol)),
        hist.get(("indexPriceKlines", symbol)),
    )
    mw, iw = last_hours(mark, t, 1), last_hours(index, t, 1)
    if mw and iw:
        m, i = mw[1] - 1, iw[1] - 1
        if mark[0][m] == index[0][i] and index[2][i] > 0:
            values["mark_index_basis_1h"] = mark[2][m] / index[2][i] - 1
    return values, True


def front_audit(hist):
    symbol = "FRONTUSDT"
    traded, funding = hist.get(("klines", symbol)), hist.get(("fundingRate", symbol))
    result = dict(
        symbol=symbol,
        role="POST_HOC_SOURCE_COVERAGE_AUDIT_ONLY_NOT_FEATURE_INPUT",
        scope="REQUIRED_CANONICAL_MONTHS_ONLY; NOT_GLOBAL_INSTRUMENT_COVERAGE",
    )
    if not traded or not funding:
        return result | dict(status="SOURCES_NOT_BOTH_AVAILABLE")
    last_close = traded[1][-1]
    later = [t for t in funding[0] if t > last_close]
    result.update(
        last_traded_close_in_required_cache=f.iso(last_close),
        last_funding_event_in_required_cache=f.iso(funding[0][-1]),
        funding_events_after_cached_trade_history=len(later),
    )
    if later:
        t = ((later[0] + f.HOUR - 1) // f.HOUR) * f.HOUR
        values, active = expected(hist, symbol, t)
        result.update(
            sample_decision_time=f.iso(t),
            traded_active=active,
            all_features_unavailable=all(v is None for v in values.values()),
        )
        assert not active and result["all_features_unavailable"]
    return result


def prior_front_safety(root):
    """Actual checksum-validated prior samples; no download or predictive rows."""
    prior = root / "artifacts/derivatives-data-availability-v1"
    manifest = json.loads((prior / "manifest.json").read_text())
    wanted = {
        f.key_for(family, "FRONTUSDT", "2024-09" if family == "klines" else "2025-01")
        for family in ("klines",) + f.FAMILIES
    }
    pins = {
        r["key"]: r["sha256"]
        for r in f.source_rows(prior / "sample-validation.csv")
        if r["status"] == "VERIFIED_ROWS" and r["checksum_verified"] == "True"
    }
    source = json.loads((prior / "source-validation.json").read_text())
    samples, proof = {}, []
    for token in source["used_evidence"]:
        path = prior / "evidence" / (token + ".json.gz")
        record = json.loads(gzip.decompress(path.read_bytes()))
        key = record["request"]["url"].removeprefix(f.ARCHIVE)
        if (
            key not in wanted
            or record["status"] != 200
            or record["request"]["headers"]
            or record["truncated"]
        ):
            continue
        assert s.sha(path) == manifest["files"][str(path.relative_to(prior))]["sha256"]
        blob = bytes.fromhex(record["body_hex"])
        assert hashlib.sha256(blob).hexdigest() == pins[key] == record["sha256"]
        cols, _ = f.parse_archive(blob, key)
        family = key.split("/")[4]
        samples[family] = cols
        proof.append(dict(key=key, sha256=pins[key]))
    assert set(samples) == {"klines", *f.FAMILIES}
    t = f.ms("2025-01-01T01:00:00Z")
    # Current premium/mark/index and a recent actual funding event exist.
    for family in f.FAMILIES[1:]:
        assert f.window(samples[family], t)
    times, _ = samples["fundingRate"]
    assert bisect_right(times, t) > bisect_right(times, t - f.DAY)
    values, reasons, meta = f.values_at(
        samples["klines"], *(samples[family] for family in f.FAMILIES), t
    )
    assert meta["traded_futures_active_at_t"] == 0
    assert all(math.isnan(v) for v in values.values())
    assert set(reasons.values()) == {"INACTIVE_TRADED_FUTURES_MARKET"}
    return dict(
        role="PRIOR_AUDIT_RAW_SAMPLE_SAFETY_TEST_ONLY; NOT_RESEARCH_COHORT_INPUT",
        decision_timestamp=f.iso(t),
        funding_event_before_decision=f.iso(times[bisect_right(times, t) - 1]),
        last_traded_sample_close=f.iso(samples["klines"][1][-1]),
        recent_funding_and_derivative_candles_present=True,
        traded_active=False,
        all_five_features_unavailable=True,
        proof=proof,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    out = root / "artifacts" / f.NAME
    manifest = json.loads((out / "archive-manifest.json").read_text())
    hist, verified = independent_histories(root, manifest)
    report = dict(archives_independently_reparsed=verified, years={})
    original_fields = (
        (
            "symbol",
            "timestamp",
            "symbol_day_id",
            "eligibility_episode_id",
            "symbol_day_first",
            "setup_episode_first",
            "current_buy_eligible",
        )
        + f.CONTROLS
        + (f.FLOW_CONTROL,)
        + f.OUTCOMES
        + f.SECONDARY
    )
    for year in (2024, 2025):
        seen = set()
        available = defaultdict(int)
        n = 0
        prior = f.source_rows(
            root
            / "artifacts/futures-taker-flow-v1"
            / f"futures-flow-dataset-{year}.csv"
        )
        for row, original in zip(
            f.source_rows(out / f"funding-premium-dataset-{year}.csv"),
            prior,
            strict=True,
        ):
            key = (row["symbol"], row["timestamp"])
            assert key not in seen
            seen.add(key)
            for name in original_fields:
                assert row[name] == original[name], (year, key, name)
            t = f.ms(row["timestamp"])
            expected_values, active = expected(hist, row["symbol"], t)
            assert float(row["traded_futures_active_at_t"]) == float(active)
            for feature, value in expected_values.items():
                if value is None:
                    assert row[feature] == "", (year, key, feature)
                else:
                    assert float(row[feature]) == value, (
                        year,
                        key,
                        feature,
                        row[feature],
                        value,
                    )
                    available[feature] += 1
            if row["funding_last_event_ms"]:
                assert int(float(row["funding_last_event_ms"])) <= t
            for col in (
                "traded_source_close_ms",
                "premium_source_close_ms",
                "mark_source_close_ms",
                "index_source_close_ms",
            ):
                if row[col]:
                    assert int(float(row[col])) < t
            n += 1
        report["years"][year] = dict(
            rows=n,
            duplicate_rows=0,
            feature_available_counts=dict(available),
            feature_mismatches=0,
            source_fields_and_labels_exact=True,
        )
        print("QA_YEAR", year, n, dict(available), flush=True)
    controls = list(f.source_rows(out / "control-comparison.csv"))
    cells = defaultdict(list)
    for row in controls:
        cells[
            (
                row["year"],
                row["population"],
                row["matched_feature"],
                row["matched_pool"],
                row["outcome"],
            )
        ].append(row)
    for rows in cells.values():
        assert (
            len(
                {
                    (
                        r["population_count"],
                        r["paired_count"],
                        r["input_row_sha256"],
                        r["paired_row_sha256"],
                        r["paired_outcome_sha256"],
                    )
                    for r in rows
                }
            )
            == 1
        )
    report["matched_control_cells_verified"] = len(cells)
    source = json.loads((out / "source-validation.json").read_text())
    assert source["source_fingerprint_unchanged"] and source["source_files_unchanged"]
    for path, digest in source["input_sha256"].items():
        assert s.sha(root / path) == digest
    report.update(
        source_fingerprint_unchanged=True,
        source_hashes_unchanged=True,
        production_db_writes=0,
        new_replay=False,
        deployment=False,
    )
    report["front_case"] = front_audit(hist)
    report["front_case"]["prior_validated_sample_guard_test"] = prior_front_safety(root)
    (out / "front-coverage-audit.json").write_bytes(s.encoded(report["front_case"]))
    (out / "export-validation.json").write_bytes(s.encoded(report))
    archive = f.package(out)
    first = archive.read_bytes()
    assert f.package(out).read_bytes() == first
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        assert z.namelist() == sorted(z.namelist())
        assert all("/" not in name for name in z.namelist())
    print(
        "EXPORT_QA_PASS",
        str(archive),
        archive.stat().st_size,
        s.sha(archive),
        flush=True,
    )


if __name__ == "__main__":
    main()
