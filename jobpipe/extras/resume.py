"""Resume tailoring helper: suggest which EXISTING bullets to reorder/reword and which keywords are
missing. It never writes new experience: every suggestion must quote a bullet that exists in
resume.md, and anything that doesn't is dropped before you see it.
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher

from .. import llm
from ..scoring import Skill, parse_skills, skill_matches

SCHEMA = {
    "type": "object",
    "properties": {
        "reorder": {"type": "array", "items": {"type": "object", "properties": {
            "bullet": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["bullet", "reason"], "additionalProperties": False}},
        "reword": {"type": "array", "items": {"type": "object", "properties": {
            "bullet": {"type": "string"}, "suggestion": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["bullet", "suggestion", "reason"], "additionalProperties": False}},
        "missing_keywords": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["reorder", "reword", "missing_keywords", "notes"],
    "additionalProperties": False,
}

SYSTEM = (
    "You help tailor a resume to a job description WITHOUT inventing experience. Rules: "
    "(1) 'bullet' must be copied verbatim from the resume. "
    "(2) 'reorder' lists existing bullets to move higher, most relevant first. "
    "(3) 'reword' may only rephrase or re-emphasize facts already in that bullet (e.g. use the JD's "
    "terminology for the same technology); never add technologies, numbers, scope or outcomes not in it. "
    "(4) 'missing_keywords' are JD skills/terms absent from the resume — list them so the candidate can "
    "decide whether they honestly apply; do not suggest adding them to bullets. "
    "(5) notes: one or two sentences."
)


def resume_bullets(resume_md: str) -> list[str]:
    return [re.sub(r"^\s*[-*•]\s+", "", ln).strip() for ln in resume_md.splitlines()
            if re.match(r"^\s*[-*•]\s+\S", ln)]


def _exists(bullet: str, bullets: list[str]) -> str | None:
    best = max(bullets, key=lambda b: SequenceMatcher(None, b.lower(), bullet.lower()).ratio(), default=None)
    if best and SequenceMatcher(None, best.lower(), bullet.lower()).ratio() >= 0.9:
        return best
    return None


def keyword_gaps(description: str, resume_md: str, profile: dict) -> tuple[list[str], list[str]]:
    """(JD skills you have on the resume, JD skills not on the resume) using the profile vocabulary
    plus a small list of common backend/full-stack terms."""
    vocab = parse_skills(profile)
    extra = profile.get("jd_vocabulary", [])
    vocab += [Skill(t) for t in extra if all(t.lower() != s.name.lower() for s in vocab)]
    in_jd = skill_matches(description, vocab)
    on_resume = {s.name for s in skill_matches(resume_md, in_jd)}
    return sorted(on_resume), sorted(s.name for s in in_jd if s.name not in on_resume)


def tailor(description: str, resume_md: str, profile: dict, model: str | None = None) -> dict:
    bullets = resume_bullets(resume_md)
    have, missing = keyword_gaps(description, resume_md, profile)
    result = {"reorder": [], "reword": [], "missing_keywords": missing, "keywords_matched": have,
              "notes": "", "source": "keywords"}
    raw = llm.generate(SYSTEM, f"<resume>\n{resume_md}\n</resume>\n\n<job_description>\n{description[:12000]}\n"
                               f"</job_description>", model=model, max_tokens=4000, schema=SCHEMA)
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if data:
            dropped = 0
            for item in data.get("reorder", []):
                real = _exists(item["bullet"], bullets)
                if real:
                    result["reorder"].append({"bullet": real, "reason": item["reason"]})
                else:
                    dropped += 1
            for item in data.get("reword", []):
                real = _exists(item["bullet"], bullets)
                if real:
                    result["reword"].append({"bullet": real, "suggestion": item["suggestion"], "reason": item["reason"]})
                else:
                    dropped += 1
            result["missing_keywords"] = sorted(set(missing) | {k for k in data.get("missing_keywords", [])
                                                                if k.lower() not in resume_md.lower()})
            result["notes"] = data.get("notes", "") + (f" ({dropped} suggestion(s) dropped: not in resume.md)"
                                                      if dropped else "")
            result["source"] = "llm"
    if not result["reorder"]:
        # keyword fallback: bullets mentioning the most JD skills first
        jd_terms = [s for s in parse_skills(profile) if s.name in set(have)]
        ranked = sorted(bullets, key=lambda b: -len(skill_matches(b, jd_terms)))
        result["reorder"] = [{"bullet": b, "reason": "mentions: " + ", ".join(s.name for s in skill_matches(b, jd_terms))}
                             for b in ranked if skill_matches(b, jd_terms)][:5]
    return result


def render(result: dict) -> str:
    out = ["## Move these existing bullets up"]
    out += [f"- {r['bullet']}\n  ↳ {r['reason']}" for r in result["reorder"]] or ["- (nothing stands out)"]
    out.append("\n## Possible rewording (same facts, JD's terminology)")
    out += [f"- {r['bullet']}\n  → {r['suggestion']}\n  ↳ {r['reason']}" for r in result["reword"]] or ["- (none)"]
    out.append("\n## JD keywords not on your resume (add only if true)")
    out.append(", ".join(result["missing_keywords"]) or "(none)")
    if result.get("keywords_matched"):
        out.append("\n## Already covered: " + ", ".join(result["keywords_matched"]))
    if result.get("notes"):
        out.append("\n" + result["notes"])
    return "\n".join(out)
