"""`ats_type: auto`: try candidate boards in order and lock in the first that returns postings.

For companies whose ATS isn't confirmed (e.g. Shopify):
  - {name: Shopify, tier: 1, ats_type: auto,
     candidates: ["ashby:shopify", "lever:shopify", "greenhouse:shopify", "smartrecruiters:Shopify"]}

The pipeline stores the winner (kv `resolved:<company>`) and uses it directly on later runs. If
the resolved board then fails 3 runs in a row, the resolution is cleared and probing starts again.
"""
from __future__ import annotations

import dataclasses

from ..http import Http, HttpError
from ..models import Company, FetchResult
from .base import FetchContext, FetcherDisabled, FetcherError


def parse_candidate(c, base: Company) -> Company:
    if isinstance(c, str):
        ats, _, token = c.partition(":")
        extra = {}
    else:
        ats, token = c["ats_type"], c.get("board_token")
        extra = {k: v for k, v in c.items() if k not in ("ats_type", "board_token")}
    opts = {k: v for k, v in base.options.items() if k != "candidates"} | extra
    return dataclasses.replace(base, ats_type=ats.lower(), board_token=token or None, options=opts)


def candidates(company: Company) -> list[Company]:
    return [parse_candidate(c, company) for c in company.options.get("candidates") or []]


def fetch(company: Company, http: Http, ctx: FetchContext) -> FetchResult:
    from . import REGISTRY
    errors = []
    for cand in candidates(company):
        fn = REGISTRY.get(cand.ats_type)
        if fn is None or cand.ats_type == "auto":
            continue
        try:
            res = fn(cand, http, ctx)
        except (HttpError, FetcherError, FetcherDisabled) as e:
            errors.append(f"{cand.ats_type}:{cand.board_token} -> {type(e).__name__}: {str(e)[:80]}")
            continue
        if res.postings:
            res.resolved = cand
            return res
        errors.append(f"{cand.ats_type}:{cand.board_token} -> 0 postings")
    raise FetcherError("no candidate board worked: " + "; ".join(errors))
