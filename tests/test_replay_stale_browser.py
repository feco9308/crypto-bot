"""Prominent stale warnings in result and comparison pages at both widths."""

import threading
from pathlib import Path

import pytest
from test_replay import config
from test_replay_integration import web_case  # noqa: F401
from werkzeug.serving import make_server

from crypto_bot.replay.stale import WARNING

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [1440, 390])
def test_stale_result_and_comparison_warn_and_exclude_ranking(request, width):
    client, db, _ = request.getfixturevalue("web_case")
    ids = []
    for affected in (True, False):
        run = db.create(config())
        db.complete(
            run,
            [
                dict(
                    metrics=dict(
                        stale_data_affected=affected,
                        stale_warning=WARNING if affected else None,
                        stale_position_count=int(affected),
                        return_pct=99 if affected else 1,
                        blocked_entries={},
                    ),
                    breakdowns={},
                )
            ],
            {},
        )
        ids.append(run)
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
                page.goto(base + f"/replay/runs/{ids[0]}")
                playwright.expect(page.locator("#replay-stale-warning")).to_have_text(
                    WARNING
                )
                assert page.locator("#replay-stale-warning").is_visible()
                page.goto(base + "/replay/compare")
                for run in ids:
                    page.locator(f'input[value="{run}:0"]').check()
                page.locator("button").click()
                playwright.expect(
                    page.locator("#replay-compare-stale-warning")
                ).to_have_text(WARNING)
                playwright.expect(page.locator("#replay-comparison")).to_contain_text(
                    "STALE DATA — excluded from ranking"
                )
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                artifacts = Path(__file__).parents[1] / "artifacts"
                artifacts.mkdir(exist_ok=True)
                page.screenshot(
                    path=str(artifacts / f"replay-stale-compare-{width}.png"),
                    full_page=True,
                )
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
