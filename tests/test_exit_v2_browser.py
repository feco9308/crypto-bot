"""Five-variant config import/export without starting a research run."""

import json
import threading
from pathlib import Path

import pytest
from test_replay_integration import web_case  # noqa: F401
from werkzeug.serving import make_server

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [1440, 390])
def test_exit_v2_builder_roundtrip_no_start(request, width):
    client, _, _ = request.getfixturevalue("web_case")
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
                raw = json.loads(
                    (
                        Path(__file__).resolve().parents[1]
                        / "docs/replay/institutional-exit-v2-2024.config.json"
                    ).read_text()
                )
                page.locator("#replay-config-json").fill(json.dumps(raw))
                page.locator("#replay-config-validate").click()
                playwright.expect(
                    page.locator("#replay-config-status")
                ).to_contain_text("Valid config imported")
                assert page.locator(".replay-variant").count() == 5
                assert (
                    page.locator("[data-key=setup_rearm]").nth(4).input_value()
                    == "setup_rearm_v1"
                )
                assert (
                    page.locator("[data-key=exit_policy]").nth(2).input_value()
                    == "break_even_guard_v1"
                )
                page.locator("#replay-config-export").click()
                playwright.expect(
                    page.locator("#replay-config-status")
                ).to_contain_text("exported")
                exported = json.loads(page.locator("#replay-config-json").input_value())
                assert exported["variants"][4]["setup_rearm"] == "setup_rearm_v1"
                assert (
                    exported["variants"][3]["exit_parameters"][
                        "peak_trail_atr_multiplier"
                    ]
                    == "1.25"
                )
                assert not starts and not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
