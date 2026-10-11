"""Research initial-distance changes stay outside production registries/services."""

from copy import deepcopy

import pytest

from crypto_bot.replay.clock import ms
from crypto_bot.replay.registry import EXITS
from crypto_bot.replay.shadow_adaptive import AdaptivePeakLock
from crypto_bot.trading.config import PaperSettings
from crypto_bot.trading.domain import D, q
from research.shadow_dynamic_initial_r import (
    DEFINITIONS,
    LABELS,
    derive,
    loss_per_unit,
    pairwise,
    run_entry,
    sizing_audit,
)


def fixture():
    settings = PaperSettings()
    fill = {"reference_price": "100"}
    quantity = D(5)
    risk = q(quantity * loss_per_unit("100", "98", settings))
    entry = {
        "shadow_entry_id": "anchor:0:1",
        "symbol": "SOLUSDT",
        "entry_time": "2024-01-01T00:00:00Z",
        "entry_fill_price": "100.05",
        "original_stop_price": "98",
        "quantity": "5",
        "notional": "500.25",
        "entry_fee": "0.50025",
        "entry_slippage": "0.25",
        "risk_amount": str(risk),
        "atr": 1,
    }
    record = {
        "features": {
            "atr14_abs": 1,
            "atr72_abs": 0.8,
            "atr120_abs": 0.7,
            "pullback_p75_72h_pct": 3,
        },
        "anchor_trade": {"entry_reference_price": "100"},
    }
    return entry, record, fill, settings


def snapshot():
    return {
        "equity": "1000",
        "cash_balance": "1000",
        "reserved_cash": "0",
        "positions": [],
        "realized_pnl": "0",
        "fees_paid": "0",
        "daily_pnl": "0",
        "day_start_equity": "1000",
        "peak_equity": "1000",
        "max_drawdown": "0",
        "stale_exposure_reservation": "0",
    }


def test_control_stop_and_risk_exact_parity_reference_not_fill():
    e, r, f, s = fixture()
    d = derive(e, r, f, s)
    assert D(d["CONTROL"]["stop_price"]) == D(98)
    assert D(d["CONTROL"]["risk_amount"]) == D(e["risk_amount"])
    assert D(d["A"]["stop_price"]) == D("98.4")
    assert D(d["B"]["stop_price"]) == D("98.6")
    assert D(d["C"]["stop_price"]) == D(98)
    assert D(d["D"]["stop_price"]) == D(97)


def test_canonical_inputs_immutable_and_initial_r_changes_only():
    e, r, f, s = fixture()
    old = deepcopy(e)
    d = derive(e, r, f, s)
    for candidate in d.values():
        for key in (
            "entry_time",
            "entry_fill_price",
            "quantity",
            "notional",
            "entry_fee",
            "entry_slippage",
            "atr",
        ):
            assert candidate["entry"][key] == e[key]
    assert e == old
    assert D(d["A"]["unit_r"]) < D(d["CONTROL"]["unit_r"]) < D(d["D"]["unit_r"])


@pytest.mark.parametrize(
    "field,value", [("original_stop_price", "97"), ("risk_amount", "20")]
)
def test_control_mismatch_stops_candidates(field, value):
    e, r, f, s = fixture()
    e[field] = value
    with pytest.raises(ValueError, match="CONTROL"):
        derive(e, r, f, s)


def test_invalid_candidate_stop_not_silently_dropped():
    e, r, f, s = fixture()
    r["features"]["pullback_p75_72h_pct"] = 101
    with pytest.raises(ValueError, match="Invalid candidate"):
        derive(e, r, f, s)


def test_dynamic_r_thresholds_original_atr_trailing_unchanged():
    e, r, f, s = fixture()
    d = derive(e, r, f, s)
    a = AdaptivePeakLock(d["A"]["entry"], s)
    b = AdaptivePeakLock(d["D"]["entry"], s)
    assert a.atr == b.atr == D(1)
    assert a.thresholds[0] < b.thresholds[0]
    assert a.thresholds[0] == D(e["entry_fill_price"]) + D(d["A"]["unit_r"]) * D(".75")


