"""Campaign record + human-in-the-loop approve/send, over real HTTP (both storage backends)."""
import pytest

from tests.test_web import call, server  # noqa: F401  (fixture)
from brendo import emailer


def _run(server):
    status, r = call(server, "run", {"domain": "acme.example", "offline": True, "force": True})
    assert status == 200 and r.get("outreach"), r.get("status")
    return r


def test_run_records_an_outreach_row_awaiting_review(server):
    r = _run(server)
    o = r["outreach"]
    assert o["id"] == f"{r['run_id']}:acme.example" and o["status"] == "awaiting_review"
    assert o["subject_draft"] == r["email"]["subject"] and o["contact_email"]
    status, h = call(server, "history", {})
    assert status == 200 and h["outreach"][0]["id"] == o["id"]


def test_approve_with_edits_sends_to_self_by_default(server, settings, monkeypatch):
    settings.gmail_address, settings.gmail_app_password = "me@gmail.com", "abcdefghijklmnop"
    sent = []
    monkeypatch.setattr(emailer, "smtp_send", lambda s, to, subj, text, bcc=None: sent.append((to, subj, text, bcc)))
    o = _run(server)["outreach"]
    status, out = call(server, "approve", {"id": o["id"], "subject": "Edited subject", "body": "Hi Alex,\n\nEdited."})
    a = out["outreach"]
    assert status == 200 and a["status"] == "sent" and a["edited"] == 1
    assert sent[-1] == ("me@mydomain.test", "Edited subject", "Hi Alex,\n\nEdited.", None)
    assert a["sent_to"] == "me@mydomain.test" and a["subject_final"] == "Edited subject"
    # can't send twice
    status, out = call(server, "approve", {"id": o["id"]})
    assert status == 409


def test_prospect_send_only_when_opted_in_with_copy_to_self(server, settings, monkeypatch):
    settings.gmail_address, settings.gmail_app_password = "me@gmail.com", "abcdefghijklmnop"
    monkeypatch.setenv("BRENDO_SEND_TO_PROSPECTS", "1")
    sent = []
    monkeypatch.setattr(emailer, "smtp_send", lambda s, to, subj, text, bcc=None: sent.append((to, bcc)))
    o = _run(server)["outreach"]
    status, out = call(server, "approve", {"id": o["id"], "subject": "S", "body": "B",
                                           "to": "attacker@evil.example"})   # client can't choose
    assert status == 200 and out["outreach"]["status"] == "sent"
    assert sent[0] == ("me@mydomain.test", None)                  # step 7's review copy to self
    assert sent[-1] == (o["contact_email"], "me@mydomain.test")    # approved: prospect, copy to you


def test_reject_and_review_stats(server):
    o = _run(server)["outreach"]
    status, out = call(server, "approve", {"id": o["id"], "action": "reject"})
    assert status == 200 and out["outreach"]["status"] == "rejected"
    stats = call(server, "history", {})[1]["review_stats"]
    assert stats["reviewed"] == 1 and stats["rewrite_rate"] == 1.0


def test_gmail_not_set_up_is_recorded_not_lost(server):
    o = _run(server)["outreach"]
    status, out = call(server, "approve", {"id": o["id"]})
    assert status == 200 and out["outreach"]["status"] == "approved_not_sent"
    assert "Gmail" in out["outreach"]["error"]


def test_send_failure_is_recorded(server, settings, monkeypatch):
    settings.gmail_address, settings.gmail_app_password = "me@gmail.com", "abcdefghijklmnop"

    def boom(*a, **k):
        raise OSError("535 Username and Password not accepted")
    monkeypatch.setattr(emailer, "smtp_send", boom)
    o = _run(server)["outreach"]
    out = call(server, "approve", {"id": o["id"]})[1]["outreach"]
    assert out["status"] == "send_failed" and "535" in out["error"]


def test_unknown_id_and_bad_input(server):
    assert call(server, "approve", {"id": "nope:x.com"})[0] == 404
    o = _run(server)["outreach"]
    assert call(server, "approve", {"id": o["id"], "action": "delete"})[0] == 400
    assert call(server, "approve", {"id": o["id"], "subject": "x" * 400, "body": "b"})[0] == 400


def test_check_reports_storage_and_send_policy(server):
    out = call(server, "check", {"ping": False})[1]
    assert out["storage"].startswith(("sqlite", "postgres")) and out["approved_send_to"] == "your own inbox"
