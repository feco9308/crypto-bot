"""One-shot BTC S2 2023 confirmation. Immutable preregistration, no optimization."""

import argparse
import inspect
import json
import math
import os
import statistics
import subprocess
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from types import FunctionType, SimpleNamespace

import requests

from research import orthogonal_strategy_class as frozen

source = frozen.source
BASE = "37811156c7a5916e42fba1924945d66bf8980944"
NAME = "mean-reversion-2023-clean-validation-v1"
EXPLORATION_SHA = "fe8f2b4c1f0cc8f01f141b5d53e02a86dd75889733c976d69c6a4a33709a8231"
BTC = "BTCUSDT"
START = source.stamp("2023-01-01")
END = source.stamp("2024-01-01")
DAY = source.DAY
MONTHS = ("2022-12",) + tuple(f"2023-{m:02d}" for m in range(1, 13))
SCREEN = dict(
    gross_return_strictly_above=0,
    net_return_strictly_above=0,
    minimum_completed_NET_BTC_episodes=5,
    all_12_unchanged_NET_leave_month_returns_strictly_above=0,
)


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def exclusive_json(path, value):
    # O_EXCL is the cross-process one-shot guard; fsync before any outcome work.
    blob = source.encoded(value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    return source.sha(blob)


def code_pins():
    functions = {
        "S2_observation": frozen.observation,
        "frozen_accounting": frozen._simulate,
        "episode_ledger": frozen.EpisodeLedger,
        "allocation_wrapper": source.reallocate,
        "allocation_math": source._reallocate,
        "leave_month": source.leave_month,
        "daily_parser": source.parse_zip,
    }
    return {k: source.sha(inspect.getsource(v).encode()) for k, v in functions.items()}


def git_head():
    cwd = Path(__file__).resolve().parent.parent
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=cwd, text=True
    ).strip()


def require_clean():
    cwd = Path(__file__).resolve().parent.parent
    if subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=cwd, text=True
    ).strip():
        raise ValueError("VALIDATOR_WORKTREE_MUST_BE_CLEAN")


def exploration_provenance(root):
    folder = root / "artifacts" / frozen.NAME
    path = folder / (frozen.NAME + ".zip")
    if source.sha(path.read_bytes()) != EXPLORATION_SHA:
        raise ValueError("EXPLORATION_FINGERPRINT_CHANGED")
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:
            raise ValueError("EXPLORATION_CRC_FAILURE")
        manifest = json.loads(z.read("manifest.json"))
        for n, pin in manifest["files"].items():
            if source.sha(z.read(n)) != pin["sha256"]:
                raise ValueError("EXPLORATION_MANIFEST_MISMATCH")
        doc = json.loads(z.read("summary.json"))
    if (
        doc["final_classification"] != "ORTHOGONAL_CLASS_SIGNAL_FOUND"
        or doc["exploration_candidate"] != "MEAN_REVERSION"
    ):
        raise ValueError("FROZEN_CANDIDATE_NOT_ELIGIBLE")
    passed = [r for r in doc["portfolio_screen"] if r["passed"]]
    if [(r["strategy"], r["portfolio"]) for r in passed] != [("S2", "BTC_ONLY")]:
        raise ValueError("PASSING_VARIANT_MISMATCH")
    refs = [
        r
        for r in doc["results"]
        if r["strategy"] == "S2" and r["portfolio"] == "BTC_ONLY"
    ]
    return dict(
        exploration_bundle=str(path),
        exploration_sha256=EXPLORATION_SHA,
        final_exploration_classification=doc["final_classification"],
        exploration_candidate=doc["exploration_candidate"],
        passing_exploration_variant="S2 BTC_ONLY",
        references=[
            {
                k: r[k]
                for k in (
                    "year",
                    "gross_return_pct",
                    "net_return_pct",
                    "completed_position_episodes",
                    "positive_leave_month_runs",
                    "leave_month_count",
                )
            }
            for r in refs
        ],
    )


def archive_url(month):
    if month not in MONTHS:
        raise ValueError("ONLY_BTC_2022_DEC_WARMUP_AND_2023")
    return f"https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-{month}.zip"


