import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from django.test import override_settings
from django.urls import reverse
from playwright.sync_api import expect

from bookmarks.services import http_client
from bookmarks.services.link_health import check_bookmarks
from bookmarks.tests_e2e.helpers import LinkdingE2ETestCase

# Generous enough that /ok and /missing answer in time on a loaded machine
CHECK_TIMEOUT = 1
# Well above the check timeout, so that requests to /slow always time out
SLOW_RESPONSE_DELAY = 3


class LinkHealthRequestHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self._respond()

    def do_GET(self):
        self._respond()

    def _respond(self):
        if self.path == "/ok":
            status = 200
        elif self.path == "/slow":
            time.sleep(SLOW_RESPONSE_DELAY)
            status = 200
        else:
            status = 404
        try:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except OSError:
            # Client already gave up on the slow response
            pass

    def log_message(self, format, *args):
        pass


class LocalLinkServer:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LinkHealthRequestHandler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}{path}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@override_settings(
    LD_ALLOWED_INTERNAL_HOSTS="127.0.0.1",
    LD_LINK_CHECK_TIMEOUT=CHECK_TIMEOUT,
    LD_LINK_CHECK_HOST_DELAY=0,
)
class LinkHealthFilterE2ETestCase(LinkdingE2ETestCase):
    def setUp(self) -> None:
        super().setUp()
        # The allowlist is cached, pick up the overridden setting
        http_client.get_allowlist.cache_clear()
        self.addCleanup(http_client.get_allowlist.cache_clear)

        # Make sure requests to the local server never go through a proxy.
        # patch.dict restores the whole environment, including removed keys.
        env_patcher = mock.patch.dict(
            os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            os.environ.pop(name, None)
            os.environ.pop(name.lower(), None)

        self.link_server = LocalLinkServer()
        self.addCleanup(self.link_server.stop)

    def test_broken_filter_shows_only_broken_bookmarks(self):
        ok_bookmark = self.setup_bookmark(
            url=self.link_server.url("/ok"), title="Healthy link"
        )
        missing_bookmark = self.setup_bookmark(
            url=self.link_server.url("/missing"), title="Missing link"
        )
        slow_bookmark = self.setup_bookmark(
            url=self.link_server.url("/slow"), title="Slow link"
        )
        bookmark_ids = [ok_bookmark.id, missing_bookmark.id, slow_bookmark.id]

        # A link is only considered broken after three consecutive failed checks
        for _ in range(3):
            check_bookmarks(bookmark_ids)

        ok_bookmark.refresh_from_db()
        missing_bookmark.refresh_from_db()
        slow_bookmark.refresh_from_db()
        self.assertFalse(ok_bookmark.link_broken)
        self.assertEqual(ok_bookmark.link_status_code, 200)
        self.assertTrue(missing_bookmark.link_broken)
        self.assertEqual(missing_bookmark.link_status_code, 404)
        self.assertTrue(slow_bookmark.link_broken)
        self.assertEqual(slow_bookmark.link_check_error, "timeout")

        page = self.open(reverse("linkding:bookmarks.index"))

        # all bookmarks are listed without the filter, only broken ones have a badge
        expect(self.locate_bookmark("Healthy link")).to_be_visible()
        expect(
            self.locate_bookmark("Healthy link").locator(".link-broken-badge")
        ).to_have_count(0)
        expect(self.locate_bookmark("Missing link")).to_be_visible()
        expect(self.locate_bookmark("Slow link")).to_be_visible()

        # enable broken filter
        page.get_by_role("button", name="Search preferences").click()
        page.locator("label").filter(
            has=page.locator('input[name="broken"][value="yes"]')
        ).click()
        page.get_by_role("button", name="Apply").click()

        expect(page.locator('input[name="broken"][value="yes"]')).to_be_checked()
        expect(self.locate_bookmark("Healthy link")).to_have_count(0)
        expect(self.locate_bookmark("Missing link")).to_be_visible()
        expect(self.locate_bookmark("Slow link")).to_be_visible()
        expect(page.locator("ul.bookmark-list > li")).to_have_count(2)

        missing_badge = self.locate_bookmark("Missing link").locator(
            ".link-broken-badge"
        )
        expect(missing_badge).to_have_text("Broken")
        expect(missing_badge).to_have_attribute("title", re.compile("404"))
        slow_badge = self.locate_bookmark("Slow link").locator(".link-broken-badge")
        expect(slow_badge).to_have_text("Broken")
        expect(slow_badge).to_have_attribute("title", re.compile("timeout"))
