"""Self-contained research exports. No raw minute candles or credentials."""

import csv
import io
import json
import zipfile

from crypto_bot.replay.stale import STRICT
from crypto_bot.web.analysis import csv_value

COLUMNS = (
    "run_id",
    "variant",
    "symbol",
    "asset_group",
    "entry_time",
    "exit_time",
    "duration_seconds",
    "entry_score",
    "delta_1h",
    "delta_4h",
    "delta_24h",
    "rsi",
    "atr",
    "atr_pct",
    "relative_volume",
    "momentum",
    "range_position",
    "entry_reference_price",
    "entry_fill_price",
    "entry_drift_pct",
    "entry_drift_atr",
    "original_stop_price",
    "stop_price",
    "exit_price",
    "exit_reason",
    "risk_amount",
    "notional",
    "realized_pnl",
    "return_pct",
    "r_multiple",
    "mfe_pct",
    "mae_pct",
    "fees",
    "slippage",
    "intrabar_ambiguity",
)


def payload(store, run_id, variant=0, compact=False):
    run = store.get(run_id)
    if run["status"] != "COMPLETED":
        raise ValueError("Export requires a completed run")
    if not 0 <= variant < len(run["variants"]):
        raise ValueError("Unknown variant")
    trades, curve = store.data(run_id, variant)
    out = dict(
        metadata=run["metadata"]
        | dict(
            run_id=run_id,
            created_at=run["created_at"],
            started_at=run["started_at"],
            finished_at=run["finished_at"],
            source="BINANCE_PUBLIC_KLINES_APPROXIMATION",
            not_statistically_validated=True,
            stale_position_policy=run["config"].get("stale_position_policy", STRICT),
        ),
        config=run["config"],
        variant=run["variants"][variant]["config"],
        summary=run["variants"][variant]["summary"]["metrics"],
        breakdowns=run["variants"][variant]["summary"]["breakdowns"],
        trades=[
            (
                {k: t.get(k) for k in COLUMNS if k not in ("run_id", "variant")}
                | dict(run_id=run_id, variant=variant)
            )
            if compact
            else t
            for t in trades
            if t["status"] == "CLOSED"
        ],
    )
    out["open_positions"] = [t for t in trades if t["status"] in ("OPEN", "OPEN_STALE")]
    if not compact:
        out["equity_curve"] = curve
    return out


def csv_export(store, run_id, variant=0):
    trades = payload(store, run_id, variant, True)["trades"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
    writer.writeheader()
    for t in trades:
        writer.writerow({k: csv_value(t.get(k)) for k in COLUMNS})
    return "\ufeff" + buffer.getvalue()


def bundle(store, run_id, variant=0):
    result = io.BytesIO()
    data = payload(store, run_id, variant)
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "analysis.json", json.dumps(data, ensure_ascii=False, allow_nan=False)
        )
        z.writestr(
            "compact-analysis.json",
            json.dumps(
                payload(store, run_id, variant, True),
                ensure_ascii=False,
                allow_nan=False,
            ),
        )
        z.writestr("trades.csv", csv_export(store, run_id, variant))
        with store.connection() as db:
            audit = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT data FROM replay_audit_events WHERE run_id=? AND variant=? ORDER BY id",
                    (run_id, variant),
                )
            ]
        z.writestr("audit.json", json.dumps(audit, ensure_ascii=False, allow_nan=False))
        z.writestr(
            "README.txt",
            "Historical replay research only. NO LIVE TRADING. OHLC approximation, not tick execution. See config, archive SHA256 revisions and sample labels.",
        )
    return result.getvalue()
