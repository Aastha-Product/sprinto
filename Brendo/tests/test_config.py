import os

from brendo.config import _dotenv_value, load_dotenv


def test_inline_comments_and_quotes():
    assert _dotenv_value("1.5   # seconds") == "1.5"
    assert _dotenv_value("me@x.com # note") == "me@x.com"
    assert _dotenv_value('"abc # not a comment"') == "abc # not a comment"
    assert _dotenv_value("'quoted'") == "quoted"
    assert _dotenv_value("pass#word") == "pass#word"      # no space before # = part of value
    assert _dotenv_value("") == ""


def test_env_example_loads_cleanly(tmp_path, monkeypatch):
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / ".env.example"
    for k in ("BRENDO_SEND_TO", "BRENDO_FETCH_DELAY", "BRENDO_LLM_MODEL"):
        monkeypatch.setenv(k, "x")      # registers the key so it's removed after the test
        monkeypatch.delenv(k)
    load_dotenv(example)
    assert os.environ["BRENDO_SEND_TO"] == "you@example.com"
    assert float(os.environ["BRENDO_FETCH_DELAY"]) == 1.5
    assert os.environ["BRENDO_LLM_MODEL"] == "claude-haiku-5-5"
