"""Database-only web app. No market requests or scanner scheduling in workers."""
import hmac
import secrets
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from crypto_bot.config.settings import Settings
from crypto_bot.storage.database import Database
from crypto_bot.scanner.selection import OVERRIDES


def create_app(settings=None, database=None):
    settings = settings or Settings.load()
    database = database or Database(settings.database_url)
    database.initialize()
    app = Flask(__name__)
    app.secret_key = settings.secret_key or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict", MAX_CONTENT_LENGTH=4096)
    app.extensions["market_database"] = database

    @app.context_processor
    def common():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(32)
        return dict(csrf=session["csrf"], statuses=sorted(OVERRIDES), settings=settings)

    @app.template_filter("number")
    def number(value, digits=2):
        return "—" if value is None else f"{value:,.{digits}f}"

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.get("/")
    def index():
        rows = database.latest_rows(settings.timeframe)
        top = sorted((r for r in rows if not r["stale"]), key=lambda r: -r["total_score"])
        risers = sorted((r for r in top if (r["momentum"].get(f"delta_{settings.riser_window}h") or 0) > 0), key=lambda r: -r["momentum"][f"delta_{settings.riser_window}h"])
        sections = {"TOP MARKET SCORE": top[:settings.top_score_size], f"TOP SCORE RISERS · Δ{settings.riser_window}h": risers[:settings.top_risers_size], "AUTO WATCHLIST": [r for r in top if r["algorithm_watch"] and r["user_override"] != "IGNORE"], "MANUAL WATCHLIST": [r for r in rows if r["user_override"] == "WATCH"], "PINNED": [r for r in rows if r["user_override"] == "PINNED"], "IGNORED": [r for r in rows if r["user_override"] == "IGNORE"]}
        return render_template("index.html", rows=rows, sections=sections, run=database.latest_run(settings.timeframe), overrides=database.overrides())

    @app.get("/instrument/<symbol>")
    def detail(symbol):
        rows = {r["symbol"]: r for r in database.latest_rows(settings.timeframe)}
        history = database.history(symbol, settings.timeframe)
        if not history:
            abort(404, "No score history for this instrument/timeframe")
        row = rows.get(symbol)
        if row is None:
            from crypto_bot.scanner.selection import effective_state
            override = database.overrides().get(symbol, "AUTO")
            row = database.serialize(history[-1]) | dict(algorithm_watch=False, algorithm_reason="Outside latest scan", user_override=override, effective_state=effective_state(False, override), stale=True)
        return render_template("detail.html", row=row, history=[database.serialize(h) for h in history])

    @app.get("/api/markets")
    def api_markets():
        return jsonify(dict(run=database.latest_run(settings.timeframe), markets=database.latest_rows(settings.timeframe)))

    @app.get("/api/history/<symbol>")
    def api_history(symbol):
        history = database.history(symbol, settings.timeframe)
        if not history:
            abort(404)
        return jsonify([database.serialize(h) for h in history])

    @app.post("/override")
    def override():
        csrf = request.form.get("csrf", "")
        if not csrf or not hmac.compare_digest(csrf, session.get("csrf", "")):
            abort(403, "Invalid CSRF token; reload the page")
        symbol = request.form.get("symbol", "").upper()
        status = request.form.get("status", "")
        try:
            database.set_override(symbol, status)
        except ValueError:
            abort(400, "Invalid status")
        except KeyError:
            abort(404, "Unknown instrument; run market discovery first")
        return redirect(url_for("index"))

    return app
