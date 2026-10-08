from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..http import Http
from ..models import Company, FetchResult


class FetcherError(Exception):
    pass


class FetcherDisabled(FetcherError):
    """Raised by fetchers that are intentionally not implemented / gated (e.g. ToS)."""


@dataclass
class FetchContext:
    # external ids already in the DB for this board: lets fetchers skip per-posting detail calls
    known_ids: set[str] = field(default_factory=set)
    # cheap title prefilter: detail calls are only made for titles that could pass the filters
    title_ok: Callable[[str], bool] = lambda t: True
    max_pages: int = 50


FetchFn = Callable[[Company, Http, FetchContext], FetchResult]
