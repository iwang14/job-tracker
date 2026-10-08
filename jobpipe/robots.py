"""robots.txt and terms-of-service gates for undocumented company endpoints.

Public ATS APIs (Greenhouse, Lever, Ashby, SmartRecruiters, Workday CXS) are published for exactly
this use. Company-specific endpoints (Amazon, Microsoft, Atlassian, sitemaps) are not, so their
fetchers call `require_allowed()` first. It enforces two things:

1. You opted in after reading the site's terms: `tos_ok: true` on the company in companies.yaml.
2. The site's robots.txt allows our User-Agent to fetch that exact URL. This is checked live on
   every run, so a site that later disallows bots is respected automatically. If robots.txt can't
   be read (other than a 404), access is denied.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from .fetchers.base import FetcherDisabled, FetcherError
from .http import Http, HttpError
from .models import Company

_cache: dict[str, tuple[float, RobotFileParser | None]] = {}
_lock = threading.Lock()
TTL = 6 * 3600


class NotAllowed(FetcherError):
    """robots.txt disallows (or can't be read). Counts as a fetch failure, so you get alerted."""


class NoTosOptIn(FetcherDisabled):
    """`tos_ok` not set: the fetcher is skipped silently, like a disabled company."""


def _parser(http: Http, origin: str) -> RobotFileParser | None:
    """None means 'no robots.txt' (everything allowed)."""
    with _lock:
        hit = _cache.get(origin)
        if hit and time.monotonic() - hit[0] < TTL:
            return hit[1]
    try:
        text = http.get(origin + "/robots.txt", expect_json=False).text
        rp = RobotFileParser()
        rp.parse(text.splitlines())
    except HttpError as e:
        if e.status in (404, 410):
            rp = None
        else:
            raise NotAllowed(f"could not read {origin}/robots.txt (HTTP {e.status}); not fetching") from e
    with _lock:
        _cache[origin] = (time.monotonic(), rp)
    return rp


def robots_allows(http: Http, url: str) -> bool:
    u = urlparse(url)
    rp = _parser(http, f"{u.scheme}://{u.netloc}")
    if rp is None:
        return True
    agent = http.user_agent.split("/")[0]  # "jobpipe"
    return rp.can_fetch(agent, url)


def sitemaps(http: Http, origin: str) -> list[str]:
    rp = _parser(http, origin)
    return list(rp.site_maps() or []) if rp else []


def require_allowed(company: Company, http: Http, *urls: str) -> None:
    if not company.options.get("tos_ok"):
        raise NoTosOptIn(
            f"{company.name}: set `tos_ok: true` in companies.yaml after reviewing the site's terms of use")
    for url in urls:
        if not robots_allows(http, url):
            raise NotAllowed(f"{company.name}: robots.txt disallows {url}")


def clear_cache() -> None:
    with _lock:
        _cache.clear()