@pytest.mark.parametrize(
    "constraint",
    ["available_cash", "symbol_exposure", "total_exposure", "minimum_order_value"],
)
def test_fixed_risk_audit_detects_constraints_without_changing_entries(constraint):
    e, r, f, s = fixture()
    d = derive(e, r, f, s)
    snap = snapshot()
    if constraint == "available_cash":
        snap["cash_balance"] = "1"
    if constraint == "symbol_exposure":
        from dataclasses import replace

        s = replace(s, max_symbol_exposure_pct=D(".1"))
    if constraint == "total_exposure":
        snap["stale_exposure_reservation"] = "499"
    if constraint == "minimum_order_value":
        from dataclasses import replace

        s = replace(s, minimum_order_value=D("1000"))
    saved = deepcopy(d)
    result = sizing_audit(e, d, f, snap, s)
    assert result["CONTROL"]["WOULD_HIT_RISK_CAP"]
    assert constraint in result["CONTROL"]["constraints"]
    assert d == saved


def test_fixed_risk_quantity_uses_existing_fee_slippage_sizing():
    e, r, f, s = fixture()
    d = derive(e, r, f, s)
    a = sizing_audit(e, d, f, snapshot(), s)
    expected = q(
        D(e["risk_amount"])
        / loss_per_unit(f["reference_price"], d["A"]["stop_price"], s)
    )
    assert D(a["A"]["requested_quantity"]) == expected
    assert a["A"]["quantity_ratio"] > 1
    assert a["D"]["quantity_ratio"] < 1


class Provider:
    def __init__(self, high=100.1, low=95):
        self.calls = []
        self.high = high
        self.low = low

    def candle(self, symbol, stamp):
        self.calls.append(stamp)
        return dict(
            time=stamp,
            close_time=stamp + 59999,
            open=100.1,
            high=self.high,
            low=self.low,
            close=99,
        )


def test_shared_candle_read_all_candidates_and_policy_order_invariant():
    e, r, f, s = fixture()
    d = derive(e, r, f, s)
    config = {
        "end": "2024-01-01T00:02:00Z",
        "stale_position_policy": "RESEARCH_QUARANTINE_STALE",
    }
    p = Provider()
    a = run_entry(d, LABELS, s, config, p)
    assert len(p.calls) == 1
    b = run_entry(d, list(reversed(LABELS)), s, config, Provider())
    for mode in a:
        assert sorted(a[mode], key=lambda v: v["policy_id"]) == sorted(
            b[mode], key=lambda v: v["policy_id"]
        )
        assert all(row["initial_r_fixed_at_entry"] for row in a[mode])


def test_missing_data_no_fabricated_or_year_end_exit():
    e, r, f, s = fixture()
    d = derive(e, r, f, s)

    class Missing:
        def candle(self, symbol, stamp):
            return None

    output = run_entry(
        d,
        ("D",),
        s,
        {
            "end": "2024-01-01T00:02:00Z",
            "stale_position_policy": "RESEARCH_QUARANTINE_STALE",
        },
        Missing(),
    )
    for mode in output:
        assert output[mode][0]["status"] == "OPEN_STALE"
        assert output[mode][0]["exit_price"] is None


def test_future_bars_not_requested_after_exit():
    e, r, f, s = fixture()
    d = derive(e, r, f, s)
    p = Provider()
    output = run_entry(
        d,
        ("A",),
        s,
        {
            "end": "2025-01-01T00:00:00Z",
            "stale_position_policy": "RESEARCH_QUARANTINE_STALE",
        },
        p,
    )
    assert p.calls == [ms(e["entry_time"])]
    assert output["conservative"][0]["exit_time"] == "2024-01-01T00:01:00Z"


def test_production_registry_unchanged():
    assert not any(
        name in EXITS.items
        for name in ("dynamic_initial_r", "ATR72", "ATR120", "pullback_floor")
    )
    assert set(DEFINITIONS) == set(LABELS)


