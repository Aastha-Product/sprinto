"""Steps 6 & 7 --- draft the email, validate it, then send it to your own inbox.

Hard rule from the brief and the Nine Checks: `to:` is hardcoded to your own address; no
email reaches a real prospect in this build. The self-sent copy carries a "why" footer so
the Growth Lead can review the reasoning in the inbox (slide 35: review before any real send).

Drafts are checked in code (first name, length, cited hook, no placeholders); a failing
draft gets ONE redraft with the problems fed back, then the company is flagged.
Mock mode writes to data/outbox/ and the status is 'drafted', never 'sent'.
"""
from __future__ import annotations

import re
import smtplib
from email.mime.text import MIMEText

from .config import Settings
from .llm import LLM
from .schemas import Contact, CompanyFields, Email, CompanyResult, Qualification

MAX_WORDS = 120


def check(d: dict, company: CompanyFields, contact: Contact) -> list[str]:
    """Problems with a draft, as plain sentences the model can act on. Empty = OK."""
    problems = []
    body = d.get("body") or ""
    first = (contact.name or "").split()[0] if contact.name else ""
    if not d.get("subject"):
        problems.append("The subject is empty.")
    if first and not re.search(r"\b" + re.escape(first) + r"\b", body.split("\n\n")[0]):
        problems.append(f"The greeting must address the contact by first name ({first}).")
    words = len(body.split())
    if words > MAX_WORDS:
        problems.append(f"The body is {words} words; keep it under {MAX_WORDS}.")
    if re.search(r"\[[A-Za-z ]+\]|\{\{|\}\}", body + d.get("subject", "")):
        problems.append("Remove placeholder text like [Name] or {{...}}.")
    f = d.get("hook_evidence_field")
    if not f or not company.has(f):
        problems.append("The hook must rest on one of the given facts; "
                        f"'{f}' is empty for this company.")
    return problems


def draft(company: CompanyFields, contact: Contact, pains: list[dict], llm: LLM,
          result: CompanyResult) -> Email | None:
    cdict = company.to_dict()
    who = {"name": contact.name, "role": contact.role}
    d = llm.write_email(cdict, who, pains)
    problems = check(d, company, contact)
    if problems:
        result.log("draft", "redraft needed: " + " ".join(problems))
        d = llm.write_email(cdict, who, pains, feedback=problems)
        problems = check(d, company, contact)
        if problems:
            result.log("draft", "draft still failing checks --- flagged: " + " ".join(problems))
            return None
    email = Email(
        subject=d.get("subject", ""),
        body=d.get("body", ""),
        hook=d.get("hook", ""),
        hook_evidence_field=d.get("hook_evidence_field"),
        pains=[p.get("pain", "") for p in pains],
    )
    ev = company.evidence.get(email.hook_evidence_field or "", "")
    result.log("draft", f"hook rests on '{email.hook_evidence_field}'"
                        + (f" (evidence: {ev!r})" if ev else "") + f": {email.hook}")
    return email


def why_footer(company: CompanyFields, qual: Qualification, contact: Contact, email: Email,
               domain: str) -> str:
    lines = [
        "",
        "-" * 60,
        f"BRENDO REVIEW NOTE --- remove before sending to {contact.email}",
        f"Company: {company.name or domain} ({domain}) --- {qual.verdict}, rank {qual.rank}",
        "Why this company: " + "; ".join(qual.reasons),
        f"Why this person: {contact.name} ({contact.role}) --- {contact.reason}",
        "Why this email: hook rests on '" + (email.hook_evidence_field or "?") + "'"
        + (f" = {company.evidence.get(email.hook_evidence_field, '')!r}" if email.hook_evidence_field else ""),
        "Pains: " + " | ".join(email.pains),
    ]
    if qual.flags:
        lines.append("Check: " + " | ".join(qual.flags))
    return "\n".join(lines) + "\n"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "email").lower()).strip("-")[:50]


def send(email: Email, settings: Settings, result: CompanyResult, tag: str = "") -> Email:
    recipient = settings.send_to  # hardcoded to self --- never the prospect
    text = email.body + ("\n" + email.footer if email.footer else "")
    if settings.gmail_is_mock:
        outbox = settings.db_path.parent / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        path = outbox / f"{_slug(tag)}-{_slug(email.subject)}.txt"
        path.write_text(f"To: {recipient}\nSubject: {email.subject}\n\n{text}", encoding="utf-8")
        email.drafted = True
        email.message_id = f"mock:{path.name}"
        result.log("send", f"[mock] written to outbox/{path.name} (to: {recipient})")
        return email
    if settings.send_to_is_placeholder:
        result.log("send", f"refused: recipient '{recipient}' is a placeholder --- set BRENDO_SEND_TO "
                           "to your own address")
        return email
    try:
        msg = MIMEText(text)
        msg["Subject"] = email.subject
        msg["From"] = settings.gmail_address
        msg["To"] = recipient
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(settings.gmail_address, settings.gmail_app_password)
            s.send_message(msg)
        email.sent = True
        email.message_id = "gmail:sent"
        result.log("send", f"sent via Gmail to {recipient}")
    except Exception as exc:
        result.log("send", f"Gmail send failed: {str(exc)[:100]}")
    return email
