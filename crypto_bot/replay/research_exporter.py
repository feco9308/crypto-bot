"""Read-only complete research export. No strategy selection or replay actions."""

import hashlib
import io
import json
import re
import sqlite3
import zipfile
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from statistics import mean, median

from crypto_bot.replay.config import config_hash, input_config
from crypto_bot.replay.exporter import payload
from crypto_bot.replay.institutional_metrics import research_outcome
from crypto_bot.replay.metrics import outcome
from crypto_bot.replay.models import ReplayStore


class ReadOnlyReplayStore(ReplayStore):
    read_only = True

    def __init__(self, path):
        self.path = Path(path).resolve()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        try:
            yield db
        finally:
            db.close()


def encoded(value):
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
        + "\n"
    ).encode()


def letter(index):
    # Stable labels for all supported run sizes, including more than 26 variants.
    label = ""
    while index >= 0:
        label = chr(65 + index % 26) + label
        index = index // 26 - 1
    return label


def variant_labels(variants):
    """Keep explicit research labels (A/F/G/H); otherwise use stable indices."""
    matches = [
        re.match(r"^([A-Z])(?:$|\s*[-–—]\s)", v["config"]["name"]) for v in variants
    ]
    labels = [m[1] for m in matches if m]
    if len(labels) == len(variants) and len(set(labels)) == len(labels):
        return labels
    return [letter(i) for i in range(len(variants))]


def stats(trades, key):
    values = [float(t[key]) for t in trades if t.get(key) is not None]
    return {
        "count": len(values),
        "missing": len(trades) - len(values),
        "mean": mean(values) if values else None,
        "median": median(values) if values else None,
    }


