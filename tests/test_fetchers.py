import pytest

from jobpipe.fetchers import REGISTRY, FetchContext, FetcherDisabled
from jobpipe.http import HttpError
from jobpipe.models import Company
from jobpipe.normalize import enrich

from .conftest import FakeHttp


def test_greenhouse():
    http = FakeHttp({"GET boards-api.greenhouse.io/v1/boards/examplegh/jobs": "ats/greenhouse_jobs.json"})
    res = REGISTRY["greenhouse"](Company("Example GH", 2, "greenhouse", "examplegh"), http, FetchContext())
    assert http.calls[0][2] == {"content": "true"}
    assert len(res.postings) == 4 and res.complete
    p = res.postings[0]
    assert (p.external_id, p.title, p.source) == ("7001001", "Software Engineer II, Backend", "greenhouse")
    assert p.url == "https://boards.greenhouse.io/examplegh/jobs/7001001"
    assert "<" not in p.description and "C#" in p.description  # double-escaped HTML is decoded
    assert p.posted_at == "2026-10-07T17:00:00+00:00"
    assert p.work_mode_hint == "hybrid"
    enrich(p)
    assert p.country == "CA" and p.city == "Toronto" and p.level_guess == "II"
    assert p.salary == "CA$110,000 - CA$140,000"
    assert p.yoe_min == 2  # the "5+ years" line sits under "Nice to have"


def test_lever():
    http = FakeHttp({"GET api.lever.co/v0/postings/examplelever": "ats/lever_postings.json"})
    res = REGISTRY["lever"](Company("Example Lever", 2, "lever", "examplelever"), http, FetchContext())
    p = res.postings[0]
    assert p.title == "Software Engineer I" and p.work_mode_hint == "hybrid"
    assert p.location_raw == "Vancouver, BC; Remote - Canada"
    assert p.salary.startswith("CAD 95,000–120,000")
    assert "Requirements:" in p.description and "1+ years" in p.description
    enrich(p)
    assert "no sponsorship" in p.work_authorization_note
    assert p.level_guess == "I" and p.yoe_min == 1


def test_lever_eu_region():
    http = FakeHttp({"GET api.eu.lever.co/v0/postings/x": []})
    REGISTRY["lever"](Company("X", 3, "lever", "x", options={"region": "eu"}), http, FetchContext())
    assert "api.eu.lever.co" in http.calls[0][1]


def test_ashby():
    http = FakeHttp({"GET api.ashbyhq.com/posting-api/job-board/exampleashby": "ats/ashby_board.json"})
    res = REGISTRY["ashby"](Company("Example Ashby", 2, "ashby", "exampleashby"), http, FetchContext())
    assert http.calls[0][2] == {"includeCompensation": "true"}
    assert len(res.postings) == 1  # unlisted job skipped
    p = enrich(res.postings[0])
    assert p.work_mode == "remote" and p.remote_scope == "Canada"
    assert p.salary == "CA$120K - CA$150K"
    assert {loc.city for loc in p.locations} >= {"Toronto"}
    assert p.yoe_min == 2


def test_smartrecruiters_fetches_details_only_for_relevant_new_titles():
    http = FakeHttp({
        "GET postings/744000012345678$": "ats/smartrecruiters_detail.json",
        "GET api.smartrecruiters.com/v1/companies/ExampleSR/postings$": "ats/smartrecruiters_list.json",
    })
    ctx = FetchContext(title_ok=lambda t: "engineer" in t.lower())
    res = REGISTRY["smartrecruiters"](Company("Example SR", 3, "smartrecruiters", "ExampleSR"), http, ctx)
    assert len(res.postings) == 2
    detail_calls = [c for c in http.calls if c[1].endswith("744000012345678")]
    assert len(detail_calls) == 1 and not any(c[1].endswith("679") for c in http.calls)
    p = enrich(res.postings[0])
    assert p.url.startswith("https://jobs.smartrecruiters.com/ExampleSR/744000012345678")
    assert p.country == "US" and p.yoe_min == 3
    assert "US work auth required" in p.work_authorization_note and "no sponsorship" in p.work_authorization_note
    assert p.salary == "$120,000 - $150,000 USD"
    # known posting -> no detail call
    http.calls.clear()
    REGISTRY["smartrecruiters"](Company("Example SR", 3, "smartrecruiters", "ExampleSR"), http,
                                FetchContext(known_ids={"744000012345678"}, title_ok=ctx.title_ok))
    assert not any(c[1].endswith("744000012345678") for c in http.calls)