def cached_response(root, url, capture, session):
    if url not in {
        archive_url(m) + suffix for m in MONTHS for suffix in ("", ".CHECKSUM")
    }:
        raise ValueError("OFFICIAL_FROZEN_ARCHIVE_ONLY")
    token = source.sha(url.encode()) + ".json"
    cache = root / "data/research-cache" / NAME
    old = root / "data/research-cache" / source.NAME / token
    path = old if old.exists() else cache / token
    if path.exists():
        r = json.loads(path.read_text())
        if (
            r["url"] != url
            or r["status"] != 200
            or source.sha(bytes.fromhex(r["body_hex"])) != r["sha256"]
        ):
            raise ValueError("CACHE_INTEGRITY_FAILURE")
        return r, str(path), "REUSED_EXISTING_VALIDATED_CACHE"
    if not capture:
        raise ValueError("OFFLINE_ARCHIVE_MISSING " + url)
    cache.mkdir(parents=True, exist_ok=True)
    with session.get(url, timeout=30, allow_redirects=False) as response:
        if response.status_code != 200:
            raise RuntimeError(
                f"OFFICIAL_ARCHIVE_HTTP_{response.status_code}; STOP_NO_RETRY"
            )
        body = response.content
        if len(body) > 1000000:
            raise ValueError("BODY_LIMIT")
        r = dict(
            url=url,
            status=200,
            sha256=source.sha(body),
            body_hex=body.hex(),
            received_at=utc_now(),
            last_modified=response.headers.get("Last-Modified"),
        )
    exclusive_json(path, r)
    return r, str(path), "NEW_OFFICIAL_CHECKSUM_PIN"


def load_btc(root, capture=False):
    history = {}
    manifest = []
    with requests.Session() as session:
        session.trust_env = False
        for month in MONTHS:
            url = archive_url(month)
            check, check_path, check_mode = cached_response(
                root, url + ".CHECKSUM", capture, session
            )
            record, path, mode = cached_response(root, url, capture, session)
            expected = bytes.fromhex(check["body_hex"]).decode().split()[0].lower()
            if expected != record["sha256"]:
                raise ValueError("OFFICIAL_CHECKSUM_MISMATCH")
            bars = source.parse_zip(bytes.fromhex(record["body_hex"]), month)
            history.update({b.start: b for b in bars})
            manifest.append(
                dict(
                    symbol=BTC,
                    month=month,
                    url=url,
                    sha256=expected,
                    checksum_sha256=check["sha256"],
                    cache_path=path,
                    checksum_cache_path=check_path,
                    cache_mode=mode,
                    checksum_cache_mode=check_mode,
                    rows=len(bars),
                    role="INDICATOR_WARMUP_ONLY"
                    if month == "2022-12"
                    else "CONFIRMATORY_COHORT",
                )
            )
    if (
        len(history) != 396
        or min(history) != source.stamp("2022-12-01")
        or max(history) != END - DAY
    ):
        raise ValueError("EXACT_SOURCE_BOUNDARY_REQUIRED")
    return {BTC: history}, manifest


def bind_btc_accounting():
    """Same code objects, BTC-only quote iteration namespace; originals untouched."""
    alloc_globals = source.__dict__.copy()
    alloc_globals["SYMBOLS"] = (BTC,)
    alloc_globals["_reallocate"] = FunctionType(
        source._reallocate.__code__, alloc_globals, "_reallocate"
    )
    allocator = FunctionType(source.reallocate.__code__, alloc_globals, "reallocate")
    proxy = SimpleNamespace(
        SYMBOLS=(BTC,),
        reallocate=allocator,
        iso=source.iso,
        stamp=source.stamp,
        leave_month=source.leave_month,
    )
    scope = frozen.__dict__.copy()
    scope["source"] = proxy
    return FunctionType(frozen._simulate.__code__, scope, "_simulate_btc_only")


