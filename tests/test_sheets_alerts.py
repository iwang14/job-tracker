from datetime import date, datetime, timedelta, timezone

from jobpipe import alerts, sheets
from jobpipe.models import Posting
from jobpipe.normalize import enrich
from jobpipe.notify import ConsoleNotifier, chunk_text, md_to_telegram_html

NOW = "2026-10-08T15:00:00+00:00"


def add_posting(db, ext, title="Software Engineer II", loc="Toronto, ON", score=75, company="Acme", tier=2,
                first_seen=NOW, **kw):
    p = enrich(Posting(company=company, company_key=company.lower(), source="greenhouse", external_id=ext,
                       title=title, url=f"https://acme.example/{ext}", location_raw=loc, tier=tier))
    db.insert_posting(p, first_seen, passes_filters=1, score=score, fit_summary="Matches: C#", **kw)
    return p.id


def col(ws, name):
    header = ws.get_all_values()[0]
    return header.index(name)


def find_row(ws, pid):
    for i, r in enumerate(ws.get_all_values()):
        if r[0] == pid:
            return i, r
    raise KeyError(pid)


def test_sync_creates_tabs_and_preserves_user_columns(db, settings, spreadsheet):
    a = add_posting(db, "1", first_seen="2026-10-08T10:00:00+00:00")
    b = add_posting(db, "2", title="Backend Developer", first_seen="2026-10-08T12:00:00+00:00")
    res = sheets.sync(db, spreadsheet, settings.raw, NOW)
    ws = spreadsheet.sheets["Postings"]
    assert set(spreadsheet.sheets) == {"Postings", "Applications", "Prep", "Stats"}
    assert res.added_postings == 2
    vals = ws.get_all_values()
    assert vals[0][:3] == ["ID", "First seen", "Company"] and vals[1][0] == b  # newest first
    assert vals[1][col(ws, "Status")] == "new"
    assert spreadsheet.batch_requests, "dropdowns/conditional formatting requested on first sync"

    # user edits: status + notes; user also adds their own column at the end
    i, _ = find_row(ws, a)
    ws.rows[i][col(ws, "Status")] = "applying"
    ws.rows[i][col(ws, "My notes")] = "ask Sam for referral"
    ws.rows[0].append("My priority")
    ws.rows[i].append("high")
    # script-side change: score updated, and a newer posting arrives
    db.update_posting(a, score=88, fit_summary="Fits: .NET APIs")
    c = add_posting(db, "3", title="Full Stack Engineer", first_seen="2026-10-08T14:00:00+00:00")
    res = sheets.sync(db, spreadsheet, settings.raw, NOW)
    assert res.added_postings == 1
    i, row = find_row(ws, a)
    assert row[col(ws, "Status")] == "applying" and row[col(ws, "My notes")] == "ask Sam for referral"
    assert row[col(ws, "Score")] == "88" and row[col(ws, "Fit summary")] == "Fits: .NET APIs"
    assert row[col(ws, "My priority")] == "high"
    assert ws.get_all_values()[1][0] == c
    assert db.get(a)["user_status"] == "applying"


