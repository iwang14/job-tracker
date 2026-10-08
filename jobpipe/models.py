"""Core data types shared across the pipeline."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@dataclass
class Company:
    name: str
    tier: int = 3
    ats_type: str = "unknown"
    board_token: str | None = None
    careers_url: str | None = None
    enabled: bool = True
    # free-form per-fetcher options (e.g. Workday search text, Amazon query)
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return slugify(self.name)

    @property
    def fetcher_key(self) -> str:
        """Identity used for fetcher health tracking."""
        return f"{self.ats_type}:{self.board_token or self.key}"


@dataclass
class Location:
    city: str | None = None
    region: str | None = None   # province / state code, e.g. "ON", "WA"
    country: str | None = None  # ISO-2: "CA", "US", or other
    remote: bool = False
    raw: str = ""

    def label(self) -> str:
        parts = [p for p in (self.city, self.region, self.country) if p]
        s = ", ".join(parts) if parts else (self.raw or "")
        return f"Remote ({s})" if self.remote and s else ("Remote" if self.remote else s)


@dataclass
class Posting:
    """A normalized job posting. Fetchers fill the raw fields; normalize.enrich fills the rest."""
    company: str
    company_key: str
    source: str              # ats type or "community:<name>"
    external_id: str
    title: str
    url: str
    description: str = ""
    location_raw: str = ""
    posted_at: str | None = None        # ISO-8601 UTC
    salary: str | None = None
    work_mode_hint: str | None = None   # what the ATS says, if anything
    country_hint: str | None = None     # ISO-2 from the ATS, if any
    tier: int = 3
    # --- derived (normalize.enrich) ---
    locations: list[Location] = field(default_factory=list)
    level_guess: str = "unspecified"
    country: str = ""                   # "CA", "US", "CA|US", or other codes
    city: str = ""
    work_mode: str = "onsite"           # remote / hybrid / onsite
    remote_scope: str | None = None     # Canada / North America / US-only / Global / Other
    yoe_min: int | None = None
    work_authorization_note: str | None = None
    # --- lifecycle (db) ---
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    status: str = "open"

    @property
    def id(self) -> str:
        return posting_id(self.company_key, self.source, self.external_id)

    @property
    def content_hash(self) -> str:
        h = hashlib.sha1()
        h.update((self.title + "\n" + self.location_raw + "\n" + self.description).encode())
        return h.hexdigest()[:16]


def posting_id(company_key: str, source: str, external_id: str) -> str:
    raw = f"{company_key}|{source}|{external_id}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


@dataclass
class FetchResult:
    postings: list[Posting] = field(default_factory=list)
    # complete=False means we may not have seen every open posting (paging cap hit, filtered
    # query, ...). Incomplete results never count toward "missing" -> closed.
    complete: bool = True
    # unchanged=True means the board answered 304 Not Modified: every known posting is still open.
    unchanged: bool = False