def btc_signals(data, start=START, end=END):
    if set(data) != {BTC} or start < START or end > END or end <= start:
        raise ValueError("ONLY_2023_BTC_SCOPE")
    active = False
    signals = {}
    with localcontext() as ctx:
        ctx.prec = 45
        for t in range(start, end, DAY):
            r = frozen.observation("S2", data[BTC], t - DAY, active, t)
            r.update(symbol=BTC, signal_id=f"S2:{BTC}:{t}")
            active = r["active_after"]
            signals[t] = {BTC: r}
    return signals


def primary(data):
    if set(data) != {BTC}:
        raise ValueError("BTC_ONLY")
    signals = btc_signals(data)
    engine = bind_btc_accounting()
    with localcontext() as ctx:
        ctx.prec = 45
        gross = engine(
            data, signals, "S2", "BTC_ONLY", START, END, Decimal(0), Decimal(0)
        )
        net = engine(
            data, signals, "S2", "BTC_ONLY", START, END, source.FEE, source.SLIP
        )
    return signals, gross, net


def independent_oracle(data):
    """Independent float indicators and direct cash formulas, no frozen functions."""
    history = data[BTC]
    observations = []
    states = {}
    active = False
    for t in range(START, END, DAY):
        closes = [float(history[t - k * DAY].close) for k in range(1, 21)]
        mean = statistics.mean(closes)
        std = statistics.stdev(closes)
        lower = mean - 2 * std
        after = closes[0] < mean if active else closes[0] < lower
        observations.append(
            dict(
                execution_open=source.iso(t),
                sma20=mean,
                std20=std,
                lower_band=lower,
                active_before=active,
                active_after=after,
            )
        )
        states[t] = after
        active = after
    paths = {}
    for mode, fee, slip in [("GROSS", 0.0, 0.0), ("NET", 0.001, 0.0005)]:
        cash = 1000.0
        quantity = 0.0
        daily = []
        fills = []
        fees = slippages = 0.0
        completed = []
        opened = None
        entry_cash = 0.0
        prior = 1000.0
        for t in range(START, END, DAY):
            op = float(history[t].open)
            if states[t] and quantity == 0:
                price = op * (1 + slip)
                quantity = cash / (price * (1 + fee))
                entry_cash = cash
                cost = quantity * price * fee
                adverse = quantity * (price - op)
                fees += cost
                slippages += adverse
                cash = 0.0
                opened = t
                fills.append(
                    dict(
                        timestamp=source.iso(t),
                        side="BUY",
                        price=price,
                        fee=cost,
                        slippage=adverse,
                    )
                )
            elif not states[t] and quantity:
                price = op * (1 - slip)
                cost = quantity * price * fee
                adverse = quantity * (op - price)
                cash = quantity * price - cost
                fees += cost
                slippages += adverse
                completed.append(
                    dict(
                        entry=source.iso(opened),
                        exit=source.iso(t),
                        pnl=cash - entry_cash,
                        holding_days=(t - opened) / DAY,
                    )
                )
                fills.append(
                    dict(
                        timestamp=source.iso(t),
                        side="SELL",
                        price=price,
                        fee=cost,
                        slippage=adverse,
                    )
                )
                quantity = 0.0
            equity = cash + quantity * float(history[t].close)
            if t + DAY == END:
                value = quantity * float(history[t].close)
                exit_slip = value * slip
                exit_fee = value * (1 - slip) * fee
                equity -= exit_slip + exit_fee
                fees += exit_fee
                slippages += exit_slip
            daily.append(
                dict(
                    day=source.iso(t),
                    equity=equity,
                    ret=equity / prior - 1,
                    month=source.iso(t)[:7],
                )
            )
            prior = equity
        leave = []
        for month in sorted({r["month"] for r in daily}):
            leave.append(
                dict(
                    omitted_month=month,
                    return_pct=(
                        math.prod(1 + r["ret"] for r in daily if r["month"] != month)
                        - 1
                    )
                    * 100,
                )
            )
        paths[mode] = dict(
            ending_equity=prior,
            fees=fees,
            slippage=slippages,
            daily=daily,
            fills=fills,
            completed=completed,
            leave_month=leave,
        )
    return observations, paths


