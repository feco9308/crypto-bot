"""Institutional builder import/export uses the same schema, without implicit start."""

import json
import os
import threading
from pathlib import Path

import pytest
from test_replay_integration import web_case  # noqa: F401
from werkzeug.serving import make_server

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [1440, 390])
def test_institutional_builder_roundtrip_desktop_mobile(request, width, tmp_path):
    client, db, _ = request.getfixturevalue("web_case")
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport=dict(width=width, height=960))
                starts = []
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on(
                    "request",
                    lambda r: (
                        starts.append(r.url)
                        if r.method == "POST" and r.url.endswith("/api/replay/runs")
                        else None
                    ),
                )
                page.goto(f"http://127.0.0.1:{server.server_port}/replay")
                assert (
                    page.locator("[data-key=strategy]").input_value()
                    == "watchlist_reference_v1"
                )
                raw = json.loads(
                    (
                        Path(__file__).resolve().parents[1]
                        / "docs/replay/institutional-style-v1-2024.config.json"
                    ).read_text()
                )
                page.locator("#replay-config-json").fill(json.dumps(raw))
                page.locator("#replay-config-validate").click()
                playwright.expect(
                    page.locator("#replay-config-status")
                ).to_contain_text("Valid config imported")
                assert page.locator(".replay-variant").count() == 4
                assert (
                    page.locator("[data-key=strategy]").nth(2).input_value()
                    == "institutional_style_v1"
                )
                assert (
                    page.locator(
                        "[data-key=correlation_lookback_hours]"
                    ).first.input_value()
                    == "168"
                )
                assert (
                    page.locator(
                        "[data-key=btc_neutral_risk_multiplier]"
                    ).first.input_value()
                    == "0.5"
                )
                assert (
                    page.locator(
                        "[data-key=activation_break_even_r]"
                    ).first.input_value()
                    == "0.75"
                )
                assert page.locator("[data-key=require_1d_bullish]").first.is_checked()
                page.locator("#replay-config-export").click()
                playwright.expect(
                    page.locator("#replay-config-status")
                ).to_contain_text("exported")
                assert (
                    json.loads(page.locator("#replay-config-json").input_value()) == raw
                )
                assert not starts and not errors and db.runs() == []
                artifact = Path(
                    os.environ.get("RESEARCH_BROWSER_ARTIFACT_DIR", str(tmp_path))
                )
                artifact.mkdir(parents=True, exist_ok=True)
                page.screenshot(
                    path=str(artifact / f"institutional-builder-{width}.png"),
                    full_page=True,
                )
                page.locator("[data-key=strategy]").nth(2).select_option(
                    "watchlist_reference_v1"
                )
                assert (
                    page.locator("[data-key=correlation_lookback_hours]").count() == 1
                )
                assert not starts
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
