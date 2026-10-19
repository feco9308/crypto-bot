"""Pinned, offline, read-only market-structure diagnostic; never a replay.

python -m research.cross_sectional_market_structure --root /home/ndvi/crypto-bot
"""

import argparse
import csv
import hashlib
import itertools
import json
import math
import statistics as st
import sys
import zipfile
from array import array
from bisect import bisect_left
from collections import Counter, defaultdict
from pathlib import Path

from research import institutional_signal_edge as edge

BASE = "74ae2e80a5a0c47c0bdf2e7c4ef902b69ada0917"
NAME = "cross-sectional-market-structure-v1"
HOUR = edge.HOUR
HORIZONS = (24, 72, 168)
STRENGTH = (
    "cs_rank_return_4h_pct",
    "cs_rank_return_24h_pct",
    "cs_excess_vs_top20_median_4h",
    "cs_excess_vs_top20_median_24h",
    "btc_excess_return_4h",
    "btc_excess_return_24h",
)
BREADTH = (
    "breadth_positive_4h",
    "breadth_positive_24h",
    "breadth_above_ema50",
    "breadth_bullish_ema",
    "top20_median_return_4h",
    "top20_median_return_24h",
    "top20_return_dispersion_24h",
)
CONTROLS = ("market_score", "delta_4h", "relative_volume")
FEATURES = STRENGTH + BREADTH + CONTROLS
LABEL_PREFIX = "POST_HOC_FUTURE_LABEL_1H."
OUTCOMES = tuple(
    LABEL_PREFIX + f"{measure}_{h}h"
    for h in HORIZONS
    for measure in (
        "close_return_pct",
        "max_high_return_pct",
        "min_low_return_pct",
        "mfe_atr",
        "mae_atr",
    )
)
PRIMARY_STATS = tuple(
    LABEL_PREFIX + f"close_return_pct_{h}h" for h in HORIZONS
) + tuple(LABEL_PREFIX + f"mfe_atr_{h}h" for h in (72, 168))
SECONDARY = tuple(
    "POST_HOC_FUTURE_LABEL." + key
    for key in (
        "hit_1r_before_stop",
        "hit_2r_before_stop",
        "hit_5r_before_stop",
        "stop_before_1r",
    )
)
NAN = float("nan")
MIN_N, MIN_CLIFF, MIN_RHO = 30, 0.147, 0.05


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else NAN
    except (TypeError, ValueError):
        return NAN


def bool_number(value):
    if value not in {"True", "False", ""}:
        raise ValueError("Invalid secondary boolean")
    return NAN if value == "" else float(value == "True")


def percentile(values):
    """Average ties, (rank-1)/(n-1); a sole valid observation ranks .5."""
    valid = [i for i, v in enumerate(values) if math.isfinite(v)]
    result = [NAN] * len(values)
    ranked = edge.ranks([values[i] for i in valid])
    for i, r in zip(valid, ranked):
        result[i] = (r - 1) / (len(valid) - 1) if len(valid) > 1 else 0.5
    return result


def deciles(values):
    """Equal values stay together. Always report all ten bins, including empty."""
    return [
        min(9, int(p * 10)) + 1 if math.isfinite(p) else 0 for p in percentile(values)
    ]


def return_feature(history, timestamp, hours):
    """Exactly h completed hours between closes; never accept stale/gapped bars."""
    opens, closes, *prices = history.columns
    index = bisect_left(opens, timestamp) - 1
    start = index - hours
    if start < 0 or opens[index] != timestamp - HOUR:
        return NAN, None
    if any(
        opens[j] != timestamp - (index - j + 1) * HOUR
        or not opens[j] <= closes[j] < opens[j] + HOUR
        for j in range(start, index + 1)
    ):
        return NAN, None
    close_prices = prices[3]
    return (close_prices[index] / close_prices[start] - 1) * 100, edge.iso(
        closes[index]
    )


