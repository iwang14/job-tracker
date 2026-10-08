"""Turn raw fetcher output into normalized, comparable postings.

Pure functions (no I/O) so they are cheap to unit-test. Fetchers fill the raw fields of
`Posting`; `enrich()` derives level, locations, work mode, remote scope, YOE, salary and the
work-authorization note.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

from .models import Location, Posting

# ----------------------------------------------------------------- text utils
_BLOCK_TAGS = re.compile(r"(?i)<\s*(br|/p|/li|/h[1-6]|/div|/tr|/ul|/ol|p|h[1-6])\b[^>]*>")
_LI = re.compile(r"(?i)<\s*li\b[^>]*>")
_TAG = re.compile(r"<[^>]+>")


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)  # Greenhouse double-escapes its HTML
    s = _LI.sub("\n- ", s)
    s = _BLOCK_TAGS.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.splitlines()]
    out: list[str] = []
    for i, ln in enumerate(lines):
        if not ln:
            nxt = next((x for x in lines[i + 1:] if x), "")
            # one blank line max, and none between consecutive list items
            if not out or not out[-1] or (out[-1].startswith("- ") and nxt.startswith("- ")):
                continue
        out.append(ln)
    return "\n".join(out).strip()


def norm_title(t: str) -> str:
    t = t.lower()
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t)
    t = re.sub(r"[^a-z0-9+#]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ------------------------------------------------------------------- dates
_MONTHS = "january february march april may june july august september october november december".split()


def to_iso(value, now: datetime | None = None) -> str | None:
    """Best-effort conversion of the many date formats ATSes use into ISO-8601 UTC."""
    if value is None or value == "":
        return None
    now = now or datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat()
    s = str(value).strip()
    if re.fullmatch(r"\d{10,13}", s):
        return to_iso(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except ValueError:
        pass
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", s)  # "October 3, 2025" (Amazon)
    if m and m.group(1).lower() in _MONTHS:
        dt = datetime(int(m.group(3)), _MONTHS.index(m.group(1).lower()) + 1, int(m.group(2)), tzinfo=timezone.utc)
        return dt.isoformat()
    low = s.lower()  # Workday: "Posted Today", "Posted Yesterday", "Posted 3 Days Ago", "Posted 30+ Days Ago"
    if "today" in low or "just posted" in low:
        return now.replace(microsecond=0).isoformat()
    if "yesterday" in low:
        return (now - timedelta(days=1)).replace(microsecond=0).isoformat()
    m = re.search(r"(\d+)\+?\s*(day|hour|week|month)s?\s+ago", low)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"hour": timedelta(hours=n), "day": timedelta(days=n), "week": timedelta(weeks=n),
                 "month": timedelta(days=30 * n)}[unit]
        return (now - delta).replace(microsecond=0).isoformat()
    return None


# ---------------------------------------------------------------- locations
CA_PROVINCES = {
    "ON": "ontario", "BC": "british columbia", "QC": "quebec", "AB": "alberta", "MB": "manitoba",
    "SK": "saskatchewan", "NS": "nova scotia", "NB": "new brunswick", "NL": "newfoundland and labrador",
    "PE": "prince edward island", "YT": "yukon", "NT": "northwest territories", "NU": "nunavut",
}
US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
    "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee", "TX": "texas",
    "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}
_PROV_BY_NAME = {v: k for k, v in CA_PROVINCES.items()} | {"québec": "QC"}
_STATE_BY_NAME = {v: k for k, v in US_STATES.items()} | {"washington dc": "DC", "washington d.c.": "DC"}

COUNTRY_WORDS = {
    "canada": "CA", "can": "CA", "united states": "US", "united states of america": "US", "usa": "US",
    "us": "US", "u.s.": "US", "u.s.a.": "US", "america": "US",
    "united kingdom": "GB", "uk": "GB", "england": "GB", "scotland": "GB", "ireland": "IE", "germany": "DE",
    "france": "FR", "spain": "ES", "portugal": "PT", "netherlands": "NL", "poland": "PL", "india": "IN",
    "australia": "AU", "new zealand": "NZ", "singapore": "SG", "japan": "JP", "china": "CN", "brazil": "BR",
    "mexico": "MX", "israel": "IL", "argentina": "AR", "colombia": "CO", "philippines": "PH", "sweden": "SE",
    "denmark": "DK", "norway": "NO", "finland": "FI", "switzerland": "CH", "austria": "AT", "italy": "IT",
    "romania": "RO", "czech republic": "CZ", "czechia": "CZ", "south korea": "KR", "korea": "KR",
    "taiwan": "TW", "hong kong": "HK", "united arab emirates": "AE", "uae": "AE", "vietnam": "VN",
    "costa rica": "CR", "chile": "CL", "south africa": "ZA", "egypt": "EG", "nigeria": "NG", "kenya": "KE",
    "belgium": "BE", "greece": "GR", "turkey": "TR", "ukraine": "UA", "serbia": "RS", "lithuania": "LT",
    "estonia": "EE", "hungary": "HU", "bulgaria": "BG", "croatia": "HR", "malaysia": "MY", "indonesia": "ID",
    "thailand": "TH", "pakistan": "PK", "saudi arabia": "SA", "qatar": "QA", "luxembourg": "LU",
}
ISO3 = {"CAN": "CA", "USA": "US", "GBR": "GB", "IND": "IN", "DEU": "DE", "IRL": "IE", "AUS": "AU"}

# Cities that commonly appear without a region/country.
CITY_COUNTRY = {
    "toronto": ("CA", "ON"), "vancouver": ("CA", "BC"), "ottawa": ("CA", "ON"), "montreal": ("CA", "QC"),
    "montréal": ("CA", "QC"), "waterloo": ("CA", "ON"), "kitchener": ("CA", "ON"), "calgary": ("CA", "AB"),
    "edmonton": ("CA", "AB"), "winnipeg": ("CA", "MB"), "halifax": ("CA", "NS"), "victoria": ("CA", "BC"),
    "burnaby": ("CA", "BC"), "mississauga": ("CA", "ON"), "markham": ("CA", "ON"), "quebec city": ("CA", "QC"),
    "new york": ("US", "NY"), "new york city": ("US", "NY"), "nyc": ("US", "NY"), "san francisco": ("US", "CA"),
    "sf": ("US", "CA"), "seattle": ("US", "WA"), "bellevue": ("US", "WA"), "redmond": ("US", "WA"),
    "austin": ("US", "TX"), "boston": ("US", "MA"), "chicago": ("US", "IL"), "los angeles": ("US", "CA"),
    "mountain view": ("US", "CA"), "palo alto": ("US", "CA"), "sunnyvale": ("US", "CA"), "san jose": ("US", "CA"),
    "menlo park": ("US", "CA"), "cupertino": ("US", "CA"), "san diego": ("US", "CA"), "denver": ("US", "CO"),
    "atlanta": ("US", "GA"), "washington": ("US", "DC"), "arlington": ("US", "VA"), "pittsburgh": ("US", "PA"),
    "philadelphia": ("US", "PA"), "portland": ("US", "OR"), "salt lake city": ("US", "UT"), "miami": ("US", "FL"),
    "dallas": ("US", "TX"), "houston": ("US", "TX"), "raleigh": ("US", "NC"), "nashville": ("US", "TN"),
    "sf bay area": ("US", "CA"), "bay area": ("US", "CA"), "silicon valley": ("US", "CA"),
    "london": ("GB", None), "dublin": ("IE", None), "berlin": ("DE", None), "amsterdam": ("NL", None),
    "bangalore": ("IN", None), "bengaluru": ("IN", None), "hyderabad": ("IN", None), "sydney": ("AU", None),
    "singapore": ("SG", None), "tokyo": ("JP", None), "paris": ("FR", None), "tel aviv": ("IL", None),
}

_REMOTE_RE = re.compile(r"\b(remote|anywhere|work from home|wfh|virtual|distributed|telecommute)\b", re.I)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
_NA_RE = re.compile(r"north america|\bamericas\b|\b(us|usa|united states)\s*(/|or|&|and|\+)\s*canada\b|"
                    r"\bcanada\s*(/|or|&|and|\+)\s*(the\s+)?(us|usa|united states)\b", re.I)
_GLOBAL_RE = re.compile(r"\b(anywhere|worldwide|global(ly)?|international)\b", re.I)
_SPLIT_RE = re.compile(r"\s*(?:;|\||\n| / | or |•)\s*", re.I)


def _clean_piece(s: str) -> str:
    s = re.sub(r"\s*\+\s*\d+\s*(more|locations?)?\s*$", "", s, flags=re.I)  # "Austin, TX +2"
    s = re.sub(r"^\s*\d+\s+locations?\s*:?\s*", "", s, flags=re.I)        # "36 locations Lexington, KY"
    s = re.sub(r"\((.*?)\)", r", \1", s)
    s = re.sub(r"\b(remote|hybrid|on-?site|in-?office|office)\b\s*[-:–]?\s*", "", s, flags=re.I)
    s = re.sub(r"^\s*(in|within|from)\s+", "", s, flags=re.I)
    return s.strip(" ,-–:")


def parse_location(raw: str) -> list[Location]:
    """Parse a free-text location string, possibly containing several locations."""
    raw = (raw or "").strip()
    if not raw:
        return []
    pieces = [p for p in _SPLIT_RE.split(raw) if p and p.strip()]
    out: list[Location] = []
    for piece in pieces:
        loc = _parse_one(piece)
        if loc:
            out.append(loc)
    return out


def _parse_one(piece: str) -> Location | None:
    remote = bool(_REMOTE_RE.search(piece))
    body = _clean_piece(piece)
    loc = Location(remote=remote, raw=piece.strip())
    if not body:
        return loc
    parts = [p.strip() for p in re.split(r",|\s-\s", body) if p.strip()]
    low = [p.lower().strip(".") for p in parts]

    # Amazon style "US, WA, Seattle" / "CA, ON, Toronto"
    if len(parts) >= 2 and parts[0].upper() in ("US", "CA", "USA", "CAN") and len(parts[0]) <= 3:
        cc = ISO3.get(parts[0].upper(), parts[0].upper())
        if cc == "CA" and parts[1].upper() in CA_PROVINCES or cc == "US" and parts[1].upper() in US_STATES:
            loc.country, loc.region = cc, parts[1].upper()
            loc.city = parts[2] if len(parts) > 2 else None
            return loc

    has_prov = any(p.upper() in CA_PROVINCES and p.upper() != "CA" or lp in _PROV_BY_NAME
                   for p, lp in zip(parts, low))
    for i, (p, lp) in enumerate(zip(parts, low)):
        up = p.upper()
        # "New York, NY" / "Washington, DC" / "Quebec, QC": a leading region name followed by more parts is a city
        leading_name = i == 0 and len(parts) > 1
        if lp in COUNTRY_WORDS and not (lp == "ca"):
            loc.country = loc.country or COUNTRY_WORDS[lp]
        elif up in ISO3:
            loc.country = loc.country or ISO3[up]
        elif lp in _PROV_BY_NAME and not leading_name:
            loc.region, loc.country = _PROV_BY_NAME[lp], loc.country or "CA"
        elif lp in _STATE_BY_NAME and not leading_name:
            loc.region, loc.country = _STATE_BY_NAME[lp], loc.country or "US"
        elif len(p) == 2 and up in CA_PROVINCES and up != "CA":
            loc.region, loc.country = up, loc.country or "CA"
        elif len(p) == 2 and up == "CA":
            # "CA" is California unless a province is also present ("Toronto, ON, CA")
            if has_prov:
                loc.country = "CA"
            else:
                loc.region, loc.country = "CA", loc.country or "US"
        elif len(p) == 2 and up in US_STATES:
            loc.region, loc.country = up, loc.country or "US"
        elif loc.city is None and lp not in ("remote", "hybrid"):
            loc.city = p
    if loc.city and not loc.country:
        hit = CITY_COUNTRY.get(loc.city.lower())
        if hit:
            loc.country, loc.region = hit[0], loc.region or hit[1]
    if loc.city and loc.city.lower() in COUNTRY_WORDS:
        loc.country = loc.country or COUNTRY_WORDS[loc.city.lower()]
        loc.city = None
    if not loc.country:
        for word, cc in COUNTRY_WORDS.items():
            if len(word) > 3 and re.search(r"\b" + re.escape(word) + r"\b", body.lower()):
                loc.country = cc
                break
    return loc


# ------------------------------------------------------------ level / title
_LEVEL_PATTERNS = [
    ("intern", re.compile(r"\b(intern|internship|co-?op|apprentice)\b", re.I)),
    ("staff+", re.compile(r"\b(staff|principal|distinguished|fellow)\b", re.I)),
    ("manager", re.compile(r"\b(manager|director|head of|vp|vice president|chief)\b", re.I)),
    ("lead", re.compile(r"\b(lead|architect)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?)\b", re.I)),
    ("new grad", re.compile(r"\b(new grad(uate)?|graduate|university|college|campus|entry[- ]level|early[- ]career)\b", re.I)),
    ("III", re.compile(r"\b(iii|3|l5)\b", re.I)),
    ("II", re.compile(r"\b(ii|2|intermediate|mid[- ]level|l4)\b", re.I)),
    ("I", re.compile(r"\b(i|1|junior|jr\.?|associate|l3)\b(?!\s*\+)", re.I)),
]


def guess_level(title: str) -> str:
    t = re.sub(r"\b(c\+\+|c#|\.net|i/o)\b", " ", title, flags=re.I)
    for level, rx in _LEVEL_PATTERNS:
        if rx.search(t):
            return level
    return "unspecified"


# --------------------------------------------------------------------- YOE
_NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15}
_YOE_RE = re.compile(
    r"(?P<n>\d{1,2}|" + "|".join(_NUMWORDS) + r")\s*(?:\(\s*\d{1,2}\s*\)\s*)?(?:\+|plus)?\s*"
    r"(?:(?:-|–|to)\s*(?P<m>\d{1,2})\s*\+?\s*)?(?:years?|yrs?)\b"
    r"(?P<ctx>[^.\n;]{0,80})",
    re.I,
)
_YOE_CTX = re.compile(r"experience|industry|professional|software|development|engineering|programming|coding|"
                      r"building|working", re.I)
_PREFERRED_HDR = re.compile(r"prefer|nice[- ]to[- ]have|bonus|plus\b|stand out|ideal|desired|good to have|extra credit",
                            re.I)
_PREFERRED_INLINE = re.compile(r"prefer|nice to have|bonus|a plus|ideally|desired", re.I)


def _required_text(desc: str) -> str:
    """Drop 'Preferred qualifications' style sections so they don't count as requirements."""
    keep: list[str] = []
    skipping = False
    for line in desc.splitlines():
        s = line.strip()
        is_heading = bool(s) and len(s) <= 70 and not s.startswith("-") and (
            s.endswith(":") or s.istitle() or s.isupper() or not s.endswith("."))
        if is_heading and len(s.split()) <= 8:
            skipping = bool(_PREFERRED_HDR.search(s))
            if skipping:
                continue
        if not skipping:
            keep.append(line)
    return "\n".join(keep)


