"""Settings + a dependency-free .env loader.

Any missing key degrades that part to mock mode. With no keys at all, the whole
pipeline runs offline in mock mode (the demo / test path).
"""
from __future__ import annotations

import json
import os
import re
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
        os.environ.setdefault(key.strip(), _dotenv_value(val))


def _dotenv_value(raw: str) -> str:
    """Value of one .env line: quoted values are taken as-is; otherwise a trailing
    ' # comment' is dropped (so `KEY=value   # note` gives 'value')."""
    val = raw.strip()
    if val[:1] in ('"', "'"):
        q = val[0]
        end = val.find(q, 1)
        return val[1:end] if end != -1 else val[1:]
    cut = re.search(r"\s#", val)
    return (val[:cut.start()] if cut else val).strip()


@dataclass
class Settings:
    anthropic_api_key: str = ""
    llm_model: str = "claude-haiku-5-5"
    llm_model_draft: str = "claude-sonnet-5-5"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"      # extraction + pain points (cheap, free tier)
    gemini_model_draft: str = "gemini-4-hydrogen"    # drafting
    gemini_fallbacks: list = field(default_factory=lambda: ["gemini-3.1-flash-lite"])
    llm_provider_pref: str = "auto"                  # auto | anthropic | gemini
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
        # Gemini paid-tier rates (the free tier costs nothing; estimate assumes paid)
        "gemini-3.5-flash-lite": (0.30, 2.50),
        "gemini-4-hydrogen": (1.50, 7.50),
    })
    icp_path: Path = field(default_factory=lambda: ROOT / "config" / "icp.json")
    db_path: Path = field(default_factory=lambda: ROOT / "data" / "brendo.db")
    samples_dir: Path = field(default_factory=lambda: ROOT / "samples")
    contacts_path: Path = field(default_factory=lambda: ROOT / "inputs" / "contacts.csv")
    out_dir: Path = field(default_factory=lambda: ROOT / "out")

    # --- mock switches: True => use the built-in mock instead of a live service ---
    @property
    def provider(self) -> str:
        """Which LLM runs: anthropic | gemini | mock.

        auto picks whichever key is set (Anthropic first). A Google key (starts 'AIza')
        pasted into ANTHROPIC_API_KEY by mistake is used as the Gemini key.
        """
        pref = (self.llm_provider_pref or "auto").lower()
        has_a, has_g = bool(self.anthropic_api_key), bool(self.gemini_api_key)
        if pref == "gemini":
            return "gemini" if has_g else "mock"
        if pref == "anthropic":
            return "anthropic" if has_a else "mock"
        return "anthropic" if has_a else ("gemini" if has_g else "mock")

    @property
    def llm_is_mock(self) -> bool:
        return self.provider == "mock"

    def active_models(self) -> tuple[str, str]:
        """(extraction model, drafting model) for the active provider."""
        if self.provider == "gemini":
            return self.gemini_model, self.gemini_model_draft
        return self.llm_model, self.llm_model_draft

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


_SECRET_ENV = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "HUNTER_API_KEY",
               "GMAIL_APP_PASSWORD", "BRENDO_WEB_PASSWORD")


def redact(text: str, settings: "Settings | None" = None) -> str:
    """Replace any key or password value that appears in `text` with [hidden]."""
    secrets = {os.environ.get(k, "") for k in _SECRET_ENV}
    if settings is not None:
        secrets |= {settings.anthropic_api_key, settings.gemini_api_key,
                    settings.hunter_api_key, settings.gmail_app_password}
    for s in sorted((x for x in secrets if x and len(x) >= 6), key=len, reverse=True):
        text = text.replace(s, "[hidden]")
    return text


def get_settings() -> Settings:
    load_dotenv()
    # Names are matched case-insensitively and trimmed: on Vercel (Linux) "Gemini_API_KEY"
    # and "GEMINI_API_KEY" are different variables, and a mis-cased name silently meant mock.
    folded: dict[str, str] = {}
    for k, v in os.environ.items():
        folded.setdefault(k.strip().upper(), v)

    def e(name: str, default: str = "") -> str:
        v = os.environ.get(name)
        if v is None:
            v = folded.get(name.upper())
        return default if v is None else v.strip()
    s = Settings(
        anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
        llm_model=e("BRENDO_LLM_MODEL", "claude-haiku-5-5"),
        llm_model_draft=e("BRENDO_LLM_MODEL_DRAFT", "claude-sonnet-5-5"),
        hunter_api_key=e("HUNTER_API_KEY", ""),
        apollo_contact=e("BRENDO_APOLLO_CONTACT", ""),
        send_to=e("BRENDO_SEND_TO", "you@example.com"),
        gmail_address=e("GMAIL_ADDRESS", ""),
        # Google shows App Passwords as "abcd efgh ijkl mnop"; spaces are not part of it
        gmail_app_password=e("GMAIL_APP_PASSWORD", "").replace(" ", ""),
        cache_ttl_days=int(e("BRENDO_CACHE_TTL_DAYS", "7") or 7),
        max_fetches=int(e("BRENDO_MAX_FETCHES", "4") or 4),
        fetch_delay=float(e("BRENDO_FETCH_DELAY", "1.5") or 1.5),
        gemini_api_key=e("GEMINI_API_KEY", "") or e("GOOGLE_API_KEY", ""),
        gemini_model=e("BRENDO_GEMINI_MODEL", "") or "gemini-3.5-flash-lite",
        gemini_model_draft=e("BRENDO_GEMINI_MODEL_DRAFT", "") or "gemini-4-hydrogen",
        gemini_fallbacks=[m.strip() for m in (e("BRENDO_GEMINI_FALLBACKS", "") or "gemini-3.1-flash-lite").split(",")
                          if m.strip()],
        llm_provider_pref=e("BRENDO_LLM_PROVIDER", "auto") or "auto",
    )
    s.key_was_misplaced = False
    if s.anthropic_api_key.startswith("AIza"):
        # a Google key in the Anthropic slot: use it for Gemini instead
        s.gemini_api_key = s.gemini_api_key or s.anthropic_api_key
        s.anthropic_api_key = ""
        s.key_was_misplaced = True
    # Where the cache DB, mock outbox and exports go. Serverless hosts (Vercel) can only
    # write under /tmp, so it defaults there when running on Vercel.
    data_dir = e("BRENDO_DATA_DIR", "") or ("/tmp/brendo" if e("VERCEL") else "")
    if data_dir:
        s.db_path = Path(data_dir) / "brendo.db"
        s.out_dir = Path(data_dir) / "out"
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
