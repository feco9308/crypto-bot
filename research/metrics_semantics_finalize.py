"""Assemble evidence-only handoff after both collectors complete; never fetch data."""

import argparse
import csv
import gzip
import json
import shutil
from collections import Counter
from decimal import Decimal
from pathlib import Path

from research import derivatives_metrics_semantics as a
from research import metrics_identity_coverage as coverage

SCHEMA = [
    "create_time",
    "symbol",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
]


def readiness(
    semantics_proven=False,
    publication_proven=False,
    lag_ms=None,
    lag_proven=False,
    historical_data_exists=True,
):
    if not historical_data_exists:
        return "NOT_HISTORICALLY_USABLE"
    if not semantics_proven or not publication_proven:
        return "SEMANTICS_AMBIGUOUS"
    if lag_ms is not None:
        if lag_ms < 0 or not lag_proven:
            return "SEMANTICS_AMBIGUOUS"
        return "CONSERVATIVE_LAG_REQUIRED_AND_PROVEN"
    return "READY_FOR_DECISION_TIME_RESEARCH"


def classify(fields):
    ready = {"READY_FOR_DECISION_TIME_RESEARCH", "CONSERVATIVE_LAG_REQUIRED_AND_PROVEN"}
    flags = {r["field"]: r["status"] in ready for r in fields}
    status = (
        "METRICS_SEMANTICS_READY"
        if flags and all(flags.values())
        else "METRICS_SEMANTICS_PARTIALLY_READY"
        if any(flags.values())
        else "METRICS_SEMANTICS_AMBIGUOUS"
        if any(r["status"] == "SEMANTICS_AMBIGUOUS" for r in fields)
        else "METRICS_NOT_USABLE"
    )
    eligible = {
        r["field"]: flags[r["field"]]
        and r.get("coverage_confirmed", False)
        and r.get("data_quality_approved", False)
        for r in fields
    }
    candidate = (
        "OPEN_INTEREST"
        if any(
            eligible.get(f, False)
            for f in ("sum_open_interest", "sum_open_interest_value")
        )
        else "POSITIONING"
        if any(
            eligible.get(f, False)
            for f in (
                "count_long_short_ratio",
                "count_toptrader_long_short_ratio",
                "sum_toptrader_long_short_ratio",
            )
        )
        else "TAKER_RATIO"
        if eligible.get("sum_taker_long_short_vol_ratio")
        else "NONE"
    )
    return status, candidate


def json_file(path):
    return json.loads(path.read_text())


def csv_rows(path):
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def cached_market_boundaries(root):
    """Describe supplied price-history boundaries, never legal listing dates."""
    identities, _ = coverage.identities(root)
    hist, _ = coverage.traded_history(root)
    grouped = {}
    for year, symbol, t, _ in identities:
        row = grouped.setdefault(
            (year, symbol),
            dict(
                year=year,
                symbol=symbol,
                first_identity_ms=t,
                last_identity_ms=t,
                identities=0,
                inactive_identities=0,
                boundary_scope="PINNED_REQUIRED_HISTORY_ONLY; NOT_GLOBAL_LISTING_OR_DELISTING",
            ),
        )
        row["first_identity_ms"] = min(t, row["first_identity_ms"])
        row["last_identity_ms"] = max(t, row["last_identity_ms"])
        row["identities"] += 1
        row["inactive_identities"] += not coverage.active(hist, symbol, t)
    for row in grouped.values():
        opens = hist.get(row["symbol"])
        row["first_cached_traded_open"] = a.iso(opens[0]) if opens else None
        row["last_cached_traded_close"] = (
            a.iso(opens[-1] + coverage.HOUR - 1) if opens else None
        )
        row["legal_listing_time"] = "UNKNOWN"
        row["legal_delisting_time"] = "UNKNOWN"
    return list(grouped.values())


