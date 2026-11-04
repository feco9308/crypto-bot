"""2026 final OOS protocol. Preregistration never opens real market data."""

import argparse
import inspect
import json
import os
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from enum import Enum
from pathlib import Path
from types import FunctionType

import requests

from research import mean_reversion_clean_validation as clean

source = clean.source
frozen = clean.frozen
BASE = "4f8129668a2828b69fde5f148fad6149ac72bb10"
NAME = "mean-reversion-2026-final-oos-prereg-v1"
EXEC_NAME = "mean-reversion-2026-final-oos-v1"
EXPLORATION_COMMIT = "37811156c7a5916e42fba1924945d66bf8980944"
EXPLORATION_SHA = "fe8f2b4c1f0cc8f01f141b5d53e02a86dd75889733c976d69c6a4a33709a8231"
CLEAN_PREREG_COMMIT = "a05f58d7f62912c6a2caab9125a7565c0c09d4d3"
CLEAN_SHA = "f50e24052dce33412b3f6a97d7bd9ef6e46f6e6b7a1f5a352cbe408a890ed99e"
BTC = "BTCUSDT"
DAY = source.DAY
START = source.stamp("2026-01-01")
END = source.stamp("2027-01-01")
ELIGIBLE_UTC = datetime(2027, 1, 1, tzinfo=timezone.utc)
MONTHS = ("2025-12",) + tuple(f"2026-{m:02d}" for m in range(1, 13))
FORBIDDEN = "FINAL_OOS_DATA_ACCESS_FORBIDDEN_DURING_PREREGISTRATION"
SEAL = object()
SCREEN = dict(
    gross_return_strictly_above=0,
    net_return_strictly_above=0,
    minimum_completed_NET_BTC_episodes=5,
    all_12_unchanged_NET_leave_month_returns_strictly_above=0,
)
PREREG_FILES = (
    "preregistration.json",
    "candidate-provenance.json",
    "methodology.json",
    "frozen-screen.json",
    "source-plan.json",
    "code-pins.json",
    "execution-gate.json",
)


class Mode(Enum):
    PREREGISTRATION = "PREREGISTRATION"
    FINAL_EXECUTION = "FINAL_EXECUTION"


def now_utc():
    return datetime.now(timezone.utc)


def require_data_mode(mode):
    if mode is not Mode.FINAL_EXECUTION:
        raise ValueError(FORBIDDEN)
    if now_utc() < ELIGIBLE_UTC:
        raise ValueError("FINAL_OOS_YEAR_NOT_COMPLETED; NO_DATA_ACCESS_NO_EXECUTION")


def code_pins():
    pins = clean.code_pins()
    pins.update(
        final_oos_module_sha256=source.sha(Path(__file__).read_bytes()),
        btc_only_namespace_binding_sha256=source.sha(
            inspect.getsource(clean.bind_btc_accounting).encode()
        ),
        independent_oracle_sha256=source.sha(
            inspect.getsource(clean.independent_oracle).encode()
        ),
        independent_verifier_sha256=source.sha(
            inspect.getsource(clean.verify_independent).encode()
        ),
    )
    return pins


def prior_bundle(path, expected):
    if source.sha(path.read_bytes()) != expected:
        raise ValueError("PRIOR_ARTIFACT_SHA_MISMATCH; STOP")
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:
            raise ValueError("PRIOR_ARTIFACT_CRC_FAILURE; STOP")
        manifest = json.loads(z.read("manifest.json"))
        for n, pin in manifest["files"].items():
            if source.sha(z.read(n)) != pin["sha256"]:
                raise ValueError("PRIOR_MEMBER_SHA_MISMATCH; STOP")
        return {
            n: json.loads(z.read(n))
            for n in (
                "summary.json",
                "delivery-metadata.json",
                "candidate-provenance.json",
            )
            if n in z.namelist()
        }


