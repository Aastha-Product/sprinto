"""Step 5 --- the two most likely pain points for the contact.

Grounded in the company's extracted facts + the contact's role, so the email hook
ties to a real signal rather than a generic line (the failure the build guards against).
"""
from __future__ import annotations

from .llm import LLM
from .schemas import Contact, CompanyFields


def identify(company: CompanyFields, contact: Contact, llm: LLM) -> list[dict]:
    pains = llm.infer_pain_points(company.to_dict(), {
        "name": contact.name, "role": contact.role,
    })
    return pains[:2]
