"""Fixed matched-entry A/B/C exit experiment. No portfolio, strategy or orders.

Existing AdaptivePeakLock.observe and ShadowPosition provide all intrabar,
fees, fills and stale semantics. Candidate classes change only two predeclared
protection constants; they are never registered with any application policy.
"""

import argparse
import hashlib
import io
import json
import math
import statistics as st
import zipfile
from bisect import bisect_left
from collections import Counter, defaultdict
from contextlib import contextmanager
from decimal import ROUND_CEILING
from functools import lru_cache
from pathlib import Path

from crypto_bot.data.base import Candle
from crypto_bot.replay.clock import at, ms
from crypto_bot.replay.config import settings_for
from crypto_bot.replay.historical_data import HistoricalData
from crypto_bot.replay.shadow_adaptive import NAME, AdaptivePeakLock
from crypto_bot.replay.shadow_engine import ShadowPosition
from crypto_bot.replay.shadow_store import canonical_entries
from crypto_bot.replay.universe import CandleHistory
from crypto_bot.trading.domain import D
from research.entry_market_context import (
    ANCHOR,
    anchor,
    database_fingerprint,
    digest,
    read_only,
)

NAMES = {
    "A": NAME,
    "B": "adaptive_peak_lock_050be",
    "C": "adaptive_peak_lock_075_lock025",
}
MINUTE = 60000
FLAT_PCT = 0.000001  # Existing adaptive Shadow return tolerance, uniform for A/B/C.


class EarlyProtection(AdaptivePeakLock):
    def __init__(self, entry, settings, name):
        if name not in (NAMES["B"], NAMES["C"]):
            raise ValueError("Only the two predeclared research candidates are allowed")
        super().__init__(entry, settings)
        self.name = name
        self.net_zero_stop = self.break_even
        self.lock_r = D("0.25") if name == NAMES["C"] else D(0)
        if name == NAMES["B"]:
            self.thresholds = (
                self.entry + self.risk / self.quantity * D("0.50"),
                *self.thresholds[1:],
            )
        else:
            unit = D("0.000000000001")
            required_fill = (
                (self.cost + self.lock_r * self.risk + unit)
                / self.quantity
                / (1 - self.fee)
            ).quantize(unit, rounding=ROUND_CEILING)
            self.break_even = (required_fill / (1 - self.slip)).quantize(
                unit, rounding=ROUND_CEILING
            )

    def audit(self, result):
        data = super().audit(result)
        data["exit_stage"] = (
            "ORIGINAL_STOP",
            "BREAK_EVEN_050R"
            if self.name == NAMES["B"]
            else "POSITIVE_LOCK_075R_NET025R",
            "PEAK_TRAIL_1R",
            "TIGHT_PEAK_TRAIL_2R",
        )[self.state.stage]
        if self.name == NAMES["B"]:
            data["activation_050r_time"] = data.pop("activation_075r_time")
        data["break_even_stop"] = str(self.net_zero_stop)
        data["initial_protection_stop"] = str(self.break_even)
        data["initial_protection_target_net_r"] = str(self.lock_r)
        return data


class ReadOnlyAnchor:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connection(self):
        db = read_only(self.path)
        try:
            yield db
        finally:
            db.close()

    def data(self, run_id, index):
        with self.connection() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT data FROM replay_trades WHERE run_id=? AND variant=? ORDER BY position_id",
                    (run_id, index),
                )
            ], []


