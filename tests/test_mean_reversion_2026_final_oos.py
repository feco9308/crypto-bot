"""Synthetic-only final OOS protocol tests. No real 2026 market access."""

import copy
import csv
import io
import json
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from research import mean_reversion_2026_final_oos as o

FAKE_HEAD = "a" * 40
FUTURE = datetime(2027, 1, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def forbid_network_and_real_cache(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("NO_REAL_NETWORK_IN_SYNTHETIC_TESTS")

    monkeypatch.setattr(o.requests.sessions.Session, "request", no_network)
    original = Path.open

    def safe_open(path, *args, **kwargs):
        if str(path).startswith("/home/ndvi/crypto-bot/data/"):
            raise AssertionError("NO_REAL_MARKET_CACHE_IN_SYNTHETIC_TESTS")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", safe_open)


def history(days=365):
    first = o.source.stamp("2025-12-01")
    return {
        t: o.source.Bar(t, t + o.DAY - 1, D(100), D(100))
        for t in range(first, o.START + days * o.DAY, o.DAY)
    }


def fixture(days=365):
    return o.SyntheticFixture(history(days), o.START, o.START + days * o.DAY)


def fake_candidate():
    return dict(
        asset=o.BTC,
        required_prior_result="CLEAN_VALIDATION_PASS",
        candidate_status="CONFIRMED_ON_2023_CLEAN_COHORT",
        final_oos_eligible="YES",
        **{"2023_clean_cohort_consumed": "YES"},
        frozen_strategy_accounting_code_pins=o.clean.code_pins(),
    )


def prereg(tmp_path, monkeypatch):
    monkeypatch.setattr(o.clean, "require_clean", lambda: None)
    monkeypatch.setattr(o.clean, "git_head", lambda: FAKE_HEAD)
    monkeypatch.setattr(o, "candidate_provenance", lambda root: fake_candidate())
    monkeypatch.setattr(
        o.subprocess,
        "check_output",
        lambda *args, **kwargs: Path(o.__file__).read_bytes(),
    )
    o.preregister(tmp_path)
    path = tmp_path / "artifacts" / o.NAME / "preregistration.json"
    return o.source.sha(path.read_bytes())


def source_records():
    records = []
    for month in o.MONTHS:
        year, m = map(int, month.split("-"))
        next_month = f"{year + 1}-01-01" if m == 12 else f"{year}-{m + 1:02d}-01"
        count = (o.source.stamp(next_month) - o.source.stamp(month + "-01")) // o.DAY
        records.append(
            dict(
                symbol=o.BTC,
                month=month,
                rows=count,
                status="CHECKSUM_VALIDATED",
                sha256="a" * 64,
                checksum_sha256="b" * 64,
                url=o.archive_url(month),
            )
        )
    return records


def synthetic_zip(month):
    year, m = map(int, month.split("-"))
    end = f"{year + 1}-01-01" if m == 12 else f"{year}-{m + 1:02d}-01"
    stream = io.StringIO()
    writer = csv.writer(stream)
    for t in range(o.source.stamp(month + "-01"), o.source.stamp(end), o.DAY):
        writer.writerow(
            [t * 1000, 100, 101, 99, 100, 1, (t + o.DAY) * 1000 - 1, 100, 1, 0.5, 50, 0]
        )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("SYNTHETIC_ONLY.csv", stream.getvalue())
    return out.getvalue()


def cache_synthetic_archives(root):
    folder = root / "data/research-cache" / o.EXEC_NAME
    folder.mkdir(parents=True)
    for m in o.MONTHS:
        body = synthetic_zip(m)
        digest = o.source.sha(body)
        check = (digest + "  SYNTHETIC_ONLY.zip\n").encode()
        for suffix, payload in [("", body), (".CHECKSUM", check)]:
            url = o.archive_url(m) + suffix
            record = dict(
                url=url,
                status=200,
                sha256=o.source.sha(payload),
                body_hex=payload.hex(),
            )
            (folder / (o.source.sha(url.encode()) + ".json")).write_bytes(
                o.source.encoded(record)
            )


def test_exact_cohort_and_warmup_source_plan():
    assert o.START == o.source.stamp("2026-01-01") and o.END == o.source.stamp(
        "2027-01-01"
    )
    assert o.MONTHS == ("2025-12",) + tuple(f"2026-{m:02d}" for m in range(1, 13))
    assert o.BTC == "BTCUSDT"
    data = o.payloads(Path("/synthetic"), FAKE_HEAD, o.code_pins(), fake_candidate())
    planned = data["source-plan.json"]["archives"]
    assert planned[0]["role"] == "INDICATOR_WARMUP_ONLY"
    assert all(x["role"] == "FINAL_OOS_COHORT" for x in planned[1:])
    assert all("BTCUSDT/1d" in r["url"] for r in planned)


@pytest.mark.parametrize("month", ["2025-11", "2027-01", "2026-13", "2023-01"])
def test_other_period_and_2027_market_forbidden(month):
    with pytest.raises(ValueError, match="NO_2027"):
        o.archive_url(month)


@pytest.mark.parametrize("date", [datetime(2026, 11, 4, tzinfo=timezone.utc), FUTURE])
def test_prereg_mode_blocks_before_any_real_data_io_even_in_future(
    tmp_path, monkeypatch, date
):
    monkeypatch.setattr(o, "now_utc", lambda: date)

    def forbidden(*args, **kwargs):
        raise AssertionError("DATA_IO_WAS_ATTEMPTED")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "exists", forbidden)
    with pytest.raises(ValueError, match=o.FORBIDDEN):
        o.archive_response(
            tmp_path, o.archive_url("2026-01"), o.Mode.PREREGISTRATION, None, True
        )
    with pytest.raises(ValueError, match=o.FORBIDDEN):
        o.load_real_data(tmp_path, o.Mode.PREREGISTRATION, None, True)
    with pytest.raises(ValueError, match=o.FORBIDDEN):
        o.authorize_sources(tmp_path, o.Mode.PREREGISTRATION, "x")


def test_current_date_gate_precedes_archive_or_prereg_file_read(tmp_path, monkeypatch):
    monkeypatch.setattr(
        o, "now_utc", lambda: datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("FILES_OPENED_BEFORE_YEAR_END")

    monkeypatch.setattr(o, "verify_preregistration", forbidden)
    with pytest.raises(ValueError, match="YEAR_NOT_COMPLETED"):
        o.authorize_sources(tmp_path, o.Mode.FINAL_EXECUTION, "x")


def test_exact_utc_year_end_time_gate(monkeypatch):
    monkeypatch.setattr(o, "now_utc", lambda: datetime(2027, 1, 1, tzinfo=timezone.utc))
    o.require_data_mode(o.Mode.FINAL_EXECUTION)
    with pytest.raises(ValueError, match=o.FORBIDDEN):
        o.require_data_mode(o.Mode.PREREGISTRATION)


def test_prereg_artifacts_deterministic_and_no_results(tmp_path, monkeypatch):
    sha = prereg(tmp_path, monkeypatch)
    folder = tmp_path / "artifacts" / o.NAME
    first = {p.name: p.read_bytes() for p in folder.iterdir()}
    o.preregister(tmp_path)
    assert first == {p.name: p.read_bytes() for p in folder.iterdir()}
    assert set(first) == set(o.PREREG_FILES) | {"manifest.json"}
    assert o.source.sha((folder / "preregistration.json").read_bytes()) == sha
    assert not o.consumption_path(tmp_path).exists()
    assert not (folder / "strategy-result.csv").exists()
    assert not (folder / "episode-results.csv").exists()


def test_external_anchor_and_exact_head_are_required(tmp_path, monkeypatch):
    sha = prereg(tmp_path, monkeypatch)
    assert o.verify_preregistration(tmp_path, sha)["implementation_HEAD"] == FAKE_HEAD
    with pytest.raises(ValueError, match="ANCHOR"):
        o.verify_preregistration(tmp_path, "0" * 64)
    monkeypatch.setattr(o.clean, "git_head", lambda: "b" * 40)
    with pytest.raises(ValueError, match="EXACT_FROZEN_HEAD"):
        o.verify_preregistration(tmp_path, sha)


def test_code_pin_and_historic_strategy_pin_mismatch_blocks(tmp_path, monkeypatch):
    sha = prereg(tmp_path, monkeypatch)
    pins = o.code_pins()
    pins["S2_observation"] = "0" * 64
    monkeypatch.setattr(o, "code_pins", lambda: pins)
    with pytest.raises(ValueError, match="CODE_HASH"):
        o.verify_preregistration(tmp_path, sha)
    with pytest.raises(ValueError, match="HISTORICALLY_FROZEN"):
        o.payloads(tmp_path, FAKE_HEAD, pins, fake_candidate())


def test_missing_archive_or_unverified_checksum_blocks_execution(tmp_path, monkeypatch):
    sha = prereg(tmp_path, monkeypatch)
    monkeypatch.setattr(o, "now_utc", lambda: FUTURE)
    permit = o.authorize_sources(tmp_path, o.Mode.FINAL_EXECUTION, sha)
    for records in (
        source_records()[:-1],
        [
            dict(r, status="MISSING") if i == 0 else r
            for i, r in enumerate(source_records())
        ],
    ):
        bundle = o.VerifiedData({}, records, permit, o.SEAL)
        with pytest.raises(ValueError, match="NO_EXECUTION"):
            o.authorize_performance(bundle, o.Mode.FINAL_EXECUTION)
        assert not o.consumption_path(tmp_path).exists()


def test_official_checksum_mismatch_and_synthetic_microsecond_parser():
    body = synthetic_zip("2026-01")
    check = (o.source.sha(body) + "  SYNTHETIC_ONLY.zip").encode()
    bars, digest = o.decode_archive("2026-01", body, check)
    assert len(bars) == 31 and digest == o.source.sha(body)
    assert bars[0].start == o.START and bars[0].end == o.START + o.DAY - 1
    with pytest.raises(ValueError, match="CHECKSUM_MISMATCH"):
        o.decode_archive("2026-01", body, b"0" * 64)


def test_date_passed_but_archive_incomplete_still_no_consumption(tmp_path, monkeypatch):
    sha = prereg(tmp_path, monkeypatch)
    monkeypatch.setattr(o, "now_utc", lambda: FUTURE)
    with pytest.raises(ValueError, match="INCOMPLETE"):
        o.execute(tmp_path, o.Mode.FINAL_EXECUTION, sha, capture=False)
    assert not o.consumption_path(tmp_path).exists()


def test_consumed_marker_blocks_before_new_source_access(tmp_path, monkeypatch):
    sha = prereg(tmp_path, monkeypatch)
    monkeypatch.setattr(o, "now_utc", lambda: FUTURE)
    path = o.consumption_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(o.source.encoded({"2026_final_oos_consumed": "YES"}))
    with pytest.raises(ValueError, match="ALREADY_CONSUMED"):
        o.authorize_sources(tmp_path, o.Mode.FINAL_EXECUTION, sha)


def equality_fixture():
    f = fixture(1)
    day = o.START - o.DAY
    prior = [4, 4, -4, 2, -2, 1, -1, 1, -1] + [0] * 10
    for k, dev in enumerate(prior, 1):
        t = day - k * o.DAY
        f.history[t] = replace(f.history[t], close=D(100 + dev))
    f.history[day] = replace(f.history[day], close=D(96))
    return f


def test_exact_sma20_sample_std_lower_and_strict_entry_equality():
    f = equality_fixture()
    r = o.run_synthetic(f)
    sig = r["signals"][o.START][o.BTC]
    assert (
        D(sig["sma20"]) == 100
        and D(sig["sample_std20"]) == 2
        and D(sig["lower_band"]) == 96
    )
    assert sig["active_after"] is False
    day = o.START - o.DAY
    f.history[day] = replace(f.history[day], close=D("95.999"))
    assert o.run_synthetic(f)["signals"][o.START][o.BTC]["action"] == "BUY"


def test_exit_at_mean_equality_and_completed_candle_only():
    f = fixture(2)
    day = o.START - o.DAY
    assert o.frozen.observation("S2", f.history, day, True, o.START)["action"] == "SELL"
    f.history[day] = replace(f.history[day], end=o.START)
    with pytest.raises(ValueError, match="UNCOMPLETED"):
        o.run_synthetic(f)


def test_prereg_or_raw_data_cannot_become_a_compute_authorization():
    f = fixture(1)
    with pytest.raises(ValueError, match=o.FORBIDDEN):
        o.calculate(f.history, f.start, f.end, o.Mode.PREREGISTRATION)
    with pytest.raises(ValueError, match=o.FORBIDDEN):
        o.run_synthetic(f.history)


def test_next_open_timestamps_and_completed_episode_count():
    f = fixture(4)
    f.history[o.START] = replace(f.history[o.START], close=D(80))
    r = o.run_synthetic(f)
    fills = [x for x in r["net"]["events"] if x["event"] == "EXECUTED_NEXT_DAILY_OPEN"]
    assert [(x["timestamp"], x["side"]) for x in fills] == [
        (o.source.iso(o.START + o.DAY), "BUY"),
        (o.source.iso(o.START + 2 * o.DAY), "SELL"),
    ]
    assert all(
        o.source.stamp(x["source_close"]) < o.source.stamp(x["timestamp"])
        for x in fills
    )
    assert r["net"]["metrics"]["completed_position_episodes"] == 1


def test_same_bar_authorization_rejected_without_orders():
    f = fixture(1)
    f.history[o.START - o.DAY] = replace(f.history[o.START - o.DAY], end=o.START)
    with pytest.raises(ValueError):
        o.run_synthetic(f)


def test_terminal_estimate_not_fill_and_fee_slippage_exact():
    f = fixture(1)
    day = o.START - o.DAY
    f.history[day] = replace(f.history[day], close=D(80))
    r = o.run_synthetic(f)["net"]
    fills = [x for x in r["events"] if x["event"] == "EXECUTED_NEXT_DAILY_OPEN"]
    assert (
        len(fills) == 1
        and fills[0]["side"] == "BUY"
        and D(fills[0]["fill_price"]) == D("100.05")
    )
    assert D(fills[0]["cash_after_allocation"]) >= 0
    assert (
        r["metrics"]["completed_position_episodes"] == 0
        and r["metrics"]["open_position_episodes"] == 1
    )
    assert r["events"][-1]["event"] == "TERMINAL_COST_ESTIMATE_NOT_A_FILL"
    assert r["metrics"]["ending_equity"] == pytest.approx(
        1000 * 0.9995 * 0.999 / (1.0005 * 1.001)
    )


def test_final_signal_without_next_in_cohort_open_and_2027_price_ignored():
    f = fixture()
    day = o.END - 2 * o.DAY
    f.history[day] = replace(f.history[day], close=D(80))
    first = o.run_synthetic(f)
    f.history[o.END] = o.source.Bar(o.END, o.END + o.DAY - 1, D("1e12"), D("1e12"))
    second = o.run_synthetic(f)
    assert first == second
    assert first["net"]["metrics"]["open_position_episodes"] == 1
    assert all(o.source.stamp(x["timestamp"]) < o.END for x in first["net"]["events"])


def test_warmup_performance_excluded_no_preexisting_position():
    f = fixture(5)
    day = o.START - 2 * o.DAY
    f.history[day] = replace(f.history[day], close=D(70))
    r = o.run_synthetic(f)
    assert r["signals"][o.START][o.BTC]["active_before"] is False
    assert all(o.source.stamp(x["day"]) >= o.START for x in r["net"]["daily"])
    assert all(o.source.stamp(x["timestamp"]) >= o.START for x in r["net"]["events"])


def test_independent_oracle_cannot_call_primary_strategy(monkeypatch):
    f = fixture()
    for i in range(365):
        t = o.START + i * o.DAY
        if i % 40 in (5, 6):
            f.history[t] = replace(f.history[t], close=D(80 if i % 40 == 5 else 79))
    before = copy.deepcopy(f.history)
    r = o.run_synthetic(f)
    assert r["independent_validation"]["status"] == "PASS" and before == f.history

    def forbidden(*args, **kwargs):
        raise AssertionError("PRIMARY_AS_ORACLE")

    monkeypatch.setattr(o.frozen, "observation", forbidden)
    monkeypatch.setattr(o.clean, "bind_btc_accounting", forbidden)
    observations, paths = o.independent_oracle(f.history, f.start, f.end, f)
    assert len(observations) == 365 and len(paths["NET"]["daily"]) == 365


def twelve(value=1):
    return [
        dict(omitted_month=f"2026-{m:02d}", net_return_pct=value) for m in range(1, 13)
    ]


def test_four_condition_screen_only_pass_fail_no_threshold_relaxation():
    g = dict(return_pct=1)
    n = dict(return_pct=1, completed_position_episodes=5)
    good = o.final_screen(g, n, twelve())
    assert (
        good["final_oos_result"] == "FINAL_OOS_PASS"
        and good["candidate_status"] == "FINAL_OOS_CONFIRMED"
    )
    assert good["paper_implementation_eligible"] == "YES"
    cases = [
        (dict(return_pct=0), n, twelve()),
        (g, dict(n, return_pct=0), twelve()),
        (
            g,
            dict(n, completed_position_episodes=4, open_position_episodes=10),
            twelve(),
        ),
        (g, n, twelve(0)),
        (g, n, twelve()[:-1]),
    ]
    for gross, net, lm in cases:
        r = o.final_screen(gross, net, lm)
        assert (
            r["final_oos_result"] == "FINAL_OOS_FAIL"
            and r["candidate_status"] == "FINAL_OOS_FAILED"
        )
        assert r["paper_implementation_eligible"] == "NO"


def test_leave_month_deterministic_and_unchanged():
    r = [
        dict(month="2026-01", **{"return": 0.1}),
        dict(month="2026-02", **{"return": -0.05}),
    ]
    before = copy.deepcopy(r)
    a = o.source.leave_month(r)
    assert (
        a == o.source.leave_month(r)
        and r == before
        and a[0]["ending_equity"] == pytest.approx(950)
    )


def test_exact_code_objects_and_frozen_pins():
    assert o.clean.bind_btc_accounting().__code__ is o.frozen._simulate.__code__
    pins = o.code_pins()
    assert all(pins[k] == v for k, v in o.clean.code_pins().items())
    assert pins["S2_observation"] == o.source.sha(
        o.inspect.getsource(o.frozen.observation).encode()
    )


def test_future_execution_synthetic_only_one_shot_and_audit(tmp_path, monkeypatch):
    sha = prereg(tmp_path, monkeypatch)
    cache_synthetic_archives(tmp_path)
    monkeypatch.setattr(o, "now_utc", lambda: FUTURE)
    o.execute(tmp_path, o.Mode.FINAL_EXECUTION, sha, capture=False)
    marker = json.loads(o.consumption_path(tmp_path).read_text())
    assert marker["2026_final_oos_consumed"] == "YES"
    with pytest.raises(ValueError, match="ALREADY_CONSUMED"):
        o.execute(tmp_path, o.Mode.FINAL_EXECUTION, sha)
    o.audit_existing(tmp_path, o.Mode.FINAL_EXECUTION, sha)


def test_technical_interruption_marker_persists_no_automatic_recovery(
    tmp_path, monkeypatch
):
    sha = prereg(tmp_path, monkeypatch)
    cache_synthetic_archives(tmp_path)
    monkeypatch.setattr(o, "now_utc", lambda: FUTURE)

    def interrupted(*args, **kwargs):
        raise RuntimeError("SYNTHETIC_TECHNICAL_INTERRUPTION")

    monkeypatch.setattr(o, "calculate", interrupted)
    with pytest.raises(RuntimeError):
        o.execute(tmp_path, o.Mode.FINAL_EXECUTION, sha)
    assert (
        json.loads(o.consumption_path(tmp_path).read_text())["2026_final_oos_consumed"]
        == "YES"
    )
    with pytest.raises(ValueError, match="NO_AUTOMATIC_RETRY"):
        o.audit_existing(tmp_path, o.Mode.FINAL_EXECUTION, sha)
    with pytest.raises(ValueError, match="ALREADY_CONSUMED"):
        o.authorize_sources(tmp_path, o.Mode.FINAL_EXECUTION, sha)


def test_no_production_strategy_database_or_2026_preview_import():
    text = Path(o.__file__).read_text()
    for banned in [
        "from crypto_bot",
        "import crypto_bot",
        "import sqlite",
        "import sqlalchemy",
    ]:
        assert banned not in text
    assert "rglob(" not in text
