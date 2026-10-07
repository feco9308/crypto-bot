"""Optional actual-browser validation: pip install -e '.[test,browser-test]'."""

import math
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import NOW
from test_paper_analysis import analysis_case  # noqa: F401 -- imported pytest fixture
from test_paper_trade_audit import audit  # noqa: F401 -- imported pytest fixture
from werkzeug.serving import make_server

from crypto_bot.web.chart_data import INTERVALS

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def database(tmp_path):
    # Browser requests execute on server threads; use a shared isolated file DB.
    from crypto_bot.storage.database import Database

    db = Database(f"sqlite:///{tmp_path}/browser.db")
    db.initialize()
    yield db
    db.engine.dispose()


@pytest.fixture
def browser_audit(request):
    case = request.getfixturevalue("audit")
    client, repo, engine, pid, fake = case

    def candles(symbol, interval, start, end):
        step = INTERVALS[interval]
        begin = start // step * step
        finish = min(end, begin + step * 1000 - 1)
        rows = []
        for t in range(begin, finish + 1, step):
            value = 100 + math.sin(t / 3_600_000) * 1.5
            rows.append(
                dict(
                    time=t,
                    open=value - 0.15,
                    high=value + 0.6,
                    low=value - 0.6,
                    close=value + 0.15,
                )
            )
        return dict(candles=rows, next_start=finish + 1 if finish < end else None)

    fake.candles.side_effect = candles
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                yield browser, f"http://127.0.0.1:{server.server_port}", case
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def assert_no_page_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("width", [1440, 390])
def test_open_browser_navigation_chart_and_mobile(browser_audit, width):
    browser, base, case = browser_audit
    pid = case[3]
    page = browser.new_page(viewport=dict(width=width, height=960))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base + "/paper")
    assert_no_page_overflow(page)
    directory = Path(__file__).parents[1] / "artifacts"
    directory.mkdir(exist_ok=True)
    if width == 390:
        page.screenshot(
            path=str(directory / "paper-positions-mobile.png"), full_page=True
        )
    row = page.locator("[data-position-url]").first
    assert row.locator("a").first.is_visible()
    # A button event must not invoke row navigation (prevent the actual close POST).
    page.evaluate(
        "document.querySelector('[data-position-url] form').addEventListener('submit',e=>e.preventDefault())"
    )
    row.locator("button").click()
    assert page.url == base + "/paper"
    row.locator("td").nth(1).click()
    page.wait_for_url(f"**/paper/trades/{pid}")
    playwright.expect(page.locator("#price-chart-status")).to_contain_text(
        "public candles"
    )
    playwright.expect(page.locator("#quote-status")).to_contain_text(
        "FIXED_PUBLIC_TEST"
    )
    assert_no_page_overflow(page)
    page.locator("#trade-timeframe").select_option("15m")
    playwright.expect(page.locator("#price-chart-status")).to_contain_text("15m")
    page.locator("#chart-zoom-in").click()
    page.locator("#chart-reset").click()
    assert page.locator("#trade-price-chart").evaluate("(c)=>c.width>0 && c.height>0")
    assert not errors
    directory = Path(__file__).parents[1] / "artifacts"
    directory.mkdir(exist_ok=True)
    page.screenshot(
        path=str(directory / f"paper-audit-open-{width}.png"), full_page=True
    )
    page.locator("h1").scroll_into_view_if_needed()
    page.screenshot(path=str(directory / f"paper-audit-summary-{width}.png"))
    page.locator("#trade-charts").scroll_into_view_if_needed()
    page.screenshot(path=str(directory / f"paper-audit-charts-{width}.png"))
    page.close()


def test_closed_browser_history_and_exit_links(browser_audit, database):
    browser, base, case = browser_audit
    _, repo, engine, pid, fake = case
    database.test_quotes = {"BTCUSDT": 101}
    database.test_quote_time = NOW + timedelta(minutes=5)
    engine.manual_close(pid, NOW + timedelta(minutes=5))
    page = browser.new_page(viewport=dict(width=390, height=844))
    page.goto(base + "/paper")
    page.locator("[data-position-url] a").first.click()
    page.wait_for_url(f"**/paper/trades/{pid}")
    playwright.expect(page.locator("#price-chart-status")).to_contain_text(
        "public candles"
    )
    assert "WHY DID WE SELL? · MANUAL" in page.content()
    assert_no_page_overflow(page)
    fake.quote.assert_not_called()
    page.screenshot(
        path=str(Path(__file__).parents[1] / "artifacts/paper-audit-closed-mobile.png"),
        full_page=True,
    )
    page.get_by_role(
        "link", name="Signal #2 · complete signal / risk / order audit"
    ).click()
    page.wait_for_url("**/paper/signals/2")
    assert page.get_by_role(
        "link", name="Full position / entry / exit audit"
    ).is_visible()
    page.close()


