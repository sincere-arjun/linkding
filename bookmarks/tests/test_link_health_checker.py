from unittest import mock

import requests
from django.test import TestCase, override_settings

from bookmarks.services import http_client, link_health
from bookmarks.services.link_health import (
    HostRateLimiter,
    LinkCheckResult,
    check_bookmarks,
    check_url,
    record_result,
)
from bookmarks.tests.helpers import BookmarkFactoryMixin


def mock_response(status_code: int):
    response = mock.Mock()
    response.status_code = status_code
    return response


class CheckUrlTestCase(TestCase):
    def setUp(self):
        head_patcher = mock.patch.object(link_health.http_client, "head")
        get_patcher = mock.patch.object(link_health.http_client, "get")
        self.mock_head = head_patcher.start()
        self.mock_get = get_patcher.start()
        self.addCleanup(head_patcher.stop)
        self.addCleanup(get_patcher.stop)

    def test_head_ok(self):
        head_response = mock_response(200)
        self.mock_head.return_value = head_response

        result = check_url("https://example.com", timeout=5)

        self.assertEqual(result, LinkCheckResult(status_code=200, error="", ok=True))
        self.mock_head.assert_called_once()
        args, kwargs = self.mock_head.call_args
        self.assertEqual(args[0], "https://example.com")
        self.assertTrue(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], 5)
        self.mock_get.assert_not_called()
        head_response.close.assert_called_once()

    def test_head_status_below_400_is_ok(self):
        self.mock_head.return_value = mock_response(399)

        result = check_url("https://example.com", timeout=5)

        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 399)
        self.mock_get.assert_not_called()

    @override_settings(LD_LINK_CHECK_TIMEOUT=7)
    def test_uses_timeout_setting_by_default(self):
        self.mock_head.return_value = mock_response(200)

        check_url("https://example.com")

        self.assertEqual(self.mock_head.call_args.kwargs["timeout"], 7)

    def test_head_error_status_falls_back_to_get(self):
        self.mock_head.return_value = mock_response(405)
        self.mock_get.return_value = mock_response(200)

        result = check_url("https://example.com", timeout=5)

        self.assertEqual(result, LinkCheckResult(status_code=200, error="", ok=True))
        self.mock_get.assert_called_once()
        args, kwargs = self.mock_get.call_args
        self.assertEqual(args[0], "https://example.com")
        self.assertTrue(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["timeout"], 5)

    def test_head_and_get_error_status(self):
        self.mock_head.return_value = mock_response(404)
        self.mock_get.return_value = mock_response(404)

        result = check_url("https://example.com", timeout=5)

        self.assertEqual(result, LinkCheckResult(status_code=404, error="", ok=False))

    def test_head_exception_falls_back_to_get(self):
        self.mock_head.side_effect = http_client.ConnectionError()
        self.mock_get.return_value = mock_response(200)

        result = check_url("https://example.com", timeout=5)

        self.assertEqual(result, LinkCheckResult(status_code=200, error="", ok=True))
        self.mock_get.assert_called_once()

    def test_head_and_get_responses_are_closed(self):
        head_response = mock_response(500)
        self.mock_head.return_value = head_response
        get_response = mock_response(200)
        self.mock_get.return_value = get_response

        check_url("https://example.com", timeout=5)

        head_response.close.assert_called_once()
        get_response.close.assert_called_once()

    def test_get_error_response_is_closed(self):
        head_response = mock_response(404)
        self.mock_head.return_value = head_response
        get_response = mock_response(404)
        self.mock_get.return_value = get_response

        check_url("https://example.com", timeout=5)

        head_response.close.assert_called_once()
        get_response.close.assert_called_once()

    def test_head_error_status_then_get_timeout(self):
        self.mock_head.return_value = mock_response(404)
        self.mock_get.side_effect = http_client.Timeout()

        result = check_url("https://example.com", timeout=5)

        self.assertEqual(
            result, LinkCheckResult(status_code=None, error="timeout", ok=False)
        )

    def test_head_timeout_then_get_error_status(self):
        self.mock_head.side_effect = http_client.Timeout()
        self.mock_get.return_value = mock_response(404)

        result = check_url("https://example.com", timeout=5)

        self.assertEqual(result, LinkCheckResult(status_code=404, error="", ok=False))

    def test_timeout(self):
        self.mock_head.side_effect = http_client.Timeout()
        self.mock_get.side_effect = http_client.Timeout()

        result = check_url("https://example.com", timeout=5)

        self.assertEqual(
            result, LinkCheckResult(status_code=None, error="timeout", ok=False)
        )

    def test_connect_timeout_is_reported_as_timeout(self):
        # ConnectTimeout is both a ConnectionError and a Timeout
        self.mock_head.side_effect = requests.ConnectTimeout()
        self.mock_get.side_effect = requests.ConnectTimeout()

        result = check_url("https://example.com", timeout=5)

        self.assertEqual(
            result, LinkCheckResult(status_code=None, error="timeout", ok=False)
        )

    def test_connection_error(self):
        self.mock_head.side_effect = http_client.ConnectionError()
        self.mock_get.side_effect = http_client.ConnectionError()

        result = check_url("https://example.com", timeout=5)

        self.assertEqual(
            result,
            LinkCheckResult(status_code=None, error="connection error", ok=False),
        )

    def test_other_request_exception(self):
        self.mock_head.side_effect = http_client.RequestException()
        self.mock_get.side_effect = http_client.RequestException()

        result = check_url("https://example.com", timeout=5)

        self.assertEqual(
            result,
            LinkCheckResult(status_code=None, error="connection error", ok=False),
        )


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


