"""Offline asset eligibility diagnostic. No replay/portfolio/risk/execution service.

python -m research.institutional_eligibility_gate --root /home/ndvi/crypto-bot
Only research artifacts are written; every source database is URI mode=ro.
"""

import argparse
import csv
import gzip
import hashlib
import inspect
import io
import json
import math
import sys
import zipfile
from array import array
from collections import Counter, defaultdict
from contextlib import ExitStack
from pathlib import Path

from crypto_bot.config.settings import Settings
from crypto_bot.indicators.technical import features
from crypto_bot.replay.clock import at, ms
from crypto_bot.replay.institutional import InstitutionalContext, InstitutionalStrategy
from crypto_bot.replay.market_context import frame, trim
from crypto_bot.replay.universe import CandleHistory, HistoricalUniverse
from crypto_bot.scanner.scoring import score
from crypto_bot.trading.domain import D, MarketContext
from research import institutional_signal_edge as edge

BASE = "f84d00f3282ee973e2bbedd5c0552a2d07174899"
HOUR = edge.HOUR
GATES = (
    "SCORE",
    "DELTA",
    "WATCH",
    "RSI",
    "RELATIVE_VOLUME",
    "1H_TREND",
    "4H_TREND",
    "1D_TREND",
    "SPREAD",
    "BTC_REGIME",
    "INITIAL_STOP",
    "USER_IGNORE",
)
PASS_FIELD = dict(
    zip(
        GATES,
        (
            "score_pass",
            "delta_pass",
            "watch_pass",
            "rsi_range_pass",
            "relative_volume_pass",
            "trend_1h_pass",
            "trend_4h_pass",
            "trend_1d_pass",
            "spread_pass",
            "btc_regime_strategy_pass",
            "initial_stop_pass",
            "user_ignore_pass",
        ),
    )
)
REASONS = {
    "SCORE_BELOW_MIN": "SCORE",
    "SCORE_DELTA_NOT_POSITIVE": "DELTA",
    "OUTSIDE_ALGORITHM_WATCH": "WATCH",
    "USER_IGNORE": "USER_IGNORE",
    "RSI_OUTSIDE_RANGE": "RSI",
    "RELATIVE_VOLUME_BELOW_MIN": "RELATIVE_VOLUME",
    "SPREAD_LIMIT": "SPREAD",
    "INVALID_INITIAL_STOP": "INITIAL_STOP",
    "BTC_REGIME_UNAVAILABLE": "BTC_REGIME",
    "BTC_BEAR_REGIME": "BTC_REGIME",
    "BTC_REGIME_RISK_ZERO": "BTC_REGIME",
    **{
        f"MULTITF_{tf}_{reason}": f"{tf}_TREND"
        for tf in ("1H", "4H", "1D")
        for reason in ("UNAVAILABLE", "NOT_BULLISH")
    },
}
# Frozen descriptive conventions, not eligibility thresholds or optimized rules.
MIN_COUNT = 30
MIN_RATE_DIFFERENCE = 0.02
MIN_CLIFF = 0.147
MAX_COVERAGE_DIFFERENCE = 0.10
LABEL_KEYS = ("stop_before_1r",) + tuple(f"hit_{n}r_before_stop" for n in edge.LEVELS)


class CanonicalContext(InstitutionalContext):
    """Exact source trend memoization by closed UTC bucket, never partial bars.

    Production trend() uses the same completed-bucket end/seed for every now
    inside a bucket. Direct-context equality is tested, including partial hours.
    """

    def trend(self, symbol, hours, now, fast, slow):
        return super().trend(
            symbol, hours, now // (hours * HOUR) * (hours * HOUR), fast, slow
        )


def reference_stop(context, settings):
    # Exact expression and validity condition in InstitutionalStrategy.evaluate.
    if (
        context.atr <= 0
        or context.price - context.atr * settings.stop_atr_multiplier <= 0
    ):
        return None
    return context.price - context.atr * settings.stop_atr_multiplier


def predicates(context, settings, signal):
    """Observe the source evaluator; never change/relax a strategy setting."""
    c, s = context, settings
    audit = c.features["institutional"]
    btc = audit.get("btc", {})
    spread = c.features.get("spread_pct", c.features.get("spread"))
    result = dict(
        score_pass=c.score >= s.min_score,
        delta_available=c.delta is not None,
        delta_pass=not (
            (s.require_delta and c.delta is None)
            or (c.delta is not None and c.delta <= s.min_delta)
        ),
        watch_pass=not s.require_watch or c.algorithm_watch,
        algorithm_watch=c.algorithm_watch,
        rsi_low_pass=c.rsi >= s.min_rsi,
        rsi_high_pass=c.rsi <= s.max_rsi,
        rsi_range_pass=s.min_rsi <= c.rsi <= s.max_rsi,
        relative_volume_pass=c.relative_volume >= s.min_relative_volume,
        spread_available=spread is not None,
        spread_pass=spread is None or D(spread) <= s.max_spread_pct,
        btc_regime_available=btc.get("regime", "UNKNOWN") != "UNKNOWN",
        btc_regime_strategy_pass=not s.btc_regime_filter
        or (
            btc.get("regime", "UNKNOWN") != "UNKNOWN"
            and D(btc.get("risk_multiplier", "0")) != 0
        ),
        initial_stop_pass=reference_stop(c, s) is not None,
        user_ignore_pass=c.watch_state != "IGNORE",
    )
    for tf in ("1h", "4h", "1d"):
        t = audit.get("coin_trends", {}).get(tf, {})
        result[f"trend_{tf}_available"] = bool(t.get("ready"))
        result[f"trend_{tf}_pass"] = not getattr(
            s, "require_" + tf + "_bullish"
        ) or bool(t.get("ready") and t["ema_fast"] > t["ema_slow"])
    result["institutional_context_available"] = result["btc_regime_available"] and all(
        result[f"trend_{tf}_available"] for tf in ("1h", "4h", "1d")
    )
    failures = list(signal.reasons) if signal.action == "HOLD" else []
    if any(r not in REASONS for r in failures):
        raise ValueError("Unknown source strategy failure: " + repr(failures))
    gates = sorted({REASONS[r] for r in failures})
    observed = sorted(g for g, field in PASS_FIELD.items() if not result[field])
    if gates != observed or (signal.action == "BUY") != (not gates):
        raise ValueError(f"Production predicate mismatch: {gates} vs {observed}")
    single = gates[0] if len(gates) == 1 else None
    subgroup = (
        "RSI_LOW"
        if single == "RSI" and not result["rsi_low_pass"]
        else "RSI_HIGH"
        if single == "RSI" and not result["rsi_high_pass"]
        else single
    )
    result.update(
        strategy_action_without_portfolio=signal.action,
        final_asset_eligible=signal.action == "BUY",
        strategy_reasons=list(signal.reasons),
        failure_count=len(gates),
        failure_gates=gates,
        canonical_failure_combination="|".join(sorted(failures)) or "NONE",
        single_failure=single is not None,
        single_failure_gate=single,
        single_failure_population="ONLY_" + subgroup + "_FAIL" if subgroup else None,
        correlation_excluded=True,
    )
    return result


