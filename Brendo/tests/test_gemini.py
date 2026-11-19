"""GeminiLLM against a fake generateContent endpoint: request shape, parsing, fallbacks."""
import json

import pytest

from brendo import llm as llm_mod
from brendo.config import Settings
from brendo.llm import GeminiLLM, EXTRACT_SCHEMA, get_llm, MockLLM, RealLLM


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
        self.text = json.dumps(payload)

    def json(self):
        return self._p


def ok(obj, finish="STOP", tin=50, tout=10):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]},
                                      "finishReason": finish}],
                      "usageMetadata": {"promptTokenCount": tin, "candidatesTokenCount": tout,
                                        "thoughtsTokenCount": 5}})


def err(status, msg):
    return Resp(status, {"error": {"code": status, "message": msg}})


@pytest.fixture
def gem(settings, monkeypatch):
    settings.gemini_api_key = "AIza-test"
    g = GeminiLLM(settings)
    g._sleep = lambda s: None
    calls, replies = [], []

    def post(url, json=None, timeout=None, headers=None):
        calls.append({"url": url, "body": json, "headers": headers})
        return replies.pop(0)

    monkeypatch.setattr(g._httpx, "post", post)
    g.calls, g.replies = calls, replies
    return g


EXTRACTED = {"name": "Acme", "is_saas": True, "buyer_description": None, "certifications": [],
             "funding_stage": "Series B", "open_roles_count": None, "enterprise_customers": None,
             "compliance_signals": [], "evidence": {"is_saas": "platform", "funding_stage": "Series B",
             "buyer_description": None, "certifications": None, "open_roles_count": None,
             "enterprise_customers": None, "compliance_signals": None}}


def test_request_shape_and_parsing(gem):
    gem.replies.append(ok(EXTRACTED))
    f = gem.extract_fields("page")
    c = gem.calls[0]
    assert c["url"].endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert c["headers"]["x-goog-api-key"] == "AIza-test"
    cfg = c["body"]["generationConfig"]
    assert cfg["responseMimeType"] == "application/json"
    js = json.dumps(cfg["responseJsonSchema"])
    assert "anyOf" not in js and '["string", "null"]' in js       # nullable as a type array
    assert f.funding_stage == "Series B"
    assert gem.usage["gemini-3.5-flash-lite"] == [50, 15]           # thinking counted as output


def test_schema_field_rejected_falls_back_to_prompt(gem):
    gem.replies += [err(400, "Invalid JSON payload: Unknown name \"responseJsonSchema\""), ok(EXTRACTED)]
    assert gem.extract_fields("x").name == "Acme"
    second = gem.calls[1]["body"]
    assert "responseJsonSchema" not in second["generationConfig"]
    assert "JSON Schema" in second["systemInstruction"]["parts"][0]["text"]


def test_rate_limit_is_retried_with_backoff(gem):
    gem.replies += [err(429, "quota"), err(429, "quota"), ok(EXTRACTED)]
    assert gem.extract_fields("x").name == "Acme"


def test_bad_key_is_not_retried(gem):
    gem.replies.append(err(400, "API key not valid. Please pass a valid API key."))
    with pytest.raises(Exception, match="API key not valid"):
        gem.extract_fields("x")
    assert len(gem.calls) == 1


def test_safety_stop_and_bad_json_are_retried_then_fail(gem):
    gem.replies += [ok({}, finish="SAFETY"), Resp(200, {"candidates": [{"content": {"parts": [{"text": "not json"}]},
                                                                       "finishReason": "STOP"}]})]
    with pytest.raises(RuntimeError, match="not valid JSON"):
        gem.extract_fields("x")


def test_fenced_json_is_accepted(gem):
    fenced = Resp(200, {"candidates": [{"content": {"parts": [{"text": "```json\n" + json.dumps(EXTRACTED) + "\n```"}]},
                                        "finishReason": "STOP"}]})
    gem.replies.append(fenced)
    assert gem.extract_fields("x").name == "Acme"


def test_missing_draft_model_falls_back_to_extraction_model(gem):
    email = {"subject": "s", "body": "Hi Jane", "hook": "h", "hook_evidence_field": "funding_stage"}
    gem.replies += [err(404, "models/gemini-4-hydrogen is not found"), ok(email)]
    assert gem.write_email({}, {"name": "Jane"}, [])["subject"] == "s"
    assert "gemini-4-hydrogen" in gem.calls[0]["url"] and "flash-lite" in gem.calls[1]["url"]


def test_provider_selection():
    s = Settings()
    assert s.provider == "mock" and isinstance(get_llm(s), MockLLM)
    s.gemini_api_key = "AIza-x"
    assert s.provider == "gemini" and s.active_models() == ("gemini-3.5-flash-lite", "gemini-4-hydrogen")
    s.anthropic_api_key = "sk-ant-x"
    assert s.provider == "anthropic"
    s.llm_provider_pref = "gemini"
    assert s.provider == "gemini"


