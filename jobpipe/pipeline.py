"""One pipeline run: fetch -> normalize -> dedupe -> filter -> score -> alert -> sheet."""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import alerts, dedupe, filters, llm, normalize, scoring, sheets
from .config import Settings
from .db import DB, row_locations
from .fetchers import REGISTRY, FetchContext, FetcherDisabled
from .http import Http
from .models import Company, FetchResult
from .notify import Notifier, SafeNotifier

log = logging.getLogger(__name__)


@dataclass
class RunStats:
    fetched: int = 0
    new: int = 0
    new_matching: int = 0
    updated: int = 0
    closed: int = 0
    duplicates: int = 0
    llm_calls: int = 0
    alerts: int = 0
    enriched: int = 0
    failed_fetchers: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


def fetch_one(company: Company, http: Http, ctx: FetchContext) -> FetchResult:
    fn = REGISTRY.get(company.ats_type)
    if fn is None:
        raise FetcherDisabled(f"no fetcher for ats_type {company.ats_type!r}")
    return fn(company, http, ctx)


def fetch_all(companies: list[Company], db: DB, http: Http, settings: Settings,
              extra_jobs: list | None = None) -> list[tuple[Company, Http, FetchResult | None, Exception | None]]:
    fcfg = settings.section("filters")
    title_check = lambda t: filters.title_ok(t, fcfg).passes
    jobs = []
    for c in companies:
        ctx = FetchContext(known_ids=db.known_external_ids(c.key, c.source), title_ok=title_check,
                           max_pages=int(settings.section("fetch").get("max_pages", 50)))
        jobs.append((c, http.scoped(), ctx))
    out = []
    workers = int(settings.section("fetch").get("workers", 8))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_one, c, h, ctx): (c, h) for c, h, ctx in jobs}
        for source, fn in extra_jobs or []:
            h = http.scoped()
            futs[pool.submit(fn, h)] = (source, h)
        for fut in as_completed(futs):
            c, h = futs[fut]
            try:
                out.append((c, h, fut.result(), None))
            except Exception as e:  # noqa: BLE001 - isolation: one broken board never breaks the run
                (log.debug if isinstance(e, FetcherDisabled) else log.warning)(
                    "fetch failed for %s: %s", getattr(c, "name", c), e)
                out.append((c, h, None, e))
    return out


def ingest(db: DB, company: Company | None, result: FetchResult, settings: Settings, now: str,
           stats: RunStats, source: str | None = None, bootstrap_all: bool | None = None) -> None:
    """Upsert one board's postings, dedupe new ones, apply filters, close missing ones."""
    fcfg = settings.section("filters")
    boards: dict[tuple[str, str], set[str]] = {}
    bootstrap_boards = set()
    for p in result.postings:
        bk = (p.company_key, p.source)
        if bk not in boards:
            boards[bk] = set()
            first = bootstrap_all if bootstrap_all is not None else not db.board_has_history(*bk)
            if first:
                bootstrap_boards.add(bk)
        boards[bk].add(p.id)

    for p in result.postings:
        stats.fetched += 1
        existing = db.get(p.id)
        # e.g. SmartRecruiters/Workday skip detail calls for known postings; community lists never carry the
        # real description (it may have been filled in by discovery.enrich), so they only compare title/location
        list_only = not p.description or p.source.startswith("community:")
        if existing is not None and list_only:
            p.description = existing["description"]
        normalize.enrich(p)
        fr = filters.evaluate(p, fcfg)
        if existing is not None:
            if list_only:
                changed = (existing["title"], existing["location_raw"]) != (p.title, p.location_raw)
            else:
                changed = existing["content_hash"] != p.content_hash
        content_hash = existing["content_hash"] if existing is not None and list_only else p.content_hash
        if existing is None:
            extra = {"passes_filters": int(fr.passes), "filter_reason": fr.reason,
                     "bootstrap": int((p.company_key, p.source) in bootstrap_boards)}
            match = dedupe.find_match(db, p, now)
            if match:
                kind, canonical = match
                stats.duplicates += 1
                if kind == "duplicate":
                    can_row = db.get(canonical)
                    if p.source.startswith("community") or (can_row and not can_row["source"].startswith("community")):
                        extra["duplicate_of"] = canonical
                    else:
                        # ATS posting supersedes a community-list copy we saw first
                        db.update_posting(canonical, duplicate_of=p.id)
                        if can_row and can_row["alerted_at"]:
                            extra["alerted_at"] = can_row["alerted_at"]
                else:
                    extra["repost_of"] = canonical
            if not fr.passes:
                p.description = ""  # keep the state file small: only matching postings keep their text
            db.insert_posting(p, now, content_hash=content_hash, **extra)
            stats.new += 1
            if fr.passes and "duplicate_of" not in extra:
                stats.new_matching += 1
        elif changed or existing["status"] != "open":
            if not fr.passes:
                p.description = ""
            db.refresh_posting(p, now, passes_filters=int(fr.passes), filter_reason=fr.reason, content_hash=content_hash)
            if changed and fr.passes:
                db.update_posting(p.id, score=None)  # rescore
            stats.updated += 1
        else:
            db.touch([p.id], now)

    if result.complete and company is not None:
        src = source or company.source
        seen = boards.get((company.key, src), set())
        close_after = int(settings.section("fetch").get("close_after_missed_runs", 2))
        stats.closed += len(db.mark_missing(company.key, src, seen, now, close_after))
    db.commit()