def market_context(row, watch, now, audit, settings):
    f = row["features"]
    delta = row["momentum"].get(f"delta_{settings.delta_window}h")
    return MarketContext(
        now // HOUR,
        row["symbol"],
        at(now),
        D(f["price"]),
        D(row["total_score"]),
        D(delta) if delta is not None else None,
        D(f["rsi"]),
        D(f["ema50"]),
        D(f["ema200"]),
        D(f["atr"]),
        D(f["relative_volume"]),
        "WATCH" if row["symbol"] in watch else "AUTO",
        row["symbol"] in watch,
        f | {"institutional": audit},
    )


def episode(row, state):
    symbol, now, eligible = (
        row["symbol"],
        ms(row["timestamp"]),
        row["final_asset_eligible"],
    )
    previous = state.get(symbol)
    first = previous is None or now - previous[0] != HOUR or previous[1] != eligible
    since, ordinal = (now, 1) if first else (previous[2], previous[3] + 1)
    state[symbol] = now, eligible, since, ordinal
    row.update(
        symbol_day_id=f"{row['year']}/{symbol}/{row['timestamp'][:10]}",
        eligibility_episode_id=f"{row['year']}/{symbol}/{edge.iso(since)}/{'BUY' if eligible else 'HOLD'}",
        eligibility_episode_ordinal=ordinal,
        eligibility_episode_first=first,
    )
    return row


def row_record(row, watch, now, context, settings, signal, year, rank, state):
    f = row["features"]
    audit = context.features["institutional"]
    stop = reference_stop(context, settings)
    if signal.action == "BUY" and stop != signal.stop_reference:
        raise ValueError("Source BUY stop construction mismatch")
    record = dict(
        year=year,
        run_id=edge.RUNS[year],
        timestamp=edge.iso(now),
        symbol=row["symbol"],
        top20_rank=rank,
        auto_eligible=row["auto_eligible"],
        row_scope="TOP20_SHARED_ONLY",
        proposed_entry_reference=float(context.price),
        original_stop_reference=float(stop) if stop is not None else None,
        original_stop_reference_exact=str(stop) if stop is not None else None,
        stop_reference_provenance="PRODUCTION_ATR_EXPRESSION_DIAGNOSTIC_REFERENCE_NOT_ORDER",
        stop_distance_pct=float((context.price - stop) / context.price * 100)
        if stop is not None
        else None,
        decision_source_timestamp=edge.iso(f["candle_time"]),
        market_score=row["total_score"],
        score_components=row["components"],
        **{f"component_{k}": v for k, v in row["components"].items()},
        **{k: row["momentum"].get(k) for k in ("delta_1h", "delta_4h", "delta_24h")},
        **{
            k: f[k]
            for k in (
                "relative_volume",
                "rsi",
                "atr",
                "atr_pct",
                "range_position",
                "quote_volume",
                "volatility",
                "spread",
            )
        },
        price_momentum=f["momentum"],
        btc_regime=audit["btc"]["regime"],
        btc_risk_multiplier=audit["btc"]["risk_multiplier"],
        watch_reason=watch.get(row["symbol"]),
        institutional_context=audit,
        **predicates(context, settings, signal),
    )
    if ms(record["decision_source_timestamp"]) >= now:
        raise ValueError("Unclosed decision candle")
    for name, t in list(audit["coin_trends"].items()) + [
        ("btc_" + k, v) for k, v in audit["btc"]["trends"].items()
    ]:
        if t["source_timestamp"] and ms(t["source_timestamp"]) >= now:
            raise ValueError("Partial/future HTF source")
        if name.startswith("btc_"):
            continue
        prefix = "" if name == "1h" else name + "_"
        a, b = t["ema_fast"], t["ema_slow"]
        record[prefix + "ema50_distance_pct"] = (
            (f["price"] / a - 1) * 100 if a else None
        )
        record[prefix + "ema200_distance_pct"] = (
            (f["price"] / b - 1) * 100 if b else None
        )
        record[prefix + "ema50_ema200_distance_pct"] = (
            (a / b - 1) * 100 if a and b else None
        )
    return episode(record, state)


def feature_signature():
    names = (
        CanonicalContext,
        reference_stop,
        predicates,
        market_context,
        episode,
        row_record,
        build_features,
    )
    return edge.digest(
        dict(
            code="\n".join(inspect.getsource(obj) for obj in names),
            prior_helper_sha256=hashlib.sha256(
                Path(edge.__file__).read_bytes()
            ).hexdigest(),
            base=BASE,
        )
    )


