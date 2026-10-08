"""Fill in job descriptions for community-list postings whose apply link is a public ATS posting.

Community lists only give title/company/location, so those postings score low and the YOE filter
can't see requirements. When the apply link points at Greenhouse, Lever or Ashby, the same public
APIs the fetchers use return the full posting. Only postings that already pass the hard filters
are enriched, capped per run (settings.yaml -> community_enrich.max_per_run).
"""
from __future__ import annotations

import logging
import re

from .. import filters, normalize
from ..db import DB, row_locations
from ..http import Http, HttpError
from ..models import Posting

log = logging.getLogger(__name__)

GH = re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_-]+)/jobs/(\d+)", re.I)
LEVER = re.compile(r"jobs\.(eu\.)?lever\.co/([A-Za-z0-9_.-]+)/([0-9a-f-]{36})", re.I)
ASHBY = re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)/([0-9a-f-]{36})", re.I)


def fetch_description(url: str, http: Http, ashby_boards: dict) -> str | None:
    if m := GH.search(url):
        j = http.get(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}").data
        return normalize.html_to_text(j.get("content"))
    if m := LEVER.search(url):
        host = "api.eu.lever.co" if m.group(1) else "api.lever.co"
        j = http.get(f"https://{host}/v0/postings/{m.group(2)}/{m.group(3)}").data
        parts = [j.get("descriptionPlain") or normalize.html_to_text(j.get("description"))]
        parts += [f"{l.get('text', '')}:\n{normalize.html_to_text(l.get('content'))}" for l in j.get("lists") or []]
        parts.append(j.get("additionalPlain") or "")
        return "\n\n".join(p for p in parts if p)
    if m := ASHBY.search(url):
        board = m.group(1)
        if board not in ashby_boards:
            data = http.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}").data
            ashby_boards[board] = {j["id"]: j for j in data.get("jobs", [])}
        j = ashby_boards[board].get(m.group(2))
        return (j.get("descriptionPlain") or normalize.html_to_text(j.get("descriptionHtml"))) if j else None
    return None


def enrich_community(db: DB, http: Http, settings, max_per_run: int | None = None) -> int:
    cfg = settings.section("community_enrich")
    if cfg.get("enabled", True) is False:
        return 0
    limit = max_per_run if max_per_run is not None else int(cfg.get("max_per_run", 15))
    rows = db.query(
        "SELECT * FROM postings WHERE source LIKE 'community:%' AND passes_filters=1 AND duplicate_of IS NULL "
        "AND status='open' AND enrich_tried=0 AND length(description) < 120 AND (url LIKE '%greenhouse.io%' OR url LIKE '%lever.co%' "
        "OR url LIKE '%ashbyhq.com%') ORDER BY tier, first_seen_at DESC LIMIT ?", (limit,))
    fcfg = settings.section("filters")
    done, ashby_boards = 0, {}
    http = http.scoped()
    http.retries = 0  # best effort: one attempt per posting, so a dead host can't stretch the run
    for r in rows:
        try:
            desc = fetch_description(r["url"], http, ashby_boards)
        except (HttpError, ValueError, KeyError, TypeError, AttributeError) as e:  # best effort, never fails the run
            log.info("enrich: %s -> %s", r["url"], e)
            desc = None
        db.update_posting(r["id"], enrich_tried=1)  # one attempt per posting
        if not desc:
            continue
        p = Posting(company=r["company"], company_key=r["company_key"], source=r["source"],
                    external_id=r["external_id"], title=r["title"], url=r["url"], description=desc,
                    location_raw=r["location_raw"], locations=row_locations(r), posted_at=r["posted_at"],
                    tier=r["tier"])
        normalize.enrich(p)
        fr = filters.evaluate(p, fcfg)  # now the YOE filter can see the requirements
        db.update_posting(r["id"], description=desc if fr.passes else "", yoe_min=p.yoe_min,
                          work_authorization_note=p.work_authorization_note, salary=p.salary or r["salary"],
                          work_mode=p.work_mode, remote_scope=p.remote_scope,
                          passes_filters=int(fr.passes), filter_reason=fr.reason, score=None)
        done += 1
    db.commit()
    return done
