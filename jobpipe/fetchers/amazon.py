"""Amazon (amazon.jobs) — the JSON search endpoint behind the public search page.

GET https://www.amazon.jobs/en/search.json?base_query=...&category[]=software-development
    &country[]=USA&country[]=CAN&result_limit=100&offset=N&sort=recent

UNDOCUMENTED endpoint, gated by jobpipe.robots: runs only with `tos_ok: true` on the company
(after you've reviewed amazon.jobs' terms) AND when amazon.jobs/robots.txt allows the URL.
Parameter names are overridable via `params:` in companies.yaml in case the site changes them.
ToS-clean alternatives that need no opt-in: the SimplifyJobs listings source and Amazon's own
job-alert emails (email_alerts in settings.yaml).
"""
from __future__ import annotations

from ..http import Http
from ..models import Company, FetchResult, Location, Posting
from ..normalize import ISO3, html_to_text, to_iso
from ..robots import require_allowed
from .base import FetchContext

API = "https://www.amazon.jobs/en/search.json"
PAGE = 100


def fetch(company: Company, http: Http, ctx: FetchContext) -> FetchResult:
    params = {
        "base_query": company.options.get("query", "software development engineer"),
        "category[]": company.options.get("categories", ["software-development"]),
        "country[]": company.options.get("countries", ["USA", "CAN"]),
        "result_limit": PAGE, "sort": "recent",
    }
    params.update(company.options.get("params", {}))
    require_allowed(company, http, API + "?base_query=software&offset=0")
    out, offset, complete = [], 0, True
    max_pages = min(ctx.max_pages, int(company.options.get("max_pages", 10)))
    for _ in range(max_pages):
        data = http.get(API, params={**params, "offset": offset}).data
        jobs = data.get("jobs") or []
        for j in jobs:
            cc = ISO3.get((j.get("country_code") or "").upper(), (j.get("country_code") or "")[:2] or None)
            desc = "\n\n".join(x for x in (
                html_to_text(j.get("description")),
                "Basic Qualifications:\n" + html_to_text(j.get("basic_qualifications")) if j.get("basic_qualifications") else "",
                "Preferred Qualifications:\n" + html_to_text(j.get("preferred_qualifications")) if j.get("preferred_qualifications") else "",
            ) if x)
            path = j.get("job_path") or ""
            out.append(Posting(
                company=company.name, company_key=company.key, source="amazon",
                external_id=str(j.get("id_icims") or j.get("id")), title=(j.get("title") or "").strip(),
                url=("https://www.amazon.jobs" + path) if path.startswith("/") else (path or ""),
                description=desc, location_raw=j.get("normalized_location") or j.get("location") or "",
                locations=[Location(city=j.get("city"), region=j.get("state"), country=cc,
                                    raw=j.get("location") or "")],
                posted_at=to_iso(j.get("posted_date")), tier=company.tier,
            ))
        offset += len(jobs)
        if not jobs or offset >= int(data.get("hits") or 0):
            break
    else:
        complete = False
    return FetchResult(out, complete=complete)