def primary_labels(history, timestamp, end, reference, atr):
    """A separate POST_HOC step. Never feeds feature construction.

    Windows [decision, decision+h): h consecutive hourly bars, all closed strictly
    before the window end. An incomplete window has NO partial outcome values.
    MFE/MAE retain signed extrema exactly as the requested formula specifies.
    """
    result = {}
    opens, closes, _, highs, lows, prices, *_ = history.columns
    index = bisect_left(opens, timestamp)
    for hours in HORIZONS:
        stop = timestamp + hours * HOUR
        status = "COMPLETE"
        if stop > end:
            status = "RUN_END_CENSORED"
        elif not math.isfinite(reference) or reference <= 0:
            status = "INVALID_REFERENCE"
        elif index + hours > len(opens) or any(
            opens[index + k] != timestamp + k * HOUR
            or not opens[index + k] <= closes[index + k] < opens[index + k] + HOUR
            for k in range(hours)
        ):
            status = "MISSING_HOUR_CENSORED"
        result[LABEL_PREFIX + f"status_{hours}h"] = status
        for measure in (
            "close_return_pct",
            "max_high_return_pct",
            "min_low_return_pct",
            "mfe_atr",
            "mae_atr",
        ):
            result[LABEL_PREFIX + f"{measure}_{hours}h"] = NAN
        if status != "COMPLETE":
            continue
        hi = max(highs[index : index + hours])
        lo = min(lows[index : index + hours])
        result[LABEL_PREFIX + f"close_return_pct_{hours}h"] = (
            prices[index + hours - 1] / reference - 1
        ) * 100
        result[LABEL_PREFIX + f"max_high_return_pct_{hours}h"] = (
            hi / reference - 1
        ) * 100
        result[LABEL_PREFIX + f"min_low_return_pct_{hours}h"] = (
            lo / reference - 1
        ) * 100
        if math.isfinite(atr) and atr > 0:
            result[LABEL_PREFIX + f"mfe_atr_{hours}h"] = (hi - reference) / atr
            result[LABEL_PREFIX + f"mae_atr_{hours}h"] = (lo - reference) / atr
    return result


class Table:
    """Columnar arrays keep a full annual population under the research RAM budget."""

    def __init__(self):
        self.numeric = {}
        self.text = {}
        self.size = 0

    def add(self, row):
        if not self.size:
            self.numeric = {
                k: array("d") for k, v in row.items() if not isinstance(v, str)
            }
            self.text = {k: [] for k, v in row.items() if isinstance(v, str)}
        if set(row) != self.numeric.keys() | self.text.keys():
            raise ValueError("Inconsistent diagnostic schema")
        for k, col in self.numeric.items():
            col.append(row[k])
        for k, col in self.text.items():
            col.append(row[k])
        self.size += 1

    def write(self, path):
        fields = sorted(self.numeric.keys() | self.text.keys())
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for i in range(self.size):
                row = {k: col[i] for k, col in self.text.items()}
                row.update(
                    {
                        k: col[i] if math.isfinite(col[i]) else ""
                        for k, col in self.numeric.items()
                    }
                )
                writer.writerow(row)


def cohort_features(table, expected=20):
    """Only exact canonical shared Top20 members, never held-only companions."""
    groups = defaultdict(list)
    for i, t in enumerate(table.numeric["timestamp_ms"]):
        groups[int(t)].append(i)
    for feature in STRENGTH[:4] + BREADTH:
        table.numeric[feature] = array("d", [NAN]) * table.size
    market = Table()
    for t, indices in sorted(groups.items()):
        if (
            len(indices) != expected
            or len({table.text["symbol"][i] for i in indices}) != expected
        ):
            raise ValueError("Exact unique canonical Top20 membership required")
        if sorted(table.numeric["top20_rank"][i] for i in indices) != list(
            range(1, expected + 1)
        ):
            raise ValueError("Invalid canonical universe ranks")
        context = {}
        for h in (4, 24):
            values = [table.numeric[f"ret_{h}h"][i] for i in indices]
            # A partial cohort never masquerades as Top20 breadth.
            median = st.median(values) if all(map(math.isfinite, values)) else NAN
            context[f"top20_median_return_{h}h"] = median
            context[f"breadth_positive_{h}h"] = (
                sum(v > 0 for v in values) / expected if math.isfinite(median) else NAN
            )
            ranks = percentile(values) if math.isfinite(median) else [NAN] * expected
            for i, v, rank in zip(indices, values, ranks):
                table.numeric[f"cs_rank_return_{h}h_pct"][i] = rank
                table.numeric[f"cs_excess_vs_top20_median_{h}h"][i] = v - median
            if h == 24:
                context["top20_return_dispersion_24h"] = (
                    st.pstdev(values) if math.isfinite(median) else NAN
                )
        for name, a, b in (
            ("breadth_above_ema50", "reference", "ema50"),
            ("breadth_bullish_ema", "ema50", "ema200"),
        ):
            pairs = [(table.numeric[a][i], table.numeric[b][i]) for i in indices]
            context[name] = (
                sum(a > b for a, b in pairs) / expected
                if all(math.isfinite(a) and math.isfinite(b) for a, b in pairs)
                else NAN
            )
        for k, v in context.items():
            for i in indices:
                table.numeric[k][i] = v
        row = dict(
            timestamp=edge.iso(t),
            timestamp_ms=t,
            market_day_id=edge.iso(t)[:10],
            **context,
        )
        # Fixed membership at decision, not future winners or future Top20 membership.
        for outcome in OUTCOMES:
            values = [table.numeric[outcome][i] for i in indices]
            row[outcome] = st.median(values) if all(map(math.isfinite, values)) else NAN
            row[outcome + "_member_coverage"] = (
                sum(map(math.isfinite, values)) / expected
            )
        market.add(row)
    return market


