"""Step 4 --- find the point of contact, and record WHY this person.

Sources, in order:
  Option B --- inputs/contacts.csv (domain,name,role,email): contacts looked up by hand in
             Apollo before the run. Keyed by domain, so each company gets its own person.
  Option A --- Hunter.io Domain Search API (free tier 25/month --- cache hard).
  Mock      --- a deterministic placeholder contact, flagged, for offline runs.

Choosing among Hunter's results is CODE, driven by the ranked buyer personas in
config/icp.json. A title that matches no persona is never picked ("nearest manager" is the
failure mode slide 32 names): the company is flagged for a human instead.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .config import Settings
from .schemas import Contact, CompanyResult


def load_contacts(path: Path) -> dict[str, dict]:
    """domain -> {name, role, email}. Missing file = no hardcoded contacts."""
    if not path or not path.exists():
        return {}
    out: dict[str, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            d = (row.get("domain") or "").strip().lower()
            if d and not d.startswith("#"):
                out[d] = {k: (row.get(k) or "").strip() or None for k in ("name", "role", "email")}
    return out


def match_persona(title: str | None, personas: list[dict]) -> dict | None:
    """Best (lowest-rank) persona whose keywords match the title, honouring seniority."""
    t = (title or "").lower()
    if not t:
        return None
    best = None
    for p in personas:
        if not any(k in t for k in p.get("keywords", [])):
            continue
        if p.get("seniority") and not any(s in t for s in p["seniority"]):
            continue
        if best is None or p["rank"] < best["rank"]:
            best = p
    return best


def pick(emails: list[dict], personas: list[dict]) -> tuple[dict | None, dict | None, str]:
    """Choose a contact from Hunter results. Returns (email record, persona, reason)."""
    scored = []
    for e in emails:
        p = match_persona(e.get("position"), personas)
        if p and e.get("value"):
            scored.append((p["rank"], -(e.get("confidence") or 0), e, p))
    if not scored:
        titles = ", ".join(sorted({(e.get("position") or "untitled") for e in emails})[:6]) or "none"
        return None, None, f"no buyer persona among {len(emails)} contacts (titles: {titles})"
    scored.sort(key=lambda s: (s[0], s[1]))
    _, _, e, p = scored[0]
    others = len(scored) - 1
    reason = (f"title '{e.get('position')}' matches persona '{p['id']}' (rank {p['rank']}); "
              f"email confidence {e.get('confidence', '?')}%"
              + (f"; chosen over {others} other matching contact(s)" if others else ""))
    return e, p, reason


def find_poc(domain: str, settings: Settings, result: CompanyResult,
             personas: list[dict] | None = None) -> Contact:
    personas = personas or []

    # Option B --- per-domain hardcode from Apollo
    hard = load_contacts(settings.contacts_path).get(domain.lower())
    if hard:
        p = match_persona(hard.get("role"), personas)
        reason = (f"looked up manually in Apollo (contacts file); role '{hard.get('role')}' "
                  + (f"matches persona '{p['id']}'" if p else "matches no buyer persona --- check fit"))
        c = Contact(name=hard.get("name"), role=hard.get("role"), email=hard.get("email"),
                    source="contacts_file", confidence=0.9, persona=p["id"] if p else None,
                    reason=reason)
        result.log("poc", f"{c.name} ({c.role}) --- {reason}")
        return c

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
            emails = r.json().get("data", {}).get("emails", []) or []
        except Exception as exc:
            msg = f"Hunter error: {str(exc)[:80]} --- flagged"
            result.log("poc", msg)
            return Contact(source="hunter", confidence=0.0, reason=msg)
        e, p, reason = pick(emails, personas)
        if not e:
            result.log("poc", f"Hunter: {reason} --- flagged, not drafting")
            return Contact(source="hunter", confidence=0.0, reason=reason)
        name = " ".join(filter(None, [e.get("first_name"), e.get("last_name")])) or None
        c = Contact(name=name, role=e.get("position"), email=e.get("value"), source="hunter",
                    confidence=float(e.get("confidence") or 0) / 100, persona=p["id"], reason=reason)
        if not name:
            c.reason += "; Hunter gave no name"
        result.log("poc", f"Hunter: {name} ({c.role}) --- {reason}")
        return c

    # Mock
    c = Contact(name="Alex Rivera", role="Head of Security", email=f"alex@{domain}",
                source="mock", confidence=0.5, persona="compliance_security_lead",
                reason="[mock] placeholder contact --- not a real person; use Hunter or contacts.csv")
    result.log("poc", c.reason)
    return c
