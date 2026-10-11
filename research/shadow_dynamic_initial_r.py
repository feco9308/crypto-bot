"""Matched-entry dynamic initial R; standalone research, read-only anchor/cache."""

import argparse
import copy
import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

from crypto_bot.replay.clock import at, ms
from crypto_bot.replay.config import settings_for
from crypto_bot.replay.shadow_adaptive import NAME
from crypto_bot.replay.shadow_engine import MINUTE, ShadowPosition
from crypto_bot.replay.shadow_store import canonical_entries
from crypto_bot.replay.stale import ReplayRiskView
from crypto_bot.risk.manager import RiskManager
from crypto_bot.trading.domain import D, PositionView, StrategySignal, q
from research.entry_market_context import ANCHOR, anchor, database_fingerprint, digest
from research.initial_risk_distance import stats, verify_filled_entries
from research.shadow_early_protection import (
    PinnedMinutes,
    ReadOnlyAnchor,
    category,
    metrics,
    parity,
    write,
)

LABELS = ("CONTROL", "A", "B", "C", "D")
DEFINITIONS = {
    "CONTROL": "2 * ATR14",
    "A": "2 * ATR72",
    "B": "2 * ATR120",
    "C": "max(2 * ATR14, 1.5 * ATR72)",
    "D": "max(2 * ATR14, pullback_P75_72h * strategy_reference_price / 100)",
}


def loss_per_unit(quote, stop, settings):
    """Same immutable expression as RiskManager; no fill-price rebasing."""
    slip = settings.paper_slippage_pct / D(100)
    fee = settings.paper_fee_pct / D(100)
    entry = D(quote) * (1 + slip)
    exit_fill = D(stop) * (1 - slip)
    return entry - exit_fill + (entry + exit_fill) * fee


def derive(entry, record, fill, settings):
    f = record["features"]
    ref = D(record["anchor_trade"]["entry_reference_price"])
    # Preserve original Decimal strategy arithmetic, never round the stop.
    control = D(str(f["atr14_abs"])) * D(2)
    distances = {
        "CONTROL": control,
        "A": D(str(f["atr72_abs"])) * 2,
        "B": D(str(f["atr120_abs"])) * 2,
        "C": max(control, D(str(f["atr72_abs"])) * D("1.5")),
        "D": max(control, D(str(f["pullback_p75_72h_pct"])) * ref / 100),
    }
    output = {}
    for name, distance in distances.items():
        stop = ref - distance
        if stop <= 0 or stop >= D(fill["reference_price"]):
            raise ValueError("Invalid candidate stop; do not drop matched entry")
        risk = q(
            D(entry["quantity"])
            * loss_per_unit(fill["reference_price"], stop, settings)
        )
        e = copy.deepcopy(entry)
        e["original_stop_price"] = str(stop)
        e["risk_amount"] = str(risk)
        output[name] = {
            "entry": e,
            "distance_abs": str(distance),
            "distance_ratio": float(distance / control),
            "unit_r": str(risk / D(entry["quantity"])),
            "unit_r_pct": float(
                risk / D(entry["quantity"]) / D(entry["entry_fill_price"]) * 100
            ),
            "stop_price": str(stop),
            "risk_amount": str(risk),
            "reference_price": str(ref),
        }
    if D(output["CONTROL"]["stop_price"]) != D(entry["original_stop_price"]):
        raise ValueError("CONTROL original stop parity mismatch; candidates forbidden")
    if D(output["CONTROL"]["risk_amount"]) != D(entry["risk_amount"]):
        raise ValueError(
            "CONTROL risk denominator parity mismatch; candidates forbidden"
        )
    return output


def snapshot_view(snapshot):
    positions = []
    for row in snapshot["positions"]:
        r = dict(row)
        for key in (
            "quantity",
            "entry_price",
            "current_price",
            "stop_price",
            "entry_fee",
            "take_profit_price",
        ):
            if r[key] is not None:
                r[key] = D(r[key])
        positions.append(PositionView(**r))
    fields = {
        k: D(snapshot[k])
        for k in (
            "equity",
            "cash_balance",
            "reserved_cash",
            "realized_pnl",
            "fees_paid",
            "daily_pnl",
            "day_start_equity",
            "peak_equity",
            "max_drawdown",
        )
    }
    return ReplayRiskView(
        **fields,
        positions=tuple(positions),
        stale_exposure_reservation=D(snapshot.get("stale_exposure_reservation", "0")),
    )


