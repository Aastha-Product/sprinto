"""Step 3 --- qualify against the ICP, with the adaptive loop.

The model makes judgments (extraction); CODE decides whether there is enough to judge
and renders the verdict. The loop fetches a *named* source when a required field is
missing, bounded by a hard fetch cap; at the cap it is a graceful handoff, not 'try
harder'. Short-circuits come straight from the Evidence Map (deck slide 40).
"""
from __future__ import annotations

from . import extractor
from .llm import LLM
from .scraper import Scraper
from .schemas import CompanyFields, CompanyResult, Qualification


def _answered(question: dict, acc: CompanyFields, icp: dict) -> bool:
    qid = question["id"]
    if qid == "already_certified":
        return bool(acc.certifications)                      # a positive answer ends it
    if qid == "is_b2b_saas":
        return acc.is_saas is not None
    if qid == "right_stage":
        return _stage_sufficient(acc, icp)
    return False


def _early_decided(acc: CompanyFields) -> bool:
    """A disqualifying answer we already hold --- no need to fetch more."""
    return bool(acc.certifications) or acc.is_saas is False


def _stage_sufficient(acc: CompanyFields, icp: dict) -> bool:
    min_roles = icp.get("stage_thresholds", {}).get("min_open_roles", 5)
    return bool(
        acc.funding_stage
        or (acc.open_roles_count or 0) >= min_roles
        or acc.enterprise_customers
    )


def _decide(acc: CompanyFields, icp: dict) -> tuple[str, list[str], str]:
    # 1. already certified -> disqualify + STOP
    if acc.certifications:
        return "disqualified", [f"Already certified ({', '.join(acc.certifications)}) --- no need for Sprinto"], "normal"
    # 2. not B2B SaaS -> disqualify + STOP
    if acc.is_saas is False:
        return "disqualified", ["Not B2B SaaS"], "normal"
    # 3. SaaS unknown -> manual review
    if acc.is_saas is None:
        return "flagged", ["SaaS status could not be determined --- manual_review"], "normal"
    # 4. stage
    if _stage_sufficient(acc, icp):
        signals = []
        if acc.funding_stage:
            signals.append(f"funding: {acc.funding_stage}")
        if (acc.open_roles_count or 0):
            signals.append(f"{acc.open_roles_count} open roles")
        if acc.enterprise_customers:
            signals.append("enterprise customers")
        return "qualified", ["B2B SaaS, not yet certified; " + ", ".join(signals)], "normal"
    return "qualified_flagged", ["B2B SaaS, not certified, but stage signal thin --- lower rank, flagged"], "lower"


def qualify(domain: str, scraper: Scraper, llm: LLM, icp: dict, max_fetches: int,
            result: CompanyResult) -> tuple[CompanyFields, Qualification]:
    acc = CompanyFields()
    q = Qualification()

    hp = scraper.fetch(domain, "/")
    result.log("scrape", f"homepage via {hp.method} ({hp.status})")
    if hp.clean:
        extractor.merge(acc, extractor.extract(hp.clean, llm))
        result.log("extract", f"from homepage: {_filled(acc)}")

    # adaptive loop --- fill missing answers from named sources, under the global cap.
    # If a disqualifying short-circuit is already decided, stop (Evidence Map: "stop").
    fetches = 0
    for question in icp["questions"]:
        if _early_decided(acc):
            result.log("loop", "short-circuit already decided --- stop fetching")
            break
        if _answered(question, acc, icp):
            continue
        for src in question["sources"][: question["limit"]]:
            if fetches >= max_fetches:
                break
            f = scraper.fetch(domain, src)
            fetches += 1
            result.log("loop", f"need '{question['id']}' -> fetched {src} ({f.status})")
            if f.clean:
                extractor.merge(acc, extractor.extract(f.clean, llm))
            if _answered(question, acc, icp):
                result.log("loop", f"'{question['id']}' answered after {src}")
                break
        if fetches >= max_fetches:
            q.reasons.append(f"Hit fetch cap ({max_fetches} beyond homepage) --- graceful handoff")
            break

    verdict, reasons, rank = _decide(acc, icp)
    q.verdict = verdict
    q.reasons = reasons + q.reasons
    q.rank = rank
    q.fetches_used = fetches
    q.evidence = dict(acc.evidence)
    result.log("qualify", f"{verdict} ({rank}) --- {'; '.join(reasons)}")
    return acc, q


def _filled(acc: CompanyFields) -> str:
    present = [n for n in ("is_saas", "buyer_description", "certifications",
                           "funding_stage", "open_roles_count", "enterprise_customers")
               if acc.has(n)]
    return ", ".join(present) or "nothing"
