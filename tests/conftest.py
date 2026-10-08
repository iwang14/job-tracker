from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from jobpipe.config import Settings, load_settings
from jobpipe.db import DB
from jobpipe.http import Http, HttpError, Response

FIX = Path(__file__).parent / "fixtures"
CONFIG = Path(__file__).resolve().parent.parent / "config"


def load_fixture(name: str):
    p = FIX / name
    return json.loads(p.read_text()) if p.suffix == ".json" else p.read_text()


class FakeHttp(Http):
    """Serves fixtures by (method, url regex). Unmatched requests raise 404 like a missing board."""

    def __init__(self, routes: dict | None = None):
        super().__init__(min_interval=0)
        self.routes: list[tuple[str, re.Pattern, object]] = []
        self.calls: list[tuple[str, str, dict | None, object]] = []
        for k, v in (routes or {}).items():
            self.add(k, v)

    def add(self, key: str, payload) -> "FakeHttp":
        method, _, pattern = key.partition(" ")
        self.routes.append((method, re.compile(pattern), payload))
        return self

    def scoped(self):
        return self

    def request(self, method, url, *, params=None, json_body=None, headers=None, conditional=False,
                expect_json=True) -> Response:
        self.calls.append((method, url, params, json_body))
        for m, rx, payload in self.routes:
            if m == method and rx.search(url):
                if callable(payload):
                    payload = payload(url, params, json_body)
                if isinstance(payload, Response):
                    return payload
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, str) and (payload.endswith(".json") or payload.endswith(".md")):
                    payload = load_fixture(payload)
                if isinstance(payload, str):
                    return Response(200, text=payload)
                return Response(200, data=payload, text=json.dumps(payload))
        raise HttpError(404, url, "no fake route")


@pytest.fixture(autouse=True)
def _fresh_robots_cache():
    from jobpipe import robots
    robots.clear_cache()
    yield


@pytest.fixture
def settings() -> Settings:
    return load_settings(CONFIG / "settings.yaml", CONFIG / "profile.yaml")


@pytest.fixture
def db(tmp_path) -> DB:
    d = DB(tmp_path / "state.db")
    yield d
    d.close()


# ------------------------------------------------------------------ fake gspread
class WorksheetNotFound(Exception):
    pass


def _a1_to_rc(a1: str) -> tuple[int, int]:
    m = re.fullmatch(r"([A-Z]+)(\d+)", a1)
    col = 0
    for ch in m.group(1):
        col = col * 26 + (ord(ch) - 64)
    return int(m.group(2)), col


class FakeWorksheet:
    _next_id = 1

    def __init__(self, title: str):
        self.title = title
        self.id = FakeWorksheet._next_id
        FakeWorksheet._next_id += 1
        self.rows: list[list[str]] = []

    def get_all_values(self):
        width = max((len(r) for r in self.rows), default=0)
        return [[str(c) for c in r] + [""] * (width - len(r)) for r in self.rows]

    def _set(self, row: int, col: int, value):
        while len(self.rows) < row:
            self.rows.append([])
        r = self.rows[row - 1]
        while len(r) < col:
            r.append("")
        r[col - 1] = "" if value is None else str(value)

    def update(self, range_name=None, values=None, **kw):
        r0, c0 = _a1_to_rc(range_name)
        for i, row in enumerate(values):
            for j, v in enumerate(row):
                self._set(r0 + i, c0 + j, v)

    def batch_update(self, data, **kw):
        for d in data:
            self.update(range_name=d["range"], values=d["values"])

    def insert_rows(self, values, row=1, **kw):
        for i, v in enumerate(values):
            self.rows.insert(row - 1 + i, [("" if x is None else str(x)) for x in v])

    def append_rows(self, values, **kw):
        for v in values:
            self.rows.append([("" if x is None else str(x)) for x in v])

    def delete_rows(self, start, end=None):
        end = end or start
        del self.rows[start - 1:end]

    def clear(self):
        self.rows = []


class FakeSpreadsheet:
    def __init__(self):
        self.sheets: dict[str, FakeWorksheet] = {}
        self.batch_requests: list = []

    def worksheet(self, title):
        if title not in self.sheets:
            raise WorksheetNotFound(title)
        return self.sheets[title]

    def add_worksheet(self, title, rows=100, cols=20):
        self.sheets[title] = FakeWorksheet(title)
        return self.sheets[title]

    def batch_update(self, body):
        self.batch_requests.append(body)


@pytest.fixture
def spreadsheet():
    return FakeSpreadsheet()
