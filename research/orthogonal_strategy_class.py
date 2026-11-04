"""Two frozen research-only daily classes. No production engine, API or database."""

import argparse
import csv
import io
import json
import math
import statistics
import zipfile
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path

from research import low_turnover_benchmark as source

BASE = "61a9de499c7e5477d6c7ce1008eddb90cd33957d"
NAME = "orthogonal-strategy-class-v1"
SOURCE_SHA = "3d1547e474e4e490dde81fa5a2860839601199e277d14f22086b6c66ada8d899"
DAY = source.DAY
D = Decimal
STRATEGIES = {"S1": "BREAKOUT", "S2": "MEAN_REVERSION"}
PORTFOLIOS = {
    "BTC_ONLY": ("BTCUSDT",),
    "ETH_ONLY": ("ETHUSDT",),
    "EQUAL_WEIGHT": source.SYMBOLS,
}
PERIODS = source.PERIODS
MIN_EPISODES = 5
ZERO = D(0)
ONE = D(1)


@dataclass(frozen=True)
class Candle:
    start: int
    end: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def source_proof(root):
    folder = root / "artifacts" / source.NAME
    path = folder / (source.NAME + ".zip")
    if source.sha(path.read_bytes()) != SOURCE_SHA:
        raise ValueError("REFERENCE_SOURCE_CHANGED")
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:
            raise ValueError("REFERENCE_CRC_FAILURE")
        manifest = json.loads(z.read("manifest.json"))
        for n, pin in manifest["files"].items():
            if (
                source.sha(z.read(n)) != pin["sha256"]
                or source.sha((folder / n).read_bytes()) != pin["sha256"]
            ):
                raise ValueError("REFERENCE_MANIFEST_FAILURE")
    refs = json.loads((folder / "current-strategy-reference.json").read_text())
    for ref in refs:
        if source.sha(Path(ref["source"]).read_bytes()) != ref["source_sha256"]:
            raise ValueError("INSTITUTIONAL_REFERENCE_CHANGED")
    return dict(
        benchmark_bundle_sha256=SOURCE_SHA,
        benchmark_files={n: p["sha256"] for n, p in manifest["files"].items()},
        institutional_sources={r["source"]: r["source_sha256"] for r in refs},
    )


def load_ohlc(root):
    proof = source_proof(root)
    checked, manifest = source.load_data(root, capture=False)
    folder = root / "artifacts" / source.NAME
    if source.encoded(manifest) != (folder / "daily-data-manifest.json").read_bytes():
        raise ValueError("DAILY_SOURCE_CONTRACT_CHANGED")
    cache = root / "data/research-cache" / source.NAME
    result = {s: {} for s in source.SYMBOLS}
    for pin in manifest:
        record = json.loads(
            (cache / (source.sha(pin["url"].encode()) + ".json")).read_text()
        )
        body = bytes.fromhex(record["body_hex"])
        if source.sha(body) != pin["sha256"]:
            raise ValueError("DAILY_CHECKSUM_MISMATCH")
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            lines = list(csv.reader(io.StringIO(z.read(z.namelist()[0]).decode())))
        unit = 1000 if pin["month"] >= "2025-01" else 1
        for row in lines:
            start = int(row[0]) // unit
            bar = checked[pin["symbol"]][start]
            result[pin["symbol"]][start] = Candle(
                start, bar.end, bar.open, D(row[2]), D(row[3]), bar.close
            )
    return result, manifest, proof


def bars(history, dates, known_at):
    result = [history.get(t) for t in dates]
    if any(
        b is None or b.start % DAY or b.end != b.start + DAY - 1 or b.end >= known_at
        for b in result
    ):
        raise ValueError("MISSING_OR_UNCOMPLETED_HISTORY")
    return result


