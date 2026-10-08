from datetime import datetime, timedelta, timezone

from jobpipe import dedupe
from jobpipe.models import Location, Posting
from jobpipe.normalize import enrich
from jobpipe.scoring import keyword_fit, location_points, score_posting

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def mk(title, loc, ext="1", source="greenhouse", url=None, company="Acme"):
    return enrich(Posting(company=company, company_key=company.lower(), source=source, external_id=ext,
                          title=title, url=url or f"https://acme.example/jobs/{ext}", location_raw=loc))


# -------------------------------------------------------------------- dedupe
def test_exact_identity_is_stable():
    assert mk("SWE", "Toronto", ext="9").id == mk("SWE", "Toronto", ext="9").id
    assert mk("SWE", "Toronto", ext="9").id != mk("SWE", "Toronto", ext="9", source="lever").id


def test_fuzzy_duplicate_and_repost(db):
    now = NOW.isoformat()
    a = mk("Software Engineer II - Backend", "Toronto, ON", ext="100")
    db.insert_posting(a, now)
    # same job, new req id, slightly different punctuation, still open -> duplicate
    b = mk("Software Engineer II, Backend", "Toronto, Ontario, Canada", ext="101")
    assert dedupe.find_match(db, b, now) == ("duplicate", a.id)
    # different city -> not a duplicate
    assert dedupe.find_match(db, mk("Software Engineer II - Backend", "Austin, TX", ext="102"), now) is None
    # different role -> not a duplicate
    assert dedupe.find_match(db, mk("Software Engineer II - Frontend", "Toronto, ON", ext="103"), now) is None
    # once the original is closed, the same title again is a repost
    db.update_posting(a.id, status="closed", closed_at=now)
    assert dedupe.find_match(db, b, now) == ("repost", a.id)
    # closed long ago -> outside the repost window
    old = (NOW - timedelta(days=90)).isoformat()
    db.update_posting(a.id, closed_at=old)
    assert dedupe.find_match(db, b, now) is None


def test_duplicate_by_url_across_sources(db):
    now = NOW.isoformat()
    a = mk("Software Engineer", "Toronto, ON", ext="100", url="https://boards.greenhouse.io/acme/jobs/100")
    db.insert_posting(a, now)
    c = mk("SWE - New Grad 2027", "Toronto", ext="zz", source="community:x",
           url="https://www.boards.greenhouse.io/acme/jobs/100/")
    assert dedupe.find_match(db, c, now) == ("duplicate", a.id)


def test_locations_overlap_country_vs_city():
    assert dedupe.locations_overlap([Location(country="CA")], [Location(city="Toronto", country="CA")])
    assert not dedupe.locations_overlap([Location(country="US")], [Location(city="Toronto", country="CA")])


# -------------------------------------------------------------------- scoring
def test_keyword_fit(settings):
    profile = settings["profile"]
    ratio, hits = keyword_fit("Backend Engineer", "We use C#, .NET, TypeScript, PostgreSQL and AWS.", profile)
    assert {"C#", ".NET", "TypeScript", "PostgreSQL", "AWS"} <= set(hits) and ratio == 1.0
    ratio, hits = keyword_fit("Engineer", "Java and Spring.", profile)
    assert ratio == 0 and hits == []
    # "C" alone or "C++" must not count as C#
    _, hits = keyword_fit("Engineer", "C++ and C.", profile)
    assert "C#" not in hits


def test_location_points(settings):
    lw = settings["scoring"]["location_weights"]
    assert location_points([Location(city="Vancouver", country="CA")], "onsite", None, lw) == (15, "Vancouver")
    assert location_points([Location(country="CA", remote=True)], "remote", "Canada", lw) == (10, "remote-Canada")
    assert location_points([Location(city="Toronto", country="CA"), Location(city="Vancouver", country="CA")],
                           "hybrid", None, lw)[0] == 15
    assert location_points([Location(city="Seattle", country="US")], "onsite", None, lw)[0] == 0


def test_score_composition(settings):
    kw = dict(title="Software Engineer", description="C#, .NET, TypeScript, Python, AWS, SQL",
              locations=[Location(city="Vancouver", country="CA")], work_mode="onsite", remote_scope=None,
              posted_at=(NOW - timedelta(hours=3)).isoformat(), first_seen_at=None,
              settings=settings.raw, profile=settings["profile"], now=NOW)
    s1 = score_posting(tier=1, **kw)
    assert s1.total == 100  # 70 skills + 15 Vancouver + 15 tier-1 + 10 fresh, clamped
    s3 = score_posting(tier=3, **{**kw, "locations": [Location(city="Austin", country="US")]})
    assert s3.total == 80 and s3.breakdown["location"] == 0 and s3.breakdown["fresh"] == 10
    s_llm = score_posting(tier=3, llm_fit=20, **{**kw, "locations": [Location(city="Austin", country="US")]})
    assert s_llm.total < s3.total and s_llm.breakdown["llm_fit"] == 20
