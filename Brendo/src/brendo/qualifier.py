"""Step 3 --- qualify against the ICP, with the adaptive loop.

The model makes judgments (extraction); CODE decides whether there is enough to judge
and renders the verdict. The loop fetches a *named* source when a required field is
missing, bounded by a hard fetch cap; at the cap it is a graceful handoff, not 'try
harder'. Short-circuits come straight from the Evidence Map (deck slide 40).

Ranking (brief's ICP, never a gate): Series A-C ranks above seed / late stage, and
compliance pressure (audit prep, compliance hiring) lifts a company to 'high'.
"""
from __future__ import annotations

from . import extractor
from .llm import LLM
from .scraper import Scraper
from .schemas import CompanyFields, CompanyResult, Qualification


class SiteUnreachable(Exception):
    """The homepage could not be read: a failure, not a thin signal (no extra fetches)."""


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


def stage_band(acc: CompanyFields, icp: dict) -> str:
    """target (Series A-C) | early (pre-seed/seed) | late (Series D+) | unknown."""
    r = icp.get("ranking", {})
    stage = (acc.funding_stage or "").strip().lower()
    if not stage:
        return "unknown"
    if any(stage == t or stage.startswith(t) for t in r.get("target_stages", [])):
        return "target"
    if any(e in stage for e in r.get("early_stages", [])):
        return "early"
    if any(o in stage for o in r.get("other_stages", [])):
        return "other"
    if stage.startswith("series"):
        return "late"
    return "unknown"


def _rank(acc: CompanyFields, icp: dict, base: str) -> tuple[str, list[str], list[str]]:
    """Return (rank, reason fragments, reviewer flags)."""
    band = stage_band(acc, icp)
    notes, flags = [], []
    if acc.compliance_signals:
        notes.append(f"compliance pressure: {acc.compliance_signals[0]!r}")
    if band == "target":
        notes.append(f"{acc.funding_stage} is in Sprinto's Series A-C sweet spot")
    elif band in ("early", "late", "other"):
        notes.append(f"{acc.funding_stage} is outside the typical Series A-C range")
        flags.append(f"Stage {acc.funding_stage} is outside Series A-C --- check fit")

    if base == "lower":
        return "lower", notes, flags
    if band in ("early", "late", "other"):
        return "lower", notes, flags
    if acc.compliance_signals and band in ("target", "unknown"):
        return "high", notes, flags
    return "normal", notes, flags


def _decide(acc: CompanyFields, icp: dict, cert_sources: list[str],
            unreadable_trust: str | None = None) -> tuple[str, list[str], str, list[str]]:
    # 1. already certified -> disqualify + STOP
    if acc.certifications:
        return ("disqualified", [f"Already certified ({', '.join(acc.certifications)}) --- no need for Sprinto"],
                "normal", [])
    # 2. not B2B SaaS -> disqualify + STOP
    if acc.is_saas is False:
        return "disqualified", ["Not B2B SaaS"], "normal", []
    # 3. SaaS unknown -> manual review
    if acc.is_saas is None:
        return "flagged", ["SaaS status could not be determined --- manual_review"], "normal", []

    flags = []
    if unreadable_trust:
        flags.append(f"Publishes a trust centre ({unreadable_trust}) that needs a browser to read --- "
                     "likely already certified; check it before any outreach")
    elif not acc.certifications:
        where = " + ".join(["homepage"] + cert_sources)
        flags.append(f"No named certification found ({where}) --- treated as not certified")

    # 4. stage
    if _stage_sufficient(acc, icp):
        signals = []
        if acc.funding_stage:
            signals.append(f"funding: {acc.funding_stage}")
        if acc.open_roles_count:
            signals.append(f"{acc.open_roles_count} open roles")
        if acc.enterprise_customers:
            signals.append("enterprise customers")
        rank, notes, rflags = _rank(acc, icp, "lower" if unreadable_trust else "normal")
        reasons = ["B2B SaaS, not yet certified; " + ", ".join(signals)] + notes
        return "qualified", reasons, rank, flags + rflags
    rank, notes, rflags = _rank(acc, icp, "lower")
    flags.append("Stage signal thin (no funding stage, <5 open roles, no enterprise tier) --- lower rank")
    return ("qualified_flagged", ["B2B SaaS, not certified, but stage signal thin"] + notes,
            rank, flags + rflags)


_JOBS = ("career", "jobs", "job-", "/job", "hiring", "join", "work-with-us", "ashbyhq", "greenhouse", "lever.co",
         "workable", "recruitee")


