import copy
from datetime import datetime, timedelta, timezone

from jobpipe import pipeline
from jobpipe.http import Response
from jobpipe.models import Company
from jobpipe.notify import ConsoleNotifier

from .conftest import FakeHttp, load_fixture

T0 = datetime(2026, 10, 8, 11, 7, tzinfo=timezone.utc)  # 07:07 Toronto (before the 08:00 digest)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def companies():
    return [
        Company("Example GH", 2, "greenhouse", "examplegh"),
        Company("Example Lever", 1, "lever", "examplelever"),
        Company("Broken Co", 3, "greenhouse", "doesnotexist"),
        Company("Google", 1, "google"),
    ]


def make_http(gh, lever):
    return FakeHttp({
        "GET boards-api.greenhouse.io/v1/boards/examplegh/jobs": lambda *a: gh,
        "GET api.lever.co/v0/postings/examplelever": lambda *a: lever,
    })


def run(db, settings, http, clock, notifier, **kw):
    return pipeline.run(db, companies(), settings, notifier, http=http, community=False, now_fn=clock, **kw)


def test_end_to_end(db, settings):
    gh = load_fixture("ats/greenhouse_jobs.json")
    lever = load_fixture("ats/lever_postings.json")
    clock, note = Clock(T0), ConsoleNotifier()

    # ---- run 1: bootstrap. Everything is new, nothing is instant-alerted.
    s1 = run(db, settings, make_http(gh, lever), clock, note)
    assert s1.new == 6 and s1.fetched == 6
    assert s1.alerts == 0
    assert s1.failed_fetchers == ["greenhouse:doesnotexist"]  # Google is disabled, not a failure
    rows = {r["external_id"]: r for r in db.query("SELECT * FROM postings")}
    assert rows["7001001"]["passes_filters"] == 1 and rows["7001001"]["bootstrap"] == 1
    assert rows["7001002"]["filter_reason"].startswith("title")          # Senior
    assert rows["7001003"]["filter_reason"] == "location: remote scope US-only"
    assert rows["7001004"]["filter_reason"] == "yoe: requires 5+ years"
    assert rows["7001002"]["description"] == ""                           # non-matching text not stored
    good = rows["7001001"]
    assert good["score"] is not None and good["fit_summary"].startswith("Matches:")
    assert not note.sent  # 07:07 local: no digest yet

    # ---- run 2 (+1h): a new Lever posting appears (Tier 1 -> instant alert); GH job 7001001 vanishes once.
    clock.t = T0 + timedelta(hours=1)
    lever2 = copy.deepcopy(lever)
    new = copy.deepcopy(lever[0])
    new.update(id="a1b2c3d4-0000-4000-8000-0000000000ff", text="Software Developer II, Platform",
               createdAt=int(clock.t.timestamp() * 1000), hostedUrl="https://jobs.lever.co/examplelever/ff")
    lever2.append(new)
    gh2 = {"jobs": [j for j in gh["jobs"] if j["id"] != 7001001]}
    s2 = run(db, settings, make_http(gh2, lever2), clock, note)
    assert s2.new == 1 and s2.alerts == 1
    instant = [m for m in note.sent if m.startswith("🚨")]
    assert len(instant) == 1 and "Software Developer II, Platform" in instant[0] and "jobs.lever.co" in instant[0]
    assert db.query("SELECT status, missed_runs FROM postings WHERE external_id='7001001'")[0]["missed_runs"] == 1
    # digest went out with the 08:07 run, split by country
    digests = [m for m in note.sent if m.startswith("📬")]
    assert len(digests) == 1 and "Canada" in digests[0] and "United States" in digests[0]

    # ---- run 3: second consecutive miss -> closed. Broken fetcher hits 3 failures -> alert.
    clock.t = T0 + timedelta(hours=2)
    s3 = run(db, settings, make_http(gh2, lever2), clock, note)
    assert s3.closed == 1
    assert db.query("SELECT status FROM postings WHERE external_id='7001001'")[0]["status"] == "closed"
    failing = [m for m in note.sent if m.startswith("🛠️")]
    assert len(failing) == 1 and "doesnotexist" in failing[0]
    assert len([m for m in note.sent if m.startswith("📬")]) == 1     # digest only once per day
    assert len([m for m in note.sent if m.startswith("🚨")]) == 1     # no repeat alerts

    # ---- run 4: posting comes back -> reopened; board answers 304 -> treated as all seen
    clock.t = T0 + timedelta(hours=3)
    http = make_http(gh, lever2)
    http.routes.insert(0, ("GET", __import__("re").compile("api.lever.co"), Response(304)))
    s4 = run(db, settings, http, clock, note)
    assert db.query("SELECT status FROM postings WHERE external_id='7001001'")[0]["status"] == "open"
    assert s4.new == 0
    lever_rows = db.query("SELECT last_seen_at FROM postings WHERE source='lever' AND status='open'")
    assert all(r["last_seen_at"] == clock.t.isoformat() for r in lever_rows)
    run_rows = db.query("SELECT COUNT(*) n FROM runs WHERE finished_at IS NOT NULL")
    assert run_rows[0]["n"] == 4


