"""Brendo evaluation harness (Evals deck: "The harness is what actually runs all this").

For every row in evals/cases/*.json it:
  1. sends the row's fixed Input to the DEPLOYED system over its public HTTP API
     (the harness never imports Brendo's code; it is a black-box test);
  2. captures the output;
  3. checks the HARD assertions in code, and asks an LLM JUDGE to check the SOFT ones
     (the judge comes from a different model family than the agent, so they don't
     share blind spots);
  4. records pass/fail with the row's severity;
  5. writes a report: totals by severity, a one-line diagnosis per failure, and the
     ship decision.

Severity: Critical = kill criterion (one failure blocks shipping) · High = counts
against the pass-rate threshold · Low = logged, never blocks.

Self-checks, every run:
  - Run it twice: every row runs --repeat times (default 2); a row whose verdict
    changes between runs is FLAKY and counts as a failure.
  - The test that should fail: rows marked must_fail (the canary) must fail; if one
    passes, the harness isn't checking anything and the run is void.
  - Break it on purpose: each real output is corrupted and re-checked; a row whose
    hard assertions still pass on the broken output can't catch a wrong answer.
  - Human audit: a sample of judged rows is written for a person to grade blind;
    --audit-check compares their grades with the judge's and logs disagreements.

Usage (from Brendo/):
  python evals/harness.py --url https://<your-site>.vercel.app      # password: BRENDO_WEB_PASSWORD
  python evals/harness.py --url http://localhost:8000 --only D1
  python evals/harness.py --audit-check evals/out/audit-<stamp>.csv

Judge keys come from the environment or --env-file (default: Brendo/.env):
ANTHROPIC_API_KEY (Claude judge) or GEMINI_API_KEY (Gemini judge). Only the standard
library is used.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES_DIR = HERE / "cases"
OUT_DIR = HERE / "out"
SEVERITIES = ("Critical", "High", "Low")
HIGH_PASS_THRESHOLD = 0.9


# --- system under test -----------------------------------------------------------
class System:
    def __init__(self, url: str, password: str, timeout: int = 300):
        self.url, self.password, self.timeout = url.rstrip("/"), password, timeout

    def post(self, action: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(f"{self.url}/api/{action}", method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "X-Brendo-Password": self.password})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {"error": f"HTTP {e.code}"}
        except Exception as e:
            return 0, {"error": f"could not reach {self.url}: {e}"}

    def run_case(self, case: dict) -> tuple[int, dict]:
        if case.get("kind") == "edit":
            return self.post("edit", case["input"])
        return self.post("run", case["input"])


# --- hard assertions ---------------------------------------------------------------
def get_path(obj, path: str):
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _text(v) -> str:
    if isinstance(v, list):
        return " | ".join(_text(x) for x in v)
    if isinstance(v, dict):
        if "rationale" in v:
            return f"[{v.get('step')}] {v['rationale']}"
        return json.dumps(v)
    return "" if v is None else str(v)


def check_hard(a: dict, out: dict) -> tuple[bool, str]:
    v = get_path(out, a["path"])
    p = a["path"] or "output"
    if "equals" in a:
        return v == a["equals"], f"{p} is {v!r}, expected {a['equals']!r}"
    if "in" in a:
        return v in a["in"], f"{p} is {v!r}, expected one of {a['in']}"
    if "contains" in a:
        return a["contains"].lower() in _text(v).lower(), f"{p} does not contain {a['contains']!r}"
    if "not_contains" in a:
        return a["not_contains"].lower() not in _text(v).lower(), f"{p} contains {a['not_contains']!r}"
    if "matches" in a:
        return bool(re.search(a["matches"], _text(v))), f"{p} does not match /{a['matches']}/"
    if "lte" in a:
        return isinstance(v, (int, float)) and v <= a["lte"], f"{p} is {v!r}, expected <= {a['lte']}"
    if "gte" in a:
        return isinstance(v, (int, float)) and v >= a["gte"], f"{p} is {v!r}, expected >= {a['gte']}"
    if "exists" in a:
        return (v not in (None, "", [])) == a["exists"], f"{p} {'is empty' if a['exists'] else 'is present'}"
    return False, f"unknown assertion {a}"


def hard_failures(case: dict, status: int, out: dict) -> list[str]:
    if status != 200:
        return [f"HTTP {status}: {out.get('error', '')[:160]}"]
    return [msg for a in case["hard"] for ok, msg in [check_hard(a, out)] if not ok]


# --- judge (soft assertions) -------------------------------------------------------
def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            v = re.split(r"\s#", v)[0].strip()
            if v:
                os.environ.setdefault(k.strip(), v)


class Judge:
    """An LLM that grades soft assertions. Prefers a different model family from the agent."""

    def __init__(self, family: str, model: str, key: str):
        self.family, self.model, self.key = family, model, key

    @staticmethod
    def choose(agent_provider: str, mode: str) -> tuple["Judge | None", str]:
        have = {"anthropic": os.environ.get("ANTHROPIC_API_KEY", ""),
                "gemini": os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")}
        models = {"anthropic": os.environ.get("BRENDO_JUDGE_MODEL_ANTHROPIC", "claude-sonnet-5-5"),
                  "gemini": os.environ.get("BRENDO_JUDGE_MODEL_GEMINI", "gemini-3.5-flash-lite")}
        if mode == "none":
            return None, "judge turned off (--judge none)"
        order = ["anthropic", "gemini"]
        if mode in order:
            order = [mode]
        else:
            other = [f for f in order if f != agent_provider]
            order = other + ([agent_provider] if mode == "allow-same" else [])
        for fam in order:
            if have.get(fam):
                note = "" if fam != agent_provider else " (SAME family as the agent: shared blind spots)"
                return Judge(fam, models[fam], have[fam]), f"{fam}:{models[fam]}{note}"
        need = " or ".join(f"{f.upper()}_API_KEY" for f in order) or "a key"
        return None, (f"no judge available: the agent runs on {agent_provider}, so the judge needs "
                      f"{need} (a different family); soft rows are left unjudged")

    _SYS = ("You grade the output of an AI sales-research system against one test case. "
            "For EACH criterion decide pass or fail from the output alone; be strict: if the "
            "output does not clearly show it, it fails. Reply with ONLY JSON: "
            '{"criteria": [{"criterion": "...", "pass": true, "reason": "one sentence"}]}')

    def grade(self, case: dict, out: dict) -> list[dict]:
        prompt = json.dumps({"test_case": case["title"], "expected_behaviour": case["expected"],
                             "criteria": case["judge"], "system_output": excerpt(out)}, indent=1)
        text = self._anthropic(prompt) if self.family == "anthropic" else self._gemini(prompt)
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0) if m else text)
        lst = data.get("criteria") or []
        got = {c.get("criterion"): c for c in lst}
        res = []
        for i, crit in enumerate(case["judge"]):
            c = got.get(crit) or (lst[i] if i < len(lst) else {})   # missing = fail
            res.append({"criterion": crit, "pass": bool(c.get("pass")), "reason": str(c.get("reason", ""))[:300]})
        return res

    def _post(self, url: str, body: dict, headers: dict) -> dict:
        req = urllib.request.Request(url, method="POST", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **headers})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 503, 529) and attempt < 3:
                    time.sleep(4 * 2 ** attempt)
                    continue
                raise RuntimeError(f"judge HTTP {e.code}") from None
        raise RuntimeError("judge unavailable")

    def _anthropic(self, prompt: str) -> str:
        d = self._post("https://api.anthropic.com/v1/messages",
                       {"model": self.model, "max_tokens": 1500, "system": self._SYS,
                        "messages": [{"role": "user", "content": prompt}]},
                       {"x-api-key": self.key, "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in d.get("content", []))

    def _gemini(self, prompt: str) -> str:
        d = self._post(f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                       {"systemInstruction": {"parts": [{"text": self._SYS}]},
                        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                        "generationConfig": {"responseMimeType": "application/json"}},
                       {"x-goog-api-key": self.key})
        parts = ((d.get("candidates") or [{}])[0].get("content") or {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def excerpt(out: dict) -> dict:
    """What the judge (and a human auditor) sees: the decision, not the whole payload."""
    keep = {k: out.get(k) for k in ("status", "qualification", "contact", "email", "error",
                                    "meaningful", "reason", "correction") if k in out}
    if isinstance(keep.get("email"), dict):
        keep["email"] = {k: keep["email"].get(k) for k in
                         ("subject", "body", "hook", "hook_evidence_field", "pains", "applied_corrections")}
    if "decisions" in out:
        keep["decision_log"] = [f"[{d.get('step')}] {d.get('rationale')}" for d in out["decisions"]][-30:]
    return keep


# --- break it on purpose ---------------------------------------------------------
def broken(out: dict) -> dict:
    """A deliberately wrong version of a real output."""
    b = copy.deepcopy(out)
    b["status"] = "__broken__"
    if isinstance(b.get("qualification"), dict):
        b["qualification"].update(verdict="__broken__", reasons=[], flags=[], fetches_used=99)
    if "email" in b:
        b["email"] = {"subject": "", "body": "Dear Sir/Madam", "applied_corrections": ["__wrong__"],
                      "hook_evidence_field": None}
    b["decisions"] = []
    if "meaningful" in b:
        b["meaningful"] = not b["meaningful"]
        b["correction"] = None
    return b


# --- run -------------------------------------------------------------------------
def load_cases(only: str | None) -> list[dict]:
    cases = []
    for f in sorted(CASES_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        for c in data["cases"]:
            c["decision"] = data["decision"]
            c["file"] = f.name
            if not only or c["id"].upper().startswith(only.upper()) or only.lower() in f.stem.lower():
                cases.append(c)
    return cases


def run(args) -> int:
    load_env_file(Path(args.env_file))
    password = args.password or os.environ.get("BRENDO_WEB_PASSWORD", "")
    sut = System(args.url, password)
    status, check = sut.post("check", {"ping": False})
    if status != 200:
        print(f"Cannot use {args.url}: {check.get('error', status)}")
        return 3
    agent = check.get("provider") or ("mock" if check.get("llm") == "mock" else "unknown")
    judge, judge_note = Judge.choose(agent, args.judge)
    cases = load_cases(args.only)
    print(f"Brendo evals: {len(cases)} rows x {args.repeat} runs against {args.url} "
          f"(agent: {check.get('llm')}; judge: {judge_note})")

    rows = []
    for c in cases:
        runs = []
        for i in range(args.repeat):
            st, out = sut.run_case(c)
            fails = hard_failures(c, st, out)
            runs.append({"status": st, "out": out, "fails": fails})
            print(f"  {c['id']:6} run {i + 1}: {'pass' if not fails else 'FAIL'}"
                  + (f"  ({fails[0][:90]})" if fails else ""))
        hard_pass = [not r["fails"] for r in runs]
        flaky = len(set(hard_pass)) > 1
        soft, soft_note = None, ""
        if c.get("judge") and runs[0]["status"] == 200:
            if judge:
                try:
                    soft = judge.grade(c, runs[0]["out"])
                except Exception as exc:
                    soft_note = f"judge error: {exc}"
            else:
                soft_note = "not judged"
        ok_hard = all(hard_pass)
        ok_soft = soft is None or all(s["pass"] for s in soft)
        passed = ok_hard and ok_soft and not flaky
        if c.get("must_fail"):
            result = "canary-ok" if not any(hard_pass) else "CANARY-PASSED"
        elif passed and soft is None and c.get("judge"):
            result = "pass-hard-only"
        else:
            result = "pass" if passed else ("flaky" if flaky else "fail")
        diag = ""
        if result in ("fail", "flaky"):
            first = next((r["fails"] for r in runs if r["fails"]), [])
            soft_fail = next((s for s in (soft or []) if not s["pass"]), None)
            diag = ("verdict changed between runs; " if flaky else "") + (
                first[0] if first else f"judge: '{soft_fail['criterion']}' — {soft_fail['reason']}" if soft_fail else "")
        # break it on purpose: the hard assertions must reject a corrupted output
        catches = True
        if runs[0]["status"] == 200 and not c.get("must_fail"):
            catches = bool(hard_failures(c, 200, broken(runs[0]["out"])))
        rows.append({"id": c["id"], "decision": c["decision"], "title": c["title"],
                     "severity": c["severity"], "expected": c["expected"], "result": result,
                     "diagnosis": diag, "soft": soft, "soft_note": soft_note,
                     "catches_broken_output": catches, "out": runs[0]["out"], "case": c})
    return report(rows, args, check, judge_note)


def report(rows: list[dict], args, check: dict, judge_note: str) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    real = [r for r in rows if not r["case"].get("must_fail")]
    canary_broken = [r for r in rows if r["result"] == "CANARY-PASSED"]
    weak = [r for r in real if not r["catches_broken_output"]]
    by = {s: [r for r in real if r["severity"] == s] for s in SEVERITIES}
    failed = lambda rs: [r for r in rs if r["result"] in ("fail", "flaky")]  # noqa: E731
    crit_fail, high = failed(by["Critical"]), by["High"]
    high_rate = (1 - len(failed(high)) / len(high)) if high else 1.0
    unjudged = [r for r in real if r["result"] == "pass-hard-only"]

    if canary_broken or weak:
        verdict, code = "VOID: the harness is not checking properly (see self-checks)", 3
    elif crit_fail:
        verdict, code = "DO NOT SHIP: a Critical (kill-criterion) row failed", 2
    elif high_rate < HIGH_PASS_THRESHOLD:
        verdict, code = f"HOLD: High pass rate {high_rate:.0%} is below {HIGH_PASS_THRESHOLD:.0%}", 1
    elif unjudged:
        verdict, code = f"INCOMPLETE: hard checks pass, but {len(unjudged)} soft row(s) were not judged", 4
    else:
        verdict, code = "SHIP: no Critical failure, High rows above threshold", 0

    lines = [f"# Brendo evaluation report — {stamp} UTC", "",
             f"- System: {args.url} (agent: {check.get('llm')}, models: {check.get('models')})",
             f"- Judge: {judge_note}",
             f"- Runs per row: {args.repeat}",
             f"- **Verdict: {verdict}**", "",
             "| Severity | Rows | Passed | Failed | Not judged |", "|---|---|---|---|---|"]
    for s in SEVERITIES:
        rs = by[s]
        lines.append(f"| {s} | {len(rs)} | {sum(r['result'] == 'pass' for r in rs)} | {len(failed(rs))} | "
                     f"{sum(r['result'] == 'pass-hard-only' for r in rs)} |")
    lines += ["", "## Failures (one-line diagnosis)", ""]
    lines += [f"- **{r['id']}** [{r['severity']}] {r['title']}: {r['diagnosis']}" for r in failed(real)] or ["- none"]
    lines += ["", "## Self-checks", "",
              f"- Run it twice: {sum(r['result'] == 'flaky' for r in real)} flaky row(s)",
              f"- The test that should fail: {'OK, the canary failed as it must' if not canary_broken else 'BROKEN: the canary passed'}",
              f"- Break it on purpose: {len(real) - len(weak)}/{len(real)} rows reject a corrupted output"
              + ("" if not weak else " — too weak: " + ", ".join(r["id"] for r in weak)),
              "", "## All rows", "", "| ID | Decision | Severity | Result | Expected |", "|---|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['decision'].split(':')[0]} | {r['severity']} | {r['result']} | {r['expected']} |"
              for r in rows]
    md = OUT_DIR / f"report-{stamp}.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT_DIR / f"report-{stamp}.json").write_text(json.dumps(
        [{k: v for k, v in r.items() if k != "case"} for r in rows], indent=1, default=str), encoding="utf-8")

    judged = [r for r in real if r["soft"]]
    audit = None
    if judged:
        sample = random.Random(stamp).sample(judged, max(1, round(len(judged) * 0.1)))
        audit = OUT_DIR / f"audit-{stamp}.csv"
        with audit.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["case_id", "criterion", "expected", "system_output", "human_pass (y/n)", "human_note"])
            for r in sample:
                for s in r["soft"]:
                    w.writerow([r["id"], s["criterion"], r["expected"],
                                json.dumps(excerpt(r["out"]))[:4000], "", ""])

    print("\n" + "\n".join(lines[:lines.index("## All rows")]))
    print(f"\nReport: {md}")
    if audit:
        print(f"Human audit sample (grade it blind, then --audit-check): {audit}")
    return code


def audit_check(path: Path) -> int:
    """Compare a person's blind grades with the judge's verdicts; log every disagreement."""
    stamp = path.stem.replace("audit-", "")
    rep = path.with_name(f"report-{stamp}.json")
    verdicts = {}
    for r in json.loads(rep.read_text(encoding="utf-8")):
        for s in r.get("soft") or []:
            verdicts[(r["id"], s["criterion"])] = s
    rows, dis = 0, []
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            h = (row.get("human_pass (y/n)") or "").strip().lower()
            if h not in ("y", "n"):
                continue
            rows += 1
            j = verdicts.get((row["case_id"], row["criterion"]))
            if j and j["pass"] != (h == "y"):
                dis.append([row["case_id"], row["criterion"], "pass" if j["pass"] else "fail",
                            "pass" if h == "y" else "fail", j["reason"], row.get("human_note", "")])
    out = path.with_name(f"disagreements-{stamp}.csv")
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "criterion", "judge", "human", "judge_reason", "human_note"])
        w.writerows(dis)
    print(f"{rows} human grade(s); {len(dis)} disagreement(s) with the judge -> {out}")
    if rows:
        print(f"Judge agreement: {(rows - len(dis)) / rows:.0%}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run Brendo's evaluation table against the live system")
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--password", help="site password (default: $BRENDO_WEB_PASSWORD)")
    ap.add_argument("--repeat", type=int, default=2, help="runs per row (default 2: 'run it twice')")
    ap.add_argument("--only", help="run rows whose id starts with this (e.g. D1, A-) or one case file")
    ap.add_argument("--judge", default="auto", choices=["auto", "anthropic", "gemini", "allow-same", "none"],
                    help="auto = a different model family from the agent")
    ap.add_argument("--env-file", default=str(HERE.parent / ".env"), help="where to read judge keys")
    ap.add_argument("--audit-check", type=Path, help="compare a filled audit CSV with the judge")
    args = ap.parse_args(argv)
    if args.audit_check:
        return audit_check(args.audit_check)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
