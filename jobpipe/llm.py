"""Claude API helpers. Only used when ANTHROPIC_API_KEY is set; every caller has a fallback."""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-5-5"  # cheapest current Claude model
MAX_JD_CHARS = 20000                # JDs are short; this only bounds pathological pages

FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "fit": {"type": "integer", "description": "0-100 semantic fit of candidate to role"},
        "summary": {"type": "string", "description": "One line: why it fits / main gaps"},
    },
    "required": ["fit", "summary"],
    "additionalProperties": False,
}

FIT_SYSTEM = (
    "You assess how well a software developer's background fits a job posting. "
    "Judge the actual work (backend/full-stack, languages, domain, seniority), not keyword counts. "
    "fit: 0-100 where 80+ means a strong, realistic match for this candidate's level, 50 a stretch "
    "or partial match, below 30 a poor match. summary: ONE line under 140 characters in the form "
    "'Fits: ... | Gaps: ...'."
)


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


_client = None


def client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(max_retries=2, timeout=60)
    return _client


def _first_text(resp) -> str:
    return next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")


def profile_brief(profile: dict) -> str:
    skills = profile.get("skills") or {}
    names = lambda group: ", ".join(s if isinstance(s, str) else s["name"] for s in skills.get(group, []) or [])
    return (
        f"Candidate: {profile.get('headline', 'Software developer')}. "
        f"Experience: {profile.get('years_experience', '?')} years. "
        f"Targets: {', '.join(profile.get('target_roles', []))}.\n"
        f"Core skills: {names('core')}\nAlso: {names('secondary')}\nFamiliar: {names('nice')}\n"
        f"Notes: {profile.get('notes', '')}"
    )


def semantic_fit(title: str, company: str, description: str, profile: dict, model: str | None = None) -> tuple[int, str] | None:
    """Returns (fit, one-line summary) or None on any failure (caller falls back to keywords)."""
    if not available():
        return None
    import anthropic
    try:
        resp = client().messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=1024,
            system=FIT_SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": FIT_SCHEMA}},
            messages=[{"role": "user", "content": (
                f"{profile_brief(profile)}\n\n<job company=\"{company}\" title=\"{title}\">\n"
                f"{description[:MAX_JD_CHARS]}\n</job>"
            )}],
        )
        if resp.stop_reason == "refusal":
            log.warning("LLM refused to score %s / %s", company, title)
            return None
        data = json.loads(_first_text(resp))
        return max(0, min(100, int(data["fit"]))), str(data["summary"]).strip()[:200]
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        log.warning("LLM scoring failed: %s", e)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        log.warning("LLM returned unparseable output: %s", e)
    return None


def generate(system: str, prompt: str, model: str | None = None, max_tokens: int = 4000,
             schema: dict | None = None) -> str | None:
    """Free-form generation for the helper commands (referral, resume tailoring)."""
    if not available():
        return None
    import anthropic
    output_config: dict = {"effort": "medium"}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    try:
        resp = client().messages.create(
            model=model or DEFAULT_MODEL, max_tokens=max_tokens, system=system,
            output_config=output_config, messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            return None
        return _first_text(resp)
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        log.warning("LLM call failed: %s", e)
        return None
