import sys
from pathlib import Path

import pytest

# make `import brendo` work without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brendo.config import get_settings  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    """Offline, all-mock settings with an isolated DB, outbox, contacts file and out dir.

    Keys from a developer's .env are blanked so tests never call a live service.
    """
    s = get_settings()
    s.anthropic_api_key = ""
    s.gemini_api_key = ""
    s.llm_provider_pref = "auto"
    s.hunter_api_key = ""
    s.gmail_address = ""
    s.gmail_app_password = ""
    s.apollo_contact = ""
    s.send_to = "me@mydomain.test"
    s.db_path = tmp_path / "data" / "t.db"
    s.out_dir = tmp_path / "out"
    s.contacts_path = tmp_path / "contacts.csv"
    s.fetch_delay = 0.0
    return s


_DB_ENV = ("BRENDO_DATABASE_URL", "DATABASE_URL", "POSTGRES_URL")


@pytest.fixture(autouse=True)
def _database_backend(monkeypatch):
    """Default: SQLite (any DATABASE_URL from the shell is ignored).
    With BRENDO_TEST_PG=<postgres url>, every test runs against a fresh Postgres schema."""
    import os
    pg = os.environ.get("BRENDO_TEST_PG", "")
    for k in _DB_ENV:
        monkeypatch.delenv(k, raising=False)
    if pg:
        import psycopg
        with psycopg.connect(pg, autocommit=True) as c:
            c.execute("DROP SCHEMA public CASCADE")
            c.execute("CREATE SCHEMA public")
        monkeypatch.setenv("DATABASE_URL", pg)
    yield