def _absorb(acc: CompanyFields, page_clean: str, llm: LLM, result: CompanyResult, where: str) -> None:
    new = extractor.extract(page_clean, llm)
    dropped = extractor.verify(new, page_clean, jobs_page=any(j in where.lower() for j in _JOBS))
    for d in dropped:
        result.log("verify", f"dropped from {where}: {d}")
    extractor.merge(acc, new)


def qualify(domain: str, scraper: Scraper, llm: LLM, icp: dict, max_fetches: int,
            result: CompanyResult) -> tuple[CompanyFields, Qualification]:
    acc = CompanyFields()
    q = Qualification()

    hp = scraper.fetch(domain, "/")
    result.log("scrape", f"homepage via {hp.method} ({hp.status}{': ' + hp.reason if hp.reason else ''})")
    if hp.failed:
        raise SiteUnreachable(f"homepage {hp.status}: {hp.reason or 'no content'}")
    if hp.clean:
        _absorb(acc, hp.clean, llm, result, "homepage")
        result.log("extract", f"from homepage: {_filled(acc)}")

    # adaptive loop --- fill missing answers from named sources, under the global cap.
    # Sources per question: links the company itself publishes (matching the question's
    # link_keywords), then the Evidence Map's fixed paths. A missing page (404) does not
    # use up the question's limit; every request counts toward the global cap.
    # If a disqualifying short-circuit is already decided, stop (Evidence Map: "stop").
    fetches = 0
    cert_sources: list[str] = []
    unreadable_trust: str | None = None
    unanswered_at_cap: str | None = None
    tried: set[str] = {hp.url.rstrip("/")}
    for question in icp["questions"]:
        if _early_decided(acc):
            result.log("loop", "short-circuit already decided --- stop fetching")
            break
        if _answered(question, acc, icp):
            continue
        sources = _published_links(hp.links, question.get("link_keywords", []),
                                   icp.get("link_denylist", [])) + question["sources"]
        used = 0
        for src in sources:
            if used >= question["limit"]:
                break
            key = (src if src.startswith("http") else f"https://{domain}{src}").rstrip("/")
            if key in tried:
                continue
            if fetches >= max_fetches:
                unanswered_at_cap = question["id"]
                break
            tried.add(key)
            f = scraper.fetch(domain, src)
            fetches += 1
            label = src if not src.startswith("http") else f"{src} (linked from homepage)"
            result.log("loop", f"need '{question['id']}' -> fetched {label} ({f.status})")
            if f.status == "missing":
                continue                        # nothing there: try the next source
            used += 1
            if question["id"] == "already_certified":
                cert_sources.append(src if not src.startswith("http") else src.split("://", 1)[1].rstrip("/"))
                if src.startswith("http") and "trust" in src.lower() and len(f.clean) < 200:
                    unreadable_trust = src.split("://", 1)[1].rstrip("/")
            if f.clean:
                _absorb(acc, f.clean, llm, result, label)
            if _answered(question, acc, icp):
                result.log("loop", f"'{question['id']}' answered after {label}")
                break
        if unanswered_at_cap:
            break

    if acc.certifications:
        unreadable_trust = None
    verdict, reasons, rank, flags = _decide(acc, icp, cert_sources, unreadable_trust)
    if unanswered_at_cap:
        flags.append(f"Hit fetch cap ({max_fetches} beyond homepage) with "
                     f"'{unanswered_at_cap}' unanswered --- graceful handoff")
    q.verdict, q.reasons, q.rank, q.flags = verdict, reasons, rank, flags
    q.fetches_used = fetches
    q.evidence = dict(acc.evidence)
    result.log("qualify", f"{verdict} (rank {rank}) --- {'; '.join(reasons)}")
    for fl in flags:
        result.log("flag", fl)
    return acc, q


def _published_links(links: list[tuple[str, str]], keywords: list[str], deny: list[str],
                     limit: int = 2) -> list[str]:
    """Links on the homepage whose URL or text names what this question needs."""
    if not keywords:
        return []
    out = []
    for href, text in links:
        low_href, low_text = href.lower(), (text or "").lower()
        if any(d in low_href for d in deny):
            continue
        path = low_href.split("://", 1)[-1]
        if any(k in path or k in low_text for k in keywords) and href not in out:
            out.append(href)
        if len(out) >= limit:
            break
    return out


def _filled(acc: CompanyFields) -> str:
    present = [n for n in ("is_saas", "buyer_description", "certifications", "funding_stage",
                           "open_roles_count", "enterprise_customers", "compliance_signals")
               if acc.has(n)]
    return ", ".join(present) or "nothing"
