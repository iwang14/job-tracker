"""Community-maintained GitHub job lists (e.g. SimplifyJobs New-Grad-Positions).

Each README table row becomes a posting (source "community:<name>"), and any company not in
companies.yaml is recorded as a suggestion (with its ATS detected from the apply link) for you to
approve with `python -m jobpipe suggestions`.

Handles both table styles these repos use: HTML <table> (SimplifyJobs) and Markdown pipes.
Rows whose company cell is "↳" belong to the previous company.
"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass

from .. import filters, normalize
from ..db import DB
from ..http import Http
from ..models import Company, FetchResult, Posting, slugify
from .ats_detect import detect_from_text

_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_TD = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
_HREF = re.compile(r'href="([^"]+)"', re.I)
_TAG = re.compile(r"<[^>]+>")
_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
CLOSED_MARKERS = ("🔒", "closed")


@dataclass
class CommunitySource:
    raw: dict

    @property
    def name(self) -> str:
        return self.raw.get("name") or slugify(self.raw["url"])[-40:]

    @property
    def source(self) -> str:
        return f"community:{self.name}"

    @property
    def key(self) -> str:
        return self.source

    @property
    def url(self) -> str:
        return self.raw["url"]


@dataclass
class Row:
    company: str
    title: str
    location: str
    url: str
    age: str = ""


def _text(cell: str) -> str:
    t = _TAG.sub(" ", cell.replace("</br>", "; ").replace("<br>", "; ").replace("<br/>", "; "))
    t = _MD_LINK.sub(r"\1", t)
    t = html.unescape(t).replace("**", "")
    return re.sub(r"\s+", " ", t).strip(" ;")


def _apply_link(cell: str) -> str:
    links = _HREF.findall(cell) + [m.group(2) for m in _MD_LINK.finditer(cell)]
    # prefer the employer's link over simplify.jobs / imgur
    for link in links:
        if "simplify.jobs" not in link and "imgur.com" not in link:
            return html.unescape(link)
    return html.unescape(links[0]) if links else ""


def _strip_tracking(url: str) -> str:
    url = re.sub(r"([?&])(utm_[a-z]+|ref)=[^&]*", r"\1", url)
    return re.sub(r"[?&]+$", "", url.replace("?&", "?").replace("&&", "&"))


def _columns(header: list[str]) -> dict[str, int]:
    cols = {}
    for i, h in enumerate(header):
        h = h.lower()
        if "company" in h:
            cols.setdefault("company", i)
        elif any(k in h for k in ("role", "position", "title")):
            cols.setdefault("title", i)
        elif "location" in h:
            cols.setdefault("location", i)
        elif any(k in h for k in ("application", "apply", "link", "posting")):
            cols.setdefault("link", i)
        elif any(k in h for k in ("age", "date", "posted")):
            cols.setdefault("age", i)
    return cols


def parse_tables(text: str) -> list[Row]:
    rows: list[Row] = []
    if "<table" in text.lower():
        for table in re.findall(r"<table\b.*?</table>", text, re.S | re.I):
            trs = _TR.findall(table)
            if not trs:
                continue
            header = [_text(c) for c in _TD.findall(trs[0])]
            cols = _columns(header)
            if "company" not in cols or "title" not in cols:
                continue
            rows += _rows_from_cells([_TD.findall(tr) for tr in trs[1:]], cols)
    lines = text.splitlines()
    i = 0
    while i < len(lines) - 1:
        if lines[i].strip().startswith("|") and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1]):
            header = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            cols = _columns(header)
            body = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                body.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            if "company" in cols and "title" in cols:
                rows += _rows_from_cells(body, cols)
            i = j
        else:
            i += 1
    return rows


def _rows_from_cells(cell_rows: list[list[str]], cols: dict[str, int]) -> list[Row]:
    out, last_company = [], ""
    for cells in cell_rows:
        if len(cells) <= max(cols.values()):
            continue
        company = _text(cells[cols["company"]])
        if company in ("↳", "") and last_company:
            company = last_company
        last_company = company
        link_cell = cells[cols.get("link", cols["title"])]
        title_cell = cells[cols["title"]]
        if any(m in link_cell for m in CLOSED_MARKERS) or "🔒" in title_cell:
            continue  # closed listing
        url = _apply_link(link_cell) or _apply_link(title_cell)
        if not url:
            continue
        out.append(Row(
            company=company.replace("🔥", "").strip(), title=_text(title_cell).replace("🛂", "").replace("🇺🇸", "").strip(),
            location=_text(cells[cols["location"]]) if "location" in cols else "",
            url=_strip_tracking(url), age=_text(cells[cols["age"]]) if "age" in cols else "",
        ))
    return out


def _match_company(name: str, by_key: dict[str, Company]) -> Company | None:
    k = slugify(re.sub(r"\b(inc|llc|ltd|corp|corporation)\b\.?", "", name, flags=re.I))
    return by_key.get(k) or by_key.get(k.replace("-", ""))


def fetch_source(src: dict, http: Http, companies: list[Company]) -> FetchResult:
    cs = CommunitySource(src)
    r = http.get(cs.url, expect_json=False, conditional=True)
    if r.not_modified:
        return FetchResult(unchanged=True)
    by_key = {}
    for c in companies:
        by_key[c.key] = c
        by_key[c.key.replace("-", "")] = c
    postings = []
    for row in parse_tables(r.text):
        c = _match_company(row.company, by_key)
        ext = hashlib.sha1(row.url.encode()).hexdigest()[:16]
        postings.append(Posting(
            company=c.name if c else row.company, company_key=c.key if c else slugify(row.company),
            source=cs.source, external_id=ext, title=row.title, url=row.url, location_raw=row.location,
            posted_at=_age_to_iso(row.age),
            tier=c.tier if c else int(src.get("tier", 3)),
        ))
    return FetchResult(postings)


def _age_to_iso(age: str) -> str | None:
    m = re.fullmatch(r"(\d+)\s*(d|h|w|mo)", age or "")
    if not m:
        return None
    unit = {"d": "day", "h": "hour", "w": "week", "mo": "month"}[m.group(2)]
    return normalize.to_iso(f"{m.group(1)} {unit}s ago")


def ingest_community(db: DB, cs: CommunitySource, result: FetchResult, settings, now: str, stats) -> None:
    """Like pipeline.ingest but board-agnostic, and records suggested companies."""
    from ..pipeline import ingest  # local import: pipeline imports this module lazily too
    tracked = {r["company_key"] for r in db.query("SELECT DISTINCT company_key FROM postings WHERE source NOT LIKE 'community:%'")}
    from ..config import load_companies
    known = {c.key for c in load_companies()} | tracked
    first_time = not db.query("SELECT 1 FROM postings WHERE source=? LIMIT 1", (cs.source,))
    ingest(db, None, result, settings, now, stats, bootstrap_all=first_time)
    seen = {p.id for p in result.postings}
    close_after = int(settings.section("fetch").get("close_after_missed_runs", 2))
    stats.closed += len(db.mark_missing_source(cs.source, seen, now, close_after))
    fcfg = settings.section("filters")
    for p in result.postings:
        if p.company_key in known:
            continue
        # only suggest companies that post roles you'd actually want
        if not filters.title_ok(p.title, fcfg).passes:
            continue
        det = detect_from_text(p.url)
        db.conn.execute(
            "INSERT INTO suggestions(company_key, name, ats_type, board_token, sample_url, source, first_seen_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(company_key) DO UPDATE SET seen_count=suggestions.seen_count+1, "
            "ats_type=COALESCE(suggestions.ats_type, excluded.ats_type), "
            "board_token=COALESCE(suggestions.board_token, excluded.board_token)",
            (p.company_key, p.company, det.ats_type if det else None, det.board_token if det else None,
             p.url, cs.source, now))
    db.commit()


__all__ = ["CommunitySource", "parse_tables", "fetch_source", "ingest_community"]
