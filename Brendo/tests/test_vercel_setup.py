"""brendo.vercel_setup against a fake Vercel API (no network, no real token)."""
import json

import httpx
import pytest

from brendo import vercel_setup as vs


def test_read_env_keeps_only_brendo_keys_and_cleans_values(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# comment\nGemini_API_KEY= AQ.key   # inline note\nHUNTER_API_KEY=h1\n"
                 "GMAIL_APP_PASSWORD= abcd efgh ijkl mnop\nOTHER_SECRET=nope\nBRENDO_SEND_TO=\n")
    env = vs.read_env(f)
    assert env == {"GEMINI_API_KEY": "AQ.key", "HUNTER_API_KEY": "h1",
                   "GMAIL_APP_PASSWORD": "abcdefghijklmnop"}


def test_problems_catch_the_real_mistakes():
    bad = {"GEMINI_API_KEY": "k", "HUNTER_API_KEY": "h", "BRENDO_SEND_TO": "you@example.com",
           "GMAIL_ADDRESS": "me@gmail.om", "GMAIL_APP_PASSWORD": "ub2@*h7H6u.2c"}
    text = " ".join(vs.problems(bad))
    assert "you@example.com" in text and "mistyped" in text and "16 letters" in text
    good = dict(bad, BRENDO_SEND_TO="me@gmail.com", GMAIL_ADDRESS="me@gmail.com",
                GMAIL_APP_PASSWORD="abcdefghijklmnop")
    assert vs.problems(good) == []


def fake_vercel():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        p, q = req.url.path, dict(req.url.params)
        if p == "/v2/teams":
            return httpx.Response(200, json={"teams": [{"id": "team_1"}]})
        if p == "/v9/projects/sprinto":
            if q.get("teamId") != "team_1":
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, json={"id": "prj_1", "name": "sprinto", "rootDirectory": "Brendo",
                                             "targets": {"production": {"alias": ["sprinto-five.vercel.app"]}}})
        if p == "/v10/projects/prj_1/env":
            return httpx.Response(201, json={"created": []})
        if p == "/v6/deployments":
            return httpx.Response(200, json={"deployments": [{"uid": "dpl_old"}]})
        if p == "/v13/deployments" and req.method == "POST":
            return httpx.Response(200, json={"id": "dpl_new", "url": "sprinto-x.vercel.app"})
        if p == "/v13/deployments/dpl_new":
            return httpx.Response(200, json={"readyState": "READY"})
        return httpx.Response(500, json={"error": {"message": "unexpected " + p}})

    client = httpx.Client(base_url=vs.API, transport=httpx.MockTransport(handler),
                          headers={"Authorization": "Bearer t"})
    return vs.Vercel("t", client=client), calls


def test_full_flow_saves_encrypted_env_and_redeploys():
    v, calls = fake_vercel()
    project, team = v.find_project("sprinto")
    assert team == "team_1" and project["id"] == "prj_1"
    v.save_env("prj_1", team, {"GEMINI_API_KEY": "secret", "BRENDO_SEND_TO": "me@gmail.com"})
    body = json.loads(calls[-1].content)
    assert all(e["type"] == "encrypted" and e["target"] == ["production", "preview"] for e in body)
    assert calls[-1].url.params["upsert"] == "true"
    dep = v.redeploy(project, team)
    redeploy_body = json.loads(calls[-1].content)
    assert redeploy_body == {"name": "sprinto", "deploymentId": "dpl_old", "target": "production"}
    assert v.wait_ready(dep["id"], team, sleep=lambda s: None) == "READY"


def test_main_prints_names_never_values(tmp_path, monkeypatch, capsys):
    f = tmp_path / ".env"
    f.write_text("GEMINI_API_KEY=SECRETKEY123\nHUNTER_API_KEY=HUNTERSECRET\nBRENDO_SEND_TO=me@gmail.com\n"
                 "GMAIL_ADDRESS=me@gmail.com\nGMAIL_APP_PASSWORD=abcdefghijklmnop\n")
    v, _ = fake_vercel()
    monkeypatch.setattr(vs, "Vercel", lambda token: v)
    monkeypatch.setenv("VERCEL_TOKEN", "TOKEN999")
    monkeypatch.setattr(vs, "site_check", lambda url, pw: {"llm": "live: gemini", "send": "live: gmail"})
    monkeypatch.setattr(vs.Vercel, "wait_ready", lambda self, d, t, timeout=600, sleep=None: "READY", raising=False)
    assert vs.main(["--env-file", str(f)]) == 0
    out = capsys.readouterr().out
    assert "GEMINI_API_KEY" in out and "live: gmail" in out and "sprinto-five.vercel.app" in out
    for secret in ("SECRETKEY123", "HUNTERSECRET", "abcdefghijklmnop", "TOKEN999"):
        assert secret not in out


def test_missing_env_file_stops_cleanly(tmp_path, capsys):
    assert vs.main(["--env-file", str(tmp_path / "nope.env"), "--yes"]) == 1
    assert "No .env" in capsys.readouterr().out
