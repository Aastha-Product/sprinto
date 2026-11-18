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
    verdict: str = "pending"          # qualified | qualified_flagged | disqualified | flagged | failed
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)
    fetches_used: int = 0
    rank: str = "normal"              # normal | lower


@dataclass
class Contact:
    name: str | None = None
    role: str | None = None
    email: str | None = None
    source: str = ""                  # hunter | apollo_hardcode | mock
    confidence: float = 0.0


@dataclass
class Email:
    subject: str = ""
    body: str = ""
    hook: str = ""
    pains: list[str] = field(default_factory=list)
    sent: bool = False
    message_id: str | None = None


@dataclass
class CompanyResult:
    domain: str
    status: str = "pending"           # mirrors Qualification.verdict, plus sent/skipped
    company: CompanyFields | None = None
    qualification: Qualification | None = None
    contact: Contact | None = None
    email: Email | None = None
    decisions: list[dict] = field(default_factory=list)
    error: str | None = None

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
        }
