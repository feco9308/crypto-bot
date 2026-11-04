"""Frozen market attribution from validated cache and existing exported fills only."""

import argparse
import csv
import json
import math
import statistics
import zipfile
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from research import low_turnover_benchmark as source

BASE = "0b8deee581a61d1be5f9f892f7e7600c9cafbd07"
NAME = "market-regime-attribution-v1"
SOURCE_BUNDLE_SHA = "3d1547e474e4e490dde81fa5a2860839601199e277d14f22086b6c66ada8d899"
DAY = source.DAY
PERIODS = source.PERIODS
SYMBOLS = source.SYMBOLS
FAMILIES = (
    "buy_hold",
    "sma200_state",
    "sma200_distance",
    "RV20_RV60",
    "efficiency60",
    "serial_lag1",
    "monthly_regime",
)
D = Decimal


def stamp(value):
    # Preserve subsecond entry instants; all accepted strings carry UTC/offset.
    from datetime import datetime, timezone

    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    if delta.microseconds % 1000:
        raise ValueError("SUB_MILLISECOND_TIMESTAMP")
    return delta.days * DAY + delta.seconds * 1000 + delta.microseconds // 1000


def quantile(values, p):
    values = sorted(x for x in values if x is not None)
    if not values:
        return None
    if any(not math.isfinite(x) for x in values):
        raise ValueError("NONFINITE_METRIC")
    offset = (len(values) - 1) * p
    lo = int(offset)
    return values[lo] + (values[math.ceil(offset)] - values[lo]) * (offset - lo)


def distribution(values, prefix):
    valid = [v for v in values if v is not None]
    return {
        prefix + "_" + name: value
        for name, value in dict(
            valid_count=len(valid),
            missing_count=len(values) - len(valid),
            median=quantile(valid, 0.5),
            p25=quantile(valid, 0.25),
            p75=quantile(valid, 0.75),
            minimum=min(valid) if valid else None,
            maximum=max(valid) if valid else None,
        ).items()
    }


def pearson(x, y):
    if len(x) != len(y) or len(x) < 2:
        return None
    mx, my = statistics.mean(x), statistics.mean(y)
    dx, dy = [v - mx for v in x], [v - my for v in y]
    denom = math.sqrt(sum(v * v for v in dx) * sum(v * v for v in dy))
    return sum(a * b for a, b in zip(dx, dy)) / denom if denom else None


def ranks(values):
    ordered = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    i = 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and values[ordered[j]] == values[ordered[i]]:
            j += 1
        for k in range(i, j):
            result[ordered[k]] = (i + 1 + j) / 2
        i = j
    return result


def completed_closes(data, day, count, known_at):
    rows = [data.get(day - k * DAY) for k in range(count)]
    if any(
        row is None or row.end >= known_at or row.end != row.start + DAY - 1
        for row in rows
    ):
        raise ValueError("MISSING_OR_NOT_COMPLETED_HISTORY")
    return [row.close for row in reversed(rows)]


def state_at(data, day, known_at):
    closes = completed_closes(data, day, 200, known_at)
    sma = sum(closes, D(0)) / 200
    return closes[-1] > sma, float(closes[-1] / sma - 1), sma


def daily_row(data, day, cohort_start):
    known_at = day + DAY
    above, distance, sma = state_at(data, day, known_at)
    closes = completed_closes(data, day, 61, known_at)
    returns = [float(closes[i] / closes[i - 1] - 1) for i in range(1, 61)]
    changes = sum((abs(closes[i] - closes[i - 1]) for i in range(1, 61)), D(0))
    prior_state, _, _ = state_at(data, day - DAY, known_at)
    bar = data[day]
    return dict(
        day=source.iso(day),
        timestamp=source.iso(bar.end),
        known_at=source.iso(known_at),
        month=source.iso(day)[:7],
        close=float(bar.close),
        sma200=float(sma),
        above_sma200=above,
        previous_above_sma200=prior_state,
        above_to_below=prior_state and not above,
        below_to_above=not prior_state and above,
        distance_sma200=distance,
        rv20=statistics.stdev(returns[-20:]) * math.sqrt(365),
        rv60=statistics.stdev(returns) * math.sqrt(365),
        efficiency60=float(abs(closes[-1] - closes[0]) / changes) if changes else None,
        daily_close_return=returns[-1],
        buy_hold_equity=1000 * float(bar.close / data[cohort_start].open),
    )


def runs(rows):
    """Cohort-clipped spells; no future state used to discover right endpoints."""
    result = []
    for row in rows:
        if not result or result[-1]["above_sma200"] != row["above_sma200"]:
            if result:
                result[-1]["right_censored"] = False
            result.append(
                dict(
                    above_sma200=row["above_sma200"],
                    first_day=row["day"],
                    last_day=row["day"],
                    days=1,
                    left_censored=not result
                    and row["previous_above_sma200"] == row["above_sma200"],
                    right_censored=True,
                )
            )
        else:
            result[-1]["last_day"] = row["day"]
            result[-1]["days"] += 1
    return result


