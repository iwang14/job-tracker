"""Lever Postings API (public, no auth).

GET https://api.lever.co/v0/postings/{company}?mode=json   (EU boards: api.eu.lever.co)
Docs: https://github.com/lever/postings-api
"""
from __future__ import annotations

from ..http import Http
from ..models import Company, FetchResult, Posting
from ..normalize import html_to_text, to_iso
from .base import FetchContext

API = "https://api.lever.co/v0/postings/{token}"
API_EU = "https://api.eu.lever.co/v0/postings/{token}"
WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}


def _salary(sr: dict | None) -> str | None:
    if not sr or sr.get("min") is None:
        return None
    cur = sr.get("currency") or ""
    interval = (sr.get("interval") or "").replace("-", " ")
    return f"{cur} {sr['min']:,}–{sr.get('max', sr['min']):,} {interval}".strip()


def fetch(company: Company, http: Http, ctx: FetchContext) -> FetchResult:
    base = API_EU if company.options.get("region") == "eu" else API
    r = http.get(base.format(token=company.board_token), params={"mode": "json"}, conditional=True)
    if r.not_modified:
        return FetchResult(unchanged=True)
    out = []
    for j in r.data or []:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
        for lst in j.get("lists") or []:
            parts.append(f"{lst.get('text', '')}:\n{html_to_text(lst.get('content'))}")
        parts.append(j.get("additionalPlain") or html_to_text(j.get("additional")))
        out.append(Posting(
            company=company.name, company_key=company.key, source="lever",
            external_id=j["id"], title=(j.get("text") or "").strip(),
            url=j.get("hostedUrl") or j.get("applyUrl") or "",
            description="\n\n".join(p for p in parts if p),
            location_raw="; ".join(locs),
            posted_at=to_iso(j.get("createdAt")),
            salary=_salary(j.get("salaryRange")),
            work_mode_hint=WORKPLACE.get((j.get("workplaceType") or "").lower()),
            country_hint=j.get("country"),
            tier=company.tier,
        ))
    return FetchResult(out)