def score_pending(db: DB, settings: Settings, stats: RunStats, now: datetime) -> None:
    profile = settings.get("profile") or {}
    sc = settings.section("scoring")
    use_llm = llm.available() and sc.get("llm_enabled", True)
    budget = int(sc.get("llm_max_calls_per_run", 40))
    rows = db.query("SELECT * FROM postings WHERE passes_filters=1 AND duplicate_of IS NULL AND status='open' "
                    "AND score IS NULL ORDER BY tier, first_seen_at DESC")
    for r in rows:
        fit, summary = None, None
        if use_llm:
            cached = db.llm_get(r["id"], r["content_hash"])
            if cached:
                fit, summary = cached["fit"], cached["summary"]
            elif stats.llm_calls < budget and len(r["description"] or "") >= 120:
                got = llm.semantic_fit(r["title"], r["company"], r["description"], profile, sc.get("llm_model"))
                stats.llm_calls += 1
                if got:
                    fit, summary = got
                    db.llm_put(r["id"], r["content_hash"], sc.get("llm_model") or llm.DEFAULT_MODEL, fit, summary)
            elif stats.llm_calls >= budget:
                continue  # leave unscored; next run picks it up (keeps scoring consistent)
        s = scoring.score_posting(
            title=r["title"], description=r["description"], locations=row_locations(r), work_mode=r["work_mode"],
            remote_scope=r["remote_scope"], tier=r["tier"], posted_at=r["posted_at"],
            first_seen_at=r["first_seen_at"], settings=settings.raw, profile=profile, llm_fit=fit, now=now)
        if summary is None:
            hits = s.breakdown["skill_hits"]
            summary = ("Matches: " + ", ".join(hits[:6])) if hits else "Few profile keywords found"
            if r["yoe_min"] is not None:
                summary += f" | asks {r['yoe_min']}+ yrs"
        db.update_posting(r["id"], score=s.total, score_breakdown=json.dumps(s.breakdown), fit_summary=summary)
    db.commit()


def resolve_auto(db: DB, c: Company) -> Company:
    """ats_type auto: swap in the board resolved on an earlier run, if any."""
    if c.ats_type != "auto":
        return c
    from .fetchers.auto import parse_candidate
    c.options["_health_key"] = c.fetcher_key
    saved = db.kv_get(f"resolved:{c.key}")
    if not saved:
        return c
    eff = parse_candidate(json.loads(saved), c)
    eff.options["_health_key"] = c.fetcher_key
    return eff


def health_key(c) -> str:
    if isinstance(c, Company):
        return c.options.get("_health_key") or c.fetcher_key
    return f"community:{c.name}"


