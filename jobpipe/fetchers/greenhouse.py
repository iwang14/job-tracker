"""Greenhouse Job Board API (public, no auth).

GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
Docs: https://developers.greenhouse.io/job-board.html
"""
from __future__ import annotations

from ..http import Http
from ..models import Company, FetchResult, Posting
from ..normalize import html_to_text, to_iso
from .base import FetchContext

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


def fetch(company: Company, http: Http, ctx: FetchContext) -> FetchResult:
    url = API.format(token=company.board_token)
    r = http.get(url, params={"content": "true"}, conditional=True)
    if r.not_modified:
        return FetchResult(unchanged=True)
    out = []
    for j in r.data.get("jobs", []):
        meta = {m.get("name", "").lower(): m.get("value") for m in j.get("metadata") or [] if isinstance(m, dict)}
        workplace = meta.get("workplace type") or meta.get("location type") or meta.get("remote")
        out.append(Posting(
            company=company.name, company_key=company.key, source="greenhouse",
            external_id=str(j["id"]), title=j.get("title", "").strip(),
            url=j.get("absolute_url") or "",
            description=html_to_text(j.get("content")),
            location_raw=(j.get("location") or {}).get("name", "") or "",
            posted_at=to_iso(j.get("first_published") or j.get("updated_at")),
            work_mode_hint=str(workplace).lower() if isinstance(workplace, str) else None,
            tier=company.tier,
        ))
    return FetchResult(out)
