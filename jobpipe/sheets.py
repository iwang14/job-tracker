"""Google Sheet tracker sync.

Ownership rules (so syncs never clobber your edits):
  * Postings tab: the script owns every column except `Status` and `My notes`.
    Existing rows are updated cell-by-cell for script columns only; user columns are written
    exactly once, when the row is created (Status = "new").
  * Applications tab: rows are created by the script when you set a posting to "applied";
    after that you own the row. The script only fills `Last update` / `First response` when it
    sees the Stage change, and only if you haven't already changed them yourself.
  * Prep tab: append-only checklist rows.
  * Stats tab: fully script-owned and rewritten each run.
Columns are located by header text, so you can reorder or add your own columns.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .db import DB, row_locations
from .models import Location

log = logging.getLogger(__name__)

POSTINGS = "Postings"
APPLICATIONS = "Applications"
PREP = "Prep"
STATS = "Stats"

P_SCRIPT = ["ID", "First seen", "Company", "Title", "Level", "Location", "Mode", "Remote scope",
            "Country", "Score", "Fit summary", "Salary", "Work auth", "Posted", "Link", "Tier", "Open?"]
P_USER = ["Status", "My notes"]
P_HEADERS = P_SCRIPT + P_USER
STATUS_VALUES = ["new", "applying", "applied", "skipped"]

A_HEADERS = ["Posting ID", "Company", "Role", "Link", "Tier", "Country", "Date applied", "Channel",
             "Referral contact", "Resume version", "Stage", "Last update", "Next action",
             "Next action date", "First response", "Notes"]
CHANNELS = ["referral", "company site", "LinkedIn", "recruiter", "other"]
STAGES = ["applied", "OA", "recruiter screen", "tech screen", "onsite", "offer", "rejected", "ghosted"]
CALLBACK_STAGES = {"OA", "recruiter screen", "tech screen", "onsite", "offer"}
INTERVIEW_STAGES = {"OA", "recruiter screen", "tech screen", "onsite"}
ACTIVE_STAGES = {"applied"} | INTERVIEW_STAGES
PREP_STAGES = {"OA", "tech screen"}

PREP_HEADERS = ["Created", "Company", "Role", "Stage", "Checklist", "Done"]


# ---------------------------------------------------------------- helpers
def parse_date(s: str | None) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def col_letter(idx0: int) -> str:
    n, s = idx0 + 1, ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def a1(row1: int, col0: int) -> str:
    return f"{col_letter(col0)}{row1}"


def header_index(header: list[str]) -> dict[str, int]:
    return {h.strip(): i for i, h in enumerate(header) if h.strip()}


def cell(row: list[str], idx: dict[str, int], name: str) -> str:
    i = idx.get(name)
    return row[i].strip() if i is not None and i < len(row) else ""


def app_key(row: list[str], idx: dict[str, int]) -> str:
    pid = cell(row, idx, "Posting ID")
    if pid:
        return pid
    return "manual:" + "|".join(cell(row, idx, k).lower() for k in ("Company", "Role", "Date applied"))


def location_label(locs: list[Location], raw: str) -> str:
    labels = []
    for loc in locs:
        lab = loc.label()
        if lab and lab not in labels:
            labels.append(lab)
    s = "; ".join(labels) if labels else raw
    return s if len(s) <= 120 else s[:117] + "..."


# ------------------------------------------------------------ connection
def open_sheet():
    """Open the spreadsheet from env (GOOGLE_SERVICE_ACCOUNT_JSON + SHEET_ID). None if unconfigured."""
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.environ.get("SHEET_ID")
    if not sa or not sheet_id:
        return None
    import gspread
    info = json.loads(sa) if sa.strip().startswith("{") else json.load(open(sa, encoding="utf-8"))
    return gspread.service_account_from_dict(info).open_by_key(sheet_id)


def get_or_create(ss, title: str, headers: list[str], rows: int = 1000) -> tuple[Any, bool]:
    try:
        ws = ss.worksheet(title)
        created = False
    except Exception as e:  # gspread.WorksheetNotFound (kept generic so tests need no gspread)
        if e.__class__.__name__ != "WorksheetNotFound":
            raise
        ws = ss.add_worksheet(title=title, rows=rows, cols=max(len(headers), 10))
        created = True
    values = ws.get_all_values()
    if not values or not any(values[0]):
        ws.update(range_name="A1", values=[headers])
    else:
        # add any missing headers at the end (never move the user's columns)
        missing = [h for h in headers if h not in header_index(values[0])]
        if missing:
            start = len(values[0])
            ws.update(range_name=a1(1, start), values=[missing])
    return ws, created


# ---------------------------------------------------------------- formatting
def _validation(sheet_id: int, col0: int, values: list[str]) -> dict:
    return {"setDataValidation": {
        "range": {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": col0, "endColumnIndex": col0 + 1},
        "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in values]},
                 "showCustomUi": True, "strict": False},
    }}


def _date_format(sheet_id: int, col0: int) -> dict:
    return {"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": col0, "endColumnIndex": col0 + 1},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}},
        "fields": "userEnteredFormat.numberFormat",
    }}


def format_requests(postings_ws, apps_ws) -> list[dict]:
    """Dropdowns, date formats, frozen headers, and overdue-next-action highlighting."""
    p_idx = header_index(postings_ws.get_all_values()[0])
    a_idx = header_index(apps_ws.get_all_values()[0])
    reqs = []
    for ws in (postings_ws, apps_ws):
        reqs.append({"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                                               "fields": "gridProperties.frozenRowCount"}})
    reqs.append(_validation(postings_ws.id, p_idx["Status"], STATUS_VALUES))
    reqs.append(_validation(apps_ws.id, a_idx["Stage"], STAGES))
    reqs.append(_validation(apps_ws.id, a_idx["Channel"], CHANNELS))
    for name in ("Date applied", "Last update", "Next action date", "First response"):
        reqs.append(_date_format(apps_ws.id, a_idx[name]))
    nad, stage = col_letter(a_idx["Next action date"]), col_letter(a_idx["Stage"])
    reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": [{"sheetId": apps_ws.id, "startRowIndex": 1}],
        "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue":
                f'=AND(${nad}2<>"", ${nad}2<TODAY(), ISERROR(MATCH(${stage}2, {{"offer","rejected","ghosted"}}, 0)))'}]},
            "format": {"backgroundColor": {"red": 0.96, "green": 0.8, "blue": 0.8},
                       "textFormat": {"foregroundColor": {"red": 0.6, "green": 0, "blue": 0}}},
        }}}})
    openc = col_letter(p_idx["Open?"])
    reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": [{"sheetId": postings_ws.id, "startRowIndex": 1}],
        "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": f'=${openc}2="closed"'}]},
                        "format": {"textFormat": {"foregroundColor": {"red": 0.6, "green": 0.6, "blue": 0.6},
                                                  "strikethrough": True}}}}}})
    return reqs


# ------------------------------------------------------------------ sync
@dataclass
class SyncResult:
    added_postings: int = 0
    updated_postings: int = 0
    removed_postings: int = 0
    new_applications: list[dict] = field(default_factory=list)
    stage_changes: list[tuple[str, str, str]] = field(default_factory=list)
    prep_added: list[dict] = field(default_factory=list)
    applications: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def posting_row_values(r) -> dict[str, Any]:
    return {
        "ID": r["id"], "First seen": (r["first_seen_at"] or "")[:16].replace("T", " "),
        "Company": r["company"], "Title": r["title"], "Level": r["level_guess"] or "",
        "Location": location_label(row_locations(r), r["location_raw"]), "Mode": r["work_mode"] or "",
        "Remote scope": r["remote_scope"] or "", "Country": r["country"] or "",
        "Score": r["score"] if r["score"] is not None else "", "Fit summary": r["fit_summary"] or "",
        "Salary": r["salary"] or "", "Work auth": r["work_authorization_note"] or "",
        "Posted": (r["posted_at"] or "")[:10], "Link": r["url"], "Tier": r["tier"],
        "Open?": "open" if r["status"] == "open" else "closed",
    }


def _same(a: Any, b: str) -> bool:
    return str(a if a is not None else "").strip() == (b or "").strip()


def sync_postings(db: DB, ws, cfg: dict, now: str, res: SyncResult) -> list[list[str]]:
    values = ws.get_all_values()
    header = values[0]
    idx = header_index(header)
    existing: dict[str, int] = {}
    for i, row in enumerate(values[1:], start=2):
        pid = cell(row, idx, "ID")
        if pid:
            existing[pid] = i
            status = cell(row, idx, "Status")
            if status:
                db.update_posting(pid, user_status=status)

    window = (datetime.fromisoformat(now) - timedelta(days=int(cfg.get("postings_window_days", 21)))).isoformat()
    # min_score hides low-signal rows (mostly community-list postings with no description);
    # Tier-1 companies are always shown, and rows already in the sheet are never dropped by it.
    min_score = int(cfg.get("min_score", 0))
    rows = db.query(
        "SELECT * FROM postings WHERE passes_filters=1 AND duplicate_of IS NULL "
        "AND ((status='open' AND first_seen_at>=? AND (COALESCE(score, 0)>=? OR tier=1)) OR id IN (%s)) "
        "ORDER BY first_seen_at DESC, score DESC"
        % ",".join("?" * len(existing)), (window, min_score, *existing.keys()))

    updates, new_rows = [], []
    for r in rows:
        vals = posting_row_values(r)
        if r["id"] in existing:
            rownum = existing[r["id"]]
            current = values[rownum - 1]
            for name in P_SCRIPT:
                ci = idx.get(name)
                if ci is None:
                    continue
                cur = current[ci] if ci < len(current) else ""
                if not _same(vals[name], cur):
                    updates.append({"range": a1(rownum, ci), "values": [[vals[name]]]})
        else:
            if r["status"] != "open":
                continue
            out = [""] * len(header)
            for name, v in vals.items():
                if name in idx:
                    out[idx[name]] = v
            out[idx["Status"]] = "new"
            new_rows.append(out)
    # RAW: values are stored exactly as written, so the next run's comparison is stable
    # (USER_ENTERED would turn "2026-10-08 01:46" into a date and look "changed" every run).
    if updates:
        ws.batch_update(updates, value_input_option="RAW")
        res.updated_postings = len({u["range"].lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for u in updates})
    if new_rows:
        ws.insert_rows(new_rows, row=2, value_input_option="RAW")
        res.added_postings = len(new_rows)

    # prune: closed rows you never acted on, after a grace period
    prune_days = int(cfg.get("prune_closed_after_days", 14))
    cutoff = (datetime.fromisoformat(now) - timedelta(days=prune_days)).isoformat()
    stale = {r["id"] for r in db.query(
        "SELECT id FROM postings WHERE status='closed' AND closed_at<?", (cutoff,))}
    values = ws.get_all_values()
    to_delete = [i for i, row in enumerate(values[1:], start=2)
                 if cell(row, idx, "ID") in stale and cell(row, idx, "Status") in ("", "new", "skipped")]
    for rownum in sorted(to_delete, reverse=True):
        ws.delete_rows(rownum)
    res.removed_postings = len(to_delete)
    return ws.get_all_values() if to_delete else values


def copy_applied(db: DB, postings_values: list[list[str]], apps_ws, today: str, cfg: dict, res: SyncResult) -> None:
    p_idx = header_index(postings_values[0])
    a_values = apps_ws.get_all_values()
    a_idx = header_index(a_values[0])
    already = {cell(r, a_idx, "Posting ID") for r in a_values[1:]}
    already |= {r["app_key"] for r in db.query("SELECT app_key FROM app_state")}
    follow_up = int(cfg.get("follow_up_days", 10))
    new = []
    for row in postings_values[1:]:
        if cell(row, p_idx, "Status").lower() != "applied":
            continue
        pid = cell(row, p_idx, "ID")
        if not pid or pid in already:
            continue
        rec = {
            "Posting ID": pid, "Company": cell(row, p_idx, "Company"), "Role": cell(row, p_idx, "Title"),
            "Link": cell(row, p_idx, "Link"), "Tier": cell(row, p_idx, "Tier"),
            "Country": cell(row, p_idx, "Country"), "Date applied": today, "Channel": "company site",
            "Stage": "applied", "Last update": today, "Next action": "Follow up if no response",
            "Next action date": (date.fromisoformat(today) + timedelta(days=follow_up)).isoformat(),
        }
        out = [""] * len(a_values[0])
        for k, v in rec.items():
            out[a_idx[k]] = v
        new.append(out)
        res.new_applications.append(rec)
        already.add(pid)
        db.conn.execute("INSERT OR IGNORE INTO app_state(app_key, posting_id, stage, copied_at, stage_changed_at) "
                        "VALUES(?,?,?,?,?)", (pid, pid, "applied", today, today))
    if new:
        apps_ws.append_rows(new, value_input_option="USER_ENTERED")


def track_stages(db: DB, apps_ws, prep_ws, today: str, res: SyncResult) -> list[dict]:
    values = apps_ws.get_all_values()
    idx = header_index(values[0])
    updates, prep_rows, apps = [], [], []
    for rownum, row in enumerate(values[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        key = app_key(row, idx)
        stage = cell(row, idx, "Stage") or "applied"
        st = db.conn.execute("SELECT * FROM app_state WHERE app_key=?", (key,)).fetchone()
        prev = st["stage"] if st else None
        if st is None:
            db.conn.execute("INSERT INTO app_state(app_key, posting_id, stage, copied_at, stage_changed_at, "
                            "reached_callback) VALUES(?,?,?,?,?,?)",
                            (key, cell(row, idx, "Posting ID") or None, stage, today, today,
                             int(stage in CALLBACK_STAGES)))
        elif prev != stage:
            res.stage_changes.append((key, prev or "", stage))
            db.conn.execute("UPDATE app_state SET stage=?, stage_changed_at=?, reached_callback=MAX(reached_callback, ?) "
                            "WHERE app_key=?", (stage, today, int(stage in CALLBACK_STAGES), key))
            last = parse_date(cell(row, idx, "Last update"))
            if last is None or last < date.fromisoformat(today):
                updates.append({"range": a1(rownum, idx["Last update"]), "values": [[today]]})
                row[idx["Last update"]] = today
            if prev == "applied" and stage != "applied" and not cell(row, idx, "First response"):
                updates.append({"range": a1(rownum, idx["First response"]), "values": [[today]]})
                row[idx["First response"]] = today
        if stage in PREP_STAGES and (prev != stage):
            done = db.conn.execute("SELECT 1 FROM prep_items WHERE app_key=? AND stage=?", (key, stage)).fetchone()
            if not done:
                company, role = cell(row, idx, "Company"), cell(row, idx, "Role")
                checklist = (f"Research recent {stage} interview reports for {company} — {role} "
                             f"(Glassdoor, Blind, LeetCode Discuss, Reddit); note question types & format; "
                             f"re-read the JD and map 3 STAR stories to it; review your resume bullets for this role.")
                prep_rows.append([today, company, role, stage, checklist, "FALSE"])
                db.conn.execute("INSERT INTO prep_items(app_key, stage, created_at) VALUES(?,?,?)", (key, stage, today))
                res.prep_added.append({"company": company, "role": role, "stage": stage})
        rec = {h: cell(row, idx, h) for h in A_HEADERS}
        rec["_key"] = key
        r2 = db.conn.execute("SELECT reached_callback FROM app_state WHERE app_key=?", (key,)).fetchone()
        rec["_callback"] = bool(r2 and r2["reached_callback"]) or stage in CALLBACK_STAGES
        apps.append(rec)
    if updates:
        apps_ws.batch_update(updates, value_input_option="USER_ENTERED")
    if prep_rows and prep_ws is not None:
        prep_ws.append_rows(prep_rows, value_input_option="USER_ENTERED")
    return apps


# ----------------------------------------------------------------- stats
def compute_stats(apps: list[dict], today: date) -> dict:
    def rate(group: list[dict]) -> tuple[int, int, str]:
        n = len(group)
        cb = sum(1 for a in group if a["_callback"])
        return n, cb, f"{(cb / n * 100):.0f}%" if n else "-"

    out: dict[str, Any] = {"total": len(apps)}
    for dim, col in (("channel", "Channel"), ("tier", "Tier"), ("country", "Country"), ("resume", "Resume version")):
        groups: dict[str, list[dict]] = defaultdict(list)
        for a in apps:
            groups[a.get(col) or "(blank)"].append(a)
        out[dim] = sorted(((k, *rate(v)) for k, v in groups.items()), key=lambda t: -t[1])
    out["overall"] = rate(apps)
    days = []
    for a in apps:
        d0, d1 = parse_date(a.get("Date applied")), parse_date(a.get("First response"))
        if d0 and d1 and d1 >= d0:
            days.append((d1 - d0).days)
    out["median_days_to_response"] = statistics.median(days) if days else None
    weeks: dict[str, int] = defaultdict(int)
    for a in apps:
        d = parse_date(a.get("Date applied"))
        if d:
            y, w, _ = d.isocalendar()
            weeks[f"{y}-W{w:02d}"] += 1
    recent = []
    for i in range(11, -1, -1):
        y, w, _ = (today - timedelta(weeks=i)).isocalendar()
        k = f"{y}-W{w:02d}"
        recent.append((k, weeks.get(k, 0)))
    out["per_week"] = recent
    funnel = defaultdict(int)
    for a in apps:
        funnel[a.get("Stage") or "applied"] += 1
    out["stages"] = [(s, funnel.get(s, 0)) for s in STAGES]
    return out


def stats_rows(stats: dict, now: str) -> list[list[Any]]:
    rows: list[list[Any]] = [[f"Updated {now[:16].replace('T', ' ')} UTC — callback = reached OA / recruiter screen / "
                              "tech screen / onsite / offer"], []]
    n, cb, r = stats["overall"]
    rows += [["Applications", n], ["Callbacks", cb], ["Callback rate", r],
             ["Median days to first response", stats["median_days_to_response"] if stats["median_days_to_response"] is not None else "-"],
             []]
    for title, key in (("By channel", "channel"), ("By tier", "tier"), ("By country", "country"),
                       ("By resume version", "resume")):
        rows.append([title, "Applications", "Callbacks", "Callback rate"])
        rows += [list(t) for t in stats[key]]
        rows.append([])
    rows.append(["Current stage", "Count"])
    rows += [list(t) for t in stats["stages"]]
    rows.append([])
    rows.append(["Week", "Applications"])
    rows += [list(t) for t in stats["per_week"]]
    return rows


def write_stats(ws, stats: dict, now: str) -> None:
    ws.clear()
    ws.update(range_name="A1", values=stats_rows(stats, now))


# ----------------------------------------------------------------- entry
def sync(db: DB, ss, settings: dict, now: str, today: str | None = None) -> SyncResult:
    cfg = settings.get("sheet") or {}
    today = today or now[:10]
    res = SyncResult()
    p_ws, p_new = get_or_create(ss, POSTINGS, P_HEADERS, rows=2000)
    a_ws, a_new = get_or_create(ss, APPLICATIONS, A_HEADERS)
    prep_ws, _ = get_or_create(ss, PREP, PREP_HEADERS, rows=200)
    s_ws, _ = get_or_create(ss, STATS, ["Stats"], rows=200)
    if p_new or a_new or db.kv_get("sheet_formatted") != "1":
        try:
            ss.batch_update({"requests": format_requests(p_ws, a_ws)})
            db.kv_set("sheet_formatted", "1")
        except Exception as e:  # noqa: BLE001 - formatting is cosmetic
            log.warning("sheet formatting failed: %s", e)
    p_values = sync_postings(db, p_ws, cfg, now, res)
    copy_applied(db, p_values, a_ws, today, cfg, res)
    res.applications = track_stages(db, a_ws, prep_ws, today, res)
    res.stats = compute_stats(res.applications, date.fromisoformat(today))
    write_stats(s_ws, res.stats, now)
    db.commit()
    return res
