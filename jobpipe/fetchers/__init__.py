"""Fetcher registry: ats_type -> fetch function. Add a new ATS by adding one entry here."""
from __future__ import annotations

from . import amazon, ashby, bigtech, eightfold, greenhouse, lever, smartrecruiters, workday
from .base import FetchContext, FetcherDisabled, FetcherError, FetchFn

REGISTRY: dict[str, FetchFn] = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "smartrecruiters": smartrecruiters.fetch,
    "workday": workday.fetch,
    "amazon": amazon.fetch,
    "eightfold": eightfold.fetch,
    "microsoft": eightfold.fetch,
    "google": bigtech.make("google"),
    "meta": bigtech.make("meta"),
    "apple": bigtech.make("apple"),
}

__all__ = ["REGISTRY", "FetchContext", "FetcherError", "FetcherDisabled"]
