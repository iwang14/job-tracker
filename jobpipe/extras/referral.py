"""Draft a short referral-request message for you to edit and send yourself. Never sends anything."""
from __future__ import annotations

from .. import llm

SYSTEM = (
    "You draft short, warm, specific referral-request messages for a job seeker to send to a contact. "
    "Write 3-4 sentences, plain text, no subject line, no placeholders other than ones given. "
    "Only state facts present in the candidate profile; never invent shared history or achievements. "
    "Make it easy to say yes: mention the role title and link, one relevant strength, and offer to send "
    "a resume."
)


def template(contact: str, company: str, title: str, url: str, profile: dict) -> str:
    strength = profile.get("referral_pitch") or profile.get("headline") or "a software developer"
    return (
        f"Hi {contact}, I hope you're doing well! I noticed {company} is hiring for a {title} role ({url}) "
        f"and it looks like a great fit for my background as {strength}. "
        f"Would you be comfortable referring me, or sharing who the best person to talk to would be? "
        f"Happy to send my resume and a short blurb to make it easy — thanks so much either way!"
    )


def draft(contact: str, company: str, title: str, url: str, description: str, profile: dict,
          relationship: str = "", model: str | None = None) -> str:
    prompt = (
        f"Contact name: {contact}\nRelationship/context: {relationship or 'unspecified (keep it neutral)'}\n"
        f"Role: {title} at {company}\nLink: {url}\n\n{llm.profile_brief(profile)}\n"
        f"Referral pitch (facts you may use): {profile.get('referral_pitch', '')}\n\n"
        f"<job_description>\n{description[:8000]}\n</job_description>"
    )
    out = llm.generate(SYSTEM, prompt, model=model, max_tokens=1500)
    return (out or "").strip() or template(contact, company, title, url, profile)
