"""Evals deck, Decisions 4 & 5: what counts as a correction, and which ones apply."""
from brendo import feedback
from brendo.llm import MockLLM
from brendo.schemas import CompanyFields

BEFORE = {"subject": "Acme: SOC 2 before the next enterprise deal",
          "body": "Hi Priya,\n\nI saw you're hiring a Compliance Manager.\n\nWorth a look?\n\nThe Sprinto team"}


def edit(body=None, subject=None):
    return {"subject": subject or BEFORE["subject"], "body": body or BEFORE["body"]}


def test_typo_fix_is_not_stored():
    v = feedback.classify(BEFORE, edit(BEFORE["body"].replace("hiring", "hirign")), MockLLM())
    assert v["meaningful"] is False and v["correction"] is None


def test_rewritten_hook_is_stored():
    new = BEFORE["body"].replace("I saw you're hiring a Compliance Manager.",
                                 "Congrats on raising your Series B last month.")
    v = feedback.classify(BEFORE, edit(new), MockLLM())
    assert v["meaningful"] is True
    c = v["correction"]
    assert c["topic"] == "hook" and c["requires_fact"] == "funding_stage" and c["lesson"]


def test_punctuation_only_change_counts_when_it_changes_tone():
    v = feedback.classify(BEFORE, edit(BEFORE["body"].replace("Worth a look?", "Worth a look!")), MockLLM())
    assert v["meaningful"] is True and v["correction"]["topic"] == "tone"


def _c(id_, topic, created, fact=None, scope="generic", context=None):
    return {"id": id_, "lesson": f"lesson {id_}", "topic": topic, "created_at": created,
            "requires_fact": fact, "scope": scope, "context": context}


def test_correction_needing_a_fact_applies_only_when_the_company_has_it():
    c = _c("c1", "hook", "2026-11-01", fact="funding_stage")
    applied, _ = feedback.select([c], CompanyFields(funding_stage="Series B"))
    assert [x["id"] for x in applied] == ["c1"]
    applied, skipped = feedback.select([c], CompanyFields())
    assert applied == [] and "funding_stage" in skipped[0][1]


def test_specific_correction_is_skipped_for_a_different_context():
    c = _c("c2", "tone", "2026-11-01", scope="specific", context="healthcare")
    applied, skipped = feedback.select([c], CompanyFields(buyer_description="for fintech teams"))
    assert applied == [] and "healthcare" in skipped[0][1]


def test_newer_correction_wins_a_conflict_on_the_same_topic():
    old, new = _c("old", "tone", "2026-10-01"), _c("new", "tone", "2026-11-10")
    applied, skipped = feedback.select([old, new], CompanyFields())
    assert [x["id"] for x in applied] == ["new"]
    assert skipped[0][0]["id"] == "old" and "most recent wins" in skipped[0][1]


def test_applied_corrections_reach_the_draft(settings):
    from brendo.orchestrator import process_one
    from brendo.scraper import Scraper
    from brendo.llm import get_llm
    c = _c("c9", "hook", "2026-11-01", fact="funding_stage")
    r = process_one("acme.example", Scraper(settings.samples_dir, offline=True), get_llm(settings),
                    settings.load_icp(), settings, corrections=[c], send=False)
    assert r.status == "drafted"
    assert r.email.applied_corrections == ["c9"]
    assert any(d["step"] == "feedback" and "c9" in d["rationale"] for d in r.decisions)