def parse_yoe(desc: str) -> int | None:
    """Minimum years of experience the posting *requires* (max over required statements)."""
    if not desc:
        return None
    text = _required_text(desc)
    best: int | None = None
    for m in _YOE_RE.finditer(text):
        ctx = m.group("ctx") or ""
        if not _YOE_CTX.search(ctx):
            continue
        # sentence containing the match: skip "preferred"/"bonus" statements
        start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
        sentence = text[start:m.end()]
        if _PREFERRED_INLINE.search(sentence) or _PREFERRED_INLINE.search(ctx):
            continue
        n_raw = m.group("n").lower()
        n = _NUMWORDS.get(n_raw) or int(n_raw)
        if n > 15:  # "company with 20 years of history"
            continue
        best = n if best is None else max(best, n)
    return best


# ------------------------------------------------------------ work auth
_AUTH_SENT = re.compile(
    r"[^.\n]*\b(sponsor(ship)?|visa|authori[sz]ed to work|work authori[sz]ation|employment authori[sz]ation|"
    r"legally (eligible|entitled|authori[sz]ed)|eligible to work|citizenship|security clearance|"
    r"u\.?s\.? person|green card|permanent resident)\b[^.\n]*", re.I)
_NO_SPONSOR = re.compile(r"(not|unable to|cannot|can't|won't|will not|does not|do not|no)\s+(be able to\s+)?"
                         r"(offer\s+|provide\s+)?(visa\s+)?sponsor|without\s+(visa\s+)?sponsorship|"
                         r"sponsorship is not (available|offered)", re.I)