def build_features(source, provider, out, source_fingerprint, known):
    """Only pure canonical scanner-frame reconstruction; no replay/ledger."""
    year, cfg = source["year"], source["config"]
    stage = out / f"feature-stage-{year}"
    stage.mkdir(exist_ok=True)
    marker = stage / "complete.json"
    signature = feature_signature()
    if marker.exists():
        done = json.loads(marker.read_text())
        if (
            done["feature_signature"] != signature
            or done["source_fingerprint"] != source_fingerprint
        ):
            raise ValueError(
                "Existing diagnostic feature stage differs; preserve it and choose another output directory"
            )
        for name, sha in done["files"].items():
            if hashlib.sha256((stage / name).read_bytes()).hexdigest() != sha:
                raise ValueError("Diagnostic stage SHA mismatch")
        provider.used.update(done["pinned_objects_used"])
        print(
            f"{year} reusing verified diagnostic feature files (not a replay checkpoint)",
            flush=True,
        )
        return stage, done
    start, end = ms(cfg["start"]), ms(cfg["end"])
    scan_start = start - cfg["warmup_hours"] * HOUR
    settings = Settings(**cfg["scanner_settings"])
    histories = {}
    for i, symbol in enumerate(source["metadata"]["resolved_candidate_symbols"]):
        h = provider.history(symbol, "1h", scan_start, end)
        if len(h):
            histories[symbol] = h
        if i % 150 == 0:
            print(f"{year} pinned canonical hourly {i}/693", flush=True)
    universe = HistoricalUniverse(
        histories, settings, float(cfg["spread_approximation_pct"])
    )
    plan, needed = {}, {"BTCUSDT"}
    for now in range(scan_start, end, HOUR):
        plan[now] = tuple(m.symbol for m in universe.ranked(now))
        if now >= start:
            needed.update(plan[now])
    context_histories = {}
    raw_start = start - max(cfg["warmup_hours"], cfg["context_warmup_hours"]) * HOUR
    for symbol in sorted(needed):
        prefix = provider.history(symbol, "1h", raw_start, scan_start)
        tail = histories.get(symbol, CandleHistory([]))
        full = object.__new__(CandleHistory)
        full.columns = [a + b for a, b in zip(prefix.columns, tail.columns)]
        context_histories[symbol] = full
    context = CanonicalContext(context_histories, start)
    strategy_settings = edge.institutional_settings(
        cfg["variants"][0]["paper_settings"]
    )
    strategy = InstitutionalStrategy(strategy_settings)
    population = sum(len(plan[now]) for now in range(start, end, HOUR))
    print(
        f"{year} canonical Top20 rows planned: {population}; {len(needed)} context symbols",
        flush=True,
    )
    history, state, count, matches = defaultdict(list), {}, 0, set()
    files = {}
    with ExitStack() as stack:
        for symbol in sorted(needed):
            path = stage / (symbol + ".jsonl.gz")
            raw = stack.enter_context(path.open("wb"))
            gz = stack.enter_context(
                gzip.GzipFile(
                    filename="", fileobj=raw, mode="wb", compresslevel=3, mtime=0
                )
            )
            files[symbol] = stack.enter_context(
                io.TextIOWrapper(gz, encoding="utf-8", newline="\n")
            )
        month = None
        for now in range(scan_start, end, HOUR):
            markets = [universe.market(symbol, now) for symbol in plan[now]]
            snapshot = frame(
                universe, settings, now, history, score, features, markets=markets
            )
            history = trim(history, now)
            if now < start:
                continue
            rows, watch = snapshot.rows(), snapshot.watch()
            for rank, row in enumerate(rows, 1):
                if not row["auto_eligible"] or row["symbol"] not in plan[now]:
                    raise ValueError(
                        "Held-only/companion row contaminated Top20 population"
                    )
                audit = context.context(row["symbol"], now, strategy_settings, [])
                if audit["correlation"]["blocked"] or audit["correlation"]["pairs"]:
                    raise ValueError(
                        "Portfolio correlation leaked into asset eligibility"
                    )
                c = market_context(row, watch, now, audit, strategy_settings)
                signal = strategy.evaluate(c, False)
                record = row_record(
                    row, watch, now, c, strategy_settings, signal, year, rank, state
                )
                key = record["symbol"], record["timestamp"]
                if key in known:
                    expected = known[key]
                    if not record["final_asset_eligible"]:
                        raise ValueError(
                            "Known source BUY failed asset eligibility: " + repr(key)
                        )
                    for k in (
                        "market_score",
                        "delta_1h",
                        "delta_4h",
                        "delta_24h",
                        "relative_volume",
                        "rsi",
                        "atr_pct",
                        "range_position",
                        "proposed_entry_reference",
                        "original_stop_reference",
                    ):
                        a, b = record[k], expected[k]
                        if (a is None) != (b is None) or (
                            a is not None
                            and not math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-9)
                        ):
                            raise ValueError(
                                "Prior candidate feature mismatch: "
                                + repr((key, k, a, b))
                            )
                    matches.add(key)
                files[row["symbol"]].write(
                    json.dumps(
                        record, sort_keys=True, separators=(",", ":"), allow_nan=False
                    )
                    + "\n"
                )
                count += 1
            stamp = edge.iso(now)[:7]
            if stamp != month:
                month = stamp
                print(
                    f"{year} feature clock {edge.iso(now)} rows={count} known_BUY_matches={len(matches)}",
                    flush=True,
                )
    if count != population or matches != set(known):
        raise ValueError("Top20 / prior BUY population mismatch")
    done = dict(
        year=year,
        feature_signature=signature,
        source_fingerprint=source_fingerprint,
        count=count,
        known_buy_candidates_verified=len(matches),
        pinned_objects_used=provider.used.copy(),
        files={
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(stage.glob("*.jsonl.gz"))
        },
    )
    marker.write_bytes(edge.encoded(done))
    InstitutionalContext.series.cache_clear()
    InstitutionalContext.trend.cache_clear()
    InstitutionalContext.returns.cache_clear()
    return stage, done


def unavailable_single(row):
    if not row["single_failure"]:
        return False
    gate = row["single_failure_gate"]
    availability = {
        "DELTA": "delta_available",
        "1H_TREND": "trend_1h_available",
        "4H_TREND": "trend_4h_available",
        "1D_TREND": "trend_1d_available",
        "BTC_REGIME": "btc_regime_available",
    }
    return gate in availability and not row[availability[gate]]


class Bag:
    def __init__(self):
        self.n = 0
        self.symbols, self.days = set(), set()
        self.episodes = set()
        self.labels = {k: Counter() for k in LABEL_KEYS}
        self.mfe72, self.mfe168, self.mae72, self.scores = (
            array("d") for _ in range(4)
        )
        self.unavailable_single = 0

    def add(self, row):
        self.n += 1
        self.symbols.add(row["symbol"])
        self.days.add(row["symbol_day_id"])
        self.episodes.add(row["eligibility_episode_id"])
        label = row["POST_HOC_FUTURE_LABEL"]
        for k in LABEL_KEYS:
            self.labels[k][label.get(k)] += 1
        for field, values in [
            ("mfe_72h_r", self.mfe72),
            ("mfe_168h_r", self.mfe168),
            ("mae_72h_r", self.mae72),
        ]:
            value = label.get(field)
            if edge.finite(value):
                values.append(value)
        self.scores.append(row["market_score"])
        self.unavailable_single += unavailable_single(row)

    def summary(self):
        result = dict(
            count=self.n,
            unique_symbols=len(self.symbols),
            symbol_days=len(self.days),
            eligibility_episodes=len(self.episodes),
            mfe_72h_count=len(self.mfe72),
            mfe_168h_count=len(self.mfe168),
            mfe_72h_coverage=len(self.mfe72) / self.n if self.n else None,
            mfe_168h_coverage=len(self.mfe168) / self.n if self.n else None,
            median_72h_mfe_r=edge.quantile(self.mfe72, 0.5),
            median_168h_mfe_r=edge.quantile(self.mfe168, 0.5),
            median_72h_mae_r=edge.quantile(self.mae72, 0.5),
            score_median=edge.quantile(self.scores, 0.5),
            score_q1=edge.quantile(self.scores, 0.25),
            score_q3=edge.quantile(self.scores, 0.75),
            unavailable_single_failure_count=self.unavailable_single,
        )
        for k, counts in self.labels.items():
            known = counts[True] + counts[False]
            result[k + "_true"] = counts[True]
            result[k + "_false"] = counts[False]
            result[k + "_unknown"] = counts[None]
            result[k + "_known"] = known
            result[k + "_rate"] = counts[True] / known if known else None
            result[k + "_coverage"] = known / self.n if self.n else None
        return result