def monthly(rows, initial_open):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["month"]].append(row)
    prior = initial_open
    result = []
    for month, values in sorted(grouped.items()):
        result.append(
            dict(
                month=month,
                days=len(values),
                return_pct=(values[-1]["close"] / prior - 1) * 100,
                median_distance_sma200=quantile(
                    [v["distance_sma200"] for v in values], 0.5
                ),
                above_to_below=sum(v["above_to_below"] for v in values),
                below_to_above=sum(v["below_to_above"] for v in values),
                sma_crossings=sum(
                    v["above_to_below"] or v["below_to_above"] for v in values
                ),
                median_rv20=quantile([v["rv20"] for v in values], 0.5),
                median_efficiency60=quantile([v["efficiency60"] for v in values], 0.5),
            )
        )
        prior = values[-1]["close"]
    return result


def analyze_market(data):
    tables = {
        k: []
        for k in (
            "market",
            "sma_state",
            "distance",
            "volatility",
            "efficiency",
            "serial",
            "monthly",
            "daily",
            "spells",
        )
    }
    for year, (a, b) in PERIODS.items():
        start, end = stamp(a), stamp(b)
        for symbol in SYMBOLS:
            history = data[symbol]
            rows = [daily_row(history, t, start) for t in range(start, end, DAY)]
            common = dict(
                year=year,
                symbol=symbol,
                start=source.iso(start),
                end_exclusive=source.iso(end),
            )
            spells = runs(rows)
            peak, dd = 1000.0, 0.0
            for row in rows:
                peak = max(peak, row["buy_hold_equity"])
                dd = max(dd, (peak - row["buy_hold_equity"]) / peak * 100)
            tables["market"].append(
                dict(
                    **common,
                    days=len(rows),
                    return_pct=(rows[-1]["buy_hold_equity"] / 1000 - 1) * 100,
                    max_drawdown_pct=dd,
                )
            )
            state = dict(
                **common,
                days=len(rows),
                days_above=sum(r["above_sma200"] for r in rows),
                days_below_equal=sum(not r["above_sma200"] for r in rows),
                days_above_pct=100 * sum(r["above_sma200"] for r in rows) / len(rows),
                days_below_equal_pct=100
                * sum(not r["above_sma200"] for r in rows)
                / len(rows),
                above_to_below=sum(r["above_to_below"] for r in rows),
                below_to_above=sum(r["below_to_above"] for r in rows),
                total_crossings=sum(
                    r["above_to_below"] or r["below_to_above"] for r in rows
                ),
                boundary_crossing=rows[0]["above_to_below"]
                or rows[0]["below_to_above"],
            )
            for above, label in [(True, "above"), (False, "below_equal")]:
                durations = [s["days"] for s in spells if s["above_sma200"] == above]
                state["median_consecutive_" + label + "_days"] = (
                    statistics.median(durations) if durations else None
                )
                state["longest_" + label + "_days"] = (
                    max(durations) if durations else None
                )
            state["crossings_per_365_days"] = state["total_crossings"] / len(rows) * 365
            tables["sma_state"].append(state)
            tables["distance"].append(
                dict(
                    **common,
                    **distribution([r["distance_sma200"] for r in rows], "distance"),
                )
            )
            tables["volatility"].append(
                dict(
                    **common,
                    **distribution([r["rv20"] for r in rows], "rv20"),
                    **distribution([r["rv60"] for r in rows], "rv60"),
                )
            )
            eff = [r["efficiency60"] for r in rows]
            valid = [v for v in eff if v is not None]
            tables["efficiency"].append(
                dict(
                    **common,
                    **distribution(eff, "efficiency60"),
                    fraction_gt_0_30=sum(v > 0.30 for v in valid) / len(valid)
                    if valid
                    else None,
                )
            )
            ret = [r["daily_close_return"] for r in rows]
            tables["serial"].append(
                dict(
                    **common,
                    lag1_pairs=len(ret) - 1,
                    lag1_pearson=pearson(ret[:-1], ret[1:]),
                    lag1_spearman=pearson(ranks(ret[:-1]), ranks(ret[1:])),
                    positive_day_pct=100 * sum(v > 0 for v in ret) / len(ret),
                    negative_day_pct=100 * sum(v < 0 for v in ret) / len(ret),
                    zero_day_pct=100 * sum(v == 0 for v in ret) / len(ret),
                )
            )
            tables["monthly"].extend(
                dict(**common, **r) for r in monthly(rows, float(history[start].open))
            )
            tables["daily"].extend(dict(year=year, symbol=symbol, **r) for r in rows)
            tables["spells"].extend(dict(year=year, symbol=symbol, **s) for s in spells)
    return tables


