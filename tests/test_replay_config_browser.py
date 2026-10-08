"""Real desktop/mobile config import/export never queues a run implicitly."""

import json
import threading
from pathlib import Path

import pytest
from test_replay_config import imported
from test_replay_integration import web_case  # noqa: F401
from werkzeug.serving import make_server

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [1440, 390])
def test_config_import_export_copy_download_and_explicit_start(
    request, width, tmp_path
):
    client, db, _ = request.getfixturevalue("web_case")
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport=dict(width=width, height=960))
                page.add_init_script("""Object.defineProperty(navigator, 'clipboard', {
                    value: {writeText: async text => {window.copiedConfig = text;}}
                });""")
                errors, starts = [], []
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
                textarea = page.locator("#replay-config-json")
                status = page.locator("#replay-config-status")
                expected = imported() | dict(
                    stale_position_policy="RESEARCH_QUARANTINE_STALE"
                )
                textarea.fill(json.dumps(expected))
                page.locator("#replay-config-validate").click()
                playwright.expect(status).to_contain_text("Valid config imported")
                assert page.locator("#replay-mode").input_value() == "COMPARE RUN"
                assert (
                    page.locator("#replay-stale-policy").input_value()
                    == "RESEARCH_QUARANTINE_STALE"
                )
                assert page.locator(".replay-variant").count() == 2
                assert not page.locator("[data-key=require_watch]").first.is_checked()
                assert not page.locator("[data-key=require_delta]").first.is_checked()
                assert (
                    page.locator("[data-key=min_score]").first.input_value() == "73.25"
                )
                assert (
                    page.locator("[data-key=initial_paper_balance]")
                    .nth(1)
                    .input_value()
                    == "1234.56"
                )
                assert page.evaluate(
                    "document.querySelector('#replay-create').checkValidity()"
                )
                page.locator("#replay-config-export").click()
                playwright.expect(status).to_contain_text("exported")
                exported = json.loads(textarea.input_value())
                assert exported == expected
                page.locator("#replay-config-copy").click()
                playwright.expect(status).to_contain_text("copied")
                assert json.loads(page.evaluate("window.copiedConfig")) == expected
                with page.expect_download() as download:
                    page.locator("#replay-config-download").click()
                assert download.value.suggested_filename == "replay-config.json"
                saved = tmp_path / "download.json"
                download.value.save_as(saved)
                assert json.loads(saved.read_text()) == expected
                textarea.fill(saved.read_text())
                page.locator("#replay-config-validate").click()
                playwright.expect(status).to_contain_text("Valid config imported")
                for bad, field in [
                    ("{bad json", "$"),
                    (dict(expected, start="bad date"), "start"),
                    (
                        dict(expected, minimum_quote_volume="bad number"),
                        "minimum_quote_volume",
                    ),
                    (
                        dict(
                            expected,
                            variants=[dict(strategy="unknown", exit_policy="unknown")],
                        ),
                        "variants[0].strategy",
                    ),
                ]:
                    textarea.fill(bad if isinstance(bad, str) else json.dumps(bad))
                    page.locator("#replay-config-validate").click()
                    playwright.expect(status).to_contain_text("Validation failed")
                    playwright.expect(
                        page.locator("#replay-config-errors")
                    ).to_contain_text(field)
                    assert page.locator(".replay-variant").count() == 2
                    assert (
                        page.locator("#replay-name").input_value() == expected["name"]
                    )
                playwright.expect(
                    page.locator("#replay-config-errors")
                ).to_contain_text("variants[0].exit_policy")
                assert db.runs() == [] and starts == []
                textarea.fill(json.dumps(exported))
                page.locator("#replay-config-validate").click()
                playwright.expect(status).to_contain_text("Valid config imported")
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                artifacts = Path(__file__).parents[1] / "artifacts"
                artifacts.mkdir(exist_ok=True)
                page.screenshot(
                    path=str(artifacts / f"replay-config-{width}.png"), full_page=True
                )
                assert not errors and db.runs() == [] and starts == []
                page.locator("#replay-create button[type=submit]").click()
                page.wait_for_url("**/replay/runs/*")
                assert len(starts) == 1 and len(db.runs()) == 1
                assert (
                    db.get(db.runs()[0]["id"])["config"]["variants"][1]["exit_policy"]
                    == "trailing_pct"
                )
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
