"""Qualifier + loop: the Evidence Map paths (slide 40), ranking, and failure handling."""
from brendo.llm import MockLLM
from brendo.qualifier import qualify, stage_band, SiteUnreachable
from brendo.schemas import CompanyResult, CompanyFields
from brendo.scraper import Scraper, Fetch
from brendo.orchestrator import run_batch

import pytest


def _run_one(settings, domain, scraper=None):
    scraper = scraper or Scraper(settings.samples_dir, offline=True)
    result = CompanyResult(domain=domain)
    company, qual = qualify(domain, scraper, MockLLM(), settings.load_icp(), settings.max_fetches, result)
    return company, qual, result


def test_qualified_via_loop(settings):                       # T-QUA-1
    company, qual, result = _run_one(settings, "acme.example")
    assert qual.verdict == "qualified"
    assert company.is_saas is True
    assert company.funding_stage == "Series B"              # found on the careers page
    assert qual.fetches_used >= 1                            # the loop actually fetched more
    assert any(d["step"] == "loop" for d in result.decisions)


def test_compliance_signal_from_careers_page_ranks_high(settings):   # T-QUA-acme
    company, qual, _ = _run_one(settings, "acme.example")
    assert any("Compliance Manager" in s for s in company.compliance_signals)
    assert qual.rank == "high"


def test_disqualified_already_certified(settings):          # T-QUA-2
    company, qual, _ = _run_one(settings, "securedhq.example")
    assert qual.verdict == "disqualified"
    assert company.certifications
    assert qual.fetches_used == 0                            # short-circuit: no extra fetches


def test_disqualified_not_saas(settings):                    # T-QUA-3
    company, qual, _ = _run_one(settings, "sunrisebakery.example")
    assert qual.verdict == "disqualified"
    assert company.is_saas is False


def test_thin_signal_stops_at_limits_and_flags(settings):   # T-QUA-4
    _, qual, _ = _run_one(settings, "thinsignal.example")
    assert qual.verdict == "qualified_flagged"
    assert qual.rank == "lower"
    assert qual.fetches_used <= settings.max_fetches
    assert any("thin" in f for f in qual.flags)


def test_cap_reason_only_when_a_question_is_left_unanswered(settings):
    _, qual, _ = _run_one(settings, "thinsignal.example")
    assert not any("fetch cap" in f for f in qual.flags)    # limits ran out, not the cap
    settings.max_fetches = 1
    _, qual, _ = _run_one(settings, "thinsignal.example")
    assert any("fetch cap" in f for f in qual.flags)


class _DeadScraper(Scraper):
    def __init__(self):
        super().__init__(None, offline=False)
        self.calls = 0

    def fetch(self, domain, path="/"):
        self.calls += 1
        return Fetch(f"https://{domain}{path}", "", "", "request", "error", "connection refused")


def test_unreachable_homepage_fails_without_extra_fetches(settings):   # T-QUA-5
    dead = _DeadScraper()
    with pytest.raises(SiteUnreachable):
        _run_one(settings, "down.example", scraper=dead)
    assert dead.calls == 1


def test_late_stage_qualifies_at_lower_rank_with_flag(settings):       # T-QUA-6
    company, qual, _ = _run_one(settings, "lateco.example")
    assert qual.verdict == "qualified"
    assert company.funding_stage == "Series E"
    assert qual.rank == "lower"
    assert any("outside Series A-C" in f for f in qual.flags)


def test_stage_bands(settings):
    icp = settings.load_icp()
    band = lambda s: stage_band(CompanyFields(funding_stage=s), icp)  # noqa: E731
    assert band("Series B") == "target"
    assert band("Seed") == "early"
    assert band("Series E") == "late"
    assert band(None) == "unknown"


def test_hero_in_header_is_kept_and_qualifies(settings):
    company, qual, _ = _run_one(settings, "headerhero.example")
    assert company.is_saas is True                           # came from the <header> hero
    assert qual.verdict == "qualified"


def test_batch_end_to_end(settings):                         # T-E2E-1
    results = run_batch(["acme.example", "securedhq.example", "sunrisebakery.example"],
                        settings, offline=True, force=True)
    by = {r.domain: r for r in results}
    assert by["acme.example"].status == "drafted"            # mock outbox, never "sent"
    assert by["acme.example"].email.drafted and not by["acme.example"].email.sent
    assert by["securedhq.example"].status == "disqualified"
    assert by["sunrisebakery.example"].status == "disqualified"


def test_batch_normalises_and_dedupes_domains(settings):
    results = run_batch(["https://www.acme.example/", "acme.example", " ", "ACME.example"],
                        settings, offline=True, force=True)
    assert [r.domain for r in results] == ["acme.example"]


def test_one_failure_does_not_stop_the_batch(settings):       # T-ERR-1
    results = run_batch(["nosuch.example", "acme.example"], settings, offline=True, force=True)
    by = {r.domain: r for r in results}
    assert by["nosuch.example"].status == "failed"
    assert "homepage missing" in by["nosuch.example"].error
    assert by["acme.example"].status == "drafted"
