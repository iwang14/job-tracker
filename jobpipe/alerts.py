"""Instant alerts, daily digest, weekly summary, fetcher-failure and follow-up reminders.

Digest / weekly are not separate cron jobs: every run checks "is it past 08:00 America/Toronto
and has today's digest not gone out yet?". That survives DST and late/skipped cron runs.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import DB, row_locations
from .notify import Notifier
from .sheets import ACTIVE_STAGES, INTERVIEW_STAGES, parse_date


def _ago(ts: str | None, now: datetime) -> str:
    if not ts:
        return "?"
    h = (now - datetime.fromisoformat(ts)).total_seconds() / 3600
    if h < 1:
        return f"{max(1, int(h * 60))}m ago"
    if h < 48:
        return f"{int(h)}h ago"
    return f"{int(h / 24)}d ago"


def _loc(r) -> str:
    locs = row_locations(r)
    labels = list(dict.fromkeys(loc.label() for loc in locs if loc.label()))
    s = "; ".join(labels[:3]) + (f" (+{len(labels) - 3})" if len(labels) > 3 else "")
    return s or r["location_raw"] or "?"


def posting_line(r, now: datetime, detailed: bool = True) -> str:
    score = r["score"] if r["score"] is not None else "?"
    head = f"**[{score}] {r['title']}** — {r['company']} (T{r['tier']})"
    if not detailed:
        return f"{head} · {_loc(r)} · {r['url']}"
    lines = [head, f"📍 {_loc(r)} · {r['work_mode']}" + (f" ({r['remote_scope']})" if r["remote_scope"] else ""),
             f"🕒 posted {_ago(r['posted_at'], now)} · seen {_ago(r['first_seen_at'], now)}"]
    if r["salary"]:
        lines.append(f"💰 {r['salary']}")
    if r["fit_summary"]:
        lines.append(f"🧠 {r['fit_summary']}")
    if r["work_authorization_note"]:
        lines.append(f"⚠️ {r['work_authorization_note'][:160]}")
    lines.append(r["url"])
    return "\n".join(lines)


# ------------------------------------------------------------- instant
def instant_candidates(db: DB, cfg: dict, now: datetime) -> list:
    threshold = int(cfg.get("instant_score", 80))
    max_age = (now - timedelta(days=int(cfg.get("instant_max_age_days", 7)))).isoformat()
    rows = db.query(
        "SELECT * FROM postings WHERE passes_filters=1 AND duplicate_of IS NULL AND repost_of IS NULL "
        "AND status='open' AND alerted_at IS NULL AND bootstrap=0 AND (tier=1 OR score>=?) "
        "AND COALESCE(posted_at, first_seen_at)>=? ORDER BY score DESC", (threshold, max_age))
    return rows


def send_instant(db: DB, notifier: Notifier, cfg: dict, now: datetime) -> int:
    rows = instant_candidates(db, cfg, now)
    if not rows:
        return 0
    cap = int(cfg.get("instant_max_per_run", 10))
    shown = rows[:cap]
    text = "🚨 **New matching posting" + ("s" if len(shown) > 1 else "") + "**\n\n" + \
        "\n\n".join(posting_line(r, now) for r in shown)
    if len(rows) > cap:
        text += f"\n\n…and {len(rows) - cap} more in today's digest / the sheet."
    ok = notifier.send(text)
    if ok is not False:
        db.conn.executemany("UPDATE postings SET alerted_at=? WHERE id=?",
                            [(now.isoformat(timespec="seconds"), r["id"]) for r in rows])
    return len(shown)


# -------------------------------------------------------------- digest
def _local(now: datetime, tz: str) -> datetime:
    return now.astimezone(ZoneInfo(tz))


def digest_due(db: DB, cfg: dict, now: datetime) -> bool:
    local = _local(now, cfg.get("timezone", "America/Toronto"))
    return local.hour >= int(cfg.get("digest_hour", 8)) and db.kv_get("digest_last_date") != local.date().isoformat()


def build_digest(db: DB, cfg: dict, now: datetime) -> str:
    since = db.kv_get("digest_last_sent_at") or (now - timedelta(days=1)).isoformat()
    rows = db.query(
        "SELECT * FROM postings WHERE passes_filters=1 AND duplicate_of IS NULL AND status='open' "
        "AND first_seen_at>=? ORDER BY score DESC", (since,))
    canada = [r for r in rows if "CA" in (r["country"] or "").split("|") or r["remote_scope"] in ("Canada", "North America", "Global")]
    us = [r for r in rows if r not in canada]
    local = _local(now, cfg.get("timezone", "America/Toronto"))
    cap = int(cfg.get("digest_max_per_section", 25))
    out = [f"📬 **Daily digest — {local:%a %b %d}** · {len(rows)} new matching postings"]
    for label, group in (("🇨🇦 Canada & remote-friendly", canada), ("🇺🇸 United States", us)):
        out.append(f"\n**{label} ({len(group)})**")
        if not group:
            out.append("_nothing new_")
        out += [posting_line(r, now, detailed=False) for r in group[:cap]]
        if len(group) > cap:
            out.append(f"…+{len(group) - cap} more in the sheet")
    return "\n".join(out)


def mark_digest_sent(db: DB, cfg: dict, now: datetime) -> None:
    local = _local(now, cfg.get("timezone", "America/Toronto"))
    db.kv_set("digest_last_date", local.date().isoformat())
    db.kv_set("digest_last_sent_at", now.isoformat(timespec="seconds"))


# -------------------------------------------------------------- weekly
def weekly_due(db: DB, cfg: dict, now: datetime) -> bool:
    local = _local(now, cfg.get("timezone", "America/Toronto"))
    week = f"{local.isocalendar()[0]}-W{local.isocalendar()[1]:02d}"
    return (local.weekday() == int(cfg.get("weekly_weekday", 0)) and local.hour >= int(cfg.get("digest_hour", 8))
            and db.kv_get("weekly_last") != week)


def build_weekly(db: DB, cfg: dict, now: datetime, stats: dict | None) -> str:
    since = (now - timedelta(days=7)).isoformat()
    total = db.query("SELECT COUNT(*) n FROM postings WHERE first_seen_at>=?", (since,))[0]["n"]
    passed = db.query("SELECT COUNT(*) n FROM postings WHERE first_seen_at>=? AND passes_filters=1 "
                      "AND duplicate_of IS NULL", (since,))[0]["n"]
    alerted = db.query("SELECT COUNT(*) n FROM postings WHERE alerted_at>=?", (since,))[0]["n"]
    closed = db.query("SELECT COUNT(*) n FROM postings WHERE closed_at>=?", (since,))[0]["n"]
    top = db.query("SELECT company, COUNT(*) n FROM postings WHERE first_seen_at>=? GROUP BY company "
                   "ORDER BY n DESC LIMIT 8", (since,))
    top_match = db.query("SELECT company, COUNT(*) n FROM postings WHERE first_seen_at>=? AND passes_filters=1 "
                         "AND duplicate_of IS NULL GROUP BY company ORDER BY n DESC LIMIT 8", (since,))
    reasons = db.query("SELECT substr(filter_reason, 1, instr(filter_reason||':', ':')-1) r, COUNT(*) n FROM postings "
                       "WHERE first_seen_at>=? AND passes_filters=0 GROUP BY r ORDER BY n DESC", (since,))
    lines = ["📊 **Weekly summary**",
             f"Funnel (7d): {total} new postings → {passed} passed filters → {alerted} instant alerts · {closed} closed",
             "Filtered out by: " + ", ".join(f"{r['r'] or '?'} {r['n']}" for r in reasons),
             "Most active (all): " + ", ".join(f"{r['company']} {r['n']}" for r in top),
             "Most matching: " + ", ".join(f"{r['company']} {r['n']}" for r in top_match)]
    if stats:
        n, cb, rate = stats["overall"]
        week = stats["per_week"][-1][1] if stats.get("per_week") else 0
        med = stats.get("median_days_to_response")
        lines.append(f"Applications: {n} total, {week} this week · callbacks {cb} ({rate}) · "
                     f"median days to response {med if med is not None else '-'}")
        best = [c for c in stats.get("channel", []) if c[1] >= 3]
        if best:
            b = max(best, key=lambda c: c[2] / c[1])
            lines.append(f"Best channel so far: {b[0]} ({b[3]} of {b[1]})")
    return "\n".join(lines)


def mark_weekly_sent(db: DB, cfg: dict, now: datetime) -> None:
    local = _local(now, cfg.get("timezone", "America/Toronto"))
    db.kv_set("weekly_last", f"{local.isocalendar()[0]}-W{local.isocalendar()[1]:02d}")


# ------------------------------------------------------- fetcher health
def fetcher_failure_alerts(db: DB, notifier: Notifier, cfg: dict) -> int:
    threshold = int(cfg.get("fetcher_failure_runs", 3))
    rows = db.query("SELECT * FROM fetcher_health WHERE consecutive_failures>=? AND alerted=0", (threshold,))
    recovered = db.query("SELECT * FROM fetcher_health WHERE consecutive_failures=0 AND alerted=1")
    if rows:
        text = "🛠️ **Fetcher failing** (≥%d runs in a row)\n" % threshold + "\n".join(
            f"• {r['fetcher_key']}: {r['consecutive_failures']} runs — {(r['last_error'] or '')[:200]}" for r in rows)
        if notifier.send(text) is not False:
            for r in rows:
                db.set_health_alerted(r["fetcher_key"], True)
    if recovered:
        if notifier.send("✅ Fetcher recovered: " + ", ".join(r["fetcher_key"] for r in recovered)) is not False:
            for r in recovered:
                db.set_health_alerted(r["fetcher_key"], False)
    return len(rows)


# ----------------------------------------------------------- reminders
def follow_up_reminders(apps: list[dict], today: date, cfg: dict) -> list[tuple[str, str, str]]:
    """Returns (app_key, kind, message) tuples."""
    days = int(cfg.get("follow_up_days", 10))
    out = []
    for a in apps:
        stage = a.get("Stage") or "applied"
        if stage not in ACTIVE_STAGES:
            continue
        label = f"{a.get('Company')} — {a.get('Role')} [{stage}]"
        last = parse_date(a.get("Last update")) or parse_date(a.get("Date applied"))
        if last and (today - last).days >= days:
            out.append((a["_key"], "stale", f"⏰ No update in {(today - last).days} days: {label}"))
        if stage in INTERVIEW_STAGES and not (a.get("Next action") or "").strip():
            out.append((a["_key"], "no_next_action", f"📝 Interview stage with no next action: {label}"))
        nad = parse_date(a.get("Next action date"))
        if nad and nad < today and (a.get("Next action") or "").strip():
            out.append((a["_key"], "overdue", f"🔴 Overdue ({nad.isoformat()}): {a.get('Next action')} — {label}"))
    return out


def send_reminders(db: DB, notifier: Notifier, apps: list[dict], cfg: dict, today: date) -> int:
    items = []
    for key, kind, msg in follow_up_reminders(apps, today, cfg):
        seen = db.conn.execute("SELECT 1 FROM reminders_sent WHERE app_key=? AND kind=? AND sent_on=?",
                               (key, kind, today.isoformat())).fetchone()
        if not seen:
            items.append((key, kind, msg))
    if not items:
        return 0
    if notifier.send("**Follow-ups**\n" + "\n".join(m for _, _, m in items)) is not False:
        db.conn.executemany("INSERT OR IGNORE INTO reminders_sent(app_key, kind, sent_on) VALUES(?,?,?)",
                            [(k, kind, today.isoformat()) for k, kind, _ in items])
    return len(items)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)

