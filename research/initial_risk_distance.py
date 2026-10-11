"""Offline, read-only initial distance diagnostics; never executes a trade."""

import argparse
import math
import statistics as st
from pathlib import Path

from crypto_bot.config.settings import Settings
from crypto_bot.indicators.technical import atr
from crypto_bot.replay.clock import at, ms
from crypto_bot.replay.shadow_store import canonical_entries
from crypto_bot.replay.universe import HistoricalUniverse
from research.entry_market_context import (
    ANCHOR,
    HOUR,
    anchor,
    database_fingerprint,
    digest,
    load_histories,
    quantile,
    read_only,
)
from research.shadow_early_protection import PinnedMinutes, ReadOnlyAnchor, write

PERIODS = (14, 24, 48, 72, 120)
HORIZONS = (1, 3, 6, 12, 24, 48)
TARGETS = (0.25, 0.5, 0.75, 1, 2)
METHODOLOGY = {
    "scope": "2024 EXPLORATION; READ_ONLY; no portfolio replay or candidate outcomes",
    "atr": "Existing Wilder ATR implementation, same latest 499 closed hourly candles; period changes smoothing only. ATR percent denominator last closed close.",
    "range": "(maximum high - minimum low)/last closed close *100 over trailing N candles.",
    "true_range": "max(high-low,abs(high-previous close),abs(low-previous close))/previous close *100; one preceding candle required.",
    "pullback": "Fixed 24-candle trailing peak INCLUDING current closed candle high: 100*(peak-high window minus current close)/peak. Sample hourly over last 72/120 candles, zeros retained. No optimization or inferred intrabar order.",
    "normalization": "Actual initial price distance = entry fill minus original stop; pct uses fill. Pullback percent converted to absolute using last closed strategy reference close, identical for actual-distance ratios and candidate D.",
    "r": "Canonical risk_amount/quantity; includes canonical costs; distinct from price stop distance. Recovery targets are original entry fill + target R*canonical risk_amount/quantity, gross price touch, not net executable return.",
    "recovery": "RETROSPECTIVE_DIAGNOSTIC_ONLY. Full 1m bars with open>=exit and close<exit+H. First-hit time is close_time+1ms availability. Missing bars: hit is true if observed; no hit is UNKNOWN unless complete. Full-window rates use complete windows only.",
    "labels": "Independent, overlapping TYPE1(no .25R by24h), TYPE2(.75R by12h), TYPE3(1R by24h); missing coverage yields UNKNOWN where a non-hit is unproven. Research labels are not causal entry judgments.",
    "time": "UTC entry decision month/quarter, never exit month.",
    "candidates": "Distance only: CONTROL=2ATR14,A=2ATR72,B=2ATR120,C=max(CONTROL,1.5ATR72),D=max(CONTROL,pullback P75 72h converted at reference close). No quantity, PnL or outcome recomputation.",
}


def verify_filled_entries(trades, entries):
    by_id = {t["position_id"]: t for t in trades}
    if len(entries) != len(trades) or len(by_id) != len(trades):
        raise ValueError("Canonical entry count mismatch")
    if len({e["anchor_position_id"] for e in entries}) != len(entries):
        raise ValueError("Duplicate canonical filled BUY")
    for e in entries:
        t = by_id[e["anchor_position_id"]]
        for key in (
            "symbol",
            "entry_time",
            "entry_fill_price",
            "quantity",
            "notional",
            "original_stop_price",
            "risk_amount",
        ):
            if e[key] != t[key]:
                raise ValueError("Canonical FILLED BUY mismatch: " + key)
    return {
        "actual_filled_buy_count": len(entries),
        "canonical_entry_hash": digest(entries),
        "canonical_fill_mismatches": 0,
    }