_YES_SPONSOR = re.compile(r"(will|can|able to|happy to|do)\s+(provide\s+|offer\s+)?(visa\s+)?sponsor|"
                          r"sponsorship (is )?(available|provided|offered)", re.I)
_US_AUTH = re.compile(r"authori[sz]ed to work in the (u\.?s\.?|united states)|"
                      r"eligible to work in the (u\.?s\.?|united states)|u\.?s\.? citizen", re.I)
_CA_AUTH = re.compile(r"(authori[sz]ed|eligible|entitled) to work in canada", re.I)
_CLEARANCE = re.compile(r"security clearance|clearance required|ts/sci|secret clearance", re.I)


def work_auth_note(desc: str) -> str | None:
    if not desc:
        return None
    sentences = [m.group(0).strip(" -") for m in _AUTH_SENT.finditer(desc)]
    if not sentences:
        return None
    joined = " ".join(sentences)
    tags = []
    if _NO_SPONSOR.search(joined):
        tags.append("no sponsorship")
    elif _YES_SPONSOR.search(joined):
        tags.append("sponsorship available")
    if _US_AUTH.search(joined):
        tags.append("US work auth required")
    if _CA_AUTH.search(joined):
        tags.append("Canada work auth required")
    if _CLEARANCE.search(joined):
        tags.append("clearance")
    snippet = sentences[0][:180]
    return ("; ".join(tags) + " — " if tags else "") + snippet


