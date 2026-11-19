"""The evaluation harness, run against a local Brendo (offline, mock model)."""
import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from brendo import web

ROOT = Path(__file__).resolve().parents[1]


def _load_harness():
    spec = importlib.util.spec_from_file_location("harness", ROOT / "evals" / "harness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def site(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "get_settings", lambda: settings)
    monkeypatch.delenv("BRENDO_WEB_PASSWORD", raising=False)
    holder, started = {}, threading.Event()
    real_init = ThreadingHTTPServer.__init__

    def grab(self, addr, handler):
        real_init(self, ("127.0.0.1", 0), handler)
        holder["srv"] = self
        started.set()

    monkeypatch.setattr(ThreadingHTTPServer, "__init__", grab)
    threading.Thread(target=web.serve, daemon=True).start()
    assert started.wait(5)
    h = _load_harness()
    monkeypatch.setattr(h, "OUT_DIR", tmp_path / "out")
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    yield h, f"http://127.0.0.1:{holder['srv'].server_address[1]}"
    holder["srv"].shutdown()
    holder["srv"].server_close()


def _args(url, *extra):
    return ["--url", url, "--env-file", "/nonexistent", *extra]


def test_every_table_row_passes_its_hard_checks_offline(site):
    h, url = site
    code = h.main(_args(url, "--judge", "none", "--repeat", "1"))
    assert code == 4                       # INCOMPLETE: hard checks pass, soft rows unjudged
    rep = json.loads(next((h.OUT_DIR).glob("report-*.json")).read_text())
    assert not [r["id"] for r in rep if r["result"] in ("fail", "flaky", "CANARY-PASSED")]
    assert all(r["catches_broken_output"] for r in rep)


def _fake_judge(h, monkeypatch, verdict: bool):
    def post(self, url, body, headers):
        crit = json.loads(body["messages"][0]["content"])["criteria"]
        reply = {"criteria": [{"criterion": c, "pass": verdict, "reason": "fake"} for c in crit]}
        return {"content": [{"type": "text", "text": json.dumps(reply)}]}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(h.Judge, "_post", post)


def test_judged_soft_rows_can_ship(site, monkeypatch):
    h, url = site
    _fake_judge(h, monkeypatch, True)
    assert h.main(_args(url, "--only", "D1-1", "--repeat", "1")) == 0     # SHIP
    assert list(h.OUT_DIR.glob("audit-*.csv"))                            # human audit sample


def test_a_failed_critical_soft_row_blocks_shipping(site, monkeypatch):
    h, url = site
    _fake_judge(h, monkeypatch, False)
    assert h.main(_args(url, "--only", "D1-11", "--repeat", "1")) == 2    # DO NOT SHIP


def test_a_canary_that_passes_voids_the_run(site, monkeypatch):
    h, url = site
    cases = h.load_cases("X-01")
    cases[0]["hard"] = [{"path": "qualification.verdict", "equals": "qualified"}]   # now it "passes"
    monkeypatch.setattr(h, "load_cases", lambda only: cases)
    assert h.main(_args(url, "--judge", "none", "--repeat", "1")) == 3    # VOID


def test_audit_check_logs_disagreements(site, tmp_path):
    h, _ = site
    out = h.OUT_DIR
    out.mkdir(parents=True)
    (out / "report-s1.json").write_text(json.dumps([{"id": "D1-09", "soft": [
        {"criterion": "c1", "pass": True, "reason": "r"}, {"criterion": "c2", "pass": False, "reason": "r"}]}]))
    audit = out / "audit-s1.csv"
    audit.write_text("case_id,criterion,expected,system_output,human_pass (y/n),human_note\n"
                     "D1-09,c1,x,{},y,\nD1-09,c2,x,{},y,judge too strict\n")
    assert h.main(["--audit-check", str(audit)]) == 0
    dis = (out / "disagreements-s1.csv").read_text()
    assert "c2" in dis and "c1" not in dis.split("\n", 1)[1]
