"""Historical blocked-entry reasons and missing-mark warnings render on mobile."""

import threading
from pathlib import Path

import pytest
from test_replay_integration import web_case  # noqa: F401
from test_replay_rejections import save_legacy
from werkzeug.serving import make_server

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [1440, 390])
def test_legacy_blocked_reasons_render_with_stale_mark_warning(request, width):
    client, db, _ = request.getfixturevalue("web_case")
    run = save_legacy(db)
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport=dict(width=width, height=960))
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{server.server_port}/replay/runs/{run}")
                playwright.expect(
                    page.locator("#replay-blocked-reasons")
                ).to_contain_text("Fresh portfolio marks unavailable")
                playwright.expect(
                    page.locator("#replay-blocked-reasons")
                ).to_contain_text("invalid stop distance")
                playwright.expect(
                    page.locator("#replay-blocked-warning")
                ).to_contain_text("Open equity can include stale marks")
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                artifacts = Path(__file__).parents[1] / "artifacts"
                artifacts.mkdir(exist_ok=True)
                page.screenshot(
                    path=str(artifacts / f"replay-rejections-{width}.png"),
                    full_page=True,
                )
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
