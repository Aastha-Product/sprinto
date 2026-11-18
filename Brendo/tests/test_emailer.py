from brendo import emailer, painpoints
from brendo.schemas import CompanyFields, Contact, CompanyResult, Email

COMPANY = CompanyFields(name="Acme", is_saas=True, funding_stage="Series B",
                        evidence={"funding_stage": "raised our Series B"})
CONTACT = Contact(name="Jane Doe", role="Head of Security", email="jane@acme.example")
GOOD = {"subject": "Acme + SOC 2", "body": "Hi Jane,\n\nSaw the Series B. Worth a chat?\n\nThe Sprinto team",
        "hook": "Series B", "hook_evidence_field": "funding_stage"}


def test_check_accepts_a_good_draft():
    assert emailer.check(GOOD, COMPANY, CONTACT) == []


def test_check_catches_each_problem():                         # T-AI-3
    bad = dict(GOOD, body="Hello [Name],\n\n" + "word " * 130, hook_evidence_field="compliance_signals")
    problems = " ".join(emailer.check(bad, COMPANY, CONTACT))
    assert "first name" in problems and "under 120" in problems
    assert "placeholder" in problems and "compliance_signals" in problems


class FakeLLM:
    def __init__(self, drafts):
        self.drafts = list(drafts)
        self.feedback = []
        self.usage = {}

    def write_email(self, company, contact, pains, feedback=None):
        self.feedback.append(feedback)
        return self.drafts.pop(0)


def test_bad_draft_is_redrafted_once_with_feedback():
    bad = dict(GOOD, body="Hi there,\n\nBuy Sprinto.")
    llm = FakeLLM([bad, GOOD])
    email = emailer.draft(COMPANY, CONTACT, [{"pain": "p", "evidence_field": "funding_stage"}] * 2,
                          llm, CompanyResult("acme.example"))
    assert email is not None and email.subject == GOOD["subject"]
    assert llm.feedback[0] is None and llm.feedback[1]       # second call carried the problems


def test_draft_failing_twice_is_flagged():
    bad = dict(GOOD, body="Hi there,\n\nBuy Sprinto.")
    assert emailer.draft(COMPANY, CONTACT, [], FakeLLM([bad, bad]), CompanyResult("a")) is None


def test_pains_must_cite_a_populated_field():                  # T-AI-2
    pains = [{"pain": "a", "evidence_field": "funding_stage"},
             {"pain": "b", "evidence_field": "compliance_signals"},   # empty for this company
             {"pain": "c", "evidence_field": "is_saas"},
             {"pain": "d", "evidence_field": "funding_stage"}]
    kept, dropped = painpoints.grounded(pains, COMPANY)
    assert [p["pain"] for p in kept] == ["a", "c"]
    assert len(dropped) == 1


def test_mock_send_writes_outbox_and_is_drafted_not_sent(settings):   # T-SND-1
    e = emailer.send(Email(subject="Hi", body="Body", footer="WHY"), settings,
                     CompanyResult("acme.example"), tag="acme.example")
    assert e.drafted and not e.sent
    files = list((settings.db_path.parent / "outbox").glob("*.txt"))
    text = files[0].read_text()
    assert "To: me@mydomain.test" in text and "WHY" in text


def test_live_send_to_placeholder_is_refused(settings, monkeypatch):  # T-SND-2
    settings.gmail_address, settings.gmail_app_password = "me@gmail.com", "app-pass"
    settings.send_to = "you@example.com"
    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not connect")))
    r = CompanyResult("acme.example")
    e = emailer.send(Email(subject="Hi", body="Body"), settings, r)
    assert not e.sent and not e.drafted
    assert any("placeholder" in d["rationale"] for d in r.decisions)


def test_live_send_goes_only_to_self(settings, monkeypatch):
    settings.gmail_address, settings.gmail_app_password = "me@gmail.com", "app-pass"
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg)

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    e = emailer.send(Email(subject="Hi", body="Body"), settings, CompanyResult("acme.example"))
    assert e.sent and sent[0]["To"] == "me@mydomain.test"


def test_why_footer_explains_company_person_and_hook():
    from brendo.schemas import Qualification
    q = Qualification(verdict="qualified", reasons=["B2B SaaS, Series B"], rank="high", flags=["check x"])
    c = Contact(name="Jane Doe", role="Head of Security", email="jane@acme.example", reason="persona match")
    em = Email(subject="s", body="b", hook_evidence_field="funding_stage", pains=["p1", "p2"])
    f = emailer.why_footer(COMPANY, q, c, em, "acme.example")
    for needle in ("Why this company", "Why this person", "persona match", "funding_stage", "check x"):
        assert needle in f