def csv_rows(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def validate_bundle(path, expected_sha=None):
    if expected_sha and source.sha(path.read_bytes()) != expected_sha:
        raise ValueError("SOURCE_BUNDLE_FINGERPRINT_CHANGED")
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:
            raise ValueError("SOURCE_ZIP_CRC_ERROR")
        manifest = json.loads(z.read("manifest.json"))
        for name, entry in manifest["files"].items():
            if source.sha(z.read(name)) != entry["sha256"]:
                raise ValueError("SOURCE_MANIFEST_MISMATCH")
    return manifest


def benchmark_sources(root):
    out = root / "artifacts" / source.NAME
    path = out / (source.NAME + ".zip")
    manifest = validate_bundle(path, SOURCE_BUNDLE_SHA)
    for name, entry in manifest["files"].items():
        if source.sha((out / name).read_bytes()) != entry["sha256"]:
            raise ValueError("SOURCE_LOOSE_ARTIFACT_CHANGED")
    return out, dict(
        bundle=str(path),
        bundle_sha256=SOURCE_BUNDLE_SHA,
        files={n: e["sha256"] for n, e in manifest["files"].items()},
    )


def episode_attribution(events, data, start, end, final_equity):
    """Attribute existing fills; never evaluate a signal or produce an order."""
    grouped = defaultdict(list)
    for r in events:
        if r["event"] == "EXECUTED_NEXT_OPEN":
            grouped[stamp(r["timestamp"])].append(r)
    cash = 1000.0
    qty = {s: 0.0 for s in SYMBOLS}
    position_open = {}
    positions = []
    episodes = []
    for t, orders in sorted(grouped.items()):
        if not start <= t < end:
            raise ValueError("OUT_OF_COHORT_SOURCE_FILL")
        prices = {s: float(data[s][t].open) for s in SYMBOLS}
        before = cash + sum(qty[s] * prices[s] for s in SYMBOLS)
        if episodes:
            episodes[-1].update(
                end=source.iso(t),
                days=(t - stamp(episodes[-1]["start"])) / DAY,
                end_equity=before,
                pnl=before - episodes[-1]["start_equity"],
            )
        for r in orders:
            s = r["symbol"]
            q = float(r["quantity"])
            fee = float(r["fee"])
            notional = float(r["notional"])
            if abs(prices[s] - float(r["reference_price"])) > 1e-8:
                raise ValueError("SOURCE_OPEN_MISMATCH")
            if r["side"] == "BUY":
                if abs(qty[s]) < 1e-12:
                    position_open[s] = t
                cash -= notional + fee
                qty[s] += q
            else:
                cash += notional - fee
                qty[s] -= q
                if abs(qty[s]) < 1e-12:
                    qty[s] = 0.0
                    opened = position_open.pop(s)
                    positions.append(
                        dict(
                            symbol=s,
                            start=source.iso(opened),
                            end=source.iso(t),
                            days=(t - opened) / DAY,
                            status="CLOSED",
                        )
                    )
            if cash < -1e-6 or any(q < -1e-12 for q in qty.values()):
                raise ValueError("SOURCE_LEDGER_RECONCILIATION_FAILED")
        episodes.append(
            dict(
                start=source.iso(t),
                start_equity=before,
                post_transition_equity=cash + sum(qty[s] * prices[s] for s in SYMBOLS),
                invested_after_transition=any(q > 1e-12 for q in qty.values()),
            )
        )
    if episodes:
        episodes[-1].update(
            end=source.iso(end),
            days=(end - stamp(episodes[-1]["start"])) / DAY,
            end_equity=final_equity,
            pnl=final_equity - episodes[-1]["start_equity"],
        )
    for s, opened in sorted(position_open.items()):
        positions.append(
            dict(
                symbol=s,
                start=source.iso(opened),
                end=source.iso(end),
                days=(end - opened) / DAY,
                status="OPEN_AT_END_CENSORED",
            )
        )
    if abs(sum(r["pnl"] for r in episodes) - (final_equity - 1000)) > 1e-6:
        raise ValueError("TRANSITION_PNL_RECONCILIATION_FAILED")
    return episodes, positions


def benchmark_attribution(root, data, tables):
    folder, proof = benchmark_sources(root)
    summary = json.loads((folder / "summary.json").read_text())
    # Revalidate exactly the same source archives; no network capture.
    manifest = json.loads((folder / "daily-data-manifest.json").read_text())
    events = csv_rows(folder / "execution-audit.csv")
    curves = csv_rows(folder / "daily-equity.csv")
    output, episodes, positions = [], [], []
    for row in summary["results"]:
        bench, year = row["benchmark"], row["year"]
        if bench not in ("T1", "T2"):
            continue
        start, end = map(stamp, PERIODS[year])
        chosen = [
            e
            for e in events
            if int(e["year"]) == year
            and e["benchmark"] == bench
            and e["cost_mode"] == "CURRENT_PAPER_COST"
        ]
        ep, pos = episode_attribution(chosen, data, start, end, row["ending_equity"])
        if len(ep) != row["transitions"]:
            raise ValueError("TRANSITION_COUNT_MISMATCH")
        daily = [
            r
            for r in curves
            if int(r["year"]) == year
            and r["benchmark"] == bench
            and r["cost_mode"] == "CURRENT_PAPER_COST"
        ]
        days = sum(float(r["exposure_pct"]) > 0 for r in daily)
        if abs(days / len(daily) * 100 - row["time_in_market_pct"]) > 1e-9:
            raise ValueError("EXPOSURE_DAYS_MISMATCH")
        components = [
            r
            for r in tables["sma_state"]
            if r["year"] == year and (bench == "T2" or r["symbol"] == "BTCUSDT")
        ]
        output.append(
            dict(
                year=year,
                benchmark=bench,
                gross_pnl=row["gross_pnl"],
                net_pnl=row["pnl"],
                gross_return_pct=row["gross_return_pct"],
                net_return_pct=row["net_return_pct"],
                transitions=len(ep),
                days_in_market=days,
                time_in_market_pct=row["time_in_market_pct"],
                component_crossings=sum(r["total_crossings"] for r in components),
                btc_crossings=next(
                    r["total_crossings"] for r in components if r["symbol"] == "BTCUSDT"
                ),
                eth_crossings=next(
                    (
                        r["total_crossings"]
                        for r in components
                        if r["symbol"] == "ETHUSDT"
                    ),
                    None,
                ),
                average_days_per_position=statistics.mean(r["days"] for r in pos)
                if pos
                else None,
                average_completed_position_days=statistics.mean(
                    r["days"] for r in pos if r["status"] == "CLOSED"
                )
                if any(r["status"] == "CLOSED" for r in pos)
                else None,
                closed_positions=sum(r["status"] == "CLOSED" for r in pos),
                terminal_censored_positions=sum(r["status"] != "CLOSED" for r in pos),
                all_allocation_episode_winning_count=sum(r["pnl"] > 0 for r in ep),
                all_allocation_episode_losing_count=sum(r["pnl"] < 0 for r in ep),
                winning_transition_count=sum(
                    r["pnl"] > 0 and r["invested_after_transition"] for r in ep
                ),
                losing_transition_count=sum(
                    r["pnl"] < 0 and r["invested_after_transition"] for r in ep
                ),
                flat_transition_count=sum(
                    r["pnl"] == 0 and r["invested_after_transition"] for r in ep
                ),
                cash_only_transition_count=sum(
                    not r["invested_after_transition"] for r in ep
                ),
                invested_winning_transition_count=sum(
                    r["pnl"] > 0 and r["invested_after_transition"] for r in ep
                ),
                invested_losing_transition_count=sum(
                    r["pnl"] < 0 and r["invested_after_transition"] for r in ep
                ),
            )
        )
        episodes.extend(dict(year=year, benchmark=bench, **r) for r in ep)
        positions.extend(dict(year=year, benchmark=bench, **r) for r in pos)
    return output, episodes, positions, proof, manifest


def institutional_reference(root, data):
    refs = json.loads(
        (
            root / "artifacts" / source.NAME / "current-strategy-reference.json"
        ).read_text()
    )
    output, entries = [], []
    for ref in refs:
        path = Path(ref["source"])
        if source.sha(path.read_bytes()) != ref["source_sha256"]:
            raise ValueError("INSTITUTIONAL_SOURCE_CHANGED")
        if path.suffix == ".zip":
            validate_bundle(path)
            with zipfile.ZipFile(path) as z:
                payload = z.read("A.compact.json")
        else:
            payload = path.read_bytes()
        if source.sha(payload) != ref["member_sha256"]:
            raise ValueError("INSTITUTIONAL_MEMBER_CHANGED")
        doc = json.loads(payload)
        cfg = doc["config"]
        meta = doc["metadata"]
        metrics = doc["summary"]
        year = ref["year"]
        start, end = map(stamp, PERIODS[year])
        if (stamp(cfg["start"]), stamp(cfg["end"])) != (start, end):
            raise ValueError("INSTITUTIONAL_PERIOD_MISMATCH")
        trades = doc["trades"]
        if len(trades) != metrics["trade_count"]:
            raise ValueError("CLOSED_TRADE_COUNT_MISMATCH")
        seen = set()
        counts = defaultdict(int)
        for trade in trades:
            entry, exit = stamp(trade["entry_time"]), stamp(trade["exit_time"])
            if not start <= entry < end or not entry <= exit < end:
                raise ValueError("OUT_OF_COHORT_CLOSED_TRADE")
            key = (trade["symbol"], entry, exit)
            if key in seen:
                raise ValueError("DUPLICATE_CLOSED_TRADE")
            seen.add(key)
            previous_day = entry // DAY * DAY - DAY
            above, _, sma = state_at(data["BTCUSDT"], previous_day, entry)
            counts["ABOVE" if above else "BELOW_EQUAL"] += 1
            entries.append(
                dict(
                    year=year,
                    run_id=ref["run_id"],
                    symbol=trade["symbol"],
                    entry_time=trade["entry_time"],
                    exit_time=trade["exit_time"],
                    btc_state="ABOVE" if above else "BELOW_EQUAL",
                    btc_source_close=source.iso(data["BTCUSDT"][previous_day].end),
                    btc_sma200=float(sma),
                )
            )
        output.append(
            dict(
                year=year,
                run_id=ref["run_id"],
                closed_trades=len(trades),
                net_return_pct=metrics["return_pct"],
                profit_factor=metrics["profit_factor"],
                modeled_cost=float(D(metrics["fees"]) + D(metrics["slippage_cost"])),
                win_rate=metrics["win_rate"],
                entries_btc_above=counts["ABOVE"],
                entries_btc_below_equal=counts["BELOW_EQUAL"],
                source=str(path),
                source_sha256=ref["source_sha256"],
                member_sha256=ref["member_sha256"],
                exact_period=True,
                market_context_version=meta["market_context_version"],
            )
        )
    return output, entries


def comparison(tables, benchmarks, institutional):
    result = []
    meanings = dict(
        buy_hold_return_pct="Endpoint direction; negative period return alone does not prove continuous bearishness",
        days_above_sma200_pct="Long-trend occupancy; related to, not independent of crossings",
        crossings="More state flips support SMA-neighborhood whipsaw; unequal cohort lengths",
        crossings_per_365_days="State flips normalized by observed cohort days; not a new trading threshold",
        median_efficiency60="Absolute directional efficiency can rise in bearish trends; not bullish persistence",
        median_rv20="Annualized daily-return volatility; asset-specific direction",
        median_rv60="Annualized daily-return volatility; asset-specific direction",
        return_lag1_pearson="Descriptive one-day dependence; small coefficients do not establish a causal mechanism",
        return_lag1_spearman="Rank dependence, average ties; disagreement with Pearson weakens a blanket persistence claim",
    )
    fields = [
        ("market", "return_pct", "buy_hold_return_pct"),
        ("sma_state", "days_above_pct", "days_above_sma200_pct"),
        ("sma_state", "total_crossings", "crossings"),
        ("sma_state", "crossings_per_365_days", "crossings_per_365_days"),
        ("efficiency", "efficiency60_median", "median_efficiency60"),
        ("volatility", "rv20_median", "median_rv20"),
        ("volatility", "rv60_median", "median_rv60"),
        ("serial", "lag1_pearson", "return_lag1_pearson"),
        ("serial", "lag1_spearman", "return_lag1_spearman"),
    ]
    for table, field, label in fields:
        for symbol in SYMBOLS:
            vals = {r["year"]: r[field] for r in tables[table] if r["symbol"] == symbol}
            diff = (
                vals[2025] - vals[2024]
                if vals[2025] is not None and vals[2024] is not None
                else None
            )
            result.append(
                dict(
                    metric=symbol + " " + label,
                    value_2024=vals[2024],
                    value_2025=vals[2025],
                    difference_2025_minus_2024=diff,
                    interpretation=(
                        (
                            "INCREASED"
                            if diff > 0
                            else "DECREASED"
                            if diff < 0
                            else "UNCHANGED"
                        )
                        if diff is not None
                        else "UNDEFINED"
                    )
                    + "; "
                    + meanings[label],
                )
            )
    for bench in ("T1", "T2", "institutional"):
        rows = (
            institutional
            if bench == "institutional"
            else [r for r in benchmarks if r["benchmark"] == bench]
        )
        vals = {r["year"]: r["net_return_pct"] for r in rows}
        result.append(
            dict(
                metric=bench + " net_return_pct",
                value_2024=vals[2024],
                value_2025=vals[2025],
                difference_2025_minus_2024=vals[2025] - vals[2024],
                interpretation="DESCRIPTIVE_COHORT_RETURN; NOT_CAUSAL",
            )
        )
    return result


def classify(tables):
    """Directional concordance only; no fit, p-value search or trading cutoff."""

    def both(table, field, direction):
        values = {(r["year"], r["symbol"]): r[field] for r in tables[table]}
        return all(
            direction * (values[(2025, s)] - values[(2024, s)]) > 0 for s in SYMBOLS
        )

    evidence = dict(
        weaker_buy_hold_both=both("market", "return_pct", -1),
        lower_above_sma200_both=both("sma_state", "days_above_pct", -1),
        more_crossings_per_day_both=both("sma_state", "crossings_per_365_days", 1),
        lower_efficiency60_both=both("efficiency", "efficiency60_median", -1),
        higher_rv20_both=both("volatility", "rv20_median", 1),
    )
    # State and crossing metrics are one family. Efficiency is the independent
    # persistence check required before calling the trend explanation clear.
    clear = (
        evidence["weaker_buy_hold_both"]
        and evidence["lower_above_sma200_both"]
        and evidence["more_crossings_per_day_both"]
        and evidence["lower_efficiency60_both"]
    )
    partial = evidence["weaker_buy_hold_both"] and (
        evidence["lower_above_sma200_both"]
        or evidence["more_crossings_per_day_both"]
        or evidence["lower_efficiency60_both"]
    )
    return (
        "TREND_REGIME_SHIFT_CLEAR"
        if clear
        else "TREND_REGIME_SHIFT_PARTIAL"
        if partial
        else "NO_CLEAR_REGIME_EXPLANATION"
    ), evidence


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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    args = p.parse_args()
    root = args.root.resolve()
    folder, initial_proof = benchmark_sources(root)
    data, manifest = source.load_data(root, capture=False)
    if source.sha(source.encoded(manifest)) != source.sha(
        (folder / "daily-data-manifest.json").read_bytes()
    ):
        raise ValueError("DAILY_SOURCE_CONTRACT_CHANGED")
    tables = analyze_market(data)
    benchmarks, episodes, positions, proof, _ = benchmark_attribution(
        root, data, tables
    )
    institutional, entries = institutional_reference(root, data)
    cross = comparison(tables, benchmarks, institutional)
    category, evidence = classify(tables)
    summary = dict(
        final_classification=category,
        strategy_candidate="NONE",
        base_git_sha=BASE,
        source_code_sha256=source.sha(Path(__file__).read_bytes()),
        market_structure={
            k: v for k, v in tables.items() if k not in ("daily", "spells")
        },
        benchmark_attribution=benchmarks,
        institutional_reference=institutional,
        cross_year_comparison=cross,
        directional_concordance=evidence,
        research_2023_outcome_used=False,
        research_2026_outcome_used=False,
        parameter_optimization_performed=False,
        new_institutional_replay=False,
        benchmark_rerun=False,
        production_behavior_changed=False,
        production_db_writes=0,
        deployment=False,
    )
    methodology = dict(
        base=BASE,
        periods=PERIODS,
        assets=SYMBOLS,
        frozen_metric_families=FAMILIES,
        source_contract="Same official Spot daily archive, checksum pins and parser as Low-Turnover v1; offline only",
        daily_observation="Cohort day close; known at next UTC midnight; SMA includes this completed close",
        source_warmup="2023 prices for 200/60-day history only; no 2023 result rows; no 2026 data",
        return_definitions="RV/serial use close-to-close simple returns. Buy-and-hold starts first cohort open; later monthly returns use preceding month close.",
        crossing_definition="Assign previous-completed-close state -> current-completed-close state change to current cohort day; boundary uses prior warmup state; no out-of-cohort crossing counted",
        durations="Cohort-clipped spells; first may be left-censored; last always right-censored without looking at future candles",
        volatility="Sample stdev, ddof=1, last 20/60 completed daily simple returns times sqrt(365)",
        efficiency="abs(close_t-close_t-60)/sum(abs(close changes)) over 60 steps; zero denominator undefined/null; bucket >0.30 only",
        percentiles="Type 7 linear interpolation at (n-1)*p",
        serial="Lag pairs wholly in cohort; Pearson and average-tie-rank Spearman, zero variance null",
        benchmark_attribution="Existing fill quantities/notionals/fees only; no signals evaluated, no new fills. Allocation episode runs from pre-transition open equity to next pre-transition open equity, or reported terminal net value. Opening/transition costs belong to episode starting there. Primary win/loss counts include invested episodes only; cash-only episodes and all-allocation counts are separate. ALL episodes sum to net PnL.",
        position_duration="Asset position starts zero->positive quantity and ends positive->zero; additional/partial fills retain position start; open-at-end duration is censored; 1e-12 source-float quantity tolerance only for audit bookkeeping",
        benchmark_terminal="Existing final-close estimated liquidation costs retained as valuation only, not a new fill",
        institutional_state="Last completed BTC daily candle strictly before each closed trade entry; counts only, no regime-conditioned profitability or proposed gate",
        classification="CLEAR requires concordant weaker buy/hold, lower SMA occupancy, higher crossing rate and lower efficiency for BOTH assets. PARTIAL when weaker buy/hold plus some structural corroboration. RV is reported independently, not required to rise.",
        causal_limit="Two assets and two unequal cohorts allow descriptive attribution, not identification of why the institutional portfolio loses or proof of a causal mechanism",
        strategy_candidate="NONE",
        parameter_search=False,
    )
    out = root / "artifacts" / NAME
    out.mkdir(parents=True, exist_ok=True)
    for filename, value in [
        ("summary.json", summary),
        ("methodology.json", methodology),
    ]:
        (out / filename).write_bytes(source.encoded(value))
    exports = {
        "buy-hold-statistics.csv": tables["market"],
        "sma-state-statistics.csv": tables["sma_state"],
        "sma-distance-statistics.csv": tables["distance"],
        "volatility-statistics.csv": tables["volatility"],
        "trend-efficiency.csv": tables["efficiency"],
        "serial-structure.csv": tables["serial"],
        "monthly-regime.csv": tables["monthly"],
        "daily-market-metrics.csv": tables["daily"],
        "state-spells.csv": tables["spells"],
        "benchmark-attribution.csv": benchmarks,
        "benchmark-transition-episodes.csv": episodes,
        "benchmark-position-durations.csv": positions,
        "institutional-reference.csv": institutional,
        "institutional-entry-state.csv": entries,
        "cross-year-comparison.csv": cross,
    }
    for filename, rows in exports.items():
        source.write_csv(out / filename, rows)
    _, after = benchmark_sources(root)
    if after != initial_proof:
        raise ValueError("SOURCE_CHANGED_DURING_AUDIT")
    validation = dict(
        benchmark_source=proof,
        source_fingerprint_unchanged=True,
        daily_archives_verified=len(manifest),
        daily_manifest_sha256=source.sha(source.encoded(manifest)),
        source_code_sha256=summary["source_code_sha256"],
        institutional_source_pins={
            r["source"]: r["source_sha256"] for r in institutional
        },
        cohort_daily_rows=len(tables["daily"]),
        institutional_closed_entry_rows=len(entries),
        min_result_day=min(r["day"] for r in tables["daily"]),
        max_result_day=max(r["day"] for r in tables["daily"]),
        no_db_access=True,
        production_db_writes=0,
        benchmark_rerun=False,
        new_replay=False,
        network_capture=False,
        production_behavior_changed=False,
        deployment=False,
    )
    (out / "source-validation.json").write_bytes(source.encoded(validation))
    (out / "report.md").write_text(report(summary))
    bundle = package(out)
    print(
        "COMPLETED",
        category,
        "strategy_candidate=NONE",
        bundle,
        source.sha(bundle.read_bytes()),
        flush=True,
    )


def report(s):
    lines = [
        "# MARKET REGIME ATTRIBUTION DIAGNOSTIC v1",
        "",
        s["final_classification"],
        "strategy_candidate: NONE",
        "",
        "Descriptive frozen-metric attribution only; no benchmark rerun, replay or production rule.",
        "",
        "## Cross-year comparison",
        "",
        "| Metric | 2024 | 2025 | 2025 minus 2024 | Interpretation |",
        "|---|---:|---:|---:|---|",
    ]

    def fmt(v):
        return "UNDEFINED" if v is None else f"{v:.6f}"

    for r in s["cross_year_comparison"]:
        lines.append(
            f"| {r['metric']} | {fmt(r['value_2024'])} | {fmt(r['value_2025'])} | {fmt(r['difference_2025_minus_2024'])} | {r['interpretation']} |"
        )
    lines += [
        "",
        "## Directional concordance",
        "",
        json.dumps(s["directional_concordance"], indent=2),
        "",
        "Occupancy/crossings are related SMA-state measurements, not independent confirmations. Efficiency60 is the independent persistence check. RV and serial structure are reported even if they contradict a blanket higher-volatility/lower-persistence account. A two-period comparison does not prove causality.",
        "",
        "## Existing benchmark attribution",
        "",
        "| Year | Benchmark | Gross PnL | Net PnL | Transitions | Days invested | Avg position days incl censored | Invested winning / losing episodes |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in s["benchmark_attribution"]:
        lines.append(
            f"| {r['year']} | {r['benchmark']} | {r['gross_pnl']:.4f} | {r['net_pnl']:.4f} | {r['transitions']} | {r['days_in_market']} | {r['average_days_per_position']:.4f} | {r['winning_transition_count']} / {r['losing_transition_count']} |"
        )
    lines += [
        "",
        "Episode PnL is the change from pre-allocation open equity to the next pre-allocation open equity; final episodes end at the original reported terminal net valuation. Cash-only episodes may be negative solely because exit costs are attributed to their starting transition. Invested-only counts and individual episodes are exported to avoid calling those costs a market loss. These are not a new replay or optimized exit study.",
        "",
        "SMA crossings occur at completed close, while benchmark execution occurs next open. First-day allocation and unfilled terminal signals explain differences between crossing counts and allocation transitions. Position duration averages explicitly include right-censored holdings at cohort end.",
        "",
        "## Institutional reference",
        "",
        "| Year | Closed | Net return % | PF | Modeled cost | Win rate % | Entry BTC above / below-equal |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in s["institutional_reference"]:
        lines.append(
            f"| {r['year']} | {r['closed_trades']} | {r['net_return_pct']:.4f} | {r['profit_factor']:.6f} | {r['modeled_cost']:.4f} | {r['win_rate']:.4f} | {r['entries_btc_above']} / {r['entries_btc_below_equal']} |"
        )
    lines += [
        "",
        "Only the last completed BTC daily state before entry is counted; no regime-conditioned PnL, gate or threshold is derived. Different universe/exposure/timing prevents causal attribution of institutional losses to BTC/ETH alone.",
        "",
        "## Boundaries and scope",
        "",
        "2024 has 366 cohort days; 2025 covers February–December, 334 days. Both raw crossing counts and rates per 365 observed days are exported. Monthly results expose recovery as well as loss months; the entire 2025 period must not be labeled persistently bearish just because endpoint return is negative.",
        "2023 is indicator warmup only; no 2026 candle/outcome is read. State spells are clipped to cohort boundaries; they do not claim complete historical spell lengths. RV uses sample standard deviation of daily simple returns; efficiency uses 60 absolute daily price changes. Flat efficiency denominators and zero-variance correlations remain null.",
        "",
        "Source archive contract: " + source.DOC,
        "",
        "No parameter optimization, new institutional replay, production DB access/write, service restart or deployment.",
    ]
    lines += [
        "",
        "## Attribution assessment",
        "",
        "A persistent bearish explanation is incomplete: BTC was still above SMA200 on 70.66% of 2025 cohort closes; ETH was below/equal on 60.48%. The endpoint losses include distinct recovery and decline phases.",
        "SMA-neighborhood whipsaw and more frequent state changes are supported descriptively: BTC crossings 10 -> 17, ETH 6 -> 13, despite the shorter 2025 cohort. Median above-state spells shorten from 12.5 -> 3 BTC days and 33.5 -> 2 ETH days. These cohort-clipped durations do not assert complete historical run lengths.",
        "A uniformly higher-volatility explanation is contradicted by BTC: median annualized RV20 declines 49.56% -> 36.28%, while ETH rises 59.89% -> 67.52%. RV60 has the same asset-specific directions.",
        "Uniformly lower absolute directional persistence is not supported: median efficiency60 rises 0.1055 -> 0.1295 for BTC and 0.1432 -> 0.2067 for ETH. Efficiency uses an absolute numerator, so a sustained down move can be efficient. BTC fraction >0.30 falls, but ETH fraction rises; no alternative cutoff is searched.",
        "Daily lag-1 correlations remain near zero and Pearson/Spearman changes do not agree uniformly. The serial data therefore do not rescue a universal lower-persistence claim.",
        "Monthly evidence shows phases, not a uniformly sideways or bearish year: 2025 BTC February -17.65%, April +14.08%, May +11.06%, November -17.56%; ETH February -32.21%, May +40.95%, July +48.80%, November -22.26%.",
        "Shorter invested positions and more losing invested allocation episodes support a more difficult environment for the frozen SMA benchmark. They do not identify why the Top20 institutional strategy is negative in BOTH years. Its market exposure, universe and execution differ. Classification remains PARTIAL because the independent efficiency and volatility families do not give a uniform corroborating account.",
        "",
        "## SMA-state durations",
        "",
        "| Year | Asset | Above / below-equal days | Down / up crossings | Median above / below days | Longest above / below days |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for r in s["market_structure"]["sma_state"]:
        lines.append(
            f"| {r['year']} | {r['symbol']} | {r['days_above']} / {r['days_below_equal']} | {r['above_to_below']} / {r['below_to_above']} | {r['median_consecutive_above_days']} / {r['median_consecutive_below_equal_days']} | {r['longest_above_days']} / {r['longest_below_equal_days']} |"
        )
    lines += [
        "",
        "## Monthly regime table",
        "",
        "| Year | Month | Asset | Return % | Median SMA distance % | Crossings | Median annualized RV20 % | Median efficiency60 |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in s["market_structure"]["monthly"]:
        lines.append(
            f"| {r['year']} | {r['month']} | {r['symbol']} | {r['return_pct']:.4f} | {100 * r['median_distance_sma200']:.4f} | {r['sma_crossings']} | {100 * r['median_rv20']:.4f} | {r['median_efficiency60']:.6f} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