def memberships(row):
    groups = [
        "ALL_TOP20_ELIGIBLE_DATA_ROWS",
        "CURRENT_BUY_ELIGIBLE"
        if row["final_asset_eligible"]
        else "CURRENT_NOT_ELIGIBLE",
    ]
    if row["single_failure"]:
        gate = row["single_failure_gate"]
        groups.append("ONLY_" + gate + "_FAIL")
        if gate == "RSI":
            groups.append(row["single_failure_population"])
        availability = "UNAVAILABLE" if unavailable_single(row) else "AVAILABLE"
        groups.append("ONLY_" + gate + "_FAIL_" + availability)
    elif row["failure_count"] > 1:
        groups.append("MULTI_FAILURE")
    for gate, field in PASS_FIELD.items():
        groups.append("PASS_" + gate if row[field] else "FAIL_" + gate)
    groups.append("COMBINATION:" + row["canonical_failure_combination"])
    return groups


def binary_auc(a, b):
    p1, n1 = a.labels["hit_5r_before_stop"][True], a.labels["stop_before_1r"][True]
    p0, n0 = b.labels["hit_5r_before_stop"][True], b.labels["stop_before_1r"][True]
    return (
        (p1 * (n0 + 0.5 * n1) + 0.5 * p0 * n0) / ((p1 + p0) * (n1 + n0))
        if p1 + p0 and n1 + n0
        else None
    )


def compare(a, b):
    sa, sb = a.summary(), b.summary()
    auc = edge.auc(a.mfe72, b.mfe72)

    def delta(k):
        return sa[k] - sb[k] if sa[k] is not None and sb[k] is not None else None

    result = dict(
        a_count=a.n,
        b_count=b.n,
        a=sa,
        b=sb,
        rate_5r_difference=delta("hit_5r_before_stop_rate"),
        stop_before_1r_difference=delta("stop_before_1r_rate"),
        cliffs_delta_mfe72=2 * auc - 1 if auc is not None else None,
        mfe72_distribution_auc=auc,
        gate_binary_auc_5r_vs_stop=binary_auc(a, b),
        known_5r_coverage_difference=delta("hit_5r_before_stop_coverage"),
        adequate_sample=all(
            sa[k] >= MIN_COUNT and sb[k] >= MIN_COUNT
            for k in (
                "hit_5r_before_stop_known",
                "stop_before_1r_known",
                "mfe_72h_count",
            )
        ),
    )
    result["balanced_coverage"] = all(
        sa[k] is not None
        and sb[k] is not None
        and min(sa[k], sb[k]) >= 0.8
        and abs(sa[k] - sb[k]) <= MAX_COVERAGE_DIFFERENCE
        for k in (
            "hit_5r_before_stop_coverage",
            "stop_before_1r_coverage",
            "mfe_72h_coverage",
        )
    )
    return result


def direction(comparison):
    r = comparison
    if not r["adequate_sample"] or any(
        r[k] is None
        for k in (
            "rate_5r_difference",
            "stop_before_1r_difference",
            "cliffs_delta_mfe72",
        )
    ):
        return "UNCLEAR"
    if (
        r["rate_5r_difference"] >= MIN_RATE_DIFFERENCE
        and r["stop_before_1r_difference"] <= -MIN_RATE_DIFFERENCE
        and r["cliffs_delta_mfe72"] >= MIN_CLIFF
    ):
        return "HELPFUL"
    if (
        r["rate_5r_difference"] <= -MIN_RATE_DIFFERENCE
        and r["stop_before_1r_difference"] >= MIN_RATE_DIFFERENCE
        and r["cliffs_delta_mfe72"] <= -MIN_CLIFF
    ):
        return "HARMFUL"
    return "UNCLEAR"


def label_year(year, stage, provider, out):
    bags = defaultdict(Bag)
    # One symbol's archive/trees/feature rows at a time, no annual row dict list.
    count = 0
    target = out / f"top20-opportunity-dataset-{year}.csv"
    with target.open("w", newline="") as dest:
        writer = None
        files = sorted(stage.glob("*.jsonl.gz"))
        for i, path in enumerate(files):
            with gzip.open(path, "rt") as stream:
                rows = [json.loads(line) for line in stream]
            if not rows:
                continue
            symbol = rows[0]["symbol"]
            print(
                f"{year} pinned future labels {i + 1}/{len(files)} {symbol} rows={len(rows)}",
                flush=True,
            )
            end = ms("2025-01-01T00:00:00Z" if year == 2024 else "2026-01-01T00:00:00Z")
            history = provider.history(symbol, "1m", ms(rows[0]["timestamp"]), end)
            labels = edge.FutureLabels(history, end)
            for row in rows:
                if row["original_stop_reference"] is None:
                    row["POST_HOC_FUTURE_LABEL"] = dict(
                        label_type="POST_HOC_FUTURE_LABEL",
                        censor_reason="INVALID_REFERENCE_STOP_DISTANCE",
                    )
                else:
                    row["POST_HOC_FUTURE_LABEL"] = labels.get(
                        ms(row["timestamp"]),
                        row["proposed_entry_reference"],
                        row["original_stop_reference"],
                    )
                flat = {k: v for k, v in row.items() if k != "POST_HOC_FUTURE_LABEL"}
                for k in (
                    "label_type",
                    "price_r_unit",
                    "observation_end",
                    "censor_reason",
                    "time_to_stop_hours",
                    "stop_time",
                    "stop_before_1r",
                ):
                    flat["POST_HOC_FUTURE_LABEL." + k] = row[
                        "POST_HOC_FUTURE_LABEL"
                    ].get(k)
                for h in edge.HORIZONS:
                    for key in (f"mfe_{h}h_r", f"mae_{h}h_r", f"horizon_{h}h_status"):
                        flat["POST_HOC_FUTURE_LABEL." + key] = row[
                            "POST_HOC_FUTURE_LABEL"
                        ].get(key)
                for n in edge.LEVELS:
                    for key in (
                        f"hit_{n}r_before_stop",
                        f"time_to_{n}r_hours",
                        f"hit_{n}r_time",
                    ):
                        flat["POST_HOC_FUTURE_LABEL." + key] = row[
                            "POST_HOC_FUTURE_LABEL"
                        ].get(key)
                if writer is None:
                    writer = csv.DictWriter(dest, sorted(flat), lineterminator="\n")
                    writer.writeheader()
                writer.writerow(
                    {
                        k: json.dumps(v, sort_keys=True, allow_nan=False)
                        if isinstance(v, (dict, list))
                        else v
                        for k, v in flat.items()
                    }
                )
                for scope in (
                    ("raw_hourly", "episode_first")
                    if row["eligibility_episode_first"]
                    else ("raw_hourly",)
                ):
                    for group in memberships(row):
                        bags[(scope, group)].add(row)
                count += 1
            del labels, history, rows
    print(f"{year} labeled rows {count}", flush=True)
    return bags, count


