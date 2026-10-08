from datetime import datetime, timezone

import pytest

from jobpipe import filters
from jobpipe.models import Posting
from jobpipe.normalize import (enrich, find_salary, guess_level, html_to_text, parse_location, parse_yoe,
                               to_iso, work_auth_note)


def P(title="Software Engineer", loc="Toronto, ON", desc="", **kw):
    return enrich(Posting(company="X", company_key="x", source="greenhouse", external_id="1", title=title,
                          url="https://x/1", description=desc, location_raw=loc, **kw))


# ------------------------------------------------------------------ locations
@pytest.mark.parametrize("raw,expected", [
    ("Toronto, ON", [("Toronto", "ON", "CA", False)]),
    ("Toronto, ON, CA", [("Toronto", "ON", "CA", False)]),
    ("San Francisco, CA", [("San Francisco", "CA", "US", False)]),
    ("Vancouver, British Columbia, Canada", [("Vancouver", "BC", "CA", False)]),
    ("Remote - Canada", [(None, None, "CA", True)]),
    ("Remote (US)", [(None, None, "US", True)]),
    ("US, WA, Seattle", [("Seattle", "WA", "US", False)]),
    ("CA, ON, Toronto", [("Toronto", "ON", "CA", False)]),
    ("New York, NY; Toronto, ON", [("New York", "NY", "US", False), ("Toronto", "ON", "CA", False)]),
    ("London, UK", [("London", None, "GB", False)]),
    ("Ottawa", [("Ottawa", "ON", "CA", False)]),
    ("Washington, DC", [("Washington", "DC", "US", False)]),
])
def test_parse_location(raw, expected):
    got = [(l.city, l.region, l.country, l.remote) for l in parse_location(raw)]
    assert got == expected


def test_remote_scope():
    assert P(loc="Remote - Canada").remote_scope == "Canada"
    assert P(loc="Remote (US or Canada)").remote_scope == "North America"
    assert P(loc="Remote - US").remote_scope == "US-only"
    assert P(loc="Remote - US", desc="Remote candidates based in Canada are welcome.").remote_scope == "North America"
    assert P(loc="Remote", desc="Open to anyone in North America.").remote_scope == "North America"
    assert P(loc="Remote").remote_scope == "Unspecified"
    assert P(loc="Toronto, ON").remote_scope is None


def test_work_mode():
    assert P(loc="Hybrid - Toronto, ON").work_mode == "hybrid"
    assert P(loc="Toronto", desc="This role is hybrid, 3 days in office.").work_mode == "hybrid"
    assert P(loc="Toronto", work_mode_hint="remote").work_mode == "remote"
    assert P(loc="Toronto").work_mode == "onsite"


# ---------------------------------------------------------------------- level
@pytest.mark.parametrize("title,level", [
    ("Software Engineer I", "I"), ("SDE I", "I"), ("Software Development Engineer II", "II"),
    ("Software Engineer 2", "II"), ("Intermediate Software Developer", "II"), ("Software Engineer III", "III"),
    ("Associate Software Engineer", "I"), ("Software Engineer, New Grad 2027", "new grad"),
    ("Senior Software Engineer", "senior"), ("Sr. Software Developer", "senior"), ("Staff Engineer", "staff+"),
    ("Engineering Manager", "manager"), ("Tech Lead, Payments", "lead"), ("Software Engineer Intern", "intern"),
    ("Software Engineer, Backend", "unspecified"), ("C# Developer", "unspecified"), ("C++ Software Engineer", "unspecified"),
])
def test_guess_level(title, level):
    assert guess_level(title) == level


