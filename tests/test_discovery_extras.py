import json

import pytest
import yaml

from jobpipe import pipeline
from jobpipe.discovery import ats_detect, community, seed
from jobpipe.extras import referral, resume
from jobpipe.http import Http
from jobpipe.models import Company

from .conftest import FakeHttp, load_fixture


# ------------------------------------------------------------------ community
def test_parse_simplify_html_table():
    rows = community.parse_tables(load_fixture("community/simplify_newgrad_excerpt.md"))
    assert len(rows) >= 10
    ixl = [r for r in rows if r.company == "IXL Learning"]
    assert len(ixl) == 2 and {r.location for r in ixl} == {"Raleigh, NC", "San Mateo, CA"}  # "↳" rows
    assert ixl[0].url == "https://www.ixl.com/company/jobs?gh_jid=8862049002"  # employer link, tracking stripped
    spruce = next(r for r in rows if r.company == "SpruceID")
    assert spruce.url.startswith("https://jobs.ashbyhq.com/spruceid/") and spruce.age == "0d"
    assert not any("simplify.jobs" in r.url for r in rows)


def test_parse_markdown_table():
    rows = community.parse_tables(load_fixture("community/speedyapply_excerpt.md"))
    assert rows and rows[0].company == "Microsoft"
    assert rows[0].url.startswith("https://apply.careers.microsoft.com/careers/job/")
    assert any(r.company == "Roblox" for r in rows)


def test_community_ingest_suggests_new_companies_and_dedupes_tracked(db, settings, monkeypatch, tmp_path):
    (tmp_path / "companies.yaml").write_text("companies:\n  - {name: Affirm, tier: 2, ats_type: greenhouse, board_token: affirm}\n")
    monkeypatch.setattr("jobpipe.config.CONFIG_DIR", tmp_path)
    md = load_fixture("community/simplify_newgrad_excerpt.md")
    http = FakeHttp({"GET raw.githubusercontent.com": md})
    tracked = [Company("Affirm", 2, "greenhouse", "affirm")]
    src = {"name": "simplify", "url": "https://raw.githubusercontent.com/x/README.md"}
    res = community.fetch_source(src, http, tracked)
    affirm = [p for p in res.postings if p.company_key == "affirm"]
    assert affirm and all(p.tier == 2 for p in affirm)
    stats = pipeline.RunStats()
    community.ingest_community(db, community.CommunitySource(src), res, settings, "2026-10-08T12:00:00+00:00", stats)
    sugg = {r["company_key"]: r for r in db.query("SELECT * FROM suggestions")}
    assert "affirm" not in sugg
    assert sugg["spruceid"]["ats_type"] == "ashby" and sugg["spruceid"]["board_token"] == "spruceid"
    # first time this source is seen -> backfill, no instant alerts
    assert all(r["bootstrap"] == 1 for r in db.query("SELECT bootstrap FROM postings"))


# --------------------------------------------------------------- ATS detector
@pytest.mark.parametrize("url,ats,token", [
    ("https://boards.greenhouse.io/stripe", "greenhouse", "stripe"),
    ("https://job-boards.greenhouse.io/affirm/jobs/8008649003", "greenhouse", "affirm"),
    ("https://boards.greenhouse.io/embed/job_board?for=figma", "greenhouse", "figma"),
    ("https://jobs.lever.co/plaid/abc-123", "lever", "plaid"),
    ("https://jobs.ashbyhq.com/notion", "ashby", "notion"),
    ("https://jobs.smartrecruiters.com/Visa/744000", "smartrecruiters", "Visa"),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Santa-Clara/SWE_JR1",
     "workday", "nvidia/wd5/NVIDIAExternalCareerSite"),
    ("https://www.amazon.jobs/en/jobs/123", "amazon", None),
    ("https://apply.careers.microsoft.com/careers/job/1", "microsoft", "apply.careers.microsoft.com|microsoft.com"),
    ("https://www.ixl.com/company/jobs?gh_jid=8862049002", "greenhouse", None),
])
def test_detect_from_url(url, ats, token):
    d = ats_detect.detect_from_text(url)
    assert (d.ats_type, d.board_token) == (ats, token)


def test_detect_from_careers_page_and_verify():
    page = '<html><script src="https://boards.greenhouse.io/embed/job_board/js?for=acmeco"></script></html>'
    http = FakeHttp({
        "GET acme.example/careers": page,
        "GET boards-api.greenhouse.io/v1/boards/acmeco/jobs": {"jobs": [{"id": 1}, {"id": 2}]},
    })
    d = ats_detect.detect("https://acme.example/careers", http)
    assert (d.ats_type, d.board_token, d.verified, d.job_count) == ("greenhouse", "acmeco", True, 2)


def test_probe_picks_board_with_jobs():
    http = FakeHttp({"GET api.ashbyhq.com/posting-api/job-board/acme$": {"jobs": [{"id": "x"}]}})
    d = ats_detect.probe("Acme Inc.", http)
    assert (d.ats_type, d.board_token) == ("ashby", "acme")