def verify_independent(signals, gross, net, data):
    observed, paths = independent_oracle(data)
    for row in observed:
        t = source.stamp(row["execution_open"])
        s = signals[t][BTC]
        if (s["active_before"], s["active_after"]) != (
            row["active_before"],
            row["active_after"],
        ):
            raise ValueError("INDEPENDENT_STATE_DISAGREEMENT")
        for field, other in [
            ("sma20", "sma20"),
            ("sample_std20", "std20"),
            ("lower_band", "lower_band"),
        ]:
            if abs(float(s[field]) - row[other]) > 1e-8:
                raise ValueError("INDEPENDENT_INDICATOR_DISAGREEMENT")
    for mode, result in [("GROSS", gross), ("NET", net)]:
        check = paths[mode]
        metrics = result["metrics"]
        if abs(metrics["ending_equity"] - check["ending_equity"]) > 1e-7:
            raise ValueError("INDEPENDENT_CASH_DISAGREEMENT")
        for row, other in zip(result["daily"], check["daily"], strict=True):
            if abs(row["equity"] - other["equity"]) > 1e-7:
                raise ValueError("INDEPENDENT_DAILY_CASH_DISAGREEMENT")
        fills = [
            r for r in result["events"] if r["event"] == "EXECUTED_NEXT_DAILY_OPEN"
        ]
        for row, other in zip(fills, check["fills"], strict=True):
            if (row["timestamp"], row["side"]) != (
                other["timestamp"],
                other["side"],
            ) or abs(float(row["fill_price"]) - other["price"]) > 1e-8:
                raise ValueError("INDEPENDENT_FILL_DISAGREEMENT")
            if source.stamp(row["source_close"]) >= source.stamp(row["timestamp"]):
                raise ValueError("SAME_BAR_FILL")
        for field, other in [("fee", "fees"), ("modeled_slippage", "slippage")]:
            if abs(metrics[field] - check[other]) > 1e-8:
                raise ValueError("INDEPENDENT_COST_DISAGREEMENT")
        if metrics["completed_position_episodes"] != len(check["completed"]):
            raise ValueError("INDEPENDENT_EPISODE_DISAGREEMENT")
        for row, other in zip(result["leave_month"], check["leave_month"], strict=True):
            if (
                row["omitted_month"] != other["omitted_month"]
                or abs(row["net_return_pct"] - other["return_pct"]) > 1e-7
            ):
                raise ValueError("INDEPENDENT_LEAVE_MONTH_DISAGREEMENT")
    return dict(
        status="PASS",
        indicator_observations=365,
        daily_cash_observations=730,
        gross_net_paths=2,
        same_strategy_function_used_as_oracle=False,
        indicator_state_fill_cash_cost_episode_leave_month_checks="PASS",
    )


def confirmatory_screen(gross, net, leave):
    exact_months = {f"2023-{m:02d}" for m in range(1, 13)}
    criteria = dict(
        gross_return_positive=gross["return_pct"] > 0,
        net_return_positive=net["return_pct"] > 0,
        completed_net_episodes_at_least_five=net["completed_position_episodes"] >= 5,
        all_twelve_net_leave_month_returns_positive=len(leave) == 12
        and {r["omitted_month"] for r in leave} == exact_months
        and all(r["net_return_pct"] > 0 for r in leave),
    )
    passed = all(criteria.values())
    return dict(
        clean_validation_result="CLEAN_VALIDATION_PASS"
        if passed
        else "CLEAN_VALIDATION_FAIL",
        candidate_status="CONFIRMED_ON_2023_CLEAN_COHORT"
        if passed
        else "CLEAN_VALIDATION_FAILED",
        final_oos_eligible="YES" if passed else "NO",
        criteria=criteria,
        frozen_criteria=SCREEN,
        **{"2023_clean_cohort_consumed": "YES"},
    )


