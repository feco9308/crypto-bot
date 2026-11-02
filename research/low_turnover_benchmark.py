"""Frozen standalone daily Spot benchmark. No production imports, DB or replay."""

import argparse
import csv
import hashlib
import io
import json
import math
import re
import statistics
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import requests

BASE = "70b30e65d1a8015790930880640f3986c908f75a"
NAME = "low-turnover-benchmark-v1"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
BENCHMARKS = ("B0", "B1", "B2", "T1", "T2")
DAY = 86400000
SMA_DAYS = 200
D = Decimal
ZERO = D(0)
ONE = D(1)
FEE = D("0.001")
SLIP = D("0.0005")
PERIODS = {2024: ("2024-01-01", "2025-01-01"), 2025: ("2025-02-01", "2026-01-01")}
DOC = "https://github.com/binance/binance-public-data/blob/master/README.md"


def stamp(s):
    return (
        int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()) * 1000
    )


def iso(t):
    return (
        datetime.fromtimestamp(t / 1000, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def encoded(value):
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Bar:
    start: int
    end: int
    open: Decimal
    close: Decimal


def parse_zip(blob, month):
    """Normalize official 2025 Spot microseconds without rounding close labels."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        if len(names) != 1 or not names[0].endswith(".csv"):
            raise ValueError("INVALID_ZIP_SCHEMA")
        rows = list(csv.reader(io.StringIO(z.read(names[0]).decode())))
    result = []
    for row in rows:
        if len(row) != 12:
            raise ValueError("EXPECTED_12_KLINE_FIELDS")
        unit = 1000 if month >= "2025-01" else 1
        raw_start, raw_end = int(row[0]), int(row[6])
        start = raw_start // unit
        end = raw_end // unit
        op, hi, lo, cl = (D(row[n]) for n in (1, 2, 3, 4))
        if (
            raw_start % unit
            or start % DAY
            or end != start + DAY - 1
            or not all(v.is_finite() and v > 0 for v in (op, hi, lo, cl))
            or lo > min(op, cl)
            or hi < max(op, cl)
            or hi < lo
        ):
            raise ValueError("INVALID_DAILY_CANDLE")
        if iso(start)[:7] != month:
            raise ValueError("ARCHIVE_MONTH_MISMATCH")
        result.append(Bar(start, end, op, cl))
    expected = stamp(month + "-01")
    for b in result:
        if b.start != expected:
            raise ValueError("DUPLICATE_GAP_OR_UNSORTED_DAILY_BAR")
        expected += DAY
    nxt = (
        datetime.fromisoformat(month + "-01").replace(day=28) + timedelta(days=4)
    ).replace(day=1)
    if expected != stamp(nxt.date().isoformat()):
        raise ValueError("INCOMPLETE_MONTH")
    return result


def archive_url(symbol, month):
    if symbol not in SYMBOLS or not re.fullmatch(r"202[345]-\d{2}", month):
        raise ValueError("FORBIDDEN_SYMBOL_OR_YEAR")
    if not "2023-06" <= month <= "2025-12":
        raise ValueError("OUTSIDE_FROZEN_HISTORY")
    return f"https://data.binance.vision/data/spot/monthly/klines/{symbol}/1d/{symbol}-1d-{month}.zip"


def fetch(cache, url, capture, session):
    # URLs are constructed internally from the fixed symbols/months only.
    if not re.fullmatch(
        r"https://data\.binance\.vision/data/spot/monthly/klines/(BTCUSDT|ETHUSDT)/1d/\1-1d-202[345]-\d{2}\.zip(?:\.CHECKSUM)?",
        url,
    ):
        raise ValueError("ONLY_OFFICIAL_FROZEN_PUBLIC_ARCHIVES")
    path = cache / (sha(url.encode()) + ".json")
    if path.exists():
        r = json.loads(path.read_text())
        if r["url"] != url or sha(bytes.fromhex(r["body_hex"])) != r["sha256"]:
            raise ValueError("CACHE_CORRUPTION")
        return r
    if not capture:
        raise ValueError("OFFLINE_CACHE_MISSING " + url)
    with session.get(url, timeout=30, allow_redirects=False) as response:
        if response.status_code != 200:
            raise RuntimeError(
                f"ARCHIVE_HTTP_{response.status_code}; STOP_NO_RETRY {url}"
            )
        body = response.content
        if len(body) > 1000000:
            raise ValueError("BODY_LIMIT")
        r = dict(
            url=url,
            status=200,
            sha256=sha(body),
            body_hex=body.hex(),
            received_at=datetime.now(timezone.utc).isoformat(),
            last_modified=response.headers.get("Last-Modified"),
        )
    tmp = path.with_suffix(".part")
    tmp.write_bytes(encoded(r))
    tmp.replace(path)
    return r


def load_data(root, capture=False):
    cache = root / "data/research-cache" / NAME
    cache.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.trust_env = False
    data = {s: {} for s in SYMBOLS}
    manifest = []
    months = [
        f"{y}-{m:02d}"
        for y in (2023, 2024, 2025)
        for m in range(1, 13)
        if "2023-06" <= f"{y}-{m:02d}" <= "2025-12"
    ]
    for symbol in SYMBOLS:
        for month in months:
            url = archive_url(symbol, month)
            check = fetch(cache, url + ".CHECKSUM", capture, session)
            record = fetch(cache, url, capture, session)
            expected = bytes.fromhex(check["body_hex"]).decode().split()[0].lower()
            if record["sha256"] != expected or not re.fullmatch(
                "[0-9a-f]{64}", expected
            ):
                raise ValueError("OFFICIAL_CHECKSUM_MISMATCH")
            bars = parse_zip(bytes.fromhex(record["body_hex"]), month)
            data[symbol].update({b.start: b for b in bars})
            manifest.append(
                dict(
                    symbol=symbol,
                    month=month,
                    url=url,
                    sha256=expected,
                    checksum_sha256=check["sha256"],
                    rows=len(bars),
                    first_open=iso(bars[0].start),
                    last_close=iso(bars[-1].end),
                    role="WARMUP_ONLY" if month[:4] == "2023" else "DAILY_SOURCE",
                )
            )
            print("VALIDATED", symbol, month, flush=True)
    session.close()
    return data, manifest


def active(data, symbol, decision):
    """Only the exact preceding 200 *completed* daily closes are observable."""
    bars = [data[symbol].get(decision - k * DAY) for k in range(1, SMA_DAYS + 1)]
    if any(b is None or b.end >= decision for b in bars):
        return None
    average = sum((b.close for b in bars), ZERO) / SMA_DAYS
    return bars[0].close > average


def target(benchmark, data, decision):
    if benchmark == "B0":
        return {"BTCUSDT": ONE}
    if benchmark == "B1":
        return {"ETHUSDT": ONE}
    if benchmark == "B2":
        return {s: D("0.5") for s in SYMBOLS}
    symbols = SYMBOLS if benchmark == "T2" else ("BTCUSDT",)
    flags = {s: active(data, s, decision) for s in symbols}
    if any(v is None for v in flags.values()):
        return None
    selected = [s for s, flag in flags.items() if flag]
    return {s: ONE / len(selected) for s in selected}


def reallocate(cash, quantities, prices, weights, fee, slip):
    """Minimum required trades to target post-cost open-valued weights; no debt."""
    with localcontext() as ctx:
        ctx.prec = 45
        return _reallocate(cash, quantities, prices, weights, fee, slip)


def _reallocate(cash, quantities, prices, weights, fee, slip):
    values = {s: quantities[s] * prices[s] for s in SYMBOLS}
    equity = cash + sum(values.values(), ZERO)
    sell_cost = ONE - (ONE - slip) * (ONE - fee)
    buy_cost = (ONE + slip) * (ONE + fee) - ONE
    low, high = ZERO, equity
    for _ in range(140):
        mid = (low + high) / 2
        sells = sum(
            (max(values[s] - weights.get(s, ZERO) * mid, ZERO) for s in SYMBOLS), ZERO
        )
        buys = sum(
            (max(weights.get(s, ZERO) * mid - values[s], ZERO) for s in SYMBOLS), ZERO
        )
        if mid + sells * sell_cost + buys * buy_cost > equity:
            high = mid
        else:
            low = mid
    post = low
    orders = []
    desired = {s: weights.get(s, ZERO) * post / prices[s] for s in SYMBOLS}
    # Sell first, then use only settled simulated cash.
    for side in ("SELL", "BUY"):
        for s in SYMBOLS:
            diff = desired[s] - quantities[s]
            if (side == "BUY" and diff <= 0) or (side == "SELL" and diff >= 0):
                continue
            qty = abs(diff)
            if qty * prices[s] < D("1e-25"):
                continue
            fill = prices[s] * (ONE + slip if side == "BUY" else ONE - slip)
            notional, cost = qty * fill, qty * fill * fee
            cash += notional - cost if side == "SELL" else -notional - cost
            quantities[s] += qty if side == "BUY" else -qty
            if cash < D("-1e-25") or quantities[s] < 0:
                raise AssertionError("BORROWING_OR_SHORT_FORBIDDEN")
            orders.append(
                dict(
                    symbol=s,
                    side=side,
                    quantity=float(qty),
                    reference_price=float(prices[s]),
                    fill_price=float(fill),
                    notional=float(notional),
                    fee=float(cost),
                    slippage=float(qty * abs(fill - prices[s])),
                )
            )
    return max(cash, ZERO), orders


def leave_month(daily):
    """Delete one month's realized daily equity returns, compound the rest; no re-fit."""
    months = sorted({r["month"] for r in daily})
    result = []
    for month in months:
        equity = math.prod(1 + r["return"] for r in daily if r["month"] != month) * 1000
        result.append(
            dict(
                omitted_month=month,
                ending_equity=equity,
                net_pnl=equity - 1000,
                net_return_pct=(equity / 1000 - 1) * 100,
            )
        )
    return result


def run(data, benchmark, start, end, fee=ZERO, slip=ZERO):
    if (
        benchmark not in BENCHMARKS
        or end <= start
        or start < stamp("2024-01-01")
        or end > stamp("2026-01-01")
    ):
        raise ValueError("INVALID_FROZEN_RUN")
    with localcontext() as ctx:
        ctx.prec = 45
        return _run(data, benchmark, start, end, fee, slip)


def _run(data, benchmark, start, end, fee, slip):
    cash, qty = D(1000), {s: ZERO for s in SYMBOLS}
    previous_target = None
    daily, events, monthly = [], [], defaultdict(list)
    prior, peak, dd = 1000.0, 1000.0, 0.0
    in_market = transitions = 0
    t = start
    while t < end:
        # This research harness uses completed historical candles only.
        if any(t not in data[s] or data[s][t].end >= end for s in SYMBOLS):
            raise ValueError("MISSING_OR_INCOMPLETE_COHORT_DAY")
        wanted = target(benchmark, data, t)
        if wanted is None:
            # Unknown signal cannot fabricate an entry or liquidate existing holdings.
            events.append(dict(event="NO_FILL_UNOBSERVABLE_SIGNAL", timestamp=iso(t)))
        elif wanted != previous_target:
            prices = {s: data[s][t].open for s in SYMBOLS}
            cash, orders = reallocate(cash, qty, prices, wanted, fee, slip)
            if orders:
                transitions += 1
            for order in orders:
                inputs = {}
                if benchmark.startswith("T"):
                    for symbol in SYMBOLS if benchmark == "T2" else ("BTCUSDT",):
                        closes = [
                            data[symbol][t - k * DAY].close
                            for k in range(1, SMA_DAYS + 1)
                        ]
                        inputs[symbol] = dict(
                            last_completed_close=str(closes[0]),
                            sma200=str(sum(closes, ZERO) / SMA_DAYS),
                            source_close=iso(t - 1),
                            window_days=SMA_DAYS,
                        )
                events.append(
                    dict(
                        event="EXECUTED_NEXT_OPEN",
                        timestamp=iso(t),
                        decision_data_close=iso(t - 1)
                        if benchmark.startswith("T")
                        else None,
                        signal_inputs=inputs,
                        target_weights={s: str(w) for s, w in wanted.items()},
                        **order,
                    )
                )
            previous_target = wanted
        exposed = any(qty[s] > 0 for s in SYMBOLS)
        in_market += exposed
        mark = cash + sum((qty[s] * data[s][t].close for s in SYMBOLS), ZERO)
        terminal_fee = terminal_slippage = ZERO
        if t + DAY == end:
            # A valuation adjustment, never an order/fill at the decision-day close.
            for s in SYMBOLS:
                reference = qty[s] * data[s][t].close
                terminal_slippage += reference * slip
                terminal_fee += reference * (ONE - slip) * fee
            if terminal_fee or terminal_slippage or exposed:
                events.append(
                    dict(
                        event="TERMINAL_LIQUIDATION_COST_ESTIMATE_NOT_A_FILL",
                        timestamp=iso(data[SYMBOLS[0]][t].end),
                        fee=float(terminal_fee),
                        slippage=float(terminal_slippage),
                        notional=float(
                            sum(
                                (
                                    qty[s] * data[s][t].close * (ONE - slip)
                                    for s in SYMBOLS
                                ),
                                ZERO,
                            )
                        ),
                    )
                )
            mark -= terminal_fee + terminal_slippage
        value = float(mark)
        peak = max(peak, value)
        dd = max(dd, (peak - value) / peak * 100)
        ret = value / prior - 1
        day = dict(
            timestamp=iso(t + DAY - 1),
            month=iso(t)[:7],
            equity=value,
            return_=ret,
            exposure_pct=float((mark - cash) / mark * 100) if mark else 0,
        )
        day["return"] = day.pop("return_")
        daily.append(day)
        monthly[day["month"]].append(day)
        prior = value
        t += DAY
    returns = [r["return"] for r in daily]
    vol = statistics.stdev(returns) if len(returns) > 1 else 0.0
    month_rows = []
    for month, rows in sorted(monthly.items()):
        r = math.prod(1 + x["return"] for x in rows) - 1
        month_rows.append(
            dict(
                month=month,
                return_pct=r * 100,
                pnl=rows[-1]["equity"]
                - (
                    1000
                    if not month_rows
                    else monthly[month_rows[-1]["month"]][-1]["equity"]
                ),
            )
        )
    trades = [r for r in events if r["event"] == "EXECUTED_NEXT_OPEN"]
    terminal = [r for r in events if r["event"].startswith("TERMINAL_")]
    metrics = dict(
        starting_equity=1000,
        ending_equity=prior,
        return_pct=(prior / 1000 - 1) * 100,
        pnl=prior - 1000,
        total_fees=sum(r["fee"] for r in trades + terminal),
        total_modeled_slippage=sum(r["slippage"] for r in trades + terminal),
        turnover_notional=sum(r["notional"] for r in trades),
        terminal_estimated_exit_notional=sum(r["notional"] for r in terminal),
        turnover_including_terminal_estimate=sum(
            r["notional"] for r in trades + terminal
        ),
        entries=sum(r["side"] == "BUY" for r in trades),
        exits=sum(r["side"] == "SELL" for r in trades),
        transitions=transitions,
        terminal_open_assets=sum(qty[s] > 0 for s in SYMBOLS),
        time_in_market_pct=in_market / len(daily) * 100,
        max_drawdown_pct=dd,
        daily_return_volatility=vol,
        annualized_volatility=vol * math.sqrt(365),
        descriptive_sharpe_rf0=statistics.mean(returns) / vol * math.sqrt(365)
        if vol
        else None,
        best_month=max(month_rows, key=lambda r: r["return_pct"]),
        worst_month=min(month_rows, key=lambda r: r["return_pct"]),
        positive_months=sum(r["return_pct"] > 0 for r in month_rows),
        total_months=len(month_rows),
        end_valuation="FINAL_COMPLETED_CLOSE_LESS_ESTIMATED_EXIT_COSTS_NOT_AN_EXECUTED_FILL",
    )
    return dict(
        metrics=metrics,
        daily=daily,
        monthly=month_rows,
        events=events,
        leave_month=leave_month(daily) if benchmark.startswith("T") else [],
    )


def classify(results):
    eligible, any_positive = [], False
    for bench in ("T1", "T2"):
        rows = [r for r in results if r["benchmark"] == bench]
        both = len(rows) == 2 and all(
            r["net_return_pct"] > 0 and r["gross_return_pct"] > 0 for r in rows
        )
        robust = both and all(
            r["positive_leave_months"] == r["leave_month_count"] for r in rows
        )
        if robust:
            eligible.append(bench)
        any_positive |= any(r["net_return_pct"] > 0 for r in rows)
    # Frozen categorical wording B/C overlaps: mixed takes precedence for a positive
    # single year; C is reserved for no positive trend-benchmark year at all.
    category = (
        "LOW_TURNOVER_EDGE_PRESENT"
        if eligible
        else "LOW_TURNOVER_RESULT_MIXED"
        if any_positive
        else "LOW_TURNOVER_EDGE_NOT_FOUND"
    )
    return category, "DAILY_TREND" if eligible else "NONE"


def write_csv(path, rows):
    keys = sorted({k for r in rows for k in r})
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(
            {
                k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v
                for k, v in row.items()
            }
            for row in rows
        )


def package(out):
    files = {
        p.name: dict(sha256=sha(p.read_bytes()), bytes=p.stat().st_size)
        for p in sorted(out.iterdir())
        if p.is_file() and p.name not in ("manifest.json", NAME + ".zip")
    }
    (out / "manifest.json").write_bytes(encoded(dict(base=BASE, files=files)))
    path = out / (NAME + ".zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted([*files, "manifest.json"]):
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, (out / name).read_bytes())
    return path


def current_references(root):
    """Read only existing exported metrics; pin inputs, never open a database."""
    sources = {
        2024: root / "artifacts/institutional-entry-v2-2024-exploration/A.compact.json",
        2025: root.parent
        / "crypto-bot-entry-export/artifacts/institutional-entry-v1-complete-export/replay-research-f9d41057d61e4714b61df69d320620e0.zip",
    }
    rows = []
    for year, path in sources.items():
        if not path.exists():
            rows.append(
                dict(
                    year=year,
                    status="NOT DIRECTLY COMPARABLE",
                    reason="EXISTING_EXPORT_NOT_FOUND",
                )
            )
            continue
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as z:
                payload = z.read("A.compact.json")
                manifest = json.loads(z.read("manifest.json"))
                validation = json.loads(z.read("export-validation.json"))
        else:
            payload = path.read_bytes()
            manifest = json.loads((path.parent / "manifest.json").read_text())
            validation = json.loads(
                (path.parent / "export-validation.json").read_text()
            )
        if sha(payload) != manifest["files"]["A.compact.json"]["sha256"]:
            raise ValueError("REFERENCE_MANIFEST_MISMATCH")
        doc = json.loads(payload)
        cfg, meta, metrics = doc["config"], doc["metadata"], doc["summary"]
        if (
            not validation["run_completed_verified"]
            or not validation["config_hash_verified"]
            or meta["config_hash"] != manifest["config_hash"]
            or meta["run_id"] != validation["run_id"]
        ):
            raise ValueError("REFERENCE_VALIDATION_MISMATCH")
        expected = PERIODS[year]
        comparable = stamp(cfg["start"]) == stamp(expected[0]) and stamp(
            cfg["end"]
        ) == stamp(expected[1])
        row = dict(
            year=year,
            status="EXACT_PERIOD_DESCRIPTIVE_ONLY"
            if comparable
            else "NOT DIRECTLY COMPARABLE",
            source=str(path),
            source_sha256=sha(path.read_bytes()),
            member_sha256=sha(payload),
            run_id=meta["run_id"],
            config_hash=meta["config_hash"],
            source_git_sha=meta["git_commit"],
            start=cfg["start"],
            end=cfg["end"],
            market_context_version=meta["market_context_version"],
            note="Top20 institutional, capped exposure versus BTC/ETH fully invested; no causal architecture attribution or gross replay",
        )
        if comparable:
            row.update(
                trade_count=metrics["trade_count"],
                return_pct=metrics["return_pct"],
                profit_factor=metrics["profit_factor"],
                total_modeled_cost=float(
                    D(metrics["fees"]) + D(metrics["slippage_cost"])
                ),
                fees=metrics["fees"],
                slippage=metrics["slippage_cost"],
                turnover_notional=metrics["turnover_notional"],
                gross_return_available=False,
                cost_to_initial_capital_pct=float(
                    (D(metrics["fees"]) + D(metrics["slippage_cost"])) / 10
                ),
            )
        rows.append(row)
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument(
        "--capture",
        action="store_true",
        help="Fetch missing fixed official daily archives only",
    )
    args = p.parse_args()
    data, manifest = load_data(args.root, args.capture)
    out = args.root / "artifacts" / NAME
    out.mkdir(parents=True, exist_ok=True)
    results, cost_rows, months, leave, audit, curves = [], [], [], [], [], []
    for year, (s, e) in PERIODS.items():
        for bench in BENCHMARKS:
            gross = run(data, bench, stamp(s), stamp(e))
            net = run(data, bench, stamp(s), stamp(e), FEE, SLIP)
            row = dict(
                year=year,
                benchmark=bench,
                start=iso(stamp(s)),
                end_exclusive=iso(stamp(e)),
                **net["metrics"],
                gross_return_pct=gross["metrics"]["return_pct"],
                net_return_pct=net["metrics"]["return_pct"],
                gross_pnl=gross["metrics"]["pnl"],
                gross_metrics=gross["metrics"],
                net_metrics=net["metrics"],
            )
            if net["leave_month"]:
                lm = [r["net_return_pct"] for r in net["leave_month"]]
                row.update(
                    positive_leave_months=sum(x > 0 for x in lm),
                    leave_month_count=len(lm),
                    worst_leave_month_return_pct=min(lm),
                    best_leave_month_return_pct=max(lm),
                )
            results.append(row)
            degradation = gross["metrics"]["pnl"] - net["metrics"]["pnl"]
            cost_rows.append(
                dict(
                    year=year,
                    benchmark=bench,
                    gross_pnl=gross["metrics"]["pnl"],
                    net_pnl=net["metrics"]["pnl"],
                    gross_minus_net_pnl=degradation,
                    cash_cost_including_terminal_estimate=net["metrics"]["total_fees"]
                    + net["metrics"]["total_modeled_slippage"],
                    cost_div_gross_absolute_pnl=degradation
                    / abs(gross["metrics"]["pnl"])
                    if gross["metrics"]["pnl"]
                    else None,
                    cash_cost_div_gross_absolute_pnl=(
                        net["metrics"]["total_fees"]
                        + net["metrics"]["total_modeled_slippage"]
                    )
                    / abs(gross["metrics"]["pnl"])
                    if gross["metrics"]["pnl"]
                    else None,
                    note="Independent cash paths; degradation also includes foregone compounding",
                )
            )
            for mode, payload in [("GROSS", gross), ("CURRENT_PAPER_COST", net)]:
                for name, destination in [
                    ("monthly", months),
                    ("events", audit),
                    ("daily", curves),
                ]:
                    destination.extend(
                        dict(year=year, benchmark=bench, cost_mode=mode, **r)
                        for r in payload[name]
                    )
            leave.extend(
                dict(year=year, benchmark=bench, **r) for r in net["leave_month"]
            )
    category, candidate = classify(results)
    references = current_references(args.root)
    methodology = dict(
        base=BASE,
        symbols=SYMBOLS,
        benchmarks=BENCHMARKS,
        sma_days=SMA_DAYS,
        periods=PERIODS,
        starting_capital=1000,
        fee_per_side=float(FEE),
        slippage_per_side=float(SLIP),
        official_documentation=DOC,
        timestamp_units="2023/2024 milliseconds; 2025 microseconds",
        signal="Previous 200 completed daily closes, inclusive of decision-day close; strict close > SMA200",
        fill="Next daily OPEN only, strictly after source close; never same-close",
        T2_allocation="Trade only when active-symbol set changes; hit equal post-cost open-value weights, then drift",
        terminal="Final completed close mark less estimated sell fee/slippage; not a fill, positions remain open",
        leave_month="Compound unchanged realized daily returns except omitted calendar month; no strategy rerun or data deletion",
        classification="A: a trend benchmark gross/net positive both years and every leave-month positive; B: at least one positive trend-year but not A; C: none positive. B takes precedence in overlapping frozen B/C wording.",
        sharpe="Sample standard deviation of daily simple returns; sqrt(365) annualization; risk-free zero; descriptive",
        drawdown="Daily-close equity including initial 1000 and terminal estimated exit-cost adjustment; no intraday inference",
        warmup="2023-06 onward for indicators only; no 2023 performance; January 2025 not in 2025 research cohort",
        no_2026_prices=True,
        parameter_search=False,
    )
    summary = dict(
        base_git_sha=BASE,
        source_code_sha256=sha(Path(__file__).read_bytes()),
        frozen_methodology_sha256=sha(encoded(methodology)),
        final_category=category,
        next_architecture_candidate=candidate,
        results=results,
        cost_decomposition=cost_rows,
        current_strategy_descriptive_reference=references,
        production_db_writes=0,
        production_behavior_changed=False,
        new_institutional_replay=False,
        deployment=False,
        parameter_optimization_performed=False,
        research_2023_outcome_used=False,
        research_2026_outcome_used=False,
        primary_interpretation=dict(
            net_positive_both_years=[
                b
                for b in ("T1", "T2")
                if all(r["net_return_pct"] > 0 for r in results if r["benchmark"] == b)
            ],
            high_turnover_gross_net_comparison="NOT IDENTIFIABLE FROM NET-ONLY EXISTING REFERENCES; no new institutional gross replay",
        ),
    )
    (out / "summary.json").write_bytes(encoded(summary))
    (out / "methodology.json").write_bytes(encoded(methodology))
    (out / "daily-data-manifest.json").write_bytes(encoded(manifest))
    (out / "current-strategy-reference.json").write_bytes(encoded(references))
    write_csv(
        out / "benchmark-metrics-by-cost.csv",
        [
            dict(year=r["year"], benchmark=r["benchmark"], cost_mode=mode, **r[key])
            for r in results
            for mode, key in [
                ("GROSS", "gross_metrics"),
                ("CURRENT_PAPER_COST", "net_metrics"),
            ]
        ],
    )
    for filename, rows in [
        ("benchmark-results.csv", results),
        ("monthly-results.csv", months),
        ("leave-month-sensitivity.csv", leave),
        ("cost-decomposition.csv", cost_rows),
        ("execution-audit.csv", audit),
        ("daily-equity.csv", curves),
    ]:
        write_csv(out / filename, rows)
    validation = dict(
        source_code_sha256=sha(Path(__file__).read_bytes()),
        frozen_methodology_sha256=sha(encoded(methodology)),
        archives_verified=len(manifest),
        schema_fields=12,
        symbols=SYMBOLS,
        source_rows={s: len(data[s]) for s in SYMBOLS},
        source_sha256=sha(encoded(manifest)),
        reference_source_sha256={
            r["source"]: r["source_sha256"] for r in references if "source" in r
        },
        research_rows={
            str(y): (stamp(e) - stamp(s)) // DAY for y, (s, e) in PERIODS.items()
        },
        no_2026_archive=True,
        warmup_only_2023=True,
        no_db_access=True,
        production_db_writes=0,
        parameter_search=False,
        new_institutional_replay=False,
        deployment=False,
    )
    (out / "source-validation.json").write_bytes(encoded(validation))
    (out / "report.md").write_text(render_report(summary))
    bundle = package(out)
    print(
        "COMPLETE",
        category,
        candidate,
        str(bundle),
        sha(bundle.read_bytes()),
        flush=True,
    )


def render_report(summary):
    lines = [
        "# LOW-TURNOVER BENCHMARK DIAGNOSTIC v1",
        "",
        summary["final_category"],
        "next_architecture_candidate: " + summary["next_architecture_candidate"],
        "",
        "Research/architecture diagnostic only. No parameter search or production promotion.",
        "",
        "| Year | Benchmark | Gross % | Net % | Max DD % | Turnover (executed) | Fees + slippage incl terminal estimate | Entries/exits | Time invested % | Positive months |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in summary["results"]:
        lines.append(
            f"| {r['year']} | {r['benchmark']} | {r['gross_return_pct']:.4f} | {r['net_return_pct']:.4f} | {r['max_drawdown_pct']:.4f} | {r['turnover_notional']:.2f} | {r['total_fees'] + r['total_modeled_slippage']:.4f} | {r['entries']}/{r['exits']} | {r['time_in_market_pct']:.2f} | {r['positive_months']}/{r['total_months']} |"
        )
    lines += [
        "",
        "## Frozen interpretation and limitations",
        "",
        "Only T1/T2 are trend hypotheses; B0/B1/B2 are descriptive buy-and-hold references.",
        "The gross/net degradation uses independently simulated gross and cost-inclusive capital paths, so it includes compounding effects, not merely cash fees.",
        "Terminal values use the final completed 2024/2025 close less an estimated exit cost. This estimate is separately audited, never an executed fill. No 2026 open or outcome is consumed.",
        "Time in market is the fraction of cohort daily intervals with any asset held. T2 rebalances only on active-set changes; unchanged weights drift.",
        "Leave-month sensitivity excludes one calendar month of unchanged daily equity returns and compounds the others; it does not re-fit or remove candles from SMA history.",
        "Daily close drawdown cannot establish intraday drawdown. Sharpe is descriptive, not a statistical proof.",
        "",
        "## Leave-month robustness",
        "",
    ]
    for r in summary["results"]:
        if r["benchmark"].startswith("T"):
            lines.append(
                f"{r['year']} {r['benchmark']}: {r['positive_leave_months']}/{r['leave_month_count']} positive; worst {r['worst_leave_month_return_pct']:.4f}%, best {r['best_leave_month_return_pct']:.4f}%."
            )
    lines += [
        "",
        "## Cost sensitivity",
        "",
        "| Year | Benchmark | Gross minus net PnL | Degradation / absolute gross PnL |",
        "|---|---|---:|---:|",
    ]
    for r in summary["cost_decomposition"]:
        lines.append(
            f"| {r['year']} | {r['benchmark']} | {r['gross_minus_net_pnl']:.4f} | {r['cost_div_gross_absolute_pnl']:.6f} |"
        )
    lines += [
        "",
        "## Existing institutional references",
        "",
        "| Year | Run ID | Closed trades | Return % | PF | Fees + modeled slippage |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for r in summary["current_strategy_descriptive_reference"]:
        if r["status"] == "EXACT_PERIOD_DESCRIPTIVE_ONLY":
            lines.append(
                f"| {r['year']} | {r['run_id']} | {r['trade_count']} | {r['return_pct']:.4f} | {r['profit_factor']:.6f} | {r['total_modeled_cost']:.4f} |"
            )
        else:
            lines.append(f"{r['year']}: NOT DIRECTLY COMPARABLE.")
    lines += [
        "",
        "Existing 1h research references are descriptive only: different universe, allocation, exposure, and timing. Net-only artifacts cannot establish a causal gross-to-net degradation comparison.",
        "Both frozen trend benchmarks are net positive in 2024 but net negative in 2025. Neither supports a stable positive two-period conclusion. Lower cost drag alone does not establish an edge or identify the cause of the institutional losses.",
        "The frozen category wording overlaps: B explicitly covers a positive single year while C says neither benchmark positive in both years. This report applies the more specific mixed-year B rule; the underlying returns and next candidate NONE do not depend on that label convention.",
        "",
        "Source: " + DOC,
        "",
        "No production DB access/write, replay, service restart or deployment.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