# --------------------------------------------------------------- salary
_SALARY_RE = re.compile(
    r"(?:(?:CA|C|US|USD|CAD)\s?)?\$\s?\d{2,3}(?:,\d{3})+(?:\.\d\d)?\s*(?:-|–|—|to)\s*(?:(?:CA|C|US|USD|CAD)\s?)?"
    r"\$?\s?\d{2,3}(?:,\d{3})+(?:\.\d\d)?(?:\s*(?:USD|CAD))?"
    r"|\$\s?\d{2,3}(?:\.\d)?k\s*(?:-|–|to)\s*\$?\s?\d{2,3}(?:\.\d)?k", re.I)


def find_salary(desc: str) -> str | None:
    m = _SALARY_RE.search(desc or "")
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else None


# ------------------------------------------------------------ work mode
_CANADA_OK = re.compile(r"(remote|work from|based|located|reside|candidates)[^.\n]{0,60}\bcanada\b|"
                        r"\bcanada\b[^.\n]{0,40}(remote|welcome|eligible|ok\b|okay)", re.I)
_US_ONLY = re.compile(r"(remote|work from|based|located|reside)[^.\n]{0,40}\b(u\.?s\.?|united states|usa)\b(?![^.\n]{0,20}canada)"
                      r"|\b(us|u\.s\.)[- ]only\b|must (be located|reside) in the (u\.?s\.?|united states)", re.I)


