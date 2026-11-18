"""Entry point: run the pipeline over a domain list and print a report.

  python -m brendo.cli --offline                 # demo on bundled samples, no keys
  python -m brendo.cli --domains inputs/domains.csv
  python -m brendo.cli --domain acme.example --offline --force
  python -m brendo.cli --why acme.example        # print the decision log (defensibility)
  python -m brendo.cli --export                  # campaign record for the latest run
  python -m brendo.cli --review out/campaign-<run>.csv   # import review outcomes
  python -m brendo.cli --purge acme.example      # delete everything stored for a domain
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .config import get_settings
from .orchestrator import run_batch, format_report
from .store import Store
from . import exporter, poc


def _read_domains(path: Path) -> list[str]:
    out: list[str] = []
    with path.open(encoding="utf-8") as fh:
        reader = csv.reader(fh)
        for i, row in enumerate(reader):
            if not row:
                continue
            cell = row[0].strip()
            if i == 0 and cell.lower() == "domain":
                continue
            out.append(cell)
    return out


def _check(settings) -> int:
    print("\nBrendo readiness check\n" + "-" * 44)
    for mod in ("httpx", "bs4", "anthropic"):
        try:
            __import__(mod)
            print(f"  dep {mod:10} ok")
        except Exception:
            print(f"  dep {mod:10} MISSING  (pip install -r requirements.txt)")
    try:
        n = len(settings.load_icp().get("questions", []))
        print(f"  icp config    ok ({n} questions)")
    except Exception as exc:
        print(f"  icp config    ERROR: {exc}")
    print(f"  samples dir   {'ok' if settings.samples_dir.exists() else 'missing'}")

    print(f"  LLM           {'mock (set ANTHROPIC_API_KEY)' if settings.llm_is_mock else f'live: {settings.llm_model} / {settings.llm_model_draft}'}")
    n_contacts = len(poc.load_contacts(settings.contacts_path))
    poc_mode = "mock (set HUNTER_API_KEY)" if settings.hunter_is_mock else "live: hunter.io"
    print(f"  PoC           {poc_mode}; contacts.csv: {n_contacts} hardcoded domain(s)")
    if settings.apollo_contact:
        print("  WARNING       BRENDO_APOLLO_CONTACT is ignored (it gave every company the same person);"
              " put Apollo lookups in inputs/contacts.csv instead")
    print(f"  Send          {'mock-outbox (set GMAIL_ADDRESS + GMAIL_APP_PASSWORD)' if settings.gmail_is_mock else 'live: gmail'}")
    print(f"  Recipient     {settings.send_to}" + ("   <-- placeholder: live sends will be refused!" if settings.send_to_is_placeholder else ""))
    for m in {settings.llm_model, settings.llm_model_draft}:
        if settings.price_for(m) is None:
            print(f"  Price         no price set for {m}; cost won't be estimated (BRENDO_PRICE_...)")

    if not settings.llm_is_mock:
        try:
            from .llm import RealLLM
            RealLLM(settings).ping()
            print("  LLM ping      ok (key + model work)")
        except Exception as exc:
            print(f"  LLM ping      FAILED: {str(exc)[:120]}")
    print("-" * 44)
    print("Anything 'mock' still runs end-to-end; fill keys in .env to take it live.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="brendo", description="Automated cold-outreach pipeline")
    ap.add_argument("--domains", type=Path, help="CSV of domains (header 'domain')")
    ap.add_argument("--domain", help="a single domain to run")
    ap.add_argument("--offline", action="store_true", help="read samples/ instead of the web")
    ap.add_argument("--browser", action="store_true", help="allow headless-browser fallback")
    ap.add_argument("--force", action="store_true", help="ignore the cache")
    ap.add_argument("--why", help="print the decision log for one domain and exit")
    ap.add_argument("--run", help="with --why: explain this run instead of the latest")
    ap.add_argument("--check", action="store_true", help="report real-mode readiness and exit")
    ap.add_argument("--export", nargs="?", const="latest", metavar="RUN",
                    help="write the campaign record (CSV + Markdown) for a run (default: latest)")
    ap.add_argument("--review", type=Path, metavar="CSV",
                    help="import review outcomes from a filled-in campaign CSV")
    ap.add_argument("--purge", metavar="DOMAIN", help="delete everything stored for a domain")
    args = ap.parse_args(argv)

    settings = get_settings()

    if args.check:
        return _check(settings)

    if args.export:
        store = Store(settings.db_path)
        run_id = store.latest_run_id() if args.export == "latest" else args.export
        if not run_id or not store.get_run(run_id):
            store.close()
            print("No such run. Run the pipeline first.")
            return 1
        csv_path, md_path = exporter.export(store, run_id, settings.out_dir)
        store.close()
        print(f"Campaign record for run {run_id}:\n  {csv_path}\n  {md_path}")
        print("Fill review_outcome (approved/edited/rejected) and poc_correct (y/n), then --review the CSV.")
        return 0

    if args.review:
        store = Store(settings.db_path)
        n, problems = exporter.import_reviews(store, args.review)
        stats = store.review_stats()
        store.close()
        for p in problems:
            print(f"  skipped: {p}")
        print(f"Stored {n} review(s).")
        rr, pa = stats["rewrite_rate"], stats["poc_accuracy"]
        print(f"  rewrite rate: {rr:.0%} of {stats['reviewed']} reviewed (target < 20%, kill > 50%)"
              if rr is not None else "  rewrite rate: no reviewed drafts yet")
        print(f"  PoC accuracy: {pa:.0%} of {stats['poc_checked']} checked (kill < 60%)"
              if pa is not None else "  PoC accuracy: no contacts checked yet")
        return 0

    if args.purge:
        store = Store(settings.db_path)
        n = store.purge(args.purge.strip().lower())
        store.close()
        print(f"Deleted {n} row(s) for {args.purge}.")
        return 0

    if args.why:
        store = Store(settings.db_path)
        rows = store.decisions_for(args.why.strip().lower(), args.run)
        store.close()
        if not rows:
            print(f"No decisions logged for {args.why}. Run it first.")
            return 1
        print(f"\nDecision log --- {args.why}\n" + "-" * 48)
        for r in rows:
            print(f"  [{r['step']}] {r['rationale']}")
        return 0

    if args.domain:
        domains = [args.domain]
        input_file = args.domain
    else:
        path = args.domains or (settings.icp_path.parents[1] / "inputs" / "domains.csv")
        domains = _read_domains(path)
        input_file = str(path)

    mode = "offline/samples" if args.offline else "live web"
    llm_mode = "mock" if settings.llm_is_mock else settings.llm_model
    print(f"Running {len(domains)} domain(s) --- source: {mode}, llm: {llm_mode}, "
          f"poc: contacts.csv + {'mock' if settings.hunter_is_mock else 'hunter'}, "
          f"send: {'mock-outbox' if settings.gmail_is_mock else 'gmail'}")
    if not settings.gmail_is_mock and settings.send_to_is_placeholder:
        print("WARNING: BRENDO_SEND_TO is a placeholder --- live sends will be refused.")

    results = run_batch(domains, settings, offline=args.offline, use_browser=args.browser,
                        force=args.force, input_file=input_file)
    print(format_report(results, settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
