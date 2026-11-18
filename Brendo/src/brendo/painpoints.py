"""Step 5 --- the two most likely pain points for the contact.

Grounded in the company's VERIFIED facts + the contact's role. Code keeps only pains that
cite a populated field, so the hook ties to a real signal rather than a generic line (the
failure the build guards against). Fewer than two grounded pains = flag, not invent.
"""
from __future__ import annotations

from .llm import LLM
from .schemas import Contact, CompanyFields


def grounded(pains: list[dict], company: CompanyFields) -> tuple[list[dict], list[str]]:
    kept, dropped = [], []
    for p in pains:
        f = p.get("evidence_field")
        if p.get("pain") and f and company.has(f):
            kept.append(p)
        else:
            dropped.append(f"{p.get('pain')!r} cites '{f}', which is empty")
    return kept[:2], dropped


def identify(company: CompanyFields, contact: Contact, llm: LLM) -> tuple[list[dict], list[str]]:
    pains = llm.infer_pain_points(company.to_dict(), {"name": contact.name, "role": contact.role})
    return grounded(pains, company)
