"""Build / extend companies.yaml from a seed list, with a human review step.

    python -m jobpipe seed                       # detect ATS for config/seed_companies.yaml
                                                 # -> writes config/companies.candidates.yaml
    (review/edit that file: fix tiers, delete rows you don't want)
    python -m jobpipe approve config/companies.candidates.yaml   # merge into companies.yaml

Nothing is added to companies.yaml without the approve step.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from ..config import CONFIG_DIR, load_companies, load_yaml
from ..http import Http
from ..models import slugify
from .ats_detect import Detection, detect, probe, verify


BIG = ("amazon", "microsoft", "google", "meta", "apple")


def detect_seed(entry: dict, http: Http) -> dict:
    name, ats, token = entry["name"], entry.get("ats_type"), entry.get("board_token")
    det: Detection | None = None
    if ats in BIG:
        det = Detection(ats, token, "seed")  # custom fetchers: nothing to verify against
    elif ats and token:
        det = verify(Detection(ats, token, "seed"), http)
    if det is None or not (det.verified or det.ats_type in BIG):
        found = detect(entry["careers_url"], http, name=name) if entry.get("careers_url") else probe(name, http)
        det = found or det
    out = {"name": name, "tier": int(entry.get("tier", 3))}
    if det:
        out.update(ats_type=det.ats_type, board_token=det.board_token)
        out["_verified"] = det.verified
        if det.job_count is not None:
            out["_jobs"] = det.job_count
    else:
        out.update(ats_type="unknown", board_token=None, _verified=False)
    if entry.get("careers_url"):
        out["careers_url"] = entry["careers_url"]
    return out


def run_seed(seed_path: Path | None = None, out_path: Path | None = None, http: Http | None = None) -> Path:
    seed = load_yaml(seed_path or CONFIG_DIR / "seed_companies.yaml") or []
    existing = {c.key for c in load_companies()}
    todo = [e for e in seed if slugify(e["name"]) not in existing]
    http = http or Http(min_interval=0.3)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda e: detect_seed(e, http), todo))
    out_path = out_path or CONFIG_DIR / "companies.candidates.yaml"
    header = ("# Review me! Fix tiers, delete companies you don't want, then run:\n"
              "#   python -m jobpipe approve " + str(out_path.name) + "\n"
              "# _verified: the board token answered with a job list. Unverified rows are added disabled.\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump({"companies": results}, f, sort_keys=False, allow_unicode=True, width=120)
    return out_path


def company_line(c: dict) -> str:
    """Render one company as a single flow-style YAML line (one line per company)."""
    keys = ["name", "tier", "ats_type", "board_token", "careers_url", "enabled"]
    extra = {k: v for k, v in c.items() if k not in keys and not k.startswith("_")}
    d = {k: c[k] for k in keys if c.get(k) is not None}
    d.update(extra)
    return "  - " + yaml.safe_dump(d, default_flow_style=True, sort_keys=False, width=1000).strip()


def approve(candidates_path: Path, names: list[str] | None = None, companies_path: Path | None = None) -> list[str]:
    companies_path = companies_path or CONFIG_DIR / "companies.yaml"
    data = load_yaml(candidates_path) or {}
    rows = data.get("companies", data) if isinstance(data, dict) else data
    existing = {c.key for c in load_companies(companies_path)}
    wanted = {slugify(n) for n in names} if names else None
    lines, added = [], []
    for r in rows or []:
        key = slugify(r["name"])
        if key in existing or (wanted is not None and key not in wanted):
            continue
        if not r.get("_verified"):  # incl. custom big-company fetchers: enable by hand after review
            r["enabled"] = False
        lines.append(company_line(r))
        added.append(r["name"])
    if lines:
        text = companies_path.read_text(encoding="utf-8") if companies_path.exists() else "companies:\n"
        if not text.endswith("\n"):
            text += "\n"
        companies_path.write_text(text + "\n".join(lines) + "\n", encoding="utf-8")
    return added
