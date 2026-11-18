"""The website's server side: what the Vercel functions in api/ call.

Each request runs ONE company through the same tested pipeline the CLI uses
(orchestrator.run_batch), so a request stays well inside a serverless time limit and
the page can show progress company by company.

Access: every endpoint needs the site password (BRENDO_WEB_PASSWORD, sent in the
X-Brendo-Password header). Without it anyone with the link could spend the LLM and
Hunter quotas. If the variable is not set, the site refuses to run at all.

Safety rules carried over from the CLI, unchanged: the recipient is BRENDO_SEND_TO
and nothing else; keys are read from the environment and never returned.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
from http.server import BaseHTTPRequestHandler

from .config import Settings, get_settings
from .orchestrator import run_batch, _display
from .store import Store
from . import exporter, poc

MAX_BODY = 512 * 1024
_HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class WebError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --- access -----------------------------------------------------------------------
def check_password(given: str | None, local: bool = False) -> None:
    expected = os.environ.get("BRENDO_WEB_PASSWORD", "")
    if not expected and local:
        return  # `python -m brendo.web` on your own PC, bound to 127.0.0.1 only
    if not expected:
        raise WebError(503, "This site has no password set. In Vercel, add the environment "
                            "variable BRENDO_WEB_PASSWORD and redeploy.")
    if not given or not hmac.compare_digest(given.encode(), expected.encode()):
        raise WebError(401, "Wrong site password.")


# --- input ------------------------------------------------------------------------
def clean_domain(raw: str) -> str:
    """A bare public hostname, or WebError. Blocks IPs and internal names, since the
    server fetches https://<domain>/ on the user's behalf."""
    d = (raw or "").strip().lower()
    d = d.removeprefix("https://").removeprefix("http://").split("/")[0].split("?")[0]
    d = d.split(":")[0].removeprefix("www.").strip(".")
    try:
        ipaddress.ip_address(d)
        raise WebError(400, f"'{raw}' is an IP address; give a company domain like attio.com")
    except ValueError:
        pass
    if not _HOST_RE.match(d) or d.endswith((".local", ".internal", ".localhost")):
        raise WebError(400, f"'{raw}' is not a company domain (expected something like attio.com)")
    return d


# --- endpoints --------------------------------------------------------------------
def readiness(settings: Settings, ping: bool = True) -> dict:
    m_ext, m_draft = settings.active_models()
    out = {
        "llm": "mock" if settings.llm_is_mock else f"live: {settings.provider}",
        "models": None if settings.llm_is_mock else [m_ext, m_draft],
        "poc": "mock" if settings.hunter_is_mock else "live: hunter.io",
        "contacts_file": len(poc.load_contacts(settings.contacts_path)),
        "send": "mock (drafts only)" if settings.gmail_is_mock else "live: gmail",
        "recipient": settings.send_to,
        "recipient_placeholder": settings.send_to_is_placeholder,
        "ping": None,
        "note": None,
        "ok": True,
    }
    if ping and not settings.llm_is_mock:
        try:
            from .llm import get_llm
            llm = get_llm(settings)
            llm.ping()
            out["ping"] = "ok"
            out["note"] = getattr(llm, "ping_note", None)
        except Exception as exc:
            out["ping"] = f"FAILED: {str(exc)[:160]}"
            out["ok"] = False
    if not settings.gmail_is_mock and settings.send_to_is_placeholder:
        out["ok"] = False
    return out


