"""Step 2 --- structured extraction, evidence verification, and merge across pages.

The model fills named fields from clean text. Before a field is trusted, CODE checks that
its evidence snippet really appears in the page it came from (slide 30: "a model might be
confident about an empty page"). A field whose evidence is missing or not on the page is
dropped and the drop is logged, so a hallucinated "Series B" can never qualify a company.

Across loop iterations we MERGE: a later page fills only the fields still empty, so the
careers page can supply 'stage' without overwriting what the homepage established.
"""
from __future__ import annotations

import re

from .llm import LLM
from .schemas import CompanyFields

_SCALARS = ("is_saas", "buyer_description", "funding_stage", "open_roles_count",
            "enterprise_customers")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _on_page(snippet: str, page_norm: str) -> bool:
    sn = _norm(snippet)
    return bool(sn) and sn in page_norm


def extract(text: str, llm: LLM) -> CompanyFields:
    return llm.extract_fields(text)


def verify(fields: CompanyFields, page_text: str) -> list[str]:
    """Drop every field the page does not support. Returns human-readable drop notes."""
    page = _norm(page_text)
    dropped: list[str] = []

    for name in _SCALARS:
        if not fields.has(name):
            continue
        ev = fields.evidence.get(name)
        if not ev or not _on_page(ev, page):
            dropped.append(f"{name}={getattr(fields, name)!r} (evidence not found on page)")
            setattr(fields, name, None)
            fields.evidence.pop(name, None)

    # list fields: keep only items whose own words are on the page
    certs = [c for c in fields.certifications if _on_page(c, page) or _on_page(c.replace(" ", ""), page)]
    for c in fields.certifications:
        if c not in certs:
            dropped.append(f"certification {c!r} (not on page)")
    fields.certifications = certs
    if not certs:
        fields.evidence.pop("certifications", None)

    sigs = [s for s in fields.compliance_signals if _on_page(s, page)]
    for s in fields.compliance_signals:
        if s not in sigs:
            dropped.append(f"compliance signal {s!r} (not on page)")
    fields.compliance_signals = sigs
    if sigs:
        fields.evidence["compliance_signals"] = sigs[0]
    else:
        fields.evidence.pop("compliance_signals", None)
    return dropped


def merge(acc: CompanyFields, new: CompanyFields) -> None:
    """Fill empty fields in `acc` from `new`; never overwrite an existing value."""
    for name in ("name",) + _SCALARS:
        if not acc.has(name) and new.has(name):
            setattr(acc, name, getattr(new, name))
            if name in new.evidence:
                acc.evidence[name] = new.evidence[name]
    # list fields: union
    for c in new.certifications:
        if c not in acc.certifications:
            acc.certifications.append(c)
    if new.certifications and "certifications" in new.evidence:
        acc.evidence.setdefault("certifications", new.evidence["certifications"])
    for s in new.compliance_signals:
        if s not in acc.compliance_signals:
            acc.compliance_signals.append(s)
    if new.compliance_signals and "compliance_signals" in new.evidence:
        acc.evidence.setdefault("compliance_signals", new.evidence["compliance_signals"])