def csv_rows(path):
    with path.open(newline="") as stream:
        yield from csv.DictReader(stream)


def build_year(year, source, provider, prior, out):
    start, end = edge.ms(source["config"]["start"]), edge.ms(source["config"]["end"])
    btc = provider.history("BTCUSDT", "1h", start - 25 * HOUR, end)
    table = Table()
    seen_symbols, seen_keys, seen_days = set(), set(), set()
    path = prior / f"top20-opportunity-dataset-{year}.csv"
    for symbol, rows in itertools.groupby(csv_rows(path), lambda r: r["symbol"]):
        if symbol in seen_symbols:
            raise ValueError("Expected deterministic symbol-grouped prior dataset")
        seen_symbols.add(symbol)
        history = provider.history(symbol, "1h", start - 25 * HOUR, end)
        last_t = None
        for raw in rows:
            t = edge.ms(raw["timestamp"])
            if not start <= t < end or (last_t is not None and t <= last_t):
                raise ValueError("Unexpected decision cohort/order")
            last_t = t
            key = (symbol, t)
            if key in seen_keys:
                raise ValueError("Duplicate canonical observation")
            seen_keys.add(key)
            if (
                raw["row_scope"] != "TOP20_SHARED_ONLY"
                or raw["run_id"] != source["run_id"]
            ):
                raise ValueError("Held-only or foreign run contamination")
            if edge.ms(raw["decision_source_timestamp"]) >= t:
                raise ValueError("Future decision source")
            reference, atr = number(raw["proposed_entry_reference"]), number(raw["atr"])
            context = json.loads(raw["institutional_context"])
            trend = context["coin_trends"]["1h"]
            if trend["source_timestamp"] and edge.ms(trend["source_timestamp"]) >= t:
                raise ValueError("Future EMA source")
            day_id = raw["symbol_day_id"]
            row = dict(
                year=year,
                run_id=source["run_id"],
                symbol=symbol,
                timestamp=raw["timestamp"],
                timestamp_ms=t,
                top20_rank=number(raw["top20_rank"]),
                symbol_day_id=day_id,
                symbol_day_first=float(day_id not in seen_days),
                eligibility_episode_id=raw["eligibility_episode_id"],
                setup_episode_first=float(raw["eligibility_episode_first"] == "True"),
                current_buy_eligible=float(raw["final_asset_eligible"] == "True"),
                reference=reference,
                decision_time_atr=atr,
                feature_source_timestamp=raw["decision_source_timestamp"],
                ema50=number(trend["ema_fast"]),
                ema200=number(trend["ema_slow"]),
                **{k: number(raw[k]) for k in CONTROLS},
                **{k: bool_number(raw[k]) for k in SECONDARY},
            )
            seen_days.add(day_id)
            for h in (4, 24):
                ret, stamp = return_feature(history, t, h)
                bret, bstamp = return_feature(btc, t, h)
                row[f"ret_{h}h"] = ret
                row[f"return_{h}h_source_timestamp"] = stamp or ""
                row[f"btc_return_{h}h_source_timestamp"] = bstamp or ""
                row[f"btc_excess_return_{h}h"] = ret - bret
            # Features finalized before any forward bars are queried.
            row.update(primary_labels(history, t, end, reference, atr))
            table.add(row)
        if len(seen_symbols) % 50 == 0:
            print(year, "symbols", len(seen_symbols), "rows", table.size, flush=True)
    if table.size != (end - start) // HOUR * 20:
        raise ValueError("Canonical full Top20 population missing")
    market = cohort_features(table)
    table.write(out / f"market-structure-dataset-{year}.csv")
    market.write(out / f"market-level-breadth-{year}.csv")
    return table, market


