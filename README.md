# jobpipe: personal job-search pipeline

Finds new backend/full-stack SWE postings (entry to intermediate, Canada + US) on public job-board
APIs within about an hour. It filters and scores them, alerts you on Discord or Telegram, and keeps
everything in one Google Sheet where you track applications and see which channels get callbacks.

It never submits applications. It never scrapes LinkedIn, Indeed, or any site whose terms forbid it.

```
GitHub Actions cron (private repo)
 ├─ restore state.db  ◄── `state` branch (one gzip'd commit, force-pushed)
 ├─ fetch (parallel, ETag-cached, per-host rate limit)
 │    Greenhouse · Lever · Ashby · SmartRecruiters · Workday · Amazon* · Microsoft*  (+ community GitHub lists)
 ├─ normalize → dedupe (exact + fuzzy/re-post) → hard filters → score (keywords → Claude Haiku, cached)
 ├─ alerts: instant (Tier 1 or ≥80) · daily digest 08:00 Toronto · weekly summary · fetcher failures · follow-ups
 ├─ Google Sheet: Postings ⇄ Applications ⇄ Prep ⇄ Stats
 └─ save state.db  ──► `state` branch
```
\* opt-in per company; see [Big-company sites](#big-company-sites-and-terms-of-service). Also: SimplifyJobs listings JSON, job-alert emails (IMAP), sitemaps.

## Repo layout

```
config/
  companies.yaml        one line per company (name, tier, ats_type, board_token, careers_url)
  seed_companies.yaml   starter list for `jobpipe seed` (hints only, verified before use)
  settings.yaml         filters, score weights, alert rules, sheet options, community lists
  profile.yaml          your skills / headline (keyword scoring, LLM prompt, helpers)
jobpipe/
  db.py                 SQLite schema + queries (postings, llm_cache, http_cache, fetcher_health, …)
  http.py               polite client: User-Agent, per-host rate limit, retry/backoff, ETag
  fetchers/             one module per ATS / company site; registry in __init__.py
  normalize.py          locations, level, work mode, remote scope, YOE, salary, work-auth note
  dedupe.py             fuzzy duplicate + re-post detection
  filters.py            hard filters
  scoring.py, llm.py    0-100 score; Claude semantic fit with keyword fallback
  sheets.py             Google Sheet sync (ownership rules below)
  alerts.py, notify.py  alert logic; Discord / Telegram / console backends
  discovery/            seed list, community-list ingester, ATS detector
  extras/               referral-message and resume-tailoring helpers
  pipeline.py, cli.py   orchestration and `python -m jobpipe …`
scripts/state.sh        save/restore the state DB on the `state` branch
tests/                  pytest + fixtures (see tests/fixtures/ats/README.md)
```

## Data model

`postings` (one row per posting; key = hash of `(company, source, external_id)`, which is also unique):

| field | notes |
|---|---|
| id, company, source, external_id, tier | `source` = ATS type or `community:<list>` |
| title, level_guess | level ∈ I / II / III / new grad / senior / staff+ / lead / manager / intern / unspecified |
| country, city, locations_json, location_raw | country = `CA`, `US`, `CA\|US`, … |
| work_mode, remote_scope | remote/hybrid/onsite · Canada / North America / US-only / Global / Unspecified |
| posted_at, first_seen_at, last_seen_at | ISO-8601 UTC |
| url, description, salary, work_authorization_note, yoe_min | description stored only for postings that pass filters (keeps state small) |
| status, missed_runs, closed_at | closed after 2 consecutive successful fetches without it |
| duplicate_of, repost_of | fuzzy dedupe results |
| passes_filters, filter_reason, score, score_breakdown, fit_summary | |
| alerted_at, bootstrap, user_status | bootstrap = found on a board's first fetch (goes to digest, not instant alerts) |

Supporting tables: `llm_cache` (by posting ID + content hash, so nothing is scored twice), `http_cache`
(ETag/Last-Modified), `fetcher_health` (consecutive failures), `runs`, `kv` (digest/weekly bookkeeping),
`app_state` / `prep_items` / `reminders_sent` (sheet transitions fire once), and `suggestions`
(companies found in community lists).

## Persistence: SQLite on a dedicated branch

**Choice:** commit `state.db.gz` to a `state` branch at the end of every run, as a single
force-pushed commit with no history.

Why this over Turso or Supabase:
- **No extra account, secret or network dependency.** The workflow's own `GITHUB_TOKEN` is enough. A hosted DB would add a second service that can go down, change its free tier, or pause idle projects (Supabase pauses free projects after a week of inactivity).
- **Plain SQLite everywhere.** The same file runs in tests and in CI, and you can open it locally (`scripts/state.sh pull && sqlite3 state.db`). No ORM and no remote driver.
- **Small and bounded.** Only matching postings keep their description, the file is VACUUMed and gzipped, and the branch keeps a single commit, so the repo doesn't grow by one DB copy per run.
- **Safe enough for one writer.** `concurrency: jobpipe-state` serializes runs. Restore fails the job, and does *not* silently start fresh, if the branch exists but can't be fetched. That way a bad run can never overwrite good state.

Trade-off: there is no point-in-time history. If you want backups, download the `state` branch now and then. The sheet is the source of truth for your own actions anyway.

## Setup

### 1. Repo
1. Create a **private** repo and push this code to `main`. Scheduled workflows only run from the default branch.
2. Settings → Actions → General → Workflow permissions: **Read and write**. This lets the run push the `state` branch.

### 2. Google Sheet + service account
1. In <https://console.cloud.google.com/>, create a project, then enable the **Google Sheets API** and the **Google Drive API**.
2. IAM & Admin → Service accounts → **Create**. No roles are needed. Open it → Keys → Add key → JSON, and download the file.
3. Create an empty Google Sheet and **share it with the service account's email** (`…@….iam.gserviceaccount.com`) as **Editor**.
4. Copy the sheet ID, the long part of `https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`.
5. Optional: run locally once to create the tabs and formatting. This also happens on the first CI run.
   ```bash
   export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service-account.json)" SHEET_ID=...
   python -m jobpipe setup-sheet
   ```

### 3. Alerts webhook (pick one)
- **Discord:** Server settings → Integrations → Webhooks → New webhook → copy the URL.
- **Telegram:** talk to @BotFather → `/newbot` → copy the token. Message your bot once, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.

Without either, alerts are printed in the Actions log (`console` notifier). You can add another
backend by subclassing `notify.Notifier`.

### 4. GitHub secrets and variables
Settings → Secrets and variables → Actions:

| name | kind | value |
|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | secret | full contents of the JSON key |
| `SHEET_ID` | secret | sheet ID |
| `DISCORD_WEBHOOK_URL` | secret | Discord webhook URL, **or** |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | secrets | Telegram bot token and chat ID |
| `ANTHROPIC_API_KEY` | secret | optional; enables Claude scoring and the helpers |
| `NOTIFIER` | variable | optional: `discord` / `telegram` / `console` (auto-detected otherwise) |
| `JOBPIPE_CONTACT` | variable | optional: put a URL or email in the User-Agent so boards can reach you |
| `EMAIL_IMAP_USER`, `EMAIL_IMAP_PASSWORD` | secrets | optional: Gmail address + app password for [job-alert emails](#official-job-alert-emails-amazon-microsoft) |

### 5. First runs
1. Actions → **jobpipe** → Run workflow → mode **verify** (tick *record* to save raw responses as fixtures). Each enabled board is hit once and you get `OK` / `ERR` rows. **Fix any ERR rows in `companies.yaml`.** The build sandbox couldn't reach the job-board APIs, so every token is unverified. Shopify uses `auto`, so it fixes itself if any public board answers.
2. Run mode **run** once. The first fetch of each board is treated as a backfill: it fills the sheet and the digest without a burst of instant alerts.
3. From then on the cron takes over.

### Local development
```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
python -m jobpipe run --no-sheet          # alerts print to the console
scripts/state.sh pull                     # fetch the CI state DB for the helpers below
```

## Adding companies

Each company is one line in `config/companies.yaml`:
```yaml
  - {name: Stripe, tier: 2, ats_type: greenhouse, board_token: stripe}
  - {name: NVIDIA, tier: 2, ats_type: workday, board_token: nvidia/wd5/NVIDIAExternalCareerSite, search_text: software engineer}
```
- **Don't know the ATS?** Run `python -m jobpipe detect https://company.com/careers --name "Company" --tier 2`. It checks the URL and the page HTML for Greenhouse, Lever, Ashby, SmartRecruiters and Workday links, verifies the token against the API, falls back to probing slug guesses, and prints the line to paste.
- **Bulk seed:** `python -m jobpipe seed`, or the **seed** workflow mode, which uploads the file as an artifact. It writes `config/companies.candidates.yaml`. Review it, then run `python -m jobpipe approve config/companies.candidates.yaml [names…]`. Unverified rows are added with `enabled: false`.
- **From community lists:** every company that appears in the configured GitHub lists with a SWE-looking title, and isn't in `companies.yaml`, is recorded with its ATS detected from the apply link. Run the **suggestions** workflow mode (artifact: `suggested_companies.yaml`, tokens verified), or `python -m jobpipe suggestions` after `scripts/state.sh pull`. Delete the rows you don't want, then run `jobpipe approve suggested_companies.yaml`. `--reject NAME…` hides a suggestion permanently.
- **New ATS:** add a module in `jobpipe/fetchers/` returning `FetchResult` and register it in `fetchers/__init__.py`.

## Big-company sites and terms of service

Public ATS APIs (Greenhouse, Lever, Ashby, SmartRecruiters, Workday CXS) are published for this use
and need nothing extra. Company-specific endpoints are undocumented, so they go through a two-part
gate in `jobpipe/robots.py`:
1. **`tos_ok: true`** on the company in `companies.yaml`, which you set after reading that site's terms. Without it the fetcher is skipped silently, like a disabled company.
2. **robots.txt**, checked live on every run for jobpipe's User-Agent. If the site disallows the URL, or its robots.txt can't be read, nothing is fetched and the failure alert tells you after 3 runs.

| company | ToS-clean coverage (always on) | opt-in fetcher (`tos_ok: true`) |
|---|---|---|
| **Amazon** (Tier 1) | SimplifyJobs listings, with exact posted dates · **Amazon's own job-alert emails** (below) | `amazon`: amazon.jobs `search.json` |
| **Microsoft** | SimplifyJobs and SpeedyApply · **Microsoft's job-alert emails** | `microsoft`: Eightfold `api/apply/v2/jobs`, falling back to `api/pcsx/search` |
| **Atlassian** (Tier 1) | SimplifyJobs (Atlassian's iCIMS links) | `atlassian`: the `/endpoint/careers/listings` JSON behind atlassian.com/careers. Applications run on iCIMS (`careers-americas.icims.com`), which has no public API. |
| **Shopify** (Tier 1) | `ats_type: auto` probes the public Ashby, Lever, Greenhouse and SmartRecruiters APIs and **locks in whichever answers**. It re-probes if that board fails 3 runs in a row. | `sitemap`: Shopify hosts its own site (`shopify.com/careers/<slug>_<uuid>`), so this reads job URLs from its published sitemap. |
| Google, Meta, Apple | SimplifyJobs and SpeedyApply | none: no public API, and their terms restrict automated collection |

Links from all of these are reduced to the job ID before deduping (`amazon.jobs/jobs/<id>`,
`careers.microsoft.com/job/<id>`, iCIMS and Greenhouse IDs). That way the same job arriving from the
fetcher, a community list and an alert email shows up once.

### Official job-alert emails (Amazon, Microsoft)
Reading alerts that a company sends to your own inbox doesn't involve their site at all, so this is
the cleanest route.
1. On amazon.jobs, search *software development engineer* (USA + Canada) and **create a job alert**. Do the same on careers.microsoft.com.
2. In Gmail, add a filter: *from:(amazon.jobs OR microsoft.com) subject:job* → apply label **`jobpipe`**.
3. Create a Google **App Password**: Google Account → Security → 2-Step Verification → App passwords. IMAP is on by default in current Gmail.
4. Add secrets `EMAIL_IMAP_USER` (your address) and `EMAIL_IMAP_PASSWORD` (the app password), then set `email_alerts.enabled: true` in `settings.yaml`.

Each full run reads the last 3 days of that label over IMAP. It is read-only: messages aren't marked
read, moved or deleted. Job links are extracted with the per-company regexes in
`settings.yaml → email_alerts.rules`, and you can add rules for any other company's alert emails. Alerts
are as fresh as the company sends them; Amazon's are typically daily. The opt-in fetcher or SimplifyJobs
will usually see a posting sooner.

## Filters and scoring

**Hard filters** (`settings.yaml → filters`, applied before any LLM call):
- **Title:** must match an include term (SWE, SDE, software developer, backend, full-stack, …) and no exclude term (senior, staff, principal, lead, manager, director, intern, firmware, …). The level guess must not be senior, staff+, lead, manager or intern.
- **Experience:** the required YOE parsed from the description must be ≤ 4. "Preferred" / "nice to have" sections and sentences are ignored, so "3+ required, 5+ preferred" passes.
- **Location:** any on-site or hybrid city in CA or US; remote roles open to Canada, North America or globally. US-only remote is dropped unless the description says Canada is OK.
- **Work authorization:** mentions of sponsorship, visas, US work authorization or clearance are summarized in `work_authorization_note`, for example `no sponsorship; US work auth required — …`.

**Score (0–100)** = skill fit (≤70) + location (Vancouver +15, remote-Canada +10, Ottawa/Toronto +5, US +0) + tier (T1 +15, T2 +7) + freshness (≤24h +10, ≤72h +5, ≤7d +2). All weights are configurable.
- Skill fit is keyword overlap with `profile.yaml`. When `ANTHROPIC_API_KEY` is set, it is blended 35/65 with **Claude Haiku 5.5** (`claude-haiku-5-5`, the cheapest current model), which returns a semantic fit and a one-line *"Fits: … | Gaps: …"*.
- The LLM only sees postings that passed the hard filters. Results are cached by posting ID and content hash, and there is a cap of 40 calls per run.

## Google Sheet

| tab | who owns what |
|---|---|
| **Postings** | Filtered open jobs, newest first. The script owns every column except **Status** (dropdown: new / applying / applied / skipped) and **My notes**. Existing rows are updated cell by cell for script columns only, and columns are found by header, so you can add or reorder your own. Closed rows are struck through; untouched closed rows are pruned after 14 days. |
| **Applications** | When you set a posting to **applied**, it's copied here instantly if you installed the optional Apps Script (below), otherwise by the next hourly run. The copy has Date applied = today, Stage = applied, Channel = company site, and a follow-up next action in 10 days. **After that the row is yours.** The script only fills *Last update* and *First response* when it sees the Stage change. You can add manual rows (leave Posting ID blank). Overdue next actions turn **red**. |
| **Prep** | Moving to **OA** or **tech screen** appends a checklist row: research recent interview reports for that company and role, map STAR stories to the JD, and so on. |
| **Stats** | Rewritten each run: callback rate by channel, tier, country and resume version; median days to first response; stage counts; applications per week. A *callback* means the application reached OA, recruiter screen, tech screen, onsite or offer, and it still counts if the application was later rejected. |

### Instant "applied → Applications" (optional)
Paste `apps_script/Code.gs` into the sheet (Extensions → Apps Script → Save). It's an `onEdit` simple
trigger, so it needs no authorization and copies the row the moment you pick **applied**. The hourly
Python sync does the same copy and checks *Posting ID* first, so the two never duplicate, and if the
script ever breaks nothing is lost.

### Less noise
`sheet.min_score` (default 25) and `alerts.digest_min_score` (default 25) keep low-scoring postings out of the
Postings tab and the digest. These are mostly community-list rows with no description. Tier 1 is always shown,
and rows already in the sheet are never removed by this. The weekly summary reports how many were hidden.

Community postings whose apply link is a public Greenhouse, Lever or Ashby job also get their **full
description fetched** (`community_enrich`, up to 15 per run). They then score properly, and the YOE
filter can drop the ones that need 5+ years.

## Alerts

- **Instant:** a Tier-1 posting, or score ≥ 80, that is new, not a duplicate or re-post, and posted within 7 days. The alert shows title, company, location, score, posted time, salary, fit line, work-auth note and link. Several are batched into one message.
- **Daily digest:** the first run at or after 08:00 America/Toronto (DST-safe) sends everything new since the last digest, sorted by score and split into 🇨🇦 Canada & remote-friendly / 🇺🇸 US. Follow-up reminders go out at the same time.
- **Weekly (Monday):** funnel (new → passed filters → alerted, closed), top reasons postings were filtered out, the companies posting the most, and application stats.
- **Fetcher failing:** a board that fails 3 runs in a row triggers an alert, plus a recovery notice when it starts working again.
- **Follow-ups:** an active application with no update for 10 days, an interview stage with no next action, or an overdue next action.

## Helpers (run locally after `scripts/state.sh pull`)

```bash
python -m jobpipe referral <posting-id-or-url> --contact "Sam" --relationship "former teammate at X"
python -m jobpipe tailor  <posting-id-or-url> --resume resume.md
python -m jobpipe digest [--weekly] [--send]
```
- **referral** drafts a 3–4 sentence message for you to edit and send yourself. It uses a template when no API key is set.
- **tailor** suggests which **existing** `resume.md` bullets to move up or reword, and lists JD keywords that are missing from your resume. Any suggestion that doesn't quote a real bullet is dropped, so it never invents experience.

## Schedule and cost

GitHub Free gives private repos **2,000 Actions minutes per month** (Pro gives 3,000). Each job is
billed **rounded up to the next minute**, so the number of runs matters more than run length.

| workflow | schedule | runs/month | est. duration | billed min/month |
|---|---|---|---|---|
| full run (all companies, community lists, sheet, digest) | `7 * * * *` hourly | 720 | 45–75 s (setup ~15 s, fetch ~20–40 s, sheet ~5 s) | 720–1,440 |
| Tier-1 fast lane (no sheet, no community lists) | `37 11-23,0-3 * * *` (07:37–23:37 Toronto) | 510 | 25–40 s | 510 |
| tests on push | per push | ~20 | ~40 s | ~20 |
| **total** | | | | **≈1,250–1,970** |

- Typical runs bill 1 minute, so expect around 1,300 min/month (about 65% of the free tier). The upper bound assumes every hourly run spills into a second minute.
- **If you get close to the limit:** drop the Tier-1 lane to 09–21 local, or slow the hourly cron to every 2 hours outside 07–23 local. A public repo has unlimited free minutes, but it would expose your companies, profile and state.
- Check usage under Settings → Billing → Usage. Each run also prints per-phase timings in its stats.
- GitHub can delay scheduled runs by 5–20 minutes at busy times. Combined with the hourly and :37 Tier-1 runs, worst-case discovery time is about 1 hour for most boards and about 30–45 minutes for Tier 1.
- **Claude cost:** about 2.5K input and 0.3K output tokens per new matching posting with Haiku 5.5 ($0.10 / $0.50 per MTok) is about $0.0004 per posting. At around 30 per day, that's under $0.50/month.

## Tests

```bash
python -m pytest
```
- Each fetcher is tested against recorded-shape fixtures in `tests/fixtures/ats/`. These are synthetic, written from each API's documented response shape; replace them with real recordings via `verify --record`.
- The community-list parser is tested on real excerpts of the SimplifyJobs (HTML) and SpeedyApply (Markdown) READMEs.
- Other coverage:
  - normalization and filters
  - dedupe and re-posts
  - scoring and LLM caching
  - end-to-end runs: bootstrap, instant alerts, closing after 2 misses, the 304 path, failure alerts, the digest
  - sheet sync against an in-memory fake: your edits preserved, the applied → Applications copy, stage transitions, prep rows, stats, pruning
  - alerts: DST-safe digest timing, reminders
  - the helpers' never-invent guard
