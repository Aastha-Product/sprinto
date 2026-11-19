"""The 7-step runner: batch over domains, isolate failures, persist everything.

One company failing never stops the batch (FR10): its reason is logged and the run
continues. A company with a final verdict researched within the cache TTL is reused
(cost dial #3). Each company is timed, and token use is totalled per run, so the report
carries the slide 37 measures (time per company) and the slide 15 cost.
"""
from __future__ import annotations

import time

from .config import Settings
from .llm import get_llm
from .scraper import Scraper
from .store import Store
from .schemas import CompanyResult
from .qualifier import SiteUnreachable
from . import qualifier, poc, painpoints, emailer

_QUALIFIED = {"qualified", "qualified_flagged"}


def process_one(domain: str, scraper: Scraper, llm, icp: dict, settings: Settings,
                corrections: list[dict] | None = None, contact=None,
                send: bool = True) -> CompanyResult:
    """Run the 7 steps for one domain. `contact` overrides the PoC lookup (evaluation
    fixtures); send=False stops at a checked draft (status 'drafted')."""
    result = CompanyResult(domain=domain)
    t0 = time.monotonic()
    try:
        company, qual = qualifier.qualify(domain, scraper, llm, icp, settings.max_fetches, result)
        result.company, result.qualification = company, qual
        result.status = qual.verdict

        if qual.verdict in _QUALIFIED:
            if contact is None:
                contact = poc.find_poc(domain, settings, result, icp.get("personas", []))
            else:
                result.log("poc", f"{contact.name} ({contact.role}) --- {contact.reason or 'given'}")
            result.contact = contact
            if not contact.email or not contact.name:
                missing = "email" if not contact.email else "name (the email must address them by name)"
                qual.flags.append(f"No usable contact ({missing}) --- find one by hand")
                result.log("halt", f"No contact {missing} --- flagged, not drafting")
                return result
            pains, dropped = painpoints.identify(company, contact, llm)
            for d in dropped:
                result.log("pains", f"dropped ungrounded pain: {d}")
            if len(pains) < 2:
                qual.flags.append("Fewer than two pain points grounded in the facts --- not drafted")
                result.log("halt", f"only {len(pains)} grounded pain point(s) --- flagged, not drafting")
                return result
            result.log("pains", " | ".join(f"{p['pain']} [{p['evidence_field']}]" for p in pains))
            email = emailer.draft(company, contact, pains, llm, result, corrections)
            if email is None:
                qual.flags.append("Draft failed the quality checks twice --- write it by hand")
                return result
            email.footer = emailer.why_footer(company, qual, contact, email, domain)
            if send:
                email = emailer.send(email, settings, result, tag=domain)
            else:
                email.drafted = True
                result.log("send", "not sent --- draft only (evaluation run)")
            result.email = email
            if email.sent:
                result.status = "sent"
            elif email.drafted:
                result.status = "drafted"
        else:
            result.log("skip", f"{qual.verdict} --- no outreach")
    except SiteUnreachable as exc:
        result.status = "failed"
        result.error = str(exc)[:200]
        result.log("error", f"{result.error} --- skipped, will retry next run")
    except Exception as exc:
        result.status = "failed"
        result.error = str(exc)[:200]
        result.log("error", result.error)
    finally:
        result.seconds = time.monotonic() - t0
    return result


def _cost(llm, settings: Settings) -> tuple[int, int, float | None]:
    tin = sum(v[0] for v in llm.usage.values())
    tout = sum(v[1] for v in llm.usage.values())
    cost = 0.0
    for model, (i, o) in llm.usage.items():
        price = settings.price_for(model)
        if price is None:
            return tin, tout, None
        cost += i / 1e6 * price[0] + o / 1e6 * price[1]
    return tin, tout, cost