def pre_features(candles, decision, fill, stop):
    candles = [c for c in candles if c.close_time < decision][-499:]
    if len(candles) < 400 or candles[-1].open_time != decision - HOUR:
        raise ValueError("Missing fresh pre-entry history")
    if any(b.open_time - a.open_time != HOUR for a, b in zip(candles, candles[1:])):
        raise ValueError("Noncontiguous pre-entry history")
    ref = candles[-1].close
    distance = fill - stop
    f = {
        "reference_price": ref,
        "current_stop_distance_abs": distance,
        "current_stop_distance_pct": 100 * distance / fill,
        "strategy_reference_stop_distance_abs": ref - stop,
    }
    for p in PERIODS:
        value = atr(candles, p)
        f[f"atr{p}_abs"] = value
        f[f"atr{p}_pct"] = 100 * value / ref
        f[f"stop_over_atr{p}"] = distance / value
    trs = [
        100 * max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close)) / a.close
        for a, b in zip(candles, candles[1:])
    ]
    for n in (24, 48, 72, 120):
        window = candles[-n:]
        f[f"range_{n}h_pct"] = (
            100 * (max(c.high for c in window) - min(c.low for c in window)) / ref
        )
        for label, value in [
            ("median", st.median(trs[-n:])),
            ("mean", st.mean(trs[-n:])),
            ("p75", quantile(trs[-n:], 0.75)),
            ("p90", quantile(trs[-n:], 0.9)),
        ]:
            f[f"tr_{label}_{n}h_pct"] = value
    for n in (72, 120):
        samples = []
        for i in range(len(candles) - n, len(candles)):
            peak = max(c.high for c in candles[i - 23 : i + 1])
            samples.append(100 * (peak - candles[i].close) / peak)
        for label, value in [
            ("median", st.median(samples)),
            ("p75", quantile(samples, 0.75)),
            ("p90", quantile(samples, 0.9)),
            ("max", max(samples)),
        ]:
            f[f"pullback_{label}_{n}h_pct"] = value
    for label, n in [("p75", 72), ("p90", 72), ("p75", 120)]:
        absolute = f[f"pullback_{label}_{n}h_pct"] * ref / 100
        f[f"stop_over_pullback_{label}_{n}h"] = (
            distance / absolute if absolute else None
        )
    control = 2 * f["atr14_abs"]
    distances = {
        "CONTROL": control,
        "A": 2 * f["atr72_abs"],
        "B": 2 * f["atr120_abs"],
        "C": max(control, 1.5 * f["atr72_abs"]),
        "D": max(control, f["pullback_p75_72h_pct"] * ref / 100),
    }
    candidates = {
        k: {
            "abs": v,
            "pct": 100 * v / ref,
            "ratio_to_control": v / control,
            "change_pct": 100 * (v / control - 1),
        }
        for k, v in distances.items()
    }
    return (
        f,
        candidates,
        {
            "first_candle_open": candles[0].open_time,
            "last_candle_close": candles[-1].close_time,
            "decision": decision,
            "count": len(candles),
        },
    )


def recovery(trade, candle_at):
    start = ms(trade["exit_time"])
    unit = float(trade["risk_amount"]) / float(trade["quantity"])
    prices = {str(r): float(trade["entry_fill_price"]) + r * unit for r in TARGETS}
    first = {k: None for k in prices}
    missing = 0
    windows = {}
    for minute in range(48 * 60):
        stamp = start + minute * 60000
        c = candle_at(trade["symbol"], stamp)
        if c is None or c["time"] != stamp or c["close_time"] >= stamp + 60000:
            missing += 1
        else:
            for k, price in prices.items():
                if first[k] is None and c["high"] >= price:
                    first[k] = c["close_time"] + 1
        h = (minute + 1) / 60
        if h in HORIZONS:
            windows[str(int(h))] = {
                "complete": missing == 0,
                "missing_minutes": missing,
                "hits": {
                    k: (True if hit is not None else False if missing == 0 else None)
                    for k, hit in first.items()
                },
                "first_hit_available_at": {
                    k: at(v).isoformat() if v is not None else None
                    for k, v in first.items()
                },
            }
    a = windows["24"]["hits"]["0.25"]
    labels = {
        "TYPE1": None if a is None else not a,
        "TYPE2": windows["12"]["hits"]["0.75"],
        "TYPE3": windows["24"]["hits"]["1"],
    }
    return {
        "scope": "RETROSPECTIVE_DIAGNOSTIC_ONLY",
        "unit_r_abs": unit,
        "target_prices": prices,
        "windows": windows,
        "labels": labels,
    }


def stats(values):
    xs = sorted(x for x in values if x is not None and math.isfinite(x))
    if not xs:
        return {"n": 0, "missing": len(values)}
    result = {
        "n": len(xs),
        "missing": len(values) - len(xs),
        "mean": st.mean(xs),
        "median": st.median(xs),
        "p25": quantile(xs, 0.25),
        "p75": quantile(xs, 0.75),
        "min": xs[0],
        "max": xs[-1],
        "p10": quantile(xs, 0.1),
        "p90": quantile(xs, 0.9),
    }
    result["coefficient_of_variation"] = (
        st.pstdev(xs) / abs(result["mean"]) if result["mean"] else None
    )
    result["relative_iqr"] = (
        (result["p75"] - result["p25"]) / abs(result["median"])
        if result["median"]
        else None
    )
    # Five equal-width descriptive bins, including maximum in final bin.
    width = (xs[-1] - xs[0]) / 5
    result["distribution"] = (
        [
            {
                "lower": xs[0] + i * width,
                "upper": xs[0] + (i + 1) * width,
                "n": sum(
                    xs[0] + i * width <= x and (x < xs[0] + (i + 1) * width or i == 4)
                    for x in xs
                ),
            }
            for i in range(5)
        ]
        if width
        else [{"lower": xs[0], "upper": xs[-1], "n": len(xs)}]
    )
    return result