def candidate_provenance(root):
    # Only exact prior artifact paths; never enumerate a cache or inspect 2026 data.
    exploration = prior_bundle(
        root / "artifacts" / frozen.NAME / (frozen.NAME + ".zip"), EXPLORATION_SHA
    )
    previous = prior_bundle(
        root / "artifacts" / clean.NAME / (clean.NAME + ".zip"), CLEAN_SHA
    )
    s = previous["summary.json"]
    delivery = previous["delivery-metadata.json"]
    if (
        s["clean_validation_result"] != "CLEAN_VALIDATION_PASS"
        or s["candidate_status"] != "CONFIRMED_ON_2023_CLEAN_COHORT"
        or s["final_oos_eligible"] != "YES"
        or s["2023_clean_cohort_consumed"] != "YES"
        or delivery["preregistered_implementation_git_head"] != CLEAN_PREREG_COMMIT
        or delivery["final_git_head"] != BASE
    ):
        raise ValueError("REQUIRED_CLEAN_PROVENANCE_MISMATCH; STOP")
    exp = exploration["summary.json"]
    if (
        exp["final_classification"] != "ORTHOGONAL_CLASS_SIGNAL_FOUND"
        or exp["exploration_candidate"] != "MEAN_REVERSION"
    ):
        raise ValueError("EXPLORATION_PROVENANCE_MISMATCH; STOP")
    if source.sha(Path(frozen.__file__).read_bytes()) != exp["source_code_sha256"]:
        raise ValueError("FROZEN_EXPLORATION_CODE_CHANGED; STOP")
    if [
        (r["strategy"], r["portfolio"]) for r in exp["portfolio_screen"] if r["passed"]
    ] != [("S2", "BTC_ONLY")]:
        raise ValueError("PASSING_EXPLORATION_VARIANT_MISMATCH; STOP")
    return dict(
        asset=BTC,
        strategy="S2_BOLLINGER_MEAN_REVERSION_20_2",
        portfolio="BTC_ONLY",
        passing_exploration_variant="S2 BTC_ONLY",
        frozen_strategy_accounting_code_pins=previous["candidate-provenance.json"][
            "frozen_code_pins"
        ],
        exploration_commit=EXPLORATION_COMMIT,
        exploration_artifact_sha256=EXPLORATION_SHA,
        clean_prereg_implementation_commit=CLEAN_PREREG_COMMIT,
        clean_final_HEAD=BASE,
        clean_artifact_sha256=CLEAN_SHA,
        required_prior_result=s["clean_validation_result"],
        candidate_status=s["candidate_status"],
        final_oos_eligible=s["final_oos_eligible"],
        **{"2023_clean_cohort_consumed": s["2023_clean_cohort_consumed"]},
        historical_reference_only=[
            dict(
                year=2023,
                net_return_pct=s["net"]["return_pct"],
                completed_episodes=s["net"]["completed_position_episodes"],
                positive_leave_month_runs=s["positive_leave_month_runs"],
                leave_month_count=12,
            )
        ]
        + [
            dict(
                year=r["year"],
                net_return_pct=r["net_return_pct"],
                completed_episodes=r["completed_position_episodes"],
                positive_leave_month_runs=r["positive_leave_month_runs"],
                leave_month_count=r["leave_month_count"],
            )
            for r in exp["results"]
            if r["strategy"] == "S2" and r["portfolio"] == "BTC_ONLY"
        ],
    )


def archive_url(month):
    if month not in MONTHS:
        raise ValueError("ONLY_FROZEN_BTC_MONTHS; NO_2027_MARKET_PRICE")
    return f"https://data.binance.vision/data/spot/monthly/klines/{BTC}/1d/{BTC}-1d-{month}.zip"


def consumption_path(root):
    return root / "artifacts" / EXEC_NAME / "final-oos-consumption.json"