def describe(year, bags):
    populations = []
    gate_values = []
    singles = []
    combos = []
    score_distributions = []
    enrichment = []
    for scope in ("raw_hourly", "episode_first"):
        for group in (
            "ALL_TOP20_ELIGIBLE_DATA_ROWS",
            "CURRENT_BUY_ELIGIBLE",
            "CURRENT_NOT_ELIGIBLE",
            "MULTI_FAILURE",
        ):
            populations.append(
                dict(
                    year=year,
                    scope=scope,
                    population=group,
                    **bags[(scope, group)].summary(),
                )
            )
        current = bags[(scope, "CURRENT_BUY_ELIGIBLE")]
        for other in ("ALL_TOP20_ELIGIBLE_DATA_ROWS", "CURRENT_NOT_ELIGIBLE"):
            enrichment.append(
                dict(
                    year=year,
                    scope=scope,
                    a_population="CURRENT_BUY_ELIGIBLE",
                    b_population=other,
                    **compare(current, bags[(scope, other)]),
                )
            )
        for gate in GATES:
            gate_values.append(
                dict(
                    year=year,
                    scope=scope,
                    gate=gate,
                    dependent_gate=gate == "WATCH",
                    a_population="PASS_" + gate,
                    b_population="FAIL_" + gate,
                    **compare(
                        bags[(scope, "PASS_" + gate)], bags[(scope, "FAIL_" + gate)]
                    ),
                )
            )
        for gate in GATES + ("RSI_LOW", "RSI_HIGH"):
            for availability in (
                ("ALL", "AVAILABLE", "UNAVAILABLE") if gate in GATES else ("ALL",)
            ):
                name = (
                    "ONLY_"
                    + gate
                    + "_FAIL"
                    + ("_" + availability if availability != "ALL" else "")
                )
                singles.append(
                    dict(
                        year=year,
                        scope=scope,
                        gate=gate,
                        availability=availability,
                        independent_production_gate=gate in GATES,
                        dependent_gate=gate == "WATCH",
                        a_population="CURRENT_BUY_ELIGIBLE",
                        b_population=name,
                        **compare(current, bags[(scope, name)]),
                    )
                )
        for (s, group), bag in list(bags.items()):
            if s != scope:
                continue
            if group.startswith("COMBINATION:"):
                combos.append(
                    dict(
                        year=year,
                        scope=scope,
                        canonical_failure_combination=group.split(":", 1)[1],
                        **bag.summary(),
                    )
                )
            if group in {
                "ALL_TOP20_ELIGIBLE_DATA_ROWS",
                "CURRENT_BUY_ELIGIBLE",
                "ONLY_SCORE_FAIL",
                "PASS_SCORE",
                "FAIL_SCORE",
            }:
                score_distributions.append(
                    dict(
                        year=year,
                        scope=scope,
                        population=group,
                        count=bag.n,
                        minimum=min(bag.scores, default=None),
                        q1=edge.quantile(bag.scores, 0.25),
                        median=edge.quantile(bag.scores, 0.5),
                        q3=edge.quantile(bag.scores, 0.75),
                        maximum=max(bag.scores, default=None),
                    )
                )
    return dict(
        populations=populations,
        enrichment=enrichment,
        gate_values=gate_values,
        single_failures=singles,
        failure_combinations=combos,
        score_distributions=score_distributions,
    )


def conclusions(analyses):
    singles = [r for y in sorted(analyses) for r in analyses[y]["single_failures"]]
    classifications = []
    for gate in GATES:
        rows = [r for r in singles if r["gate"] == gate and r["availability"] == "ALL"]
        expected = {
            (y, s) for y in (2024, 2025) for s in ("raw_hourly", "episode_first")
        }
        complete = {(r["year"], r["scope"]) for r in rows} == expected
        dirs = [direction(r) for r in rows]
        balanced = complete and all(r["balanced_coverage"] for r in rows)
        tag = (
            "GATE_APPEARS_HELPFUL"
            if complete and all(d == "HELPFUL" for d in dirs)
            else "GATE_MAY_BE_HARMFUL"
            if complete and all(d == "HARMFUL" for d in dirs)
            else "GATE_VALUE_UNCLEAR"
        )
        eligible_for_hypothesis = (
            tag == "GATE_MAY_BE_HARMFUL"
            and balanced
            and all(r["b"]["unavailable_single_failure_count"] == 0 for r in rows)
        )
        classifications.append(
            dict(
                gate=gate,
                classification=tag,
                dependent_gate=gate == "WATCH",
                episode_first_does_not_reverse=complete
                and len(set(dirs)) == 1
                and dirs[0] != "UNCLEAR",
                balanced_coverage=balanced,
                interesting_for_later_frozen_ablation_hypothesis=eligible_for_hypothesis,
                comparisons=[
                    dict(
                        year=r["year"],
                        scope=r["scope"],
                        direction=direction(r),
                        a_count=r["a_count"],
                        b_count=r["b_count"],
                        rate_5r_difference=r["rate_5r_difference"],
                        stop_before_1r_difference=r["stop_before_1r_difference"],
                        cliffs_delta_mfe72=r["cliffs_delta_mfe72"],
                    )
                    for r in rows
                ],
            )
        )
    candidates = [
        r["gate"]
        for r in classifications
        if r["interesting_for_later_frozen_ablation_hypothesis"]
    ]
    candidate = candidates[0] if len(candidates) == 1 else "NONE"
    e = [
        r
        for y in sorted(analyses)
        for r in analyses[y]["enrichment"]
        if r["b_population"] == "ALL_TOP20_ELIGIBLE_DATA_ROWS"
    ]
    complete = len(e) == 4
    balanced = complete and all(
        r["balanced_coverage"] and r["adequate_sample"] for r in e
    )
    if balanced and all(direction(r) == "HELPFUL" for r in e):
        category = "CURRENT_ELIGIBILITY_SHOWS_USEFUL_ENRICHMENT"
    elif (
        balanced
        and all(direction(r) != "HELPFUL" for r in e)
        and all(
            r["rate_5r_difference"] <= MIN_RATE_DIFFERENCE
            and r["cliffs_delta_mfe72"] <= MIN_CLIFF
            for r in e
        )
    ):
        category = "CURRENT_ELIGIBILITY_SHOWS_LITTLE_ENRICHMENT"
    else:
        category = "CURRENT_ELIGIBILITY_MIXED"

    def strongest(tag):
        rows = [r for r in classifications if r["classification"] == tag]
        if not rows:
            return "NONE"
        return max(
            rows,
            key=lambda r: min(abs(v["cliffs_delta_mfe72"]) for v in r["comparisons"]),
        )["gate"]

    return dict(
        category=category,
        ablation_candidate_gate=candidate,
        gate_classifications=classifications,
        strongest_helpful_gate=strongest("GATE_APPEARS_HELPFUL"),
        strongest_potentially_harmful_gate=strongest("GATE_MAY_BE_HARMFUL"),
        overall_coverage_balanced=balanced,
        qualified_ablation_gates=candidates,
        ablation_selection="Exactly one qualifying gate or NONE if zero/multiple; no automatic ablation",
    )


