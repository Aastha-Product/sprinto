"""The website's server side, exercised over real HTTP (offline, all mock)."""
import csv
import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from brendo import web


@pytest.fixture
def server(settings, monkeypatch):
    """One Vercel-style function per action, each on its own port."""
    monkeypatch.setattr(web, "get_settings", lambda: settings)
    monkeypatch.setenv("BRENDO_WEB_PASSWORD", "s3cret")
    servers = {}
    for action in ("check", "run", "export"):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(action))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers[action] = srv
    yield servers
    for srv in servers.values():
        srv.shutdown()
        srv.server_close()


def call(servers, action, body=None, password="s3cret"):
    port = servers[action].server_address[1]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/{action}", method="POST",
                                 data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    if password is not None:
        req.add_header("X-Brendo-Password", password)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_password_is_required(server, monkeypatch):
    assert call(server, "check", password=None)[0] == 401
    assert call(server, "check", password="nope")[0] == 401
    monkeypatch.delenv("BRENDO_WEB_PASSWORD")
    status, out = call(server, "check")
    assert status == 503 and "BRENDO_WEB_PASSWORD" in out["error"]


def test_check_reports_modes_without_keys(server):
    status, out = call(server, "check", {"ping": True})
    assert status == 200
    assert out["llm"] == "mock" and out["poc"] == "mock" and out["send"].startswith("mock")
    assert out["recipient"] == "me@mydomain.test"
    assert not any("key" in k.lower() for k in out)  # never echoes keys


def test_run_one_company_offline(server):
    status, out = call(server, "run", {"domain": "acme.example", "offline": True, "force": True})
    assert status == 200, out
    assert out["status"] == "drafted"
    assert out["contact"]["name"] and out["email"]["subject"]
    assert any(d["step"] == "send" for d in out["decisions"])
    assert out["usage"]["input_tokens"] >= 0

    status, out = call(server, "run", {"domain": "securedhq.example", "offline": True})
    assert status == 200 and out["status"] == "disqualified"


def test_run_rejects_bad_domains(server):
    for bad in ("../../etc", "127.0.0.1", "localhost", "intranet.local", "not a domain"):
        status, out = call(server, "run", {"domain": bad})
        assert status == 400, bad
    status, out = call(server, "run", {"domain": "../config", "offline": True})
    assert status == 400 and "bundled sample" in out["error"]


def test_export_fills_review_columns(server):
    _, acme = call(server, "run", {"domain": "acme.example", "offline": True, "force": True})
    status, out = call(server, "export", {
        "results": [acme],
        "reviews": {"acme.example": {"review_outcome": "edited", "poc_correct": "y"}},
    })
    assert status == 200
    row = next(csv.DictReader(io.StringIO(out["csv"])))
    assert row["domain"] == "acme.example" and row["review_outcome"] == "edited"
    assert row["poc_correct"] == "y" and row["subject"]
    assert "Why this company" in out["markdown"]
    assert call(server, "export", {"results": []})[0] == 400


@pytest.mark.parametrize("raw,expected", [
    ("Attio.com", "attio.com"),
    ("https://www.dub.co/pricing", "dub.co"),
    ("plausible.io:443", "plausible.io"),
])
def test_clean_domain(raw, expected):
    assert web.clean_domain(raw) == expected


def test_local_site_serves_page_and_api_without_password(settings, monkeypatch):
    monkeypatch.setattr(web, "get_settings", lambda: settings)
    monkeypatch.delenv("BRENDO_WEB_PASSWORD", raising=False)
    started = threading.Event()
    holder = {}

    real_init = ThreadingHTTPServer.__init__

    def grab(self, addr, handler):
        real_init(self, ("127.0.0.1", 0), handler)
        holder["srv"] = self
        started.set()

    monkeypatch.setattr(ThreadingHTTPServer, "__init__", grab)
    threading.Thread(target=web.serve, daemon=True).start()
    assert started.wait(5)
    port = holder["srv"].server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            assert b"Brendo" in r.read()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/blueprint.html", timeout=10) as r:
            assert b"Blueprint" in r.read()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/check", method="POST",
                                     data=b'{"ping": false}',
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert json.loads(r.read())["llm"] == "mock"
    finally:
        holder["srv"].shutdown()
        holder["srv"].server_close()


def test_vercel_can_detect_every_api_function():
    """Vercel detects Python functions by a visible top-level `class handler(` (see api/*.py)."""
    import importlib.util
    import re
    from http.server import BaseHTTPRequestHandler
    from pathlib import Path
    api = Path(__file__).resolve().parents[1] / "api"
    files = sorted(api.glob("*.py"))
    assert files
    for f in files:
        assert re.search(r"^class handler\(", f.read_text(encoding="utf-8"), re.M), f.name
        spec = importlib.util.spec_from_file_location(f.stem, f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert issubclass(mod.handler, BaseHTTPRequestHandler) and hasattr(mod.handler, "do_POST")
