"""Matched-entry descriptive outcomes; deliberately no portfolio equity metric."""

from collections import defaultdict
from itertools import combinations
from statistics import mean, median

from crypto_bot.replay.metrics import bucket

TIE_TOLERANCE_PCT = 0.000001


def sample(n):
    return (
        "INSUFFICIENT SAMPLE"
        if n < 30
        else "SMALL SAMPLE"
        if n < 100
        else "LARGER SAMPLE"
    )


def outcome(rows):
    closed = [r for r in rows if r["comparison_eligible"]]
    returns = [r["return_pct"] for r in closed]
    rs = [r["r_multiple"] for r in closed if r["r_multiple"] is not None]
    positive = sum(max(r, 0) for r in returns)
    negative = -sum(min(r, 0) for r in returns)
    return dict(
        matched_entries=len(rows),
        comparable_closed_count=len(closed),
        incomplete_count=len(rows) - len(closed),
        open_count=sum(r["position_status"] == "OPEN" for r in rows),
        stale_count=sum(r["status"] == "OPEN_STALE" for r in rows),
        historical_stale_count=sum(bool(r["first_stale_time"]) for r in rows),
        win_rate=sum(r > 0 for r in returns) / len(returns) * 100 if returns else None,
        mean_return_pct=mean(returns) if returns else None,
        median_return_pct=median(returns) if returns else None,
        expectancy_r=mean(rs) if rs else None,
        median_r=median(rs) if rs else None,
        profit_factor_normalized=positive / negative if negative else None,
        profit_factor_basis="RETURN_PERCENT",
        mean_normalized_pnl_per_1000=mean(
            [r["normalized_pnl_per_1000"] for r in closed]
        )
        if closed
        else None,
        ambiguous_count=sum(r["intrabar_ambiguity"] for r in closed),
        sample_label=sample(len(closed)),
        statistically_validated=False,
    )


def compare(entries, rows):
    policies = sorted({r["policy_id"] for r in rows})
    lookup = {(r["shadow_entry_id"], r["policy_id"]): r for r in rows}
    common = [
        e["shadow_entry_id"]
        for e in entries
        if all(lookup[e["shadow_entry_id"], p]["comparison_eligible"] for p in policies)
    ]
    summaries = {}
    for p in policies:
        selected = [r for r in rows if r["policy_id"] == p]
        summaries[p] = dict(
            **outcome(selected),
            common_matched_outcomes=outcome([lookup[e, p] for e in common]),
        )
    pairs = []
    for a, b in combinations(policies, 2):
        paired = [
            (lookup[e["shadow_entry_id"], a], lookup[e["shadow_entry_id"], b])
            for e in entries
            if lookup[e["shadow_entry_id"], a]["comparison_eligible"]
            and lookup[e["shadow_entry_id"], b]["comparison_eligible"]
        ]
        diffs = [x["return_pct"] - y["return_pct"] for x, y in paired]
        rdiffs = [
            x["r_multiple"] - y["r_multiple"]
            for x, y in paired
            if x["r_multiple"] is not None and y["r_multiple"] is not None
        ]
        pairs.append(
            dict(
                policy_a=a,
                policy_b=b,
                canonical_entries=len(entries),
                matched_entries=len(paired),
                a_wins=sum(d > TIE_TOLERANCE_PCT for d in diffs),
                b_wins=sum(d < -TIE_TOLERANCE_PCT for d in diffs),
                ties=sum(abs(d) <= TIE_TOLERANCE_PCT for d in diffs),
                incomplete=len(entries) - len(paired),
                mean_return_difference=mean(diffs) if diffs else None,
                median_return_difference=median(diffs) if diffs else None,
                mean_r_difference=mean(rdiffs) if rdiffs else None,
                tie_tolerance_pct=TIE_TOLERANCE_PCT,
                sample_label=sample(len(paired)),
                statistically_validated=False,
            )
        )
    return dict(
        canonical_entry_count=len(entries),
        common_comparable_entry_count=len(common),
        policy_summaries=summaries,
        pairwise=pairs,
    )


def analysis(entries, rows):
    result = compare(entries, rows)
    specs = dict(
        symbol=lambda e: e["symbol"],
        asset_group=lambda e: e["asset_group"],
        btc_regime=lambda e: e.get("btc_regime", "UNKNOWN"),
        atr_pct=lambda e: bucket(
            e.get("atr_pct"),
            [0, 1, 2, 3, 5, float("inf")],
            ["0-1%", "1-2%", "2-3%", "3-5%", ">5%"],
        ),
        range_position=lambda e: bucket(
            e.get("range_position"),
            [0, 0.25, 0.5, 0.75, 1.00001],
            ["0-.25", ".25-.50", ".50-.75", ".75-1.00"],
        ),
        rsi=lambda e: bucket(
            e.get("rsi"),
            [0, 30, 40, 50, 60, 70, 101],
            ["0-30", "30-40", "40-50", "50-60", "60-70", "70+"],
        ),
        entry_score=lambda e: bucket(
            e.get("entry_score"),
            [0, 50, 70, 75, 80, 85, 90, 101],
            ["0-50", "50-70", "70-75", "75-80", "80-85", "85-90", "90+"],
        ),
        relative_volume=lambda e: bucket(
            e.get("relative_volume"),
            [0, 1, 1.5, 2, 3, float("inf")],
            ["0-1", "1-1.5", "1.5-2", "2-3", "3+"],
        ),
        month=lambda e: e["entry_time"][:7],
        quarter=lambda e: (
            e["entry_time"][:4] + "-Q" + str((int(e["entry_time"][5:7]) - 1) // 3 + 1)
        ),
    )
    breakdowns = {}
    for name, function in specs.items():
        groups = defaultdict(list)
        for e in entries:
            groups[function(e)].append(e)
        breakdowns[name] = {}
        for key, group in sorted(groups.items()):
            ids = {e["shadow_entry_id"] for e in group}
            breakdowns[name][key] = compare(
                group, [r for r in rows if r["shadow_entry_id"] in ids]
            )
    result.update(
        breakdowns=breakdowns,
        incomplete_outcome_count=sum(not r["comparison_eligible"] for r in rows),
        stale_outcome_count=sum(r["status"] == "OPEN_STALE" for r in rows),
        incomplete_entry_count=len(
            {r["shadow_entry_id"] for r in rows if not r["comparison_eligible"]}
        ),
        stale_entry_count=len(
            {r["shadow_entry_id"] for r in rows if r["status"] == "OPEN_STALE"}
        ),
    )
    # canonical_entry_count is supplied by the top-level export, too.
    result.pop("canonical_entry_count")
    return result


def export(result, compact=False):
    if not compact:
        return result
    return {
        k: v for k, v in result.items() if k not in ("canonical_entries", "outcomes")
    }
