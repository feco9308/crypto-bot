"""Real Chromium validation of replay forms, result charts and mobile navigation."""

import threading
from pathlib import Path

import pytest
from test_replay import START, config
from test_replay_integration import Stops, web_case  # noqa: F401
from werkzeug.serving import make_server

from crypto_bot.replay.cache import CandleCache
from crypto_bot.replay.engine import ReplayEngine

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [1440, 390])
def test_replay_browser_forms_results_exports_compare_and_mobile(request, width):
    client, db, _ = request.getfixturevalue("web_case")
    run_id = db.create(config(variants=[{}, dict(exit_policy="fixed_tp")]))
    ReplayEngine(db, Stops()).run(run_id)
    trades, _ = db.data(run_id)
    cache = CandleCache(db.path.parent / "replay-cache")
    cache.put(
        "BTCUSDT",
        "1m",
        [
            (t, t + 59999, 110, 111, 99, 109, 10, 1100)
            for t in range(START - 2 * 3600000, START + 48 * 3600000, 60000)
        ],
    )
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
                page.on("pageerror", lambda e: errors.append(str(e)))
                base = f"http://127.0.0.1:{server.server_port}"
                page.goto(base + "/replay")
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                page.locator("#replay-preset").select_option("builtin:trail")
                assert (
                    page.locator("[data-key=exit_policy]").input_value()
                    == "trailing_pct"
                )
                assert page.locator("[data-key=activation_pct]").input_value() == "1.5"
                page.locator("#replay-mode").select_option("COMPARE RUN")
                page.locator("#replay-add-variant").click()
                assert page.locator(".replay-variant").count() == 2
                page.locator("#replay-preset-name").fill("Browser replay preset")
                page.locator("#replay-save-preset").click()
                playwright.expect(page.locator("#replay-form-status")).to_contain_text(
                    "saved"
                )
                page.screenshot(
                    path=str(artifacts / f"replay-form-{width}.png"), full_page=True
                )
                page.locator("button[type=submit]").click()
                page.wait_for_url("**/replay/runs/*")
                playwright.expect(page.locator("#replay-progress")).to_have_text(
                    "QUEUED"
                )
                page.goto(base + f"/replay/runs/{run_id}")
                playwright.expect(page.locator("#replay-progress")).to_have_text(
                    "COMPLETED"
                )
                playwright.expect(page.locator("#replay-sample")).to_contain_text(
                    "INSUFFICIENT SAMPLE"
                )
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                page.locator("#replay-chart-exposure").check()
                page.locator("#replay-chart-dd").check()
                with page.expect_download() as download:
                    page.locator('[data-replay-export="compact.json"]').click()
                assert download.value.suggested_filename.endswith(".compact.json")
                page.screenshot(
                    path=str(artifacts / f"replay-results-{width}.png"), full_page=True
                )
                page.locator("#replay-trades a").first.click()
                playwright.expect(page.locator("#replay-chart-status")).to_contain_text(
                    "cached 1m candles"
                )
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                assert trades
                page.screenshot(
                    path=str(artifacts / f"replay-trade-{width}.png"), full_page=True
                )
                page.goto(base + "/replay/compare")
                choices = page.locator("input[name=selection]")
                choices.nth(0).check()
                choices.nth(1).check()
                page.locator("button").click()
                playwright.expect(page.locator("#replay-comparison tr")).to_have_count(
                    2
                )
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                page.goto(base + "/replay/presets")
                playwright.expect(page.locator("body")).to_contain_text(
                    "Browser replay preset"
                )
                assert not errors
                page.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