def require_unconsumed(root):
    if consumption_path(root).exists():
        raise ValueError("2026_FINAL_OOS_ALREADY_CONSUMED; NO_SECOND_EXPERIMENT")


def payloads(root, head, pins, candidate):
    if any(
        pins.get(k) != v
        for k, v in candidate["frozen_strategy_accounting_code_pins"].items()
    ):
        raise ValueError("HISTORICALLY_FROZEN_STRATEGY_CODE_PIN_MISMATCH; STOP")
    source_plan = dict(
        asset=BTC,
        timeframe="1d",
        official_source="https://data.binance.vision",
        archives=[
            dict(
                month=m,
                url=archive_url(m),
                checksum_url=archive_url(m) + ".CHECKSUM",
                role="INDICATOR_WARMUP_ONLY" if m == "2025-12" else "FINAL_OOS_COHORT",
            )
            for m in MONTHS
        ],
        parser="Unchanged official Spot daily 12-column parser, microsecond normalization, complete-month/gap/OHLC checks",
        parser_sha256=pins["daily_parser"],
        expected_archives=13,
        expected_warmup_rows=31,
        expected_cohort_rows=365,
        checksum_policy="Official SHA256 required for every ZIP. Freeze exact 13 input versions before consumption; no refresh during recovery.",
        availability="NOT_INSPECTED_DURING_PREREGISTRATION",
        real_2026_price_fields_read=False,
    )
    gate = dict(
        mode="PREREGISTRATION",
        current_status="NO_EXECUTION",
        earliest_utc="2027-01-01T00:00:00Z",
        all_required=[
            "UTC >= 2027-01-01T00:00:00Z",
            "All 13 official monthly BTCUSDT 1d archives complete",
            "All 13 official CHECKSUMs available and valid",
            "Exact preregistered implementation HEAD",
            "Exact strategy/accounting/adapter code pins",
            "No prior final OOS consumed marker",
        ],
        archive_availability="NOT_INSPECTED_DURING_PREREGISTRATION",
        checksum_availability="NOT_INSPECTED_DURING_PREREGISTRATION",
        consumed_marker=str(consumption_path(root)),
        marker_created_now=False,
        no_automatic_schedule_or_execution=True,
    )
    prereg = dict(
        status="FINAL_OOS_PREREGISTERED",
        base_git_sha=BASE,
        implementation_HEAD=head,
        repository_root=str(root),
        candidate="S2 BTC_ONLY",
        dataset_role="FINAL_OUT_OF_SAMPLE",
        start="2026-01-01T00:00:00Z",
        end_exclusive="2027-01-01T00:00:00Z",
        warmup_only="2025-12",
        starting_equity=1000,
        net_fee_per_side="0.001",
        net_adverse_slippage_per_side="0.0005",
        final_classifications_allowed=["FINAL_OOS_PASS", "FINAL_OOS_FAIL"],
        real_market_data_access_during_preregistration="FORBIDDEN",
        real_2026_market_data_read=False,
        final_oos_executed=False,
        ytd_preview_performed=False,
        external_anchor="Future execution requires this preregistration.json SHA256 as an explicit expected value; no automatic run.",
    )
    method = dict(
        strategy="Identical frozen S2 observation: inclusive SMA20, sample std ddof=1, lower=SMA20-2*std; entry close<lower; exit close>=SMA20",
        accounting="Same frozen _simulate/EpisodeLedger and BTC-only namespace binding as 2023; no financial formula changes",
        execution="Next IN-COHORT daily OPEN only; source candle completed; start flat without inherited warmup position",
        terminal="Final completed 2026-12-31 close less estimated sell costs, not a SELL or completed episode; no 2027 open/close",
        leave_month="Delete each calendar month from unchanged NET daily equity returns; compound remainder; no refit/state/threshold changes",
        independent_oracle="Separate frozen float indicator/state/cash implementation from clean validation, rebound only to 2026 boundaries; no primary strategy/simulator call",
        data_guard=FORBIDDEN,
        synthetic_tests_only=True,
        source_plan_is_not_source_inspection=True,
        one_shot="Exclusive consumed marker and directory fsync before first performance calculation; remains for PASS, FAIL, interruption",
        recovery="Audit-only of existing pinned inputs/code/result hashes; no network, resimulation or automatic retry. Missing/incomplete results after interruption require STOP and separate authorization; no new statistical experiment or changed source version.",
        successful_result=dict(
            candidate_status="FINAL_OOS_CONFIRMED",
            paper_implementation_eligible="YES",
            automatic_deployment=False,
        ),
        failed_result=dict(
            candidate_status="FINAL_OOS_FAILED",
            paper_implementation_eligible="NO",
            tuning_permitted=False,
        ),
        parameters_changed=False,
        regime_filter=False,
        alternative_asset=False,
        production_change=False,
    )
    return dict(
        zip(
            PREREG_FILES,
            (
                prereg,
                candidate,
                method,
                dict(**SCREEN, accepted_months=[f"2026-{m:02d}" for m in range(1, 13)]),
                source_plan,
                pins,
                gate,
            ),
            strict=True,
        )
    )


