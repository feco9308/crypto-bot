"""Pure descriptive helpers reused from validated diagnostics; no runtime imports.

Provenance: 26351dde5e09017c822f931b9afd1101380390bd (flow/market structure
and institutional_signal_edge). No outcome definition or episode construction.
"""

import csv
import hashlib
import json
import math
import sqlite3
import statistics as st
from array import array
from bisect import bisect_left, bisect_right
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

NAN = float("nan")


def encoded(value):
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


@contextmanager
def read_only(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    try:
        yield db
    finally:
        db.close()


def source_fingerprint(db, run_ids):
    """Logical immutable source rows; exclude unrelated live worker heartbeats."""
    result = {}
    for (table,) in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'replay_%' ORDER BY name"
    ):
        columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
        key = (
            "id"
            if table == "replay_runs"
            else "run_id"
            if "run_id" in columns
            else None
        )
        if key is None:
            continue
        for rid in run_ids:
            h, n = hashlib.sha256(), 0
            for row in db.execute(
                f'SELECT * FROM "{table}" WHERE "{key}"=? ORDER BY rowid', (rid,)
            ):
                h.update(json.dumps(row, separators=(",", ":")).encode() + b"\n")
                n += 1
            result[f"{rid}/{table}"] = {"rows": n, "sha256": h.hexdigest()}
    # Creation guard covers every run, not just the two sources.
    result["all_run_ids"] = digest(
        db.execute("SELECT id FROM replay_runs ORDER BY id").fetchall()
    )
    return result


def ranks(values):
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        rank = (cursor + 1 + end) / 2
        for i in order[cursor:end]:
            result[i] = rank
        cursor = end
    return result


def spearman(x, y):
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    return st.correlation(ranks(x), ranks(y))


def auc(positives, negatives):
    """Mann-Whitney AUC with ties; no fitted weights or cutoffs."""
    if not positives or not negatives:
        return None
    negative = sorted(negatives)
    return sum(
        bisect_left(negative, p)
        + (bisect_right(negative, p) - bisect_left(negative, p)) / 2
        for p in positives
    ) / (len(positives) * len(negative))


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


def percentile(values):
    """Average ties, (rank-1)/(n-1); a sole valid observation ranks .5."""
    valid = [i for i, v in enumerate(values) if math.isfinite(v)]
    result = [NAN] * len(values)
    ranked = ranks([values[i] for i in valid])
    for i, r in zip(valid, ranked):
        result[i] = (r - 1) / (len(valid) - 1) if len(valid) > 1 else 0.5
    return result


def deciles(values):
    """Equal values stay together. Always report all ten bins, including empty."""
    return [
        min(9, int(p * 10)) + 1 if math.isfinite(p) else 0 for p in percentile(values)
    ]


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
    rho = spearman([i for i, v in pairs], [v for i, v in pairs])
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


def row_digest(table, indices):
    h = hashlib.sha256()
    for i in indices:
        h.update(
            (table.text["symbol"][i] + "/" + table.text["timestamp"][i] + "\n").encode()
        )
    return h.hexdigest()


def effect(table, indices, feature, outcome):
    good = [i for i in indices if math.isfinite(table.numeric[feature][i])]
    values = [table.numeric[feature][i] for i in good]
    ranks = percentile(values)
    pairs = [i for i in good if math.isfinite(table.numeric[outcome][i])]
    top = [
        i
        for i, p in zip(good, ranks)
        if p >= 0.8 and math.isfinite(table.numeric[outcome][i])
    ]
    bottom = [
        i
        for i, p in zip(good, ranks)
        if p <= 0.2 and math.isfinite(table.numeric[outcome][i])
    ]
    a = [table.numeric[outcome][i] for i in top]
    b = [table.numeric[outcome][i] for i in bottom]
    auc_value = auc(a, b)
    result = dict(
        feature=feature,
        outcome=outcome,
        population_count=len(indices),
        feature_count=len(good),
        paired_count=len(pairs),
        paired_row_sha256=row_digest(table, pairs),
        spearman=spearman(
            [table.numeric[feature][i] for i in pairs],
            [table.numeric[outcome][i] for i in pairs],
        ),
        top_count=len(a),
        bottom_count=len(b),
        top_median=median(a),
        bottom_median=median(b),
        median_difference=st.median(a) - st.median(b) if a and b else None,
        auc=auc_value,
        cliffs_delta=2 * auc_value - 1 if auc_value is not None else None,
    )
    return result, good, ranks, top, bottom


def leave_group_effect(table, top, bottom, outcome, group, all_groups=()):
    """Exact leave-one-group U using fixed full-population quintile memberships."""
    a = sorted(table.numeric[outcome][i] for i in top)
    b = sorted(table.numeric[outcome][i] for i in bottom)
    if not a or not b:
        return []
    total = auc(a, b) * len(a) * len(b)
    ga, gb = defaultdict(list), defaultdict(list)
    for i in top:
        ga[group(i)].append(table.numeric[outcome][i])
    for i in bottom:
        gb[group(i)].append(table.numeric[outcome][i])
    result = []
    for label in sorted(ga.keys() | gb.keys() | set(all_groups)):
        x, y = ga[label], gb[label]
        n, m = len(a) - len(x), len(b) - len(y)
        removed_a = sum(
            bisect_left(b, v) + (bisect_right(b, v) - bisect_left(b, v)) * 0.5
            for v in x
        )
        removed_b = sum(
            len(a) - bisect_right(a, v) + (bisect_right(a, v) - bisect_left(a, v)) * 0.5
            for v in y
        )
        both = (auc(x, y) * len(x) * len(y)) if x and y else 0
        u = total - removed_a - removed_b + both
        result.append(
            dict(
                removed_group=label,
                top_count=n,
                bottom_count=m,
                removed_top_count=len(x),
                removed_bottom_count=len(y),
                cliffs_delta=2 * u / (n * m) - 1 if n and m else None,
            )
        )
    return result
