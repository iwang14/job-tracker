"""Workday CXS JSON endpoint (the one the public Workday careers site itself calls).

List:   POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
        body {"appliedFacets": {}, "limit": 20, "offset": N, "searchText": "..."}
Detail: GET  https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}

companies.yaml token format: "tenant/wdN/site", e.g. "nvidia/wd5/NVIDIAExternalCareerSite".
Optional `search_text` (default "software") keeps result sets small for big employers.
Quirk: `total` is only populated on the first page.
"""
from __future__ import annotations

from ..http import Http
from ..models import Company, FetchResult, Location, Posting
from ..normalize import COUNTRY_WORDS, html_to_text, parse_location, to_iso
from .base import FetchContext, FetcherError

PAGE = 20
REMOTE_TYPES = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite",
                "flexible": "hybrid"}


def parse_token(token: str) -> tuple[str, str, str]:
    parts = (token or "").strip("/").split("/")
    if len(parts) != 3 or not parts[1].startswith("wd"):
        raise FetcherError(f"workday token must be 'tenant/wdN/site', got {token!r}")
    return parts[0], parts[1], parts[2]


def fetch(company: Company, http: Http, ctx: FetchContext) -> FetchResult:
    tenant, wd, site = parse_token(company.board_token or "")
    host = f"https://{tenant}.{wd}.myworkdayjobs.com"
    base = f"{host}/wday/cxs/{tenant}/{site}"
    search = company.options.get("search_text", "software")
    out, offset, total, complete = [], 0, None, True
    for _ in range(ctx.max_pages):
        body = {"appliedFacets": company.options.get("facets", {}), "limit": PAGE, "offset": offset,
                "searchText": search}
        data = http.post(f"{base}/jobs", json_body=body, headers={"Content-Type": "application/json"}).data
        if total is None:
            total = data.get("total") or 0
        rows = data.get("jobPostings") or []
        for j in rows:
            path = j.get("externalPath") or ""
            title = (j.get("title") or "").strip()
            p = Posting(
                company=company.name, company_key=company.key, source="workday", external_id=path,
                title=title, url=f"{host}/en-US/{site}{path}", location_raw=j.get("locationsText") or "",
                posted_at=to_iso(j.get("postedOn")), tier=company.tier,
            )
            if path not in ctx.known_ids and ctx.title_ok(title):
                info = (http.get(f"{base}{path}").data or {}).get("jobPostingInfo") or {}
                p.description = html_to_text(info.get("jobDescription"))
                p.url = info.get("externalUrl") or p.url
                p.posted_at = to_iso(info.get("startDate")) or p.posted_at
                names = [info.get("location")] + list(info.get("additionalLocations") or [])
                p.location_raw = "; ".join(n for n in names if n) or p.location_raw
                cc = ((info.get("country") or {}).get("descriptor") or "")
                p.locations = parse_location(p.location_raw)
                code = COUNTRY_WORDS.get(cc.lower()) if cc else None
                for loc in p.locations:
                    loc.country = loc.country or code
                if code and not p.locations:
                    p.locations = [Location(country=code, raw=cc)]
                p.work_mode_hint = REMOTE_TYPES.get((info.get("remoteType") or "").lower())
            out.append(p)
        offset += len(rows)
        if not rows or offset >= total:
            break
    else:
        complete = False
    return FetchResult(out, complete=complete)