def observation(strategy, history, day, active, known_at):
    """Decision at completed day close; returns state only, never an order."""
    current = bars(history, [day], known_at)[0]
    if strategy == "S1":
        prior = bars(history, [day - k * DAY for k in range(1, 21)], known_at)
        upper = max(b.high for b in prior)
        lower = min(b.low for b in prior[:10])
        enter = current.close > upper
        exit_ = current.close < lower
        features = dict(
            prior20_high=str(upper),
            prior10_low=str(lower),
            decision_high_excluded=True,
            decision_low_excluded=True,
            oldest_entry_channel_day=source.iso(day - 20 * DAY),
            oldest_exit_channel_day=source.iso(day - 10 * DAY),
        )
    elif strategy == "S2":
        window = bars(history, [day - k * DAY for k in range(20)], known_at)
        mean = sum((b.close for b in window), ZERO) / 20
        variance = sum(((b.close - mean) ** 2 for b in window), ZERO) / 19
        std = variance.sqrt()
        lower = mean - 2 * std
        enter = current.close < lower
        exit_ = current.close >= mean
        features = dict(
            sma20=str(mean),
            sample_std20=str(std),
            lower_band=str(lower),
            ddof=1,
            oldest_window_day=source.iso(day - 19 * DAY),
        )
    else:
        raise ValueError("ONLY_S1_S2")
    after = not exit_ if active else enter
    return dict(
        decision_day=source.iso(day),
        source_close=source.iso(current.end),
        known_at=source.iso(known_at),
        source_close_ms=current.end,
        known_at_ms=known_at,
        close=str(current.close),
        active_before=active,
        active_after=after,
        entry_condition=enter,
        exit_condition=exit_,
        action="BUY"
        if after and not active
        else "SELL"
        if active and not after
        else "HOLD",
        **features,
    )


def schedule(data, strategy, start, end):
    if (
        strategy not in STRATEGIES
        or start < source.stamp("2024-01-01")
        or end > source.stamp("2026-01-01")
        or end <= start
    ):
        raise ValueError("OUTSIDE_FROZEN_SCOPE")
    with localcontext() as ctx:
        ctx.prec = 45
        active = {s: False for s in source.SYMBOLS}
        result = {}
        for t in range(start, end, DAY):
            result[t] = {}
            for symbol in source.SYMBOLS:
                row = observation(strategy, data[symbol], t - DAY, active[symbol], t)
                row.update(symbol=symbol, signal_id=f"{strategy}:{symbol}:{t}")
                result[t][symbol] = row
                active[symbol] = row["active_after"]
        return result


def weights_for(signals, assets):
    selected = [s for s in assets if signals[s]["active_after"]]
    return {s: ONE / len(selected) for s in selected}


