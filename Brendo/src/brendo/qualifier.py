"""Step 3 --- qualify against the ICP, with the adaptive loop.

The model makes judgments (extraction); CODE decides whether there is enough to judge
and renders the verdict. The loop fetches a *named* source when a required field is
missing, bounded by a hard fetch cap; at the cap it is a graceful handoff, not 'try
harder'. Short-circuits come straight from the Evidence Map (deck slide 40).

Shortlist rules (Evals deck, Decision 1): funding stage must be Series A-C (Seed, D+ and
bootstrapped are rejected); headcount has a SOFT 50-200 band (near the band is accepted and
noted, far outside is rejected); unknown stage or size never rejects on its own. Compliance
pressure (audit prep, compliance hiring) lifts a qualified company to rank 'high'.

Sufficiency (Decision 2): a funding mention older than funding.max_age_years is stale and
counts as unknown; a homepage that redirects to another site, or page text that tries to
instruct AI tools, is handed to a human ('escalated'). Cap (Decision 3): when the global
fetch cap stops the loop with a required question unanswered, the company is escalated ---
never a silent extra fetch.
"""
from __future__ import annotations

import datetime
import re
from urllib.parse import urlparse

from . import extractor, guard
from .llm import LLM
from .scraper import Scraper
from .schemas import CompanyFields, CompanyResult, Qualification


class SiteUnreachable(Exception):
    """The homepage could not be read: a failure, not a thin signal (no extra fetches)."""


def _year_now() -> int:
    return datetime.date.today().year


def funding_is_stale(acc: CompanyFields, icp: dict) -> bool:
    max_age = icp.get("funding", {}).get("max_age_years", 2)
    return bool(acc.funding_stage and acc.funding_year and _year_now() - acc.funding_year > max_age)


def _answered(question: dict, acc: CompanyFields, icp: dict) -> bool:
    qid = question["id"]
    if qid == "already_certified":
        return bool(acc.certifications)                      # a positive answer ends it
    if qid == "is_b2b_saas":
        return acc.is_saas is not None
    if qid == "right_stage":
        return _stage_sufficient(acc, icp)
    if qid == "right_size":
        return acc.employee_count is not None
    return False


def _early_decided(acc: CompanyFields, icp: dict) -> bool:
    """A disqualifying answer we already hold --- no need to fetch more."""
    return (bool(acc.certifications) or acc.is_saas is False
            or stage_band(acc, icp) in ("early", "late", "other")
            or size_band(acc, icp).startswith("far"))


def _stage_sufficient(acc: CompanyFields, icp: dict) -> bool:
    min_roles = icp.get("stage_thresholds", {}).get("min_open_roles", 5)
    return bool(
        (acc.funding_stage and not funding_is_stale(acc, icp))
        or (acc.open_roles_count or 0) >= min_roles
        or acc.enterprise_customers
    )


def stage_band(acc: CompanyFields, icp: dict) -> str:
    """target (Series A-C) | early (pre-seed/seed) | late (Series D+) | other | stale | unknown."""
    r = icp.get("ranking", {})
    stage = (acc.funding_stage or "").strip().lower()
    if not stage:
        return "unknown"
    if funding_is_stale(acc, icp):
        return "stale"
    if any(stage == t or stage.startswith(t) for t in r.get("target_stages", [])):
        return "target"
    if any(e in stage for e in r.get("early_stages", [])):
        return "early"
    if any(o in stage for o in r.get("other_stages", [])):
        return "other"
    if stage.startswith("series"):
        return "late"
    return "unknown"


def size_band(acc: CompanyFields, icp: dict) -> str:
    """in | near_low | near_high | far_low | far_high | unknown (Evals deck: soft 50-200)."""
    n = acc.employee_count
    b = icp.get("size_band", {})
    if n is None or not b:
        return "unknown"
    if b["min"] <= n <= b["max"]:
        return "in"
    if b["near_low"] <= n < b["min"]:
        return "near_low"
    if b["max"] < n <= b["near_high"]:
        return "near_high"
    return "far_low" if n < b["min"] else "far_high"