def episode_audit(gross, net):
    output = []

    def lookup(result):
        return {
            r["timestamp"]: r
            for r in result["events"]
            if r["event"] == "EXECUTED_NEXT_DAILY_OPEN"
        }

    gf, nf = lookup(gross), lookup(net)
    for ge, ne in zip(gross["episodes"], net["episodes"], strict=True):
        if (ge["entry_time"], ge["exit_time"]) != (ne["entry_time"], ne["exit_time"]):
            raise ValueError("GROSS_NET_EPISODE_LINK_MISMATCH")
        entry = nf[ne["entry_time"]]
        exit_ = nf.get(ne["exit_time"])
        output.append(
            dict(
                episode_id=ne["episode_id"],
                symbol=BTC,
                status=ne["status"],
                censored=ne["status"] != "CLOSED",
                entry_decision_timestamp=entry["source_close"],
                entry_fill_timestamp=entry["timestamp"],
                entry_fill_price_net=entry["fill_price"],
                entry_fill_price_gross=gf[ne["entry_time"]]["fill_price"],
                exit_decision_timestamp=exit_["source_close"] if exit_ else None,
                exit_fill_timestamp=ne["exit_time"],
                exit_fill_price_net=exit_["fill_price"] if exit_ else None,
                exit_fill_price_gross=gf[ge["exit_time"]]["fill_price"]
                if exit_
                else None,
                holding_days=ne["holding_days"],
                gross_pnl=ge["realized_pnl"],
                net_pnl=ne["realized_pnl"],
                fees=ne["fees_executed"],
                slippage=ne["slippage_executed"],
                gross_episode_return_pct=ge["return_pct"] if exit_ else None,
                net_episode_return_pct=ne["return_pct"] if exit_ else None,
                terminal_valuation_gross_pnl=ge["valuation_pnl"] if not exit_ else None,
                terminal_valuation_net_pnl=ne["valuation_pnl"] if not exit_ else None,
                terminal_fee_estimate=net["metrics"]["terminal_fee_estimate"]
                if not exit_
                else None,
                terminal_slippage_estimate=net["metrics"]["terminal_slippage_estimate"]
                if not exit_
                else None,
            )
        )
    return output


def ensure_unconsumed(out):
    if (out / "cohort-consumption.json").exists():
        raise ValueError("2023_CLEAN_COHORT_ALREADY_CONSUMED; REFUSE_SECOND_RUN")


def prepare(root, capture=False):
    out = root / "artifacts" / NAME
    out.mkdir(parents=True, exist_ok=True)
    ensure_unconsumed(out)
    require_clean()
    path = out / "candidate-provenance.json"
    proposed = dict(
        base_git_sha=BASE,
        validator_implementation_git_sha=git_head(),
        adapter_sha256=source.sha(Path(__file__).read_bytes()),
        frozen_code_pins=code_pins(),
        strategy="S2",
        portfolio="BTC_ONLY",
        asset=BTC,
        period=dict(start="2023-01-01T00:00:00Z", end_exclusive="2024-01-01T00:00:00Z"),
        starting_equity=1000,
        fee_per_side="0.001",
        slippage_per_side="0.0005",
        screen=SCREEN,
        source_plan=[archive_url(m) for m in MONTHS],
        **exploration_provenance(root),
    )
    if path.exists():
        stored = json.loads(path.read_text())
        comparison = {k: v for k, v in stored.items() if k != "frozen_at_UTC"}
        if comparison != proposed:
            raise ValueError("PREREGISTRATION_CHANGED")
    else:
        exclusive_json(path, dict(**proposed, frozen_at_UTC=utc_now()))
    _, manifest = load_btc(root, capture)
    datafreeze = dict(
        archive_manifest=manifest,
        archive_manifest_sha256=source.sha(source.encoded(manifest)),
        source_contract="Original official Spot daily parser and checksum schema; BTC only; 2022-12 warmup, 2023 cohort",
        market_data_rows=396,
        performance_days=365,
        result_not_yet_computed=True,
    )
    frozen_path = out / "frozen-data-provenance.json"
    if frozen_path.exists():
        # Retrieval-vs-reuse is a cache transport fact, not a data revision.
        old = json.loads(frozen_path.read_text())
        if [
            (r["url"], r["sha256"], r["checksum_sha256"])
            for r in old["archive_manifest"]
        ] != [(r["url"], r["sha256"], r["checksum_sha256"]) for r in manifest]:
            raise ValueError("FROZEN_ARCHIVES_CHANGED")
    else:
        exclusive_json(frozen_path, datafreeze)
    print(
        "PREPARED_ONLY; NO_PERFORMANCE_COMPUTED",
        source.sha(path.read_bytes()),
        source.sha(frozen_path.read_bytes()),
        flush=True,
    )


