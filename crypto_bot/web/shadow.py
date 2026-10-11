"""Research pages/API receive only isolated replay storage callbacks."""

import json

from flask import Response, abort, jsonify, render_template

from crypto_bot.replay.registry import EXITS
from crypto_bot.replay.shadow_config import validate
from crypto_bot.replay.shadow_metrics import export
from crypto_bot.replay.shadow_store import ShadowStore


def register_shadow(app, replay_store, csrf, json_body):
    def stores():
        source, _ = replay_store()
        return source, ShadowStore(source.path.parent / "shadow-exit.db")

    def get(ident):
        try:
            return stores()[1].get(ident)
        except KeyError:
            abort(404, "Shadow research not found")

    @app.get("/replay/shadow-exit")
    def shadow_home():
        source, db = stores()
        return render_template(
            "shadow.html",
            choices=source.compare_options(),
            jobs=db.jobs(),
            exits=EXITS.items,
        )

    @app.post("/api/replay/shadow/config/validate")
    def shadow_validate():
        csrf()
        source, _ = replay_store()
        return jsonify(valid=True, config=validate(json_body(), source))

    @app.post("/api/replay/shadow/runs")
    def shadow_create():
        csrf()
        source, db = stores()
        config = validate(json_body(), source)
        try:
            ident = db.create(config, source)
        except ValueError as exc:
            abort(400, str(exc))
        return jsonify(run_id=ident, url=f"/replay/shadow-exit/{ident}"), 202

    @app.get("/replay/shadow-exit/<ident>")
    def shadow_result_page(ident):
        get(ident)
        return render_template("shadow_result.html", ident=ident)

    @app.get("/api/replay/shadow/runs/<ident>")
    def shadow_result(ident):
        job = get(ident)
        if job["result"]:
            job["result"] = export(job["result"], compact=True)
        return jsonify(job)

    @app.get("/api/replay/shadow/runs/<ident>/export.<kind>")
    def shadow_export(ident, kind):
        if kind not in ("compact.json", "json"):
            abort(404)
        job = get(ident)
        if job["status"] != "COMPLETED":
            abort(409, "Complete shadow research before exporting")
        return Response(
            json.dumps(
                export(job["result"], compact=kind == "compact.json"),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="shadow-{ident}.{kind}"'
            },
        )