class EpisodeLedger:
    """Asset episodes from first buy until fully flat; partial fills stay together."""

    def __init__(self, prefix):
        self.prefix = prefix
        self.open = {}
        self.closed = []
        self.count = 0

    def fill(self, symbol, side, quantity, fill_price, fee, slippage, t):
        notional = quantity * fill_price
        if side == "BUY":
            if symbol not in self.open:
                self.count += 1
                self.open[symbol] = dict(
                    episode_id=f"{self.prefix}:{self.count}",
                    symbol=symbol,
                    opened_at=source.iso(t),
                    quantity=ZERO,
                    entry_cost=ZERO,
                    sell_proceeds=ZERO,
                    fees=ZERO,
                    slippage=ZERO,
                    buy_fills=0,
                    sell_fills=0,
                )
            ep = self.open[symbol]
            ep["quantity"] += quantity
            ep["entry_cost"] += notional + fee
            ep["buy_fills"] += 1
        else:
            ep = self.open[symbol]
            ep["quantity"] -= quantity
            ep["sell_proceeds"] += notional - fee
            ep["sell_fills"] += 1
        ep["fees"] += fee
        ep["slippage"] += slippage
        if ep["quantity"] < -D("1e-30"):
            raise ValueError("EPISODE_SHORT_FORBIDDEN")
        if side == "SELL" and abs(ep["quantity"]) < D("1e-30"):
            ep["quantity"] = ZERO
            pnl = ep["sell_proceeds"] - ep["entry_cost"]
            self.closed.append(self.record(ep, t, "CLOSED", pnl, pnl))
            del self.open[symbol]

    @staticmethod
    def record(ep, t, status, pnl, realized):
        return dict(
            episode_id=ep["episode_id"],
            symbol=ep["symbol"],
            status=status,
            entry_time=ep["opened_at"],
            exit_time=source.iso(t) if status == "CLOSED" else None,
            valuation_at=source.iso(t) if status != "CLOSED" else None,
            holding_days=(t - source.stamp(ep["opened_at"])) / DAY,
            invested_cash=float(ep["entry_cost"]),
            sell_proceeds=float(ep["sell_proceeds"]),
            realized_pnl=float(realized) if realized is not None else None,
            valuation_pnl=float(pnl),
            return_pct=float(pnl / ep["entry_cost"] * 100),
            fees_executed=float(ep["fees"]),
            slippage_executed=float(ep["slippage"]),
            buy_fills=ep["buy_fills"],
            sell_fills=ep["sell_fills"],
            quantity=str(ep["quantity"]),
        )

    def finalize(self, data, end, fee, slip):
        result = list(self.closed)
        for s, ep in sorted(self.open.items()):
            value = (
                ep["quantity"] * data[s][end - DAY].close * (ONE - slip) * (ONE - fee)
            )
            pnl = ep["sell_proceeds"] + value - ep["entry_cost"]
            result.append(self.record(ep, end, "OPEN_AT_END", pnl, None))
        return result


def simulate(data, signals, strategy, portfolio, start, end, fee=ZERO, slip=ZERO):
    if (
        strategy not in STRATEGIES
        or portfolio not in PORTFOLIOS
        or start < source.stamp("2024-01-01")
        or end > source.stamp("2026-01-01")
        or end <= start
    ):
        raise ValueError("OUTSIDE_FROZEN_SCOPE")
    with localcontext() as ctx:
        ctx.prec = 45
        return _simulate(data, signals, strategy, portfolio, start, end, fee, slip)