def research_files(store, run_id):
    if not getattr(store, "read_only", False):
        raise ValueError("Research export requires a read-only store")
    run = store.get(run_id)
    if run["status"] != "COMPLETED":
        raise ValueError("Research export requires a completed run")
    cfg, meta = run["config"], run["metadata"]
    if config_hash(cfg) != meta["config_hash"]:
        raise ValueError("Stored replay config hash mismatch")
    if len(run["variants"]) != len(cfg["variants"]):
        raise ValueError("Stored replay variant count mismatch")
    if meta.get(
        "market_context_version",
        cfg.get("market_context_version", "LEGACY_SHARED_HELD_V1"),
    ) != cfg.get("market_context_version", "LEGACY_SHARED_HELD_V1"):
        raise ValueError("Stored replay context version mismatch")
    files = {}
    variants = []
    counts = []
    coverage = []
    labels = variant_labels(run["variants"])
    for index, variant in enumerate(run["variants"]):
        if variant["number"] != index or variant["config"] != cfg["variants"][index]:
            raise ValueError("Stored replay variant configuration mismatch")
        label = labels[index]
        full = payload(store, run_id, index)
        compact = payload(store, run_id, index, True)
        trades = sorted(
            full["trades"] + full["open_positions"], key=lambda t: t["position_id"]
        )
        ids = [t["position_id"] for t in trades]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate replay position record")
        m = full["summary"]
        closed = full["trades"]
        if (
            len(closed) != m["trade_count"]
            or len(full["open_positions"]) != m["open_positions"]
        ):
            raise ValueError("Stored replay position counts mismatch")
        stale = sum(t["status"] == "OPEN_STALE" for t in trades)
        if stale != m.get("stale_position_count", stale):
            raise ValueError("Stored stale position counts mismatch")
        normalized = [
            t
            | dict(
                run_id=run_id,
                variant=index,
                variant_label=label,
                realized_r=t.get("r_multiple"),
                giveback_r=t.get("peak_to_exit_giveback_r"),
                holding_time_seconds=t.get("duration_seconds"),
            )
            for t in trades
        ]
        files[label + ".analysis.json"] = encoded(full)
        files[label + ".compact.json"] = encoded(compact)
        files[label + ".trades.json"] = encoded(
            dict(
                run_id=run_id,
                variant=index,
                variant_label=label,
                config=variant["config"],
                trades=normalized,
            )
        )
        excursion = {
            name: stats(closed, key)
            for name, key in [
                ("mfe_r", "mfe_r"),
                ("mae_r", "mae_r"),
                ("realized_r", "r_multiple"),
                ("giveback_r", "peak_to_exit_giveback_r"),
            ]
        }
        stages = full["breakdowns"].get("research_exit_stage")
        if stages is None:
            groups = {}
            for trade in closed:
                groups.setdefault(
                    trade.get(
                        "research_exit_stage", trade.get("exit_stage", "UNKNOWN")
                    ),
                    [],
                ).append(trade)
            stages = {
                stage: outcome(group) | research_outcome(group)
                for stage, group in sorted(groups.items())
            }
        variants.append(
            dict(
                variant=index,
                variant_label=label,
                name=variant["config"]["name"],
                config=variant["config"],
                metrics=m,
                breakdowns=full["breakdowns"],
                closed=len(closed),
                open=len(trades) - len(closed) - stale,
                open_stale=stale,
                final_equity=m["final_equity"],
                net_pnl=m["net_pnl"],
                return_pct=m["return_pct"],
                profit_factor=m["profit_factor"],
                expectancy_r=m.get("expectancy_r", m.get("avg_r")),
                max_drawdown=m["max_drawdown"],
                win_rate=m["win_rate"],
                turnover=m.get(
                    "turnover_notional", research_outcome(trades)["turnover_notional"]
                ),
                fees=m["fees"],
                slippage=m["slippage_cost"],
                average_holding_time_seconds=m["average_hold_seconds"],
                excursions=excursion,
                exit_stage_breakdown=stages,
                reentry_rearm={
                    k: v for k, v in m.items() if "reentry" in k or "rearm" in k
                },
            )
        )
        counts.append(
            dict(
                variant=index,
                label=label,
                closed=len(closed),
                open=len(trades) - len(closed) - stale,
                open_stale=stale,
                positions=len(trades),
                equity_curve=len(full["equity_curve"]),
            )
        )
        required = [
            "position_id",
            "symbol",
            "status",
            "entry_time",
            "exit_time",
            "entry_fill_price",
            "quantity",
            "notional",
            "risk_amount",
            "realized_pnl",
            "realized_r",
            "mfe_r",
            "mae_r",
            "giveback_r",
            "exit_reason",
            "holding_time_seconds",
        ]
        coverage.append(
            dict(
                variant=index,
                fields={
                    k: dict(
                        present=sum(k in t for t in normalized),
                        non_null=sum(t.get(k) is not None for t in normalized),
                    )
                    for k in required
                },
            )
        )
    if sum(v["positions"] for v in counts) != run["progress"].get(
        "trades_opened", sum(v["positions"] for v in counts)
    ):
        raise ValueError("Stored replay trade total mismatch")
    if sum(v["closed"] for v in counts) != run["progress"].get(
        "trades_closed", sum(v["closed"] for v in counts)
    ):
        raise ValueError("Stored replay closed trade total mismatch")
    comparisons = {}
    for i, left in enumerate(variants):
        for right in variants[i + 1 :]:
            delta = {}
            for key in [
                "final_equity",
                "net_pnl",
                "return_pct",
                "profit_factor",
                "expectancy_r",
                "max_drawdown",
                "win_rate",
                "closed",
                "turnover",
                "fees",
                "slippage",
                "average_holding_time_seconds",
            ]:
                a, b = left[key], right[key]
                delta[key] = (
                    str(Decimal(str(b)) - Decimal(str(a)))
                    if a is not None and b is not None
                    else None
                )
            comparisons[left["variant_label"] + "_vs_" + right["variant_label"]] = dict(
                reference=left["variant"], candidate=right["variant"], difference=delta
            )
    metadata = meta | dict(
        run_id=run_id,
        status=run["status"],
        created_at=run["created_at"],
        started_at=run["started_at"],
        finished_at=run["finished_at"],
        market_context_version=cfg.get(
            "market_context_version", "LEGACY_SHARED_HELD_V1"
        ),
        dataset_role=cfg["dataset_role"],
        period=dict(start=cfg["start"], end=cfg["end"]),
        universe=dict(
            size=cfg["universe_size"],
            method=cfg["universe_method"],
            candidate_symbols=meta.get(
                "resolved_candidate_symbols", cfg.get("candidate_symbols")
            ),
        ),
    )
    summary = dict(
        run_id=run_id,
        status=run["status"],
        metadata=metadata,
        progress=run["progress"],
        variants=variants,
        comparisons=comparisons,
        winner_selected=False,
        definitions=dict(
            differences="candidate minus reference; percentages are percentage-point differences",
            config="config.json is public import JSON; validated-config.json is the frozen run snapshot",
            excursions="Closed-trade observations from stored records; null means unavailable",
            open_values="Open/open_stale records retain null exit/return/giveback values",
            rearm_success="Stored counter: first FALSE reset; not necessarily a subsequent filled BUY",
        ),
    )
    files["run-summary.json"] = encoded(summary)
    files["config.json"] = encoded(input_config(cfg))
    files["validated-config.json"] = encoded(cfg)
    files["run-metadata.json"] = encoded(metadata)
    files["comparison.json"] = encoded(
        dict(run_id=run_id, comparisons=comparisons, winner_selected=False)
    )
    records = {
        name: dict(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
        for name, body in sorted(files.items())
    }
    validation = dict(
        run_id=run_id,
        run_completed_verified=True,
        config_hash=meta["config_hash"],
        config_hash_verified=True,
        market_context_version=metadata["market_context_version"],
        exact_git_sha=meta["git_commit"],
        file_record_counts=counts,
        required_field_coverage=coverage,
        duplicate_positions=False,
        all_variants_included=len(variants),
        total_closed=sum(v["closed"] for v in counts),
        total_open=sum(v["open"] for v in counts),
        total_open_stale=sum(v["open_stale"] for v in counts),
        read_only_database_access=dict(
            sqlite_uri_mode="ro",
            pragma_query_only=True,
            new_replay_started=False,
            database_writes=False,
        ),
        sha256_files=records,
        hash_policy="Validation excludes itself; manifest includes validation but excludes itself.",
    )
    files["export-validation.json"] = encoded(validation)
    records = records | {
        "export-validation.json": dict(
            bytes=len(files["export-validation.json"]),
            sha256=hashlib.sha256(files["export-validation.json"]).hexdigest(),
        )
    }
    files["manifest.json"] = encoded(
        dict(
            run_id=run_id,
            config_hash=meta["config_hash"],
            exact_git_sha=meta["git_commit"],
            market_context_version=metadata["market_context_version"],
            files=records,
            self_hash_excluded=True,
        )
    )
    return files


def research_bundle(store, run_id):
    files = research_files(store, run_id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, body in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, body, compresslevel=9)
    return buffer.getvalue()