def write_identical_or_exclusive(path, value):
    blob = source.encoded(value)
    if path.exists():
        if path.read_bytes() != blob:
            raise ValueError("FROZEN_PREREGISTRATION_ARTIFACT_CHANGED")
    else:
        clean.exclusive_json(path, value)


def preregister(root):
    clean.require_clean()
    require_unconsumed(root)
    head = clean.git_head()
    committed = subprocess.check_output(
        ["git", "show", head + ":research/mean_reversion_2026_final_oos.py"],
        cwd=Path(__file__).resolve().parent.parent,
    )
    if committed != Path(__file__).read_bytes():
        raise ValueError("PREREG_MODULE_MUST_BE_COMMITTED")
    candidate = candidate_provenance(root)
    pins = code_pins()
    out = root / "artifacts" / NAME
    out.mkdir(parents=True, exist_ok=True)
    for name, value in payloads(root, head, pins, candidate).items():
        write_identical_or_exclusive(out / name, value)
    manifest = dict(
        base_git_sha=BASE,
        implementation_HEAD=head,
        files={
            n: dict(
                sha256=source.sha((out / n).read_bytes()),
                bytes=(out / n).stat().st_size,
            )
            for n in PREREG_FILES
        },
    )
    write_identical_or_exclusive(out / "manifest.json", manifest)
    print(
        "FINAL_OOS_PREREGISTERED",
        head,
        "preregistration_sha256="
        + source.sha((out / "preregistration.json").read_bytes()),
        flush=True,
    )


def verify_preregistration(root, expected_sha):
    clean.require_clean()
    out = root / "artifacts" / NAME
    path = out / "preregistration.json"
    if not expected_sha or source.sha(path.read_bytes()) != expected_sha:
        raise ValueError("EXTERNAL_PREREGISTRATION_ANCHOR_MISMATCH")
    manifest = json.loads((out / "manifest.json").read_text())
    for n in PREREG_FILES:
        if source.sha((out / n).read_bytes()) != manifest["files"][n]["sha256"]:
            raise ValueError("PREREGISTRATION_MEMBER_CHANGED")
    prereg = json.loads(path.read_text())
    pins = json.loads((out / "code-pins.json").read_text())
    if prereg["implementation_HEAD"] != clean.git_head() or prereg[
        "repository_root"
    ] != str(root):
        raise ValueError("EXACT_FROZEN_HEAD_OR_ROOT_MISMATCH")
    if pins != code_pins():
        raise ValueError("STRATEGY_ACCOUNTING_CODE_HASH_MISMATCH")
    return prereg


@dataclass(frozen=True)
class SourcePermit:
    root: Path
    prereg_sha256: str
    implementation_head: str
    seal: object