def _simulate(data, signals, strategy, portfolio, start, end, fee, slip):
    assets = PORTFOLIOS[portfolio]
    mode = "GROSS" if fee == slip == 0 else "NET"
    ledger = EpisodeLedger(f"{start}:{strategy}:{portfolio}:{mode}")
    cash = D(1000)
    quantity = {s: ZERO for s in source.SYMBOLS}
    previous = None
    events = []
    daily = []
    transitions = 0
    peak = 1000.0
    dd = 0.0
    prior = 1000.0
    days_in = 0
    fees = slippage = turnover = ZERO
    terminal_fee = terminal_slip = terminal_notional = ZERO
    for t in range(start, end, DAY):
        if any(
            signals[t][s]["source_close_ms"] >= t or signals[t][s]["known_at_ms"] > t
            for s in assets
        ):
            raise ValueError("UNOBSERVABLE_OR_SAME_BAR_SIGNAL")
        if any(
            t not in data[s] or data[s][t].end != t + DAY - 1 or data[s][t].end >= end
            for s in source.SYMBOLS
        ):
            raise ValueError("MISSING_OR_INCOMPLETE_COHORT_BAR")
        desired = weights_for(signals[t], assets)
        if desired != previous:
            prices = {s: data[s][t].open for s in source.SYMBOLS}
            before = quantity.copy()
            cash, orders = source.reallocate(cash, quantity, prices, desired, fee, slip)
            transitions += bool(orders)
            for order in orders:
                s = order["symbol"]
                side = order["side"]
                q = abs(quantity[s] - before[s])
                fill = prices[s] * (ONE + slip if side == "BUY" else ONE - slip)
                cost = q * fill * fee
                adverse = q * abs(fill - prices[s])
                notional = q * fill
                ledger.fill(s, side, q, fill, cost, adverse, t)
                fees += cost
                slippage += adverse
                turnover += notional
                events.append(
                    dict(
                        event="EXECUTED_NEXT_DAILY_OPEN",
                        timestamp=source.iso(t),
                        symbol=s,
                        side=side,
                        signal_id=signals[t][s]["signal_id"],
                        source_close=signals[t][s]["source_close"],
                        own_signal_action=signals[t][s]["action"],
                        reason="ACTIVE_SET_CHANGE",
                        active_assets=list(desired),
                        target_weights={s: str(w) for s, w in desired.items()},
                        quantity=str(q),
                        reference_price=str(prices[s]),
                        fill_price=str(fill),
                        notional=str(notional),
                        fee=str(cost),
                        slippage=str(adverse),
                        cash_after_allocation=str(cash),
                    )
                )
            previous = desired
        days_in += any(quantity[s] > 0 for s in assets)
        mark = cash + sum((quantity[s] * data[s][t].close for s in assets), ZERO)
        if t + DAY == end:
            for s in assets:
                reference = quantity[s] * data[s][t].close
                terminal_slip += reference * slip
                terminal_fee += reference * (ONE - slip) * fee
                terminal_notional += reference * (ONE - slip)
            mark -= terminal_fee + terminal_slip
            if any(quantity[s] > 0 for s in assets):
                events.append(
                    dict(
                        event="TERMINAL_COST_ESTIMATE_NOT_A_FILL",
                        timestamp=source.iso(t + DAY - 1),
                        fee=str(terminal_fee),
                        slippage=str(terminal_slip),
                        notional=str(terminal_notional),
                    )
                )
        equity = float(mark)
        peak = max(peak, equity)
        dd = max(dd, (peak - equity) / peak * 100)
        daily.append(
            dict(
                day=source.iso(t),
                timestamp=source.iso(t + DAY - 1),
                month=source.iso(t)[:7],
                equity=equity,
                return_=equity / prior - 1,
                invested=any(quantity[s] > 0 for s in assets),
            )
        )
        prior = equity
    episodes = ledger.finalize(data, end, fee, slip)
    if abs(sum(r["valuation_pnl"] for r in episodes) - (prior - 1000)) > 1e-7:
        raise ValueError("EPISODE_EQUITY_RECONCILIATION_FAILED")
    completed = [r for r in episodes if r["status"] == "CLOSED"]
    monthly = []
    for month in sorted({r["month"] for r in daily}):
        month_rows = [r for r in daily if r["month"] == month]
        monthly.append(
            dict(
                month=month,
                return_pct=(math.prod(1 + r["return_"] for r in month_rows) - 1) * 100,
            )
        )
    closed_returns = [r["return_pct"] for r in completed]
    hold = [r["holding_days"] for r in completed]
    executed = [r for r in events if r["event"] == "EXECUTED_NEXT_DAILY_OPEN"]
    metrics = dict(
        starting_equity=1000,
        ending_equity=prior,
        pnl=prior - 1000,
        return_pct=(prior / 1000 - 1) * 100,
        max_drawdown_pct=dd,
        turnover=float(turnover),
        fee=float(fees + terminal_fee),
        modeled_slippage=float(slippage + terminal_slip),
        actual_modeled_fee=float(fees),
        actual_modeled_slippage=float(slippage),
        terminal_fee_estimate=float(terminal_fee),
        terminal_slippage_estimate=float(terminal_slip),
        terminal_exit_notional_estimate=float(terminal_notional),
        turnover_including_terminal_estimate=float(turnover + terminal_notional),
        entries=sum(r["side"] == "BUY" for r in executed),
        exits=sum(r["side"] == "SELL" for r in executed),
        new_position_entries=len(episodes),
        completed_position_episodes=len(completed),
        open_position_episodes=len(episodes) - len(completed),
        win_rate=100 * sum(r["realized_pnl"] > 0 for r in completed) / len(completed)
        if completed
        else None,
        median_episode_return_pct=statistics.median(closed_returns)
        if completed
        else None,
        mean_episode_return_pct=statistics.mean(closed_returns) if completed else None,
        average_holding_days=statistics.mean(hold) if completed else None,
        median_holding_days=statistics.median(hold) if completed else None,
        time_in_market_pct=days_in / len(daily) * 100,
        days_in_market=days_in,
        transitions=transitions,
        best_month=max(monthly, key=lambda r: r["return_pct"]),
        worst_month=min(monthly, key=lambda r: r["return_pct"]),
        positive_months=sum(r["return_pct"] > 0 for r in monthly),
        total_months=len(monthly),
    )
    lm = source.leave_month(
        [dict(month=r["month"], **{"return": r["return_"]}) for r in daily]
    )
    return dict(
        metrics=metrics,
        episodes=episodes,
        events=events,
        daily=daily,
        monthly=monthly,
        leave_month=lm,
    )


