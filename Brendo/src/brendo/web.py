"""The website's server side: what the Vercel functions in api/ call.

Each request runs ONE company through the same tested pipeline the CLI uses
(orchestrator.run_batch), so a request stays well inside a serverless time limit and
the page can show progress company by company.

Access: if BRENDO_WEB_PASSWORD is set, every endpoint needs it (X-Brendo-Password header).
If it is not set, the site is open: anyone with the link can run it and spend the LLM
and Hunter quotas (emails still only go to BRENDO_SEND_TO). The setup check says which.

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

from .config import Settings, get_settings, redact
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
    if not expected:
        # No site password configured = open site (the owner's choice). Anyone with the
        # link can run it and spend the LLM/Hunter quotas; emails still only ever go to
        # BRENDO_SEND_TO. Set BRENDO_WEB_PASSWORD in Vercel to require a password again.
        return
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
        "site_password": bool(os.environ.get("BRENDO_WEB_PASSWORD", "")),
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
        "storage": _storage_label(settings),
        "approved_send_to": "the prospect (copy to you)" if prospects_enabled() else "your own inbox",
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
    d["run_id"] = run_id
    if run_id and (d.get("email") or {}).get("subject"):
        oid = f"{run_id}:{domain}"
        st = Store(settings.db_path)
        try:
            d["outreach"] = _public_outreach(st.get_outreach(oid))
        finally:
            st.close()
    return d


# --- human-in-the-loop: edit, approve & send / reject (workflow "HitL" + "Send Email") --
def prospects_enabled() -> bool:
    """Approved emails go to the real contact only when the owner opts in. Default: to
    BRENDO_SEND_TO (the brief: 'hardcode to: your own email address')."""
    return os.environ.get("BRENDO_SEND_TO_PROSPECTS", "").strip() == "1"


def _storage_label(settings: Settings) -> str:
    from .store import database_url
    if database_url():
        return "postgres (kept between runs)"
    if os.environ.get("VERCEL"):
        return "temporary (/tmp on Vercel: lost between runs; add a Postgres DATABASE_URL)"
    return f"sqlite ({settings.db_path.name})"


_PUBLIC_OUT = ("id", "run_id", "domain", "company", "contact_name", "contact_role", "contact_email",
               "subject_draft", "body_draft", "subject_final", "body_final", "edited", "status",
               "sent_to", "error", "created_at", "updated_at", "approved_at", "sent_at")


def _public_outreach(row: dict | None) -> dict | None:
    return {k: row.get(k) for k in _PUBLIC_OUT} if row else None


def approve(body: dict, settings: Settings) -> dict:
    from .emailer import smtp_send
    oid = str(body.get("id", ""))
    action = str(body.get("action", "approve"))
    if action not in ("approve", "reject"):
        raise WebError(400, "action must be approve or reject")
    st = Store(settings.db_path)
    try:
        row = st.get_outreach(oid)
        if not row:
            raise WebError(404, "That email isn't in the campaign record (it may be from before storage "
                                "was set up, or temporary storage was reset). Run the company again.")
        if row["status"] == "sent":
            raise WebError(409, f"Already sent to {row['sent_to']} at {row['sent_at']}.")
        now = _now_iso()
        if action == "reject":
            st.update_outreach(oid, status="rejected", approved_at=now)
            st.save_review(row["run_id"], row["domain"], "rejected", None, "rejected on the website")
            return {"outreach": _public_outreach(st.get_outreach(oid))}

        subject = str(body.get("subject") or row["subject_draft"] or "").strip()
        text = str(body.get("body") or row["body_draft"] or "").strip()
        if not subject or not text:
            raise WebError(400, "Subject and body can't be empty.")
        if len(subject) > 300 or len(text) > 10000:
            raise WebError(400, "Email too long (subject 300, body 10,000 characters max).")
        edited = int(subject != (row["subject_draft"] or "").strip() or text != (row["body_draft"] or "").strip())
        to_prospect = prospects_enabled() and bool(row.get("contact_email"))
        to = row["contact_email"] if to_prospect else settings.send_to
        fields = dict(subject_final=subject, body_final=text, edited=edited, approved_at=now)
        st.save_review(row["run_id"], row["domain"], "edited" if edited else "approved", None,
                       "approved on the website")
        if settings.gmail_is_mock:
            st.update_outreach(oid, status="approved_not_sent", error="Gmail isn't set up on this "
                               "deployment (GMAIL_ADDRESS + GMAIL_APP_PASSWORD).", **fields)
        elif not to_prospect and settings.send_to_is_placeholder:
            st.update_outreach(oid, status="send_failed", error="BRENDO_SEND_TO is still a placeholder.",
                               **fields)
        else:
            try:
                smtp_send(settings, to, subject, text, bcc=settings.send_to if to_prospect else None)
                st.update_outreach(oid, status="sent", sent_to=to, sent_at=_now_iso(), error=None, **fields)
            except Exception as exc:
                st.update_outreach(oid, status="send_failed", error=f"Gmail: {str(exc)[:160]}", **fields)
        return {"outreach": _public_outreach(st.get_outreach(oid))}
    finally:
        st.close()


def history(body: dict, settings: Settings) -> dict:
    limit = max(1, min(int(body.get("limit") or 100), 500))
    st = Store(settings.db_path)
    try:
        rows = [_public_outreach(r) for r in st.list_outreach(limit)]
        stats = st.review_stats()
    finally:
        st.close()
    return {"outreach": rows, "storage": _storage_label(settings), "review_stats": stats}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def export(results: list[dict], reviews: dict | None = None) -> dict:
    if not isinstance(results, list) or not results:
        raise WebError(400, "Nothing to export yet: run the pipeline first.")
    csv_text, md_text = exporter.render(results, reviews or {}, title="Campaign record")
    return {"csv": csv_text, "markdown": md_text}


# --- HTTP plumbing shared by the api/*.py files and the local server --------------
def _respond(req: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    # last line of defence: no key or password value ever leaves the server,
    # whatever an upstream error message happened to contain
    body = redact(json.dumps(payload)).encode()
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
        elif action == "approve":
            _respond(req, 200, approve(body, settings))
        elif action == "history":
            _respond(req, 200, history(body, settings))
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
    actions = {f"/api/{a}": a for a in ("check", "run", "export", "approve", "history")}

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
