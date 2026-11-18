"""LLM access, with a real (Anthropic Claude) client and an offline heuristic mock.

Only the *reasoning* steps use a model: extract fields, infer pain points, write the
email. Qualification itself is deterministic code (see qualifier.py). The model is
asked for JSON; parsing failures are a retry then a flag --- never a silent guess.
"""
from __future__ import annotations

import json
import re
from typing import Protocol

from .config import Settings
from .schemas import CompanyFields


# ---------- JSON helpers ----------

def _loads(text: str) -> dict:
    """Pull the first JSON object out of a model reply."""
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def _fields_from_dict(d: dict) -> CompanyFields:
    return CompanyFields(
        name=d.get("name"),
        is_saas=d.get("is_saas"),
        buyer_description=d.get("buyer_description"),
        certifications=list(d.get("certifications") or []),
        funding_stage=d.get("funding_stage"),
        open_roles_count=d.get("open_roles_count"),
        enterprise_customers=d.get("enterprise_customers"),
        evidence=dict(d.get("evidence") or {}),
    )


class LLM(Protocol):
    def extract_fields(self, text: str) -> CompanyFields: ...
    def infer_pain_points(self, company: dict, contact: dict) -> list[dict]: ...
    def write_email(self, company: dict, contact: dict, pains: list[dict]) -> dict: ...


# ---------- Real client (Anthropic Claude) ----------

_EXTRACT_SYS = (
    "You extract structured facts about a company from its own website text, for a "
    "B2B sales-qualification task. Return ONLY JSON with keys: name (str|null), "
    "is_saas (bool|null), buyer_description (str|null = who buys the product), "
    "certifications (list of named standards like 'SOC 2','ISO 27001','GDPR'), "
    "funding_stage (str|null e.g. 'Series B'), open_roles_count (int|null), "
    "enterprise_customers (bool|null), evidence (object mapping each non-null field to "
    "the short verbatim snippet that supports it). Use null when the text does not say; "
    "never infer beyond the text."
)