def run(db: DB, companies: list[Company], settings: Settings, notifier: Notifier, *, tiers: set[int] | None = None,
        sheet=None, http: Http | None = None, community: bool = True, now_fn=None) -> RunStats:
    now_fn = now_fn or (lambda: datetime.now(timezone.utc).replace(microsecond=0))
    stats = RunStats()
    run_id = db.start_run("tiers=" + ",".join(map(str, sorted(tiers))) if tiers else "all")
    notifier = notifier if isinstance(notifier, SafeNotifier) else SafeNotifier(notifier)
    http = http or Http(cache=db, min_interval=float(settings.section("fetch").get("per_host_interval", 0.5)))
    active = [resolve_auto(db, c) for c in companies if c.enabled and (not tiers or c.tier in tiers)]

    t0 = time.monotonic()
    extra = []
    if community and not tiers:
        from .discovery import community as comm
        for src in settings.get("community_sources", []) or []:
            extra.append((comm.CommunitySource(src), (lambda h, s=src: comm.fetch_source(s, h, companies))))
        from .discovery import email_alerts
        ecfg = settings.section("email_alerts")
        if email_alerts.configured(ecfg):
            extra.append((email_alerts.EmailSource(ecfg), (lambda h: email_alerts.fetch(ecfg, companies))))
    results = fetch_all(active, db, http, settings, extra_jobs=extra)
    stats.timings["fetch"] = round(time.monotonic() - t0, 1)

    now = now_fn().isoformat()
    for company, scoped_http, result, err in results:
        key = health_key(company)
        if err is not None:
            if isinstance(err, FetcherDisabled):
                continue
            row = db.record_fetch(key, False, f"{type(err).__name__}: {err}", now)
            stats.failed_fetchers.append(key)
            if key.startswith("auto:") and row["consecutive_failures"] >= 3:
                db.conn.execute("DELETE FROM kv WHERE key=?", (f"resolved:{company.key}",))  # re-probe
            continue
        if result.resolved is not None:
            r = result.resolved
            db.kv_set(f"resolved:{r.key}", json.dumps({"ats_type": r.ats_type, "board_token": r.board_token}))
            log.info("%s resolved to %s:%s", r.name, r.ats_type, r.board_token)
            r.options["_health_key"] = key
            company = r
        if result.unchanged:
            if isinstance(company, Company):
                db.touch_board(company.key, company.source, now)
            else:
                db.touch_source(company.source, now)
        elif isinstance(company, Company):
            prior = db.query("SELECT total_ok_runs FROM fetcher_health WHERE fetcher_key=?", (key,))
            # first successful fetch of a board = backfill: goes to digest/sheet, not instant alerts
            ingest(db, company, result, settings, now, stats,
                   bootstrap_all=not (prior and prior[0]["total_ok_runs"] > 0))
        else:
            company.ingest(db, result, settings, now, stats)  # community list / email source
        if scoped_http is not None:
            scoped_http.commit_validators()
        db.record_fetch(key, True, None, now)
    db.commit()

    if community and not tiers:
        from .discovery.enrich import enrich_community
        stats.enriched = enrich_community(db, http, settings)

    t1 = time.monotonic()
    score_pending(db, settings, stats, now_fn())
    stats.timings["score"] = round(time.monotonic() - t1, 1)

    acfg = settings.section("alerts")
    stats.alerts = alerts.send_instant(db, notifier, acfg, now_fn())
    alerts.fetcher_failure_alerts(db, notifier, acfg)
    db.commit()

    sync_res = None
    if sheet is not None:
        t2 = time.monotonic()
        try:
            sync_res = sheets.sync(db, sheet, settings.raw, now)
            for app in sync_res.new_applications:
                log.info("copied to Applications: %s — %s", app["Company"], app["Role"])
        except Exception as e:  # noqa: BLE001 - a sheet outage must not lose fetch state
            log.error("sheet sync failed: %s", e)
            stats.failed_fetchers.append("sheet-sync")
            db.record_fetch("sheet-sync", False, str(e), now)
        stats.timings["sheet"] = round(time.monotonic() - t2, 1)

    n = now_fn()
    if tiers:  # digest/weekly/reminders only from full runs (they need the fresh sheet state)
        db.finish_run(run_id, stats.__dict__)
        return stats
    if alerts.digest_due(db, acfg, n):
        if notifier.send(alerts.build_digest(db, acfg, n)):
            alerts.mark_digest_sent(db, acfg, n)
        if sync_res is not None:
            alerts.send_reminders(db, notifier, sync_res.applications, settings.section("sheet"), n.date())
    if alerts.weekly_due(db, acfg, n):
        if notifier.send(alerts.build_weekly(db, acfg, n, sync_res.stats if sync_res else None)):
            alerts.mark_weekly_sent(db, acfg, n)
    db.finish_run(run_id, stats.__dict__)
    return stats
