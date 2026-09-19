"""Opt-in browser regression checks; all page requests are served from fixtures."""

import os
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

from monitor import BrowserFetcher, EMPTY_MESSAGE, RESULTS_READY_SCRIPT, URL


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1",
                     "Set RUN_BROWSER_TESTS=1 to run the offline browser checks.")
class BrowserReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(
                channel=os.environ.get("BROWSER_CHANNEL", "chromium"), headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        # No network access: each test supplies its entire document.
        self.document = ""
        self.page.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html", body=self.document)
            if route.request.url == URL else route.abort())

    def fetch_fixture(self, name):
        self.document = (Path(__file__).parent / "fixtures" / name).read_text()
        fetcher = BrowserFetcher(self.playwright)
        fetcher.browser = self.browser
        fetcher.page = self.page
        return fetcher.fetch()

    def test_hidden_empty_paragraph_completes_full_browser_fetch(self):
        self.assertEqual(self.fetch_fixture("canvas_empty_results.html"), [])
        self.assertNotIn(EMPTY_MESSAGE, self.page.locator("body").inner_text().lower())

    def test_available_fixture_still_completes_full_browser_fetch(self):
        self.assertEqual(len(self.fetch_fixture("studio_available.html")), 1)

    def test_script_literal_is_not_mistaken_for_empty_results(self):
        self.document = ("<title>Canvas Utrecht</title><h1>Floor Plans</h1>"
                         f'<script>const unusedMessage = "{EMPTY_MESSAGE}";</script>')
        self.page.goto(URL)
        self.assertFalse(self.page.evaluate(RESULTS_READY_SCRIPT))

    def test_unrelated_hidden_paragraph_does_not_mark_page_ready(self):
        self.document = ("<title>Canvas Utrecht</title><h1>Floor Plans</h1>"
                         f'<div hidden><p>{EMPTY_MESSAGE}</p></div>')
        self.page.goto(URL)
        self.assertFalse(self.page.evaluate(RESULTS_READY_SCRIPT))


if __name__ == "__main__":
    unittest.main()