def test_workday_paging_and_details():
    pages = {0: "ats/workday_jobs_p1.json", 2: "ats/workday_jobs_p2.json"}
    from .conftest import load_fixture
    http = FakeHttp({
        "POST /wday/cxs/example/External/jobs$": lambda url, params, body: load_fixture(pages[body["offset"]]),
        "GET Software-Engineer_JR100$": "ats/workday_detail_jr100.json",
        "GET Software-Developer-II_JR102$": "ats/workday_detail_jr102.json",
    })
    ctx = FetchContext(title_ok=lambda t: "software" in t.lower())
    res = REGISTRY["workday"](Company("Example WD", 2, "workday", "example/wd5/External"), http, ctx)
    assert [p.title for p in res.postings] == ["Software Engineer", "Mechanical Engineer", "Software Developer II"]
    assert res.complete
    body = http.calls[0][3]
    assert body["limit"] == 20 and body["searchText"] == "software"
    p = enrich(res.postings[0])
    assert p.country == "CA" and p.work_mode == "hybrid" and {loc.city for loc in p.locations} == {"Toronto", "Ottawa"}
    assert p.url.endswith("Software-Engineer_JR100")
    mech = res.postings[1]
    assert mech.description == "" and mech.posted_at is not None  # no detail call for irrelevant titles
    assert not any("Mechanical" in c[1] for c in http.calls if c[0] == "GET")


def test_workday_bad_token():
    from jobpipe.fetchers.base import FetcherError
    with pytest.raises(FetcherError):
        REGISTRY["workday"](Company("X", 3, "workday", "nope"), FakeHttp(), FetchContext())


ALLOW_ALL = "User-agent: *\nAllow: /\n"


def test_amazon():
    http = FakeHttp({"GET amazon.jobs/robots.txt": ALLOW_ALL, "GET amazon.jobs/en/search.json": "ats/amazon_search.json"})
    res = REGISTRY["amazon"](Company("Amazon", 1, "amazon", options={"tos_ok": True}), http, FetchContext())
    params = [c for c in http.calls if "search.json" in c[1]][0][2]
    assert params["country[]"] == ["USA", "CAN"] and params["offset"] == 0
    a, b = (enrich(p) for p in res.postings)
    assert a.url == "https://www.amazon.jobs/en/jobs/3100001/software-development-engineer-i-aws"
    assert (a.country, a.city, a.posted_at[:10]) == ("CA", "Vancouver", "2026-10-07")
    assert a.yoe_min == 1  # preferred "3+ years" ignored
    assert b.yoe_min == 5 and b.level_guess == "senior"


def test_eightfold_microsoft():
    http = FakeHttp({"GET apply.careers.microsoft.com/robots.txt": HttpError(404, "x"),
                     "GET apply.careers.microsoft.com/api/apply/v2/jobs": "ats/eightfold_jobs.json"})
    c = Company("Microsoft", 2, "microsoft", "apply.careers.microsoft.com|microsoft.com", options={"tos_ok": True})
    res = REGISTRY["microsoft"](c, http, FetchContext())
    assert len(res.postings) == 1  # same id from both location queries is deduped
    p = enrich(res.postings[0])
    assert p.source == "eightfold" and p.country == "CA" and p.work_mode == "onsite"


@pytest.mark.parametrize("ats", ["google", "meta", "apple"])
def test_bigtech_are_disabled(ats):
    with pytest.raises(FetcherDisabled):
        REGISTRY[ats](Company(ats, 1, ats), FakeHttp(), FetchContext())
