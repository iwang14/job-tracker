"""Turn official job-alert emails into postings: the ToS-clean route for Amazon and Microsoft.

Set up a saved search with email alerts on amazon.jobs and on careers.microsoft.com (both offer
them). In Gmail, filter those emails into a label (e.g. `jobpipe`) and create an App Password.
Every full run reads the last few days of that label over IMAP (read-only, nothing is marked or
deleted) and extracts job links using the per-company rules in settings.yaml -> email_alerts.

Secrets: EMAIL_IMAP_USER, EMAIL_IMAP_PASSWORD (an app password, not your account password).
"""
from __future__ import annotations

import email
import html
import imaplib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

from ..models import Company, FetchResult, Posting, slugify

_A = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_JUNK_TITLES = re.compile(r"^(view|apply|see|learn|click|unsubscribe|manage|here|more|details|job details)\b", re.I)


@dataclass
class EmailSource:
    cfg: dict
    name: str = "email-alerts"
    source: str = "email"

    @property
    def key(self) -> str:
        return self.source

    def ingest(self, db, result, settings, now, stats) -> None:
        from ..pipeline import ingest
        ingest(db, None, result, settings, now, stats, bootstrap_all=False)


def configured(cfg: dict) -> bool:
    return bool(cfg.get("enabled")) and bool(os.environ.get("EMAIL_IMAP_USER")) and \
        bool(os.environ.get("EMAIL_IMAP_PASSWORD"))


def extract(msg: email.message.Message, rules: list[dict], companies: dict[str, Company]) -> list[Posting]:
    sender = (msg.get("From") or "").lower()
    try:
        sent = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError):
        sent = None
    bodies = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() in ("text/html", "text/plain"):
            payload = part.get_payload(decode=True) or b""
            bodies.append((part.get_content_type(), payload.decode(part.get_content_charset() or "utf-8", "replace")))
    out, seen = [], set()
    for rule in rules:
        if rule.get("from") and rule["from"].lower() not in sender:
            continue
        link_rx = re.compile(rule["link"], re.I)
        company = companies.get(slugify(rule["company"]))
        for ctype, body in bodies:
            links = _A.findall(body) if ctype == "text/html" else [(u, "") for u in re.findall(r"https?://\S+", body)]
            for href, text in links:
                m = link_rx.search(unquote(unquote(html.unescape(href))))
                if not m or m.group(1) in seen:
                    continue
                title = re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", text))).strip()
                if not title or _JUNK_TITLES.search(title) or len(title) > 150:
                    continue  # "View job" buttons: the same id usually also appears with its title
                seen.add(m.group(1))
                out.append(Posting(
                    company=company.name if company else rule["company"],
                    company_key=company.key if company else slugify(rule["company"]),
                    source=f"email:{slugify(rule['company'])}", external_id=m.group(1), title=title,
                    url=rule.get("url_template", "{url}").format(id=m.group(1), url=m.group(0)),
                    posted_at=sent, tier=company.tier if company else int(rule.get("tier", 3)),
                ))
    return out


def fetch(cfg: dict, companies: list[Company], imap_factory=None) -> FetchResult:
    by_key = {c.key: c for c in companies}
    imap = (imap_factory or (lambda: imaplib.IMAP4_SSL(cfg.get("imap_host", "imap.gmail.com"))))()
    try:
        imap.login(os.environ["EMAIL_IMAP_USER"], os.environ["EMAIL_IMAP_PASSWORD"])
        imap.select(f'"{cfg.get("folder", "INBOX")}"', readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=int(cfg.get("lookback_days", 3)))).strftime("%d-%b-%Y")
        _, data = imap.search(None, "SINCE", since)
        postings = []
        for num in (data[0] or b"").split()[-int(cfg.get("max_messages", 200)):]:
            _, parts = imap.fetch(num, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                postings += extract(email.message_from_bytes(raw), cfg.get("rules", []), by_key)
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    # emails only show new jobs, never the full board: never use them to close postings
    return FetchResult(postings, complete=False)
