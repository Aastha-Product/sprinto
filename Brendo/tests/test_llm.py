"""RealLLM against a fake Anthropic client: structured outputs, retries, refusals, usage."""
import json
from types import SimpleNamespace

import pytest

anthropic = pytest.importorskip("anthropic")

from brendo.llm import RealLLM, EXTRACT_SCHEMA, MockLLM  # noqa: E402


def _msg(payload, stop="end_turn", tin=100, tout=20):
    return SimpleNamespace(
        stop_reason=stop,
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        usage=SimpleNamespace(input_tokens=tin, output_tokens=tout),
    )


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _llm(settings, replies, beta_replies=None):
    settings.anthropic_api_key = "sk-test"
    llm = RealLLM(settings)
    llm.client = SimpleNamespace(messages=FakeMessages(replies),
                                 beta=SimpleNamespace(messages=FakeMessages(beta_replies or [])))
    return llm


EXTRACTED = {"name": "Acme", "is_saas": True, "buyer_description": "engineering teams",
             "certifications": [], "funding_stage": "Series B", "open_roles_count": 6,
             "enterprise_customers": None, "compliance_signals": ["Compliance Manager"],
             "evidence": {"is_saas": "software platform", "buyer_description": "for engineering teams",
                          "certifications": None, "funding_stage": "raised our Series B",
                          "open_roles_count": "6 open roles", "enterprise_customers": None,
                          "compliance_signals": "Compliance Manager"}}


def test_extract_uses_structured_output_and_counts_tokens(settings):
    llm = _llm(settings, [_msg(EXTRACTED)])
    f = llm.extract_fields("page text")
    call = llm.client.messages.calls[0]
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": EXTRACT_SCHEMA}
    assert f.funding_stage == "Series B" and f.compliance_signals == ["Compliance Manager"]
    assert "certifications" not in f.evidence                 # null evidence is dropped
    assert llm.usage[settings.llm_model] == [100, 20]


def test_transient_error_is_retried_once(settings):
    llm = _llm(settings, [RuntimeError("overloaded"), _msg(EXTRACTED)])
    assert llm.extract_fields("x").name == "Acme"


def test_refusal_twice_raises_a_clear_error(settings):
    llm = _llm(settings, [_msg({}, stop="refusal"), _msg({}, stop="refusal")])
    with pytest.raises(RuntimeError, match="refusal"):
        llm.extract_fields("x")


def test_draft_model_uses_server_side_fallback(settings):
    email = {"subject": "s", "body": "Hi Jane", "hook": "h", "hook_evidence_field": "funding_stage"}
    llm = _llm(settings, [], beta_replies=[_msg(email)])
    out = llm.write_email({}, {"name": "Jane"}, [], feedback=["fix the greeting"])
    call = llm.client.beta.messages.calls[0]
    assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
    assert "fix the greeting" in call["messages"][0]["content"]
    assert out["subject"] == "s"


def test_mock_does_not_count_working_towards_as_certified():
    f = MockLLM().extract_fields("Acme is a SaaS platform for teams\nWe are working towards SOC 2 this year.")
    assert f.certifications == []
    assert any("working towards soc 2" in s.lower() for s in f.compliance_signals)