class PinnedMinutes:
    """Cached ZIP reader; no network or cache writes; never predicts coverage end."""

    def __init__(self, root, pins):
        self.root, self.pins, self.used, self.supplemental = root, pins, {}, {}
        self.db = read_only(root / "candles.sqlite")

    @lru_cache(maxsize=64)
    def month(self, symbol, stamp):
        key = f"data/spot/monthly/klines/{symbol}/1m/{symbol}-1m-{stamp}.zip"
        row = self.db.execute(
            "SELECT status,sha256 FROM objects WHERE key=?", (key,)
        ).fetchone()
        if key in self.pins:
            sha = self.pins[key]
            if row != ("COMPLETE" if sha else "MISSING", sha):
                raise ValueError("Changed pinned minute source: " + key)
        else:
            sha = row[1] if row and row[0] == "COMPLETE" else None
            self.supplemental[key] = sha
        self.used[key] = sha
        if sha is None:
            return CandleHistory([])
        blob = (self.root / "objects" / (sha + ".zip")).read_bytes()
        if hashlib.sha256(blob).hexdigest() != sha:
            raise ValueError("Corrupt minute archive: " + key)
        with zipfile.ZipFile(io.BytesIO(blob)) as zipped:
            rows = HistoricalData.parse(
                zipped.read(zipped.namelist()[0]).decode("utf-8-sig")
            )
        return CandleHistory((Candle(r[0], r[1], *r[2:7]), r[7]) for r in rows)

    def candle(self, symbol, timestamp):
        rows = self.month(symbol, at(timestamp).strftime("%Y-%m"))
        i = bisect_left(rows.columns[0], timestamp)
        if i >= len(rows) or rows.columns[0][i] != timestamp:
            return None
        c, _ = rows[i]
        return dict(
            time=c.open_time,
            close_time=c.close_time,
            open=c.open,
            high=c.high,
            low=c.low,
            close=c.close,
            interval="1m",
        )


def position(entry, label, settings, stale_policy):
    p = ShadowPosition(
        entry,
        {"id": label, "exit_policy": NAME, "exit_parameters": {}},
        settings,
        stale_policy,
    )
    if label != "A":
        p.definition["exit_policy"] = NAMES[label]
        p.adaptive = EarlyProtection(entry, settings, NAMES[label])
    return p


def simulate(entry, labels, settings, config, provider, conservative):
    positions = [
        position(entry, label, settings, config["stale_position_policy"])
        for label in labels
    ]
    cursor, end = ms(entry["entry_time"]), ms(config["end"])
    while cursor < end and any(not p.result for p in positions):
        candle = provider.candle(entry["symbol"], cursor)
        for p in positions:
            if p.result:
                continue
            if cursor % 3600000 == 0:
                p.fresh(candle is not None, cursor)
                if candle:
                    p.mark(candle["open"], cursor)
            now = cursor + MINUTE
            p.fresh(candle is not None, now)
            if candle:
                p.bar(candle, now, conservative)
        cursor += MINUTE
    return [p.export(ms(p.result["exit_time"]) if p.result else end) for p in positions]


def parity(rows, trades):
    lookup = {t["position_id"]: t for t in trades}
    differences = []
    ids = [int(r["shadow_entry_id"].split(":")[-1]) for r in rows]
    if len(set(ids)) != len(ids) or set(ids) != set(lookup):
        differences.append(
            {
                "field": "canonical_entry_identity",
                "unique_outcomes": len(set(ids)),
                "expected_unique_entries": len(lookup),
            }
        )
    fields = (
        "exit_time",
        "exit_price",
        "exit_reason",
        "exit_stage",
        "realized_pnl",
        "realized_r",
        "fees",
        "slippage",
        "exit_fee",
        "return_pct",
    )
    money = {"exit_price", "realized_pnl", "fees", "slippage", "exit_fee"}
    for row in rows:
        pid = int(row["shadow_entry_id"].split(":")[-1])
        original = lookup[pid]
        for key in fields:
            a, b = row.get(key), original.get(key)
            equal = (
                D(a) == D(b)
                if key in money and a is not None and b is not None
                else a == b
            )
            if not equal:
                differences.append(
                    {"position_id": pid, "field": key, "shadow": a, "anchor": b}
                )
        if not row["comparison_eligible"]:
            differences.append(
                {
                    "position_id": pid,
                    "field": "comparison_eligible",
                    "status": row["status"],
                }
            )
    return {
        "passed": len(rows) == len(trades) == 1506 and not differences,
        "canonical_entry_count": len(rows),
        "checked_fields": list(fields),
        "mismatch_count": len(differences),
        "mismatches": differences,
    }


def category(row):
    if not row["comparison_eligible"]:
        return row["status"]
    return (
        "WIN"
        if row["return_pct"] > FLAT_PCT
        else "LOSS"
        if row["return_pct"] < -FLAT_PCT
        else "FLAT"
    )


