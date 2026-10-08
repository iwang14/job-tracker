"""Ashby public job posting API (no auth).

GET https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true
Docs: https://developers.ashbyhq.com/docs/public-job-posting-api
"""
from __future__ import annotations

from ..http import Http
from ..models import Company, FetchResult, Location, Posting
from ..normalize import html_to_text, parse_location, to_iso
from .base import FetchContext

API = "https://api.ashbyhq.com/posting-api/job-board/{token}"
WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}
COUNTRY = {"united states": "US", "usa": "US", "canada": "CA"}


def _loc(name: str | None, address: dict | None, remote: bool) -> list[Location]:
    pa = (address or {}).get("postalAddress") or {}
    country = pa.get("addressCountry")
    if pa.get("addressLocality") or country:
        cc = COUNTRY.get((country or "").lower(), country if country and len(country) == 2 else None)
        return [Location(city=pa.get("addressLocality"), region=pa.get("addressRegion"), country=cc,
                         remote=remote or "remote" in (name or "").lower(), raw=name or "")]
    locs = parse_location(name or "")
    for loc in locs:
        loc.remote = loc.remote or remote
    return locs


def fetch(company: Company, http: Http, ctx: FetchContext) -> FetchResult:
    r = http.get(API.format(token=company.board_token), params={"includeCompensation": "true"}, conditional=True)
    if r.not_modified:
        return FetchResult(unchanged=True)
    out = []
    for j in r.data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        remote = bool(j.get("isRemote"))
        locations = _loc(j.get("location"), j.get("address"), remote)
        for sec in j.get("secondaryLocations") or []:
            locations += _loc(sec.get("location"), sec.get("address"), remote)
        comp = j.get("compensation") or {}
        names = [j.get("location") or ""] + [s.get("location") or "" for s in j.get("secondaryLocations") or []]
        out.append(Posting(
            company=company.name, company_key=company.key, source="ashby",
            external_id=j["id"], title=(j.get("title") or "").strip(),
            url=j.get("jobUrl") or j.get("applyUrl") or "",
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            location_raw="; ".join(n for n in names if n),
            locations=locations,
            posted_at=to_iso(j.get("publishedAt")),
            salary=comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary"),
            work_mode_hint=WORKPLACE.get((j.get("workplaceType") or "").lower()) or ("remote" if remote else None),
            tier=company.tier,
        ))
    return FetchResult(out)