def screen(results):
    portfolios = []
    for strategy in STRATEGIES:
        for portfolio in PORTFOLIOS:
            rows = [
                r
                for r in results
                if r["strategy"] == strategy and r["portfolio"] == portfolio
            ]
            reasons = []
            if {r["year"] for r in rows} != {2024, 2025}:
                reasons.append("BOTH_COHORTS_REQUIRED")
            for r in rows:
                for key, label in [
                    ("gross_return_pct", "GROSS_NOT_POSITIVE"),
                    ("net_return_pct", "NET_NOT_POSITIVE"),
                ]:
                    if r[key] <= 0:
                        reasons.append(f"{r['year']}:{label}")
                if r["completed_position_episodes"] < MIN_EPISODES:
                    reasons.append(f"{r['year']}:INSUFFICIENT_EPISODES")
                if r["positive_leave_month_runs"] != r["leave_month_count"]:
                    reasons.append(f"{r['year']}:MONTH_DOMINANCE_SENSITIVITY")
            portfolios.append(
                dict(
                    strategy=strategy,
                    strategy_class=STRATEGIES[strategy],
                    portfolio=portfolio,
                    passed=not reasons,
                    reasons=reasons,
                )
            )
    passed = sorted({r["strategy_class"] for r in portfolios if r["passed"]})
    if passed:
        category = "ORTHOGONAL_CLASS_SIGNAL_FOUND"
    elif any(r["net_return_pct"] > 0 for r in results):
        category = "ORTHOGONAL_CLASS_RESULT_MIXED"
    else:
        category = "ORTHOGONAL_CLASS_NOT_FOUND"
    return dict(
        final_classification=category,
        exploration_candidate=passed[0] if len(passed) == 1 else "NONE",
        clean_validation_eligible="YES" if passed else "NO",
        qualifying_classes=passed,
        candidate_note="MULTIPLE_CLASSES_REQUIRE_PREREGISTRATION"
        if len(passed) > 1
        else None,
        portfolio_screen=portfolios,
    )


