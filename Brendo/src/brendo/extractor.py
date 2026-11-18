"""Step 2 --- structured extraction, plus merge across pages.

The LLM fills named fields from clean text. Across loop iterations we MERGE: a later
page fills only the fields still empty, so the careers page can supply 'stage' without
overwriting what the homepage already established.
"""
from __future__ import annotations

from .llm import LLM
from .schemas import CompanyFields


def extract(text: str, llm: LLM) -> CompanyFields:
    return llm.extract_fields(text)


def merge(acc: CompanyFields, new: CompanyFields) -> None:
    """Fill empty fields in `acc` from `new`; never overwrite an existing value."""
    for name in ("name", "is_saas", "buyer_description", "funding_stage",
                 "open_roles_count", "enterprise_customers"):
        if not acc.has(name) and new.has(name):
            setattr(acc, name, getattr(new, name))
            if name in new.evidence:
                acc.evidence[name] = new.evidence[name]
    # certifications: union
    for c in new.certifications:
        if c not in acc.certifications:
            acc.certifications.append(c)
    if new.certifications and "certifications" in new.evidence:
        acc.evidence.setdefault("certifications", new.evidence["certifications"])
