"""The campaign record (deck slide 38) and the review loop (slide 37).

--export writes, per run:
  out/campaign-<run>.csv --- one row per company: insights, suggested approach, draft,
                             status, next step, plus three blank review columns
                             (review_outcome, poc_correct, review_notes).
  out/campaign-<run>.md  --- the same, readable, with the 'why' for every company.

The Growth Lead fills the review columns and runs --review on the CSV. That stores the
outcomes and prints the two numbers the kill criterion needs: rewrite rate and PoC accuracy.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from .store import Store

COLUMNS = ["domain", "company", "status", "rank", "why_company", "checks", "contact", "role",
           "contact_email", "why_contact", "pain_1", "pain_2", "hook", "subject", "body",
           "next_step", "review_outcome", "poc_correct", "review_notes"]

REVIEW_OUTCOMES = ("approved", "edited", "rejected")


def _next_step(d: dict) -> str:
    s = d.get("status")
    flags = (d.get("qualification") or {}).get("flags") or []
    if s in ("sent", "drafted"):
        return "Review the draft in your inbox; edit if needed; send from your own mailbox"
    if s in ("qualified", "qualified_flagged"):
        return "Qualified but not drafted: " + (flags[-1] if flags else "see checks")
    if s == "flagged":
        return "Manual review: decide fit by hand"
    if s == "failed":
        return "Retry next run, or research by hand: " + (d.get("error") or "")
    return "No outreach"


def row_for(d: dict) -> dict:
    q = d.get("qualification") or {}
    c = d.get("contact") or {}
    e = d.get("email") or {}
    co = d.get("company") or {}
    pains = e.get("pains") or []
    return {
        "domain": d["domain"],
        "company": co.get("name") or "",
        "status": d.get("status", ""),
        "rank": q.get("rank", ""),
        "why_company": "; ".join(q.get("reasons") or []),
        "checks": " | ".join(q.get("flags") or []),
        "contact": c.get("name") or "",
        "role": c.get("role") or "",
        "contact_email": c.get("email") or "",
        "why_contact": c.get("reason") or "",
        "pain_1": pains[0] if len(pains) > 0 else "",
        "pain_2": pains[1] if len(pains) > 1 else "",
        "hook": e.get("hook") or "",
        "subject": e.get("subject") or "",
        "body": e.get("body") or "",
        "next_step": _next_step(d),
        "review_outcome": "", "poc_correct": "", "review_notes": "",
    }


def render(results: list[dict], reviews: dict[str, dict] | None = None,
           title: str = "Campaign record") -> tuple[str, str]:
    """The campaign record as (CSV text, Markdown text). `reviews` optionally fills the
    review columns: {domain: {"review_outcome", "poc_correct", "review_notes"}}."""
    rows = [row_for(d) for d in results]
    for r in rows:
        given = (reviews or {}).get(r["domain"]) or {}
        for col in ("review_outcome", "poc_correct", "review_notes"):
            if given.get(col):
                r[col] = str(given[col])
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows(rows)

    md = [f"# {title}", ""]
    order = {"sent": 0, "drafted": 0, "qualified": 1, "qualified_flagged": 2, "flagged": 3,
             "failed": 4, "disqualified": 5}
    rank_order = {"high": 0, "normal": 1, "lower": 2, "": 3}
    for r in sorted(rows, key=lambda r: (order.get(r["status"], 9), rank_order.get(r["rank"], 3))):
        md.append(f"## {r['company'] or r['domain']} ({r['domain']}) --- {r['status']}"
                  + (f", rank {r['rank']}" if r["rank"] else ""))
        md.append(f"- **Why this company:** {r['why_company'] or '---'}")
        if r["checks"]:
            md.append(f"- **Check:** {r['checks']}")
        if r["contact"]:
            md.append(f"- **Contact:** {r['contact']}, {r['role']} <{r['contact_email']}>")
            md.append(f"- **Why this person:** {r['why_contact']}")
        if r["pain_1"]:
            md.append(f"- **Pains:** {r['pain_1']} / {r['pain_2']}")
        if r["subject"]:
            md.append(f"- **Hook:** {r['hook']}")
            md.append("")
            md.append(f"> **{r['subject']}**")
            md.append(">")
            for line in r["body"].splitlines():
                md.append(f"> {line}")
        md.append(f"- **Next step:** {r['next_step']}")
        md.append("")
    return buf.getvalue(), "\n".join(md)


def export(store: Store, run_id: str, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"campaign-{run_id}.csv"
    md_path = out_dir / f"campaign-{run_id}.md"
    csv_text, md_text = render(store.results_for_run(run_id), title=f"Campaign record --- run {run_id}")
    csv_path.write_text(csv_text, encoding="utf-8", newline="")
    md_path.write_text(md_text, encoding="utf-8")
    return csv_path, md_path


def _yes_no(v: str) -> bool | None:
    v = (v or "").strip().lower()
    if v in ("y", "yes", "true", "1"):
        return True
    if v in ("n", "no", "false", "0"):
        return False
    return None


def import_reviews(store: Store, csv_path: Path) -> tuple[int, list[str]]:
    """Read a reviewed campaign CSV. Returns (rows stored, problems)."""
    name = csv_path.stem
    run_id = name.split("campaign-", 1)[1] if name.startswith("campaign-") else None
    if not run_id:
        return 0, [f"can't tell the run from the file name '{csv_path.name}' (expected campaign-<run>.csv)"]
    n, problems = 0, []
    with csv_path.open(encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=2):
            outcome = (row.get("review_outcome") or "").strip().lower()
            poc_ok = _yes_no(row.get("poc_correct", ""))
            if not outcome and poc_ok is None:
                continue
            if outcome and outcome not in REVIEW_OUTCOMES:
                problems.append(f"line {i}: review_outcome '{outcome}' is not one of {', '.join(REVIEW_OUTCOMES)}")
                continue
            store.save_review(run_id, row["domain"], outcome or "", poc_ok, row.get("review_notes", ""))
            n += 1
    return n, problems
