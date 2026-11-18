"""Single-fetch scraper (deck's narrowest, lowest-risk kind).

Order of operations: check robots.txt, request the page, and only fall back to a
headless browser if the response arrives near-empty (JS-rendered). Blocks and
CAPTCHAs are not fought --- we log the reason and move on (graceful degradation).

Offline mode reads from samples/<domain>/<file>.html so the whole pipeline (and the
tests) run with no network. Path -> file: '/' -> index, '/careers' -> careers, etc.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from urllib import robotparser

from .cleaner import clean_html

_MIN_CONTENT = 200  # chars of clean text below which we suspect a JS shell


@dataclass
class Fetch:
    url: str
    raw: str
    clean: str
    method: str          # request | browser | offline | cache
    status: str          # ok | thin | missing | blocked | error
    reason: str = ""

    @property
    def failed(self) -> bool:
        """The page could not be read at all (as opposed to 'read, but says little')."""
        return self.status in ("missing", "blocked", "error")


def _path_to_file(path: str) -> str:
    name = (path.strip("/") or "index").replace("/", "_")
    return f"{name}.html"


class Scraper:
    def __init__(self, samples_dir: Path, offline: bool = False, use_browser: bool = False,
                 delay: float = 0.0):
        self.samples_dir = samples_dir
        self.offline = offline
        self.use_browser = use_browser
        self.delay = delay  # polite pause between requests to the same site (slide 22)
        self._robots: dict[str, robotparser.RobotFileParser] = {}
        self._last_hit: dict[str, float] = {}

    def _pause(self, domain: str) -> None:
        if self.offline or self.delay <= 0:
            return
        last = self._last_hit.get(domain)
        if last is not None:
            wait = self.delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[domain] = time.monotonic()

    # --- robots ---
    def robots_allowed(self, domain: str, path: str) -> bool:
        if self.offline:
            return True
        rp = self._robots.get(domain)
        if rp is None:
            rp = robotparser.RobotFileParser()
            try:
                rp.set_url(f"https://{domain}/robots.txt")
                rp.read()
            except Exception:
                rp = None  # robots unreachable -> treat as allowed, note it
            self._robots[domain] = rp  # type: ignore[assignment]
        return True if rp is None else rp.can_fetch("*", f"https://{domain}{path}")

    # --- fetch ---
    def fetch(self, domain: str, path: str = "/") -> Fetch:
        url = f"https://{domain}{path}"
        if self.offline:
            return self._fetch_offline(domain, path, url)
        if not self.robots_allowed(domain, path):
            return Fetch(url, "", "", "request", "blocked", "robots.txt disallows")
        self._pause(domain)
        try:
            raw = self._request(url)
        except Exception as exc:
            status = "blocked" if _looks_blocked(exc) else "error"
            return Fetch(url, "", "", "request", status, str(exc)[:120])
        clean = clean_html(raw)
        if len(clean) < _MIN_CONTENT and self.use_browser:
            try:
                raw = self._browser(url)
                clean = clean_html(raw)
                return Fetch(url, raw, clean, "browser", "ok" if clean else "thin")
            except Exception as exc:
                return Fetch(url, raw, clean, "browser", "thin", str(exc)[:120])
        return Fetch(url, raw, clean, "request", "ok" if clean else "thin")

    def _request(self, url: str) -> str:
        import httpx
        headers = {"User-Agent": "BrendoResearchBot/0.1 (+contact in README)"}
        r = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
        r.raise_for_status()
        return r.text

    def _browser(self, url: str) -> str:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page()
            page.goto(url, wait_until="networkidle", timeout=20000)
            html = page.content()
            b.close()
            return html

    def _fetch_offline(self, domain: str, path: str, url: str) -> Fetch:
        f = self.samples_dir / domain / _path_to_file(path)
        if not f.exists():
            return Fetch(url, "", "", "offline", "missing", f"no sample {f.name}")
        raw = f.read_text(encoding="utf-8")
        clean = clean_html(raw)
        return Fetch(url, raw, clean, "offline", "ok" if clean else "thin")


def _looks_blocked(exc: Exception) -> bool:
    """403/429/503 from a bot wall or rate limit: logged as blocked, never bypassed (slide 21)."""
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) in (401, 403, 429, 503)
