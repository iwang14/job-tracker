"""0-100 score = skill fit + location weight + tier bonus + freshness bonus (all configurable).

Skill fit comes from keyword overlap with profile.yaml, blended with the LLM's semantic fit
when an API key is configured (see llm.py). Without a key, keyword fit is used alone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache

from .models import Location


@dataclass
class Skill:
    name: str
    weight: float = 1.0
    aliases: tuple[str, ...] = ()


def parse_skills(profile: dict) -> list[Skill]:
    out = []
    for group, default_w in (("core", 3.0), ("secondary", 1.5), ("nice", 0.75)):
        for s in (profile.get("skills") or {}).get(group, []) or []:
            if isinstance(s, str):
                out.append(Skill(s, default_w))
            else:
                out.append(Skill(s["name"], float(s.get("weight", default_w)), tuple(s.get("aliases", []))))
    return out


@lru_cache(maxsize=512)
def _skill_rx(term: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9#+])", re.I)


def skill_matches(text: str, skills: list[Skill]) -> list[Skill]:
    return [s for s in skills if any(_skill_rx(t).search(text) for t in (s.name, *s.aliases))]


def keyword_fit(title: str, description: str, profile: dict) -> tuple[float, list[str]]:
    """0..1 ratio, saturating at profile.skill_saturation matched weight."""
    skills = parse_skills(profile)
    text = f"{title}\n{description}"
    hits = skill_matches(text, skills)
    sat = float(profile.get("skill_saturation", 9.0))
    ratio = min(1.0, sum(s.weight for s in hits) / sat) if sat else 0.0
    penalty = 0.0
    for term in profile.get("avoid_keywords", []) or []:
        if _skill_rx(term).search(text):
            penalty += 0.1
    return max(0.0, ratio - penalty), [s.name for s in hits]


def location_points(locations: list[Location], work_mode: str, remote_scope: str | None, cfg: dict) -> tuple[int, str]:
    cities = {k.lower(): v for k, v in (cfg.get("cities") or {}).items()}
    best, why = int(cfg.get("default", 0)), "default"
    for loc in locations:
        if loc.remote:
            continue
        if loc.city and loc.city.lower() in cities and cities[loc.city.lower()] > best:
            best, why = cities[loc.city.lower()], loc.city
        elif loc.country == "CA" and cfg.get("canada_other", 0) > best:
            best, why = cfg["canada_other"], "Canada"
        elif loc.country == "US" and cfg.get("us", 0) > best:
            best, why = cfg["us"], "US"
    if work_mode == "remote":
        if remote_scope == "Canada" and cfg.get("remote_canada", 0) > best:
            best, why = cfg["remote_canada"], "remote-Canada"
        elif remote_scope in ("North America", "Global") and cfg.get("remote_north_america", 0) > best:
            best, why = cfg["remote_north_america"], "remote-NA"
    return int(best), why


def freshness_points(posted_at: str | None, first_seen_at: str | None, cfg: dict, now: datetime | None = None) -> int:
    ts = posted_at or first_seen_at
    if not ts:
        return 0
    now = now or datetime.now(timezone.utc)
    age_h = (now - datetime.fromisoformat(ts)).total_seconds() / 3600
    for bucket in sorted(cfg.get("buckets", []), key=lambda b: b["max_hours"]):
        if age_h <= bucket["max_hours"]:
            return int(bucket["points"])
    return 0


@dataclass
class Score:
    total: int
    breakdown: dict = field(default_factory=dict)


def score_posting(*, title: str, description: str, locations: list[Location], work_mode: str,
                  remote_scope: str | None, tier: int, posted_at: str | None, first_seen_at: str | None,
                  settings: dict, profile: dict, llm_fit: int | None = None, now: datetime | None = None) -> Score:
    sc = settings.get("scoring") or {}
    skill_max = float(sc.get("skill_max", 70))
    kw_ratio, hits = keyword_fit(title, description, profile)
    if llm_fit is not None:
        w = float(sc.get("llm_weight", 0.65))
        fit_ratio = (1 - w) * kw_ratio + w * (llm_fit / 100)
    else:
        fit_ratio = kw_ratio
    skill_pts = round(skill_max * fit_ratio)
    loc_pts, loc_why = location_points(locations, work_mode, remote_scope, sc.get("location_weights") or {})
    tier_pts = int((sc.get("tier_bonus") or {}).get(tier, (sc.get("tier_bonus") or {}).get(str(tier), 0)))
    fresh_pts = freshness_points(posted_at, first_seen_at, sc.get("freshness") or {}, now)
    total = max(0, min(100, skill_pts + loc_pts + tier_pts + fresh_pts))
    return Score(total, {
        "skills": skill_pts, "skill_hits": hits, "llm_fit": llm_fit, "location": loc_pts,
        "location_why": loc_why, "tier": tier_pts, "fresh": fresh_pts,
    })