class HostRateLimiterTestCase(TestCase):
    def setUp(self):
        self.fake = FakeClock()

    def create_limiter(self, min_interval=1.0):
        return HostRateLimiter(
            min_interval, clock=self.fake.clock, sleep=self.fake.sleep
        )

    def test_first_request_does_not_wait(self):
        limiter = self.create_limiter()

        limiter.wait("https://example.com/a")

        self.assertEqual(self.fake.sleeps, [])

    def test_same_host_waits(self):
        limiter = self.create_limiter(1.0)

        limiter.wait("https://example.com/a")
        self.fake.advance(0.25)
        limiter.wait("https://example.com/b")

        self.assertEqual(self.fake.sleeps, [0.75])

    def test_same_host_is_case_insensitive(self):
        limiter = self.create_limiter(1.0)

        limiter.wait("https://example.com/a")
        limiter.wait("https://EXAMPLE.com/b")

        self.assertEqual(self.fake.sleeps, [1.0])

    def test_different_hosts_do_not_wait(self):
        limiter = self.create_limiter(1.0)

        limiter.wait("https://example.com/a")
        limiter.wait("https://example.org/a")
        limiter.wait("https://sub.example.com/a")

        self.assertEqual(self.fake.sleeps, [])

    def test_interval_elapsed_does_not_wait(self):
        limiter = self.create_limiter(1.0)

        limiter.wait("https://example.com/a")
        self.fake.advance(1.0)
        limiter.wait("https://example.com/b")
        self.fake.advance(5)
        limiter.wait("https://example.com/c")

        self.assertEqual(self.fake.sleeps, [])

    def test_wait_is_measured_from_last_request(self):
        limiter = self.create_limiter(1.0)

        limiter.wait("https://example.com/a")
        limiter.wait("https://example.com/b")
        self.fake.advance(0.5)
        limiter.wait("https://example.com/c")

        self.assertEqual(self.fake.sleeps, [1.0, 0.5])

    def test_zero_interval_never_waits(self):
        limiter = self.create_limiter(0)

        limiter.wait("https://example.com/a")
        limiter.wait("https://example.com/b")

        self.assertEqual(self.fake.sleeps, [])


OK = LinkCheckResult(status_code=200, error="", ok=True)
NOT_FOUND = LinkCheckResult(status_code=404, error="", ok=False)
TIMEOUT = LinkCheckResult(status_code=None, error="timeout", ok=False)


class RecordResultTestCase(TestCase, BookmarkFactoryMixin):
    def test_success(self):
        bookmark = self.setup_bookmark()

        record_result(bookmark, OK)

        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_status_code, 200)
        self.assertEqual(bookmark.link_check_error, "")
        self.assertIsNotNone(bookmark.link_last_checked)
        self.assertEqual(bookmark.link_failure_count, 0)
        self.assertFalse(bookmark.link_broken)

    def test_failure_records_status_and_error(self):
        bookmark = self.setup_bookmark()

        record_result(bookmark, NOT_FOUND)
        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_status_code, 404)
        self.assertEqual(bookmark.link_check_error, "")
        self.assertIsNotNone(bookmark.link_last_checked)

        record_result(bookmark, TIMEOUT)
        bookmark.refresh_from_db()
        self.assertIsNone(bookmark.link_status_code)
        self.assertEqual(bookmark.link_check_error, "timeout")

    def test_broken_after_three_consecutive_failures(self):
        bookmark = self.setup_bookmark()

        record_result(bookmark, NOT_FOUND)
        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_failure_count, 1)
        self.assertFalse(bookmark.link_broken)

        record_result(bookmark, TIMEOUT)
        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_failure_count, 2)
        self.assertFalse(bookmark.link_broken)

        record_result(bookmark, NOT_FOUND)
        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_failure_count, 3)
        self.assertTrue(bookmark.link_broken)

        record_result(bookmark, NOT_FOUND)
        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_failure_count, 4)
        self.assertTrue(bookmark.link_broken)

    def test_success_in_between_resets_failure_count(self):
        bookmark = self.setup_bookmark()

        record_result(bookmark, NOT_FOUND)
        record_result(bookmark, NOT_FOUND)
        record_result(bookmark, OK)
        record_result(bookmark, NOT_FOUND)
        record_result(bookmark, NOT_FOUND)

        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_failure_count, 2)
        self.assertFalse(bookmark.link_broken)

    def test_success_resets_broken_bookmark(self):
        bookmark = self.setup_bookmark()
        for _ in range(3):
            record_result(bookmark, TIMEOUT)
        bookmark.refresh_from_db()
        self.assertTrue(bookmark.link_broken)

        record_result(bookmark, OK)

        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_status_code, 200)
        self.assertEqual(bookmark.link_check_error, "")
        self.assertEqual(bookmark.link_failure_count, 0)
        self.assertFalse(bookmark.link_broken)

    def test_only_updates_link_health_fields(self):
        bookmark = self.setup_bookmark(title="Original title")
        bookmark.title = "Unsaved title"

        record_result(bookmark, NOT_FOUND)

        bookmark.refresh_from_db()
        self.assertEqual(bookmark.title, "Original title")
        self.assertEqual(bookmark.link_failure_count, 1)