def classify_work_mode(p: Posting) -> str:
    hint = (p.work_mode_hint or "").lower()
    if hint in ("remote", "hybrid", "onsite"):
        return hint
    if hint in ("on-site", "in office", "in-office", "office"):
        return "onsite"
    text = " ".join([p.location_raw, p.title])
    if _HYBRID_RE.search(text):
        return "hybrid"
    if p.locations and all(loc.remote for loc in p.locations):
        return "remote"
    if any(loc.remote for loc in p.locations) or _REMOTE_RE.search(text):
        return "remote"
    head = p.description[:1500]
    if _HYBRID_RE.search(head):
        return "hybrid"
    if re.search(r"\b(fully|100%)\s+remote\b|\bthis (role|position) is remote\b", head, re.I):
        return "remote"
    return "onsite"


def classify_remote_scope(p: Posting) -> str | None:
    if p.work_mode != "remote":
        return None
    text = p.location_raw + " " + p.title
    countries = {loc.country for loc in p.locations if loc.remote and loc.country} or \
        {loc.country for loc in p.locations if loc.country}
    if _NA_RE.search(text) or {"CA", "US"} <= countries:
        return "North America"
    if countries == {"CA"}:
        return "Canada"
    if countries == {"US"}:
        return "North America" if _CANADA_OK.search(p.description) else "US-only"
    if countries:
        return "Other"
    if _GLOBAL_RE.search(text):
        return "Global"
    desc = p.description
    if _NA_RE.search(desc):
        return "North America"
    if _CANADA_OK.search(desc):
        return "Canada"
    if _US_ONLY.search(desc):
        return "US-only"
    return "Unspecified"


# --------------------------------------------------------------- enrich
def enrich(p: Posting) -> Posting:
    if not p.locations:
        p.locations = parse_location(p.location_raw)
    if p.country_hint and p.locations:
        for loc in p.locations:
            if not loc.country:
                loc.country = p.country_hint.upper()
    elif p.country_hint and not p.locations:
        p.locations = [Location(country=p.country_hint.upper(), raw=p.location_raw)]
    countries = sorted({loc.country for loc in p.locations if loc.country})
    p.country = "|".join(countries)
    p.city = next((loc.city for loc in p.locations if loc.city), "") or ""
    p.level_guess = guess_level(p.title)
    p.work_mode = classify_work_mode(p)
    p.remote_scope = classify_remote_scope(p)
    p.yoe_min = parse_yoe(p.description)
    p.work_authorization_note = work_auth_note(p.description)
    p.salary = p.salary or find_salary(p.description)
    return p
