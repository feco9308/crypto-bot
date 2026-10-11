"""Explicit research start, exports, registry selection and mobile results."""

import threading
from pathlib import Path

import pytest
from test_replay_integration import Stops, web_case  # noqa: F401
from test_shadow_exit import completed_source
from werkzeug.serving import make_server

from crypto_bot.replay.shadow_engine import ShadowEngine
from crypto_bot.replay.shadow_store import ShadowStore

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [1440, 390])
def test_shadow_browser_validation_explicit_start_and_matched_results(request, width):
    client, source, _ = request.getfixturevalue("web_case")
    completed_source(source)
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
                base = f"http://127.0.0.1:{server.server_port}"
                page.goto(base + "/replay/shadow-exit")
                assert page.locator(".shadow-exit-policy").count() == 4
                page.locator("#shadow-add").click()
                page.locator(".shadow-exit-policy").last.select_option("profit_lock")
                assert (
                    '"lock_pct"'
                    in page.locator(".shadow-exit-params").last.input_value()
                )
                page.locator("#shadow-validate").click()
                playwright.expect(page.locator("#shadow-status")).to_contain_text(
                    "no job started"
                )
                store = ShadowStore(source.path.parent / "shadow-exit.db")
                assert store.jobs() == []
                page.locator('button[type="submit"]').click()
                page.wait_for_url("**/replay/shadow-exit/*")
                ident = page.url.rsplit("/", 1)[-1]
                result = ShadowEngine(Stops()).run(store.get(ident, True))
                store.update(ident, status="COMPLETED", result=result)
                page.reload()
                playwright.expect(page.locator("#shadow-summary tr")).to_have_count(5)
                playwright.expect(page.locator("#shadow-pairwise tr")).to_have_count(10)
                playwright.expect(
                    page.locator("#shadow-anchor-summary")
                ).to_contain_text("canonical FILLED BUYs")
                for group in ("symbol", "atr_pct", "range_position", "btc_regime"):
                    page.locator("#shadow-group").select_option(group)
                    assert page.locator("#shadow-breakdown tr").count() > 0
                with page.expect_download() as event:
                    page.get_by_text("Compact JSON", exact=True).click()
                assert event.value.suggested_filename.endswith("compact.json")
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                assert not errors
                artifacts = Path(__file__).parents[1] / "artifacts"
                artifacts.mkdir(exist_ok=True)
                page.screenshot(
                    path=str(artifacts / f"shadow-research-{width}.png"), full_page=True
                )
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