def write_rows(path, rows):
    keys = sorted({k for row in rows for k in row})
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def scopes(table, market=False):
    all_rows = list(range(table.size))
    if market:
        seen, daily = set(), []
        for i in all_rows:
            day = table.text["market_day_id"][i]
            if day not in seen:
                daily.append(i)
                seen.add(day)
        return {"ONE_ROW_PER_TIMESTAMP": all_rows, "ONE_MARKET_ROW_PER_DAY": daily}
    eligible = table.numeric["current_buy_eligible"]
    first = table.numeric["symbol_day_first"]
    ep = table.numeric["setup_episode_first"]
    return {
        "ALL_TOP20": all_rows,
        "BUY_ELIGIBLE": [i for i in all_rows if eligible[i]],
        "SYMBOL_DAY_FIRST": [i for i in all_rows if first[i]],
        "BUY_SYMBOL_DAY_FIRST": [i for i in all_rows if first[i] and eligible[i]],
        "BUY_SETUP_EPISODE_FIRST": [i for i in all_rows if eligible[i] and ep[i]],
    }


def median(values):
    return st.median(values) if values else None


def curve_summary(points):
    pairs = [(i + 1, v) for i, v in enumerate(points) if v is not None]
    increases = sum(b[1] > a[1] for a, b in zip(pairs, pairs[1:]))
    decreases = sum(b[1] < a[1] for a, b in zip(pairs, pairs[1:]))
    rho = edge.spearman([i for i, v in pairs], [v for i, v in pairs])
    # Descriptive, tolerant: >=7/9 concordant steps and |rho|>=.8;
    # scaled to observed steps, with at least 5 nonempty deciles.
    monotonic = (
        len(pairs) >= 5
        and rho is not None
        and abs(rho) >= 0.8
        and max(increases, decreases) >= math.ceil(0.75 * (len(pairs) - 1))
    )
    return dict(
        decile_spearman=rho,
        increasing_steps=increases,
        decreasing_steps=decreases,
        nonempty_deciles=len(pairs),
        monotonic=monotonic,
    )


def describe(table, year, market=False):
    statistics, bins, secondary = [], [], []
    for scope, indices in scopes(table, market).items():
        print(year, "analysis", scope, len(indices), flush=True)
        for feature in BREADTH if market else FEATURES:
            good = [i for i in indices if math.isfinite(table.numeric[feature][i])]
            x = [table.numeric[feature][i] for i in good]
            ranks = percentile(x)
            ds = deciles(x)
            grouped = defaultdict(list)
            top, bottom = [], []
            for i, p, d in zip(good, ranks, ds):
                grouped[d].append(i)
                if p >= 0.8:
                    top.append(i)
                if p <= 0.2:
                    bottom.append(i)
            decile_records = []
            for d in range(1, 11):
                row = dict(
                    year=year,
                    population=scope,
                    feature=feature,
                    decile=d,
                    count=len(grouped[d]),
                )
                for outcome in OUTCOMES:
                    vals = [
                        table.numeric[outcome][i]
                        for i in grouped[d]
                        if math.isfinite(table.numeric[outcome][i])
                    ]
                    row["median_" + outcome] = median(vals)
                    row["observed_" + outcome] = len(vals)
                bins.append(row)
                decile_records.append(row)
            for outcome in PRIMARY_STATS:
                pairs = [
                    (v, table.numeric[outcome][i])
                    for v, i in zip(x, good)
                    if math.isfinite(table.numeric[outcome][i])
                ]
                a = [
                    table.numeric[outcome][i]
                    for i in top
                    if math.isfinite(table.numeric[outcome][i])
                ]
                b = [
                    table.numeric[outcome][i]
                    for i in bottom
                    if math.isfinite(table.numeric[outcome][i])
                ]
                auc = edge.auc(a, b)
                statistics.append(
                    dict(
                        year=year,
                        population=scope,
                        feature=feature,
                        outcome=outcome,
                        population_count=len(indices),
                        feature_count=len(good),
                        paired_count=len(pairs),
                        outcome_coverage=len(pairs) / len(indices) if indices else None,
                        spearman=edge.spearman(
                            [v for v, y in pairs], [y for v, y in pairs]
                        ),
                        top_quintile_observed=len(a),
                        bottom_quintile_observed=len(b),
                        top_quintile_median=median(a),
                        bottom_quintile_median=median(b),
                        median_difference=st.median(a) - st.median(b)
                        if a and b
                        else None,
                        cliffs_delta=2 * auc - 1 if auc is not None else None,
                        **curve_summary(
                            [r["median_" + outcome] for r in decile_records]
                        ),
                    )
                )
            if not market:
                for label in SECONDARY:
                    positive = [
                        v for v, i in zip(x, good) if table.numeric[label][i] == 1
                    ]
                    negative = [
                        v for v, i in zip(x, good) if table.numeric[label][i] == 0
                    ]
                    a = edge.auc(positive, negative)
                    secondary.append(
                        dict(
                            year=year,
                            population=scope,
                            feature=feature,
                            outcome=label,
                            role="SECONDARY_ONLY",
                            positive_count=len(positive),
                            negative_count=len(negative),
                            known_coverage=(len(positive) + len(negative))
                            / len(indices)
                            if indices
                            else None,
                            auc=a,
                            cliffs_delta=2 * a - 1 if a is not None else None,
                        )
                    )
    return statistics, bins, secondary


