import logging
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone

from bookmarks.models import Bookmark
from bookmarks.services import http_client
from bookmarks.services.website_loader import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)

# Number of consecutive failed checks after which a bookmark is marked as broken
MAX_FAILURES = 3

ERROR_TIMEOUT = "timeout"
ERROR_CONNECTION = "connection error"


@dataclass
class LinkCheckResult:
    status_code: int | None
    error: str
    ok: bool


def _request(method, url: str, timeout: float, **kwargs) -> LinkCheckResult:
    try:
        response = method(
            url,
            allow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": DEFAULT_USER_AGENT},
            **kwargs,
        )
    except http_client.Timeout:
        return LinkCheckResult(status_code=None, error=ERROR_TIMEOUT, ok=False)
    except http_client.RequestException:
        return LinkCheckResult(status_code=None, error=ERROR_CONNECTION, ok=False)

    try:
        status_code = response.status_code
    finally:
        response.close()
    return LinkCheckResult(status_code=status_code, error="", ok=status_code < 400)


def check_url(url: str, timeout: float = None) -> LinkCheckResult:
    if timeout is None:
        timeout = settings.LD_LINK_CHECK_TIMEOUT

    result = _request(http_client.head, url, timeout)
    if result.ok:
        return result

    # Some servers do not support HEAD requests, or respond differently to them,
    # so retry once with a GET request. Streaming avoids downloading the body.
    return _request(http_client.get, url, timeout, stream=True)


class HostRateLimiter:
    """
    Ensures a minimum interval between requests to the same host.
    """

    def __init__(self, min_interval: float, clock=time.monotonic, sleep=time.sleep):
        self.min_interval = min_interval
        self.clock = clock
        self.sleep = sleep
        self._last_request = {}

    def wait(self, url: str):
        host = (urlparse(url).hostname or "").lower()
        last_request = self._last_request.get(host)
        if last_request is not None and self.min_interval > 0:
            remaining = self.min_interval - (self.clock() - last_request)
            if remaining > 0:
                self.sleep(remaining)
        self._last_request[host] = self.clock()


def record_result(bookmark: Bookmark, result: LinkCheckResult) -> None:
    bookmark.link_status_code = result.status_code
    bookmark.link_check_error = result.error[:255]
    bookmark.link_last_checked = timezone.now()
    if result.ok:
        bookmark.link_failure_count = 0
        bookmark.link_broken = False
    else:
        bookmark.link_failure_count += 1
        bookmark.link_broken = bookmark.link_failure_count >= MAX_FAILURES
    bookmark.save(
        update_fields=[
            "link_status_code",
            "link_check_error",
            "link_last_checked",
            "link_failure_count",
            "link_broken",
        ]
    )


def check_bookmarks(bookmark_ids: list[int], rate_limiter=None) -> None:
    if rate_limiter is None:
        rate_limiter = HostRateLimiter(settings.LD_LINK_CHECK_HOST_DELAY)

    bookmarks = Bookmark.objects.filter(id__in=bookmark_ids).order_by("id")
    for bookmark in bookmarks:
        rate_limiter.wait(bookmark.url)
        result = check_url(bookmark.url)
        record_result(bookmark, result)
        logger.info(
            f"Checked link health. url={bookmark.url} status={result.status_code} error={result.error}"
        )