def sizing_audit(entry, derived, fill, snapshot, settings):
    view = snapshot_view(snapshot)
    budget = D(entry["risk_amount"])
    risk_settings = replace(settings, risk_per_trade_pct=budget / view.equity * 100)
    cost_per_unit = (
        D(fill["reference_price"])
        * (1 + settings.paper_slippage_pct / 100)
        * (1 + settings.paper_fee_pct / 100)
    )
    result = {}
    for name, d in derived.items():
        requested = budget / loss_per_unit(
            fill["reference_price"], d["stop_price"], settings
        )
        desired = q(requested)
        notional = q(
            desired
            * D(fill["reference_price"])
            * (1 + settings.paper_slippage_pct / 100)
        )
        symbol_exposure = sum(
            (p.market_value for p in view.positions if p.symbol == entry["symbol"]),
            D(0),
        )
        rooms = {
            "total_exposure": view.equity * settings.max_total_exposure_pct / 100
            - view.exposure,
            "symbol_exposure": view.equity * settings.max_symbol_exposure_pct / 100
            - symbol_exposure,
            "available_cash": view.available_cash,
        }
        caps = [key for key, room in rooms.items() if requested > room / cost_per_unit]
        if notional < settings.minimum_order_value:
            caps.append("minimum_order_value")
        signal = StrategySignal(
            entry["symbol"],
            "BUY",
            D(1),
            D(entry["entry_fill_price"]),
            D(d["stop_price"]),
            at(ms(entry["entry_time"])),
            entry.get("strategy_name", "canonical"),
            ("HYPOTHETICAL_FIXED_RISK_AUDIT",),
        )
        decision = RiskManager(risk_settings).evaluate(
            signal, view, entry_price=fill["reference_price"]
        )
        result[name] = {
            "scope": "HYPOTHETICAL_FIXED_RISK_SIZING_ONLY",
            "requested_quantity": str(desired),
            "quantity_ratio": float(desired / D(entry["quantity"])),
            "risk_budget": str(budget),
            "WOULD_HIT_RISK_CAP": bool(caps),
            "constraints": caps,
            "requested_order_cost": str(requested * cost_per_unit),
            "rooms": {k: str(v) for k, v in rooms.items()},
            "risk_manager_approved": decision.approved,
            "risk_manager_reasons": list(decision.reasons),
            "risk_manager_quantity": str(decision.quantity),
            "risk_manager_risk_amount": str(decision.risk_amount),
        }
    return result


def run_entry(derived, labels, settings, config, provider):
    positions = []
    for mode in ("conservative", "optimistic"):
        for name in labels:
            e = derived[name]["entry"]
            positions.append(
                (
                    mode,
                    name,
                    ShadowPosition(
                        e,
                        {"id": name, "exit_policy": NAME, "exit_parameters": {}},
                        settings,
                        config["stale_position_policy"],
                    ),
                )
            )
    cursor, end = ms(derived["CONTROL"]["entry"]["entry_time"]), ms(config["end"])
    while cursor < end and any(not p.result for _, _, p in positions):
        candle = provider.candle(derived["CONTROL"]["entry"]["symbol"], cursor)
        for mode, name, p in positions:
            if p.result:
                continue
            if cursor % 3600000 == 0:
                p.fresh(candle is not None, cursor)
                if candle:
                    p.mark(candle["open"], cursor)
            now = cursor + MINUTE
            p.fresh(candle is not None, now)
            if candle:
                p.bar(candle, now, mode == "conservative")
        cursor += MINUTE
    output = {"conservative": [], "optimistic": []}
    for mode, name, p in positions:
        row = p.export(ms(p.result["exit_time"]) if p.result else end)
        row["initial_r_audit"] = {
            k: v for k, v in derived[name].items() if k != "entry"
        }
        row["anchor_canonical_risk_amount"] = derived["CONTROL"]["risk_amount"]
        row["initial_risk_amount"] = derived[name]["risk_amount"]
        row["canonical_risk_amount"] = derived["CONTROL"]["risk_amount"]
        row["initial_r_fixed_at_entry"] = True
        output[mode].append(row)
    return output