def metrics(rows):
    closed = [r for r in rows if r["comparison_eligible"]]
    rs = [r["realized_r"] for r in closed]
    loss = -sum(min(0, r) for r in rs)
    counts = Counter(category(r) for r in rows)
    return {
        "outcome_count": len(rows),
        "closed": len(closed),
        "open": sum(
            r["position_status"] == "OPEN" and r["status"] != "OPEN_STALE" for r in rows
        ),
        "stale": sum(r["status"] == "OPEN_STALE" for r in rows),
        "incomplete": sum(not r["data_complete"] for r in rows),
        "status_counts": dict(Counter(r["status"] for r in rows)),
        "win": counts["WIN"],
        "flat": counts["FLAT"],
        "loss": counts["LOSS"],
        "mean_r": st.mean(rs) if rs else None,
        "median_r": st.median(rs) if rs else None,
        "total_realized_r": sum(rs),
        "pf_r": sum(max(0, r) for r in rs) / loss if loss else None,
        "mean_return_pct": st.mean(r["return_pct"] for r in closed) if closed else None,
        "normalized_pnl_sum_per_1000_entry_cost": sum(
            r["normalized_pnl_per_1000"] for r in closed
        ),
        "normalized_pnl_mean_per_1000_entry_cost": st.mean(
            r["normalized_pnl_per_1000"] for r in closed
        )
        if closed
        else None,
        "canonical_realized_pnl": str(
            sum((D(r["realized_pnl"]) for r in closed), D(0))
        ),
        "fees": str(sum((D(r["fees"]) for r in closed), D(0))),
        "slippage": str(sum((D(r["slippage"]) for r in closed), D(0))),
        "mean_hold_seconds": st.mean(r["duration_seconds"] for r in closed)
        if closed
        else None,
        "ambiguity_count": sum(r["intrabar_ambiguity"] for r in rows),
    }


def paired(control, candidate):
    lookup = {r["shadow_entry_id"]: r for r in candidate}
    pairs = [
        (a, lookup[a["shadow_entry_id"]])
        for a in control
        if a["comparison_eligible"]
        and lookup[a["shadow_entry_id"]]["comparison_eligible"]
    ]
    matrix, stages, parts = Counter(), Counter(), defaultdict(list)
    rescued, damaged, earlier_damaged = [], [], []
    for a, b in pairs:
        delta = b["realized_r"] - a["realized_r"]
        matrix[(a["exit_stage"], category(b))] += 1
        stages[(a["exit_stage"], b["exit_stage"])] += 1
        if a["exit_stage"] == "ORIGINAL_STOP" and category(b) in ("FLAT", "WIN"):
            rescued.append((a, b, delta))
            group = "rescued_original_stops"
        elif a["exit_stage"] == "BREAK_EVEN_075R" and delta > 1e-8:
            group = "improved_break_even_exits"
        elif (
            category(a) == "WIN"
            and delta < -1e-8
            and ms(b["exit_time"]) < ms(a["exit_time"])
        ):
            group = "truncated_future_winners"
        else:
            group = "other_exit_changes"
        parts[group].append(delta)
        if a["realized_r"] > 0 and delta < -1e-8:
            damaged.append((a, b, delta))
            if ms(b["exit_time"]) < ms(a["exit_time"]):
                earlier_damaged.append((a, b, delta))
    thresholds = {}
    earlier_thresholds = {}
    for threshold in (0.5, 1.0):
        subset = [(a, b, dr) for a, b, dr in damaged if a["realized_r"] > threshold]
        thresholds[str(threshold)] = {
            str(cut): {
                "count": sum(dr < -cut for _, _, dr in subset),
                "lost_r": -sum(dr for _, _, dr in subset if dr < -cut),
            }
            for cut in (0.1, 0.25, 0.5)
        }
        earlier_subset = [
            (a, b, dr) for a, b, dr in earlier_damaged if a["realized_r"] > threshold
        ]
        earlier_thresholds[str(threshold)] = {
            str(cut): {
                "count": sum(dr < -cut for _, _, dr in earlier_subset),
                "lost_r": -sum(dr for _, _, dr in earlier_subset if dr < -cut),
            }
            for cut in (0.1, 0.25, 0.5)
        }
    decomposition = {
        key: {"count": len(parts[key]), "delta_r": sum(parts[key])}
        for key in (
            "rescued_original_stops",
            "improved_break_even_exits",
            "truncated_future_winners",
            "other_exit_changes",
        )
    }
    delta = sum(b["realized_r"] - a["realized_r"] for a, b in pairs)
    residual = sum(v["delta_r"] for v in decomposition.values()) - delta
    if not math.isclose(residual, 0, abs_tol=1e-8):
        raise ValueError("Net-effect decomposition does not reconcile")
    return {
        "comparable_pairs": len(pairs),
        "excluded_pairs": len(control) - len(pairs),
        "net_delta_r": delta,
        "transition_by_control_stage_candidate_outcome": [
            {"control_stage": a, "candidate_outcome": b, "count": n}
            for (a, b), n in sorted(matrix.items())
        ],
        "transition_by_stage": [
            {"control_stage": a, "candidate_stage": b, "count": n}
            for (a, b), n in sorted(stages.items())
        ],
        "rescue": {
            "count": len(rescued),
            "flat": sum(category(b) == "FLAT" for _, b, _ in rescued),
            "positive": sum(category(b) == "WIN" for _, b, _ in rescued),
            "gained_delta_r": sum(dr for _, _, dr in rescued),
        },
        "winner_damage": {
            "count": len(damaged),
            "lost_r": -sum(dr for _, _, dr in damaged),
            "premature_exit_count": len(earlier_damaged),
            "lost_r_from_premature_exits": -sum(dr for _, _, dr in earlier_damaged),
            "control_r_thresholds": thresholds,
            "premature_exit_control_r_thresholds": earlier_thresholds,
            "same_minute_damage_count": len(damaged) - len(earlier_damaged),
            "same_minute_damage_lost_r": -sum(
                dr for a, b, dr in damaged if b["exit_time"] == a["exit_time"]
            ),
            "nonflat_winner_damage_count": sum(
                category(a) == "WIN" for a, _, _ in damaged
            ),
        },
        "net_effect_decomposition": decomposition,
        "decomposition_residual_r": residual,
    }


