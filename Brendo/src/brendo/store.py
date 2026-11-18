"""SQLite cache + decision log.

Two jobs from the blueprint: (1) the third cost dial --- don't research the same
company twice within the TTL; (2) defensibility (FR9) --- a durable record of why
each decision was made. Full normalised schema is in the ER diagram; this is the
pragmatic subset: a result row per company plus an append-only decisions table.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .schemas import CompanyResult


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
            """
        )
        self.db.commit()

    # --- runs ---
    def start_run(self, input_file: str, n_total: int) -> str:
        rid = uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO runs(id, started_at, input_file, n_total, n_qualified) VALUES (?,?,?,?,0)",
            (rid, _now(), input_file, n_total),
        )
        self.db.commit()
        return rid

    def finish_run(self, run_id: str, n_qualified: int) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, n_qualified=? WHERE id=?",
            (_now(), n_qualified, run_id),
        )
        self.db.commit()

    # --- cache ---
    def get_cached(self, domain: str, ttl_days: int) -> CompanyResult | None:
        row = self.db.execute(
            "SELECT researched_at, result_json FROM companies WHERE domain=?", (domain,)
        ).fetchone()
        if not row or not row["result_json"]:
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
        self.db.execute(
            "INSERT OR REPLACE INTO companies(domain, run_id, status, researched_at, result_json) "
            "VALUES (?,?,?,?,?)",
            (result.domain, run_id, result.status, _now(), json.dumps(result.to_dict())),
        )
        for d in result.decisions:
            self.db.execute(
                "INSERT INTO decisions(id, domain, step, rationale, at) VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex[:12], result.domain, d["step"], d["rationale"], _now()),
            )
        self.db.commit()

    def decisions_for(self, domain: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT step, rationale, at FROM decisions WHERE domain=? ORDER BY at", (domain,)
        ).fetchall()

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
