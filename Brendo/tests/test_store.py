import pytest
import sqlite3
from datetime import datetime, timedelta, timezone

from brendo.orchestrator import run_batch
from brendo.store import Store
from brendo.schemas import CompanyResult


def test_rerun_within_ttl_reuses_cache(settings):                       # T-CACHE-1
    run_batch(["acme.example"], settings, offline=True)
    results = run_batch(["acme.example"], settings, offline=True)
    assert getattr(results[0], "_cached", False)


def test_failed_result_is_not_cached(settings):                         # T-CACHE-2 (G6)
    run_batch(["nosuch.example"], settings, offline=True)
    results = run_batch(["nosuch.example"], settings, offline=True)
    assert not getattr(results[0], "_cached", False)
    assert results[0].status == "failed"


def test_expired_cache_is_ignored(settings):
    run_batch(["acme.example"], settings, offline=True)
    st = Store(settings.db_path)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    st.db.execute("UPDATE companies SET researched_at=?", (old,))
    st.db.commit()
    assert st.get_cached("acme.example", 7) is None
    st.close()


def test_why_shows_one_run_not_a_pile_up(settings):                     # T-LOG-1 (G7)
    run_batch(["acme.example"], settings, offline=True, force=True)
    st = Store(settings.db_path)
    one = len(st.decisions_for("acme.example"))
    st.close()
    run_batch(["acme.example"], settings, offline=True, force=True)
    st = Store(settings.db_path)
    assert len(st.decisions_for("acme.example")) == one
    st.close()


@pytest.mark.skipif(bool(__import__("os").environ.get("BRENDO_TEST_PG")),
                    reason="builds an old SQLite file; Postgres creates tables fresh")
def test_old_database_is_upgraded_in_place(settings):
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(settings.db_path)
    db.executescript("""
        CREATE TABLE runs (id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
                           input_file TEXT, n_total INTEGER, n_qualified INTEGER);
        CREATE TABLE companies (domain TEXT PRIMARY KEY, run_id TEXT, status TEXT,
                                researched_at TEXT, result_json TEXT);
        CREATE TABLE decisions (id TEXT PRIMARY KEY, domain TEXT, step TEXT, rationale TEXT, at TEXT);
        INSERT INTO decisions VALUES ('x', 'old.example', 'scrape', 'legacy row', '2026-01-01');
    """)
    db.commit()
    db.close()
    st = Store(settings.db_path)
    cols = {r["name"] for r in st.db.execute("PRAGMA table_info(decisions)")}
    assert {"run_id", "seq"} <= cols
    assert len(st.decisions_for("old.example")) == 1            # legacy rows still readable
    st.close()
    assert run_batch(["acme.example"], settings, offline=True)[0].status == "drafted"


def test_run_metrics_are_recorded(settings):                           # T-MET-1
    run_batch(["acme.example", "nosuch.example"], settings, offline=True, force=True)
    st = Store(settings.db_path)
    run = st.get_run(st.latest_run_id())
    assert run["n_total"] == 2 and run["n_sent"] == 1 and run["n_failed"] == 1
    assert run["seconds_total"] is not None
    st.close()


def test_review_stats_and_purge(settings):
    st = Store(settings.db_path)
    st.save_review("r1", "a.example", "approved", True)
    st.save_review("r1", "b.example", "edited", False)
    st.save_review("r1", "c.example", "approved", True)
    stats = st.review_stats("r1")
    assert stats["reviewed"] == 3 and abs(stats["rewrite_rate"] - 1 / 3) < 1e-9
    assert abs(stats["poc_accuracy"] - 2 / 3) < 1e-9
    st.save_result("r1", CompanyResult("a.example", status="disqualified"))
    assert st.purge("a.example") >= 2
    assert st.get_cached("a.example", 7) is None
    st.close()
