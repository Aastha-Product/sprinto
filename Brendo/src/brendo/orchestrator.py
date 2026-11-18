"""The 7-step runner: batch over domains, isolate failures, persist everything.

One company failing never stops the batch (FR10): its reason is logged and the run
continues. A company researched within the cache TTL is reused (cost dial #3).
"""
from __future__ import annotations

from .config import Settings
from .llm import get_llm
from .scraper import Scraper
from .store import Store
from .schemas import CompanyResult
from . import qualifier, poc, painpoints, emailer

_QUALIFIED = {"qualified", "qualified_flagged"}


def process_one(domain: str, scraper: Scraper, llm, icp: dict, settings: Settings) -> CompanyResult:
    result = CompanyResult(domain=domain)
    try:
        company, qual = qualifier.qualify(domain, scraper, llm, icp, settings.max_fetches, result)
        result.company, result.qualification = company, qual
        result.status = qual.verdict

        if qual.verdict in _QUALIFIED:
            contact = poc.find_poc(domain, settings, result)
            result.contact = contact
            if not contact.email:
                result.status = qual.verdict  # qualified but no contact -> flagged for human
                result.log("halt", "No contact email --- flagged, not drafting")
                return result
            pains = painpoints.identify(company, contact, llm)
            email = emailer.draft(company, contact, pains, llm, result)
            email = emailer.send(email, settings, result, tag=domain)
            result.email = email
            result.status = "sent" if email.sent else qual.verdict
        else:
            result.log("skip", f"{qual.verdict} --- no outreach")
    except Exception as exc:
        result.status = "failed"
        result.error = str(exc)[:200]
        result.log("error", result.error)
    return result


def run_batch(domains: list[str], settings: Settings, *, offline: bool = False,
              use_browser: bool = False, force: bool = False,
              input_file: str = "domains.csv") -> list[CompanyResult]:
    store = Store(settings.db_path)
    run_id = store.start_run(input_file, len(domains))
    scraper = Scraper(settings.samples_dir, offline=offline, use_browser=use_browser)
    llm = get_llm(settings)
    icp = settings.load_icp()

    results: list[CompanyResult] = []
    n_qualified = 0
    for domain in domains:
        domain = domain.strip()
        if not domain:
            continue
        if not force:
            cached = store.get_cached(domain, settings.cache_ttl_days)
            if cached:
                cached.log("cache", "reused cached research (within TTL)")
                results.append(cached)
                if cached.status in ("sent",) or cached.status in _QUALIFIED:
                    n_qualified += 1
                continue
        result = process_one(domain, scraper, llm, icp, settings)
        if result.status in _QUALIFIED or result.status == "sent":
            n_qualified += 1
        store.save_result(run_id, result)
        results.append(result)

    store.finish_run(run_id, n_qualified)
    store.close()
    return results


def _display(result: CompanyResult) -> dict:
    return getattr(result, "_raw", None) or result.to_dict()


def format_report(results: list[CompanyResult]) -> str:
    lines = ["", "=" * 64, "BRENDO RUN REPORT", "=" * 64]
    counts: dict[str, int] = {}
    for r in results:
        d = _display(r)
        counts[d["status"]] = counts.get(d["status"], 0) + 1
        lines.append(f"\n[{d['status'].upper()}] {d['domain']}")
        q = d.get("qualification") or {}
        for reason in q.get("reasons", []):
            lines.append(f"   why: {reason}")
        c = d.get("contact") or {}
        if c.get("name"):
            lines.append(f"   poc: {c['name']} ({c.get('role')}) <{c.get('email')}> "
                         f"[{c.get('source')} conf={c.get('confidence')}]")
        e = d.get("email") or {}
        if e.get("subject"):
            lines.append(f"   email: \"{e['subject']}\"  hook: {e.get('hook')}")
        if d.get("error"):
            lines.append(f"   error: {d['error']}")
    lines.append("\n" + "-" * 64)
    lines.append("totals: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    lines.append("-" * 64)
    return "\n".join(lines)