def claim_once(out, provenance_path, data_path):
    ensure_unconsumed(out)
    return exclusive_json(
        out / "cohort-consumption.json",
        dict(
            **{"2023_clean_cohort_consumed": "YES"},
            reason="CONFIRMATORY_EXECUTION_STARTED; remains consumed for PASS, FAIL or technical interruption",
            started_at_UTC=utc_now(),
            candidate_provenance_sha256=source.sha(provenance_path.read_bytes()),
            frozen_data_provenance_sha256=source.sha(data_path.read_bytes()),
        ),
    )


def package(out):
    files = {
        p.name: dict(sha256=source.sha(p.read_bytes()), bytes=p.stat().st_size)
        for p in sorted(out.iterdir())
        if p.is_file() and p.name not in ("manifest.json", NAME + ".zip")
    }
    (out / "manifest.json").write_bytes(source.encoded(dict(base=BASE, files=files)))
    path = out / (NAME + ".zip")
    with zipfile.ZipFile(path, "w") as z:
        for name in sorted([*files, "manifest.json"]):
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, (out / name).read_bytes())
    return path


def validate(root):
    out = root / "artifacts" / NAME
    ensure_unconsumed(out)
    require_clean()
    pp = out / "candidate-provenance.json"
    dp = out / "frozen-data-provenance.json"
    pin = json.loads(pp.read_text())
    datafreeze = json.loads(dp.read_text())
    if (
        pin["adapter_sha256"] != source.sha(Path(__file__).read_bytes())
        or pin["frozen_code_pins"] != code_pins()
        or pin["screen"] != SCREEN
        or pin["validator_implementation_git_sha"] != git_head()
    ):
        raise ValueError("PREREGISTERED_CODE_OR_SCREEN_CHANGED")
    if pin["exploration_sha256"] != exploration_provenance(root)["exploration_sha256"]:
        raise ValueError("EXPLORATION_CHANGED")
    data, manifest = load_btc(root, capture=False)
    if [(r["url"], r["sha256"], r["checksum_sha256"]) for r in manifest] != [
        (r["url"], r["sha256"], r["checksum_sha256"])
        for r in datafreeze["archive_manifest"]
    ]:
        raise ValueError("FROZEN_DATA_CHANGED")
    claim_once(out, pp, dp)
    signals, gross, net = primary(data)
    independent = verify_independent(signals, gross, net, data)
    result = confirmatory_screen(gross["metrics"], net["metrics"], net["leave_month"])
    lm = net["leave_month"]
    worst = min(lm, key=lambda r: r["net_return_pct"])
    best = max(lm, key=lambda r: r["net_return_pct"])
    summary = dict(
        **result,
        year=2023,
        strategy="S2",
        asset=BTC,
        portfolio="BTC_ONLY",
        dataset_role="CLEAN_CONFIRMATORY_VALIDATION",
        gross=gross["metrics"],
        net=net["metrics"],
        gross_net_degradation=gross["metrics"]["pnl"] - net["metrics"]["pnl"],
        positive_leave_month_runs=sum(r["net_return_pct"] > 0 for r in lm),
        total_leave_month_runs=len(lm),
        worst_leave_month=worst,
        best_leave_month=best,
        monthly_results=net["monthly"],
        independent_validation=independent,
        candidate_provenance_sha256=source.sha(pp.read_bytes()),
        frozen_data_provenance_sha256=source.sha(dp.read_bytes()),
        exploration_references=pin["references"],
        research_2022_performance_used=False,
        parameters_2024_2025_changed=False,
        data_2026_used=False,
        parameter_optimization_performed=False,
        new_institutional_replay=False,
        production_behavior_changed=False,
        production_db_writes=0,
        deployment=False,
    )
    (out / "summary.json").write_bytes(source.encoded(summary))
    (out / "validation-result.json").write_bytes(source.encoded(result))
    methodology = dict(
        frozen_code_pins=pin["frozen_code_pins"],
        adapter="Original observation and _simulate code objects; isolated namespace restricts quote iteration to BTC. Original modules/files are never mutated. Original allocation code objects with BTC-only iteration.",
        scope="Only S2 BTC; cohorts start flat; 2022-12 indicator warmup only; first decision uses completed 2022-12-31 close; 2023 fills only; no 2024 terminal open or 2026 data",
        screen=SCREEN,
        leave_month="Delete each calendar month from unchanged NET daily returns; no refit",
        terminal="Final completed 2023 close minus estimated sell fee/slippage, not a fill",
        oracle="Independent float statistics.mean/stdev and direct single-asset cash formulas; never calls frozen strategy or allocator",
        one_shot="Exclusive consumed marker fsynced BEFORE any performance computation; repeat execution refused even after interruption",
        final_oos="Eligibility only; no automatic 2026 run",
    )
    (out / "methodology.json").write_bytes(source.encoded(methodology))
    source.write_csv(
        out / "strategy-result.csv",
        [
            dict(cost_mode=mode, **run["metrics"])
            for mode, run in [("GROSS", gross), ("NET", net)]
        ],
    )
    source.write_csv(out / "episode-results.csv", episode_audit(gross, net))
    source.write_csv(
        out / "monthly-results.csv",
        [
            dict(cost_mode=mode, **r)
            for mode, run in [("GROSS", gross), ("NET", net)]
            for r in run["monthly"]
        ],
    )
    source.write_csv(out / "leave-month-sensitivity.csv", lm)
    source.write_csv(
        out / "cost-decomposition.csv",
        [
            dict(
                gross_pnl=gross["metrics"]["pnl"],
                net_pnl=net["metrics"]["pnl"],
                gross_net_degradation=summary["gross_net_degradation"],
                **{
                    k: net["metrics"][k]
                    for k in (
                        "actual_modeled_fee",
                        "actual_modeled_slippage",
                        "terminal_fee_estimate",
                        "terminal_slippage_estimate",
                        "fee",
                        "modeled_slippage",
                        "turnover",
                    )
                },
            )
        ],
    )
    source.write_csv(
        out / "execution-audit.csv",
        [
            dict(cost_mode=mode, **r)
            for mode, run in [("GROSS", gross), ("NET", net)]
            for r in run["events"]
        ],
    )
    source.write_csv(
        out / "daily-equity.csv",
        [
            dict(cost_mode=mode, **r)
            for mode, run in [("GROSS", gross), ("NET", net)]
            for r in run["daily"]
        ],
    )
    source.write_csv(
        out / "signal-audit.csv",
        [dict(execution_open=source.iso(t), **r[BTC]) for t, r in signals.items()],
    )
    if (
        code_pins() != pin["frozen_code_pins"]
        or source.sha(pp.read_bytes()) != summary["candidate_provenance_sha256"]
    ):
        raise ValueError("FROZEN_PROVENANCE_MUTATED")
    validation = dict(
        monthly_archives_verified=13,
        btc_market_rows=396,
        warmup_rows=31,
        cohort_rows=365,
        original_exploration_sha256=EXPLORATION_SHA,
        code_pins_unchanged=True,
        preregistration_unchanged=True,
        independent_validation=independent,
        no_db_access=True,
        production_db_writes=0,
        production_behavior_changed=False,
        new_institutional_replay=False,
        source_symbols=[BTC],
        data_2026_used=False,
        deployment=False,
    )
    (out / "source-validation.json").write_bytes(source.encoded(validation))
    (out / "report.md").write_text(report(summary))
    path = package(out)
    print(
        "ONE_SHOT_COMPLETED",
        result["clean_validation_result"],
        result["candidate_status"],
        "final_oos_eligible=" + result["final_oos_eligible"],
        path,
        source.sha(path.read_bytes()),
        flush=True,
    )