def run_batch(domains: list[str], settings: Settings, *, offline: bool = False,
              use_browser: bool = False, force: bool = False,
              input_file: str = "domains.csv",
              corrections: list[dict] | None = None) -> list[CompanyResult]:
    seen: set[str] = set()
    clean_domains = []
    for d in domains:
        d = d.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
        d = d.removeprefix("www.")
        if d and d not in seen:
            seen.add(d)
            clean_domains.append(d)

    store = Store(settings.db_path)
    run_id = store.start_run(input_file, len(clean_domains))
    scraper = Scraper(settings.samples_dir, offline=offline, use_browser=use_browser,
                      delay=0.0 if offline else settings.fetch_delay)
    llm = get_llm(settings)
    icp = settings.load_icp()

    results: list[CompanyResult] = []
    t_run = time.monotonic()
    for domain in clean_domains:
        if not force:
            cached = store.get_cached(domain, settings.cache_ttl_days)
            if cached:
                cached.log("cache", "reused cached research (within TTL)")
                store.save_cached_reference(run_id, cached)
                results.append(cached)
                continue
        result = process_one(domain, scraper, llm, icp, settings, corrections)
        store.save_result(run_id, result)
        results.append(result)

    statuses = [_display(r)["status"] for r in results]
    tin, tout, cost = _cost(llm, settings)
    store.finish_run(
        run_id,
        sum(1 for s in statuses if s in _QUALIFIED or s in ("sent", "drafted")),
        n_sent=sum(1 for s in statuses if s in ("sent", "drafted")),
        n_failed=statuses.count("failed"),
        seconds_total=time.monotonic() - t_run,
        input_tokens=tin, output_tokens=tout, est_cost_usd=cost,
    )
    store.close()
    for r in results:
        r._run_id = run_id  # type: ignore[attr-defined]
    return results


def _display(result: CompanyResult) -> dict:
    return getattr(result, "_raw", None) or result.to_dict()


def format_report(results: list[CompanyResult], settings: Settings | None = None) -> str:
    lines = ["", "=" * 64, "BRENDO RUN REPORT", "=" * 64]
    counts: dict[str, int] = {}
    fresh_secs = []
    for r in results:
        d = _display(r)
        cached = getattr(r, "_cached", False)
        counts[d["status"]] = counts.get(d["status"], 0) + 1
        q = d.get("qualification") or {}
        rank = f" rank={q['rank']}" if q.get("rank") and d["status"] not in ("disqualified", "failed") else ""
        lines.append(f"\n[{d['status'].upper()}] {d['domain']}{rank}" + ("  (cached)" if cached else ""))
        for reason in q.get("reasons", []):
            lines.append(f"   why: {reason}")
        for fl in q.get("flags", []):
            lines.append(f"   check: {fl}")
        c = d.get("contact") or {}
        if c.get("name"):
            lines.append(f"   poc: {c['name']} ({c.get('role')}) <{c.get('email')}> [{c.get('source')}]")
            if c.get("reason"):
                lines.append(f"        because {c['reason']}")
        e = d.get("email") or {}
        if e.get("subject"):
            lines.append(f"   email: \"{e['subject']}\"")
            lines.append(f"   hook: {e.get('hook')}")
        if d.get("error"):
            lines.append(f"   error: {d['error']}")
        if not cached and r.seconds:
            fresh_secs.append(r.seconds)
    lines.append("\n" + "-" * 64)
    lines.append("totals: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if fresh_secs:
        lines.append(f"time:   {sum(fresh_secs) / len(fresh_secs):.1f}s per researched company "
                     f"(target < 120s)")
    run_id = getattr(results[0], "_run_id", None) if results else None
    if settings and run_id:
        st = Store(settings.db_path)
        run = st.get_run(run_id)
        st.close()
        if run and run["input_tokens"]:
            cost = run["est_cost_usd"]
            lines.append(f"tokens: {run['input_tokens']} in / {run['output_tokens']} out"
                         + (f"  ~${cost:.4f}" if cost is not None else "  (no price set for a model)"))
        lines.append(f"run:    {run_id}   (--export {run_id} for the campaign record)")
    lines.append("-" * 64)
    return "\n".join(lines)