def taker_checks(ev):
    counts = Counter()
    n = 0
    for path in ev.root.glob("*.json.gz"):
        r = json.loads(gzip.decompress(path.read_bytes()))
        url = r["request"]["url"]
        if "/futures/data/takerlongshortRatio?" not in url or r["status"] != 200:
            continue
        rows = json.loads(bytes.fromhex(r["body_hex"]))
        if not isinstance(rows, list):
            continue
        for row in rows:
            denominator = Decimal(row["sellVol"])
            if denominator == 0:
                counts["ZERO_DENOMINATOR_UNDEFINED"] += 1
                continue
            ratio = Decimal(row["buyVol"]) / denominator
            counts[a.value_match(str(ratio), row["buySellRatio"])] += 1
            n += 1
    return dict(
        REST_ratio_formula="buyVol / sellVol",
        REST_timestamp_documented_as="PERIOD_START",
        REST_ratio_reconstruction_counts=dict(counts),
        REST_ratio_reconstruction_rows=n,
        archive_numerator="UNKNOWN",
        archive_denominator="UNKNOWN",
        historical_archive_interval="UNKNOWN",
        historical_archive_status="ARCHIVE_TAKER_RATIO_SEMANTICS_NOT_PROVEN",
        current_alignment_is_empirical_only=True,
        blocks_other_fields_automatically=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    out = root / "artifacts" / a.NAME
    stage = out / "coverage-stage"
    required = [
        "exact-coverage.csv",
        "symbol-mapping.csv",
        "historical-data-quality.csv",
        "historical-archive-manifest.json",
        "identity-source-validation.json",
    ]
    for name in required:
        if not (stage / name).exists():
            raise ValueError("COVERAGE_STAGE_INCOMPLETE " + name)
        shutil.copyfile(stage / name, out / name)
    sample_manifest = json_file(out / "archive-manifest.json")
    if isinstance(sample_manifest, dict):
        sample_manifest = sample_manifest["sample_records"]
    bulk = json_file(out / "historical-archive-manifest.json")
    (out / "archive-manifest.json").write_bytes(
        a.encoded(dict(sample_records=sample_manifest, historical_records=bulk))
    )
    # Rehydrate only whitelisted, already captured official sample evidence.
    ev = a.Evidence(out / "evidence")
    for path in sorted(ev.root.glob("*.json.gz")):
        record = json.loads(gzip.decompress(path.read_bytes()))
        spec = record["request"]
        ev.get(spec["url"], spec["tag"])
    fields = [
        dict(
            field=field,
            status=readiness(),
            historical_create_time_semantics="UNKNOWN",
            publication_availability="UNKNOWN",
            proven_publication_lag_ms=None,
            current_REST_timestamp_definition="PERIOD_START"
            if field == "sum_taker_long_short_vol_ratio"
            else "PERIOD_END",
            REST_definition_does_not_establish_archive_label_contract=True,
            taker_ambiguity_is_not_an_OI_veto=True,
        )
        for field in a.FIELDS
    ]
    a.write_csv(out / "field-readiness.csv", fields)
    summary = json_file(out / "summary.json")
    overall, candidate = classify(fields)
    summary.update(
        overall_result=overall,
        next_research_candidate=candidate,
        field_readiness=fields,
        exact_historical_coverage=csv_rows(out / "exact-coverage.csv"),
        production_db_access="READ_ONLY_SCHEMA_AND_EVENT_NAME_METADATA_DURING_DISCOVERY; COLLECTORS_DO_NOT_OPEN_DATABASES",
        outcome_columns_materialized=[],
        predictive_outcome_analysis_performed=False,
        production_db_writes=0,
        specification_complete=True,
    )
    (out / "summary.json").write_bytes(a.encoded(summary))
    schema = dict(
        expected_columns=SCHEMA,
        observed_headers=sorted(
            {tuple(r["schema_header"]) for r in bulk if r.get("schema_header")}
        ),
        create_time_timezone="UTC",
        grid_ms=300000,
        constraints="OI/OI value and L/S ratios finite and nonnegative; L/S ratios are unbounded, not [0,1] fractions",
        missing_is_zero=False,
        archive_physical_order_is_not_assumed_chronological=True,
    )
    (out / "schema.json").write_bytes(a.encoded(schema))
    semantics = dict(
        create_time_semantics="UNKNOWN",
        publication_availability="UNKNOWN",
        historical_period="2024/2025",
        REST_current_endpoint_timestamp_definitions={
            field: r["current_REST_timestamp_definition"]
            for field, r in zip(a.FIELDS, fields)
        },
        market_label_is_not_REST_availability=True,
        ZIP_LastModified_is_not_market_availability=True,
        fresh_alignment_cannot_be_extrapolated_to_historical_contract=True,
        arbitrary_lag_forbidden=True,
        first_client_receipt_is_only_an_observed_upper_bound_for_that_response=True,
    )
    (out / "timestamp-semantics.json").write_bytes(a.encoded(semantics))
    a.write_csv(
        out / "listing-delisting-data-boundaries.csv", cached_market_boundaries(root)
    )
    (out / "taker-semantics.json").write_bytes(a.encoded(taker_checks(ev)))
    method = dict(
        base=a.BASE,
        scope="TIMESTAMP_AND_QUALITY_AUDIT_ONLY",
        allowed_sources=[
            "data.binance.vision",
            "official Binance REST documentation",
            "official public Binance REST market data",
        ],
        symbols=list(a.SYMBOLS),
        alignment_offsets_ms=[-a.STEP, 0, a.STEP],
        offsets_are_hypotheses_not_publication_lags=True,
        historical_identity_projection=["symbol", "timestamp", "current_buy_eligible"],
        outcome_columns_read=[],
        historical_label_coverage_is_not_known_at_coverage=True,
        active_market="EXACT_PREVIOUS_COMPLETED_TRADED_1H",
        no_alias_mapping=True,
        no_replay=True,
        no_strategy=True,
        no_ML=True,
        no_deployment=True,
    )
    (out / "methodology.json").write_bytes(a.encoded(method))
    shutil.copyfile(
        out / "archive-rest-field-comparison.csv", out / "rest-archive-alignment.csv"
    )
    shutil.copyfile(
        out / "boundary-triplet-comparison.csv", out / "boundary-validation.csv"
    )
    prior = root / "artifacts/derivatives-data-availability-v1/sample-validation.csv"
    old = {
        r["key"]: r["sha256"]
        for r in csv_rows(prior)
        if r.get("family") == "metrics" and r.get("checksum_verified") == "True"
    }
    revisions = []
    for r in bulk + sample_manifest:
        key = r["url"].removeprefix("https://data.binance.vision/")
        if key in old and r.get("sha256"):
            revisions.append(
                dict(
                    url=r["url"],
                    previous_sha256=old[key],
                    current_sha256=r["sha256"],
                    status="SAME_BYTES"
                    if old[key] == r["sha256"]
                    else "REVISION_DETECTED",
                )
            )
    a.write_csv(out / "revision-checks.csv", revisions)
    quality = csv_rows(out / "historical-data-quality.csv")
    quality_summary = {
        name: sum(int(r.get(name) or 0) for r in quality)
        for name in (
            "rows",
            "out_of_order_rows",
            "duplicate_labels",
            "unaligned_labels",
            "gaps_in_sorted_unique_labels",
            "invalid_fields",
        )
    }
    proof = json_file(out / "identity-source-validation.json")
    validation = dict(
        identity_source=proof,
        quality_summary=quality_summary,
        historical_archive_status_counts=dict(Counter(r["status"] for r in bulk)),
        historical_archive_cache_root=str(
            root / "data/research-cache/metrics-semantics-daily-v1/evidence"
        ),
        revision_comparison_scope="ONLY_PRIOR_CHECKSUM_PINNED_OVERLAPPING_SAMPLES; NOT_COMPLETE_REVISION_HISTORY",
        revision_observations=len(revisions),
        revision_differences=sum(r["status"] == "REVISION_DETECTED" for r in revisions),
        outcome_analysis=False,
        new_replay=False,
        production_runtime_changed=False,
        production_db_writes=0,
        deployment=False,
    )
    (out / "source-validation.json").write_bytes(a.encoded(validation))
    report = [
        "# DERIVATIVES METRICS TIMESTAMP SEMANTICS AUDIT v1",
        "",
        "**" + overall + "**; next_research_candidate: **" + candidate + "**.",
        "",
        "`create_time_semantics = UNKNOWN`; `publication availability = UNKNOWN` for 2024/2025. No arbitrary 5/10/15-minute lag is selected.",
        "",
        "## What is proved and what is not",
        "",
        "Current official REST documentation assigns period-end labels to OI/positioning endpoints and a period-start label to taker data. Those labels do not define the archive create_time contract or the first instant of public availability. Contemporary exact value alignment can establish a sampled timestamp association, not a historical publication guarantee.",
        "",
        "ZIP HTTP Last-Modified, market/snapshot label time, and first client receipt from REST are separate clocks. Retrospective retrieval proves availability at retrieval only. The short live boundary observation gives response-specific observation bounds, not a provider-wide historical publication SLA.",
        "",
        "## Archive schema",
        "",
        ", ".join(SCHEMA),
        "",
        "Unordered archive rows and duplicate labels are retained as audit evidence. No chronological meaning is inferred from physical file order or five-minute spacing. Negative/non-finite metrics, gaps and schema variants are counted separately. L/S ratios above one are valid ratios, not impossible fractions.",
        "",
        "## Fresh archive / REST field alignment",
        "",
        "| Field | REST minus archive label ms | Counts |",
        "|---|---:|---|",
    ]
    for r in summary["field_alignment"]:
        report.append(
            "| "
            + r["archive_field"]
            + " | "
            + str(r["rest_minus_archive_ms"])
            + " | "
            + json.dumps(
                {
                    k: v
                    for k, v in r.items()
                    if k not in ("archive_field", "rest_minus_archive_ms")
                }
            )
            + " |"
        )
    report += [
        "",
        "## Per-field readiness",
        "",
        "Every field is independently SEMANTICS_AMBIGUOUS. Taker ambiguity is not an automatic OI veto: OI separately lacks the 2024/2025 archive/publication contract. Current API value/label equality alone cannot set any field READY.",
        "",
        "## Exact historical identity coverage",
        "",
        "The following is **raw label availability under the active-market guard**, not no-lookahead decision-time feature coverage. A label equal to decision time is not assumed to be already published. Actual safe decision-time coverage remains UNKNOWN for every field.",
        "",
        "| Year | Population | Field | Valid active exact-label rows / identities | Raw % |",
        "|---:|---|---|---:|---:|",
    ]
    for r in summary["exact_historical_coverage"]:
        report.append(
            f"| {r['year']} | {r['population']} | {r['field']} | {r['valid_unambiguous_value_in_active_market']} / {r['total_identity_rows']} | {r['raw_active_label_coverage_pct']} |"
        )
    report += [
        "",
        "## Active-market and outcome isolation",
        "",
        "Each identity requires an exact-symbol preceding traded futures 1h candle. Metrics cannot extend instrument lifetime; no aliases/multiplier/rebrand stitching is used. Only the three identity columns are buffered and decoded from the canonical CSV: symbol, timestamp, current_buy_eligible. Outcome payload fields are skipped by a byte-level column projection and never materialized. No MFE, MAE, forward return, stop result or PnL is analyzed.",
        "",
        "## Taker semantics",
        "",
        "REST buyVol/sellVol reconstruction is exported separately. Archive numerator, denominator and historical covered interval remain unproved: ARCHIVE_TAKER_RATIO_SEMANTICS_NOT_PROVEN. A numerical match on selected contemporary rows does not supply a historical archive contract.",
        "",
        "## Quality / revisions",
        "",
        json.dumps(validation, indent=2),
        "",
        "Hash revision comparisons cover overlapping pinned samples only. No difference in those samples would not prove that archives were never revised.",
        "",
        "## Safety",
        "",
        "Only official archive/documentation and unauthenticated public market-data GETs were used. No account/order/authenticated API, replay, strategy, ML, production DB write, service restart or deployment. Production DB discovery read only schema and event-name metadata with mode=ro and query_only=ON; audit collectors do not open a DB.",
    ]
    (out / "report.md").write_text("\n".join(report) + "\n")
    path = a.package(out, ev)
    print("FINALIZED", overall, candidate, path, a.sha(path.read_bytes()), flush=True)


if __name__ == "__main__":
    main()