def test_applied_copies_to_applications_once_and_stage_tracking(db, settings, spreadsheet):
    a = add_posting(db, "1", company="Shopify", tier=1)
    sheets.sync(db, spreadsheet, settings.raw, NOW)
    pw, aw = spreadsheet.sheets["Postings"], spreadsheet.sheets["Applications"]
    i, _ = find_row(pw, a)
    pw.rows[i][col(pw, "Status")] = "applied"

    res = sheets.sync(db, spreadsheet, settings.raw, NOW)
    assert len(res.new_applications) == 1
    apps = aw.get_all_values()
    assert len(apps) == 2
    app = dict(zip(apps[0], apps[1]))
    assert app["Company"] == "Shopify" and app["Stage"] == "applied" and app["Date applied"] == "2026-10-08"
    assert app["Next action date"] == "2026-10-18" and app["Tier"] == "1"

    # second sync: no duplicate application
    res = sheets.sync(db, spreadsheet, settings.raw, NOW)
    assert not res.new_applications and len(aw.get_all_values()) == 2

    # user fills in channel/resume and moves to OA a few days later
    later = "2026-10-12T15:00:00+00:00"
    aw.rows[1][col(aw, "Stage")] = "OA"
    aw.rows[1][col(aw, "Channel")] = "referral"
    aw.rows[1][col(aw, "Resume version")] = "v2-backend"
    aw.rows[1][col(aw, "Notes")] = "HackerRank, 90 min"
    res = sheets.sync(db, spreadsheet, settings.raw, later)
    row = dict(zip(aw.get_all_values()[0], aw.get_all_values()[1]))
    assert row["Last update"] == "2026-10-12" and row["First response"] == "2026-10-12"
    assert row["Notes"] == "HackerRank, 90 min" and row["Channel"] == "referral"  # user columns untouched
    prep = spreadsheet.sheets["Prep"].get_all_values()
    assert len(prep) == 2 and "interview reports for Shopify" in prep[1][4] and prep[1][3] == "OA"
    assert res.prep_added and res.stage_changes == [(a, "applied", "OA")]

    # rejected after OA still counts as a callback for stats
    aw.rows[1][col(aw, "Stage")] = "rejected"
    res = sheets.sync(db, spreadsheet, settings.raw, "2026-10-20T15:00:00+00:00")
    assert res.stats["overall"] == (1, 1, "100%")
    assert ("referral", 1, 1, "100%") in res.stats["channel"]
    assert res.stats["median_days_to_response"] == 4
    assert len(spreadsheet.sheets["Prep"].get_all_values()) == 2  # no new prep row for "rejected"
    stats_text = str(spreadsheet.sheets["Stats"].get_all_values())
    assert "By resume version" in stats_text and "v2-backend" in stats_text


def test_manual_application_rows_are_tracked(db, settings, spreadsheet):
    sheets.sync(db, spreadsheet, settings.raw, NOW)
    aw = spreadsheet.sheets["Applications"]
    header = aw.get_all_values()[0]
    row = [""] * len(header)
    for k, v in {"Company": "Atlassian", "Role": "SWE", "Date applied": "2026-10-01", "Stage": "applied",
                 "Channel": "LinkedIn"}.items():
        row[header.index(k)] = v
    aw.rows.append(row)
    res = sheets.sync(db, spreadsheet, settings.raw, NOW)
    assert res.stats["overall"][0] == 1 and res.applications[0]["_key"].startswith("manual:")


def test_closed_rows_pruned_only_if_untouched(db, settings, spreadsheet):
    a = add_posting(db, "1")
    b = add_posting(db, "2", title="Backend Developer")
    sheets.sync(db, spreadsheet, settings.raw, NOW)
    pw = spreadsheet.sheets["Postings"]
    i, _ = find_row(pw, b)
    pw.rows[i][col(pw, "Status")] = "applying"
    old = "2026-09-01T00:00:00+00:00"
    db.conn.execute("UPDATE postings SET status='closed', closed_at=?", (old,))
    res = sheets.sync(db, spreadsheet, settings.raw, NOW)
    ids = [r[0] for r in pw.get_all_values()[1:]]
    assert a not in ids and b in ids and res.removed_postings == 1
    _, row = find_row(pw, b)
    assert row[col(pw, "Open?")] == "closed"


def test_format_requests_shape(db, settings, spreadsheet):
    sheets.sync(db, spreadsheet, settings.raw, NOW)
    reqs = spreadsheet.batch_requests[0]["requests"]
    kinds = [list(r)[0] for r in reqs]
    assert kinds.count("setDataValidation") == 3 and kinds.count("addConditionalFormatRule") == 2
    formula = [r for r in reqs if "addConditionalFormatRule" in r][0]["addConditionalFormatRule"]["rule"]
    assert "TODAY()" in formula["booleanRule"]["condition"]["values"][0]["userEnteredValue"]