def report(s):
    g, n = s["gross"], s["net"]

    def fmt(x):
        return "UNDEFINED" if x is None else f"{x:.6f}"

    lines = [
        "# BTC MEAN REVERSION 2023 CLEAN VALIDATION v1",
        "",
        s["clean_validation_result"],
        "candidate_status: " + s["candidate_status"],
        "final_oos_eligible: " + s["final_oos_eligible"],
        "2023_clean_cohort_consumed: YES",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for k, v in [
        ("Starting equity", 1000),
        ("Gross ending equity", g["ending_equity"]),
        ("Net ending equity", n["ending_equity"]),
        ("Gross return %", g["return_pct"]),
        ("Net return %", n["return_pct"]),
        ("Gross PnL", g["pnl"]),
        ("Net PnL", n["pnl"]),
        ("Max drawdown % (NET)", n["max_drawdown_pct"]),
        ("Executed turnover", n["turnover"]),
        ("Executed fees", n["actual_modeled_fee"]),
        ("Executed slippage", n["actual_modeled_slippage"]),
        ("Terminal fee estimate", n["terminal_fee_estimate"]),
        ("Terminal slippage estimate", n["terminal_slippage_estimate"]),
        ("Gross minus net PnL", s["gross_net_degradation"]),
        ("Entries", n["entries"]),
        ("Exits", n["exits"]),
        ("Completed NET episodes", n["completed_position_episodes"]),
        ("Open NET episodes", n["open_position_episodes"]),
        ("Win rate %", n["win_rate"]),
        ("Mean completed episode return %", n["mean_episode_return_pct"]),
        ("Median completed episode return %", n["median_episode_return_pct"]),
        ("Mean holding days", n["average_holding_days"]),
        ("Median holding days", n["median_holding_days"]),
        ("Time in market %", n["time_in_market_pct"]),
    ]:
        lines.append(f"| {k} | {fmt(v)} |")
    lines += ["", "## Frozen primary screen", ""]
    for k, v in s["criteria"].items():
        lines.append(k + ": " + ("YES" if v else "NO"))
    lines += ["", "## NET monthly results", "", "| Month | Return % |", "|---|---:|"]
    for r in s["monthly_results"]:
        lines.append(f"| {r['month']} | {r['return_pct']:.6f} |")
    lines += [
        "",
        f"Positive months: {n['positive_months']}/{n['total_months']}; best {json.dumps(n['best_month'])}; worst {json.dumps(n['worst_month'])}.",
        f"Positive leave-month runs: {s['positive_leave_month_runs']}/{s['total_leave_month_runs']}; minimum {json.dumps(s['worst_leave_month'])}; maximum {json.dumps(s['best_leave_month'])}.",
        "",
        "Frozen exploration references only: 2024 gross +28.6381%, net +26.7229%, completed 5, leave-month 12/12; 2025 gross +19.2969%, net +16.1190%, completed 9, leave-month 11/11. These were NOT rerun.",
        "Primary S2 strategy/accounting code objects are unchanged; only the isolated quote iteration namespace is BTC-only. The independent oracle uses its own float indicators and cash formulas. Both gross/net paths, fills, costs, episodes and leave-month results agree.",
        "No 2022 performance or inherited warmup position, no 2024 terminal fill, no 2026 data. Terminal costs are valuation estimates; open positions do not count as completed. Win rate and holding/return statistics use completed episodes only.",
        "The consumed marker was written before outcome computation. The cohort is permanently consumed regardless of PASS/FAIL. No threshold/filter/exit/asset change, retry, new institutional replay, production modification, DB write, service restart or deployment. No automatic 2026 final OOS.",
    ]
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["prepare", "validate"])
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--capture", action="store_true")
    args = p.parse_args()
    if args.action == "prepare":
        prepare(args.root.resolve(), args.capture)
    else:
        if args.capture:
            raise ValueError("VALIDATION_IS_OFFLINE_ONLY")
        validate(args.root.resolve())


if __name__ == "__main__":
    main()