def bags_from_csv(path):
    """Pure descriptive reaggregation of immutable exported observations."""
    bags = defaultdict(Bag)
    count = 0
    with path.open(newline="") as stream:
        for raw in csv.DictReader(stream):
            row = {
                k: raw[k]
                for k in (
                    "symbol",
                    "symbol_day_id",
                    "eligibility_episode_id",
                    "canonical_failure_combination",
                )
            }
            row.update(
                market_score=float(raw["market_score"]),
                failure_count=int(raw["failure_count"]),
                single_failure_gate=raw["single_failure_gate"] or None,
                single_failure_population=raw["single_failure_population"] or None,
                strategy_reasons=json.loads(raw["strategy_reasons"]),
            )
            for key in set(PASS_FIELD.values()) | {
                "single_failure",
                "final_asset_eligible",
                "eligibility_episode_first",
                "delta_available",
                "trend_1h_available",
                "trend_4h_available",
                "trend_1d_available",
                "btc_regime_available",
            }:
                if raw[key] not in {"True", "False"}:
                    raise ValueError("Invalid boolean in verified dataset: " + key)
                row[key] = raw[key] == "True"
            label = {}
            for key in LABEL_KEYS:
                value = raw["POST_HOC_FUTURE_LABEL." + key]
                if value not in {"True", "False", ""}:
                    raise ValueError("Invalid future label boolean")
                label[key] = None if value == "" else value == "True"
            for key in ("mfe_72h_r", "mfe_168h_r", "mae_72h_r"):
                value = raw["POST_HOC_FUTURE_LABEL." + key]
                label[key] = float(value) if value else None
            row["POST_HOC_FUTURE_LABEL"] = label
            for scope in (
                ("raw_hourly", "episode_first")
                if row["eligibility_episode_first"]
                else ("raw_hourly",)
            ):
                for group in memberships(row):
                    bags[(scope, group)].add(row)
            count += 1
    return bags, count


