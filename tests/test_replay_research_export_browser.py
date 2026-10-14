"""Real browser coverage: prominent whole-run export on desktop and mobile."""

import json
import threading
import zipfile
from pathlib import Path

import pytest
from test_replay_companion_isolation import controlled  # noqa: F401
from test_replay_integration import web_case  # noqa: F401
from test_replay_research_export import research_case  # noqa: F401
from werkzeug.serving import make_server

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("labels", ["ABCDE", "AFGH"])
def test_complete_research_button_browser(request, width, labels):
    if labels == "AFGH":
        from test_institutional_entry_v1 import small_run

        client, writable, _ = request.getfixturevalue("web_case")
        request.getfixturevalue("controlled")
        store, rid = small_run(writable.path.parent, list(labels))
    else:
        client, store, rid = request.getfixturevalue("research_case")
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    artifacts = Path(__file__).parents[1] / "artifacts"
    artifacts.mkdir(exist_ok=True)
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport=dict(width=width, height=960))
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                base = f"http://127.0.0.1:{server.server_port}"
                page.goto(base + f"/replay/runs/{rid}")
                button = page.locator("#replay-research-download")
                playwright.expect(button).to_be_visible()
                playwright.expect(button).to_have_attribute("aria-disabled", "false")
                assert page.evaluate("""() => {
                    const progress = document.querySelector('#replay-progress').closest('section');
                    const research = document.querySelector('#replay-research-export');
                    const results = document.querySelector('#replay-result-variant').closest('section');
                    return Boolean(progress.compareDocumentPosition(research) & Node.DOCUMENT_POSITION_FOLLOWING)
                        && Boolean(research.compareDocumentPosition(results) & Node.DOCUMENT_POSITION_FOLLOWING);
                }""")
                button.scroll_into_view_if_needed()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                index = len(labels) - 1
                page.locator("#replay-result-variant").select_option(str(index))
                playwright.expect(
                    page.locator('[data-replay-export="zip"]')
                ).to_have_attribute(
                    "href", f"/api/replay/runs/{rid}/export.zip?variant={index}"
                )
                playwright.expect(button).to_have_attribute(
                    "href", f"/api/replay/runs/{rid}/research-bundle.zip"
                )
                with page.expect_download() as download:
                    button.click()
                assert download.value.suggested_filename == f"replay-research-{rid}.zip"
                with zipfile.ZipFile(download.value.path()) as archive:
                    for label in labels:
                        assert label + ".analysis.json" in archive.namelist()
                    assert json.loads(archive.read("manifest.json"))["run_id"] == rid
                store.update(rid, status="RUNNING")
                page.reload()
                playwright.expect(button).to_have_attribute("aria-disabled", "true")
                assert button.get_attribute("href") is None
                store.update(rid, status="COMPLETED")
                playwright.expect(button).to_have_attribute("aria-disabled", "false")
                button.scroll_into_view_if_needed()
                page.screenshot(
                    path=str(
                        artifacts / f"replay-research-export-{labels}-{width}.png"
                    ),
                    full_page=False,
                )
                assert not errors
                assert len(store.runs()) == 1
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
