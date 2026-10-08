"""Eightfold-powered career sites (Microsoft's apply.careers.microsoft.com, and others).

Tries, in order (both are the JSON the public careers page itself calls):
  GET https://{host}/api/apply/v2/jobs?domain={domain}&start=N&num=10&query=...&location=...
  GET https://{host}/api/pcsx/search?domain={domain}&start=N&query=...&location=...
The first endpoint that answers is remembered for the rest of the run.

UNDOCUMENTED, gated by jobpipe.robots (`tos_ok: true` + robots.txt). companies.yaml token:
"apply.careers.microsoft.com|microsoft.com" (host|domain).
"""
from __future__ import annotations

from ..http import Http, HttpError
from ..models import Company, FetchResult, Posting
from ..normalize import html_to_text, to_iso
from ..robots import require_allowed
from .base import FetchContext, FetcherError

PAGE = 10
WORK_OPTION = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}
ENDPOINTS = ("/api/apply/v2/jobs", "/api/pcsx/search")


def _rows(data: dict) -> tuple[list[dict], int]:
    """Both response shapes: {positions, count} and {data: {positions, count}}."""
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    return inner.get("positions") or [], int(inner.get("count") or inner.get("total") or 0)


def _posting(company: Company, host: str, j: dict) -> Posting:
    ext = str(j.get("id") or j.get("ats_job_id") or j.get("displayJobId"))
    locs = j.get("locations") or j.get("standardizedLocations") or ([j["location"]] if j.get("location") else [])
    url = j.get("canonicalPositionUrl") or j.get("positionUrl") or f"https://{host}/careers/job/{ext}"
    if url.startswith("/"):
        url = f"https://{host}{url}"
    return Posting(
        company=company.name, company_key=company.key, source="eightfold", external_id=ext,
        title=(j.get("name") or j.get("title") or "").strip(), url=url,
        description=html_to_text(j.get("job_description") or j.get("jobDescription")),
        location_raw="; ".join(l if isinstance(l, str) else str(l.get("name", "")) for l in locs),
        posted_at=to_iso(j.get("t_create") or j.get("postedTs") or j.get("creationTs") or j.get("t_update")),
        work_mode_hint=WORK_OPTION.get((j.get("work_location_option") or j.get("workLocationOption") or "").lower()),
        tier=company.tier,
    )


def fetch(company: Company, http: Http, ctx: FetchContext) -> FetchResult:
    try:
        host, domain = (company.board_token or "").split("|")
    except ValueError as e:
        raise FetcherError("eightfold token must be 'host|domain'") from e
    endpoints = [company.options["endpoint"]] if company.options.get("endpoint") else list(ENDPOINTS)
    require_allowed(company, http, *(f"https://{host}{e}?domain={domain}" for e in endpoints[:1]))
    out, complete, seen = [], True, set()
    for location in company.options.get("locations", ["Canada", "United States"]):
        start, max_pages = 0, min(ctx.max_pages, int(company.options.get("max_pages", 10)))
        for _ in range(max_pages):
            params = {"domain": domain, "start": start, "num": PAGE, "sort_by": "timestamp",
                      "query": company.options.get("query", "software engineer"), "location": location}
            data, last_err = None, None
            for ep in list(endpoints):
                try:
                    data = http.get(f"https://{host}{ep}", params=params).data
                    endpoints = [ep]  # stick with the one that works
                    break
                except HttpError as e:
                    last_err = e
            if data is None:
                raise last_err or FetcherError("no eightfold endpoint answered")
            rows, total = _rows(data)
            for j in rows:
                p = _posting(company, host, j)
                if p.external_id not in seen:
                    seen.add(p.external_id)
                    out.append(p)
            start += len(rows)
            if not rows or start >= total:
                break
        else:
            complete = False
    return FetchResult(out, complete=complete)