def coverage(table):
    return {
        str(h): dict(
            counts=dict(Counter(table.text[LABEL_PREFIX + f"status_{h}h"])),
            complete_return_pct=sum(
                math.isfinite(v)
                for v in table.numeric[LABEL_PREFIX + f"close_return_pct_{h}h"]
            )
            / table.size
            * 100,
            complete_atr_pct=sum(
                math.isfinite(v) for v in table.numeric[LABEL_PREFIX + f"mfe_atr_{h}h"]
            )
            / table.size
            * 100,
        )
        for h in HORIZONS
    }


def consistent_feature(feature, stats, secondary):
    """Frozen descriptive evidence screen, not significance or an entry rule."""
    index = {
        (r["year"], r["population"], r["outcome"]): r
        for r in stats
        if r["feature"] == feature
    }
    populations = [
        "ALL_TOP20",
        "SYMBOL_DAY_FIRST",
        "BUY_ELIGIBLE",
        "BUY_SETUP_EPISODE_FIRST",
    ]
    if feature in BREADTH:
        populations += ["ONE_ROW_PER_TIMESTAMP", "ONE_MARKET_ROW_PER_DAY"]
    rows = [
        index.get((y, p, LABEL_PREFIX + f"mfe_atr_{h}h"))
        for y in (2024, 2025)
        for p in populations
        for h in (72, 168)
    ]
    directions = [
        1 if r["cliffs_delta"] > 0 else -1
        for r in rows
        if r and r["cliffs_delta"] is not None
    ]
    direction = directions[0] if directions and len(set(directions)) == 1 else 0
    adequate = all(
        r and min(r["top_quintile_observed"], r["bottom_quintile_observed"]) >= MIN_N
        for r in rows
    )
    nontrivial = bool(
        direction
        and adequate
        and all(
            abs(r["cliffs_delta"]) >= MIN_CLIFF
            and r["spearman"] is not None
            and r["spearman"] * direction >= MIN_RHO
            for r in rows
        )
    )
    # A strong inverse return association prevents a positive MFE-only nomination.
    return_contradiction = (
        any(
            r["feature"] == feature
            and r["outcome"]
            in (
                LABEL_PREFIX + "close_return_pct_72h",
                LABEL_PREFIX + "close_return_pct_168h",
            )
            and r["spearman"] is not None
            and r["spearman"] * direction <= -MIN_RHO
            for r in stats
        )
        if direction
        else False
    )
    inverse = (
        [
            r
            for r in secondary
            if r["feature"] == feature
            and r["outcome"] in (SECONDARY[2], SECONDARY[3])
            and min(r["positive_count"], r["negative_count"]) >= MIN_N
            and r["cliffs_delta"] is not None
            and r["cliffs_delta"]
            * direction
            * (-1 if r["outcome"] == SECONDARY[3] else 1)
            <= -MIN_CLIFF
        ]
        if direction
        else []
    )
    strengths = [
        abs(r["cliffs_delta"]) for r in rows if r and r["cliffs_delta"] is not None
    ]
    return dict(
        feature=feature,
        direction=direction,
        adequate_counts=adequate,
        cross_year_horizon_dependence_sign_consistent=bool(direction),
        nontrivial_all_required_populations=nontrivial,
        primary_return_strong_contradiction=return_contradiction,
        secondary_strong_contradiction=bool(inverse),
        secondary_contradiction_cells=len(inverse),
        minimum_required_abs_cliff=min(strengths) if strengths else None,
        stable=nontrivial and not return_contradiction and not inverse,
    )


