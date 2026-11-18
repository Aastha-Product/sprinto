"""Turn raw HTML into model-ready signal.

The deck's rule: strip nav/footer/cookie, dedupe repeated blocks, keep the real
content, cap the output (~2,000 tokens). One adaptation: we keep short lines that
carry a named signal (certifications, 'Series B', 'open roles', 'enterprise'),
because the 8-word drop would otherwise discard exactly the facts we qualify on.

<header> is NOT dropped wholesale: many sites put the hero <h1> there, and the deck says
keep hero copy. Its navigation still goes, because the <nav> inside it is dropped.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

MAX_CHARS = 8000  # ~2,000 tokens

_BOILERPLATE = re.compile(
    r"(cookie|accept all|sign up|sign in|log ?in|subscribe|© ?\d{4}|all rights reserved"
    r"|privacy policy|terms of service|follow us|back to top)",
    re.IGNORECASE,
)
_SIGNAL = re.compile(
    r"(soc ?2|iso ?27001|gdpr|hipaa|pci|series [a-e]|seed|open roles?|enterprise|"
    r"saas|compliance|funding|raised|security|audit|grc|ciso|questionnaire)",
    re.IGNORECASE,
)
_DROP_TAGS = {"script", "style", "nav", "footer", "form", "aside", "noscript", "svg"}


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in _DROP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.out.append(data.strip())


def _raw_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup  # preferred when installed
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(list(_DROP_TAGS)):
            t.decompose()
        return soup.get_text("\n")
    except Exception:
        p = _Stripper()
        p.feed(html)
        return "\n".join(p.out)


def clean_html(html: str) -> str:
    text = _raw_text(html or "")
    seen: set[str] = set()
    kept: list[str] = []
    for ln in (l.strip() for l in text.splitlines()):
        if not ln or ln in seen:
            continue
        if _BOILERPLATE.search(ln):
            continue
        if len(ln.split()) < 8 and not _SIGNAL.search(ln):
            continue
        seen.add(ln)
        kept.append(ln)
    return "\n".join(kept)[:MAX_CHARS]
