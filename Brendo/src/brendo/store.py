"""SQLite cache + decision log + run metrics + review outcomes.

Jobs:
  1. Cost dial #3 (slide 15) --- don't research the same company twice within the TTL.
     Only FINAL verdicts are cached; a failed company is retried on the next run.
  2. Defensibility (FR9) --- an append-only record of why, grouped by run, so `--why`
     explains one run instead of a pile-up of every run.
  3. Measurement (slide 37) --- time, tokens and cost per run, and the Growth Lead's
     review outcomes (rewrite rate, PoC accuracy) for the kill criterion.

Older databases are upgraded in place: missing columns/tables are added on open.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .schemas import CompanyResult

# A result is cached only when it is a decision, not an accident.
FINAL_STATUSES = {"sent", "drafted", "qualified", "qualified_flagged", "flagged", "escalated",
                  "disqualified"}

_RUN_COLUMNS = {
    "n_sent": "INTEGER", "n_failed": "INTEGER", "seconds_total": "REAL",
    "input_tokens": "INTEGER", "output_tokens": "INTEGER", "est_cost_usd": "REAL",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
                input_file TEXT, n_total INTEGER, n_qualified INTEGER
            );
            CREATE TABLE IF NOT EXISTS companies (
                domain TEXT PRIMARY KEY, run_id TEXT, status TEXT,
                researched_at TEXT, result_json TEXT
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY, domain TEXT, step TEXT, rationale TEXT, at TEXT
            );
            CREATE TABLE IF NOT EXISTS results (
                run_id TEXT, domain TEXT, status TEXT, result_json TEXT, at TEXT,
                PRIMARY KEY (run_id, domain)
            );
            CREATE TABLE IF NOT EXISTS reviews (
                run_id TEXT, domain TEXT, outcome TEXT, poc_correct INTEGER,
                notes TEXT, reviewed_at TEXT,
                PRIMARY KEY (run_id, domain)
            );
            """
        )
        self._add_columns("runs", _RUN_COLUMNS)
        self._add_columns("decisions", {"run_id": "TEXT", "seq": "INTEGER"})
        self.db.commit()

    def _add_columns(self, table: str, cols: dict[str, str]) -> None:
        have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols.items():
            if name not in have:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")

    # --- runs ---
    def start_run(self, input_file: str, n_total: int) -> str:
        rid = uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO runs(id, started_at, input_file, n_total, n_qualified) VALUES (?,?,?,?,0)",
            (rid, _now(), input_file, n_total),
        )
        self.db.commit()
        return rid

    def finish_run(self, run_id: str, n_qualified: int, *, n_sent: int = 0, n_failed: int = 0,
                   seconds_total: float = 0.0, input_tokens: int = 0, output_tokens: int = 0,
                   est_cost_usd: float | None = None) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, n_qualified=?, n_sent=?, n_failed=?, seconds_total=?, "
            "input_tokens=?, output_tokens=?, est_cost_usd=? WHERE id=?",
            (_now(), n_qualified, n_sent, n_failed, seconds_total, input_tokens, output_tokens,
             est_cost_usd, run_id),
        )
        self.db.commit()

    def latest_run_id(self) -> str | None:
        row = self.db.execute(
            "SELECT id FROM runs WHERE finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    # --- cache ---
    def get_cached(self, domain: str, ttl_days: int) -> CompanyResult | None:
        row = self.db.execute(
            "SELECT status, researched_at, result_json FROM companies WHERE domain=?", (domain,)
        ).fetchone()
        if not row or not row["result_json"] or row["status"] not in FINAL_STATUSES:
            return None
        try:
            researched = datetime.fromisoformat(row["researched_at"])
        except Exception:
            return None
        if (datetime.now(timezone.utc) - researched).days > ttl_days:
            return None
        return _result_from_json(json.loads(row["result_json"]))

    # --- persist ---
    def save_result(self, run_id: str, result: CompanyResult) -> None:
        payload = json.dumps(result.to_dict())
        # per-run snapshot (feeds --export) is always written
        self.db.execute(
            "INSERT OR REPLACE INTO results(run_id, domain, status, result_json, at) VALUES (?,?,?,?,?)",
            (run_id, result.domain, result.status, payload, _now()),
        )
        if result.status in FINAL_STATUSES:
            self.db.execute(
                "INSERT OR REPLACE INTO companies(domain, run_id, status, researched_at, result_json) "
                "VALUES (?,?,?,?,?)",
                (result.domain, run_id, result.status, _now(), payload),
            )
        else:
            # never let a failure shadow an older good result or block a retry
            self.db.execute("DELETE FROM companies WHERE domain=? AND status NOT IN (%s)"
                            % ",".join("?" * len(FINAL_STATUSES)),
                            (result.domain, *FINAL_STATUSES))
        self.db.execute("DELETE FROM decisions WHERE domain=? AND run_id=?", (result.domain, run_id))
        for i, d in enumerate(result.decisions):
            self.db.execute(
                "INSERT INTO decisions(id, run_id, domain, seq, step, rationale, at) VALUES (?,?,?,?,?,?,?)",
                (uuid.uuid4().hex[:12], run_id, result.domain, i, d["step"], d["rationale"], _now()),
            )
        self.db.commit()

    def save_cached_reference(self, run_id: str, result: CompanyResult) -> None:
        """Record that a run reused a cached result (so --export shows it), without new decisions."""
        raw = getattr(result, "_raw", None) or result.to_dict()
        self.db.execute(
            "INSERT OR REPLACE INTO results(run_id, domain, status, result_json, at) VALUES (?,?,?,?,?)",
            (run_id, result.domain, raw.get("status", result.status), json.dumps(raw), _now()),
        )
        self.db.commit()

    def decisions_for(self, domain: str, run_id: str | None = None) -> list[sqlite3.Row]:
        """Decisions from one run: the given run, else the latest run that researched the domain."""
        if run_id is None:
            row = self.db.execute(
                "SELECT run_id FROM decisions WHERE domain=? AND run_id IS NOT NULL "
                "ORDER BY at DESC LIMIT 1", (domain,)
            ).fetchone()
            if row is None:  # legacy rows from before runs were tracked
                return self.db.execute(
                    "SELECT step, rationale, at FROM decisions WHERE domain=? ORDER BY at", (domain,)
                ).fetchall()
            run_id = row["run_id"]
        return self.db.execute(
            "SELECT step, rationale, at FROM decisions WHERE domain=? AND run_id=? ORDER BY seq",
            (domain, run_id),
        ).fetchall()

    def results_for_run(self, run_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT result_json FROM results WHERE run_id=? ORDER BY domain", (run_id,)
        ).fetchall()
        return [json.loads(r["result_json"]) for r in rows]

    # --- reviews (slide 37 measurement plan) ---
    def save_review(self, run_id: str, domain: str, outcome: str, poc_correct: bool | None,
                    notes: str = "") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO reviews(run_id, domain, outcome, poc_correct, notes, reviewed_at) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, domain, outcome,
             None if poc_correct is None else int(poc_correct), notes, _now()),
        )
        self.db.commit()

    def review_stats(self, run_id: str | None = None) -> dict:
        where, args = ("WHERE run_id=?", (run_id,)) if run_id else ("", ())
        rows = self.db.execute(f"SELECT outcome, poc_correct FROM reviews {where}", args).fetchall()
        reviewed = [r for r in rows if r["outcome"] in ("approved", "edited", "rejected")]
        edited = sum(1 for r in reviewed if r["outcome"] in ("edited", "rejected"))
        poc = [r["poc_correct"] for r in rows if r["poc_correct"] is not None]
        return {
            "reviewed": len(reviewed),
            "rewrite_rate": (edited / len(reviewed)) if reviewed else None,
            "poc_checked": len(poc),
            "poc_accuracy": (sum(poc) / len(poc)) if poc else None,
        }

    # --- privacy ---
    def purge(self, domain: str) -> int:
        n = 0
        for table in ("companies", "decisions", "results", "reviews"):
            n += self.db.execute(f"DELETE FROM {table} WHERE domain=?", (domain,)).rowcount
        self.db.commit()
        return n

    def close(self) -> None:
        self.db.close()


def _result_from_json(d: dict) -> CompanyResult:
    """Rehydrate a cached result loosely (dicts are fine for reporting)."""
    r = CompanyResult(domain=d["domain"], status=d.get("status", "pending"))
    r.decisions = d.get("decisions", [])
    r.error = d.get("error")
    r._cached = True  # type: ignore[attr-defined]
    r._raw = d        # type: ignore[attr-defined]
    return r
