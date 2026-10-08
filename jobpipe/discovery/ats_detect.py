"""Detect a company's ATS from a careers URL (or an apply link) and verify the board token.

    python -m jobpipe detect https://www.example.com/careers
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from ..http import Http, HttpError
from ..models import slugify


@dataclass
class Detection:
    ats_type: str
    board_token: str | None
    evidence: str
    verified: bool = False
    job_count: int | None = None


URL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("lever", re.compile(r"api\.(?:eu\.)?lever\.co/v0/postings/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.%-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)", re.I)),
    ("workday", re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[a-z0-9-]+/)?(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)),
    ("icims", re.compile(r"([a-z0-9-]+)\.icims\.com", re.I)),
    ("amazon", re.compile(r"amazon\.jobs", re.I)),
    ("microsoft", re.compile(r"(?:apply\.)?careers\.microsoft\.com|jobs\.careers\.microsoft\.com", re.I)),
    ("google", re.compile(r"google\.com/about/careers|careers\.google\.com", re.I)),
    ("meta", re.compile(r"metacareers\.com", re.I)),
    ("apple", re.compile(r"jobs\.apple\.com", re.I)),
    ("atlassian", re.compile(r"atlassian\.com/company/careers", re.I)),
]
BIG_TOKENS = {"atlassian": None, "amazon": None, "microsoft": "apply.careers.microsoft.com|microsoft.com", "google": None,
              "meta": None, "apple": None}
IGNORED_FIRST_SEGMENTS = {"embed", "v1", "jobs", "job", "en-us", "api"}


def detect_from_text(text: str) -> Detection | None:
    """Match the first ATS reference in a URL or HTML page."""
    for ats, rx in URL_PATTERNS:
        m = rx.search(text or "")
        if not m:
            continue
        if ats == "workday":
            tenant, wd, site = m.group(1), m.group(2), m.group(3)
            if site.lower() in ("wday", "job", "jobs"):
                continue
            return Detection("workday", f"{tenant}/{wd}/{site}", m.group(0))
        if ats in BIG_TOKENS:
            return Detection(ats, BIG_TOKENS[ats], m.group(0))
        token = m.group(1)
        if token.lower() in IGNORED_FIRST_SEGMENTS:
            continue
        return Detection(ats, token, m.group(0))
    # Greenhouse-hosted job on a company domain: ?gh_jid=123 tells us the ATS but not the token
    q = parse_qs(urlparse(text).query) if text and text.startswith("http") else {}
    if "gh_jid" in q:
        return Detection("greenhouse", None, "gh_jid")
    return None


VERIFY_URLS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{t}/jobs", lambda d: len(d.get("jobs", []))),
    "lever": ("https://api.lever.co/v0/postings/{t}?mode=json", lambda d: len(d) if isinstance(d, list) else 0),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{t}", lambda d: len(d.get("jobs", []))),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=1",
                        lambda d: int(d.get("totalFound", 0))),
}


def verify(det: Detection, http: Http) -> Detection:
    if det.ats_type == "workday" and det.board_token:
        tenant, wd, site = det.board_token.split("/")
        try:
            r = http.post(f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                          json_body={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
            det.verified, det.job_count = True, int(r.data.get("total") or 0)
        except HttpError:
            det.verified = False
        return det
    spec = VERIFY_URLS.get(det.ats_type)
    if not spec or not det.board_token:
        return det
    url, count = spec
    try:
        r = http.get(url.format(t=det.board_token))
        det.verified, det.job_count = True, count(r.data)
    except HttpError:
        det.verified = False
    return det


def slug_candidates(name: str) -> list[str]:
    base = re.sub(r"\b(inc|llc|ltd|corp|corporation|technologies|technology|labs|hq|co)\b\.?", "", name, flags=re.I)
    s1 = slugify(base).replace("-", "")
    s2 = slugify(base)
    s3 = slugify(name).replace("-", "")
    return list(dict.fromkeys(x for x in (s1, s2, s3) if x))


def probe(name: str, http: Http) -> Detection | None:
    """Try the common public ATS APIs with slug guesses; return the board with the most jobs."""
    best = None
    for slug in slug_candidates(name):
        for ats in ("greenhouse", "lever", "ashby"):
            d = verify(Detection(ats, slug, f"probe:{ats}/{slug}"), http)
            if d.verified and (d.job_count or 0) > 0 and (best is None or d.job_count > best.job_count):
                best = d
    return best


def detect(careers_url: str, http: Http, name: str | None = None) -> Detection | None:
    det = detect_from_text(careers_url)
    if det is None or det.board_token is None and det.ats_type not in BIG_TOKENS:
        try:
            page = http.get(careers_url, expect_json=False).text
            det = detect_from_text(page) or det
        except HttpError:
            pass
    if det is not None and det.board_token:
        det = verify(det, http)
        if det.verified:
            return det
    if name:
        probed = probe(name, http)
        if probed:
            return probed
    return det
