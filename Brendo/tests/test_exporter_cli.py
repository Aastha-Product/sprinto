import csv

from brendo import cli, exporter
from brendo.orchestrator import run_batch
from brendo.store import Store


def test_export_has_campaign_record_fields_and_review_roundtrip(settings):
    run_batch(["acme.example", "securedhq.example", "nosuch.example"], settings, offline=True, force=True)
    st = Store(settings.db_path)
    run_id = st.latest_run_id()
    csv_path, md_path = exporter.export(st, run_id, settings.out_dir)
    rows = {r["domain"]: r for r in csv.DictReader(csv_path.open(encoding="utf-8"))}
    acme = rows["acme.example"]
    for col in ("why_company", "why_contact", "pain_1", "hook", "subject", "body", "next_step"):
        assert acme[col], col
    assert rows["nosuch.example"]["next_step"].startswith("Retry")
    assert "Why this company" in md_path.read_text(encoding="utf-8")

    # the Growth Lead reviews and re-imports
    with csv_path.open(encoding="utf-8") as fh:
        data = list(csv.DictReader(fh))
    data[[r["domain"] for r in data].index("acme.example")].update(review_outcome="edited", poc_correct="y")
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=exporter.COLUMNS)
        w.writeheader()
        w.writerows(data)
    n, problems = exporter.import_reviews(st, csv_path)
    assert n == 1 and not problems
    stats = st.review_stats(run_id)
    assert stats["rewrite_rate"] == 1.0 and stats["poc_accuracy"] == 1.0
    st.close()


def test_cli_why_export_and_purge(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert cli.main(["--domain", "acme.example", "--offline"]) == 0
    assert "[DRAFTED] acme.example" in capsys.readouterr().out
    assert cli.main(["--why", "acme.example"]) == 0
    out = capsys.readouterr().out
    assert "[qualify]" in out and "[poc]" in out and "[draft]" in out
    assert cli.main(["--export"]) == 0
    assert list(settings.out_dir.glob("campaign-*.csv"))
    assert cli.main(["--purge", "acme.example"]) == 0
    assert cli.main(["--why", "acme.example"]) == 1


def test_cli_check_runs_offline(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert cli.main(["--check"]) == 0
    assert "contacts.csv" in capsys.readouterr().out
