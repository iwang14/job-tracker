"""Duplicate / re-post detection.

Exact identity is (company_key, source, external_id) and is enforced by the DB. This module
catches the fuzzy cases:
  * the same job listed twice (e.g. on the ATS and on a community list, or two req ids), and
  * re-posts (job closed, then re-opened under a new id).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import urlparse

from .db import DB, row_locations
from .models import Location, Posting
from .normalize import norm_title

TITLE_THRESHOLD = 0.92
REPOST_WINDOW_DAYS = 45


def canonical_url(url: str) -> str:
    u = urlparse(url or "")
    return (u.netloc.lower().removeprefix("www.") + u.path.rstrip("/")).lower()


def _loc_keys(locs: list[Location]) -> set[tuple]:
    keys = set()
    for loc in locs:
        if loc.remote:
            keys.add(("remote", loc.country))
        elif loc.city:
            keys.add((loc.country, loc.city.lower().strip()))
        elif loc.country:
            keys.add((loc.country, None))
    return keys


def locations_overlap(a: list[Location], b: list[Location]) -> bool:
    ka, kb = _loc_keys(a), _loc_keys(b)
    if not ka or not kb:
        return True  # unknown location on either side: rely on the title match
    if ka & kb:
        return True
    # "Toronto, ON" vs "Canada": country-only side matches any city in that country
    ca = {k[0] for k in ka if k[1] is None}
    cb = {k[0] for k in kb if k[1] is None}
    return bool(ca & {k[0] for k in kb}) or bool(cb & {k[0] for k in ka})


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()


def find_match(db: DB, p: Posting, now: str, threshold: float = TITLE_THRESHOLD) -> tuple[str, str] | None:
    """Return ("duplicate"|"repost", canonical_id) if `p` matches an existing posting."""
    since = (datetime.fromisoformat(now) - timedelta(days=REPOST_WINDOW_DAYS)).isoformat()
    p_url = canonical_url(p.url)
    best: tuple[float, object] | None = None
    for row in db.candidates_for_dedupe(p.company_key, p.id, since):
        if p_url and canonical_url(row["url"]) == p_url:
            best = (2.0, row)
            break
        sim = title_similarity(p.title, row["title"])
        if sim >= threshold and locations_overlap(p.locations, row_locations(row)):
            if best is None or sim > best[0]:
                best = (sim, row)
    if not best:
        return None
    row = best[1]
    canonical = row["duplicate_of"] or row["id"]
    return ("duplicate" if row["status"] == "open" else "repost", canonical)
