"""Amazon / Microsoft / Shopify / Atlassian workarounds: ToS + robots gate, alternate endpoints,
auto-resolution, sitemap, SimplifyJobs JSON, description enrichment, alert emails, min score."""
import email
import json
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from jobpipe import dedupe, pipeline, robots, sheets
from jobpipe.discovery import community, email_alerts, enrich
from jobpipe.fetchers import REGISTRY, FetchContext, FetcherDisabled, FetcherError
from jobpipe.http import HttpError
from jobpipe.models import Company, Posting
from jobpipe.normalize import enrich as enrich_posting
from jobpipe.notify import ConsoleNotifier

from .conftest import FakeHttp, load_fixture

ALLOW = "User-agent: *\nAllow: /\n"


# ------------------------------------------------------------------- gate
def test_gate_requires_tos_opt_in_and_respects_robots():
    c = Company("Amazon", 1, "amazon")
    with pytest.raises(FetcherDisabled):  # no tos_ok -> silently skipped, like a disabled company
        REGISTRY["amazon"](c, FakeHttp(), FetchContext())
    c.options["tos_ok"] = True
    http = FakeHttp({"GET amazon.jobs/robots.txt": "User-agent: *\nDisallow: /en/search.json\n"})
    with pytest.raises(robots.NotAllowed):  # robots disallow -> a real failure (alerts after 3 runs)
        REGISTRY["amazon"](c, http, FetchContext())
    assert not any("search.json" in call[1] for call in http.calls)
    robots.clear_cache()
    agent_specific = "User-agent: jobpipe\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    with pytest.raises(robots.NotAllowed):
        REGISTRY["amazon"](c, FakeHttp({"GET amazon.jobs/robots.txt": agent_specific}), FetchContext())
    robots.clear_cache()
    with pytest.raises(robots.NotAllowed):  # unreadable robots.txt -> fail closed
        REGISTRY["amazon"](c, FakeHttp({"GET amazon.jobs/robots.txt": HttpError(503, "x")}), FetchContext())


def test_gated_fetcher_without_opt_in_is_not_a_failure(db, settings):
    stats = pipeline.run(db, [Company("Amazon", 1, "amazon")], settings, ConsoleNotifier(),
                         http=FakeHttp(), community=False)
    assert stats.failed_fetchers == []


# -------------------------------------------------------------- microsoft
def test_eightfold_falls_back_to_pcsx():
    pcsx = {"status": 200, "data": {"count": 1, "positions": [
        {"id": 1970393556989020, "name": "Software Engineer", "locations": ["Redmond, Washington, United States"],
         "postedTs": 1791380000, "positionUrl": "/careers/job/1970393556989020", "workLocationOption": "hybrid"}]}}
    http = FakeHttp({"GET apply.careers.microsoft.com/robots.txt": ALLOW,
                     "GET /api/apply/v2/jobs": HttpError(404, "gone"),
                     "GET /api/pcsx/search": pcsx})
    c = Company("Microsoft", 2, "microsoft", "apply.careers.microsoft.com|microsoft.com", options={"tos_ok": True})
    res = REGISTRY["microsoft"](c, http, FetchContext())
    p = enrich_posting(res.postings[0])
    assert p.url == "https://apply.careers.microsoft.com/careers/job/1970393556989020"
    assert p.work_mode == "hybrid" and p.country == "US"
    # after the first fallback, later pages/locations go straight to pcsx
    v2_calls = [c for c in http.calls if "apply/v2" in c[1]]
    assert len(v2_calls) == 1


# -------------------------------------------------------------- atlassian
def test_atlassian_listings():
    listings = [
        {"id": 23001, "title": "Software Engineer, Backend", "locations": ["Remote - Canada", "Toronto - Canada"],
         "category": "Engineering", "overview": "<p>Build Jira backend services in Java and Kotlin.</p>",
         "qualifications": "<ul><li>2+ years of software engineering experience</li></ul>",
         "portalJobPost": {"id": 9, "portalUrl": "https://careers-americas.icims.com/jobs/23001/software-engineer/job"}},
        {"id": 23002, "title": "Principal Engineer", "locations": ["Sydney - Australia"],
         "portalJobPost": {"portalUrl": "https://globalcareers-atlassian.icims.com/jobs/23002/x/job"}},
    ]
    http = FakeHttp({"GET atlassian.com/robots.txt": ALLOW, "GET atlassian.com/endpoint/careers/listings": listings})
    res = REGISTRY["atlassian"](Company("Atlassian", 1, "atlassian", options={"tos_ok": True}), http, FetchContext())
    p = enrich_posting(res.postings[0])
    assert p.url.startswith("https://careers-americas.icims.com/jobs/23001")
    assert p.work_mode == "remote" and p.remote_scope == "Canada" and p.yoe_min == 2
    assert enrich_posting(res.postings[1]).country == "AU"
    with pytest.raises(FetcherError):
        REGISTRY["atlassian"](Company("Atlassian", 1, "atlassian", options={"tos_ok": True}),
                              FakeHttp({"GET robots.txt": ALLOW, "GET listings": {"weird": 1}}), FetchContext())