def references(root):
    folder = root / "artifacts" / source.NAME
    old = json.loads((folder / "summary.json").read_text())
    rows = []
    for r in old["results"]:
        if r["benchmark"] in ("T1", "T2"):
            rows.append(
                dict(
                    year=r["year"],
                    reference=r["benchmark"],
                    gross_return_pct=r["gross_return_pct"],
                    net_return_pct=r["net_return_pct"],
                    max_drawdown_pct=r["max_drawdown_pct"],
                    costs=r["total_fees"] + r["total_modeled_slippage"],
                    turnover=r["turnover_notional"],
                    source=str(folder / "summary.json"),
                    note="Existing completed benchmark, never rerun",
                )
            )
    for r in json.loads((folder / "current-strategy-reference.json").read_text()):
        rows.append(
            dict(
                year=r["year"],
                reference="INSTITUTIONAL_BASELINE",
                net_return_pct=r["return_pct"],
                profit_factor=r["profit_factor"],
                closed_trades=r["trade_count"],
                costs=r["total_modeled_cost"],
                run_id=r["run_id"],
                source=r["source"],
                note="Exact period, different universe/exposure; descriptive only",
            )
        )
    return rows


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    data, manifest, proof = load_ohlc(root)
    result = []
    exports = {
        n: []
        for n in (
            "episodes",
            "monthly",
            "leave_month",
            "costs",
            "execution",
            "daily",
            "signals",
            "cost_metrics",
        )
    }
    for year, (a, b) in PERIODS.items():
        start, end = source.stamp(a), source.stamp(b)
        for strategy in STRATEGIES:
            signals = schedule(data, strategy, start, end)
            for t, observations in signals.items():
                exports["signals"].extend(
                    dict(
                        year=year, strategy=strategy, execution_open=source.iso(t), **r
                    )
                    for r in observations.values()
                )
            for portfolio in PORTFOLIOS:
                gross = simulate(data, signals, strategy, portfolio, start, end)
                net = simulate(
                    data,
                    signals,
                    strategy,
                    portfolio,
                    start,
                    end,
                    source.FEE,
                    source.SLIP,
                )
                lm = [r["net_return_pct"] for r in net["leave_month"]]
                common = dict(
                    year=year,
                    strategy=strategy,
                    strategy_class=STRATEGIES[strategy],
                    portfolio=portfolio,
                )
                row = dict(
                    **common,
                    **net["metrics"],
                    gross_return_pct=gross["metrics"]["return_pct"],
                    net_return_pct=net["metrics"]["return_pct"],
                    gross_pnl=gross["metrics"]["pnl"],
                    net_pnl=net["metrics"]["pnl"],
                    positive_leave_month_runs=sum(v > 0 for v in lm),
                    leave_month_count=len(lm),
                    worst_leave_month_return_pct=min(lm),
                    best_leave_month_return_pct=max(lm),
                    episode_adequacy="SUFFICIENT_EPISODES"
                    if net["metrics"]["completed_position_episodes"] >= MIN_EPISODES
                    else "INSUFFICIENT_EPISODES",
                )
                result.append(row)
                for mode, run in [("GROSS", gross), ("NET", net)]:
                    for key in ("episodes", "monthly", "daily"):
                        exports[key].extend(
                            dict(**common, cost_mode=mode, **r) for r in run[key]
                        )
                    exports["execution"].extend(
                        dict(**common, cost_mode=mode, **r) for r in run["events"]
                    )
                    exports["cost_metrics"].append(
                        dict(**common, cost_mode=mode, **run["metrics"])
                    )
                exports["leave_month"].extend(
                    dict(**common, cost_mode="NET", **r) for r in net["leave_month"]
                )
                exports["costs"].append(
                    dict(
                        **common,
                        gross_pnl=gross["metrics"]["pnl"],
                        net_pnl=net["metrics"]["pnl"],
                        gross_net_degradation=gross["metrics"]["pnl"]
                        - net["metrics"]["pnl"],
                        **{
                            k: net["metrics"][k]
                            for k in (
                                "actual_modeled_fee",
                                "actual_modeled_slippage",
                                "turnover",
                                "terminal_fee_estimate",
                                "terminal_slippage_estimate",
                                "fee",
                                "modeled_slippage",
                            )
                        },
                    )
                )
    final = screen(result)
    summary = dict(
        **final,
        results=result,
        references=references(root),
        base_git_sha=BASE,
        source_code_sha256=source.sha(Path(__file__).read_bytes()),
        dataset_role="EXPLORATION",
        research_2023_outcome_used=False,
        research_2026_outcome_used=False,
        clean_validation_run=False,
        parameter_optimization_performed=False,
        new_institutional_replay=False,
        production_behavior_changed=False,
        production_db_writes=0,
        deployment=False,
    )
    methodology = dict(
        base=BASE,
        periods=PERIODS,
        strategies=dict(
            S1="Long when completed close > previous 20 HIGH max, flat when close < previous 10 LOW min; decision candle excluded from both channels",
            S2="SMA20 and sample std ddof=1 on 20 closes INCLUDING decision close; long below SMA20 - 2*std; exit close >= SMA20",
        ),
        portfolios=PORTFOLIOS,
        starting_equity=1000,
        costs=dict(fee_per_side=".001", slippage_per_side=".0005"),
        signal_state="Start flat independently in each cohort. First opening decision may observe the preceding completed warmup candle; no warmup position/PnL is carried. Per-asset state independent of portfolio/cost model.",
        allocation="Equal weight among active assets, 100% if only one active; cash otherwise. Rebalance only when active set changes. Use post-cost open-valued target weights, sell excess first, no debt.",
        execution="Next in-cohort daily open only; completed decision candle strictly precedes fill. No last-day close fill or 2026 open.",
        terminal="Last completed cohort close less estimated sell fee/slippage. Valuation only; remaining asset episodes OPEN_AT_END, not completed.",
        episodes="Asset lifetime from first buy until fully flat; additions/partial sells remain same episode. Return = (total sell proceeds net of fees - total buy costs including fees)/total buy costs. Open terminal estimate excluded from closed statistics and minimum episode screen.",
        holding="Average/median holding days and win rate are CLOSED asset episodes only; open episode ages are separately censored in episode CSV. Entries/exits count actual BUY/SELL fills, including partial rebalances.",
        metrics="Daily-close drawdown including initial equity and terminal valuation costs; time in market counts days with any holdings.",
        leave_month="Delete one month from unchanged NET daily equity returns, compound remainder; no refit or changed signal state.",
        cross_year_screen="Same portfolio must have gross/net >0 in BOTH cohorts, >=5 completed NET asset episodes in EACH, and ALL NET leave-month deletions >0 in BOTH cohorts. No ranking or post-hoc threshold.",
        classification="A if any class passes; B if any NET result positive but none passes (specific mixed rule takes precedence over overlapping B/C wording); C if no positive NET row. If both classes pass, candidate NONE and MULTIPLE_CLASSES_REQUIRE_PREREGISTRATION.",
        clean_validation="YES iff category A; eligibility only, 2023 validation NOT run",
        parameter_grid=False,
        regime_filter=False,
        strategy_combination=False,
        source_contract="Original official Spot daily archives and checksum-pinned cache, offline only; 2023 warmup only; no 2026 data",
    )
    out = root / "artifacts" / NAME
    out.mkdir(parents=True, exist_ok=True)
    for n, payload in [
        ("summary.json", summary),
        ("methodology.json", methodology),
        ("daily-data-manifest.json", manifest),
    ]:
        (out / n).write_bytes(source.encoded(payload))
    mapping = {
        "strategy-results.csv": result,
        "episode-results.csv": exports["episodes"],
        "monthly-results.csv": exports["monthly"],
        "leave-month-sensitivity.csv": exports["leave_month"],
        "cost-decomposition.csv": exports["costs"],
        "execution-audit.csv": exports["execution"],
        "daily-equity.csv": exports["daily"],
        "signal-audit.csv": exports["signals"],
        "metrics-by-cost.csv": exports["cost_metrics"],
        "reference-comparison.csv": summary["references"],
        "cross-year-screen.csv": final["portfolio_screen"],
    }
    for n, rows in mapping.items():
        source.write_csv(out / n, rows)
    if source_proof(root) != proof:
        raise ValueError("REFERENCE_CHANGED_DURING_BENCHMARK")
    validation = dict(
        original_sources=proof,
        source_fingerprint_unchanged=True,
        monthly_archives_verified=len(manifest),
        daily_manifest_sha256=source.sha(source.encoded(manifest)),
        source_code_sha256=summary["source_code_sha256"],
        strategy_rows=len(result),
        gross_net_metric_rows=len(exports["cost_metrics"]),
        no_new_download=True,
        no_db_access=True,
        minimum_completed_episodes=MIN_EPISODES,
        research_2023_outcome_used=False,
        research_2026_outcome_used=False,
        clean_validation_run=False,
        new_institutional_replay=False,
        production_db_writes=0,
        deployment=False,
    )
    (out / "source-validation.json").write_bytes(source.encoded(validation))
    (out / "report.md").write_text(report(summary))
    p = package(out)
    print(
        "COMPLETED",
        final["final_classification"],
        final["exploration_candidate"],
        "clean_validation_eligible=" + final["clean_validation_eligible"],
        p,
        source.sha(p.read_bytes()),
        flush=True,
    )