def features_summary(rows):
    return (
        {k: stats([r["features"][k] for r in rows]) for k in rows[0]["features"]}
        if rows
        else {}
    )


def recover_summary(rows):
    stopped = [r["recovery"] for r in rows if r["recovery"]]
    result = {"n": len(stopped), "labels": {}}
    for name in ("TYPE1", "TYPE2", "TYPE3"):
        vals = [r["labels"][name] for r in stopped]
        known = sum(v is not None for v in vals)
        result["labels"][name] = {
            "count": sum(v is True for v in vals),
            "known": known,
            "unknown": len(vals) - known,
            "pct": 100 * sum(v is True for v in vals) / known if known else None,
        }
    result["type2_type3_overlap"] = sum(
        r["labels"]["TYPE2"] is True and r["labels"]["TYPE3"] is True for r in stopped
    )
    result["windows"] = {}
    for h in HORIZONS:
        windows = [r["windows"][str(h)] for r in stopped]
        complete = [w for w in windows if w["complete"]]
        result["windows"][str(h)] = {
            "complete_windows": len(complete),
            "incomplete_windows": len(windows) - len(complete),
            "targets": {},
        }
        for target in TARGETS:
            k = str(target)
            hits = sum(w["hits"][k] is True for w in complete)
            result["windows"][str(h)]["targets"][k] = {
                "hits_complete": hits,
                "pct_complete": 100 * hits / len(complete) if complete else None,
                "observed_hits_all": sum(w["hits"][k] is True for w in windows),
                "unknown": sum(w["hits"][k] is None for w in windows),
            }
    return result


