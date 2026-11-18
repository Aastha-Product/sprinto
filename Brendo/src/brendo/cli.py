"""Entry point: run the pipeline over a domain list and print a report.

  python -m brendo.cli --offline                 # demo on bundled samples, no keys
  python -m brendo.cli --domains inputs/domains.csv
  python -m brendo.cli --domain acme.example --offline --force
  python -m brendo.cli --why acme.example        # print the decision log (defensibility)
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .config import get_settings
from .orchestrator import run_batch, format_report
from .store import Store


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
    poc = "apollo-hardcode" if settings.apollo_contact else ("mock (set HUNTER_API_KEY)" if settings.hunter_is_mock else "live: hunter.io")
    print(f"  PoC           {poc}")
    print(f"  Send          {'mock-outbox (set GMAIL_ADDRESS + GMAIL_APP_PASSWORD)' if settings.gmail_is_mock else 'live: gmail'}")
    print(f"  Recipient     {settings.send_to}" + ("   <-- still the placeholder!" if settings.send_to == "you@example.com" else ""))

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
    ap.add_argument("--check", action="store_true", help="report real-mode readiness and exit")
    args = ap.parse_args(argv)

    settings = get_settings()

    if args.check:
        return _check(settings)

    if args.why:
        store = Store(settings.db_path)
        rows = store.decisions_for(args.why)
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
          f"poc: {'apollo' if settings.apollo_contact else ('mock' if settings.hunter_is_mock else 'hunter')}, "
          f"send: {'mock-outbox' if settings.gmail_is_mock else 'gmail'}")

    results = run_batch(domains, settings, offline=args.offline, use_browser=args.browser,
                        force=args.force, input_file=input_file)
    print(format_report(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