def summarize(rows, canonical_trades):
    by_policy = {key: [r for r in rows if r["policy_id"] == key] for key in NAMES}
    result = {
        "policies": {key: metrics(value) for key, value in by_policy.items()},
        "comparisons": {
            key: paired(by_policy["A"], by_policy[key]) for key in ("B", "C")
        },
        "stage_contribution": {},
        "time_robustness": {},
        "mfe_cohorts": [],
    }
    for key, value in by_policy.items():
        stages = sorted({r["exit_stage"] for r in value})
        result["stage_contribution"][key] = {
            stage: metrics([r for r in value if r["exit_stage"] == stage])
            for stage in stages
        }
    for label in [f"Q{i}" for i in range(1, 5)] + [
        f"2024-{i:02d}" for i in range(1, 13)
    ]:

        def selected(r):
            return (
                (int(r["entry_time"][5:7]) - 1) // 3 + 1 == int(label[1:])
                if label.startswith("Q")
                else r["entry_time"].startswith(label)
            )

        subset = {k: [r for r in v if selected(r)] for k, v in by_policy.items()}
        result["time_robustness"][label] = {
            "policies": {k: metrics(v) for k, v in subset.items()},
            "comparisons": {k: paired(subset["A"], subset[k]) for k in ("B", "C")},
        }
    lookup = {t["position_id"]: t for t in canonical_trades}
    for threshold in (0.25, 0.4, 0.5, 0.6, 0.7):
        ids = {
            r["shadow_entry_id"]
            for r in by_policy["A"]
            if r["exit_stage"] == "ORIGINAL_STOP"
            and lookup[int(r["shadow_entry_id"].split(":")[-1])]["highest_r"]
            >= threshold
        }
        subset = {
            k: [r for r in v if r["shadow_entry_id"] in ids]
            for k, v in by_policy.items()
        }
        result["mfe_cohorts"].append(
            {
                "control_highest_r_minimum": threshold,
                "n": len(ids),
                "policies": {k: metrics(v) for k, v in subset.items()},
                "comparisons": {k: paired(subset["A"], subset[k]) for k in ("B", "C")},
            }
        )
    result["monthly_delta_robustness"] = {}
    for key in ("B", "C"):
        monthly = {
            label: value["comparisons"][key]["net_delta_r"]
            for label, value in result["time_robustness"].items()
            if label.startswith("2024-")
        }
        total = result["comparisons"][key]["net_delta_r"]
        best = sorted(monthly, key=monthly.get, reverse=True)
        result["monthly_delta_robustness"][key] = {
            "positive_months": sum(v > 1e-8 for v in monthly.values()),
            "negative_months": sum(v < -1e-8 for v in monthly.values()),
            "leave_one_month_out_delta_r": {m: total - dr for m, dr in monthly.items()},
            "best_month": best[0],
            "without_best_month_delta_r": total - monthly[best[0]],
            "best_two_months": best[:2],
            "without_best_two_months_delta_r": total
            - sum(monthly[m] for m in best[:2]),
            "diagnostic_only_not_a_month_filter": True,
        }
    return result


