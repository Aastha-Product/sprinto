"""The feedback loop (Evals deck: Act -> Signal -> Capture -> Store -> Retrieve -> Apply).

Signal + capture (Decision 5): when the Growth Lead edits a draft, judge whether the edit
is a MEANINGFUL correction, on intent, not size. A rewritten hook or a tone change (even
punctuation only) counts; a typo fix does not. A meaningful edit becomes one stored
lesson ("Mention the prospect's latest funding round in the opening line").

Retrieve + apply (Decision 4): before drafting for a prospect, pick the lessons that are
relevant to it:
  - a lesson that needs a fact (e.g. funding_stage) applies only if this company has it;
  - a 'specific' lesson (e.g. 'fintech') applies only to prospects in that context;
  - two lessons on the same topic conflict: the most recent wins, the older is skipped.
Every applied and skipped lesson is logged with the reason, so the choice is defensible.

Storage: a list of plain dicts. The website keeps it in the browser (Vercel has no lasting
disk) and sends it with each run; the CLI reads data/corrections.json.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .llm import CITABLE_FIELDS, EDIT_TOPICS
from .schemas import CompanyFields

MAX_APPLIED = 6


def classify(before: dict, after: dict, llm) -> dict:
    """Judge one edit. Returns {meaningful, reason, correction (or None)}."""
    b = {"subject": (before or {}).get("subject", ""), "body": (before or {}).get("body", "")}
    a = {"subject": (after or {}).get("subject", ""), "body": (after or {}).get("body", "")}
    if b == a:
        return {"meaningful": False, "reason": "no change", "correction": None}
    v = llm.classify_edit(b, a)
    if not v.get("meaningful") or not (v.get("lesson") or "").strip():
        return {"meaningful": False, "reason": v.get("reason") or "not a correction", "correction": None}
    topic = v.get("topic") if v.get("topic") in EDIT_TOPICS else "other"
    fact = v.get("requires_fact") if v.get("requires_fact") in CITABLE_FIELDS else None
    correction = {
        "id": "c_" + uuid.uuid4().hex[:8],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lesson": v["lesson"].strip()[:300],
        "topic": topic,
        "requires_fact": fact,
        "scope": "specific" if v.get("scope") == "specific" and v.get("context") else "generic",
        "context": (v.get("context") or None) if v.get("scope") == "specific" else None,
        "before": b, "after": a,
    }
    return {"meaningful": True, "reason": v.get("reason") or "", "correction": correction}


def _context_words(company: CompanyFields) -> str:
    parts = [company.name or "", company.buyer_description or "", " ".join(company.compliance_signals)]
    return " ".join(parts).lower()


def select(corrections: list[dict] | None, company: CompanyFields) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(applied, [(skipped, why)]) for this prospect. Newest first within a topic."""
    applied: list[dict] = []
    skipped: list[tuple[dict, str]] = []
    valid = [c for c in (corrections or []) if isinstance(c, dict) and (c.get("lesson") or "").strip()]
    newest_first = sorted(valid, key=lambda c: c.get("created_at") or "", reverse=True)
    seen_topic: dict[str, dict] = {}
    ctx = _context_words(company)
    for c in newest_first:
        fact = c.get("requires_fact")
        if fact and fact in CITABLE_FIELDS and not company.has(fact):
            skipped.append((c, f"needs {fact}, which this company doesn't have"))
            continue
        if c.get("scope") == "specific" and c.get("context"):
            words = [w for w in re.findall(r"[a-z0-9]{3,}", c["context"].lower())]
            if words and not any(w in ctx for w in words):
                skipped.append((c, f"written for '{c['context']}' prospects; this one is different"))
                continue
        topic = c.get("topic") or "other"
        if topic != "other" and topic in seen_topic:
            skipped.append((c, f"conflicts with newer correction {seen_topic[topic]['id']} on "
                               f"'{topic}' --- the most recent wins"))
            continue
        if len(applied) >= MAX_APPLIED:
            skipped.append((c, "more than the per-draft limit"))
            continue
        seen_topic[topic] = c
        applied.append(c)
    return applied, skipped


def load_file(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []
