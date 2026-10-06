"""Read-only analysis routes with bounded historical background work and exports."""

import csv
import io
import json
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import (
    Response,
    abort,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
from sqlalchemy import func, inspect, select, text

from crypto_bot.monitoring.service import build_info
from crypto_bot.storage.database import iso_utc, utcnow
from crypto_bot.storage.paper_models import (
    PaperAccount,
    PaperPosition,
    PortfolioSnapshot,
)
from crypto_bot.web.analysis_data import config, project, trade_query
from crypto_bot.web.analysis_data import text as safe_text
from crypto_bot.web.analysis_history import AnalysisHistory, HistoricalData
from crypto_bot.web.analysis_metrics import summary
from crypto_bot.web.analysis_readonly import ReadDatabase
from crypto_bot.web.trade_audit import milliseconds


def filters(args):
    allowed = {"status", "symbol", "strategy", "from", "to", "limit", "offset"}
    if set(args) - allowed or any(len(args.getlist(k)) != 1 for k in args):
        raise ValueError("Unsupported or repeated query parameter")
    out = {}
    for key in ("status", "symbol", "strategy"):
        if args.get(key):
            if len(args[key]) > 80:
                raise ValueError("Filter too long")
            out[key] = args[key]
    if "status" in out and out["status"] not in ("OPEN", "CLOSED"):
        raise ValueError("Expected OPEN or CLOSED")
    for key in ("from", "to"):
        if args.get(key):
            try:
                value = datetime.fromisoformat(args[key].replace("Z", "+00:00"))
                # Naive input is explicitly interpreted as UTC.
                out[key] = (
                    value.astimezone(timezone.utc).replace(tzinfo=None)
                    if value.tzinfo
                    else value
                )
            except ValueError as exc:
                raise ValueError("Invalid ISO UTC timestamp") from exc
    if "from" in out and "to" in out and out["from"] > out["to"]:
        raise ValueError("from must precede to")
    try:
        limit = int(args.get("limit", 500))
        offset = int(args.get("offset", 0))
    except ValueError as exc:
        raise ValueError("Invalid pagination") from exc
    if not 1 <= limit <= 1000 or not 0 <= offset <= 10000000:
        raise ValueError("limit must be 1..1000 and offset 0..10000000")
    return out, limit, offset


def conditions(value):
    result = []
    for key, column in [
        ("status", PaperPosition.status),
        ("symbol", PaperPosition.symbol),
        ("strategy", PaperPosition.strategy_name),
    ]:
        if key in value:
            result.append(column == value[key])
    if "from" in value:
        result.append(PaperPosition.opened_at >= value["from"])
    if "to" in value:
        result.append(PaperPosition.opened_at <= value["to"])
    return result


class AnalysisService:
    def __init__(self, database, history):
        self.database = ReadDatabase(database)
        self.history = history
        self.aggregate_cache = OrderedDict()
        self.lock = threading.RLock()
        self.aggregate_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="paper-analysis-summary"
        )
        self.aggregate_pending = set()

    def available(self):
        return inspect(self.database.engine).has_table("paper_positions")

    def trades(self, value, limit=500, offset=0, now=None):
        if not self.available():
            return [], 0
        now = now or utcnow()
        with self.database.session() as db:
            total = db.scalar(
                select(func.count(PaperPosition.id)).where(*conditions(value))
            )
            rows = db.execute(
                trade_query()
                .where(*conditions(value))
                .order_by(PaperPosition.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            # Finish DB read before scheduling network work.
            trades = [project(row, now) for row in rows]
        return [t | self.history.metrics(t, milliseconds(now)) for t in trades], total

    def meta(self, value, count):
        info = build_info()
        meta = dict(
            application_version=info["version"],
            git_commit=info["git_commit"],
            git_branch=info["git_branch"],
            database_schema_version=None,
            score_version=[],
            generated_at_utc=iso_utc(utcnow()),
            trade_count=count,
            scanner_timeframe=[],
            paper_config={},
            filters={
                k: iso_utc(v) if isinstance(v, datetime) else safe_text(v)
                for k, v in value.items()
            },
            read_only=True,
            consistency="CAPTURED_POSITION_IDS_WITH_PER_BATCH_AUDIT",
            money_encoding="DECIMAL_STRING",
            analysis_only=True,
        )
        if self.available():
            from crypto_bot.storage.models import Snapshot
            from crypto_bot.storage.paper_models import SignalRecord

            with self.database.session() as db:
                if inspect(self.database.engine).has_table("alembic_version"):
                    meta["database_schema_version"] = db.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar()
                account = db.get(PaperAccount, 1)
                if account:
                    meta["paper_config"] = config(account.configuration)
                rows = db.execute(
                    select(Snapshot.score_version, Snapshot.timeframe)
                    .join(SignalRecord, SignalRecord.snapshot_id == Snapshot.id)
                    .join(PaperPosition, PaperPosition.signal_id == SignalRecord.id)
                    .where(*conditions(value))
                    .distinct()
                )
                pairs = list(rows)
                meta["score_version"] = sorted({p[0] for p in pairs})
                meta["scanner_timeframe"] = sorted({p[1] for p in pairs})
        return meta

    def totals(self, value):
        key = (tuple(sorted(value.items())), self.history.revision)
        schedule = False
        with self.lock:
            cached = self.aggregate_cache.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            if key not in self.aggregate_pending and len(self.aggregate_pending) < 4:
                self.aggregate_pending.add(key)
                schedule = True
        if schedule:
            self.aggregate_executor.submit(self._compute_totals, value.copy(), key)
        with self.lock:
            cached = self.aggregate_cache.get(key)
            if cached:
                return cached[1]
        pending = summary([])
        pending.update(
            aggregation_status="PENDING",
            sample_message="Statistics are being calculated",
            current_equity=None,
            max_drawdown=None,
            generated_at_utc=iso_utc(utcnow()),
        )
        for name in list(pending):
            if isinstance(pending[name], (int, float)) and not isinstance(
                pending[name], bool
            ):
                pending[name] = None
        return pending

    def _compute_totals(self, value, key):
        try:
            result = self._calculate_totals(value)
        except Exception:
            result = summary([])
            result.update(
                aggregation_status="UNAVAILABLE",
                sample_message="Stored analysis temporarily unavailable",
            )
        with self.lock:
            self.aggregate_pending.discard(key)
            self.aggregate_cache[key] = (time.monotonic() + 30, result)
            while len(self.aggregate_cache) > 16:
                self.aggregate_cache.popitem(last=False)

    def _calculate_totals(self, value):
        trades = []
        ids = self.export_ids(value)
        for index in range(0, len(ids), 1000):
            trades.extend(self.export_batch(ids[index : index + 1000]))
        result = summary(trades)
        result["analysis_status_counts"] = {
            s: sum(t["analysis_data_status"] == s for t in trades)
            for s in ("READY", "PARTIAL", "PENDING", "UNAVAILABLE")
        }
        result["current_equity"] = None
        result["max_drawdown"] = None
        if self.available():
            with self.database.session() as db:
                latest = db.scalar(
                    select(PortfolioSnapshot)
                    .order_by(PortfolioSnapshot.id.desc())
                    .limit(1)
                )
                account = db.get(PaperAccount, 1)
                if latest:
                    result["current_equity"] = str(latest.total_equity)
                    result["max_drawdown"] = str(latest.max_drawdown)
                    result["portfolio_as_of_utc"] = iso_utc(latest.timestamp)
                elif account:
                    result["current_equity"] = str(account.cash_balance)
                    result["max_drawdown"] = str(account.max_drawdown)
        result["generated_at_utc"] = iso_utc(utcnow())
        result["aggregation_status"] = "READY"
        return result

    def export_ids(self, value):
        if not self.available():
            return []
        with self.database.session() as db:
            return list(
                db.scalars(
                    select(PaperPosition.id)
                    .where(*conditions(value))
                    .order_by(PaperPosition.id.desc())
                )
            )

    def export_batch(self, ids):
        now = utcnow()
        with self.database.session() as db:
            rows = db.execute(
                trade_query()
                .where(PaperPosition.id.in_(ids))
                .order_by(PaperPosition.id.desc())
            ).all()
            trades = [project(row, now) for row in rows]
        return [t | self.history.metrics(t, milliseconds(now)) for t in trades]

    def close(self):
        self.aggregate_executor.shutdown(wait=False, cancel_futures=True)
        self.history.close()
        self.database.close()


def csv_value(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    value = str(value)
    # Avoid spreadsheet formulas while retaining genuine negative numeric values.
    if value[:1] in ("=", "+", "-", "@", "\t", "\r") and not re.fullmatch(
        r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", value
    ):
        return "'" + value
    return value


def register_analysis(app, database, scanner_settings):
    history = AnalysisHistory(HistoricalData(scanner_settings.api_url))
    service = AnalysisService(database, history)
    app.extensions["paper_analysis"] = service

    def parsed():
        try:
            return filters(request.args)
        except ValueError as exc:
            abort(400, str(exc))

    @app.get("/paper/analysis")
    def paper_analysis():
        value, limit, offset = parsed()
        trades, count = service.trades(value, min(limit, 50), offset)
        return render_template(
            "paper_analysis.html",
            analysis=service.totals(value),
            trades=trades,
            total=count,
        )

    @app.get("/api/paper/analysis/trades")
    def analysis_trades():
        value, limit, offset = parsed()
        trades, count = service.trades(value, limit, offset)
        return jsonify(
            trades=trades,
            total=count,
            limit=limit,
            offset=offset,
            next_offset=offset + len(trades) if offset + len(trades) < count else None,
            read_only=True,
            consistency="PAGINATED_READ",
        )

    @app.get("/api/paper/analysis/summary")
    def analysis_summary():
        value, _, _ = parsed()
        return jsonify(service.totals(value))

    @app.get("/api/paper/analysis/meta")
    def analysis_meta():
        value, limit, offset = parsed()
        meta = service.meta(value, len(export_ids(value, limit, offset)))
        meta.update(
            export_scope="PAGE"
            if "limit" in request.args or "offset" in request.args
            else "ALL_MATCHING_TRADES"
        )
        return jsonify(meta)

    def export_ids(value, limit, offset):
        ids = service.export_ids(value)
        return (
            ids[offset : offset + limit]
            if "limit" in request.args or "offset" in request.args
            else ids
        )

    def export_pages(ids, limit):
        for index in range(0, len(ids), limit):
            yield service.export_batch(ids[index : index + limit])

    @app.get("/api/paper/analysis/export.json")
    def analysis_export_json():
        value, limit, offset = parsed()
        ids = export_ids(value, limit, offset)
        count = len(ids)
        paginated = "limit" in request.args or "offset" in request.args
        meta = service.meta(value, count)
        meta.update(
            export_scope="PAGE" if paginated else "ALL_MATCHING_TRADES",
            limit=limit if paginated else None,
            offset=offset,
        )

        def body():
            yield '{"metadata":' + json.dumps(meta, ensure_ascii=False) + ',"trades":['
            first = True
            for page in export_pages(ids, limit):
                for trade in page:
                    yield ("" if first else ",") + json.dumps(
                        trade, ensure_ascii=False, allow_nan=False
                    )
                    first = False
            yield "]}"

        return Response(
            stream_with_context(body()),
            mimetype="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="paper-analysis.json"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/paper/analysis/export.csv")
    def analysis_export_csv():
        value, limit, offset = parsed()
        ids = export_ids(value, limit, offset)

        def body():
            yield "\ufeff"  # Excel-compatible UTF-8 BOM.
            writer = None
            buffer = io.StringIO()
            for page in export_pages(ids, limit):
                for trade in page:
                    if writer is None:
                        writer = csv.DictWriter(buffer, fieldnames=list(trade))
                        writer.writeheader()
                    writer.writerow({k: csv_value(v) for k, v in trade.items()})
                    yield buffer.getvalue()
                    buffer.seek(0)
                    buffer.truncate()
            if writer is None:
                yield "position_id,symbol,status\r\n"

        return Response(
            stream_with_context(body()),
            content_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="paper-analysis.csv"',
                "Cache-Control": "no-store",
            },
        )