def test_decomposition_separates_denominator_scaling_from_cash_outcome():
    row = {
        "shadow_entry_id": "x",
        "comparison_eligible": True,
        "status": "CLOSED",
        "position_status": "CLOSED",
        "data_complete": True,
        "realized_r": 1.0,
        "canonical_risk_amount": "10",
        "realized_pnl": "10",
        "return_pct": 1.0,
        "exit_stage": "PEAK_TRAIL_1R",
        "activation_075r_time": "t",
        "activation_1r_time": "t",
        "activation_2r_time": None,
        "normalized_pnl_per_1000": 10.0,
        "fees": "1",
        "slippage": ".5",
        "duration_seconds": 60,
        "intrabar_ambiguity": False,
    }
    candidate = dict(row, canonical_risk_amount="20", realized_r=0.5)
    result = pairwise([row], [candidate])
    assert result["cash_exit_effect_anchor_r"] == 0
    assert result["denominator_scaling_effect_r"] == -0.5
    assert result["delta_total_r"] == -0.5
    assert result["decomposition_residual"] == 0


def test_control_fixed_risk_roundtrip_does_not_count_as_sizing_change():
    e, r, f, s = fixture()
    d = derive(e, r, f, s)
    result = sizing_audit(e, d, f, snapshot(), s)
    assert result["CONTROL"]["quantity_ratio"] == 1
    assert result["CONTROL"]["quantity_ratio_vs_canonical"] <= 1
    assert result["C"]["quantity_ratio"] == 1


def test_private_archive_completion_get_only_no_shared_cache_writes(tmp_path):
    import hashlib
    import io
    import sqlite3
    import zipfile

    from research.shadow_dynamic_initial_r import ResearchMinutes

    cache = tmp_path / "cache"
    cache.mkdir()
    db = sqlite3.connect(cache / "candles.sqlite")
    db.execute("create table objects(key text primary key,status text,sha256 text)")
    db.commit()
    db.close()
    before = (cache / "candles.sqlite").read_bytes()
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr(
            "bars.csv", "1704067200000,100,102,99,101,1,1704067259999,100,1,0,0,0\n"
        )
    blob = zipped.getvalue()
    sha = hashlib.sha256(blob).hexdigest()

    class Response:
        status_code = 200
        content = blob
        text = sha + "  bars.zip"

        def raise_for_status(self):
            pass

    class Client:
        def __init__(self):
            self.urls = []

        def get(self, url):
            self.urls.append(url)
            assert url.startswith(
                "https://data.binance.vision/data/spot/monthly/klines/SOLUSDT/1m/"
            )
            return Response()

    client = Client()
    p = ResearchMinutes(cache, {}, tmp_path / "private", client)
    candle = p.candle("SOLUSDT", 1704067200000)
    assert candle["high"] == 102
    assert len(client.urls) == 2 and client.urls[-1].endswith(".CHECKSUM")
    p.db.close()
    assert (cache / "candles.sqlite").read_bytes() == before
    assert not (cache / "objects").exists()
    assert (
        next(iter(p.download_audit.values()))["first_observed_lookup"] == 1704067200000
    )
    assert next(iter(p.download_audit.values()))["official_checksum_verified"]


def test_pinned_missing_archive_never_overridden_by_private_download(tmp_path):
    import sqlite3

    from research.shadow_dynamic_initial_r import ResearchMinutes

    cache = tmp_path / "cache"
    cache.mkdir()
    key = "data/spot/monthly/klines/SOLUSDT/1m/SOLUSDT-1m-2024-01.zip"
    db = sqlite3.connect(cache / "candles.sqlite")
    db.execute("create table objects(key text primary key,status text,sha256 text)")
    db.execute("insert into objects values(?,?,?)", (key, "MISSING", None))
    db.commit()
    db.close()

    class Never:
        def get(self, url):
            raise AssertionError("Pinned revisions must stay immutable")

    p = ResearchMinutes(cache, {key: None}, tmp_path / "private", Never())
    assert p.candle("SOLUSDT", 1704067200000) is None
    p.db.close()
