"""Offline integration tests: MockLLM + sample sites exercise all three ICP paths."""
from brendo.config import get_settings
from brendo.llm import MockLLM
from brendo.scraper import Scraper
from brendo.qualifier import qualify
from brendo.schemas import CompanyResult
from brendo.orchestrator import run_batch


def _run_one(domain):
    s = get_settings()
    scraper = Scraper(s.samples_dir, offline=True)
    icp = s.load_icp()
    result = CompanyResult(domain=domain)
    company, qual = qualify(domain, scraper, MockLLM(), icp, s.max_fetches, result)
    return company, qual, result


def test_qualified_via_loop():
    company, qual, result = _run_one("acme.example")
    assert qual.verdict == "qualified"
    assert company.is_saas is True
    assert company.funding_stage                      # found on the careers page
    assert qual.fetches_used >= 1                      # the loop actually fetched more
    # defensibility: the loop step is recorded
    assert any(d["step"] == "loop" for d in result.decisions)


def test_disqualified_already_certified():
    company, qual, result = _run_one("securedhq.example")
    assert qual.verdict == "disqualified"
    assert company.certifications                      # SOC 2 / ISO 27001 found
    assert qual.fetches_used == 0                      # short-circuit: stop, no extra fetches


def test_disqualified_not_saas():
    company, qual, result = _run_one("sunrisebakery.example")
    assert qual.verdict == "disqualified"
    assert company.is_saas is False


def test_batch_runs_end_to_end_and_sends_for_qualified(tmp_path):
    s = get_settings()
    s.db_path = tmp_path / "t.db"                      # isolate the test DB
    results = run_batch(["acme.example", "securedhq.example", "sunrisebakery.example"],
                        s, offline=True, force=True)
    by = {r.domain: r for r in results}
    assert by["acme.example"].status == "sent"
    assert by["acme.example"].email and by["acme.example"].email.sent
    assert by["securedhq.example"].status == "disqualified"
    assert by["sunrisebakery.example"].status == "disqualified"
