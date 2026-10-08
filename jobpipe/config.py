"""Load YAML configuration. Everything tunable lives in config/*.yaml."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import Company

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("JOBPIPE_CONFIG_DIR", ROOT / "config"))


def load_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def parse_company(d: dict) -> Company:
    known = {"name", "tier", "ats_type", "ats", "board_token", "token", "careers_url", "enabled"}
    return Company(
        name=d["name"],
        tier=int(d.get("tier", 3)),
        ats_type=(d.get("ats_type") or d.get("ats") or "unknown").lower(),
        board_token=d.get("board_token", d.get("token")),
        careers_url=d.get("careers_url"),
        enabled=bool(d.get("enabled", True)),
        options={k: v for k, v in d.items() if k not in known},
    )


def load_companies(path: Path | None = None) -> list[Company]:
    data = load_yaml(path or CONFIG_DIR / "companies.yaml") or {}
    rows = data.get("companies", data) if isinstance(data, dict) else data
    return [parse_company(r) for r in rows or []]


@dataclass
class Settings:
    raw: dict = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def section(self, key: str) -> dict:
        return self.raw.get(key) or {}


def load_settings(path: Path | None = None, profile_path: Path | None = None) -> Settings:
    raw = load_yaml(path or CONFIG_DIR / "settings.yaml") or {}
    profile = load_yaml(profile_path or CONFIG_DIR / "profile.yaml") or {}
    raw["profile"] = deep_merge(raw.get("profile") or {}, profile)
    return Settings(raw)
