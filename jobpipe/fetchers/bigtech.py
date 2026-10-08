"""Big-company custom sites that do NOT get a scraper.

Google, Meta and Apple don't publish a job API. Their career sites are rendered from internal
endpoints (Meta: GraphQL with rotating doc ids/tokens; Google: server-rendered HTML; Apple:
CSRF-protected search API) and their terms restrict automated collection. Per the "no ToS
violations" rule these fetchers intentionally raise FetcherDisabled.

ToS-clean coverage for these companies comes from:
  * community GitHub lists (config/settings.yaml -> community_sources), which carry Google/Meta/
    Apple new-grad postings with direct links, and
  * the companies' own job-alert emails (set those up manually on each careers site).
"""
from __future__ import annotations

from ..http import Http
from ..models import Company, FetchResult
from .base import FetchContext, FetcherDisabled

REASONS = {
    "google": "Google Careers has no public API; HTML scraping isn't permitted by Google's ToS.",
    "meta": "Meta Careers uses a private GraphQL API; Meta's terms prohibit automated collection.",
    "apple": "Apple Jobs uses a private CSRF-protected API; no documented public endpoint.",
}


def make(name: str):
    def fetch(company: Company, http: Http, ctx: FetchContext) -> FetchResult:
        raise FetcherDisabled(REASONS[name] + " Covered via community lists instead.")
    return fetch