def test_tier_fast_lane_only_fetches_tier1(db, settings):
    http = make_http(load_fixture("ats/greenhouse_jobs.json"), load_fixture("ats/lever_postings.json"))
    pipeline.run(db, companies(), settings, ConsoleNotifier(), http=http, community=False, tiers={1},
                 now_fn=Clock(T0))
    assert {c[1].split("/")[2] for c in http.calls} == {"api.lever.co"}


def test_incomplete_results_never_close(db, settings):
    from jobpipe.models import FetchResult
    c = Company("Example GH", 2, "greenhouse", "examplegh")
    now = T0.isoformat()
    full = load_fixture("ats/greenhouse_jobs.json")
    http = make_http(full, [])
    res = pipeline.fetch_one(c, http, pipeline.FetchContext())
    stats = pipeline.RunStats()
    pipeline.ingest(db, c, res, settings, now, stats)
    for _ in range(3):
        pipeline.ingest(db, c, FetchResult([], complete=False), settings, now, stats)
    assert stats.closed == 0


def test_changed_description_triggers_rescore(db, settings):
    gh = load_fixture("ats/greenhouse_jobs.json")
    lever = load_fixture("ats/lever_postings.json")
    run(db, settings, make_http(gh, lever), Clock(T0), ConsoleNotifier())
    gh2 = copy.deepcopy(gh)
    gh2["jobs"][0]["content"] += "&lt;p&gt;Now also React.&lt;/p&gt;"
    s = run(db, settings, make_http(gh2, lever), Clock(T0 + timedelta(hours=1)), ConsoleNotifier())
    assert s.updated == 1
    row = db.query("SELECT score, description FROM postings WHERE external_id='7001001'")[0]
    assert "React" in row["description"] and row["score"] is not None


def test_llm_used_and_cached(db, settings, monkeypatch):
    from jobpipe import llm
    calls = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(llm, "semantic_fit", lambda *a, **k: calls.append(a) or (90, "Fits: C#/.NET APIs | Gaps: none"))
    gh, lever = load_fixture("ats/greenhouse_jobs.json"), load_fixture("ats/lever_postings.json")
    run(db, settings, make_http(gh, lever), Clock(T0), ConsoleNotifier())
    n = len(calls)
    assert n == 2  # only postings that passed the hard filters
    row = db.query("SELECT fit_summary, score_breakdown FROM postings WHERE external_id='7001001'")[0]
    assert row["fit_summary"].startswith("Fits:") and '"llm_fit": 90' in row["score_breakdown"]
    # force a rescore: cache hit, no new call
    db.conn.execute("UPDATE postings SET score=NULL")
    run(db, settings, make_http(gh, lever), Clock(T0 + timedelta(hours=1)), ConsoleNotifier())
    assert len(calls) == n