def control_proof(rows, trades, derived):
    result = parity(rows, trades)
    by_id = {t["position_id"]: t for t in trades}
    extra = []
    for row in rows:
        pid = int(row["shadow_entry_id"].split(":")[-1])
        t = by_id[pid]
        for key in ("activation_075r_time", "activation_1r_time", "activation_2r_time"):
            if row[key] != t[key]:
                extra.append({"position_id": pid, "field": key})
        for name, a, b in [
            ("original_stop", row["original_stop"], t["original_stop_price"]),
            ("risk_amount", row["canonical_risk_amount"], t["risk_amount"]),
            (
                "derived_stop",
                derived[pid]["CONTROL"]["stop_price"],
                t["original_stop_price"],
            ),
            ("derived_risk", derived[pid]["CONTROL"]["risk_amount"], t["risk_amount"]),
        ]:
            if D(a) != D(b):
                extra.append({"position_id": pid, "field": name})
    result["mismatches"].extend(extra)
    result["mismatch_count"] += len(extra)
    result["checked_fields"] += [
        "original_stop",
        "risk_amount",
        "derived_stop",
        "derived_risk",
        "activation_075r_time",
        "activation_1r_time",
        "activation_2r_time",
    ]
    result["passed"] = result["passed"] and not extra
    return result


def measured(rows):
    result = metrics(rows)
    closed = [r for r in rows if r["comparison_eligible"]]
    result["exit_stage_counts"] = dict(Counter(r["exit_stage"] for r in closed))
    result["original_stop_pct"] = (
        100 * sum(r["exit_stage"] == "ORIGINAL_STOP" for r in closed) / len(closed)
        if closed
        else None
    )
    for key in ("075r", "1r", "2r"):
        count = sum(bool(r["activation_" + key + "_time"]) for r in rows)
        result["activation_" + key] = {
            "count": count,
            "pct": 100 * count / len(rows) if rows else None,
        }
    return result


