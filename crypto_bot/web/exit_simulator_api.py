"""GET-only counterfactual routes over the existing read-only analysis service."""

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from flask import abort, jsonify, request

from crypto_bot.storage.database import iso_utc, utcnow
from crypto_bot.web.analysis_metrics import actual_exit_metrics
from crypto_bot.web.exit_simulator import aggregate, scenarios, simulate
from crypto_bot.web.trade_audit import milliseconds


class ExitSimulator:
    def __init__(self, analysis, executor=None):
        self.analysis = analysis
        self.executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="exit-simulator"
        )
        self.lock = threading.RLock()
        self.cache = OrderedDict()
        self.pending = set()
        self.summary_cache = OrderedDict()
        self.summary_pending = set()

    def trade(self, trade, selected=None):
        metrics, path = self.analysis.history.path(trade, milliseconds(utcnow()))
        out = {
            k: trade.get(k)
            for k in (
                "position_id",
                "symbol",
                "entry_time",
                "exit_time",
                "entry_fill_price",
                "quantity",
                "entry_quote_drift_pct",
                "entry_quote_drift_atr",
            )
        }
        out.update(metrics, **actual_exit_metrics(trade, metrics))
        out["original_stop_price"] = trade["stop_price"]
        out["execution_cost_config"] = {
            k: trade.get("paper_config_snapshot", {}).get(k)
            for k in ("paper_fee_pct", "paper_slippage_pct")
        }
        out["scenarios"] = []
        out["simulation_status"] = (
            "PENDING" if metrics["analysis_data_status"] == "PENDING" else "UNAVAILABLE"
        )
        out["scenarios"] = simulate(trade, (), "1m", selected)
        for record in out["scenarios"]:
            record["data_timeframe"] = None
            if record["scenario_type"] != "BASELINE":
                record["simulation_status"] = out["simulation_status"]
        if selected == "BASELINE":
            out["simulation_status"] = "READY"
        if path:
            interval, candles, revision = path
            # Entry/exit audit and the immutable historical tuple identify cached calculations.
            key = (
                trade["position_id"],
                trade["entry_ms"],
                trade["exit_ms"],
                trade["realized_pnl"],
                trade["entry_fill_price"],
                trade["quantity"],
                trade["notional"],
                trade["entry_fee"],
                trade["exit_fee"],
                trade["exit_fill_price"],
                trade["stop_price"],
                repr(trade["paper_config_snapshot"]),
                revision,
            )
            with self.lock:
                cached = self.cache.get(key)
                if cached and cached[0] > time.monotonic():
                    self.cache.move_to_end(key)
                    out["scenarios"] = [
                        s
                        for s in cached[1]
                        if not selected or s["scenario_name"] == selected
                    ]
                    out["simulation_status"] = (
                        "READY"
                        if all(s["usable"] for s in out["scenarios"])
                        else "UNAVAILABLE"
                    )
                else:
                    out["simulation_status"] = "PENDING"
                    if key not in self.pending and len(self.pending) < 32:
                        self.pending.add(key)
                        self.executor.submit(
                            self._calculate, key, trade.copy(), candles, interval
                        )
        return out

    def _calculate(self, key, trade, candles, interval):
        try:
            result = simulate(trade, candles, interval)
        except Exception:
            # Isolate analysis failures; expose unavailable instead of invented results.
            result = simulate(trade, (), interval)
        with self.lock:
            self.pending.discard(key)
            self.cache[key] = (time.monotonic() + 3600, result)
            while len(self.cache) > 512:
                self.cache.popitem(last=False)

    def totals(self, value, selected):
        key = (tuple(sorted(value.items())), selected)
        with self.lock:
            cached = self.summary_cache.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            if key not in self.summary_pending and len(self.summary_pending) < 4:
                self.summary_pending.add(key)
                self.analysis.aggregate_executor.submit(
                    self._totals, value.copy(), selected, key
                )
            if cached:
                return cached[1]
        return aggregate([], selected) | {
            "aggregation_status": "PENDING",
            "sample_size": None,
        }

    def _totals(self, value, selected, key):
        try:
            trades = []
            ids = self.analysis.export_ids(value)
            for offset in range(0, len(ids), 100):
                trades.extend(
                    self.trade(t, selected)
                    for t in self.analysis.export_batch(ids[offset : offset + 100])
                )
            result = aggregate(trades, selected)
            result["simulation_status_counts"] = {
                status: sum(t["simulation_status"] == status for t in trades)
                for status in ("READY", "PENDING", "UNAVAILABLE")
            }
            result["aggregation_status"] = "READY"
            result["generated_at_utc"] = iso_utc(utcnow())
        except Exception:
            result = aggregate([], selected) | {
                "aggregation_status": "UNAVAILABLE",
                "sample_size": None,
            }
        with self.lock:
            self.summary_pending.discard(key)
            ttl = 3 if result.get("simulation_status_counts", {}).get("PENDING") else 30
            self.summary_cache[key] = (time.monotonic() + ttl, result)
            while len(self.summary_cache) > 16:
                self.summary_cache.popitem(last=False)

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


def register_exit_simulator(app, analysis):
    from crypto_bot.web.analysis import filters

    simulator = ExitSimulator(analysis)
    analysis.simulator = simulator
    app.extensions["paper_exit_simulator"] = simulator

    def parsed():
        allowed = {"position_id", "symbol", "from", "to", "scenario", "limit", "offset"}
        if set(request.args) - allowed or any(
            len(request.args.getlist(k)) != 1 for k in request.args
        ):
            abort(400, "Unsupported or repeated simulator parameter")
        args = request.args.copy()
        selected = args.pop("scenario", None) or None
        if selected and selected not in {s.name for s in scenarios()}:
            abort(400, "Unknown scenario")
        position_id = args.pop("position_id", None)
        try:
            if "limit" not in args:
                args["limit"] = "50"
            value, limit, offset = filters(args)
            value["status"] = "CLOSED"
            if position_id is not None:
                position_id = int(position_id)
                if position_id <= 0:
                    raise ValueError("position_id must be positive")
                value["position_id"] = position_id
        except ValueError as exc:
            abort(400, str(exc))
        return value, min(limit, 100), offset, selected

    @app.get("/api/paper/analysis/exit-simulator")
    def exit_simulator_trades():
        value, limit, offset, selected = parsed()
        trades, count = analysis.trades(value, limit, offset)
        return jsonify(
            trades=[simulator.trade(t, selected) for t in trades],
            total=count,
            limit=limit,
            offset=offset,
            next_offset=offset + len(trades) if offset + len(trades) < count else None,
            scenario_catalog=[
                dict(
                    scenario_name=s.name, scenario_type=s.kind, parameters=s.parameters
                )
                for s in scenarios()
            ],
            read_only=True,
            counterfactual_only=True,
            result_basis="CONSERVATIVE",
        )

    @app.get("/api/paper/analysis/exit-simulator/summary")
    def exit_simulator_summary():
        value, _, _, selected = parsed()
        return jsonify(simulator.totals(value, selected))