def intrabar_sensitivity(conservative, optimistic):
    lookup = {(r["shadow_entry_id"], r["policy_id"]): r for r in optimistic}
    result = {}
    for key in NAMES:
        pairs = [
            (a, lookup[a["shadow_entry_id"], key])
            for a in conservative
            if a["policy_id"] == key
        ]
        result[key] = {
            "paired_entries": len(pairs),
            "different_exit_timestamp": sum(
                a["exit_time"] != b["exit_time"] for a, b in pairs
            ),
            "different_exit_fill": sum(
                a["exit_price"] != b["exit_price"] for a, b in pairs
            ),
            "different_exit_stage": sum(
                a["exit_stage"] != b["exit_stage"] for a, b in pairs
            ),
            "optimistic_minus_conservative_total_r": sum(
                b["realized_r"] - a["realized_r"]
                for a, b in pairs
                if a["comparison_eligible"] and b["comparison_eligible"]
            ),
        }
    return result


def write(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--summarize-existing",
        action="store_true",
        help="Require all three finished phases; recalculate reports without price execution",
    )
    args = parser.parse_args()
    output = args.output or args.root / "artifacts/shadow-early-protection-2024"
    output.mkdir(parents=True, exist_ok=True)
    source = ReadOnlyAnchor(args.root / "data/replay.db")
    with source.connection() as db:
        config, metadata, trades = anchor(db)
        before = database_fingerprint(db)
    if config["fallback_5m"] or not config["conservative"]:
        raise ValueError("Expected conservative, no-fallback B anchor")
    entries = canonical_entries(source, ANCHOR, 0)
    if len(entries) != 1506:
        raise ValueError("Canonical BUY fill count must remain 1506")
    settings = settings_for(config["variants"][0])
    provider = PinnedMinutes(
        args.root / "data/replay-cache", metadata["historical_data_revision"]
    )
    entry_hash = digest(entries)
    expected = {
        "anchor_id": ANCHOR,
        "canonical_entry_count": len(entries),
        "entry_hash": entry_hash,
        "anchor_fingerprint": before,
    }
    if (output / "before.json").exists() and json.loads(
        (output / "before.json").read_text()
    ) != expected:
        raise ValueError(
            "Existing experiment has a different anchor; will not overwrite"
        )
    if args.summarize_existing and any(
        not (output / name).exists()
        for name in (
            "control-conservative.json",
            "candidates-conservative.json",
            "all-optimistic.json",
        )
    ):
        raise ValueError("All completed phases are required; no simulation was started")
    write(output / "before.json", expected)

    def batch(filename, labels, conservative):
        path = output / filename
        if path.exists():
            saved = json.loads(path.read_text())
            if (
                saved["entry_hash"] != entry_hash
                or saved["anchor_fingerprint"] != before
            ):
                raise ValueError(
                    "Existing phase has changed anchor; will not overwrite"
                )
            provider.used.update(saved["source_revisions"])
            provider.supplemental.update(saved["supplemental_sources"])
            return saved["outcomes"]
        rows = []
        for i, e in enumerate(entries):
            rows.extend(simulate(e, labels, settings, config, provider, conservative))
            if i % 50 == 0 or i + 1 == len(entries):
                progress = {
                    "phase": filename,
                    "canonical_entries_processed": i + 1,
                    "canonical_entry_count": len(entries),
                    "entry_time": e["entry_time"],
                }
                write(output / "progress.json", progress)
                print(json.dumps(progress), flush=True)
        write(
            path,
            expected
            | {
                "outcomes": rows,
                "source_revisions": provider.used,
                "supplemental_sources": provider.supplemental,
            },
        )
        return rows

    control = batch("control-conservative.json", ["A"], True)
    proof = parity(control, trades)
    write(output / "control-parity.json", proof)
    if not proof["passed"]:
        write(
            output / "validation.json",
            {"passed": False, "control_parity": proof, "candidates_not_started": True},
        )
        raise ValueError("Control parity mismatch: candidates MUST NOT run")
    primary = control + batch("candidates-conservative.json", ["B", "C"], True)
    optimistic = batch("all-optimistic.json", ["A", "B", "C"], False)
    with source.connection() as db:
        if database_fingerprint(db) != before:
            raise ValueError("Source B records changed")
    if digest(entries) != entry_hash:
        raise ValueError("Canonical entries mutated")
    compact = {
        "schema_version": 1,
        "dataset_role": "2024 EXPLORATION",
        **expected,
        "policies": NAMES,
        "control_parity": proof,
        "methodology": {
            "experiment": "MATCHED-ENTRY SHADOW EXITS; NOT PORTFOLIO PERFORMANCE",
            "entries": "Persisted FILLED BUYs only, no filtering/regeneration/resizing",
            "execution": "Unchanged ShadowPosition.close/bar and AdaptivePeakLock.observe/walk/ratchet",
            "primary": "conservative",
            "sensitivity": "optimistic, same existing path selection",
            "flat": "abs(return_pct) <= 0.000001 for all three policies, existing adaptive tolerance",
            "net_lock": "Canonical initial risk * 0.25 added to cost; inverse fee/slippage with existing ceiling-quantization guard",
            "gaps": "No invented fill, no retrospective/last-price/year-end exit; a gapped outcome remains incomplete even if resumed",
            "timing": "Closed minute OHLC known at minute end; no future bar used; entry-month robustness",
            "cohorts": "Frozen original B control highest_r, never truncated candidate MFE",
            "normalization": "PnL per 1000 canonical entry cost; totals sum independent trades, not portfolio equity",
            "decomposition": "Disjoint priority: rescued stops, improved control BE, earlier damaged winners, other; all deltas reconciled",
            "winner_damage": "Control realized R > 0 with candidate delta R < -1e-8, including near-zero BE trades; nonflat winner count also reported. Threshold tables cover all damage, plus a separate strict-earlier subset. Same-minute OHLC damage separately reported; not claimed as proven earlier timing",
            "archive_sources": "Read-only cached monthly ZIPs, SHA256 verified; supplemental existing cached objects explicitly listed; source availability is never a close trigger",
            "optimization": "NONE; exactly the three predeclared policies",
        },
        "conservative": summarize(primary, trades),
        "optimistic": summarize(optimistic, trades),
        "intrabar_sensitivity": intrabar_sensitivity(primary, optimistic),
    }
    compact_path = output / "2024-B-shadow-early-protection.compact.json"
    detailed_path = output / "2024-B-shadow-early-protection.detailed.json"
    write(compact_path, compact)
    write(
        detailed_path,
        {
            "metadata": compact,
            "canonical_entries": entries,
            "canonical_control_trades": trades,
            "conservative_outcomes": primary,
            "optimistic_outcomes": optimistic,
            "minute_archive_revisions": provider.used,
            "supplemental_cached_sources": provider.supplemental,
        },
    )
    validation = {
        "passed": True,
        "control_parity": proof,
        "canonical_entry_count": len(entries),
        "entry_hash_before": entry_hash,
        "entry_hash_after": digest(entries),
        "source_anchor_unchanged": True,
        "production_code_changed": False,
        "portfolio_replays_started": 0,
        "service_restarts": 0,
        "exports": {
            p.name: {
                "path": str(p.resolve()),
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
            for p in (compact_path, detailed_path)
        },
    }
    write(output / "validation.json", validation)
    print(
        json.dumps(
            {
                "completed": True,
                "control_parity": proof,
                "exports": validation["exports"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