def pairwise(control, candidate):
    lookup = {r["shadow_entry_id"]: r for r in candidate}
    pairs = [
        (a, lookup[a["shadow_entry_id"]])
        for a in control
        if a["comparison_eligible"]
        and lookup[a["shadow_entry_id"]]["comparison_eligible"]
    ]
    parts = defaultdict(list)
    matrix = Counter()
    stages = Counter()
    rescue = Counter()
    damage = Counter()
    cash_effect = scale_effect = 0.0
    damage_deltas = defaultdict(list)
    missed075 = missed1 = 0
    for a, b in pairs:
        dr = b["realized_r"] - a["realized_r"]
        ca, cb = category(a), category(b)
        matrix[(ca, cb)] += 1
        stages[(a["exit_stage"], b["exit_stage"])] += 1
        if a["exit_stage"] == "ORIGINAL_STOP" and cb in ("WIN", "FLAT"):
            group = "rescued_original_stops"
            rescue[cb] += 1
        elif ca == "WIN" and cb == "LOSS":
            group = "newly_stopped_control_winners"
        elif ca == "WIN" and dr < -1e-8:
            group = "truncated_winners"
        elif a["exit_stage"] != "ORIGINAL_STOP" and dr > 1e-8:
            group = "improved_existing_exits"
        else:
            group = "other_exit_changes"
        parts[group].append(dr)
        if a["realized_r"] > 0 and dr < -1e-8:
            damage[cb] += 1
            damage_deltas[cb].append(dr)
        ar = D(a.get("initial_risk_amount", a["canonical_risk_amount"]))
        br = D(b.get("initial_risk_amount", b["canonical_risk_amount"]))
        ap = D(a["realized_pnl"])
        bp = D(b["realized_pnl"])
        cash_effect += float((bp - ap) / ar)
        scale_effect += float(bp * (1 / br - 1 / ar))
        missed075 += bool(a["activation_075r_time"]) and not bool(
            b["activation_075r_time"]
        )
        missed1 += bool(a["activation_1r_time"]) and not bool(b["activation_1r_time"])
    delta = math.fsum(b["realized_r"] - a["realized_r"] for a, b in pairs)
    decomposition = {
        k: {"n": len(parts[k]), "delta_r": math.fsum(parts[k])}
        for k in (
            "rescued_original_stops",
            "improved_existing_exits",
            "newly_stopped_control_winners",
            "truncated_winners",
            "other_exit_changes",
        )
    }
    residual = math.fsum(v["delta_r"] for v in decomposition.values()) - delta
    if abs(residual) > 1e-8 or abs(cash_effect + scale_effect - delta) > 1e-8:
        raise ValueError("Decomposition does not reconcile")
    am = measured([a for a, _ in pairs])
    bm = measured([b for _, b in pairs])
    return {
        "comparable_pairs": len(pairs),
        "excluded_pairs": len(control) - len(pairs),
        "delta_total_r": delta,
        "delta_mean_r": delta / len(pairs) if pairs else None,
        "delta_pf_r": bm["pf_r"] - am["pf_r"]
        if bm["pf_r"] is not None and am["pf_r"] is not None
        else None,
        "delta_original_stop_count": bm["exit_stage_counts"].get("ORIGINAL_STOP", 0)
        - am["exit_stage_counts"].get("ORIGINAL_STOP", 0),
        "delta_activation_075r": bm["activation_075r"]["count"]
        - am["activation_075r"]["count"],
        "delta_activation_1r": bm["activation_1r"]["count"]
        - am["activation_1r"]["count"],
        "delta_mean_hold_seconds": bm["mean_hold_seconds"] - am["mean_hold_seconds"]
        if pairs
        else None,
        "delta_transaction_cost": str(
            D(bm["fees"]) + D(bm["slippage"]) - D(am["fees"]) - D(am["slippage"])
        ),
        "transition_by_outcome": [
            {"control": a, "candidate": b, "n": n}
            for (a, b), n in sorted(matrix.items())
        ],
        "transition_by_stage": [
            {"control": a, "candidate": b, "n": n}
            for (a, b), n in sorted(stages.items())
        ],
        "decomposition": decomposition,
        "decomposition_residual": residual,
        "cash_exit_effect_anchor_r": cash_effect,
        "denominator_scaling_effect_r": scale_effect,
        "rescue": dict(rescue),
        "damage_of_positive_control_trades": dict(damage),
        "damage_delta_r_by_outcome": {
            k: math.fsum(v) for k, v in damage_deltas.items()
        },
        "control_activated075_candidate_did_not": missed075,
        "control_activated1_candidate_did_not": missed1,
    }


def compare_subset(control, candidate, ids):
    a = [r for r in control if r["shadow_entry_id"] in ids]
    b = [r for r in candidate if r["shadow_entry_id"] in ids]
    return {
        "n": len(a),
        "control": measured(a),
        "candidate": measured(b),
        "pairwise": pairwise(a, b),
    }