def report(summary):
    lines = [
        "# ORTHOGONAL STRATEGY CLASS BENCHMARK v1",
        "",
        summary["final_classification"],
        "exploration_candidate: " + summary["exploration_candidate"],
        "clean_validation_eligible: " + summary["clean_validation_eligible"],
        "",
        "2024 and 2025 are EXPLORATION; no 2023 clean validation or 2026 outcome is used.",
        "",
        "| Year | Strategy | Portfolio | Gross % | Net % | Max DD % | Turnover | Costs incl terminal estimate | Closed/open episodes | Win % | Mean/median hold days (closed) | Invested % | Positive months | Positive leave-month |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    def fmt(x):
        return "UNDEFINED" if x is None else f"{x:.4f}"

    for r in summary["results"]:
        lines.append(
            f"| {r['year']} | {r['strategy']} | {r['portfolio']} | {fmt(r['gross_return_pct'])} | {fmt(r['net_return_pct'])} | {fmt(r['max_drawdown_pct'])} | {r['turnover']:.2f} | {r['fee'] + r['modeled_slippage']:.4f} | {r['completed_position_episodes']}/{r['open_position_episodes']} | {fmt(r['win_rate'])} | {fmt(r['average_holding_days'])}/{fmt(r['median_holding_days'])} | {r['time_in_market_pct']:.2f} | {r['positive_months']}/{r['total_months']} | {r['positive_leave_month_runs']}/{r['leave_month_count']} |"
        )
    lines += [
        "",
        "## Frozen cross-year screen",
        "",
        "Same portfolio, gross/net positive both years, >=5 completed episodes per year, and every unchanged-return leave-month deletion positive. Open terminal positions do not inflate the episode count.",
        "",
    ]
    for r in summary["portfolio_screen"]:
        lines.append(
            f"{r['strategy']} {r['portfolio']}: {'PASS' if r['passed'] else '; '.join(r['reasons'])}"
        )
    if summary["candidate_note"]:
        lines += ["", summary["candidate_note"]]
    lines += [
        "",
        "## Accounting and scope",
        "",
        "Signals use completed daily data and next in-cohort daily opens. Donchian channels exclude the decision candle; Bollinger includes its close and uses sample ddof=1. Cohorts start flat; no warmup holdings or PnL are inherited.",
        "Equal weights drift until the active asset set changes. Partial portfolio rebalances are fills, not new completed asset episodes. Closed episode return uses cumulative entry cash including fees and cumulative net sale proceeds. Open positions remain censored and carry terminal valuation estimates, without a forced close.",
        "Turnover is executed fill notional. Total fee/slippage above includes separately exported terminal estimates; actual executed fees/slippage are separate in cost-decomposition.csv. Gross-net degradation also contains compounding effects.",
        "Holding-time and win-rate statistics use CLOSED episodes only. No closed episodes means undefined, not zero. Drawdown uses daily close observations; no intraday precision is claimed.",
        "The specific mixed-positive B rule takes precedence where the supplied B/C category wording overlaps. The numeric results and cross-year pass/fail screen are unaffected by that naming convention.",
        "",
        "## Existing references — never rerun",
        "",
        "| Year | Reference | Net % | Modeled cost |",
        "|---|---|---:|---:|",
    ]
    for r in summary["references"]:
        lines.append(
            f"| {r['year']} | {r['reference']} | {r['net_return_pct']:.4f} | {r['costs']:.4f} |"
        )
    lines += [
        "",
        "No parameter search, regime filter, strategy combination, new institutional replay, DB access/write, production change, service restart or deployment. Eligibility never starts 2023 validation.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
