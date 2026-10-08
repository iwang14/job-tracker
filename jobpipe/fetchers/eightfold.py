"""Eightfold-powered career sites (Microsoft's apply.careers.microsoft.com, and others).

GET https://{host}/api/apply/v2/jobs?domain={domain}&start=N&num=10&query=...&location=...&sort_by=timestamp

UNDOCUMENTED and unverified from the build sandbox: disabled by default. Run
`python -m jobpipe verify --company Microsoft` from Actions before enabling.
companies.yaml: token "apply.careers.microsoft.com|microsoft.com" (host|domain).
"""
from __future__ import annotations

from ..http import Http
from ..models import Company, FetchResult, Posting
from ..normalize import html_to_text, to_iso
from .base import FetchContext, FetcherError

PAGE = 10
WORK_OPTION = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}


def fetch(company: Company, http: Http, ctx: FetchContext) -> FetchResult:
    try:
        host, domain = (company.board_token or "").split("|")
    except ValueError as e:
        raise FetcherError("eightfold token must be 'host|domain'") from e
    out, complete = [], True
    seen: set[str] = set()
    for location in company.options.get("locations", ["Canada", "United States"]):
        start, max_pages = 0, min(ctx.max_pages, int(company.options.get("max_pages", 10)))
        for _ in range(max_pages):
            data = http.get(f"https://{host}/api/apply/v2/jobs", params={
                "domain": domain, "start": start, "num": PAGE, "sort_by": "timestamp",
                "query": company.options.get("query", "software engineer"), "location": location,
            }).data
            rows = data.get("positions") or []
            for j in rows:
                ext = str(j.get("id") or j.get("ats_job_id"))
                if ext in seen:
                    continue
                seen.add(ext)
                locs = j.get("locations") or ([j["location"]] if j.get("location") else [])
                out.append(Posting(
                    company=company.name, company_key=company.key, source="eightfold", external_id=ext,
                    title=(j.get("name") or "").strip(),
                    url=j.get("canonicalPositionUrl") or f"https://{host}/careers/job/{ext}",
                    description=html_to_text(j.get("job_description")),
                    location_raw="; ".join(locs),
                    posted_at=to_iso(j.get("t_create") or j.get("t_update")),
                    work_mode_hint=WORK_OPTION.get((j.get("work_location_option") or "").lower()),
                    tier=company.tier,
                ))
            start += len(rows)
            if not rows or start >= int(data.get("count") or 0):
                break
        else:
            complete = False
    return FetchResult(out, complete=complete)
