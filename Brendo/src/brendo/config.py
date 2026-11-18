"""Settings + a dependency-free .env loader.

Any missing key degrades that part to mock mode. With no keys at all, the whole
pipeline runs offline in mock mode (the demo / test path).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# repo root = .../brendo (three parents up from this file: src/brendo/config.py)
ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env reader. No external dependency. Does not overwrite real env vars."""
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


@dataclass
class Settings:
    anthropic_api_key: str = ""
    llm_model: str = "claude-haiku-5-5"
    llm_model_draft: str = "claude-sonnet-5-5"
    hunter_api_key: str = ""
    apollo_contact: str = ""           # legacy global hardcode --- ignored, see contacts_path
    send_to: str = "you@example.com"
    gmail_address: str = ""
    gmail_app_password: str = ""
    cache_ttl_days: int = 7
    max_fetches: int = 4
    fetch_delay: float = 1.5           # seconds between requests to the same site (slide 22)
    # USD per million tokens (input, output). Rates change: check the price page and
    # override with BRENDO_PRICE_<MODEL> = "in,out" (e.g. BRENDO_PRICE_CLAUDE_HAIKU_5_5=0.10,0.50).
    prices: dict = field(default_factory=lambda: {
        "claude-haiku-5-5": (0.10, 0.50),
        "claude-sonnet-5-5": (2.00, 10.00),
    })
    icp_path: Path = field(default_factory=lambda: ROOT / "config" / "icp.json")
    db_path: Path = field(default_factory=lambda: ROOT / "data" / "brendo.db")
    samples_dir: Path = field(default_factory=lambda: ROOT / "samples")
    contacts_path: Path = field(default_factory=lambda: ROOT / "inputs" / "contacts.csv")
    out_dir: Path = field(default_factory=lambda: ROOT / "out")

    # --- mock switches: True => use the built-in mock instead of a live service ---
    @property
    def llm_is_mock(self) -> bool:
        return not self.anthropic_api_key

    @property
    def hunter_is_mock(self) -> bool:
        return not self.hunter_api_key

    @property
    def gmail_is_mock(self) -> bool:
        return not (self.gmail_address and self.gmail_app_password)

    @property
    def send_to_is_placeholder(self) -> bool:
        to = (self.send_to or "").strip().lower()
        return not to or to.endswith("@example.com")

    def price_for(self, model: str) -> tuple[float, float] | None:
        return self.prices.get(model)

    def load_icp(self) -> dict:
        return json.loads(self.icp_path.read_text(encoding="utf-8"))


def get_settings() -> Settings:
    load_dotenv()
    e = os.environ.get
    s = Settings(
        anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
        llm_model=e("BRENDO_LLM_MODEL", "claude-haiku-5-5"),
        llm_model_draft=e("BRENDO_LLM_MODEL_DRAFT", "claude-sonnet-5-5"),
        hunter_api_key=e("HUNTER_API_KEY", ""),
        apollo_contact=e("BRENDO_APOLLO_CONTACT", ""),
        send_to=e("BRENDO_SEND_TO", "you@example.com"),
        gmail_address=e("GMAIL_ADDRESS", ""),
        gmail_app_password=e("GMAIL_APP_PASSWORD", ""),
        cache_ttl_days=int(e("BRENDO_CACHE_TTL_DAYS", "7") or 7),
        max_fetches=int(e("BRENDO_MAX_FETCHES", "4") or 4),
        fetch_delay=float(e("BRENDO_FETCH_DELAY", "1.5") or 1.5),
    )
    if e("BRENDO_CONTACTS_FILE"):
        s.contacts_path = Path(e("BRENDO_CONTACTS_FILE"))
    for k, v in os.environ.items():
        if k.startswith("BRENDO_PRICE_") and "," in v:
            model = k[len("BRENDO_PRICE_"):].lower().replace("_", "-")
            try:
                i, o = (float(x) for x in v.split(",", 1))
                s.prices[model] = (i, o)
            except ValueError:
                pass
    return s
