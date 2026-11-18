"""Steps 6 & 7 --- draft the email, then send via Gmail to your own inbox.

Hard rule from the brief and the Nine Checks: send is hardcoded to your own address;
no email reaches a real prospect in this build. Mock mode writes the email to
data/outbox/ so the full path is demonstrable with no Gmail credentials.
"""
from __future__ import annotations

import re
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

from .config import Settings
from .llm import LLM
from .schemas import Contact, CompanyFields, Email, CompanyResult


def draft(company: CompanyFields, contact: Contact, pains: list[dict], llm: LLM,
          result: CompanyResult) -> Email:
    d = llm.write_email(company.to_dict(), {"name": contact.name, "role": contact.role}, pains)
    email = Email(
        subject=d.get("subject", ""),
        body=d.get("body", ""),
        hook=d.get("hook", ""),
        pains=[p.get("pain", "") for p in pains],
    )
    result.log("draft", f"hook: {email.hook}")
    return email


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "email").lower()).strip("-")[:50]


def send(email: Email, settings: Settings, result: CompanyResult, tag: str = "") -> Email:
    recipient = settings.send_to  # hardcoded to self
    if settings.gmail_is_mock:
        outbox = settings.db_path.parent / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        path = outbox / f"{_slug(tag)}-{_slug(email.subject)}.txt"
        path.write_text(
            f"To: {recipient}\nSubject: {email.subject}\n\n{email.body}\n",
            encoding="utf-8",
        )
        email.sent = True
        email.message_id = f"mock:{path.name}"
        result.log("send", f"[mock] written to outbox/{path.name} (to: {recipient})")
        return email
    try:
        msg = MIMEText(email.body)
        msg["Subject"] = email.subject
        msg["From"] = settings.gmail_address
        msg["To"] = recipient
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(settings.gmail_address, settings.gmail_app_password)
            s.send_message(msg)
        email.sent = True
        email.message_id = "gmail:sent"
        result.log("send", f"sent via Gmail to {recipient}")
    except Exception as exc:
        result.log("send", f"Gmail send failed: {str(exc)[:100]}")
        email.sent = False
    return email