def _rank(acc: CompanyFields, icp: dict, base: str) -> tuple[str, list[str], list[str]]:
    """Return (rank, reason fragments, reviewer flags). Outside-A-C stages never get here."""
    band = stage_band(acc, icp)
    notes, flags = [], []
    if acc.compliance_signals:
        notes.append(f"compliance pressure: {acc.compliance_signals[0]!r}")
    if band == "target":
        notes.append(f"{acc.funding_stage} is in Sprinto's Series A-C sweet spot")
    if base == "lower":
        return "lower", notes, flags
    if acc.compliance_signals and band in ("target", "unknown", "stale"):
        return "high", notes, flags
    return "normal", notes, flags


def _size_notes(acc: CompanyFields, icp: dict) -> tuple[list[str], list[str]]:
    b, n, sb = icp.get("size_band", {}), acc.employee_count, size_band(acc, icp)
    if sb == "in":
        return [f"{n} employees is inside the {b['min']}-{b['max']} target band"], []
    if sb in ("near_low", "near_high"):
        side = "below" if sb == "near_low" else "above"
        return ([f"{n} employees is just {side} the {b['min']}-{b['max']} band --- near enough to accept"],
                [f"Size {n} is near-band ({side} {b['min']}-{b['max']}) --- accepted; confirm fit"])
    return [], ["Headcount not stated on the pages read --- size unknown"]


def _decide(acc: CompanyFields, icp: dict, cert_sources: list[str],
            unreadable_trust: str | None = None) -> tuple[str, list[str], str, list[str]]:
    # 1. already certified -> disqualify + STOP
    if acc.certifications:
        return ("disqualified", [f"already certified ({', '.join(acc.certifications)}) --- no need for Sprinto"],
                "normal", [])
    # 2. not B2B SaaS -> disqualify + STOP
    if acc.is_saas is False:
        return "disqualified", ["not B2B SaaS"], "normal", []
    # 3. funding outside Series A-C -> disqualify + STOP
    band = stage_band(acc, icp)
    if band in ("early", "late", "other"):
        return ("disqualified", [f"funding stage {acc.funding_stage} is outside Series A-C"],
                "normal", [])
    # 4. size far outside the soft band -> disqualify
    sb = size_band(acc, icp)
    if sb.startswith("far"):
        b = icp["size_band"]
        return ("disqualified", [f"{acc.employee_count} employees is far outside the "
                                 f"{b['min']}-{b['max']} band (accepted range {b['near_low']}-{b['near_high']})"],
                "normal", [])
    # 5. SaaS unknown -> human
    if acc.is_saas is None:
        return "escalated", ["SaaS status could not be determined --- manual_review"], "normal", []

    flags = []
    if unreadable_trust:
        flags.append(f"Publishes a trust centre ({unreadable_trust}) that needs a browser to read --- "
                     "likely already certified; check it before any outreach")
    elif not acc.certifications:
        where = " + ".join(["homepage"] + cert_sources)
        flags.append(f"No named certification found ({where}) --- treated as not certified")
    if band == "stale":
        flags.append(f"Funding news ({acc.funding_stage}, {acc.funding_year}) is more than "
                     f"{icp.get('funding', {}).get('max_age_years', 2)} years old --- stale, "
                     "not used as the stage signal")
    size_reasons, size_flags = _size_notes(acc, icp)
    flags += size_flags

    # 6. stage
    if _stage_sufficient(acc, icp):
        signals = []
        if acc.funding_stage and band != "stale":
            signals.append(f"funding: {acc.funding_stage}" + (f" ({acc.funding_year})" if acc.funding_year else ""))
        if acc.open_roles_count:
            signals.append(f"{acc.open_roles_count} open roles")
        if acc.enterprise_customers:
            signals.append("enterprise customers")
        rank, notes, rflags = _rank(acc, icp, "lower" if unreadable_trust else "normal")
        reasons = ["B2B SaaS, not yet certified; " + ", ".join(signals)] + notes + size_reasons
        return "qualified", reasons, rank, flags + rflags
    rank, notes, rflags = _rank(acc, icp, "lower")
    flags.append("Stage signal thin (no current funding stage, <5 open roles, no enterprise tier) --- lower rank")
    return ("qualified_flagged", ["B2B SaaS, not certified, but stage signal thin"] + notes + size_reasons,
            rank, flags + rflags)


