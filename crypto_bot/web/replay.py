"""Replay-only resources. Routes never receive the production database for writes."""

import hmac
import os
import time
from pathlib import Path

from flask import Response, abort, jsonify, render_template, request, session
from werkzeug.exceptions import BadRequest, UnsupportedMediaType

from crypto_bot.replay.cache import CandleCache
from crypto_bot.replay.config import (
    ConfigValidationError,
    input_config,
    invalid,
    isolated_paths,
    validate,
)
from crypto_bot.replay.exporter import bundle, csv_export, payload
from crypto_bot.replay.models import ReplayStore
from crypto_bot.replay.registry import EXITS, STRATEGIES


def register_replay(app, production_database):
    production = production_database.engine.url.database
    root = (
        Path(production).resolve().parent
        if production and production != ":memory:"
        else Path("data").resolve()
    )

    def store():
        database, cache = isolated_paths(
            os.getenv("REPLAY_DATA_DIR", str(root)),
            production if production and production != ":memory:" else None,
        )
        return ReplayStore(database), cache

    def run(run_id):
        try:
            return store()[0].get(run_id)
        except KeyError:
            abort(404, "Replay run not found")

    def variant(value):
        try:
            n = int(request.args.get("variant", 0))
            if not 0 <= n < len(value["variants"]):
                raise ValueError()
            return n
        except ValueError:
            abort(400, "Unknown replay variant")

    def csrf():
        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
        if not token or not hmac.compare_digest(token, session.get("csrf", "")):
            abort(403, "Invalid CSRF token; reload the Replay Lab")

    @app.errorhandler(ConfigValidationError)
    def replay_validation_error(error):
        return jsonify(valid=False, errors=error.errors), 400

    def json_body():
        try:
            return request.get_json()
        except (BadRequest, UnsupportedMediaType):
            invalid("$", "Malformed JSON; supply a valid JSON object")

    @app.post("/api/replay/config/validate")
    def replay_validate_config():
        csrf()
        # Pure validation: no store, run creation, preset save or worker action.
        return jsonify(valid=True, config=input_config(validate(json_body())))

    @app.before_request
    def replay_body_limit():
        if request.path.startswith("/api/replay/"):
            request.max_content_length = 65536

    @app.get("/replay")
    def replay_home():
        db, _ = store()
        worker = db.worker()
        if worker.get("heartbeat") and time.time() - worker["heartbeat"] > 30:
            worker["status"] = "STALE"
        return render_template(
            "replay.html",
            runs=db.runs(),
            worker=worker,
            strategies=list(STRATEGIES.items),
            exits=EXITS.items,
            presets=db.presets(),
        )

    @app.post("/api/replay/runs")
    def replay_create():
        csrf()
        try:
            config = validate(json_body())
            run_id = store()[0].create(config)
        except ConfigValidationError:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            abort(400, str(exc))
        return jsonify(run_id=run_id, url=f"/replay/runs/{run_id}"), 202

    @app.get("/api/replay/runs")
    def replay_runs_api():
        return jsonify(runs=store()[0].runs(), worker=store()[0].worker())

    @app.get("/api/replay/runs/<run_id>")
    def replay_run_api(run_id):
        return jsonify(run(run_id))

    @app.post("/api/replay/runs/<run_id>/<action>")
    def replay_control(run_id, action):
        csrf()
        db, _ = store()
        original = run(run_id)
        if action == "clone":
            return jsonify(run_id=db.create(original["config"])), 202
        commands = {"pause": "PAUSE", "resume": "RUN", "cancel": "CANCEL"}
        if action not in commands:
            abort(404)
        try:
            db.control(run_id, commands[action])
        except ValueError as exc:
            abort(409, str(exc))
        return jsonify(run(run_id))

    @app.get("/replay/runs/<run_id>")
    def replay_run_page(run_id):
        value = run(run_id)
        return render_template("replay_run.html", run=value, variant=variant(value))

    @app.get("/api/replay/runs/<run_id>/results")
    def replay_results(run_id):
        value = run(run_id)
        index = variant(value)
        db, _ = store()
        trades, curve = db.data(run_id, index)
        # Tables are paginated; exports contain every CLOSED trade.
        try:
            offset = int(request.args.get("offset", 0))
            limit = int(request.args.get("limit", 100))
            if offset < 0 or not 1 <= limit <= 1000:
                raise ValueError()
        except ValueError:
            abort(400, "Invalid replay pagination")
        return jsonify(
            summary=value["variants"][index]["summary"],
            trades=trades[offset : offset + limit],
            trade_count=len(trades),
            equity_curve=curve,
            variant=index,
        )

    @app.get("/replay/runs/<run_id>/trades/<int:trade_id>")
    def replay_trade_page(run_id, trade_id):
        value = run(run_id)
        index = variant(value)
        db, _ = store()
        trades, _ = db.data(run_id, index)
        trade = next((t for t in trades if t["position_id"] == trade_id), None)
        if not trade:
            abort(404)
        return render_template(
            "replay_trade.html",
            run=value,
            variant=index,
            trade=trade,
            events=db.audit(run_id, index, trade_id),
        )

    @app.get("/api/replay/runs/<run_id>/trades/<int:trade_id>/chart")
    def replay_trade_chart(run_id, trade_id):
        value = run(run_id)
        index = variant(value)
        db, cache_path = store()
        trades, _ = db.data(run_id, index)
        trade = next((t for t in trades if t["position_id"] == trade_id), None)
        if not trade:
            abort(404)
        from crypto_bot.replay.clock import ms

        start = ms(trade["entry_time"]) - 2 * 3600000
        end = ms(trade["exit_time"] or value["config"]["end"]) + 3600000
        try:
            interval = request.args.get("interval", "1m")
            limit = int(request.args.get("limit", 1000))
            cursor = int(request.args.get("start", start))
        except ValueError:
            abort(400)
        if (
            interval not in ("1m", "5m", "1h")
            or not 1 <= limit <= 5000
            or not start <= cursor <= end
        ):
            abort(400)
        # Cache-only chart: GET cannot download data or affect replay decisions.
        cache = CandleCache(cache_path)
        rows = cache.rows(trade["symbol"], interval, cursor, end, limit)
        return jsonify(
            candles=[
                dict(time=r[0], open=r[2], high=r[3], low=r[4], close=r[5])
                for r in rows
            ],
            trade=trade,
            next_start=rows[-1][0] + 1
            if len(rows) == limit and rows[-1][0] < end
            else None,
            source="REPLAY_LOCAL_CACHE_VISUALIZATION_ONLY",
        )

    @app.get("/api/replay/runs/<run_id>/export.<format>")
    def replay_export(run_id, format):
        value = run(run_id)
        index = variant(value)
        db, _ = store()
        if value["status"] != "COMPLETED":
            abort(409, "Replay export requires a completed run")
        import json

        if format in ("json", "compact.json"):
            body = json.dumps(
                payload(db, run_id, index, format == "compact.json"),
                ensure_ascii=False,
                allow_nan=False,
            )
            mime = "application/json"
        elif format == "csv":
            body = csv_export(db, run_id, index)
            mime = "text/csv; charset=utf-8"
        elif format == "zip":
            body = bundle(db, run_id, index)
            mime = "application/zip"
        else:
            abort(404)
        return Response(
            body,
            content_type=mime,
            headers={
                "Content-Disposition": f'attachment; filename="replay-analysis-{run_id}-variant-{index}.{format}"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/replay/compare")
    def replay_compare_page():
        return render_template(
            "replay_compare.html", choices=store()[0].compare_options()
        )

    @app.get("/api/replay/compare")
    def replay_compare_api():
        choices = request.args.getlist("selection")
        if not 2 <= len(choices) <= 10 or len(set(choices)) != len(choices):
            abort(400, "Choose 2..10 distinct completed runs/variants")
        results = []
        for choice in choices:
            try:
                run_id, index = choice.split(":")
                index = int(index)
            except ValueError:
                abort(400, "Expected run_id:variant")
            value = run(run_id)
            if value["status"] != "COMPLETED" or not 0 <= index < len(
                value["variants"]
            ):
                abort(400, "Choose completed variants")
            selected = value["variants"][index]
            results.append(
                dict(
                    run_id=run_id,
                    variant=index,
                    config=selected["config"],
                    period=[value["config"]["start"], value["config"]["end"]],
                    dataset_role=value["config"]["dataset_role"],
                    summary=selected["summary"]["metrics"],
                )
            )
        periods = {tuple(r["period"]) for r in results}
        return jsonify(
            variants=results,
            not_statistically_validated=True,
            periods_match=len(periods) == 1,
            comparison_warning=None
            if len(periods) == 1
            else "Different periods; returns are not directly comparable",
        )

    @app.get("/replay/presets")
    def replay_presets_page():
        return render_template("replay_presets.html", presets=store()[0].presets())

    @app.route("/api/replay/presets", methods=["GET", "POST"])
    def replay_presets_api():
        db, _ = store()
        if request.method == "POST":
            csrf()
            try:
                value = json_body()
                db.presets(value["name"], validate(value["config"]))
            except ConfigValidationError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                abort(400, str(exc))
        return jsonify(presets=db.presets())
