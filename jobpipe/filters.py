"""Hard filters (cheap, deterministic). Everything is driven by settings.yaml -> filters."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .models import Posting


@dataclass
class FilterResult:
    passes: bool
    reason: str = ""


@lru_cache(maxsize=64)
def _rx(words: tuple[str, ...]) -> re.Pattern:
    parts = []
    for w in words:
        if w.startswith("re:"):
            parts.append(w[3:])
        else:
            parts.append(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])")
    return re.compile("|".join(parts) or r"(?!x)x", re.I)


def title_ok(title: str, cfg: dict) -> FilterResult:
    t = title.lower()
    if not _rx(tuple(cfg.get("title_include", []))).search(t):
        return FilterResult(False, "title: not a SWE role")
    m = _rx(tuple(cfg.get("title_exclude", []))).search(t)
    if m:
        return FilterResult(False, f"title: excluded '{m.group(0).strip()}'")
    return FilterResult(True)


def level_ok(p: Posting, cfg: dict) -> FilterResult:
    if p.level_guess in set(cfg.get("exclude_levels", [])):
        return FilterResult(False, f"level: {p.level_guess}")
    return FilterResult(True)


def yoe_ok(p: Posting, cfg: dict) -> FilterResult:
    max_yoe = cfg.get("max_required_yoe", 4)
    if p.yoe_min is not None and p.yoe_min > max_yoe:
        return FilterResult(False, f"yoe: requires {p.yoe_min}+ years")
    return FilterResult(True)


def location_ok(p: Posting, cfg: dict) -> FilterResult:
    countries = set(cfg.get("countries", ["CA", "US"]))
    remote_scopes = set(cfg.get("remote_scopes", ["Canada", "North America", "Global"]))
    if not p.locations:
        return FilterResult(bool(cfg.get("allow_unknown_location", True)), "location: unknown")
    if p.work_mode == "remote":
        scope = p.remote_scope or "Unspecified"
        if scope in remote_scopes:
            return FilterResult(True)
        if scope == "Unspecified" and cfg.get("allow_unspecified_remote", False):
            return FilterResult(True)
        # remote role that also lists an on-site city in an allowed country (e.g. "Remote US; Toronto")
        onsite = [loc for loc in p.locations if not loc.remote and loc.country in countries]
        if onsite:
            return FilterResult(True)
        return FilterResult(False, f"location: remote scope {scope}")
    if any(loc.country in countries for loc in p.locations if not loc.remote):
        return FilterResult(True)
    if all(loc.country is None for loc in p.locations):
        return FilterResult(bool(cfg.get("allow_unknown_location", True)), "location: unknown")
    return FilterResult(False, f"location: {p.country or p.location_raw}")


def evaluate(p: Posting, cfg: dict) -> FilterResult:
    for check in (lambda: title_ok(p.title, cfg), lambda: level_ok(p, cfg),
                  lambda: yoe_ok(p, cfg), lambda: location_ok(p, cfg)):
        r = check()
        if not r.passes:
            return r
    return FilterResult(True, "")