# ----------------------------------------------------------------------- YOE
@pytest.mark.parametrize("desc,yoe", [
    ("You have 3+ years of professional software development experience.", 3),
    ("3-5 years of experience building web applications", 3),
    ("Minimum of five (5) years of software engineering experience", 5),
    ("At least 2 years experience with Python. 7+ years of experience preferred.", 2),
    ("Requirements:\n- 2+ years of coding experience\nPreferred Qualifications:\n- 6+ years of industry experience", 2),
    ("Our company has 25 years of experience in retail.", None),
    ("Bachelor's degree. Familiarity with SQL.", None),
    ("- 1+ years of experience with React\n- 5+ years of professional software development experience", 5),
])
def test_parse_yoe(desc, yoe):
    assert parse_yoe(desc) == yoe


def test_work_auth_and_salary():
    n = work_auth_note("Great team. Candidates must be authorized to work in the US. We will not sponsor visas.")
    assert n.startswith("no sponsorship; US work auth required")
    assert work_auth_note("Visa sponsorship is available for this role.").startswith("sponsorship available")
    assert work_auth_note("No mention here.") is None
    assert find_salary("Pay: $95,000 – $125,000 per year") == "$95,000 – $125,000"
    assert find_salary("range $140k - $180k") == "$140k - $180k"


def test_to_iso_and_html():
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    assert to_iso(1791400000000) == to_iso(1791400000)
    assert to_iso("Posted 3 Days Ago", now) == "2026-10-05T12:00:00+00:00"
    assert to_iso("Posted Yesterday", now) == "2026-10-07T12:00:00+00:00"
    assert to_iso("2026-10-07T13:00:00-04:00") == "2026-10-07T17:00:00+00:00"
    assert to_iso("garbage") is None
    assert html_to_text("&lt;ul&gt;&lt;li&gt;One&lt;/li&gt;&lt;li&gt;Two &amp;amp; three&lt;/li&gt;&lt;/ul&gt;") == "- One\n- Two & three"


# -------------------------------------------------------------------- filters
@pytest.fixture
def fcfg(settings):
    return settings.section("filters")


@pytest.mark.parametrize("title,ok", [
    ("Software Engineer II, Backend", True), ("SDE I", True), ("Full Stack Developer", True),
    ("Software Developer", True), ("Backend Engineer", True), ("Associate Software Engineer", True),
    ("Senior Software Engineer", False), ("Staff Software Engineer", False), ("Software Engineering Manager", False),
    ("Principal Engineer", False), ("Software Engineer Intern", False), ("Firmware Engineer", False),
    ("Data Analyst", False), ("Product Designer", False), ("Sr. Software Developer", False),
    ("Lead Software Engineer", False), ("Solutions Engineer", False),
])
def test_title_filter(fcfg, title, ok):
    assert filters.title_ok(title, fcfg).passes is ok


@pytest.mark.parametrize("loc,desc,ok", [
    ("Toronto, ON", "", True),
    ("Austin, TX", "", True),
    ("London, UK", "", False),
    ("Remote - Canada", "", True),
    ("Remote (North America)", "", True),
    ("Remote - US", "", False),
    ("Remote - US", "This role is open to remote candidates in Canada.", True),
    ("Remote", "", False),
    ("Remote - US; Toronto, ON", "", True),
    ("Bangalore, India", "", False),
    ("", "", True),
])
def test_location_filter(fcfg, loc, desc, ok):
    assert filters.location_ok(P(loc=loc, desc=desc), fcfg).passes is ok


def test_evaluate_reasons(fcfg):
    assert filters.evaluate(P(desc="5+ years of professional software engineering experience"), fcfg).reason == \
        "yoe: requires 5+ years"
    assert filters.evaluate(P(title="Senior Software Engineer"), fcfg).reason.startswith("title")
    assert filters.evaluate(P(loc="Remote - US"), fcfg).reason == "location: remote scope US-only"
    assert filters.evaluate(P(desc="2+ years of experience in software development"), fcfg).passes


def test_location_count_suffixes():
    assert [(l.city, l.region, l.country) for l in parse_location("Cedar Rapids, IA +2")] == [("Cedar Rapids", "IA", "US")]
    locs = parse_location("36 locations Lexington, KY; Boston, MA")
    assert [(l.city, l.country) for l in locs] == [("Lexington", "US"), ("Boston", "US")]