def summary(rows):
    groups = {
        k: [r for r in rows if r["anchor_trade"]["exit_stage"] == k]
        for k in (
            "ORIGINAL_STOP",
            "BREAK_EVEN_075R",
            "PEAK_TRAIL_1R",
            "TIGHT_PEAK_TRAIL_2R",
        )
    }
    groups["NON_ORIGINAL_STOP"] = [
        r for r in rows if r["anchor_trade"]["exit_stage"] != "ORIGINAL_STOP"
    ]
    cohorts = {}
    edges = (0, 0.25, 0.4, 0.5, 0.6, 0.7, math.inf)
    for lo, hi in zip(edges, edges[1:]):
        sub = [
            r
            for r in groups["ORIGINAL_STOP"]
            if lo <= r["anchor_trade"]["highest_r"] < hi
        ]
        cohorts[f"[{lo},{hi})"] = {"n": len(sub), "features": features_summary(sub)}
    buckets = {}
    for key, edges in [
        ("stop_over_atr72", [0, 1, 1.5, 2, 2.5, 3, math.inf]),
        ("stop_over_pullback_p75_72h", [0, 0.75, 1, 1.25, 1.5, 2, math.inf]),
    ]:
        buckets[key] = {}
        for lo, hi in zip(edges, edges[1:]):
            sub = [
                r
                for r in groups["ORIGINAL_STOP"]
                if r["features"][key] is not None and lo <= r["features"][key] < hi
            ]
            buckets[key][f"[{lo},{hi})"] = recover_summary(sub)
    candidates = {}
    for name in ("CONTROL", "A", "B", "C", "D"):
        values = [r["candidate_distances"][name] for r in rows]
        candidates[name] = {
            k: stats([v[k] for v in values])
            for k in ("abs", "pct", "ratio_to_control", "change_pct")
        }
        candidates[name].update(
            wider=sum(v["ratio_to_control"] > 1 + 1e-12 for v in values),
            narrower=sum(v["ratio_to_control"] < 1 - 1e-12 for v in values),
            equal=sum(abs(v["ratio_to_control"] - 1) <= 1e-12 for v in values),
        )
    return {
        "n": len(rows),
        "features": features_summary(rows),
        "outcomes": {
            k: {"n": len(v), "features": features_summary(v)} for k, v in groups.items()
        },
        "mfe_cohorts": cohorts,
        "recovery": recover_summary(rows),
        "volatility_buckets": buckets,
        "candidate_distances": candidates,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    args = p.parse_args()
    output = args.root / "artifacts/initial-risk-distance-2024"
    output.mkdir(parents=True, exist_ok=True)
    with read_only(args.root / "data/replay.db") as db:
        config, metadata, trades = anchor(db)
        before = database_fingerprint(db)
    filled_proof = verify_filled_entries(
        trades,
        canonical_entries(ReadOnlyAnchor(args.root / "data/replay.db"), ANCHOR, 0),
    )
    selected = dict(config, candidate_symbols=sorted({t["symbol"] for t in trades}))
    histories, hourly_audit = load_histories(
        args.root / "data/replay-cache", selected, metadata
    )
    universe = HistoricalUniverse(
        histories,
        Settings(**config["scanner_settings"]),
        float(config["spread_approximation_pct"]),
    )
    provider = PinnedMinutes(
        args.root / "data/replay-cache", metadata["historical_data_revision"]
    )
    rows = []
    for i, t in enumerate(trades):
        decision = ms(t["scanner_snapshot_timestamp"])
        candles = [c for c, _ in universe.as_of(t["symbol"], decision)]
        f, candidates, proof = pre_features(
            candles,
            decision,
            float(t["entry_fill_price"]),
            float(t["original_stop_price"]),
        )
        if not math.isclose(f["atr14_abs"], t["atr"], rel_tol=1e-12, abs_tol=1e-15):
            raise ValueError("ATR14 parity failed")
        f["canonical_unit_r_abs"] = float(t["risk_amount"]) / float(t["quantity"])
        f["canonical_unit_r_pct"] = (
            100 * f["canonical_unit_r_abs"] / float(t["entry_fill_price"])
        )
        rows.append(
            {
                "anchor_trade": t,
                "features": f,
                "candidate_distances": candidates,
                "pre_entry_proof": proof,
                "recovery": recovery(t, provider.candle)
                if t["exit_stage"] == "ORIGINAL_STOP"
                else None,
            }
        )
        if i % 100 == 0:
            print(f"Diagnostic {i}/{len(trades)}", flush=True)
    provider.db.close()
    with read_only(args.root / "data/replay.db") as db:
        after = database_fingerprint(db)
    if before != after:
        raise ValueError("Anchor database changed")
    validation = {
        **filled_proof,
        "anchor_id": ANCHOR,
        "canonical_count": len(rows),
        "anchor_unchanged": True,
        "anchor_fingerprint": before,
        "canonical_trade_hash": digest(trades),
        "atr14_parity_mismatches": 0,
        "no_lookahead_violations": sum(
            r["pre_entry_proof"]["last_candle_close"]
            >= r["pre_entry_proof"]["decision"]
            for r in rows
        ),
        "hourly_archive_revisions": hourly_audit,
        "minute_archive_revisions": provider.used,
        "supplemental_cached_sources": provider.supplemental,
        "production_code_touched": False,
        "portfolio_replay_started": False,
        "network_used": False,
    }
    result = summary(rows)
    result.update(
        anchor_id=ANCHOR,
        methodology=METHODOLOGY,
        no_lookahead_proof={
            k: validation[k]
            for k in (
                "canonical_count",
                "atr14_parity_mismatches",
                "no_lookahead_violations",
                "anchor_unchanged",
            )
        },
    )
    result["monthly"] = {
        f"2024-{m:02}": summary(
            [
                r
                for r in rows
                if r["anchor_trade"]["scanner_snapshot_timestamp"].startswith(
                    f"2024-{m:02}"
                )
            ]
        )
        for m in range(1, 13)
    }
    result["quarterly"] = {
        f"Q{q}": summary(
            [
                r
                for r in rows
                if (int(r["anchor_trade"]["scanner_snapshot_timestamp"][5:7]) - 1) // 3
                + 1
                == q
            ]
        )
        for q in range(1, 5)
    }
    result["symbol_distance_stability"] = {
        symbol: {
            name: stats(
                [
                    r["candidate_distances"][name]["pct"]
                    for r in rows
                    if r["anchor_trade"]["symbol"] == symbol
                ]
            )
            for name in ("CONTROL", "A", "B", "C", "D")
        }
        for symbol in sorted(histories)
    }
    write(output / "initial-risk-distance-diagnostic.compact.json", result)
    write(
        output / "initial-risk-distance-diagnostic.detailed.json",
        {"anchor_id": ANCHOR, "methodology": METHODOLOGY, "records": rows},
    )
    write(output / "validation.json", validation)
    print("Completed read-only diagnostic", flush=True)


if __name__ == "__main__":
    main()