def test_google_key_in_anthropic_slot_is_used_for_gemini(monkeypatch):
    from brendo import config
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "BRENDO_LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "AIzaSyFAKE")
    s = config.get_settings()
    assert s.provider == "gemini" and s.gemini_api_key == "AIzaSyFAKE"
    assert s.key_was_misplaced and s.anthropic_api_key == ""


def test_gemini_run_end_to_end_offline_sites(settings, monkeypatch):
    """Full pipeline on a sample site with Gemini answering from a fake server."""
    from brendo.orchestrator import run_batch
    settings.gemini_api_key = "AIza-test"
    page_fields = dict(EXTRACTED, buyer_description="engineering teams",
                       evidence=dict(EXTRACTED["evidence"],
                                     is_saas="Acme is a software platform",
                                     buyer_description="for engineering teams",
                                     funding_stage="raised our Series B"))
    pains = {"pains": [{"pain": "SOC 2 asks from enterprise buyers", "evidence_field": "funding_stage"},
                       {"pain": "Proving security to business buyers", "evidence_field": "buyer_description"}]}
    email = {"subject": "Acme + SOC 2", "body": "Hi Alex,\n\nCongrats on the Series B. Worth a chat?\n\nThe Sprinto team",
             "hook": "Series B", "hook_evidence_field": "funding_stage"}

    def post(url, json=None, timeout=None, headers=None):
        sys_text = json["systemInstruction"]["parts"][0]["text"]
        if sys_text.startswith("You extract"):
            return ok(page_fields)
        if "pain points" in sys_text:
            return ok(pains)
        return ok(email)

    import httpx
    monkeypatch.setattr(httpx, "post", post)
    r = run_batch(["acme.example"], settings, offline=True, force=True)[0]
    assert r.status == "drafted", r.decisions
    assert r.email.subject == "Acme + SOC 2"


def test_timeout_is_retried(gem):
    import httpx
    gem.replies += [httpx.ReadTimeout("The read operation timed out"), ok(EXTRACTED)]
    orig = gem._httpx.post

    def post(*a, **k):
        r = orig(*a, **k)
        if isinstance(r, Exception):
            raise r
        return r
    gem._httpx.post = post
    assert gem.extract_fields("x").name == "Acme"


def test_draft_quota_used_up_falls_back_to_extraction_model(gem):
    email = {"subject": "s", "body": "Hi Jane", "hook": "h", "hook_evidence_field": "funding_stage"}
    gem.replies += [err(429, "You exceeded your current quota")] * 2 + [ok(email)]
    assert gem.write_email({}, {"name": "Jane"}, [])["subject"] == "s"
    assert "flash-lite" in gem.calls[-1]["url"] and gem.draft_fallback
    assert gem.model_draft == gem.model                       # stays switched for the run


def test_ping_treats_draft_quota_as_a_note(gem):
    gem.replies += [ok({})] + [err(429, "You exceeded your current quota")] * 4
    assert gem.ping() is True and "quota" in gem.ping_note


def test_draft_model_overloaded_falls_back(gem):
    email = {"subject": "s", "body": "Hi Jane", "hook": "h", "hook_evidence_field": "funding_stage"}
    gem.replies += [err(503, "This model is currently experiencing high demand")] * 4 + [ok(email)]
    assert gem.write_email({}, {"name": "Jane"}, [])["subject"] == "s"
    assert "flash-lite" in gem.calls[-1]["url"] and "overloaded" in gem.draft_fallback


def test_ping_treats_overloaded_draft_model_as_a_note(gem):
    gem.replies += [ok({})] + [err(503, "high demand")] * 4
    assert gem.ping() is True and "overloaded" in gem.ping_note


def test_silent_draft_model_is_abandoned_after_two_timeouts(gem):
    import httpx
    email = {"subject": "s", "body": "Hi Jane", "hook": "h", "hook_evidence_field": "funding_stage"}
    gem.replies += [httpx.ReadTimeout("timed out"), httpx.ReadTimeout("timed out"), ok(email)]
    orig, seen = gem._httpx.post, []

    def post(*a, **k):
        seen.append(k.get("timeout"))
        r = orig(*a, **k)
        if isinstance(r, Exception):
            raise r
        return r
    gem._httpx.post = post
    assert gem.write_email({}, {"name": "Jane"}, [])["subject"] == "s"
    assert len(gem.calls) == 3 and "hydrogen" in gem.calls[1]["url"] and "flash-lite" in gem.calls[2]["url"]
    assert max(seen) <= 60                       # never the old 120 s wait


def test_whole_chain_when_primary_models_are_overloaded(gem):
    """Live: hydrogen out of quota AND 3.5-flash-lite overloaded -> 3.1-flash-lite answers,
    and the overloaded models are skipped for the rest of the run."""
    gem.replies += [err(503, "high demand")] * 2 + [ok(EXTRACTED)]
    assert gem.extract_fields("x").name == "Acme"
    assert "gemini-3.1-flash-lite" in gem.calls[-1]["url"]
    gem.replies += [ok(EXTRACTED)]
    gem.extract_fields("y")
    assert "gemini-3.1-flash-lite" in gem.calls[-1]["url"]          # 3.5 not retried this run
    assert "overloaded" in gem._down["gemini-3.5-flash-lite"]
