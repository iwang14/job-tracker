"""Polite HTTP client: User-Agent, per-host rate limit, retries with backoff, ETag/Last-Modified.

Fetchers only talk to `Http`, so tests can swap in `FakeHttp` (tests/conftest.py) that serves
recorded fixtures.
"""
from __future__ import annotations

import copy
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

DEFAULT_UA = "jobpipe/0.1 (personal job-search bot; low volume; +{contact})"


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url


@dataclass
class Response:
    status: int
    data: Any = None          # parsed JSON (or None)
    text: str = ""
    etag: str | None = None
    last_modified: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class HostLimiter:
    """At most one request per `min_interval` seconds per host, across threads."""

    def __init__(self, min_interval: float = 0.5):
        self.min_interval = min_interval
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next.get(host, 0.0))
            self._next[host] = start + self.min_interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)


class Http:
    def __init__(self, cache=None, min_interval: float = 0.5, timeout: float = 20.0,
                 retries: int = 3, user_agent: str | None = None):
        contact = os.environ.get("JOBPIPE_CONTACT", "github.com")
        self.user_agent = user_agent or os.environ.get("JOBPIPE_USER_AGENT") or DEFAULT_UA.format(contact=contact)
        self.cache = cache            # object with get_validators/set_validators (db.DB), optional
        self.limiter = HostLimiter(min_interval)
        self.timeout = timeout
        self.retries = retries
        self._local = threading.local()
        self._cache_lock = threading.Lock()
        self.pending_validators: list[tuple[str, str | None, str | None]] = []

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = self.user_agent
            self._local.session = s
        return s

    def request(self, method: str, url: str, *, params: dict | None = None, json_body: Any = None,
                headers: dict | None = None, conditional: bool = False, expect_json: bool = True) -> Response:
        hdrs = {"Accept": "application/json" if expect_json else "text/html,*/*"}
        hdrs.update(headers or {})
        cache_key = url + ("?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items())) if params else "")
        if conditional and self.cache is not None:
            with self._cache_lock:
                etag, lm = self.cache.get_validators(cache_key)
            if etag:
                hdrs["If-None-Match"] = etag
            if lm:
                hdrs["If-Modified-Since"] = lm

        host = urlparse(url).netloc
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            self.limiter.wait(host)
            try:
                r = self._session().request(method, url, params=params, json=json_body, headers=hdrs,
                                            timeout=self.timeout)
            except requests.RequestException as e:
                last_exc = e
                self._backoff(attempt)
                continue
            if r.status_code == 304:
                return Response(304, etag=r.headers.get("ETag"))
            if r.status_code == 429 or r.status_code >= 500:
                last_exc = HttpError(r.status_code, url, r.text)
                self._backoff(attempt, r.headers.get("Retry-After"))
                continue
            if r.status_code >= 400:
                raise HttpError(r.status_code, url, r.text)
            resp = Response(r.status_code, text=r.text, etag=r.headers.get("ETag"),
                            last_modified=r.headers.get("Last-Modified"))
            if expect_json:
                try:
                    resp.data = r.json()
                except ValueError as e:
                    raise HttpError(r.status_code, url, "invalid JSON: " + r.text[:100]) from e
            if conditional:
                # Validators are only persisted after the caller has successfully parsed the body
                # (see commit_validators) so a parse failure never leaves us stuck on a 304.
                self.pending_validators.append((cache_key, resp.etag, resp.last_modified))
            return resp
        raise last_exc or HttpError(0, url, "request failed")

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, json_body: Any = None, **kw) -> Response:
        return self.request("POST", url, json_body=json_body, **kw)

    def scoped(self) -> "Http":
        """A view sharing session/limiter/cache but with its own pending-validator list, so the
        pipeline can persist ETags only for fetchers that succeeded end to end."""
        child = copy.copy(self)
        child.pending_validators = []
        return child

    def commit_validators(self) -> None:
        if self.cache is None:
            return
        with self._cache_lock:
            for key, etag, lm in self.pending_validators:
                self.cache.set_validators(key, etag, lm)
        self.pending_validators = []

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None = None) -> None:
        if retry_after and retry_after.isdigit():
            delay = min(float(retry_after), 30.0)
        else:
            delay = min(2 ** attempt + random.random(), 20.0)
        time.sleep(delay)
