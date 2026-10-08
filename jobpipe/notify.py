"""Pluggable notifiers. Pick with NOTIFIER=discord|telegram|console (default: whichever is configured).

Messages are written once as lightweight Markdown-ish text; each backend adapts and chunks it.
"""
from __future__ import annotations

import logging
import os
import re
import time

import requests

log = logging.getLogger(__name__)


class Notifier:
    max_len = 1900

    def send(self, text: str) -> None:
        for chunk in chunk_text(text, self.max_len):
            self._send(chunk)

    def _send(self, text: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    max_len = 100000

    def __init__(self):
        self.sent: list[str] = []

    def _send(self, text: str) -> None:
        self.sent.append(text)
        print("----- notification -----\n" + text)


class DiscordNotifier(Notifier):
    max_len = 1900  # Discord hard limit is 2000 chars per message

    def __init__(self, webhook_url: str):
        self.url = webhook_url

    def _send(self, text: str) -> None:
        for attempt in range(4):
            r = requests.post(self.url, json={"content": text, "flags": 4}, timeout=15)  # 4 = suppress embeds
            if r.status_code == 429:
                time.sleep(min(float(r.json().get("retry_after", 2)), 10))
                continue
            r.raise_for_status()
            return


class TelegramNotifier(Notifier):
    max_len = 3800  # Telegram limit is 4096

    def __init__(self, token: str, chat_id: str):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id

    def _send(self, text: str) -> None:
        html = md_to_telegram_html(text)
        r = requests.post(self.url, json={"chat_id": self.chat_id, "text": html, "parse_mode": "HTML",
                                          "disable_web_page_preview": True}, timeout=15)
        if r.status_code == 429:
            time.sleep(min(float(r.json().get("parameters", {}).get("retry_after", 2)), 10))
            r = requests.post(self.url, json={"chat_id": self.chat_id, "text": html, "parse_mode": "HTML",
                                              "disable_web_page_preview": True}, timeout=15)
        r.raise_for_status()


def md_to_telegram_html(text: str) -> str:
    esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    esc = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', esc)
    esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
    return esc


def chunk_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def from_env() -> Notifier:
    kind = (os.environ.get("NOTIFIER") or "").lower()
    discord = os.environ.get("DISCORD_WEBHOOK_URL")
    tg_token, tg_chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if kind == "discord" or (not kind and discord):
        if discord:
            return DiscordNotifier(discord)
    if kind == "telegram" or (not kind and tg_token and tg_chat):
        if tg_token and tg_chat:
            return TelegramNotifier(tg_token, tg_chat)
    if kind and kind != "console":
        log.warning("NOTIFIER=%s but its credentials are missing; printing to console", kind)
    return ConsoleNotifier()


class SafeNotifier(Notifier):
    """Never let a webhook outage fail the run (state must still be saved)."""

    def __init__(self, inner: Notifier):
        self.inner = inner
        self.failures = 0

    def send(self, text: str) -> bool:
        try:
            self.inner.send(text)
            return True
        except Exception as e:  # noqa: BLE001
            self.failures += 1
            log.error("notification failed: %s", e)
            return False
