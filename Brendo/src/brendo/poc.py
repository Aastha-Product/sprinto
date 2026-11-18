"""Step 4 --- find the point of contact.

Option A: Hunter.io Domain Search API (free tier 25/month --- cache hard).
Option B: Apollo hardcode (BRENDO_APOLLO_CONTACT="Name|Role|email"), looked up manually.
Mock: a deterministic placeholder contact, flagged low-confidence (for offline runs).
"""
from __future__ import annotations

from .config import Settings
from .schemas import Contact, CompanyResult

_DM = ("founder", "ceo", "cto", "head", "vp", "vice president", "director",
       "security", "compliance", "engineering", "grc")


def _pick(emails: list[dict]) -> dict | None:
    for e in emails:
        pos = (e.get("position") or "").lower()
        if any(k in pos for k in _DM):
            return e
    return emails[0] if emails else None


def find_poc(domain: str, settings: Settings, result: CompanyResult) -> Contact:
    # Option B --- Apollo hardcode
    if settings.apollo_contact:
        parts = [p.strip() for p in settings.apollo_contact.split("|")]
        name = parts[0] if parts else None
        role = parts[1] if len(parts) > 1 else None
        email = parts[2] if len(parts) > 2 else None
        result.log("poc", f"Apollo hardcode: {name} ({role})")
        return Contact(name=name, role=role, email=email, source="apollo_hardcode", confidence=0.9)

    # Option A --- Hunter.io
    if not settings.hunter_is_mock:
        try:
            import httpx
            r = httpx.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": settings.hunter_api_key, "limit": 10},
                timeout=15,
            )
            r.raise_for_status()
            emails = r.json().get("data", {}).get("emails", [])
            pick = _pick(emails)
            if pick:
                name = " ".join(filter(None, [pick.get("first_name"), pick.get("last_name")])) or None
                c = Contact(name=name, role=pick.get("position"), email=pick.get("value"),
                            source="hunter", confidence=float(pick.get("confidence", 50)) / 100)
                result.log("poc", f"Hunter: {name} ({c.role}) conf={c.confidence:.2f}")
                return c
            result.log("poc", "Hunter returned no contacts --- flagged")
            return Contact(source="hunter", confidence=0.0)
        except Exception as exc:
            result.log("poc", f"Hunter error: {str(exc)[:80]} --- flagged")
            return Contact(source="hunter", confidence=0.0)

    # Mock
    handle = domain.split(".")[0]
    c = Contact(name="Alex Rivera", role="Head of Engineering",
                email=f"alex@{domain}", source="mock", confidence=0.5)
    result.log("poc", f"[mock] placeholder contact for {handle} --- verify before real send")
    return c