def render(stats, bins, secondary, counts, out):
    consistency = [consistent_feature(f, stats, secondary) for f in FEATURES]
    controls = [r for r in consistency if r["feature"] in CONTROLS and r["stable"]]
    best_control = max([r["minimum_required_abs_cliff"] for r in controls], default=0)
    candidates = [
        r
        for r in consistency
        if r["feature"] not in CONTROLS
        and r["stable"]
        and r["minimum_required_abs_cliff"] >= best_control + 0.02
    ]
    candidates.sort(key=lambda r: (-r["minimum_required_abs_cliff"], r["feature"]))
    candidate = candidates[0]["feature"] if candidates else "NONE"
    interesting = any(
        r["feature"] not in CONTROLS
        and r["cliffs_delta"] is not None
        and abs(r["cliffs_delta"]) >= MIN_CLIFF
        for r in stats
    )
    category = (
        "ORTHOGONAL_FEATURE_SIGNAL_FOUND"
        if candidates
        else "ORTHOGONAL_FEATURE_SIGNAL_WEAK_OR_UNSTABLE"
        if interesting
        else "ORTHOGONAL_FEATURE_SIGNAL_NOT_FOUND"
    )
    summary = dict(
        title="Cross-Sectional Market Structure Diagnostic v1",
        category=category,
        hypothesis_candidate_feature=candidate,
        strongest_stable_new_feature=candidate,
        research_role="EXPLORATION_BOTH_YEARS",
        source_runs=edge.RUNS,
        base=BASE,
        counts=counts,
        feature_consistency=consistency,
        caution="Descriptive marginal association only; neither conditional orthogonality nor independent samples, causality, net trade edge or a validated strategy is established. No multiplicity-adjusted inference. No ranking/filter implemented.",
        secondary_role="1m known subsets are nonrandom, coverage-limited consistency checks only; cannot establish a primary signal.",
    )
    write_rows(out / "feature-statistics.csv", stats)
    write_rows(out / "feature-deciles.csv", bins)
    write_rows(out / "one-minute-consistency.csv", secondary)
    write_rows(out / "cross-year-consistency.csv", consistency)
    write_rows(
        out / "dependency-sensitivity.csv",
        [r for r in stats if r["population"] not in {"ALL_TOP20", "BUY_ELIGIBLE"}],
    )
    write_rows(
        out / "control-feature-comparison.csv",
        [
            r
            | {
                "feature_role": "CURRENT_CONTROL"
                if r["feature"] in CONTROLS
                else "PRESPECIFIED_NEW"
            }
            for r in stats
            if r["population"] in {"ALL_TOP20", "BUY_ELIGIBLE"}
        ],
    )
    (out / "summary.json").write_bytes(edge.encoded(summary))
    lines = [
        "# Cross-Sectional Market Structure Diagnostic v1",
        "",
        "READ-ONLY EXPLORATION — 2024 and 2025. No replay, deployment or production change.",
        "",
        f"Category: **{category}**",
        f"hypothesis_candidate_feature: **{candidate}**",
        "",
        summary["caution"],
        "",
        "## Primary 1h coverage",
        "",
        "```json",
        json.dumps(counts, indent=2),
        "```",
        "",
        "## Fixed-feature evidence",
        "",
        "Cliff below is top vs bottom quintile 72h / 168h MFE ATR. Positive = higher feature has greater subsequent excursion; this is not realized PnL.",
        "",
        "| Feature | 2024 ALL | 2025 ALL | 2024 BUY | 2025 BUY | Stable screen |",
        "|---|---|---|---|---|---|",
    ]
    lookup = {
        (r["year"], r["population"], r["feature"], r["outcome"]): r for r in stats
    }
    for check in consistency:
        f = check["feature"]
        cells = []
        for y, p in (
            (2024, "ALL_TOP20"),
            (2025, "ALL_TOP20"),
            (2024, "BUY_ELIGIBLE"),
            (2025, "BUY_ELIGIBLE"),
        ):
            vals = [
                lookup[(y, p, f, LABEL_PREFIX + f"mfe_atr_{h}h")]["cliffs_delta"]
                for h in (72, 168)
            ]
            cells.append(" / ".join("NA" if v is None else f"{v:+.4f}" for v in vals))
        lines.append("| " + " | ".join([f] + cells + [str(check["stable"])]) + " |")
    lines += [
        "",
        "## Interpretation limits",
        "",
        "Full tests and source SHA checks are technical validity, not proof of predictive edge. Hourly rows overlap; symbol-day and eligible episode-first screens are mandatory. Breadth uses an additional authoritative one-row-per-timestamp fixed-cohort median outcome and first-market-row-per-day sensitivity. Market outcomes are unavailable unless all 20 member labels are observed. Delisting/gaps are censored, never filled or shortened.",
        "",
        "Each population's deciles/quintiles are descriptive within-sample quantile groups. They are not traded thresholds. Equal values are never split; empty bins are explicit. No interactions, grid, fitting or threshold search. MFE/MAE are signed raw extrema divided by past ATR, not stop-based/net-fee trades.",
        "",
        "1m results are secondary and retain UNKNOWN. Read `methodology.json`, feature-statistics/dependency-sensitivity and one-minute-consistency together; a raw-only effect cannot nominate a feature. No 2023 research cohort or 2026 archive is used.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    return summary


def methodology():
    return dict(
        base=BASE,
        feature_families=dict(
            relative_strength=STRENGTH,
            market_breadth=BREADTH,
            current_controls=CONTROLS,
        ),
        source="Manifest-verified prior canonical Top20/eligibility rows; exact source-run pinned monthly 1h ZIPs. No network/download/merged cache fallback. Source-required 2023 warmup only; no 2026 data.",
        decisions="Every coin/BTC return uses contiguous completed hourly closes: close(t-1h) / close(t-(h+1)h)-1; all source close_time < decision. Reuse unchanged decision-time institutional 1h EMA and ATR. Membership is source canonical shared Top20, no held-only rows.",
        ranks="Average tied ordinal ranks, percentile=(rank-1)/(n-1), 0 weakest/1 strongest. Fixed 20 members; if any return unavailable, cohort return ranks/median/breadth/dispersion unavailable. Dispersion is population standard deviation (ddof=0), returns percentage points. Breadth comparisons strictly >, denominator 20, no partial cohort.",
        primary="POST_HOC_FUTURE_LABEL_1H only. h completed bars opening in [decision, decision+h). Full continuous window mandatory; censor entire horizon at gap or run-end, no shortening. Close/high/low returns from decision reference. MFE=(max high-reference)/past ATR, MAE=(min low-reference)/past ATR; signed, no clamp, no stop or intrabar simulation. ATR invalid only removes ATR outcomes, not percentage returns.",
        secondary="Exact existing POST_HOC_FUTURE_LABEL 1m barrier columns joined by symbol/time from verified CSV; UNKNOWN preserved, no new minute reads/downloads. +1/+2/+5R and stop-before1 exported; feature AUC/Cliff for all four. Secondary never establishes a signal.",
        populations="ALL_TOP20; BUY_ELIGIBLE unchanged final asset eligibility excluding portfolio/correlation. SYMBOL_DAY_FIRST is first Top20 row per UTC symbol-day; BUY_SYMBOL_DAY_FIRST its eligible subset. BUY_SETUP_EPISODE_FIRST uses prior same-symbol consecutive-hour, unchanged eligibility boolean episode; gaps/bool flips reset, reason changes do not. Market breadth authoritative: ONE_ROW_PER_TIMESTAMP, fixed decision Top20 median of future labels (all20 observed), plus ONE_MARKET_ROW_PER_DAY.",
        statistics="Ten fixed descriptive rank deciles, ties together, empty bins explicit; top/bottom quintile percentile >=.8/<=.2. Pairwise complete-case Spearman; Cliff=2*Mann-Whitney-AUC-1; no p-values, fitted model, thresholds, interaction or grid. Monotonic if >=5 nonempty bins, |decile rho|>=.8 and >=75% adjacent observed steps concordant.",
        frozen_screen=dict(
            minimum_each_quintile=MIN_N,
            minimum_abs_cliff=MIN_CLIFF,
            minimum_abs_spearman=MIN_RHO,
            required="Both years, 72/168h MFE ATR same direction with Cliff>=.147 and same-direction rho>=.05 in ALL, symbol-day-first, BUY and BUY episode-first; breadth ALSO market timestamp and market-day. n>=30 each quintile. No primary 72/168 return rho strongly opposite (>=.05) in any scope. No adequately observed 1m +5R/stop Cliff strongly opposite (>=.147).",
            candidate="At most one stable prespecified new feature: greatest minimum required absolute Cliff, feature-name tie break; exceeds best stable control by .02. Descriptive screening, not optimization or statistical validation.",
            category="A if a candidate survives; B if any new feature has nontrivial Cliff anywhere but none survives; C otherwise.",
        ),
        limitations="Overlapping horizons and cross-symbol dependence; descriptive, no confidence/inference claims. Both years exploration. ATR-normalized excursion is not profits. Orthogonal category is a requested label, not evidence of conditional incremental information beyond controls.",
        safety=dict(
            db_mode="ro",
            query_only=True,
            network=False,
            new_replay=False,
            database_writes=0,
            production_change=False,
            deployment=False,
        ),
    )


def package(out):
    files = sorted(
        p
        for p in out.iterdir()
        if p.suffix in {".json", ".csv", ".md"} and p.name != "manifest.json"
    )
    manifest = dict(
        base=BASE,
        source_runs=edge.RUNS,
        research_script_sha256=sha(__file__),
        files={p.name: dict(bytes=p.stat().st_size, sha256=sha(p)) for p in files},
    )
    (out / "manifest.json").write_bytes(edge.encoded(manifest))
    target = out / (NAME + ".zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(files + [out / "manifest.json"]):
            info = zipfile.ZipInfo(p.name, (2024, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            with p.open("rb") as src, z.open(info, "w", force_zip64=True) as dst:
                for blob in iter(lambda: src.read(1024 * 1024), b""):
                    dst.write(blob)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    out = (args.output or root / "artifacts" / NAME).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sys.addaudithook(edge.no_network)
    prior = root / "artifacts/institutional-eligibility-gate-diagnostic-v1"
    manifest = json.loads((prior / "manifest.json").read_text())
    for name in (
        "source-validation.json",
        "summary.json",
        "methodology.json",
        "top20-opportunity-dataset-2024.csv",
        "top20-opportunity-dataset-2025.csv",
    ):
        if sha(prior / name) != manifest["files"][name]["sha256"]:
            raise ValueError("Prior diagnostic SHA mismatch: " + name)
    old = json.loads((prior / "source-validation.json").read_text())
    (out / "methodology.json").write_bytes(edge.encoded(methodology()))
    print("Source fingerprints before (read-only)", flush=True)
    with edge.read_only(root / "data/replay.db") as db:
        before = edge.source_fingerprint(db, list(edge.RUNS.values()))
        if {k: v for k, v in before.items() if k != "all_run_ids"} != {
            k: v for k, v in old["after"].items() if k != "all_run_ids"
        }:
            raise ValueError("Source changed since prior diagnostic")
        sources = {y: edge.load_source(db, y) for y in (2024, 2025)}
    comparison = edge.baseline_comparability(sources[2024], sources[2025], root)
    if not comparison["usable"]:
        raise ValueError("2025 baseline not comparable")
    all_stats, all_bins, all_secondary, providers, cache_before, counts = (
        [],
        [],
        [],
        {},
        {},
        {},
    )
    for year in (2024, 2025):
        provider = edge.Archives(
            root / "data/replay-cache",
            sources[year]["metadata"]["historical_data_revision"],
        )
        providers[year] = provider
        cache_before[year] = provider.fingerprint()
        table, market = build_year(year, sources[year], provider, prior, out)
        counts[year] = dict(
            rows=table.size,
            buy_eligible=int(sum(table.numeric["current_buy_eligible"])),
            market_rows=market.size,
            primary_coverage=coverage(table),
        )
        for data, is_market in ((table, False), (market, True)):
            stats, bins, secondary = describe(data, year, is_market)
            all_stats += stats
            all_bins += bins
            all_secondary += secondary
        del table, market
    summary = render(all_stats, all_bins, all_secondary, counts, out)
    print("Source fingerprints after (read-only)", flush=True)
    with edge.read_only(root / "data/replay.db") as db:
        after = edge.source_fingerprint(db, list(edge.RUNS.values()))
    if before != after:
        raise ValueError("Immutable source/run inventory changed")
    for y, provider in providers.items():
        if cache_before[y] != provider.fingerprint():
            raise ValueError("Pinned metadata changed")
        provider.verify_used()
        if any("-2026-" in k for k in provider.used):
            raise ValueError("2026 source contamination")
    validation = dict(
        before=before,
        after=after,
        source_fingerprint_unchanged=True,
        source_runs=edge.RUNS,
        source_sha=edge.SOURCE_SHA,
        source_config_hashes={
            y: s["metadata"]["config_hash"] for y, s in sources.items()
        },
        source_market_context="ISOLATED_TOPN_V1",
        prior_manifest_sha256=sha(prior / "manifest.json"),
        input_csv_sha256={
            y: sha(prior / f"top20-opportunity-dataset-{y}.csv") for y in sources
        },
        source_2025_comparability=comparison,
        read_only_uri=True,
        query_only=True,
        no_network=True,
        new_replay=False,
        database_writes=0,
        production_behavior_changed=False,
        deployment=False,
        cache_metadata_before=cache_before,
        cache_metadata_after={y: p.fingerprint() for y, p in providers.items()},
        pinned_objects_used={y: p.used for y, p in providers.items()},
        counts=counts,
    )
    (out / "source-validation.json").write_bytes(edge.encoded(validation))
    bundle = package(out)
    print(
        json.dumps(
            dict(
                category=summary["category"],
                candidate=summary["hypothesis_candidate_feature"],
                zip=str(bundle),
                bytes=bundle.stat().st_size,
                sha256=sha(bundle),
            ),
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