def _site(host: str) -> str:
    host = (host or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def same_site(domain: str, url: str) -> bool:
    """True if `url` is on `domain` or one of its subdomains (or the reverse)."""
    h, d = _site(urlparse(url).netloc), _site(domain)
    return not h or h == d or h.endswith("." + d) or d.endswith("." + h)


def _mentions(page_clean: str, domain: str, name: str | None) -> bool:
    low = page_clean.lower()
    label = _site(domain).split(".")[0]
    return label in low.replace(" ", "") or bool(name and name.lower() in low)


_JOBS = ("career", "jobs", "job-", "/job", "hiring", "join", "work-with-us", "ashbyhq", "greenhouse", "lever.co",
         "workable", "recruitee")


def _absorb(acc: CompanyFields, page_clean: str, llm: LLM, result: CompanyResult, where: str,
            injections: list[str]) -> None:
    page_clean, hits = guard.scan(page_clean)
    for h in hits:
        result.log("guard", f"removed text aimed at AI tools from {where}: {h!r}")
    injections += hits
    new = extractor.extract(page_clean, llm)
    dropped = extractor.verify(new, page_clean, jobs_page=any(j in where.lower() for j in _JOBS))
    for d in dropped:
        result.log("verify", f"dropped from {where}: {d}")
    extractor.merge(acc, new)


def qualify(domain: str, scraper: Scraper, llm: LLM, icp: dict, max_fetches: int,
            result: CompanyResult) -> tuple[CompanyFields, Qualification]:
    acc = CompanyFields()
    q = Qualification()

    injections: list[str] = []
    hp = scraper.fetch(domain, "/")
    result.log("scrape", f"homepage via {hp.method} ({hp.status}{': ' + hp.reason if hp.reason else ''})")
    if hp.failed:
        raise SiteUnreachable(f"homepage {hp.status}: {hp.reason or 'no content'}")
    if hp.final_url and not same_site(domain, hp.final_url):
        # Decision 2, identity: the domain now serves another company's site
        moved = _site(urlparse(hp.final_url).netloc)
        q.verdict, q.rank = "escalated", "normal"
        q.reasons = [f"homepage redirects to {moved} --- may be a different company "
                     "(acquired, renamed or parked); not accepted as a match"]
        q.flags = ["Identity mismatch: confirm which company this domain belongs to"]
        result.log("qualify", f"escalated --- {q.reasons[0]}")
        return acc, q
    if hp.clean:
        _absorb(acc, hp.clean, llm, result, "homepage", injections)
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
        if _early_decided(acc, icp):
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
                if question.get("required", True):
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
            if f.clean and src.startswith("http") and not same_site(domain, src) \
                    and not _mentions(f.clean, domain, acc.name):
                result.log("verify", f"ignored {label}: it is on another site and never names "
                                     f"{acc.name or domain} --- may be about a different company")
            elif f.clean:
                _absorb(acc, f.clean, llm, result, label, injections)
            if _answered(question, acc, icp):
                result.log("loop", f"'{question['id']}' answered after {label}")
                break
        if unanswered_at_cap:
            break

    if acc.certifications:
        unreadable_trust = None
    verdict, reasons, rank, flags = _decide(acc, icp, cert_sources, unreadable_trust)
    if acc.name and hp.clean and not _mentions(hp.clean, domain, None) \
            and not any(t in _site(domain) for t in re.findall(r"[a-z0-9]{3,}", acc.name.lower())):
        flags.append(f"The homepage calls the company '{acc.name}', which doesn't match "
                     f"{domain} --- confirm it's the right company")
    if unanswered_at_cap:
        flags.append(f"Hit fetch cap ({max_fetches} beyond homepage) with "
                     f"'{unanswered_at_cap}' unanswered --- escalated to a human")
        if verdict != "disqualified":
            verdict = "escalated"
            reasons = [f"fetch cap reached ({fetches} of {max_fetches}) with '{unanswered_at_cap}' "
                       "unanswered --- handed to a human, no extra fetch"] + reasons
    if injections:
        verdict = "escalated"
        reasons = ["page text tries to instruct AI tools --- removed, and handed to a human"] + reasons
        flags.append("Possible prompt injection on the site: " + "; ".join(repr(i) for i in injections[:2]))
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
