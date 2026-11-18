from brendo.extractor import merge, verify
from brendo.schemas import CompanyFields

PAGE = """Acme is the B2B software platform for engineering teams
We recently raised our Series B to expand the platform.
We currently have 6 open roles across the company.
Compliance Manager"""


def test_verify_drops_a_fact_whose_evidence_is_not_on_the_page():     # T-EXT-2 (G8)
    f = CompanyFields(funding_stage="Series C", open_roles_count=6,
                      evidence={"funding_stage": "We closed a $40M Series C",
                                "open_roles_count": "We currently have 6 open roles"})
    dropped = verify(f, PAGE)
    assert f.funding_stage is None and "funding_stage" not in f.evidence
    assert f.open_roles_count == 6
    assert any("funding_stage" in d for d in dropped)


def test_verify_drops_a_fact_with_no_evidence_at_all():
    f = CompanyFields(is_saas=True)
    verify(f, PAGE)
    assert f.is_saas is None


def test_verify_ignores_case_and_punctuation():
    f = CompanyFields(funding_stage="Series B",
                      evidence={"funding_stage": "we recently raised our series b, to expand"})
    verify(f, PAGE)
    assert f.funding_stage == "Series B"


def test_verify_filters_list_items():
    f = CompanyFields(certifications=["SOC 2"], compliance_signals=["Compliance Manager", "Hiring a CISO"])
    verify(f, PAGE, jobs_page=True)
    assert f.certifications == []                            # SOC 2 isn't on this page
    assert f.compliance_signals == ["Compliance Manager"]


def test_merge_fills_gaps_without_overwriting():               # T-EXT-1
    acc = CompanyFields(is_saas=True, evidence={"is_saas": "platform"})
    new = CompanyFields(is_saas=False, funding_stage="Series B", compliance_signals=["Compliance Manager"],
                        evidence={"funding_stage": "Series B", "compliance_signals": "Compliance Manager"})
    merge(acc, new)
    assert acc.is_saas is True                               # homepage answer kept
    assert acc.funding_stage == "Series B"
    assert acc.compliance_signals == ["Compliance Manager"]


def test_question_style_link_text_is_not_a_compliance_signal():
    page = PAGE + "\nSecurity questions or concerns?"
    f = CompanyFields(compliance_signals=["Security questions or concerns?", "Compliance Manager"])
    dropped = verify(f, page, jobs_page=True)
    assert f.compliance_signals == ["Compliance Manager"]
    assert any("questions or concerns" in d for d in dropped)


def test_signal_off_a_jobs_page_must_be_first_person():
    page = "Synthred Aug 12 USD 62,500 Isla Harrington Security review underway 25d\nOur team answers every security questionnaire buyers send us."
    f = CompanyFields(compliance_signals=["Security review underway",
                                          "Our team answers every security questionnaire buyers send us."])
    dropped = verify(f, page)
    assert f.compliance_signals == ["Our team answers every security questionnaire buyers send us."]
    assert any("demo data" in d for d in dropped)
    g = CompanyFields(compliance_signals=["Compliance Manager"])
    verify(g, "Open roles: Compliance Manager", jobs_page=True)
    assert g.compliance_signals == ["Compliance Manager"]
