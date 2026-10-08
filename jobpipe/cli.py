"""Command line: `python -m jobpipe <command>`. See README for the full list."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import alerts, notify, pipeline, sheets
from .config import CONFIG_DIR, ROOT, load_companies, load_settings
from .db import DB
from .http import Http, Response


def _db(args) -> DB:
    return DB(args.db)


def cmd_run(args) -> int:
    settings = load_settings()
    companies = load_companies()
    db = _db(args)
    sheet = None if args.no_sheet else sheets.open_sheet()
    if sheet is None and not args.no_sheet:
        logging.info("Google Sheet not configured (GOOGLE_SERVICE_ACCOUNT_JSON / SHEET_ID); skipping sync")
    tiers = {int(t) for t in args.tiers.split(",")} if args.tiers else None
    stats = pipeline.run(db, companies, settings, notify.from_env(), tiers=tiers, sheet=sheet,
                         community=not args.no_community)
    db.close()
    print(json.dumps(stats.__dict__, indent=1))
    return 0


def cmd_digest(args) -> int:
    settings = load_settings()
    db = _db(args)
    now = alerts.now_utc()
    acfg = settings.section("alerts")
    text = alerts.build_weekly(db, acfg, now, None) if args.weekly else alerts.build_digest(db, acfg, now)
    if args.send:
        notify.from_env().send(text)
    else:
        print(text)
    return 0


def cmd_detect(args) -> int:
    from .discovery.ats_detect import detect
    det = detect(args.url, Http(), name=args.name)
    if not det:
        print("No ATS detected. Try --name 'Company' to probe Greenhouse/Lever/Ashby slugs.")
        return 1
    from .discovery.seed import company_line
    print(f"# {det.ats_type} via {det.evidence}; verified={det.verified} jobs={det.job_count}")
    print(company_line({"name": args.name or det.board_token or "TODO", "tier": args.tier, "ats_type": det.ats_type,
                        "board_token": det.board_token, "careers_url": args.url}))
    return 0


def cmd_seed(args) -> int:
    from .discovery.seed import run_seed
    out = run_seed(Path(args.seed) if args.seed else None)
    print(f"Wrote {out}. Review it, then: python -m jobpipe approve {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    return 0


def cmd_approve(args) -> int:
    from .discovery.seed import approve
    added = approve(Path(args.file), args.names or None)
    print(f"Added {len(added)} companies to config/companies.yaml: {', '.join(added)}")
    return 0


def cmd_suggestions(args) -> int:
    db = _db(args)
    if args.reject:
        for n in args.reject:
            db.conn.execute("UPDATE suggestions SET status='rejected' WHERE company_key=? OR lower(name)=lower(?)", (n, n))
        db.commit()
    rows = db.query("SELECT * FROM suggestions WHERE status='pending' ORDER BY seen_count DESC, name")
    if args.export:
        import yaml
        payload = {"companies": [{"name": r["name"], "tier": 3, "ats_type": r["ats_type"] or "unknown",
                                  "board_token": r["board_token"], "_verified": False, "_sample": r["sample_url"]}
                                 for r in rows]}
        if args.verify:
            from .discovery.ats_detect import Detection, probe, verify
            http = Http()
            for c in payload["companies"]:
                d = verify(Detection(c["ats_type"], c["board_token"], "suggestion"), http) if c["board_token"] else None
                if not (d and d.verified):
                    d = probe(c["name"], http) or d
                if d:
                    c.update(ats_type=d.ats_type, board_token=d.board_token, _verified=d.verified)
        Path(args.export).write_text("# Suggested companies from community lists. Delete rows you don't want,\n"
                                     f"# then: python -m jobpipe approve {args.export}\n" +
                                     yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        print(f"Wrote {len(rows)} suggestions to {args.export}")
    else:
        for r in rows:
            print(f"{r['name']:<35} {r['ats_type'] or '?':<15} {r['board_token'] or '-':<30} seen={r['seen_count']}  {r['sample_url']}")
    return 0


class RecordingHttp(Http):
    def __init__(self, out_dir: Path):
        super().__init__(min_interval=0.5)
        self.out_dir = out_dir
        self.n = 0

    def request(self, method, url, **kw) -> Response:
        r = super().request(method, url, **kw)
        self.n += 1
        safe = url.split("://", 1)[-1].replace("/", "_")[:120]
        (self.out_dir / f"{self.n:03d}_{safe}.json").write_text(
            json.dumps({"method": method, "url": url, "params": kw.get("params"), "body": kw.get("json_body"),
                        "status": r.status, "data": r.data}, indent=1)[:5_000_000], encoding="utf-8")
        return r


def cmd_verify(args) -> int:
    """Hit each (enabled, or --company) board once and sanity-check the parsed output. No DB writes."""
    from .fetchers.base import FetchContext, FetcherDisabled
    companies = [c for c in load_companies() if (args.company and c.name.lower() in [n.lower() for n in args.company])
                 or (not args.company and c.enabled)]
    out_dir = Path(args.record) if args.record else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    bad = 0
    for c in companies:
        http = RecordingHttp(out_dir / c.key) if out_dir else Http()
        if out_dir:
            (out_dir / c.key).mkdir(parents=True, exist_ok=True)
        try:
            res = pipeline.fetch_one(c, http, FetchContext(max_pages=2, title_ok=lambda t: "engineer" in t.lower()))
            n = len(res.postings)
            with_desc = sum(1 for p in res.postings if p.description)
            sample = res.postings[0] if res.postings else None
            ok = n == 0 or (sample.title and sample.url)
            bad += not ok
            print(f"{'OK ' if ok else 'BAD'} {c.name:<28} {c.ats_type:<15} postings={n:<5} with_description={with_desc:<5}"
                  + (f" e.g. {sample.title!r} @ {sample.location_raw!r} {sample.url}" if sample else ""))
        except FetcherDisabled as e:
            print(f"SKIP {c.name:<27} {c.ats_type:<15} {e}")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"ERR {c.name:<28} {c.ats_type:<15} {type(e).__name__}: {str(e)[:200]}")
    return 1 if bad else 0


def _posting(db: DB, ref: str):
    row = db.get(ref) or next(iter(db.query("SELECT * FROM postings WHERE url=?", (ref,))), None)
    if row is None:
        sys.exit(f"posting {ref!r} not found in {db.path} (pull the state DB first — see README)")
    return row


def cmd_referral(args) -> int:
    from .extras.referral import draft
    db = _db(args)
    r = _posting(db, args.posting)
    print(draft(args.contact, r["company"], r["title"], r["url"], r["description"],
                load_settings().get("profile") or {}, relationship=args.relationship or ""))
    return 0


def cmd_tailor(args) -> int:
    from .extras.resume import render, tailor
    db = _db(args)
    r = _posting(db, args.posting)
    resume_md = Path(args.resume).read_text(encoding="utf-8")
    print(f"# {r['title']} — {r['company']}\n")
    print(render(tailor(r["description"], resume_md, load_settings().get("profile") or {})))
    return 0


def cmd_setup_sheet(args) -> int:
    ss = sheets.open_sheet()
    if ss is None:
        sys.exit("Set GOOGLE_SERVICE_ACCOUNT_JSON and SHEET_ID first (see README)")
    db = _db(args)
    db.kv_set("sheet_formatted", "0")
    res = sheets.sync(db, ss, load_settings().raw, datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    db.close()
    print(f"Sheet ready. Postings added: {res.added_postings}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="jobpipe")
    ap.add_argument("--db", default=str(ROOT / "state.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="fetch, filter, score, alert, sync sheet")
    p.add_argument("--tiers", help="comma list, e.g. 1 (Tier-1 fast lane)")
    p.add_argument("--no-sheet", action="store_true")
    p.add_argument("--no-community", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("digest", help="print (or --send) the daily digest / --weekly summary now")
    p.add_argument("--send", action="store_true")
    p.add_argument("--weekly", action="store_true")
    p.set_defaults(fn=cmd_digest)

    p = sub.add_parser("detect", help="detect the ATS behind a careers URL")
    p.add_argument("url")
    p.add_argument("--name")
    p.add_argument("--tier", type=int, default=3)
    p.set_defaults(fn=cmd_detect)

    p = sub.add_parser("seed", help="detect ATS for config/seed_companies.yaml -> candidates file to review")
    p.add_argument("--seed")
    p.set_defaults(fn=cmd_seed)

    p = sub.add_parser("approve", help="merge a reviewed candidates file into companies.yaml")
    p.add_argument("file")
    p.add_argument("names", nargs="*")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("suggestions", help="companies seen in community lists but not tracked")
    p.add_argument("--export")
    p.add_argument("--verify", action="store_true", help="with --export: verify/probe ATS tokens")
    p.add_argument("--reject", nargs="*")
    p.set_defaults(fn=cmd_suggestions)

    p = sub.add_parser("verify", help="hit each board once and sanity-check parsing")
    p.add_argument("--company", nargs="*")
    p.add_argument("--record", help="directory to save raw responses (fixtures)")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("referral", help="draft a referral request for a posting")
    p.add_argument("posting", help="posting ID (sheet column A) or URL")
    p.add_argument("--contact", required=True)
    p.add_argument("--relationship")
    p.set_defaults(fn=cmd_referral)

    p = sub.add_parser("tailor", help="resume tailoring suggestions for a posting")
    p.add_argument("posting")
    p.add_argument("--resume", default=str(CONFIG_DIR.parent / "resume.md"))
    p.set_defaults(fn=cmd_tailor)

    p = sub.add_parser("setup-sheet", help="create tabs, dropdowns and formatting")
    p.set_defaults(fn=cmd_setup_sheet)

    args = ap.parse_args(argv)
    return args.fn(args)
