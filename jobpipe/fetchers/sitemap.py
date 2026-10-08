"""Generic sitemap fetcher for companies that host their own careers site (e.g. Shopify).

Sitemaps are published for crawlers, but this still goes through jobpipe.robots (`tos_ok: true`
+ robots.txt). Job pages are found by URL pattern; the title comes from the URL slug and
posted_at from <lastmod>. There's no description, so these score on title/location only. The
posting appears in the sheet and alerts, and you open the link for details.

companies.yaml:
  - {name: Shopify, tier: 1, ats_type: sitemap, tos_ok: true,
     sitemap_url: "https://www.shopify.com/sitemap.xml",            # optional: else robots.txt Sitemap:
     url_pattern: "/careers/[a-z0-9-]+_[0-9a-f-]{36}"}
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from ..http import Http, HttpError
from ..models import Company, FetchResult, Posting
from ..normalize import to_iso
from ..robots import require_allowed, sitemaps
from .base import FetchContext, FetcherError

_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>(?:\s*<lastmod>\s*(.*?)\s*</lastmod>)?", re.S | re.I)
MAX_SITEMAPS = 25


def title_from_slug(url: str) -> str:
    slug = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    slug = re.sub(r"[_-]?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", "", slug, flags=re.I)
    slug = re.sub(r"[_-]\d{4,}$", "", slug)
    words = re.split(r"[-_]+", slug)
    small = {"and", "or", "of", "the", "in", "on", "for", "to", "a", "at"}
    return " ".join(w if w.isupper() else (w if w in small else w.capitalize()) for w in words if w)


def fetch(company: Company, http: Http, ctx: FetchContext) -> FetchResult:
    pattern = re.compile(company.options.get("url_pattern") or r"/(careers|jobs)/", re.I)
    roots = [company.options["sitemap_url"]] if company.options.get("sitemap_url") else []
    if not roots:
        base = company.careers_url or company.board_token or ""
        u = urlparse(base)
        if not u.netloc:
            raise FetcherError("sitemap fetcher needs sitemap_url or careers_url")
        roots = sitemaps(http, f"{u.scheme}://{u.netloc}") or [f"{u.scheme}://{u.netloc}/sitemap.xml"]
    require_allowed(company, http, *roots)

    out, queue, seen_maps, seen_urls = [], list(roots), set(), set()
    child_hint = re.compile(company.options.get("child_sitemap_pattern") or r"career|job", re.I)
    while queue and len(seen_maps) < MAX_SITEMAPS:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        try:
            xml = http.get(sm, expect_json=False, conditional=False).text
        except HttpError:
            if sm in roots:
                raise
            continue
        entries = [(loc.replace("&amp;", "&"), lastmod) for loc, lastmod in _LOC.findall(xml)]
        if "<sitemapindex" in xml[:500].lower():
            # follow child sitemaps that look career-related; if none do, follow them all (capped)
            children = [loc for loc, _ in entries]
            queue += [c for c in children if child_hint.search(c)] or children
            continue
        for loc, lastmod in entries:
            if pattern.search(urlparse(loc).path) and loc not in seen_urls:
                seen_urls.add(loc)
                out.append(Posting(
                    company=company.name, company_key=company.key, source="sitemap",
                    external_id=urlparse(loc).path, title=title_from_slug(loc), url=loc,
                    location_raw=company.options.get("default_location", ""),
                    posted_at=to_iso(lastmod) if lastmod else None, tier=company.tier,
                ))
    return FetchResult(out, complete=len(seen_maps) < MAX_SITEMAPS)
