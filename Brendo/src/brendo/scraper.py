"""Single-fetch scraper (deck's narrowest, lowest-risk kind).

Order of operations: check robots.txt, request the page, and only fall back to a
headless browser if the response arrives near-empty (JS-rendered). Blocks and
CAPTCHAs are not fought --- we log the reason and move on (graceful degradation).

robots.txt is fetched with Brendo's own User-Agent (not Python's default, which bot
walls answer with 403) and read per RFC 9309: 2xx = obey the rules, 4xx = no rules,
5xx / unreachable = assume everything is disallowed. Redirects are followed.

Each fetch also returns the page's links, so the qualifier can follow a company's own
"Security" / "Trust" / "SOC 2" link instead of guessing a path.

Offline mode reads from samples/<domain>/<file>.html so the whole pipeline (and the
tests) run with no network. Path -> file: '/' -> index, '/careers' -> careers, etc.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

from .cleaner import clean_html

_MIN_CONTENT = 200  # chars of clean text below which we suspect a JS shell
USER_AGENT = "BrendoResearchBot/0.2 (+single-fetch research; respects robots.txt)"
_AGENT = "BrendoResearchBot"


@dataclass
class Fetch:
    url: str
    raw: str
    clean: str
    method: str          # request | browser | offline
    status: str          # ok | thin | missing | blocked | error
    reason: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)   # (absolute url, link text)
    final_url: str = ""          # where redirects ended (identity check)

    @property
    def failed(self) -> bool:
        """The page could not be read at all (as opposed to 'read, but says little')."""
        return self.status in ("missing", "blocked", "error")


def _path_to_file(path: str) -> str:
    name = (path.strip("/") or "index").replace("/", "_")
    return f"{name}.html"


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    out, seen = [], set()
    for m in re.finditer(r'<a\b[^>]*\bhref\s*=\s*["\']([^"\'#]+)["\'][^>]*>(.*?)</a>',
                         html or "", re.IGNORECASE | re.DOTALL):
        href = urljoin(base_url, m.group(1).strip())
        if not href.startswith("http") or href in seen:
            continue
        seen.add(href)
        text = re.sub(r"<[^>]+>", " ", m.group(2))
        out.append((href, re.sub(r"\s+", " ", text).strip()[:80]))
    return out


class Scraper:
    def __init__(self, samples_dir: Path, offline: bool = False, use_browser: bool = False,
                 delay: float = 0.0):
        self.samples_dir = samples_dir
        self.offline = offline
        self.use_browser = use_browser
        self.delay = delay  # polite pause between requests to the same site (slide 22)
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._robots_note: dict[str, str] = {}
        self._last_hit: dict[str, float] = {}

    def _pause(self, host: str) -> None:
        if self.offline or self.delay <= 0:
            return
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    # --- robots ---
    def _load_robots(self, host: str) -> robotparser.RobotFileParser | None:
        """None = no rules (allow all). A parser with disallow_all = deny all."""
        rp = robotparser.RobotFileParser()
        try:
            import httpx
            r = httpx.get(f"https://{host}/robots.txt", headers={"User-Agent": USER_AGENT},
                          timeout=15, follow_redirects=True)
        except Exception as exc:
            self._robots_note[host] = f"robots.txt unreachable ({str(exc)[:60]})"
            rp.disallow_all = True
            return rp
        if r.status_code >= 500:
            self._robots_note[host] = f"robots.txt server error {r.status_code}"
            rp.disallow_all = True
            return rp
        if r.status_code >= 400:
            return None            # RFC 9309: unavailable robots.txt = no restrictions
        rp.parse(r.text.splitlines())
        return rp

    def robots_allowed(self, host: str, path: str) -> bool:
        if self.offline:
            return True
        if host not in self._robots:
            self._robots[host] = self._load_robots(host)
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(_AGENT, f"https://{host}{path or '/'}")

    # --- fetch ---
    def fetch(self, domain: str, path: str = "/") -> Fetch:
        """Fetch a path on `domain`, or an absolute URL (a link the company published)."""
        url = path if path.startswith("http") else f"https://{domain}{path}"
        if self.offline:
            return self._fetch_offline(domain, url)
        parsed = urlparse(url)
        host, upath = parsed.netloc, (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        if not self.robots_allowed(host, upath):
            note = self._robots_note.get(host, "robots.txt disallows")
            return Fetch(url, "", "", "request", "blocked", note)
        self._pause(host)
        try:
            res = self._request(url)
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code in (404, 410):
                return Fetch(url, "", "", "request", "missing", f"HTTP {code}")
            status = "blocked" if _looks_blocked(exc) else "error"
            return Fetch(url, "", "", "request", status, str(exc)[:120])
        raw, final = res if isinstance(res, tuple) else (res, url)
        clean = clean_html(raw)
        links = extract_links(raw, final)
        if len(clean) < _MIN_CONTENT and self.use_browser:
            try:
                raw = self._browser(url)
                return Fetch(url, raw, clean_html(raw), "browser",
                             "ok" if clean_html(raw) else "thin", links=extract_links(raw, url))
            except Exception as exc:
                return Fetch(url, raw, clean, "browser", "thin", str(exc)[:120], links)
        return Fetch(url, raw, clean, "request", "ok" if clean else "thin", links=links,
                     final_url=final)

    def _request(self, url: str) -> tuple[str, str]:
        import httpx
        r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)
        r.raise_for_status()
        return r.text, str(r.url)

    def _browser(self, url: str) -> str:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle", timeout=20000)
            html = page.content()
            b.close()
            return html

    def _fetch_offline(self, domain: str, url: str) -> Fetch:
        parsed = urlparse(url)
        if parsed.netloc != domain:
            return Fetch(url, "", "", "offline", "missing", "off-site link (offline)")
        f = self.samples_dir / domain / _path_to_file(parsed.path or "/")
        if not f.exists():
            return Fetch(url, "", "", "offline", "missing", f"no sample {f.name}")
        raw = f.read_text(encoding="utf-8")
        clean = clean_html(raw)
        return Fetch(url, raw, clean, "offline", "ok" if clean else "thin",
                     links=extract_links(raw, url))


def _looks_blocked(exc: Exception) -> bool:
    """401/403/429/503 from a bot wall or rate limit: logged as blocked, never bypassed (slide 21)."""
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) in (401, 403, 429, 503)


def _as_html(text: str) -> str:
    """Plain-text fixture pages become simple HTML: one paragraph per line."""
    if "<" in text and ">" in text:
        return text
    import html as _h
    return "<main>" + "".join(f"<p>{_h.escape(ln)}</p>" for ln in text.splitlines() if ln.strip()) + "</main>"


class FixtureScraper(Scraper):
    """Serves fixed pages instead of the web: the evaluation harness's 'fixed scenario'
    (Evals deck: the exact input, no variations). pages = {"/": text_or_html, ...};
    redirect_to = a URL the homepage pretends to redirect to (identity tests)."""

    def __init__(self, pages: dict, redirect_to: str | None = None):
        super().__init__(Path("."), offline=True)
        self.pages = {(k if k.startswith("/") else "/" + k).rstrip("/") or "/": v for k, v in pages.items()}
        self.redirect_to = redirect_to

    def fetch(self, domain: str, path: str = "/") -> Fetch:
        url = path if path.startswith("http") else f"https://{domain}{path}"
        parsed = urlparse(url)
        key = (parsed.path or "/").rstrip("/") or "/"
        if parsed.netloc != domain or key not in self.pages:
            return Fetch(url, "", "", "fixture", "missing", "not in fixture")
        raw = _as_html(str(self.pages[key]))
        clean = clean_html(raw)
        final = self.redirect_to if (key == "/" and self.redirect_to) else url
        return Fetch(url, raw, clean, "fixture", "ok" if clean else "thin",
                     links=extract_links(raw, url), final_url=final)
