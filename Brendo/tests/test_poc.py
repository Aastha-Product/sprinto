from brendo import poc
from brendo.schemas import CompanyResult


def _personas(settings):
    return settings.load_icp()["personas"]


def test_contacts_file_gives_each_domain_its_own_person(settings):     # T-POC-1 (G1)
    settings.contacts_path.write_text(
        "domain,name,role,email\n"
        "acme.example,Jane Doe,Head of Security,jane@acme.example\n"
        "beta.example,Raj Iyer,CTO,raj@beta.example\n", encoding="utf-8")
    a = poc.find_poc("acme.example", settings, CompanyResult("acme.example"), _personas(settings))
    b = poc.find_poc("beta.example", settings, CompanyResult("beta.example"), _personas(settings))
    assert (a.name, b.name) == ("Jane Doe", "Raj Iyer")
    assert a.source == "contacts_file" and a.persona == "compliance_security_lead"
    assert b.persona == "engineering_leader"
    assert "Apollo" in a.reason


def test_persona_ranking_prefers_security_lead_over_engineering_manager(settings):  # T-POC-2 (G2)
    emails = [
        {"value": "em@x.com", "first_name": "Eve", "last_name": "M", "position": "Engineering Manager", "confidence": 99},
        {"value": "cto@x.com", "first_name": "Cy", "last_name": "T", "position": "CTO", "confidence": 90},
        {"value": "sec@x.com", "first_name": "Sam", "last_name": "S", "position": "Head of Security", "confidence": 70},
    ]
    e, p, reason = poc.pick(emails, _personas(settings))
    assert e["value"] == "sec@x.com" and p["id"] == "compliance_security_lead"
    assert "Head of Security" in reason and "chosen over 1 other" in reason


def test_no_persona_match_means_no_contact(settings):
    emails = [{"value": "a@x.com", "position": "Engineering Manager", "confidence": 99},
              {"value": "b@x.com", "position": "Security Engineer", "confidence": 95}]
    e, p, reason = poc.pick(emails, _personas(settings))
    assert e is None and "no buyer persona" in reason      # never "nearest manager"


def _hunter(monkeypatch, settings, payload=None, exc=None):
    settings.hunter_api_key = "k"

    class R:
        def raise_for_status(self):
            if exc:
                raise exc

        def json(self):
            return payload

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: R())


def test_hunter_result_is_ranked_and_explained(monkeypatch, settings):
    _hunter(monkeypatch, settings, {"data": {"emails": [
        {"value": "vp@acme.example", "first_name": "Val", "last_name": "P", "position": "VP Engineering", "confidence": 92}]}})
    r = CompanyResult("acme.example")
    c = poc.find_poc("acme.example", settings, r, _personas(settings))
    assert c.name == "Val P" and c.source == "hunter" and c.persona == "engineering_leader"
    assert any("VP Engineering" in d["rationale"] for d in r.decisions)


def test_hunter_empty_or_error_is_flagged(monkeypatch, settings):     # T-POC-3
    _hunter(monkeypatch, settings, {"data": {"emails": []}})
    c = poc.find_poc("acme.example", settings, CompanyResult("acme.example"), _personas(settings))
    assert c.email is None
    _hunter(monkeypatch, settings, exc=RuntimeError("429 Too Many Requests"))
    c = poc.find_poc("acme.example", settings, CompanyResult("acme.example"), _personas(settings))
    assert c.email is None and "Hunter error" in c.reason


def test_hunter_key_goes_in_a_header_and_never_into_logs(settings, monkeypatch):
    """httpx puts the request URL in its error text, which reaches the decision log and the
    website; the key must be in a header and any echo of it must be hidden."""
    import httpx
    from brendo import poc
    from brendo.schemas import CompanyResult

    settings.hunter_api_key = "hunterSECRET123"
    seen = {}

    def boom(url, params=None, headers=None, timeout=None):
        seen.update(params=params, headers=headers)
        raise httpx.ConnectError("failed for key hunterSECRET123")

    monkeypatch.setattr(httpx, "get", boom)
    result = CompanyResult(domain="acme.com")
    c = poc.find_poc("acme.com", settings, result, [])
    assert "api_key" not in seen["params"]
    assert seen["headers"]["X-API-KEY"] == "hunterSECRET123"
    text = c.reason + " ".join(d["rationale"] for d in result.decisions)
    assert "hunterSECRET123" not in text and "[hidden]" in text