# --------------------------------------------------------------------- alerts
def test_digest_due_respects_toronto_time_and_dst(db, settings):
    cfg = settings.section("alerts")
    summer_7am = datetime(2026, 7, 1, 11, 30, tzinfo=timezone.utc)   # 07:30 EDT
    summer_8am = datetime(2026, 7, 1, 12, 5, tzinfo=timezone.utc)    # 08:05 EDT
    winter_8am = datetime(2026, 12, 1, 13, 5, tzinfo=timezone.utc)   # 08:05 EST
    winter_7am = datetime(2026, 12, 1, 12, 5, tzinfo=timezone.utc)   # 07:05 EST
    assert not alerts.digest_due(db, cfg, summer_7am)
    assert alerts.digest_due(db, cfg, summer_8am)
    assert not alerts.digest_due(db, cfg, winter_7am) and alerts.digest_due(db, cfg, winter_8am)
    alerts.mark_digest_sent(db, cfg, summer_8am)
    assert not alerts.digest_due(db, cfg, summer_8am + timedelta(hours=5))
    assert alerts.digest_due(db, cfg, summer_8am + timedelta(days=1))


def test_digest_sections_and_weekly(db, settings):
    add_posting(db, "1", loc="Vancouver, BC", score=90)
    add_posting(db, "2", loc="Austin, TX", score=70, title="Backend Developer")
    add_posting(db, "3", loc="Remote - Canada", score=60, title="Full Stack Engineer")
    now = datetime(2026, 10, 8, 16, tzinfo=timezone.utc)
    text = alerts.build_digest(db, settings.section("alerts"), now)
    ca, us = text.split("United States")
    assert "[90]" in ca and "[60]" in ca and "[70]" in us
    assert ca.index("[90]") < ca.index("[60]")  # sorted by score
    weekly = alerts.build_weekly(db, settings.section("alerts"), now,
                                 sheets.compute_stats([], date(2026, 10, 8)))
    assert "3 new postings → 3 passed filters" in weekly and "Acme 3" in weekly


def test_instant_alert_rules(db, settings):
    cfg = settings.section("alerts")
    now = datetime.fromisoformat(NOW)
    add_posting(db, "t1", tier=1, score=10, title="Software Engineer I")       # tier 1 -> alert
    add_posting(db, "hi", tier=3, score=85, title="Backend Developer")         # score >= 80 -> alert
    add_posting(db, "lo", tier=3, score=50, title="Full Stack Engineer")       # no
    add_posting(db, "boot", tier=1, score=95, title="Software Developer", bootstrap=1)  # backfill -> digest only
    add_posting(db, "old", tier=1, score=95, title="SWE", posted_at=None,
                first_seen="2026-09-01T00:00:00+00:00")                     # too old
    note = ConsoleNotifier()
    assert alerts.send_instant(db, note, cfg, now) == 2
    assert alerts.send_instant(db, note, cfg, now) == 0  # marked alerted


def test_follow_up_reminders(db, settings):
    today = date(2026, 10, 20)
    apps = [
        {"_key": "a", "Company": "A", "Role": "SWE", "Stage": "applied", "Date applied": "2026-10-05",
         "Last update": "2026-10-05", "Next action": "", "Next action date": ""},
        {"_key": "b", "Company": "B", "Role": "SWE", "Stage": "tech screen", "Date applied": "2026-10-15",
         "Last update": "2026-10-18", "Next action": "", "Next action date": ""},
        {"_key": "c", "Company": "C", "Role": "SWE", "Stage": "rejected", "Date applied": "2026-09-01",
         "Last update": "2026-09-02", "Next action": "", "Next action date": ""},
        {"_key": "d", "Company": "D", "Role": "SWE", "Stage": "onsite", "Date applied": "2026-10-01",
         "Last update": "2026-10-19", "Next action": "Send thank-you", "Next action date": "2026-10-19"},
    ]
    got = {(k, kind) for k, kind, _ in alerts.follow_up_reminders(apps, today, settings.section("sheet"))}
    assert got == {("a", "stale"), ("b", "no_next_action"), ("d", "overdue")}
    note = ConsoleNotifier()
    assert alerts.send_reminders(db, note, apps, settings.section("sheet"), today) == 3
    assert alerts.send_reminders(db, note, apps, settings.section("sheet"), today) == 0  # once per day


def test_notify_helpers():
    assert md_to_telegram_html("**x** [y](https://z) <a>") == '<b>x</b> <a href="https://z">y</a> &lt;a&gt;'
    chunks = chunk_text("\n".join(["line %d" % i for i in range(500)]), 200)
    assert all(len(c) <= 200 for c in chunks) and "".join(chunks).count("line") == 500