def test_seed_and_approve(tmp_path, monkeypatch):
    monkeypatch.setattr("jobpipe.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("jobpipe.discovery.seed.CONFIG_DIR", tmp_path)
    (tmp_path / "companies.yaml").write_text("companies:\n  - {name: Stripe, tier: 2, ats_type: greenhouse, board_token: stripe}\n")
    (tmp_path / "seed.yaml").write_text(yaml.safe_dump([
        {"name": "Stripe", "ats_type": "greenhouse", "board_token": "stripe"},       # already tracked: skipped
        {"name": "Acme", "tier": 2, "ats_type": "greenhouse", "board_token": "acme"},
        {"name": "Nope Corp", "tier": 3},
    ]))
    http = FakeHttp({"GET boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [{"id": 1}]}})
    out = seed.run_seed(tmp_path / "seed.yaml", tmp_path / "cand.yaml", http=http)
    cand = yaml.safe_load(out.read_text())["companies"]
    assert [c["name"] for c in cand] == ["Acme", "Nope Corp"]
    assert cand[0]["_verified"] is True and cand[1]["ats_type"] == "unknown"
    added = seed.approve(out)
    assert added == ["Acme", "Nope Corp"]
    text = (tmp_path / "companies.yaml").read_text()
    lines = text.strip().splitlines()
    assert lines[-2] == "  - {name: Acme, tier: 2, ats_type: greenhouse, board_token: acme}"
    assert "enabled: false" in lines[-1]  # unverified rows are added disabled
    from jobpipe.config import load_companies
    assert [c.name for c in load_companies(tmp_path / "companies.yaml")] == ["Stripe", "Acme", "Nope Corp"]
    assert seed.approve(out) == []  # idempotent


# --------------------------------------------------------------------- extras
RESUME = """# Me
## Experience
- Built C#/.NET REST APIs for billing
- Wrote React + TypeScript dashboard features
- Organized team lunches
"""


def test_resume_tailor_drops_invented_bullets(settings, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = {"reorder": [{"bullet": "Built C#/.NET REST APIs for billing", "reason": "core backend match"},
                        {"bullet": "Led a team of 10 engineers", "reason": "invented!"}],
            "reword": [{"bullet": "Wrote React + TypeScript dashboard features",
                        "suggestion": "Built React/TypeScript front-end features for a customer dashboard",
                        "reason": "JD says front-end"}],
            "missing_keywords": ["Kubernetes", "C#"], "notes": "Strong backend overlap."}
    monkeypatch.setattr("jobpipe.llm.generate", lambda *a, **k: json.dumps(fake))
    jd = "We use C#, .NET, Kubernetes, GraphQL and PostgreSQL."
    out = resume.tailor(jd, RESUME, settings["profile"])
    assert [r["bullet"] for r in out["reorder"]] == ["Built C#/.NET REST APIs for billing"]
    assert "1 suggestion(s) dropped" in out["notes"]
    assert "Kubernetes" in out["missing_keywords"] and "C#" not in out["missing_keywords"]
    assert "GraphQL" in out["missing_keywords"] and "PostgreSQL" in out["missing_keywords"]
    assert "Move these existing bullets up" in resume.render(out)


def test_resume_tailor_keyword_fallback(settings, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = resume.tailor("C#, .NET and React required. Kubernetes a plus.", RESUME, settings["profile"])
    assert out["source"] == "keywords"
    assert out["reorder"][0]["bullet"] == "Built C#/.NET REST APIs for billing"
    assert "Kubernetes" in out["missing_keywords"]
    assert all(r["bullet"] != "Organized team lunches" for r in out["reorder"])


def test_referral_template_without_key(settings, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    msg = referral.draft("Sam", "Shopify", "Software Engineer II", "https://x/1", "desc", settings["profile"])
    assert msg.startswith("Hi Sam") and "Shopify" in msg and "https://x/1" in msg
    assert 3 <= msg.count(". ") + msg.count("! ") + msg.count("? ") + 1 <= 5


# ----------------------------------------------------------------------- http
class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return self._body


def test_http_etag_retry_and_ua(db, monkeypatch):
    http = Http(cache=db, min_interval=0, retries=2)
    monkeypatch.setattr(Http, "_backoff", staticmethod(lambda *a: None))
    seen = []
    script = [_Resp(503), _Resp(200, {"ok": 1}, {"ETag": 'W/"abc"'}), _Resp(304)]

    class S:
        headers = {}

        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            seen.append(dict(headers))
            return script.pop(0)

    http._local.session = S()
    scoped = http.scoped()
    r = scoped.get("https://api.example/jobs", conditional=True)
    assert r.data == {"ok": 1} and len(seen) == 2  # retried the 503
    assert db.get_validators("https://api.example/jobs") == (None, None)  # not committed yet
    scoped.commit_validators()
    assert db.get_validators("https://api.example/jobs")[0] == 'W/"abc"'
    r = http.get("https://api.example/jobs", conditional=True)
    assert r.not_modified and seen[-1]["If-None-Match"] == 'W/"abc"'
    assert "jobpipe" in Http().user_agent


def test_http_connection_errors_become_httperror(monkeypatch):
    import requests
    from jobpipe.http import HttpError
    http = Http(min_interval=0, retries=1)
    monkeypatch.setattr(Http, "_backoff", staticmethod(lambda *a: None))

    class S:
        headers = {}

        def request(self, *a, **k):
            raise requests.exceptions.ProxyError("tunnel 403")

    http._local.session = S()
    with pytest.raises(HttpError) as e:
        http.get("https://x.example/jobs")
    assert e.value.status == 0 and "ProxyError" in str(e.value)