def authorize_sources(root, mode, expected_sha):
    require_data_mode(mode)  # date/mode checks happen BEFORE any archive/cache I/O
    prereg = verify_preregistration(root, expected_sha)
    require_unconsumed(root)
    return SourcePermit(root, expected_sha, prereg["implementation_HEAD"], SEAL)


def require_source_permit(mode, permit):
    require_data_mode(mode)
    if not isinstance(permit, SourcePermit) or permit.seal is not SEAL:
        raise ValueError("VERIFIED_EXECUTION_SOURCE_PERMIT_REQUIRED")
    if permit.implementation_head != clean.git_head():
        raise ValueError("FROZEN_HEAD_CHANGED")
    require_unconsumed(permit.root)


def archive_response(root, url, mode, permit, capture=False):
    require_source_permit(mode, permit)
    if root != permit.root or url not in {
        archive_url(m) + suffix for m in MONTHS for suffix in ("", ".CHECKSUM")
    }:
        raise ValueError("ONLY_EXACT_OFFICIAL_SOURCE_PLAN")
    cache = root / "data/research-cache" / EXEC_NAME
    path = cache / (source.sha(url.encode()) + ".json")
    if path.exists():
        r = json.loads(path.read_text())
        if (
            r["url"] != url
            or r["status"] != 200
            or source.sha(bytes.fromhex(r["body_hex"])) != r["sha256"]
        ):
            raise ValueError("CACHE_CHECKSUM_MISMATCH")
        return r
    if not capture:
        raise ValueError("INCOMPLETE_OFFICIAL_ARCHIVE_SET; NO_EXECUTION")
    cache.mkdir(parents=True, exist_ok=True)
    with requests.Session() as session:
        session.trust_env = False
        with session.get(url, timeout=30, allow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError(
                    f"OFFICIAL_ARCHIVE_HTTP_{response.status_code}; NO_EXECUTION_NO_RETRY"
                )
            body = response.content
            if len(body) > 1000000:
                raise ValueError("ARCHIVE_BODY_LIMIT")
            r = dict(url=url, status=200, sha256=source.sha(body), body_hex=body.hex())
    clean.exclusive_json(path, r)
    return r


def decode_archive(month, body, check):
    # Pure bytes parser. Production bytes reach here only through guarded archive_response.
    if month not in MONTHS:
        raise ValueError("OUTSIDE_FROZEN_MONTHS")
    expected = check.decode().split()[0].lower()
    if source.sha(body) != expected:
        raise ValueError("OFFICIAL_CHECKSUM_MISMATCH; NO_EXECUTION")
    return source.parse_zip(body, month), expected


def source_set_ready(manifest):
    if len(manifest) != 13 or {r["month"] for r in manifest} != set(MONTHS):
        raise ValueError("INCOMPLETE_SOURCE_ARCHIVE_SET; NO_EXECUTION")
    if any(
        r["status"] != "CHECKSUM_VALIDATED"
        or r["symbol"] != BTC
        or len(r["sha256"]) != 64
        or len(r["checksum_sha256"]) != 64
        for r in manifest
    ):
        raise ValueError("UNVERIFIED_SOURCE_CHECKSUM; NO_EXECUTION")
    if (
        sum(r["rows"] for r in manifest if r["month"] == "2025-12") != 31
        or sum(r["rows"] for r in manifest if r["month"] != "2025-12") != 365
    ):
        raise ValueError("INCOMPLETE_SOURCE_ROWS; NO_EXECUTION")


@dataclass(frozen=True)
class VerifiedData:
    history: dict
    manifest: list
    permit: SourcePermit
    seal: object


def load_real_data(root, mode, permit, capture=False):
    require_source_permit(mode, permit)
    history = {}
    manifest = []
    for m in MONTHS:
        url = archive_url(m)
        check = archive_response(root, url + ".CHECKSUM", mode, permit, capture)
        archive = archive_response(root, url, mode, permit, capture)
        bars, pin = decode_archive(
            m, bytes.fromhex(archive["body_hex"]), bytes.fromhex(check["body_hex"])
        )
        history.update({b.start: b for b in bars})
        manifest.append(
            dict(
                symbol=BTC,
                month=m,
                url=url,
                sha256=pin,
                checksum_sha256=check["sha256"],
                rows=len(bars),
                status="CHECKSUM_VALIDATED",
            )
        )
    source_set_ready(manifest)
    if set(history) != set(range(source.stamp("2025-12-01"), END, DAY)):
        raise ValueError("GAP_DUPLICATE_OR_OUTSIDE_COHORT")
    return VerifiedData(history, manifest, permit, SEAL)


@dataclass(frozen=True)
class SyntheticFixture:
    history: dict
    start: int = START
    end: int = END
    label: str = "SYNTHETIC_ONLY"


@dataclass(frozen=True)
class PerformancePermit:
    history: dict
    source_permit: SourcePermit
    inputs_sha256: str
    seal: object


def authorize_performance(bundle, mode):
    require_source_permit(mode, bundle.permit)
    verify_preregistration(bundle.permit.root, bundle.permit.prereg_sha256)
    if bundle.seal is not SEAL:
        raise ValueError("VERIFIED_DATA_REQUIRED")
    source_set_ready(bundle.manifest)
    out = bundle.permit.root / "artifacts" / EXEC_NAME
    out.mkdir(parents=True, exist_ok=True)
    inputs = dict(
        preregistration_sha256=bundle.permit.prereg_sha256,
        implementation_HEAD=bundle.permit.implementation_head,
        code_pins=code_pins(),
        archive_manifest=bundle.manifest,
    )
    write_identical_or_exclusive(out / "frozen-inputs.json", inputs)
    inputs_sha = source.sha((out / "frozen-inputs.json").read_bytes())
    clean.exclusive_json(
        consumption_path(bundle.permit.root),
        dict(
            **{"2026_final_oos_consumed": "YES"},
            started_at_UTC=now_utc().isoformat(),
            preregistration_sha256=bundle.permit.prereg_sha256,
            implementation_HEAD=bundle.permit.implementation_head,
            frozen_inputs_sha256=inputs_sha,
            remains_consumed_on=["PASS", "FAIL", "TECHNICAL_INTERRUPTION"],
            new_experiment_retry_allowed=False,
        ),
    )
    fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return PerformancePermit(bundle.history, bundle.permit, inputs_sha, SEAL)


def require_compute_auth(history, start, end, auth):
    if isinstance(auth, SyntheticFixture):
        if (
            auth.label != "SYNTHETIC_ONLY"
            or auth.history is not history
            or auth.start != start
            or auth.end != end
        ):
            raise ValueError(FORBIDDEN)
    elif isinstance(auth, PerformancePermit) and auth.seal is SEAL:
        require_data_mode(Mode.FINAL_EXECUTION)
        if auth.history is not history or start != START or end != END:
            raise ValueError("EXACT_FINAL_COHORT_REQUIRED")
        marker = json.loads(consumption_path(auth.source_permit.root).read_text())
        if (
            marker["frozen_inputs_sha256"] != auth.inputs_sha256
            or marker["2026_final_oos_consumed"] != "YES"
        ):
            raise ValueError("CONSUMPTION_REQUIRED_BEFORE_PERFORMANCE")
    else:
        raise ValueError(FORBIDDEN)
    if start < START or end > END or end <= start:
        raise ValueError("ONLY_2026_COHORT; NO_2027_PRICE")


def build_signals(history, start, end, auth):
    require_compute_auth(history, start, end, auth)
    active = False
    signals = {}
    with localcontext() as ctx:
        ctx.prec = 45
        for t in range(start, end, DAY):
            row = frozen.observation("S2", history, t - DAY, active, t)
            row.update(symbol=BTC, signal_id=f"S2:{BTC}:{t}")
            signals[t] = {BTC: row}
            active = row["active_after"]
    return signals


def independent_oracle(history, start, end, auth):
    require_compute_auth(history, start, end, auth)
    scope = clean.__dict__.copy()
    scope.update(START=start, END=end)
    separate = FunctionType(
        clean.independent_oracle.__code__, scope, "final_oos_independent_oracle"
    )
    return separate({BTC: history})


def calculate(history, start, end, auth):
    require_compute_auth(history, start, end, auth)
    signals = build_signals(history, start, end, auth)
    engine = clean.bind_btc_accounting()
    with localcontext() as ctx:
        ctx.prec = 45
        gross = engine(
            {BTC: history},
            signals,
            "S2",
            "BTC_ONLY",
            start,
            end,
            Decimal(0),
            Decimal(0),
        )
        net = engine(
            {BTC: history},
            signals,
            "S2",
            "BTC_ONLY",
            start,
            end,
            source.FEE,
            source.SLIP,
        )
    # Reuse the separate verification algorithm, with this independently bounded oracle.
    scope = clean.__dict__.copy()
    scope.update(START=start, END=end)
    scope["independent_oracle"] = lambda data: independent_oracle(
        data[BTC], start, end, auth
    )
    verifier = FunctionType(
        clean.verify_independent.__code__, scope, "final_oos_verify_independent"
    )
    proof = verifier(signals, gross, net, {BTC: history})
    proof.update(
        indicator_observations=(end - start) // DAY,
        daily_cash_observations=2 * ((end - start) // DAY),
    )
    return dict(signals=signals, gross=gross, net=net, independent_validation=proof)


def run_synthetic(fixture):
    if not isinstance(fixture, SyntheticFixture):
        raise ValueError(FORBIDDEN)
    return calculate(fixture.history, fixture.start, fixture.end, fixture)


def final_screen(gross, net, leave):
    criteria = dict(
        gross_return_positive=gross["return_pct"] > 0,
        net_return_positive=net["return_pct"] > 0,
        completed_NET_BTC_episodes_at_least_five=net["completed_position_episodes"]
        >= 5,
        all_twelve_NET_leave_month_returns_positive=len(leave) == 12
        and {r["omitted_month"] for r in leave}
        == {f"2026-{m:02d}" for m in range(1, 13)}
        and all(r["net_return_pct"] > 0 for r in leave),
    )
    passed = all(criteria.values())
    return dict(
        final_oos_result="FINAL_OOS_PASS" if passed else "FINAL_OOS_FAIL",
        candidate_status="FINAL_OOS_CONFIRMED" if passed else "FINAL_OOS_FAILED",
        paper_implementation_eligible="YES" if passed else "NO",
        criteria=criteria,
        **{"2026_final_oos_consumed": "YES"},
    )


def execute(root, mode, expected_sha, capture=False):
    permit = authorize_sources(root, mode, expected_sha)
    bundle = load_real_data(root, mode, permit, capture)
    performance = authorize_performance(bundle, mode)
    runs = calculate(bundle.history, START, END, performance)
    result = final_screen(
        runs["gross"]["metrics"], runs["net"]["metrics"], runs["net"]["leave_month"]
    )
    out = root / "artifacts" / EXEC_NAME
    summary = dict(
        **result,
        gross=runs["gross"]["metrics"],
        net=runs["net"]["metrics"],
        independent_validation=runs["independent_validation"],
        preregistration_sha256=expected_sha,
        frozen_inputs_sha256=performance.inputs_sha256,
        production_behavior_changed=False,
        production_db_writes=0,
        deployment=False,
    )
    (out / "summary.json").write_bytes(source.encoded(summary))
    source.write_csv(
        out / "episode-results.csv", clean.episode_audit(runs["gross"], runs["net"])
    )
    for filename, key in [
        ("monthly-results.csv", "monthly"),
        ("execution-audit.csv", "events"),
        ("daily-equity.csv", "daily"),
    ]:
        source.write_csv(
            out / filename,
            [
                dict(cost_mode=mode, **r)
                for mode, run in [("GROSS", runs["gross"]), ("NET", runs["net"])]
                for r in run[key]
            ],
        )
    source.write_csv(out / "leave-month-sensitivity.csv", runs["net"]["leave_month"])
    files = {
        p.name: dict(sha256=source.sha(p.read_bytes()), bytes=p.stat().st_size)
        for p in sorted(out.iterdir())
        if p.is_file() and p.name != "manifest.json"
    }
    (out / "manifest.json").write_bytes(
        source.encoded(
            dict(implementation_HEAD=permit.implementation_head, files=files)
        )
    )
    print("FINAL_OOS_EXECUTION_COMPLETED", result["final_oos_result"], flush=True)


def audit_existing(root, mode, expected_sha):
    # No recovery simulation/retry. Audit is unavailable during preregistration too.
    require_data_mode(mode)
    verify_preregistration(root, expected_sha)
    out = root / "artifacts" / EXEC_NAME
    marker = json.loads(consumption_path(root).read_text())
    if marker["2026_final_oos_consumed"] != "YES":
        raise ValueError("CONSUMED_MARKER_REQUIRED")
    inputs = out / "frozen-inputs.json"
    if source.sha(inputs.read_bytes()) != marker["frozen_inputs_sha256"]:
        raise ValueError("FROZEN_INPUTS_CHANGED; STOP")
    frozen_inputs = json.loads(inputs.read_text())
    source_set_ready(frozen_inputs["archive_manifest"])
    cache = root / "data/research-cache" / EXEC_NAME
    for row in frozen_inputs["archive_manifest"]:
        records = []
        for suffix in ("", ".CHECKSUM"):
            url = row["url"] + suffix
            record = json.loads(
                (cache / (source.sha(url.encode()) + ".json")).read_text()
            )
            if (
                record["url"] != url
                or source.sha(bytes.fromhex(record["body_hex"])) != record["sha256"]
            ):
                raise ValueError("FROZEN_SOURCE_CACHE_CORRUPTED; STOP")
            records.append(record)
        if (
            records[0]["sha256"] != row["sha256"]
            or records[1]["sha256"] != row["checksum_sha256"]
            or bytes.fromhex(records[1]["body_hex"]).decode().split()[0].lower()
            != row["sha256"]
        ):
            raise ValueError("FROZEN_SOURCE_VERSION_CHANGED; STOP")
    manifest_path = out / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(
            "TECHNICAL_INTERRUPTION; AUDIT_ONLY; NO_AUTOMATIC_RETRY_OR_NEW_EXPERIMENT"
        )
    manifest = json.loads(manifest_path.read_text())
    for n, p in manifest["files"].items():
        if source.sha((out / n).read_bytes()) != p["sha256"]:
            raise ValueError("EXECUTION_ARTIFACT_HASH_MISMATCH; STOP")
    print("AUDIT_ONLY_PASS; NO_NETWORK_NO_RESIMULATION", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["preregister", "execute", "audit"])
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--expected-prereg-sha256")
    p.add_argument("--capture", action="store_true")
    args = p.parse_args()
    root = args.root.resolve()
    if args.action == "preregister":
        if args.capture or args.expected_prereg_sha256:
            raise ValueError(FORBIDDEN)
        preregister(root)
    elif args.action == "execute":
        execute(root, Mode.FINAL_EXECUTION, args.expected_prereg_sha256, args.capture)
    else:
        if args.capture:
            raise ValueError("AUDIT_CANNOT_DOWNLOAD")
        audit_existing(root, Mode.FINAL_EXECUTION, args.expected_prereg_sha256)


if __name__ == "__main__":
    main()
