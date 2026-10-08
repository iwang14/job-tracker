"""SQLite persistence.

The database file is restored from / saved to the `state` branch by the GitHub workflow
(see scripts/state.sh). Everything here is plain sqlite3 so the file is portable and can be
inspected locally with `sqlite3 state.db`.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import Location, Posting

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    company_key TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    tier INTEGER NOT NULL DEFAULT 3,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    location_raw TEXT NOT NULL DEFAULT '',
    locations_json TEXT NOT NULL DEFAULT '[]',
    level_guess TEXT,
    country TEXT,
    city TEXT,
    work_mode TEXT,
    remote_scope TEXT,
    posted_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    salary TEXT,
    work_authorization_note TEXT,
    yoe_min INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    missed_runs INTEGER NOT NULL DEFAULT 0,
    closed_at TEXT,
    content_hash TEXT,
    duplicate_of TEXT,
    repost_of TEXT,
    passes_filters INTEGER NOT NULL DEFAULT 0,
    filter_reason TEXT,
    score INTEGER,
    score_breakdown TEXT,
    fit_summary TEXT,
    alerted_at TEXT,
    bootstrap INTEGER NOT NULL DEFAULT 0,
    user_status TEXT,
    UNIQUE (company_key, source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_postings_board ON postings (company_key, source, status);
CREATE INDEX IF NOT EXISTS idx_postings_first_seen ON postings (first_seen_at);

CREATE TABLE IF NOT EXISTS llm_cache (
    posting_id TEXT PRIMARY KEY,
    content_hash TEXT,
    model TEXT,
    fit INTEGER,
    summary TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS http_cache (
    url TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS fetcher_health (
    fetcher_key TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_ok_at TEXT,
    last_run_at TEXT,
    alerted INTEGER NOT NULL DEFAULT 0,
    total_ok_runs INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    mode TEXT,
    stats TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Applications we have copied from the Postings tab and the last stage we saw for each,
-- so stage transitions (prep checklist, first-response date) fire exactly once.
CREATE TABLE IF NOT EXISTS app_state (
    app_key TEXT PRIMARY KEY,
    posting_id TEXT,
    stage TEXT,
    copied_at TEXT,
    stage_changed_at TEXT,
    reached_callback INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS prep_items (
    app_key TEXT,
    stage TEXT,
    created_at TEXT,
    PRIMARY KEY (app_key, stage)
);

-- Companies found in community lists that aren't in companies.yaml yet (for review/approval).
CREATE TABLE IF NOT EXISTS suggestions (
    company_key TEXT PRIMARY KEY,
    name TEXT,
    ats_type TEXT,
    board_token TEXT,
    sample_url TEXT,
    source TEXT,
    seen_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS reminders_sent (
    app_key TEXT,
    kind TEXT,
    sent_on TEXT,
    PRIMARY KEY (app_key, kind, sent_on)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: str | Path = "state.db"):
        self.path = str(path)
        # check_same_thread=False: fetch threads read ETag validators; access is serialized by Http._cache_lock.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    # ------------------------------------------------------------------ kv
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------ http cache
    def get_validators(self, url: str) -> tuple[str | None, str | None]:
        row = self.conn.execute("SELECT etag, last_modified FROM http_cache WHERE url=?", (url,)).fetchone()
        return (row["etag"], row["last_modified"]) if row else (None, None)

    def set_validators(self, url: str, etag: str | None, last_modified: str | None) -> None:
        if not etag and not last_modified:
            return
        self.conn.execute(
            "INSERT INTO http_cache(url, etag, last_modified, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, last_modified=excluded.last_modified, "
            "updated_at=excluded.updated_at",
            (url, etag, last_modified, utcnow()),
        )

    def clear_validators(self, url: str) -> None:
        self.conn.execute("DELETE FROM http_cache WHERE url=?", (url,))

    # -------------------------------------------------------------- postings
    def get(self, pid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM postings WHERE id=?", (pid,)).fetchone()

    def known_external_ids(self, company_key: str, source: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT external_id FROM postings WHERE company_key=? AND source=?", (company_key, source)
        )
        return {r["external_id"] for r in rows}

    def board_has_history(self, company_key: str, source: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM postings WHERE company_key=? AND source=? LIMIT 1", (company_key, source)
        ).fetchone()
        return row is not None

    def candidates_for_dedupe(self, company_key: str, exclude_id: str, since: str) -> list[sqlite3.Row]:
        """Postings at the same company that a new posting could be a duplicate/repost of."""
        return list(self.conn.execute(
            "SELECT id, title, locations_json, url, status, source, duplicate_of FROM postings "
            "WHERE company_key=? AND id<>? AND (status='open' OR closed_at>=?)",
            (company_key, exclude_id, since),
        ))

    def insert_posting(self, p: Posting, now: str, **extra) -> None:
        cols = {
            "id": p.id, "company": p.company, "company_key": p.company_key, "source": p.source,
            "external_id": p.external_id, "tier": p.tier, "title": p.title, "url": p.url,
            "description": p.description, "location_raw": p.location_raw,
            "locations_json": json.dumps([asdict(loc) for loc in p.locations]),
            "level_guess": p.level_guess, "country": p.country, "city": p.city,
            "work_mode": p.work_mode, "remote_scope": p.remote_scope, "posted_at": p.posted_at,
            "first_seen_at": now, "last_seen_at": now, "salary": p.salary,
            "work_authorization_note": p.work_authorization_note, "yoe_min": p.yoe_min,
            "status": "open", "content_hash": p.content_hash,
        }
        cols.update(extra)
        names = ",".join(cols)
        qs = ",".join("?" * len(cols))
        self.conn.execute(f"INSERT INTO postings ({names}) VALUES ({qs})", tuple(cols.values()))

    def update_posting(self, pid: str, **fields) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE postings SET {sets} WHERE id=?", (*fields.values(), pid))

    def refresh_posting(self, p: Posting, now: str, **extra) -> None:
        """A known posting was seen again with changed content: refresh the derived fields."""
        self.update_posting(
            p.id, title=p.title, url=p.url, description=p.description, location_raw=p.location_raw,
            locations_json=json.dumps([asdict(loc) for loc in p.locations]), level_guess=p.level_guess,
            country=p.country, city=p.city, work_mode=p.work_mode, remote_scope=p.remote_scope,
            salary=p.salary, work_authorization_note=p.work_authorization_note, yoe_min=p.yoe_min,
            last_seen_at=now, missed_runs=0, status="open", closed_at=None, tier=p.tier,
            **{"content_hash": p.content_hash, **extra},
        )

    def touch(self, ids: Iterable[str], now: str) -> None:
        self.conn.executemany(
            "UPDATE postings SET last_seen_at=?, missed_runs=0, status='open', closed_at=NULL WHERE id=?",
            [(now, i) for i in ids],
        )

    def touch_board(self, company_key: str, source: str, now: str) -> None:
        self.conn.execute(
            "UPDATE postings SET last_seen_at=?, missed_runs=0 WHERE company_key=? AND source=? AND status='open'",
            (now, company_key, source),
        )

    def touch_source(self, source: str, now: str) -> None:
        self.conn.execute("UPDATE postings SET last_seen_at=?, missed_runs=0 WHERE source=? AND status='open'",
                          (now, source))

    def mark_missing_source(self, source: str, seen_ids: set[str], now: str, close_after: int = 2) -> list[str]:
        keys = {r["company_key"] for r in self.conn.execute(
            "SELECT DISTINCT company_key FROM postings WHERE source=? AND status='open'", (source,))}
        closed = []
        for k in keys:
            closed += self.mark_missing(k, source, seen_ids, now, close_after)
        return closed

    def mark_missing(self, company_key: str, source: str, seen_ids: set[str], now: str,
                     close_after: int = 2) -> list[str]:
        """Increment missed_runs for open postings not seen; close those that hit the threshold."""
        rows = self.conn.execute(
            "SELECT id, missed_runs FROM postings WHERE company_key=? AND source=? AND status='open'",
            (company_key, source),
        ).fetchall()
        closed = []
        for r in rows:
            if r["id"] in seen_ids:
                continue
            missed = r["missed_runs"] + 1
            if missed >= close_after:
                self.conn.execute(
                    "UPDATE postings SET missed_runs=?, status='closed', closed_at=? WHERE id=?",
                    (missed, now, r["id"]),
                )
                closed.append(r["id"])
            else:
                self.conn.execute("UPDATE postings SET missed_runs=? WHERE id=?", (missed, r["id"]))
        return closed

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    # ----------------------------------------------------------- llm cache
    def llm_get(self, pid: str, content_hash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM llm_cache WHERE posting_id=? AND content_hash=?", (pid, content_hash)
        ).fetchone()

    def llm_put(self, pid: str, content_hash: str, model: str, fit: int, summary: str) -> None:
        self.conn.execute(
            "INSERT INTO llm_cache(posting_id, content_hash, model, fit, summary, created_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(posting_id) DO UPDATE SET content_hash=excluded.content_hash, model=excluded.model, "
            "fit=excluded.fit, summary=excluded.summary, created_at=excluded.created_at",
            (pid, content_hash, model, fit, summary, utcnow()),
        )

    # -------------------------------------------------------- fetcher health
    def record_fetch(self, fetcher_key: str, ok: bool, error: str | None, now: str) -> sqlite3.Row:
        if ok:
            self.conn.execute(
                "INSERT INTO fetcher_health(fetcher_key, consecutive_failures, last_ok_at, last_run_at, total_ok_runs) "
                "VALUES(?, 0, ?, ?, 1) ON CONFLICT(fetcher_key) DO UPDATE SET consecutive_failures=0, "
                "last_ok_at=excluded.last_ok_at, last_run_at=excluded.last_run_at, "
                "total_ok_runs=fetcher_health.total_ok_runs+1",
                (fetcher_key, now, now),
            )
        else:
            self.conn.execute(
                "INSERT INTO fetcher_health(fetcher_key, consecutive_failures, last_error, last_run_at) "
                "VALUES(?, 1, ?, ?) ON CONFLICT(fetcher_key) DO UPDATE SET "
                "consecutive_failures=fetcher_health.consecutive_failures+1, last_error=excluded.last_error, "
                "last_run_at=excluded.last_run_at",
                (fetcher_key, (error or "")[:500], now),
            )
        return self.conn.execute("SELECT * FROM fetcher_health WHERE fetcher_key=?", (fetcher_key,)).fetchone()

    def set_health_alerted(self, fetcher_key: str, alerted: bool) -> None:
        self.conn.execute("UPDATE fetcher_health SET alerted=? WHERE fetcher_key=?", (int(alerted), fetcher_key))

    # ----------------------------------------------------------------- runs
    def start_run(self, mode: str) -> int:
        cur = self.conn.execute("INSERT INTO runs(started_at, mode) VALUES(?, ?)", (utcnow(), mode))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: dict) -> None:
        self.conn.execute("UPDATE runs SET finished_at=?, stats=? WHERE id=?", (utcnow(), json.dumps(stats), run_id))
        self.conn.commit()


def row_locations(row: sqlite3.Row) -> list[Location]:
    return [Location(**d) for d in json.loads(row["locations_json"] or "[]")]
