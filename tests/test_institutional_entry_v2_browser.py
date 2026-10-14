"""Import frozen hypotheses into the existing UI without starting research."""

import json
import threading
from pathlib import Path

import pytest
from test_replay_integration import web_case  # noqa: F401
from werkzeug.serving import make_server

from crypto_bot.replay.config import validate

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [1440, 390])
def test_fresh_acceleration_config_ui_roundtrip(request, width):
    client, store, _ = request.getfixturevalue("web_case")
    expected = json.loads(
        (
            Path(__file__).parents[1]
            / "docs/replay/institutional-entry-v2-2023-validation.config.json"
        ).read_text()
    )
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
                page.on(
                    "request",
                    lambda r: (
                        starts.append(r.url)
                        if r.method == "POST" and r.url.endswith("/api/replay/runs")
                        else None
                    ),
                )
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{server.server_port}/replay")
                page.locator("#replay-config-json").fill(json.dumps(expected))
                page.locator("#replay-config-validate").click()
                playwright.expect(
                    page.locator("#replay-config-status")
                ).to_contain_text("Valid config imported")
                assert page.locator(".replay-variant").count() == 4
                assert (
                    page.locator("[data-key=min_delta_4h]").nth(1).input_value()
                    == "15.0"
                )
                assert (
                    page.locator("[data-key=max_relative_volume]").nth(2).input_value()
                    == "2.0"
                )
                assert (
                    page.locator("[data-key=min_delta_4h]").nth(3).input_value()
                    == "15.0"
                )
                assert (
                    page.locator("[data-key=max_relative_volume]").nth(3).input_value()
                    == "2.0"
                )
                page.locator("#replay-config-export").click()
                playwright.expect(
                    page.locator("#replay-config-status")
                ).to_contain_text("exported")
                assert validate(
                    json.loads(page.locator("#replay-config-json").input_value())
                ) == validate(expected)
                assert store.runs() == [] and starts == [] and not errors
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