def rebuild_reports(out):
    """No source reads: verify manifest, then summarize the same exported rows."""
    manifest = json.loads((out / "manifest.json").read_text())
    for name, item in manifest["files"].items():
        if hashlib.sha256((out / name).read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Existing export SHA mismatch: " + name)
    validation = json.loads((out / "source-validation.json").read_text())
    if (
        not validation["source_fingerprint_unchanged"]
        or validation["before"] != validation["after"]
        or validation["database_writes"] != 0
    ):
        raise ValueError("Completed verified source extraction required")
    old = json.loads((out / "summary.json").read_text())
    if old["metadata"]["source_runs"] != {str(y): r for y, r in edge.RUNS.items()}:
        raise ValueError("Unexpected source runs")
    analyses = {}
    for year_text, check in sorted(validation["checks"].items()):
        year = int(year_text)
        bags, count = bags_from_csv(out / f"top20-opportunity-dataset-{year}.csv")
        if count != check["top20_count"]:
            raise ValueError("Dataset record count mismatch")
        analyses[year] = describe(year, bags)
        del bags
    render(analyses, out, old["metadata"])
    (out / "methodology.json").write_bytes(edge.encoded(methodology()))
    validation["methodology"] = methodology()
    (out / "source-validation.json").write_bytes(edge.encoded(validation))
    bundle = package(out)
    print(
        "Verified CSV-only reports:",
        bundle,
        hashlib.sha256(bundle.read_bytes()).hexdigest(),
        flush=True,
    )
    return bundle


def csv_flat(row):
    flat = {k: v for k, v in row.items() if k not in {"a", "b"}}
    for side in ("a", "b"):
        if side in row:
            flat.update({side + "_" + k: v for k, v in row[side].items()})
    return flat


def render(analyses, out, metadata):
    result = conclusions(analyses)
    files = {
        "eligibility-enrichment.csv": "enrichment",
        "gate-value-analysis.csv": "gate_values",
        "single-failure-analysis.csv": "single_failures",
        "failure-combinations.csv": "failure_combinations",
        "score-distributions.csv": "score_distributions",
    }
    for filename, key in files.items():
        edge.write_csv(
            out / filename,
            [csv_flat(r) for y in sorted(analyses) for r in analyses[y][key]],
        )
    edge.write_csv(
        out / "episode-sensitivity.csv",
        [
            csv_flat(r)
            for y in sorted(analyses)
            for r in analyses[y]["populations"] + analyses[y]["single_failures"]
            if r["scope"] == "episode_first"
        ],
    )
    edge.write_csv(
        out / "cross-year-gate-consistency.csv", result["gate_classifications"]
    )
    summary = dict(
        research="Institutional Eligibility Gate Diagnostic v1",
        role="READ_ONLY_EXPLORATION_2024_AND_OPTIONAL_2025",
        **result,
        metadata=metadata,
        analyses=analyses,
        new_replay=False,
        production_behavior_changed=False,
        database_writes=0,
        deployment=False,
    )
    (out / "summary.json").write_bytes(edge.encoded(summary))
    report(summary, out)
    return summary


def methodology():
    return dict(
        base=BASE,
        source_runs=edge.RUNS,
        role="2024 and 2025 EXPLORATION; no validation claim",
        population="Every fresh canonical shared historical Top20 row per UTC hour; no held-only companion rows",
        evaluator="UNCHANGED InstitutionalStrategy.evaluate(context, has_position=False); empty positions in unchanged InstitutionalContext.context; no RiskManager, ledger, order, fill or worker",
        predicates="Source reasons authoritative; assert derived predicate failures exactly match evaluator failures. RSI low/high are descriptive subdivisions of one RSI_OUTSIDE_RANGE gate. Context availability is a descriptor, not an extra independent gate.",
        delta="Exact institutional condition: missing blocks when require_delta; any present delta <= min_delta also blocks (boundary is strict >0 in this config). No alternate threshold.",
        watch="DEPENDENT_GATE: unchanged select_watchlist Top5 after score>=auto_min_score, relative volume/ATR bounds, ordered by score + selection_momentum_weight*max(0,delta_riser), symbol tie break. Never an independent causal feature.",
        reference_stop="Same Decimal price - ATR * stop_atr_multiplier expression for every row where ATR>0 and stop>0; BUY parity asserted. For HOLD this is an explicitly diagnostic reference, not a strategy stop/order. Invalid construction => labels unavailable, never fabricated.",
        future_labels="UNCHANGED institutional_signal_edge.FutureLabels; POST_HOC_FUTURE_LABEL namespace; price R, not net trade R. Four complete 6/24/72/168h windows. Barrier until original stop/first missing minute/run end. Gap open first; otherwise same-minute stop wins. No fallback/download.",
        source_coverage="Only exact source-run pinned archives. Coverage can depend on the historical strategy/portfolio wanted-symbol path. Unknown labels are never false. Each denominator, coverage and group gap is exported; incomplete differential coverage prevents ablation nomination.",
        episodes="Same symbol, consecutive hourly Top20 rows, same final asset-eligibility bool. A gap or bool change starts new episode. HOLD reason changes alone do not start an episode. Episode-first is descriptive, not fresh-setup admission.",
        statistics="Cliff and distribution AUC compare complete 72h MFE distributions. Separate binary AUC uses gate/eligibility indicator for +5R-vs-stop-before-1R cases. No fitted model/cutoff search; no causal interpretation.",
        frozen_descriptive_rules=dict(
            minimum_observed_count_each_group=MIN_COUNT,
            minimum_rate_difference=MIN_RATE_DIFFERENCE,
            minimum_cliff=MIN_CLIFF,
            helpful="+5R difference>=.02, stop difference<=-.02, Cliff72>=.147 in raw AND episode-first BOTH years; harmful is inverse; otherwise unclear",
            balanced_coverage="known +5R, stop and complete 72h MFE coverage>=.8 both groups and absolute coverage gap<=.10",
            ablation="Only harmful single-failure comparison supported in both years/scopes, balanced coverage, no unavailable-data-only failure; exactly one qualifying gate or NONE. No ablation implemented.",
            category="A only consistent helpful enrichment with balanced coverage; C only consistently no helpful/nontrivial enrichment with balanced coverage; B mixed or coverage-limited",
        ),
        no_lookahead="Every feature/coin/BTC trend source close timestamp precedes decision. Closed bucket memoization identical to direct source context. Only source-required prehistory seeds; no 2023 research cohort or 2026 archive data.",
        determinism="Stable chronological per-symbol feature JSONL; stable CSV ordering; sorted manifest SHA256; ZIP fixed date/member ordering/permissions. Diagnostic-only resumable feature files, never replay checkpoints.",
    )


def package(out):
    files = sorted(
        p
        for p in out.iterdir()
        if p.is_file()
        and p.suffix in {".json", ".csv", ".md"}
        and p.name != "manifest.json"
    )
    manifest = dict(
        base=BASE,
        research_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        source_runs=edge.RUNS,
        files={
            p.name: dict(
                bytes=p.stat().st_size,
                sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
            )
            for p in files
        },
    )
    (out / "manifest.json").write_bytes(edge.encoded(manifest))
    bundle = out / "institutional-eligibility-gate-diagnostic-v1.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(files + [out / "manifest.json"]):
            info = zipfile.ZipInfo(path.name, date_time=(2024, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, path.read_bytes())
    return bundle


def report(summary, out):
    text = [
        "# Institutional Eligibility Gate Diagnostic v1",
        "",
        "READ-ONLY EXPLORATION. No strategy/filter/ranking/replay/deployment.",
        "",
        "Category: **" + summary["category"] + "**",
        "",
        "ablation_candidate_gate: **" + summary["ablation_candidate_gate"] + "**",
        "",
        "## Population and enrichment",
        "",
        "Rates exclude UNKNOWN labels; coverage and denominator counts are in the CSV/JSON. Raw hourly rows overlap and are not independent trades.",
        "",
        "| Year | Scope | Population | Count | Known +5R coverage | +1R | +2R | +5R | Stop before +1R | Median 72h MFE R |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    def fmt(v, percent=False):
        return (
            "UNAVAILABLE" if v is None else f"{v * 100:.2f}%" if percent else f"{v:.5f}"
        )

    for y in sorted(summary["analyses"]):
        for r in summary["analyses"][y]["populations"]:
            text.append(
                f"|{y}|{r['scope']}|{r['population']}|{r['count']}|{fmt(r['hit_5r_before_stop_coverage'], True)}|{fmt(r['hit_1r_before_stop_rate'], True)}|{fmt(r['hit_2r_before_stop_rate'], True)}|{fmt(r['hit_5r_before_stop_rate'], True)}|{fmt(r['stop_before_1r_rate'], True)}|{fmt(r['median_72h_mfe_r'])}|"
            )
    text += [
        "",
        "## Single-failure comparisons (primary gate diagnostic)",
        "",
        "Current eligible minus ONLY_FAIL; Cliff uses 72h MFE. Production gate identities are preserved. RSI_LOW/HIGH are subdivisions, not extra independent gates.",
        "",
        "| Year | Scope | Gate | Eligible N | Only-fail N | +5R difference | Stop difference | Cliff72 | Known coverage balanced |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for y in sorted(summary["analyses"]):
        for r in summary["analyses"][y]["single_failures"]:
            if r["availability"] == "ALL":
                text.append(
                    f"|{y}|{r['scope']}|{r['gate']}|{r['a_count']}|{r['b_count']}|{fmt(r['rate_5r_difference'])}|{fmt(r['stop_before_1r_difference'])}|{fmt(r['cliffs_delta_mfe72'])}|{r['balanced_coverage']}|"
                )
    text += [
        "",
        "## Cross-year gate classifications",
        "",
        "| Gate | Classification | Dependent gate | Eligible for frozen hypothesis |",
        "|---|---|---|---|",
    ]
    for r in summary["gate_classifications"]:
        text.append(
            f"|{r['gate']}|{r['classification']}|{r['dependent_gate']}|{r['interesting_for_later_frozen_ablation_hypothesis']}|"
        )
    text += [
        "",
        "Strongest helpful gate: " + summary["strongest_helpful_gate"],
        "",
        "Strongest potentially harmful gate: "
        + summary["strongest_potentially_harmful_gate"],
        "",
        "## Interpretation limits",
        "",
        "2024 and 2025 are reused exploration, never clean validation. Pass/fail gate groups contain correlated co-failures; only the single-failure comparisons condition on other production gates passing, and even these are not causal experiments. Watch is explicitly DEPENDENT_GATE. Unknown/incomplete minute labels are reported, never counted as failure. The original pinned archive availability follows source-run wanted symbols and can select the observed subset; differential coverage constrains enrichment and ablation claims. Episode-first sensitivity uses the first of each binary eligibility-state episode, not the best future observation or a gate-specific episode. Numerical/censoring conventions and frozen descriptive classification criteria are in methodology.json.",
        "",
        "The prior BUY-only Market Score AUC<0.5 was not used to infer that the score threshold is harmful. Only the existing production score boundary is examined. No alternative EMA/RSI/relative-volume/score/delta threshold, fitted model, or new entry rule was tested.",
        "",
        "new replay: NO; production behavior changed: NO; database writes: 0; deployment: NO; migration: NO",
    ]
    (out / "report.md").write_text("\n".join(text) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--reports-only",
        action="store_true",
        help="Reaggregate manifest-verified exported CSVs only; no source DB access",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    out = (
        args.output or root / "artifacts/institutional-eligibility-gate-diagnostic-v1"
    ).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sys.addaudithook(edge.no_network)
    if args.reports_only:
        rebuild_reports(out)
        return
    prior = root / "artifacts/institutional-signal-edge-diagnostic-v1"
    old = json.loads((prior / "source-validation.json").read_text())
    manifest = json.loads((prior / "manifest.json").read_text())
    for name in (
        "source-validation.json",
        "candidate-dataset-2024.json",
        "candidate-dataset-2025.json",
        "2025-source-comparability.json",
    ):
        if (
            hashlib.sha256((prior / name).read_bytes()).hexdigest()
            != manifest["files"][name]["sha256"]
        ):
            raise ValueError("Prior diagnostic source file SHA mismatch: " + name)
    print("Read-only source fingerprint before", flush=True)
    with edge.read_only(root / "data/replay.db") as db:
        before = edge.source_fingerprint(db, list(edge.RUNS.values()))
        if {k: v for k, v in before.items() if k != "all_run_ids"} != {
            k: v for k, v in old["after"].items() if k != "all_run_ids"
        }:
            raise ValueError("Source fingerprint changed since prior diagnostic")
        sources = {y: edge.load_source(db, y) for y in (2024, 2025)}
    comparison = edge.baseline_comparability(sources[2024], sources[2025], root)
    usable = comparison["usable"] and old["comparability"]["usable"]
    (out / "2025-source-comparability.json").write_bytes(
        edge.encoded(comparison | dict(usable=usable))
    )
    (out / "methodology.json").write_bytes(edge.encoded(methodology()))
    analyses, providers, checks, cache_before = {}, {}, {}, {}
    for year in (2024, 2025):
        if year == 2025 and not usable:
            continue
        known_rows = json.loads((prior / f"candidate-dataset-{year}.json").read_text())
        known = {(r["symbol"], r["timestamp"]): r for r in known_rows}
        del known_rows
        provider = edge.Archives(
            root / "data/replay-cache",
            sources[year]["metadata"]["historical_data_revision"],
        )
        providers[year] = provider
        cache_before[year] = provider.fingerprint()
        stage, done = build_features(
            sources[year], provider, out, edge.digest(before), known
        )
        checks[year] = dict(
            top20_count=done["count"],
            known_buy_candidates_verified=done["known_buy_candidates_verified"],
            feature_signature=done["feature_signature"],
        )
        bags, count = label_year(year, stage, provider, out)
        if count != done["count"]:
            raise ValueError("Missing feature rows in label export")
        analyses[year] = describe(year, bags)
        del bags
    meta = dict(
        base=BASE,
        source_runs=edge.RUNS,
        source_sha=edge.SOURCE_SHA,
        source_2025_usable=usable,
        prior_manifest_sha256=hashlib.sha256(
            (prior / "manifest.json").read_bytes()
        ).hexdigest(),
        source_config_hashes={
            y: sources[y]["metadata"]["config_hash"] for y in sources
        },
        source_market_context="ISOLATED_TOPN_V1",
        checks=checks,
    )
    summary = render(analyses, out, meta)
    print("Read-only source fingerprint after", flush=True)
    with edge.read_only(root / "data/replay.db") as db:
        after = edge.source_fingerprint(db, list(edge.RUNS.values()))
    if before != after:
        raise ValueError("Immutable source data or run inventory changed")
    for year, provider in providers.items():
        if cache_before[year] != provider.fingerprint():
            raise ValueError("Source pinned metadata changed")
        provider.verify_used()
        if any("-2026-" in key for key in provider.used):
            raise ValueError("2026 archive contamination")
    validation = dict(
        before=before,
        after=after,
        source_fingerprint_unchanged=True,
        prior_diagnostic_source_fingerprint_matches=True,
        read_only_uri=True,
        query_only=True,
        no_network=True,
        new_replay=False,
        database_writes=0,
        production_behavior_changed=False,
        deployment=False,
        checks=checks,
        cache_metadata_before=cache_before,
        pinned_objects_used={y: p.used for y, p in providers.items()},
        source_2025_comparability=comparison,
        methodology=methodology(),
    )
    (out / "source-validation.json").write_bytes(edge.encoded(validation))
    bundle = package(out)
    print(
        json.dumps(
            dict(
                category=summary["category"],
                ablation_candidate_gate=summary["ablation_candidate_gate"],
                checks=checks,
                bundle=str(bundle),
                bytes=bundle.stat().st_size,
                sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
            ),
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