def summarize(rows, audits, records):
    by_label = {name: [r for r in rows if r["policy_id"] == name] for name in LABELS}
    control = by_label["CONTROL"]
    report = {
        "metrics": {name: measured(v) for name, v in by_label.items()},
        "pairwise": {name: pairwise(control, by_label[name]) for name in LABELS[1:]},
    }
    report["initial_r_distributions"] = {}
    report["sizing_audit"] = {}
    for name in LABELS:
        ds = [r["initial_r_audit"] for r in by_label[name]]
        report["initial_r_distributions"][name] = {
            "unit_r_price_pct": stats([d["unit_r_pct"] for d in ds]),
            "stop_distance_ratio": stats([d["distance_ratio"] for d in ds]),
            "target_distance_pct": {
                str(m): stats([d["unit_r_pct"] * m for d in ds]) for m in (0.75, 1, 2)
            },
        }
        aa = [a[name] for a in audits.values()]
        report["sizing_audit"][name] = {
            "quantity_ratio": stats([a["quantity_ratio"] for a in aa]),
            "quantity_increased": sum(a["quantity_ratio"] > 1 + 1e-10 for a in aa),
            "quantity_decreased": sum(a["quantity_ratio"] < 1 - 1e-10 for a in aa),
            "WOULD_HIT_RISK_CAP": sum(a["WOULD_HIT_RISK_CAP"] for a in aa),
            "constraints": dict(Counter(k for a in aa for k in a["constraints"])),
            "risk_manager_rejections": dict(
                Counter(
                    reason
                    for a in aa
                    if not a["risk_manager_approved"]
                    for reason in a["risk_manager_reasons"]
                )
            ),
        }
    report["monthly"] = {}
    report["quarterly"] = {}
    times = {r["shadow_entry_id"]: r["entry_time"] for r in control}
    for mode, groups in [
        ("monthly", [f"2024-{m:02}" for m in range(1, 13)]),
        ("quarterly", [f"Q{i}" for i in range(1, 5)]),
    ]:
        for group in groups:
            ids = {
                key
                for key, t in times.items()
                if t.startswith(group)
                or (
                    mode == "quarterly" and (int(t[5:7]) - 1) // 3 + 1 == int(group[1:])
                )
            }
            report[mode][group] = {
                "metrics": {
                    name: measured([r for r in v if r["shadow_entry_id"] in ids])
                    for name, v in by_label.items()
                },
                "pairwise": {
                    name: pairwise(
                        [r for r in control if r["shadow_entry_id"] in ids],
                        [r for r in by_label[name] if r["shadow_entry_id"] in ids],
                    )
                    for name in LABELS[1:]
                },
            }
    report["monthly_robustness"] = {}
    for name in LABELS[1:]:
        deltas = {
            m: v["pairwise"][name]["delta_total_r"]
            for m, v in report["monthly"].items()
        }
        best = sorted(deltas, key=deltas.get, reverse=True)[:2]
        remaining = {key for key, t in times.items() if t[:7] not in best}
        report["monthly_robustness"][name] = {
            "positive_delta_months": sum(v > 1e-8 for v in deltas.values()),
            "negative_delta_months": sum(v < -1e-8 for v in deltas.values()),
            "delta_r_by_month": deltas,
            "best_two_delta_months": best,
            "without_best_two_delta_months": compare_subset(
                control, by_label[name], remaining
            ),
        }
    report["stop_width_audits"] = {}
    for name in ("A", "B", "D"):
        choices = {
            "narrower": lambda x: x < 1 - 1e-12,
            "wider": lambda x: x > 1 + 1e-12,
            "unchanged": lambda x: abs(x - 1) <= 1e-12,
        }
        if name == "D":
            choices.update(
                {
                    "(1,1.25)": lambda x: 1 + 1e-12 < x < 1.25,
                    "[1.25,1.5)": lambda x: 1.25 <= x < 1.5,
                    "[1.5,2)": lambda x: 1.5 <= x < 2,
                    ">=2": lambda x: x >= 2,
                }
            )
        report["stop_width_audits"][name] = {
            label: compare_subset(
                control,
                by_label[name],
                {
                    r["shadow_entry_id"]
                    for r in by_label[name]
                    if condition(r["initial_r_audit"]["distance_ratio"])
                },
            )
            for label, condition in choices.items()
        }
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    args = p.parse_args()
    root = args.root
    output = root / "artifacts/shadow-dynamic-initial-r-2024"
    output.mkdir(parents=True, exist_ok=True)
    source = ReadOnlyAnchor(root / "data/replay.db")
    with source.connection() as db:
        config, metadata, trades = anchor(db)
        before = database_fingerprint(db)
        fills = {
            f["position_id"]: f
            for r in db.execute(
                "SELECT data FROM replay_fills WHERE run_id=? AND variant=0", (ANCHOR,)
            )
            if (f := json.loads(r[0]))["side"] == "BUY" and f["status"] == "FILLED"
        }
    entries = canonical_entries(source, ANCHOR, 0)
    filled_proof = verify_filled_entries(trades, entries)
    diagroot = root / "artifacts/initial-risk-distance-2024"
    diagpath = diagroot / "initial-risk-distance-diagnostic.detailed.json"
    diagvalidation = json.loads((diagroot / "validation.json").read_text())
    diag_sha = hashlib.sha256(diagpath.read_bytes()).hexdigest()
    if diag_sha != diagvalidation["file_sha256"][diagpath.name]:
        raise ValueError("Diagnostic export changed")
    records = json.loads(diagpath.read_text())["records"]
    by_id = {r["anchor_trade"]["position_id"]: r for r in records}
    if digest([by_id[t["position_id"]]["anchor_trade"] for t in trades]) != digest(
        trades
    ):
        raise ValueError("Diagnostic anchor mismatch")
    settings = settings_for(config["variants"][0])
    derived = {}
    audits = {}
    for e in entries:
        pid = e["anchor_position_id"]
        derived[pid] = derive(e, by_id[pid], fills[pid], settings)
        audits[pid] = sizing_audit(
            e,
            derived[pid],
            fills[pid],
            by_id[pid]["anchor_trade"]["portfolio_at_decision"],
            settings,
        )
    old = root / "artifacts/shadow-early-protection-2024"
    control_saved = json.loads((old / "control-conservative.json").read_text())
    opt_saved = json.loads((old / "all-optimistic.json").read_text())
    expected = {
        "anchor_id": ANCHOR,
        "entry_hash": digest(entries),
        "anchor_fingerprint": before,
        "diagnostic_sha256": diag_sha,
        "definitions": DEFINITIONS,
    }
    for saved in (control_saved, opt_saved):
        if (
            saved["entry_hash"] != digest(entries)
            or saved["anchor_fingerprint"] != before
        ):
            raise ValueError("Cached control anchor changed")
    control = copy.deepcopy(control_saved["outcomes"])
    optimistic_control = copy.deepcopy(
        [r for r in opt_saved["outcomes"] if r["policy_id"] == "A"]
    )
    for rows in (control, optimistic_control):
        for row in rows:
            pid = int(row["shadow_entry_id"].split(":")[-1])
            row["policy_id"] = "CONTROL"
            row["initial_r_audit"] = {
                k: v for k, v in derived[pid]["CONTROL"].items() if k != "entry"
            }
            row["anchor_canonical_risk_amount"] = derived[pid]["CONTROL"]["risk_amount"]
            row["initial_risk_amount"] = derived[pid]["CONTROL"]["risk_amount"]
            row["initial_r_fixed_at_entry"] = True
    proof = control_proof(control, trades, derived)
    write(output / "control-parity.json", proof)
    if not proof["passed"]:
        raise ValueError("Control parity failed; candidates not started")
    print(
        json.dumps(
            {"control_parity": proof["mismatch_count"], "actual_buys": len(entries)}
        ),
        flush=True,
    )
    provider = PinnedMinutes(
        root / "data/replay-cache", metadata["historical_data_revision"]
    )
    for saved in (control_saved, opt_saved):
        provider.used.update(saved["source_revisions"])
        provider.supplemental.update(saved["supplemental_sources"])
    start = time.monotonic()
    primary = list(control)
    optimistic = list(optimistic_control)
    for i, e in enumerate(entries):
        pid = e["anchor_position_id"]
        path = output / f"checkpoints/{pid}.json"
        path.parent.mkdir(exist_ok=True)
        if path.exists():
            saved = json.loads(path.read_text())
            if saved["identity"] != expected:
                raise ValueError("Checkpoint identity changed")
            outcomes = saved["outcomes"]
            provider.used.update(saved["source_revisions"])
            provider.supplemental.update(saved["supplemental_sources"])
        else:
            outcomes = run_entry(derived[pid], LABELS[1:], settings, config, provider)
            write(
                path,
                {
                    "identity": expected,
                    "outcomes": outcomes,
                    "source_revisions": provider.used,
                    "supplemental_sources": provider.supplemental,
                },
            )
        primary.extend(outcomes["conservative"])
        optimistic.extend(outcomes["optimistic"])
        if i % 25 == 0 or i + 1 == len(entries):
            progress = {
                "completed_entries": i + 1,
                "total_entries": len(entries),
                "entry_time": e["entry_time"],
                "elapsed_seconds": time.monotonic() - start,
            }
            write(output / "progress.json", progress)
            print(json.dumps(progress), flush=True)
    provider.db.close()
    with source.connection() as db:
        if database_fingerprint(db) != before:
            raise ValueError("Anchor changed")
    if digest(entries) != expected["entry_hash"]:
        raise ValueError("Canonical entry mutated")
    conservative = summarize(primary, audits, records)
    optimistic_report = summarize(optimistic, audits, records)
    compact = {
        "schema_version": 1,
        "dataset_role": "2024 EXPLORATION",
        **expected,
        "control_parity": proof,
        "canonical_entry_count": 1506,
        "methodology": {
            "scope": "MATCHED ENTRY SHADOW; NOT PORTFOLIO REPLAY",
            "stop_base": "Canonical strategy reference price, same unquantized Decimal arithmetic; ATR/pullback imported from SHA-verified completed diagnostic",
            "risk": "q(canonical_quantity * production loss_per_unit expression), using persisted BUY execution reference; exact CONTROL parity before candidate execution",
            "trailing": "Unchanged AdaptivePeakLock, original ATR14, own initial R fixed at entry; .75R BE,1R .75ATR trail,2R .5ATR trail",
            "execution": "Unchanged ShadowPosition minute OHLC, fee/slippage and conservative/optimistic selection; all eight candidate/mode states share each read-only candle",
            "sizing": "Hypothetical only: original actual risk amount, original snapshot including stale exposure reservation; unchanged RiskManager.evaluate with risk_pct=canonical_risk/equity*100. No resizing or rejected-entry removal in shadow.",
            "no_lookahead": "Imported pre-entry closed-hour feature proof, sequential post-entry closed minutes, no final-price exits or future-coverage triggers",
            "flat": "Existing abs(return_pct)<=1e-6 tolerance; positive-R damage also includes microscopic BE positives",
            "scale_decomposition": "deltaR=(candidatePnL-controlPnL)/controlRisk + candidatePnL*(1/candidateRisk-1/controlRisk)",
            "incomplete": "Only pairwise complete CLOSED outcomes comparable; excluded counts explicit. Independent-trade PnL sums are not portfolio equity.",
            "best_two": "Exclude candidate best two monthly delta-R groups from both sides; selected retrospectively for concentration diagnostic only",
            "optimization": "NONE; only five predeclared formulas",
        },
        "conservative": conservative,
        "optimistic": optimistic_report,
        "intrabar_sensitivity": {
            name: {
                "conservative_delta_r": conservative["pairwise"][name]["delta_total_r"],
                "optimistic_delta_r": optimistic_report["pairwise"][name][
                    "delta_total_r"
                ],
                "positive_only_optimistic": conservative["pairwise"][name][
                    "delta_total_r"
                ]
                <= 0
                < optimistic_report["pairwise"][name]["delta_total_r"],
            }
            for name in LABELS[1:]
        },
    }
    cp = output / "2024-B-shadow-dynamic-initial-r.compact.json"
    dp = output / "2024-B-shadow-dynamic-initial-r.detailed.json"
    write(cp, compact)
    write(
        dp,
        {
            "metadata": compact,
            "canonical_entries": entries,
            "canonical_control_trades": trades,
            "initial_r_and_sizing_audits": {
                str(pid): {
                    "distances": {
                        name: {k: v for k, v in d.items() if k != "entry"}
                        for name, d in derived[pid].items()
                    },
                    "fixed_risk_sizing": audits[pid],
                }
                for pid in derived
            },
            "conservative_outcomes": primary,
            "optimistic_outcomes": optimistic,
            "minute_archive_revisions": provider.used,
            "supplemental_cached_sources": provider.supplemental,
        },
    )
    write(
        output / "validation.json",
        {
            "passed": True,
            "control_parity": proof,
            **filled_proof,
            "anchor_unchanged": True,
            "anchor_fingerprint": before,
            "diagnostic_sha256": diag_sha,
            "entry_hash": digest(entries),
            "production_code_touched": False,
            "portfolio_replays_started": 0,
            "service_restarts": 0,
            "minute_archive_revisions": provider.used,
            "supplemental_cached_sources": provider.supplemental,
            "elapsed_seconds": time.monotonic() - start,
            "exports": {
                path.name: {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in (cp, dp)
            },
        },
    )
    print("Completed all dynamic initial R shadow research", flush=True)


if __name__ == "__main__":
    main()
