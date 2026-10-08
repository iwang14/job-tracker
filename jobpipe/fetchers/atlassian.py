"""Atlassian careers. Applications run on iCIMS (careers-americas.icims.com,
globalcareers-atlassian.icims.com); the public careers page renders from a JSON listing:

    GET https://www.atlassian.com/endpoint/careers/listings

UNDOCUMENTED and unverified from the build sandbox: gated by jobpipe.robots (`tos_ok: true` +
robots.txt). Field names are read defensively. If this ever breaks, Atlassian is still covered by
the SimplifyJobs listings source, which carries Atlassian's iCIMS links.
"""
from __future__ import annotations

from ..http import Http
from ..models import Company, FetchResult, Posting
from ..normalize import html_to_text, to_iso
from ..robots import require_allowed
from .base import FetchContext, FetcherError

API = "https://www.atlassian.com/endpoint/careers/listings"


def _locations(j: dict) -> str:
    locs = j.get("locations") or j.get("location") or []
    if isinstance(locs, str):
        return locs
    return "; ".join(l if isinstance(l, str) else (l.get("name") or l.get("city") or "") for l in locs)


def fetch(company: Company, http: Http, ctx: FetchContext) -> FetchResult:
    url = company.options.get("endpoint", API)
    require_allowed(company, http, url)
    r = http.get(url, conditional=True)
    if r.not_modified:
        return FetchResult(unchanged=True)
    data = r.data
    rows = data if isinstance(data, list) else (data or {}).get("listings") or (data or {}).get("jobs")
    if not isinstance(rows, list):
        raise FetcherError("unexpected Atlassian listings shape: " + str(type(data)))
    out = []
    for j in rows:
        portal = j.get("portalJobPost") or {}
        link = portal.get("portalUrl") or j.get("applyUrl") or j.get("url") or \
            f"https://www.atlassian.com/company/careers/details/{j.get('id')}"
        desc = "\n\n".join(html_to_text(j.get(k)) for k in ("overview", "responsibilities", "qualifications",
                                                           "description") if j.get(k))
        out.append(Posting(
            company=company.name, company_key=company.key, source="atlassian",
            external_id=str(j.get("id") or portal.get("id") or link), title=(j.get("title") or "").strip(),
            url=link, description=desc, location_raw=_locations(j),
            posted_at=to_iso(j.get("updatedDate") or j.get("postedDate") or portal.get("updatedDate")),
            tier=company.tier,
        ))
    return FetchResult(out)