class CheckBookmarksTestCase(TestCase, BookmarkFactoryMixin):
    def test_checks_and_records_each_bookmark(self):
        bookmark1 = self.setup_bookmark(url="https://example.com/1")
        bookmark2 = self.setup_bookmark(url="https://example.org/2")
        results = {
            "https://example.com/1": OK,
            "https://example.org/2": NOT_FOUND,
        }
        limiter = mock.Mock()

        with mock.patch.object(
            link_health, "check_url", side_effect=lambda url: results[url]
        ) as mock_check_url:
            check_bookmarks([bookmark1.id, bookmark2.id], rate_limiter=limiter)

        self.assertEqual(mock_check_url.call_count, 2)
        bookmark1.refresh_from_db()
        bookmark2.refresh_from_db()
        self.assertEqual(bookmark1.link_status_code, 200)
        self.assertEqual(bookmark1.link_failure_count, 0)
        self.assertEqual(bookmark2.link_status_code, 404)
        self.assertEqual(bookmark2.link_failure_count, 1)

    def test_uses_given_rate_limiter_for_all_bookmarks(self):
        bookmark1 = self.setup_bookmark(url="https://example.com/1")
        bookmark2 = self.setup_bookmark(url="https://example.com/2")
        limiter = mock.Mock()

        with mock.patch.object(link_health, "check_url", return_value=OK):
            check_bookmarks([bookmark1.id, bookmark2.id], rate_limiter=limiter)

        self.assertEqual(
            limiter.wait.call_args_list,
            [mock.call("https://example.com/1"), mock.call("https://example.com/2")],
        )

    @override_settings(LD_LINK_CHECK_HOST_DELAY=2.5)
    def test_creates_one_shared_rate_limiter_by_default(self):
        bookmark1 = self.setup_bookmark(url="https://example.com/1")
        bookmark2 = self.setup_bookmark(url="https://example.com/2")

        with (
            mock.patch.object(link_health, "check_url", return_value=OK),
            mock.patch.object(link_health, "HostRateLimiter") as mock_limiter_class,
        ):
            check_bookmarks([bookmark1.id, bookmark2.id])

        mock_limiter_class.assert_called_once_with(2.5)
        self.assertEqual(mock_limiter_class.return_value.wait.call_count, 2)

    def test_rate_limits_same_host_between_bookmarks(self):
        bookmark1 = self.setup_bookmark(url="https://example.com/1")
        bookmark2 = self.setup_bookmark(url="https://example.com/2")
        bookmark3 = self.setup_bookmark(url="https://example.org/3")
        fake = FakeClock()
        limiter = HostRateLimiter(1.0, clock=fake.clock, sleep=fake.sleep)

        with mock.patch.object(link_health, "check_url", return_value=OK):
            check_bookmarks(
                [bookmark1.id, bookmark2.id, bookmark3.id], rate_limiter=limiter
            )

        self.assertEqual(fake.sleeps, [1.0])

    def test_ignores_unknown_ids(self):
        bookmark = self.setup_bookmark(url="https://example.com/1")

        with mock.patch.object(
            link_health, "check_url", return_value=OK
        ) as mock_check_url:
            check_bookmarks([bookmark.id, 99999], rate_limiter=mock.Mock())

        mock_check_url.assert_called_once_with("https://example.com/1")

    def test_records_result_of_mocked_head_request(self):
        bookmark = self.setup_bookmark(url="https://example.com/1")

        with (
            mock.patch.object(
                link_health.http_client, "head", return_value=mock_response(200)
            ) as mock_head,
            mock.patch.object(link_health.http_client, "get") as mock_get,
        ):
            check_bookmarks([bookmark.id], rate_limiter=mock.Mock())

        mock_head.assert_called_once()
        mock_get.assert_not_called()
        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_status_code, 200)