# ---------------------------------------------------------------- sitemap
SITEMAP_INDEX = """<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://www.shopify.com/sitemap-blog.xml</loc></sitemap>
<sitemap><loc>https://www.shopify.com/careers/sitemap.xml</loc></sitemap></sitemapindex>"""
CAREERS_SITEMAP = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.shopify.com/careers</loc></url>
<url><loc>https://www.shopify.com/careers/backend-developer-ruby-on-rails_2f1c6c1e-1111-4a5b-9c8d-0123456789ab</loc>
<lastmod>2026-10-07T10:00:00Z</lastmod></url>
<url><loc>https://www.shopify.com/careers/senior-staff-data-scientist_9b2afa1b-28fa-4ae4-8f69-80ce64059065</loc></url>
</urlset>"""


def test_sitemap_via_robots_sitemap_lines():
    http = FakeHttp({
        "GET shopify.com/robots.txt": "User-agent: *\nAllow: /\nSitemap: https://www.shopify.com/sitemap.xml\n",
        "GET shopify.com/sitemap.xml$": SITEMAP_INDEX,
        "GET shopify.com/careers/sitemap.xml$": CAREERS_SITEMAP,
    })
    c = Company("Shopify", 1, "sitemap", careers_url="https://www.shopify.com/careers",
                options={"tos_ok": True, "url_pattern": "/careers/[a-z0-9-]+_[0-9a-f-]{36}"})
    res = REGISTRY["sitemap"](c, http, FetchContext())
    assert [p.title for p in res.postings] == ["Backend Developer Ruby on Rails", "Senior Staff Data Scientist"]
    assert res.postings[0].posted_at == "2026-10-07T10:00:00+00:00"
    assert not any("sitemap-blog" in call[1] for call in http.calls)  # only career-looking child maps


# -------------------------------------------------------------------- auto
def test_auto_resolves_then_sticks(db, settings):
    gh = load_fixture("ats/greenhouse_jobs.json")
    http = FakeHttp({"GET boards-api.greenhouse.io/v1/boards/shopify/jobs": gh})  # ashby/lever 404
    shop = lambda: Company("Shopify", 1, "auto", options={"candidates": ["ashby:shopify", "lever:shopify",
                                                                          "greenhouse:shopify"]})
    pipeline.run(db, [shop()], settings, ConsoleNotifier(), http=http, community=False)
    assert json.loads(db.kv_get("resolved:shopify")) == {"ats_type": "greenhouse", "board_token": "shopify"}
    assert db.query("SELECT DISTINCT source FROM postings")[0]["source"] == "greenhouse"
    assert db.query("SELECT fetcher_key FROM fetcher_health")[0]["fetcher_key"] == "auto:shopify"
    http.calls.clear()
    pipeline.run(db, [shop()], settings, ConsoleNotifier(), http=http, community=False)
    assert [c[1] for c in http.calls] == ["https://boards-api.greenhouse.io/v1/boards/shopify/jobs"]
    # resolved board dies for 3 runs -> resolution cleared so the next run probes again
    dead = FakeHttp()
    for _ in range(3):
        pipeline.run(db, [shop()], settings, ConsoleNotifier(), http=dead, community=False)
    assert db.kv_get("resolved:shopify") is None


def test_auto_reports_every_candidate_when_none_work():
    with pytest.raises(FetcherError) as e:
        REGISTRY["auto"](Company("X", 1, "auto", options={"candidates": ["ashby:x", "lever:x"]}), FakeHttp(),
                         FetchContext())
    assert "ashby:x" in str(e.value) and "lever:x" in str(e.value)


# ------------------------------------------------------------ simplify json
def test_simplify_json(db, settings):
    data = load_fixture("community/simplify_listings_excerpt.json")
    now = datetime.fromtimestamp(max(x["date_updated"] for x in data) + 86400, timezone.utc)
    tracked = [Company("Amazon", 1, "amazon"), Company("Microsoft", 2, "microsoft"), Company("Atlassian", 1, "atlassian")]
    src = {"name": "simplify", "type": "simplify_json", "url": "https://raw/x.json", "max_age_days": 400}
    ps = community.parse_simplify_json(data, src, tracked, now=now)
    assert len(ps) == len([x for x in data if x["active"]])  # inactive Roche row dropped
    amazon = [p for p in ps if p.company == "Amazon"]
    assert amazon and all(p.tier == 1 and p.posted_at for p in amazon)
    assert {p.tier for p in ps if p.company == "Atlassian"} == {1}
    # SimplifyJobs + the Amazon fetcher + an alert email all name the same job -> one canonical URL
    assert dedupe.canonical_url("https://amazon.jobs/en/jobs/10408763/software-development-engineer-2026") == \
        dedupe.canonical_url("https://www.amazon.jobs/en-gb/jobs/10408763") == "amazon.jobs/jobs/10408763"
    assert dedupe.canonical_url("https://apply.careers.microsoft.com/careers/job/1970393556860973") == \
        dedupe.canonical_url("https://jobs.careers.microsoft.com/global/en/job/1970393556860973/x")


# ----------------------------------------------------------------- enrich
def test_enrich_community_descriptions(db, settings):
    now = "2026-10-08T12:00:00+00:00"
    def add(ext, url, title="Software Engineer"):
        p = enrich_posting(Posting(company="Acme", company_key="acme", source="community:x", external_id=ext,
                                   title=title, url=url, location_raw="Toronto, ON"))
        db.insert_posting(p, now, passes_filters=1)
        return p.id
    a = add("1", "https://job-boards.greenhouse.io/acme/jobs/555")
    b = add("2", "https://jobs.lever.co/acme/a1b2c3d4-0000-4000-8000-000000000001")
    c = add("3", "https://acme.wd5.myworkdayjobs.com/x/job/1")  # not enrichable
    long_req = "<p>" + "Build Python and C# services. " * 10 + "</p><ul><li>6+ years of software development experience</li></ul>"
    http = FakeHttp({
        "GET boards-api.greenhouse.io/v1/boards/acme/jobs/555": {"id": 555, "content": long_req},
        "GET api.lever.co/v0/postings/acme/a1b2c3d4": load_fixture("ats/lever_postings.json")[0],
    })
    assert enrich.enrich_community(db, http, settings) == 2
    ra, rb, rc = db.get(a), db.get(b), db.get(c)
    assert ra["passes_filters"] == 0 and ra["filter_reason"] == "yoe: requires 6+ years"  # YOE now visible
    assert rb["passes_filters"] == 1 and "TypeScript" in rb["description"] and rb["score"] is None
    assert "no sponsorship" in rb["work_authorization_note"]
    assert rc["description"] == ""
    http.calls.clear()
    assert enrich.enrich_community(db, http, settings) == 0 and not http.calls  # one attempt each


def test_community_rows_never_overwrite_enriched_description(db, settings):
    src = {"name": "x", "type": "simplify_json", "url": "u", "max_age_days": 4000}
    row = {"id": "s1", "company_name": "Acme", "title": "Software Engineer", "active": True,
           "date_posted": 1791400000, "date_updated": 1791400000, "url": "https://jobs.lever.co/acme/a1b2c3d4-0000-4000-8000-000000000001",
           "locations": ["Toronto, ON"], "sponsorship": "Does Not Offer Sponsorship"}
    res = community.FetchResult(community.parse_simplify_json([row], src, [], now=datetime(2026, 10, 8, tzinfo=timezone.utc)))
    cs = community.CommunitySource(src)
    cs.ingest(db, res, settings, "2026-10-08T12:00:00+00:00", pipeline.RunStats())
    pid = res.postings[0].id
    db.update_posting(pid, description="FULL JD " * 30)
    res2 = community.FetchResult(community.parse_simplify_json([row], src, [], now=datetime(2026, 10, 8, tzinfo=timezone.utc)))
    cs.ingest(db, res2, settings, "2026-10-08T13:00:00+00:00", pipeline.RunStats())
    assert db.get(pid)["description"].startswith("FULL JD")


# ------------------------------------------------------------- email alerts
def _alert_email():
    msg = MIMEMultipart("alternative")
    msg["From"] = "Amazon Jobs <jobalerts@amazon.jobs>"
    msg["Date"] = "Thu, 08 Oct 2026 09:15:00 -0400"
    msg["Subject"] = "New jobs matching: software development engineer"
    body = """<html><body>
      <a href="https://click.example.com/r?u=https%3A%2F%2Fwww.amazon.jobs%2Fen%2Fjobs%2F3100001%2Fsde-i">Software Development Engineer I, AWS</a>
      <p>Vancouver, BC</p><a href="https://www.amazon.jobs/en/jobs/3100001/sde-i">View job</a>
      <a href="https://www.amazon.jobs/en/jobs/3100002/sde-ii">SDE II - Prime Video</a>
      <a href="https://www.amazon.jobs/en/settings">Manage alerts</a></body></html>"""
    msg.attach(MIMEText(body, "html"))
    return msg


def test_email_extract(settings):
    rules = settings["email_alerts"]["rules"]
    ps = email_alerts.extract(email.message_from_bytes(_alert_email().as_bytes()), rules,
                              {"amazon": Company("Amazon", 1, "amazon")})
    assert [(p.external_id, p.title) for p in ps] == [("3100001", "Software Development Engineer I, AWS"),
                                                       ("3100002", "SDE II - Prime Video")]
    assert ps[0].url == "https://www.amazon.jobs/en/jobs/3100001" and ps[0].tier == 1
    assert ps[0].posted_at == "2026-10-08T13:15:00+00:00" and ps[0].source == "email:amazon"


def test_email_fetch_is_readonly_and_never_closes(settings, monkeypatch):
    monkeypatch.setenv("EMAIL_IMAP_USER", "me@example.com")
    monkeypatch.setenv("EMAIL_IMAP_PASSWORD", "app-pass")
    raw = _alert_email().as_bytes()

    class FakeIMAP:
        ops = []
        def login(self, u, p): self.ops.append(("login", u))
        def select(self, folder, readonly=False): self.ops.append(("select", folder, readonly))
        def search(self, *a): return "OK", [b"1"]
        def fetch(self, num, what): self.ops.append(("fetch", what)); return "OK", [(b"1 (BODY[] {n}", raw), b")"]
        def logout(self): self.ops.append(("logout",))

    cfg = dict(settings["email_alerts"], enabled=True)
    assert email_alerts.configured(cfg)
    res = email_alerts.fetch(cfg, [Company("Amazon", 1, "amazon")], imap_factory=FakeIMAP)
    assert len(res.postings) == 2 and res.complete is False
    assert ("select", '"jobpipe"', True) in FakeIMAP.ops and ("fetch", "(BODY.PEEK[])") in FakeIMAP.ops


# ---------------------------------------------------------------- min score
def test_sheet_min_score_keeps_tier1(db, settings, spreadsheet):
    now = "2026-10-08T15:00:00+00:00"
    for ext, score, tier in (("lo", 10, 3), ("hi", 60, 3), ("t1", 5, 1)):
        p = enrich_posting(Posting(company="Acme", company_key="acme", source="greenhouse", external_id=ext,
                                   title="Software Engineer", url=f"https://a/{ext}", location_raw="Toronto", tier=tier))
        db.insert_posting(p, now, passes_filters=1, score=score)
    sheets.sync(db, spreadsheet, settings.raw, now)
    ids = {r[0] for r in spreadsheet.sheets["Postings"].get_all_values()[1:]}
    assert len(ids) == 2 and db.query("SELECT id FROM postings WHERE external_id='lo'")[0]["id"] not in ids


def test_db_migration_adds_columns(tmp_path):
    import sqlite3
    from jobpipe.db import DB
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE postings (id TEXT PRIMARY KEY, company TEXT NOT NULL, company_key TEXT NOT NULL, "
                "source TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, "
                "first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, status TEXT, locations_json TEXT, "
                "closed_at TEXT, duplicate_of TEXT)")
    con.commit(); con.close()
    d = DB(path)
    cols = {r["name"] for r in d.conn.execute("PRAGMA table_info(postings)")}
    assert {"user_status", "enrich_tried"} <= cols
