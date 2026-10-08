"""SmartRecruiters Posting API (public, no auth).

List:   GET https://api.smartrecruiters.com/v1/companies/{id}/postings?limit=100&offset=N
Detail: GET https://api.smartrecruiters.com/v1/companies/{id}/postings/{postingId}
Docs: https://developers.smartrecruiters.com/docs/posting-api

The list has no description, so details are fetched only for new postings whose title passes
the cheap title filter.
"""
from __future__ import annotations

from ..http import Http
from ..models import Company, FetchResult, Location, Posting
from ..normalize import html_to_text, to_iso
from .base import FetchContext

API = "https://api.smartrecruiters.com/v1/companies/{token}/postings"
PAGE = 100


def _description(detail: dict) -> str:
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        sec = sections.get(key) or {}
        if sec.get("text"):
            parts.append(f"{sec.get('title') or key}:\n{html_to_text(sec['text'])}")
    return "\n\n".join(parts)


def fetch(company: Company, http: Http, ctx: FetchContext) -> FetchResult:
    base = API.format(token=company.board_token)
    out, offset, complete = [], 0, True
    for _ in range(ctx.max_pages):
        r = http.get(base, params={"limit": PAGE, "offset": offset})
        content = r.data.get("content", [])
        for j in content:
            loc = j.get("location") or {}
            title = (j.get("name") or "").strip()
            ext = str(j["id"])
            description = ""
            url = f"https://jobs.smartrecruiters.com/{company.board_token}/{ext}"
            if ext not in ctx.known_ids and ctx.title_ok(title):
                d = http.get(f"{base}/{ext}").data
                description = _description(d)
                url = d.get("postingUrl") or url
            mode = "remote" if loc.get("remote") else ("hybrid" if loc.get("hybrid") else None)
            out.append(Posting(
                company=company.name, company_key=company.key, source="smartrecruiters",
                external_id=ext, title=title, url=url, description=description,
                location_raw=loc.get("fullLocation") or ", ".join(
                    x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x),
                locations=[Location(city=loc.get("city"), region=(loc.get("region") or None),
                                    country=(loc.get("country") or "").upper() or None,
                                    remote=bool(loc.get("remote")), raw=loc.get("fullLocation") or "")],
                posted_at=to_iso(j.get("releasedDate")),
                work_mode_hint=mode, tier=company.tier,
            ))
        offset += len(content)
        if not content or offset >= r.data.get("totalFound", 0):
            break
    else:
        complete = False
    return FetchResult(out, complete=complete)
