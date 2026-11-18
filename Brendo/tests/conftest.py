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