def run_one(domain: str, settings: Settings, *, offline: bool = False, force: bool = False) -> dict:
    if offline:
        domain = (domain or "").strip().lower()
        samples = {p.name for p in settings.samples_dir.iterdir() if p.is_dir()}
        if domain not in samples:
            raise WebError(400, f"'{domain}' is not a bundled sample. Demo mode works on: "
                                + ", ".join(sorted(samples)))
    else:
        domain = clean_domain(domain)
    results = run_batch([domain], settings, offline=offline, force=force, input_file="web")
    r = results[0]
    d = _display(r)
    d["cached"] = bool(getattr(r, "_cached", False))
    d["seconds"] = round(r.seconds, 1) if not d["cached"] else 0
    run_id = getattr(r, "_run_id", None)
    usage = {"input_tokens": 0, "output_tokens": 0, "est_cost_usd": None}
    if run_id:
        st = Store(settings.db_path)
        row = st.get_run(run_id)
        st.close()
        if row:
            usage = {"input_tokens": row["input_tokens"] or 0,
                     "output_tokens": row["output_tokens"] or 0,
                     "est_cost_usd": row["est_cost_usd"]}
    d["usage"] = usage
    return d


def export(results: list[dict], reviews: dict | None = None) -> dict:
    if not isinstance(results, list) or not results:
        raise WebError(400, "Nothing to export yet: run the pipeline first.")
    csv_text, md_text = exporter.render(results, reviews or {}, title="Campaign record")
    return {"csv": csv_text, "markdown": md_text}


# --- HTTP plumbing shared by the api/*.py files and the local server --------------
def _respond(req: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    req.send_response(status)
    req.send_header("Content-Type", "application/json")
    req.send_header("Cache-Control", "no-store")
    req.send_header("Content-Length", str(len(body)))
    req.end_headers()
    req.wfile.write(body)


def _read_body(req: BaseHTTPRequestHandler) -> dict:
    n = int(req.headers.get("Content-Length") or 0)
    if n > MAX_BODY:
        raise WebError(413, "Request too large.")
    if not n:
        return {}
    try:
        data = json.loads(req.rfile.read(n) or b"{}")
    except json.JSONDecodeError:
        raise WebError(400, "Body is not JSON.")
    if not isinstance(data, dict):
        raise WebError(400, "Body must be a JSON object.")
    return data


def handle_post(req: BaseHTTPRequestHandler, action: str, local: bool = False) -> None:
    try:
        check_password(req.headers.get("X-Brendo-Password"), local)
        body = _read_body(req)
        settings = get_settings()
        if action == "check":
            _respond(req, 200, readiness(settings, ping=bool(body.get("ping", True))))
        elif action == "run":
            _respond(req, 200, run_one(str(body.get("domain", "")), settings,
                                       offline=bool(body.get("offline")),
                                       force=bool(body.get("force"))))
        elif action == "export":
            _respond(req, 200, export(body.get("results"), body.get("reviews")))
        else:
            _respond(req, 404, {"error": "unknown action"})
    except WebError as exc:
        _respond(req, exc.status, {"error": exc.message})
    except Exception as exc:  # never leak a traceback; keys are never in messages
        _respond(req, 500, {"error": f"Server error: {str(exc)[:200]}"})


def make_handler(action: str, local: bool = False):
    """A BaseHTTPRequestHandler class for Vercel's Python runtime (it looks for `handler`)."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            handle_post(self, action, local)

        def do_GET(self) -> None:  # noqa: N802
            _respond(self, 405, {"error": "Use POST."})

        def log_message(self, *args) -> None:  # keep request lines out of logs
            pass

    return Handler


# --- local website: `python -m brendo.web` -----------------------------------------
def serve(port: int = 8000) -> None:
    """The same website on your own PC at http://localhost:<port>, using Brendo/.env.
    Bound to 127.0.0.1, so only this computer can open it."""
    from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
    from .config import ROOT

    public = ROOT / "public"
    blueprint = ROOT / "brendo-blueprint.html"
    actions = {f"/api/{a}": a for a in ("check", "run", "export")}

    class Local(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(public), **kw)

        def _route(self):
            return actions.get(self.path.split("?")[0])

        def do_POST(self) -> None:  # noqa: N802
            action = self._route()
            if not action:
                self.send_error(404)
                return
            handle_post(self, action, local=True)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] == "/blueprint.html":
                data = blueprint.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self._route():
                self.send_error(405)
                return
            super().do_GET()

        def log_message(self, *args) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Local)
    print(f"Brendo website: http://localhost:{port}   (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    import sys
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
