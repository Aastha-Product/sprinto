"""LLM access: Anthropic Claude or Google Gemini, plus an offline heuristic mock.

Only the *reasoning* steps use a model: extract fields, infer pain points, write the
email. Qualification, PoC choice and the recipient are decided in code.

Real mode uses structured outputs (`output_config.format` with a JSON schema), so every
reply is the named fields --- no regex parsing. An API error, refusal or invalid reply
gets one retry, then the company is marked failed with the reason (never a silent guess).
Token usage is accumulated per model so the run report can show cost (slide 15).
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Protocol

from .config import Settings
from .schemas import CompanyFields

FIELD_NAMES = ("name", "is_saas", "buyer_description", "certifications", "funding_stage",
               "open_roles_count", "enterprise_customers", "compliance_signals")
# fields a pain point or hook may cite as its evidence
CITABLE_FIELDS = ("buyer_description", "funding_stage", "open_roles_count",
                  "enterprise_customers", "compliance_signals", "is_saas")

SPRINTO_OFFER = (
    "Sprinto automates compliance certification (SOC 2, ISO 27001, GDPR) for SaaS "
    "companies: continuous control monitoring, evidence collection and audit readiness, "
    "so teams get certified in weeks without a large security team."
)


def _fields_from_dict(d: dict) -> CompanyFields:
    ev = {k: v for k, v in (d.get("evidence") or {}).items() if v}
    return CompanyFields(
        name=d.get("name"),
        is_saas=d.get("is_saas"),
        buyer_description=d.get("buyer_description"),
        certifications=list(d.get("certifications") or []),
        funding_stage=d.get("funding_stage"),
        open_roles_count=d.get("open_roles_count"),
        enterprise_customers=d.get("enterprise_customers"),
        compliance_signals=list(d.get("compliance_signals") or []),
        evidence=ev,
    )


class LLM(Protocol):
    usage: dict

    def extract_fields(self, text: str) -> CompanyFields: ...
    def infer_pain_points(self, company: dict, contact: dict) -> list[dict]: ...
    def write_email(self, company: dict, contact: dict, pains: list[dict],
                    feedback: list[str] | None = None) -> dict: ...


# ---------- JSON schemas for structured outputs ----------

def _nullable(t: str) -> dict:
    return {"anyOf": [{"type": t}, {"type": "null"}]}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


EXTRACT_SCHEMA = _obj({
    "name": _nullable("string"),
    "is_saas": _nullable("boolean"),
    "buyer_description": _nullable("string"),
    "certifications": {"type": "array", "items": {"type": "string"}},
    "funding_stage": _nullable("string"),
    "open_roles_count": _nullable("integer"),
    "enterprise_customers": _nullable("boolean"),
    "compliance_signals": {"type": "array", "items": {"type": "string"}},
    "evidence": _obj({k: _nullable("string") for k in FIELD_NAMES if k != "name"}),
})

PAINS_SCHEMA = _obj({
    "pains": {"type": "array", "items": _obj({
        "pain": {"type": "string"},
        "evidence_field": {"type": "string", "enum": list(CITABLE_FIELDS)},
    })},
})

EMAIL_SCHEMA = _obj({
    "subject": {"type": "string"},
    "body": {"type": "string"},
    "hook": {"type": "string"},
    "hook_evidence_field": {"type": "string", "enum": list(CITABLE_FIELDS)},
})

_EXTRACT_SYS = (
    "You extract facts about a company from text taken from its own website, for a B2B "
    "sales-qualification task. Rules: use ONLY the given text; use null (or an empty "
    "list) when the text does not say; never infer beyond it. "
    "certifications = compliance standards the company itself HOLDS (e.g. 'SOC 2', "
    "'ISO 27001', 'GDPR'), not ones it merely mentions or is working towards. "
    "compliance_signals = short verbatim phrases showing compliance pressure: open "
    "compliance/security/GRC roles, 'SOC 2 in progress', security questionnaires, "
    "enterprise security reviews, audits. buyer_description = who buys the product. "
    "evidence = for every non-null field, a short snippet copied WORD FOR WORD from the "
    "text that supports it (for list fields, the snippet for the first item)."
)

_PAINS_SYS = (
    "Given verified facts about a company and one contact's role, name the TWO most likely "
    "pain points this person has that Sprinto could solve. " + SPRINTO_OFFER + " "
    "Each pain must follow from one given fact; set evidence_field to the name of that "
    "fact's field. Be specific to this company and this role; no generic lines."
)

_EMAIL_SYS = (
    "Write a short cold email from Sprinto to the contact. " + SPRINTO_OFFER + " Rules: "
    "greet the contact by first name; open with ONE specific hook grounded in a given "
    "fact, and set hook_evidence_field to that fact's field; state nothing about the "
    "company that is not in the given facts; human and plain, not salesy; under 120 "
    "words; end with a low-friction ask; no placeholders like [Name] or {{...}}; sign "
    "off as 'The Sprinto team'. Format the body as a plain-text email: the greeting on "
    "its own line, then 2-3 short paragraphs, then the sign-off, separated by blank lines."
)


class RealLLM:
    def __init__(self, settings: Settings):
        import anthropic  # lazy; only needed in real mode
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.llm_model
        self.model_draft = settings.llm_model_draft
        self.usage: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    def _json(self, model: str, system: str, user: str, schema: dict,
              max_tokens: int = 8000, effort: str | None = None) -> dict:
        output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
        if effort:
            output_config["effort"] = effort
        kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                      messages=[{"role": "user", "content": user}],
                      output_config=output_config)
        last: Exception | None = None
        for _ in range(2):
            try:
                if model.startswith("claude-haiku"):
                    msg = self.client.messages.create(**kwargs)
                else:
                    # Server-side refusal fallback: on a policy decline the API re-runs
                    # the request on a fallback model inside the same call.
                    msg = self.client.beta.messages.create(
                        **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
                self.usage[model][0] += msg.usage.input_tokens
                self.usage[model][1] += msg.usage.output_tokens
                if msg.stop_reason == "refusal":
                    raise RuntimeError("model declined the request (refusal)")
                if msg.stop_reason == "max_tokens":
                    raise RuntimeError("reply cut off at max_tokens")
                text = next(b.text for b in msg.content if b.type == "text")
                return json.loads(text)
            except (self._anthropic.BadRequestError, self._anthropic.AuthenticationError,
                    self._anthropic.PermissionDeniedError, self._anthropic.NotFoundError):
                raise  # not retryable: wrong key, model or request shape
            except Exception as exc:
                last = exc
        raise RuntimeError(f"LLM call failed after retry: {last}")

    def ping(self) -> bool:
        """Tiny call to confirm the key + model work (used by `--check`)."""
        self.client.messages.create(
            model=self.model, max_tokens=16,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True

    def extract_fields(self, text: str) -> CompanyFields:
        d = self._json(self.model, _EXTRACT_SYS, text[:12000], EXTRACT_SCHEMA, effort="low")
        return _fields_from_dict(d)

    def infer_pain_points(self, company: dict, contact: dict) -> list[dict]:
        user = json.dumps({"facts": company, "contact": contact})
        return self._json(self.model, _PAINS_SYS, user, PAINS_SCHEMA, effort="low").get("pains", [])

    def write_email(self, company: dict, contact: dict, pains: list[dict],
                    feedback: list[str] | None = None) -> dict:
        payload = {"facts": company, "contact": contact, "pains": pains}
        if feedback:
            payload["fix_these_problems_from_the_last_draft"] = feedback
        return self._json(self.model_draft, _EMAIL_SYS, json.dumps(payload), EMAIL_SCHEMA)


# ---------- Real client (Google Gemini, REST over httpx) ----------

def _gemini_schema(node):
    """JSON Schema for Gemini: nullable anyOf -> a type array ["string", "null"]."""
    if isinstance(node, dict):
        any_of = node.get("anyOf")
        if any_of and all(isinstance(x, dict) and set(x) == {"type"} for x in any_of):
            return {"type": [x["type"] for x in any_of]}
        return {k: _gemini_schema(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_gemini_schema(x) for x in node]
    return node


class GeminiLLM:
    """Gemini via the generateContent REST API (key in the x-goog-api-key header).

    JSON output: responseMimeType application/json + responseJsonSchema. If the API rejects
    the schema field, the client switches (once, for the whole run) to putting the schema in
    the instructions; replies are parsed and validated the same way either way.
    """
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, settings: Settings):
        import httpx  # lazy
        self._httpx = httpx
        self.key = settings.gemini_api_key
        self.model, self.model_draft = settings.gemini_model, settings.gemini_model_draft
        self.usage: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self._schema_field = True
        self._sleep = __import__("time").sleep

    def _post(self, model: str, body: dict) -> dict:
        """One generateContent call with backoff on 429/5xx (free-tier rate limits)."""
        delay = 4.0
        for attempt in range(4):
            r = self._httpx.post(self.URL.format(model=model), json=body, timeout=90,
                                 headers={"x-goog-api-key": self.key})
            if r.status_code in (429, 500, 503) and attempt < 3:
                self._sleep(delay)
                delay *= 2
                continue
            if r.status_code >= 400:
                try:
                    msg = r.json().get("error", {}).get("message", r.text)
                except Exception:
                    msg = r.text
                raise GeminiError(r.status_code, f"Gemini {r.status_code} ({model}): {msg[:200]}")
            return r.json()
        raise GeminiError(429, f"Gemini {model}: still rate-limited after retries")

    def _json(self, model: str, system: str, user: str, schema: dict) -> dict:
        gschema = _gemini_schema(schema)
        last: Exception | None = None
        for _ in range(2):
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"responseMimeType": "application/json",
                                     "maxOutputTokens": 8192},
            }
            if self._schema_field:
                body["generationConfig"]["responseJsonSchema"] = gschema
            else:
                body["systemInstruction"]["parts"][0]["text"] = (
                    system + "\n\nReply with ONLY a JSON object matching this JSON Schema:\n"
                    + json.dumps(gschema))
            try:
                data = self._post(model, body)
            except GeminiError as exc:
                if exc.status == 400 and self._schema_field and "schema" in str(exc).lower():
                    self._schema_field = False   # fall back to schema-in-prompt for the run
                    last = exc
                    continue
                if exc.status in (400, 401, 403, 404):
                    raise                          # bad key / model / request: not retryable
                last = exc
                continue
            um = data.get("usageMetadata") or {}
            self.usage[model][0] += int(um.get("promptTokenCount") or 0)
            self.usage[model][1] += int(um.get("candidatesTokenCount") or 0) + int(um.get("thoughtsTokenCount") or 0)
            block = (data.get("promptFeedback") or {}).get("blockReason")
            cands = data.get("candidates") or []
            if block or not cands:
                last = RuntimeError(f"Gemini blocked the request ({block or 'no candidates'})")
                continue
            finish = cands[0].get("finishReason")
            if finish not in (None, "STOP"):
                last = RuntimeError(f"Gemini stopped early (finishReason={finish})")
                continue
            text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", [])
                           if not p.get("thought"))
            try:
                out = json.loads(_strip_fence(text))
                _check_required(out, schema)
                return out
            except Exception as exc:
                last = RuntimeError(f"Gemini reply was not valid JSON for the schema: {exc}")
        raise RuntimeError(f"LLM call failed after retry: {last}")

    def _draft_json(self, system: str, user: str, schema: dict) -> dict:
        try:
            return self._json(self.model_draft, system, user, schema)
        except GeminiError as exc:
            if exc.status == 404 and self.model_draft != self.model:
                self.model_draft = self.model     # draft model not available to this key
                return self._json(self.model, system, user, schema)
            raise

    def ping(self) -> bool:
        body = {"contents": [{"role": "user", "parts": [{"text": "ping"}]}],
                "generationConfig": {"maxOutputTokens": 16}}
        self._post(self.model, body)
        try:
            self._post(self.model_draft, body)
        except GeminiError as exc:
            if exc.status != 404:
                raise
            raise RuntimeError(f"drafting model '{self.model_draft}' not found for this key; "
                               f"set BRENDO_GEMINI_MODEL_DRAFT (extraction model works)") from exc
        return True

    def extract_fields(self, text: str) -> CompanyFields:
        return _fields_from_dict(self._json(self.model, _EXTRACT_SYS, text[:12000], EXTRACT_SCHEMA))

    def infer_pain_points(self, company: dict, contact: dict) -> list[dict]:
        user = json.dumps({"facts": company, "contact": contact})
        return self._json(self.model, _PAINS_SYS, user, PAINS_SCHEMA).get("pains", [])

    def write_email(self, company: dict, contact: dict, pains: list[dict],
                    feedback: list[str] | None = None) -> dict:
        payload = {"facts": company, "contact": contact, "pains": pains}
        if feedback:
            payload["fix_these_problems_from_the_last_draft"] = feedback
        return self._draft_json(_EMAIL_SYS, json.dumps(payload), EMAIL_SCHEMA)


class GeminiError(RuntimeError):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()


def _check_required(out: dict, schema: dict) -> None:
    if not isinstance(out, dict):
        raise ValueError("reply is not an object")
    missing = [k for k in schema.get("required", []) if k not in out]
    if missing:
        raise ValueError(f"missing keys {missing}")


# ---------- Offline heuristic mock (no network, no keys) ----------

_CERTS = ["SOC 2", "SOC2", "ISO 27001", "ISO27001", "GDPR", "HIPAA", "PCI DSS", "PCI-DSS"]
_SW = ["saas", "software platform", "cloud platform", "b2b software", "platform for",
       "software for", "api", "dashboard", "automate", "integrat"]
_NON_SW = ["bakery", "restaurant", "cafe", "menu", "store hours", "fresh bread", "catering"]
_BUYER = [r"for (saas|b2b|enterprise|business|compan|team|startup|developer|engineer)\w*",
          r"\bb2b\b"]
_ENT = ["enterprise", "fortune 500", "trusted by", "enterprise-grade", "enterprise plan",
        "enterprise tier"]
_PRESSURE = ["compliance manager", "compliance lead", "head of compliance", "grc",
             "security engineer", "head of security", "ciso", "soc 2 in progress",
             "working towards soc 2", "pursuing soc 2", "security questionnaire",
             "security review", "audit"]
# phrases that mention a standard without claiming to hold it
_NOT_HELD = ("in progress", "working towards", "working toward", "pursuing", "readiness",
             "preparing for")


def _line_with(text: str, needle: str) -> str:
    for ln in text.splitlines():
        if needle.lower() in ln.lower():
            return ln.strip()[:160]
    return needle


class MockLLM:
    """Deterministic keyword extraction so the pipeline runs end-to-end offline."""

    def __init__(self) -> None:
        self.usage: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    def extract_fields(self, text: str) -> CompanyFields:
        import re
        f = CompanyFields()
        low = text.lower()

        held_lines = [ln for ln in text.splitlines()
                      if not any(k in ln.lower() for k in _NOT_HELD)]
        held_low = "\n".join(held_lines).lower()
        certs = [c for c in _CERTS if c.lower() in held_low]
        if certs:
            norm = []
            for c in certs:
                n = c.replace("SOC2", "SOC 2").replace("ISO27001", "ISO 27001").replace("PCI-DSS", "PCI DSS")
                if n not in norm:
                    norm.append(n)
            f.certifications = norm
            f.evidence["certifications"] = _line_with("\n".join(held_lines), certs[0])

        has_sw = any(k in low for k in _SW)
        is_non_sw = any(k in low for k in _NON_SW)
        if is_non_sw and not has_sw:
            f.is_saas = False
            f.evidence["is_saas"] = _line_with(text, next(k for k in _NON_SW if k in low))
        elif has_sw:
            f.is_saas = True
            f.evidence["is_saas"] = _line_with(text, next(k for k in _SW if k in low))

        for pat in _BUYER:
            m = re.search(pat, low)
            if m:
                f.buyer_description = text[m.start():m.start() + 60].strip()
                f.evidence["buyer_description"] = f.buyer_description
                break

        m = re.search(r"series\s+[a-h]\b", low)
        if m:
            f.funding_stage = m.group(0).title()
            f.evidence["funding_stage"] = _line_with(text, m.group(0))
        elif re.search(r"\bseed\b", low):
            f.funding_stage = "Seed"
            f.evidence["funding_stage"] = _line_with(text, "seed")

        m = re.search(r"(\d+)\s+open roles?", low)
        if m:
            f.open_roles_count = int(m.group(1))
            f.evidence["open_roles_count"] = _line_with(text, m.group(0))

        if any(k in low for k in _ENT):
            f.enterprise_customers = True
            f.evidence["enterprise_customers"] = _line_with(text, next(k for k in _ENT if k in low))

        signals = []
        for k in _PRESSURE:
            if re.search(r"\b" + re.escape(k) + r"\b", low):
                signals.append(_line_with(text, k))
        f.compliance_signals = list(dict.fromkeys(signals))[:3]
        if f.compliance_signals:
            f.evidence["compliance_signals"] = f.compliance_signals[0]

        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if first:
            f.name = (re.split(r"\s+(?:is|—|-|\|)\s+", first)[0][:40]).strip() or None
        return f

    def infer_pain_points(self, company: dict, contact: dict) -> list[dict]:
        role = (contact.get("role") or "decision-maker").lower()
        pains = []
        sig = " ".join(company.get("compliance_signals") or []).lower()
        if sig:
            hiring = any(k in sig for k in ("manager", "engineer", "lead", "head of", "ciso", "grc"))
            pains.append({
                "pain": ("Building a compliance program while hiring for it --- audit work lands before the team exists"
                         if hiring else
                         "Answering enterprise security questionnaires by hand, deal by deal"),
                "evidence_field": "compliance_signals",
            })
        if company.get("enterprise_customers") or company.get("funding_stage"):
            pains.append({
                "pain": "Enterprise buyers asking for SOC 2 / ISO 27001 before they sign",
                "evidence_field": "enterprise_customers" if company.get("enterprise_customers") else "funding_stage",
            })
        if company.get("open_roles_count") and len(pains) < 2:
            pains.append({
                "pain": "Scaling the team without a formal security/compliance program in place",
                "evidence_field": "open_roles_count",
            })
        if len(pains) < 2 and company.get("buyer_description"):
            pains.append({
                "pain": f"Proving security to business buyers as a {role} without a certification",
                "evidence_field": "buyer_description",
            })
        return pains[:2]

    def write_email(self, company: dict, contact: dict, pains: list[dict],
                    feedback: list[str] | None = None) -> dict:
        first = (contact.get("name") or "there").split()[0]
        cname = company.get("name") or "your team"
        lead = pains[0] if pains else {"pain": "getting compliance-ready fast",
                                       "evidence_field": "buyer_description"}
        fact = _fact_phrase(company, lead["evidence_field"])
        subject = f"{cname}: SOC 2 before the next enterprise deal"
        body = (
            f"Hi {first},\n\n"
            f"I saw {fact}. In our experience that usually means "
            f"{lead['pain'][0].lower() + lead['pain'][1:]}.\n\n"
            f"Sprinto automates SOC 2, ISO 27001 and GDPR for SaaS teams, so you can get "
            f"audit-ready in weeks without a dedicated security hire.\n\n"
            f"Worth a 15-minute look?\n\n"
            f"Best,\nThe Sprinto team"
        )
        return {"subject": subject, "body": body, "hook": f"{fact} -> {lead['pain']}",
                "hook_evidence_field": lead["evidence_field"]}


def _fact_phrase(company: dict, field: str) -> str:
    v = company.get(field)
    if field == "compliance_signals" and v:
        return f'"{v[0]}" on your site'
    if field == "funding_stage" and v:
        return f"you raised your {v}"
    if field == "open_roles_count" and v:
        return f"you have {v} open roles"
    if field == "enterprise_customers" and v:
        return "you're selling to enterprise customers"
    return "what you're building"


def get_llm(settings: Settings) -> LLM:
    if settings.provider == "gemini":
        return GeminiLLM(settings)
    if settings.provider == "anthropic":
        return RealLLM(settings)
    return MockLLM()