def test_browser_missing_data_and_public_get_only(browser_audit):
    browser, base, case = browser_audit
    fake = case[4]
    from crypto_bot.web.chart_data import ChartUnavailable

    fake.candles.side_effect = ChartUnavailable("Public Binance data unavailable.")
    fake.quote.side_effect = ChartUnavailable("Public bid unavailable.")
    page = browser.new_page(viewport=dict(width=390, height=844))
    requests = []
    errors = []
    page.on("request", lambda request: requests.append((request.method, request.url)))
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base + f"/paper/trades/{case[3]}")
    playwright.expect(page.locator("#price-chart-status")).to_contain_text(
        "unavailable"
    )
    playwright.expect(page.locator("#quote-status")).to_contain_text("unavailable")
    assert page.locator("[data-quote-field=bid]").text_content() == "— / unavailable"
    assert page.locator("h1").is_visible()
    assert_no_page_overflow(page)
    assert all(
        method == "GET" and url.startswith(base + "/") for method, url in requests
    )
    assert not errors
    page.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_analysis_browser_export_and_mobile(browser_audit, request, database, width):
    import json

    request.getfixturevalue("analysis_case")
    browser, base, case = browser_audit
    if width == 390:
        database.test_quotes = {"BTCUSDT": 101}
        database.test_quote_time = NOW + timedelta(minutes=30)
        case[2].manual_close(case[3], NOW + timedelta(minutes=30))
    page = browser.new_page(
        viewport=dict(width=width, height=960), accept_downloads=True
    )
    errors = []
    requests = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append((request.method, request.url)))
    page.goto(base + "/paper/analysis")
    playwright.expect(page.locator("#sample-message")).to_contain_text(
        "Insufficient sample size"
    )
    playwright.expect(page.locator("#analysis-progress")).to_contain_text("READY 1")
    assert_no_page_overflow(page)
    assert page.locator("#analysis-trades a").first.is_visible()
    with page.expect_download() as download:
        page.locator("#export-json").click()
    payload = json.loads(Path(download.value.path()).read_text())
    assert payload["metadata"]["trade_count"] == 1 and len(payload["trades"]) == 1
    assert payload["trades"][0]["analysis_data_status"] == "READY"
    with page.expect_download() as download:
        page.locator("#export-csv").click()
    assert Path(download.value.path()).read_bytes().startswith(b"\xef\xbb\xbf")
    assert all(
        method == "GET" and url.startswith(base + "/") for method, url in requests
    )
    assert not errors
    directory = Path(__file__).parents[1] / "artifacts"
    directory.mkdir(exist_ok=True)
    page.screenshot(path=str(directory / f"paper-analysis-{width}.png"), full_page=True)
    page.locator("h1").scroll_into_view_if_needed()
    page.screenshot(path=str(directory / f"paper-analysis-summary-{width}.png"))
    page.locator("#analysis-trades a").first.click()
    page.wait_for_url(f"**/paper/trades/{case[3]}")
    page.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_exit_simulator_browser_summary_detail_and_marker(
    browser_audit, request, database, width
):
    from test_paper_analysis import Immediate

    _, analysis, _ = request.getfixturevalue("analysis_case")
    browser, base, case = browser_audit
    client, _, engine, pid, _ = case
    simulator = client.application.extensions["paper_exit_simulator"]
    simulator.executor.shutdown(wait=False, cancel_futures=True)
    simulator.executor = Immediate()
    database.test_quotes = {"BTCUSDT": 97}
    database.test_quote_time = NOW + timedelta(minutes=30)
    engine.manual_close(pid, NOW + timedelta(minutes=30))
    for _ in range(3):
        client.get(f"/api/paper/analysis/exit-simulator?position_id={pid}")
    client.get("/api/paper/analysis/exit-simulator/summary")
    client.get(f"/api/paper/analysis/exit-simulator/summary?position_id={pid}")
    page = browser.new_page(viewport=dict(width=width, height=960))
    errors, requests = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append((request.method, request.url)))
    page.goto(base + "/paper/analysis")
    playwright.expect(page.locator("#simulator-rows tr")).to_have_count(49)
    playwright.expect(page.locator("#simulator-sample")).to_contain_text(
        "SAMPLE SIZE: 1"
    )
    assert page.locator("#simulator-rows tr.simulator-baseline").count() == 1
    assert_no_page_overflow(page)
    directory = Path(__file__).parents[1] / "artifacts"
    directory.mkdir(exist_ok=True)
    page.locator("#exit-simulator").scroll_into_view_if_needed()
    page.screenshot(path=str(directory / f"exit-simulator-summary-{width}.png"))
    page.goto(base + f"/paper/trades/{pid}")
    playwright.expect(page.locator("#simulator-status")).to_contain_text("trade READY")
    playwright.expect(page.locator("#price-chart-status")).to_contain_text(
        "public candles"
    )
    page.locator("#simulator-scenario").select_option("TP_2PCT")
    playwright.expect(page.locator("#simulator-selected")).to_contain_text(
        "Conservative"
    )
    assert page.locator("#simulator-selected").text_content().find("ambiguous") >= 0
    page.locator("#simulator-bound").select_option("optimistic_result")
    playwright.expect(page.locator("#simulator-selected")).to_contain_text("Optimistic")
    received = []
    page.expose_function("captureMarker", lambda data: received.append(data))
    page.evaluate(
        "document.addEventListener('paper-simulated-exit',e=>window.captureMarker(e.detail))"
    )
    page.locator("#simulator-marker").check()
    page.wait_for_timeout(50)
    assert received and received[-1]["label"] == "WHAT-IF EXIT"
    assert_no_page_overflow(page)
    page.locator("#exit-simulator").scroll_into_view_if_needed()
    page.screenshot(path=str(directory / f"exit-simulator-detail-{width}.png"))
    page.locator("#trade-charts").scroll_into_view_if_needed()
    page.screenshot(path=str(directory / f"exit-simulator-chart-{width}.png"))
    assert all(
        method == "GET" and url.startswith(base + "/") for method, url in requests
    )
    assert not errors
    page.close()