class RealLLM:
    def __init__(self, settings: Settings):
        import anthropic  # lazy; only needed in real mode
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.llm_model
        self.model_draft = settings.llm_model_draft

    def _json(self, model: str, system: str, user: str, max_tokens: int = 1024) -> dict:
        last = None
        for _ in range(2):
            try:
                msg = self.client.messages.create(
                    model=model, max_tokens=max_tokens, system=system,
                    messages=[{"role": "user", "content": user}],
                )
                return _loads(msg.content[0].text)
            except Exception as exc:  # parse or API error -> one retry
                last = exc
        raise RuntimeError(f"LLM JSON call failed: {last}")

    def ping(self) -> bool:
        """Tiny call to confirm the key + model work (used by `--check`)."""
        self.client.messages.create(
            model=self.model, max_tokens=5,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True

    def extract_fields(self, text: str) -> CompanyFields:
        d = self._json(self.model, _EXTRACT_SYS, text[:12000])
        return _fields_from_dict(d)

    def infer_pain_points(self, company: dict, contact: dict) -> list[dict]:
        sys = (
            "Given a company's facts and a contact's role, name the TWO most likely "
            "pain points this person has that Sprinto (automated SOC 2 / ISO 27001 / "
            "GDPR compliance for SaaS) could solve. Return ONLY JSON: "
            '{"pains":[{"pain":str,"evidence":str}, ...]} --- each pain tied to a given fact.'
        )
        user = json.dumps({"company": company, "contact": contact})
        return self._json(self.model, sys, user).get("pains", [])[:2]

    def write_email(self, company: dict, contact: dict, pains: list[dict]) -> dict:
        sys = (
            "Write a short, personalised cold email from Sprinto to the contact, by name, "
            "with ONE specific hook grounded in a given fact or pain. Human, not salesy; "
            "under 120 words. Return ONLY JSON: "
            '{"subject":str,"body":str,"hook":str}.'
        )
        user = json.dumps({"company": company, "contact": contact, "pains": pains})
        return self._json(self.model_draft, sys, user, max_tokens=700)


# ---------- Offline heuristic mock (no network, no keys) ----------

_CERTS = ["SOC 2", "SOC2", "ISO 27001", "ISO27001", "GDPR", "HIPAA", "PCI DSS", "PCI-DSS"]
_SW = ["saas", "software platform", "cloud platform", "b2b software", "platform for",
       "software for", "api", "dashboard", "automate", "integrat"]
_NON_SW = ["bakery", "restaurant", "cafe", "menu", "store hours", "fresh bread", "catering"]
_BUYER = [r"for (saas|b2b|enterprise|business|compan|team|startup|developer|engineer)\w*",
          r"\bb2b\b"]
_ENT = ["enterprise", "fortune 500", "trusted by", "enterprise-grade", "enterprise plan",
        "enterprise tier"]


def _line_with(text: str, needle: str) -> str:
    for ln in text.splitlines():
        if needle.lower() in ln.lower():
            return ln.strip()[:160]
    return needle


class MockLLM:
    """Deterministic keyword extraction so the pipeline runs end-to-end offline."""

    def extract_fields(self, text: str) -> CompanyFields:
        f = CompanyFields()
        low = text.lower()

        certs = [c for c in _CERTS if c.lower() in low]
        if certs:
            # normalise duplicates like SOC2/SOC 2
            norm = []
            for c in certs:
                n = c.replace("SOC2", "SOC 2").replace("ISO27001", "ISO 27001").replace("PCI-DSS", "PCI DSS")
                if n not in norm:
                    norm.append(n)
            f.certifications = norm
            f.evidence["certifications"] = _line_with(text, certs[0])

        has_sw = any(k in low for k in _SW)
        is_non_sw = any(k in low for k in _NON_SW)
        if is_non_sw and not has_sw:
            f.is_saas = False
            f.evidence["is_saas"] = _line_with(text, next(k for k in _NON_SW if k in low))
        elif has_sw:
            f.is_saas = True
            f.evidence["is_saas"] = _line_with(text, next(k for k in _SW if k in low))

        for pat in _BUYER:
            m = re.search(pat, low)
            if m:
                f.buyer_description = text[m.start():m.start() + 60].strip()
                f.evidence["buyer_description"] = f.buyer_description
                break

        m = re.search(r"series\s+[a-e]\b", low)
        if m:
            f.funding_stage = m.group(0).title()
            f.evidence["funding_stage"] = _line_with(text, m.group(0))
        elif "seed" in low:
            f.funding_stage = "Seed"
            f.evidence["funding_stage"] = _line_with(text, "seed")

        m = re.search(r"(\d+)\s+open roles?", low)
        if m:
            f.open_roles_count = int(m.group(1))
            f.evidence["open_roles_count"] = _line_with(text, m.group(0))

        if any(k in low for k in _ENT):
            f.enterprise_customers = True
            f.evidence["enterprise_customers"] = _line_with(text, next(k for k in _ENT if k in low))

        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if first:
            f.name = (re.split(r"\s+(?:is|—|-|\|)\s+", first)[0][:40]).strip() or None
        return f

    def infer_pain_points(self, company: dict, contact: dict) -> list[dict]:
        role = (contact.get("role") or "decision-maker").lower()
        pains = []
        if company.get("open_roles_count"):
            pains.append({
                "pain": "Scaling the team without a formal security/compliance program in place",
                "evidence": f"{company['open_roles_count']} open roles signal rapid growth",
            })
        if company.get("enterprise_customers") or company.get("funding_stage"):
            pains.append({
                "pain": "Enterprise buyers demanding SOC 2 / ISO 27001 before they sign",
                "evidence": "enterprise traction / funding stage raises audit pressure",
            })
        if len(pains) < 2:
            pains.append({
                "pain": "Manual, slow path to a first compliance certification",
                "evidence": f"no named certification found for a {role}",
            })
        return pains[:2]

    def write_email(self, company: dict, contact: dict, pains: list[dict]) -> dict:
        name = (contact.get("name") or "there").split()[0]
        cname = company.get("name") or "your team"
        hook = pains[0]["pain"] if pains else "getting compliance-ready fast"
        subject = f"{cname}: a faster path to SOC 2"
        body = (
            f"Hi {name},\n\n"
            f"Noticed {cname} is growing --- which usually means {hook.lower()}.\n\n"
            f"Sprinto automates SOC 2, ISO 27001 and GDPR for SaaS teams, so you can get "
            f"audit-ready in weeks instead of quarters without a dedicated security hire.\n\n"
            f"Worth a 15-minute look? Happy to share how similar { _stage_phrase(company) } teams did it.\n\n"
            f"Best,\nThe Sprinto team"
        )
        return {"subject": subject, "body": body, "hook": hook}


def _stage_phrase(company: dict) -> str:
    return (company.get("funding_stage") or "early-stage") + ""


def get_llm(settings: Settings) -> LLM:
    return MockLLM() if settings.llm_is_mock else RealLLM(settings)
