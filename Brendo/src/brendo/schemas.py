"""Typed records passed between pipeline steps. Named fields are the contract:
qualification checks whether they are *populated* in code, never by asking the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class CompanyFields:
    """Extracted ICP facts. Each fact carries the evidence snippet it came from."""
    name: str | None = None
    is_saas: bool | None = None
    buyer_description: str | None = None          # who buys it (B2B signal)
    certifications: list[str] = field(default_factory=list)
    funding_stage: str | None = None
    open_roles_count: int | None = None
    enterprise_customers: bool | None = None
    compliance_signals: list[str] = field(default_factory=list)  # audit prep, compliance hiring...
    employee_count: int | None = None             # headcount the company states (size band)
    funding_year: int | None = None               # year the funding round was announced, if stated
    evidence: dict[str, str] = field(default_factory=dict)  # field -> source snippet

    def has(self, name: str) -> bool:
        v = getattr(self, name, None)
        if isinstance(v, list):
            return len(v) > 0
        return v is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Qualification:
    verdict: str = "pending"          # qualified | qualified_flagged | disqualified | escalated | failed
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)
    fetches_used: int = 0
    rank: str = "normal"              # high | normal | lower
    flags: list[str] = field(default_factory=list)   # notes for the human reviewer


@dataclass
class Contact:
    name: str | None = None
    role: str | None = None
    email: str | None = None
    source: str = ""                  # hunter | contacts_file | mock
    confidence: float = 0.0
    persona: str | None = None        # which buyer persona matched (icp.json)
    reason: str = ""                  # why this person (defensibility)


@dataclass
class Email:
    subject: str = ""
    body: str = ""
    hook: str = ""
    pains: list[str] = field(default_factory=list)
    hook_evidence_field: str | None = None
    footer: str = ""                  # "why" note appended to the self-sent copy
    sent: bool = False                # delivered via Gmail
    drafted: bool = False             # written to the mock outbox
    message_id: str | None = None
    applied_corrections: list[str] = field(default_factory=list)   # feedback-memory ids used


@dataclass
class CompanyResult:
    domain: str
    status: str = "pending"           # mirrors Qualification.verdict, plus sent/drafted/failed
    company: CompanyFields | None = None
    qualification: Qualification | None = None
    contact: Contact | None = None
    email: Email | None = None
    decisions: list[dict] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0

    def log(self, step: str, rationale: str) -> None:
        self.decisions.append({"step": step, "rationale": rationale})

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "status": self.status,
            "company": self.company.to_dict() if self.company else None,
            "qualification": asdict(self.qualification) if self.qualification else None,
            "contact": asdict(self.contact) if self.contact else None,
            "email": asdict(self.email) if self.email else None,
            "decisions": self.decisions,
            "error": self.error,
            "seconds": round(self.seconds, 2),
        }
