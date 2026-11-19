"""Copy your local .env keys to the Vercel project and redeploy --- run on YOUR computer.

    python -m brendo.vercel_setup                 # project "sprinto", keys from Brendo/.env
    python -m brendo.vercel_setup --project NAME  # another Vercel project name

What it does, in order:
  1. Reads Brendo/.env and keeps only the variables Brendo uses (never anything else).
  2. Checks them: recipient isn't the example.com placeholder, App Password is 16 letters.
  3. Asks for a Vercel access token (hidden input; or set VERCEL_TOKEN). Create one at
     vercel.com -> Account Settings -> Tokens, short expiry, and revoke it afterwards.
  4. Saves the variables to the project as *encrypted* values (Production + Preview).
  5. Redeploys the latest production deployment so the values take effect.
  6. Waits until it is live and prints the site's own setup check.

Only variable NAMES are ever printed; values and the token are not.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from pathlib import Path

from .config import ROOT, _dotenv_value

API = "https://api.vercel.com"

# Everything the website reads. Anything else in .env is left alone.
KEYS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "HUNTER_API_KEY", "GMAIL_ADDRESS",
        "GMAIL_APP_PASSWORD", "BRENDO_SEND_TO", "BRENDO_GEMINI_MODEL", "BRENDO_GEMINI_MODEL_DRAFT",
        "BRENDO_LLM_PROVIDER", "BRENDO_WEB_PASSWORD", "BRENDO_MAX_FETCHES", "BRENDO_CACHE_TTL_DAYS")


class SetupError(Exception):
    pass


def read_env(path: Path) -> dict[str, str]:
    """Brendo's variables from a .env file (case-insensitive names, inline comments dropped)."""
    if not path.exists():
        raise SetupError(f"No .env at {path}. Copy .env.example to .env and fill in your keys first.")
    wanted = {k.upper(): k for k in KEYS}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        name = wanted.get(key.strip().upper())
        value = _dotenv_value(val)
        if name and value:
            out[name] = value
    if "GMAIL_APP_PASSWORD" in out:
        out["GMAIL_APP_PASSWORD"] = out["GMAIL_APP_PASSWORD"].replace(" ", "")
    return out


def problems(env: dict[str, str]) -> list[str]:
    """Reasons the live site would not work end to end (empty list = good to go)."""
    p = []
    if not (env.get("GEMINI_API_KEY") or env.get("ANTHROPIC_API_KEY")):
        p.append("No GEMINI_API_KEY (or ANTHROPIC_API_KEY): the site would use a keyword stand-in, not AI.")
    if not env.get("HUNTER_API_KEY"):
        p.append("No HUNTER_API_KEY: no real contacts would be found.")
    to = env.get("BRENDO_SEND_TO", "").lower()
    if not to or to.endswith("@example.com"):
        p.append("BRENDO_SEND_TO is missing or still you@example.com: set it to your own email.")
    if not env.get("GMAIL_ADDRESS") or not env.get("GMAIL_APP_PASSWORD"):
        p.append("GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing: emails would be drafted, not sent.")
    else:
        if not env["GMAIL_ADDRESS"].lower().endswith((".com", ".in", ".co", ".org", ".net", ".io")):
            p.append(f"GMAIL_ADDRESS looks mistyped (check the ending, e.g. gmail.com).")
        if len(env["GMAIL_APP_PASSWORD"]) != 16 or not env["GMAIL_APP_PASSWORD"].isalpha():
            p.append("GMAIL_APP_PASSWORD isn't 16 letters: use a Google App Password "
                     "(myaccount.google.com/apppasswords), not your normal password.")
    return p


class Vercel:
    def __init__(self, token: str, client=None):
        import httpx
        self.http = client or httpx.Client(base_url=API, timeout=30,
                                           headers={"Authorization": f"Bearer {token}"})

    def _req(self, method: str, path: str, **kw) -> dict:
        r = self.http.request(method, path, **kw)
        try:
            data = r.json()
        except Exception:
            data = {}
        if r.status_code >= 400:
            msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
            raise SetupError(f"Vercel {r.status_code} on {path.split('?')[0]}: {msg or r.text[:150]}")
        return data

    def find_project(self, name: str) -> tuple[dict, str | None]:
        """(project, teamId) --- looks in your personal account and every team."""
        scopes: list[str | None] = [None]
        try:
            scopes += [t["id"] for t in self._req("GET", "/v2/teams").get("teams", [])]
        except SetupError:
            pass
        for team in scopes:
            try:
                return self._req("GET", f"/v9/projects/{name}", params=_team(team)), team
            except SetupError:
                continue
        raise SetupError(f"No Vercel project named '{name}' is visible to this token.")

    def save_env(self, project_id: str, team: str | None, env: dict[str, str]) -> None:
        body = [{"key": k, "value": v, "type": "encrypted", "target": ["production", "preview"]}
                for k, v in env.items()]
        self._req("POST", f"/v10/projects/{project_id}/env", params={**_team(team), "upsert": "true"},
                  json=body)

    def redeploy(self, project: dict, team: str | None) -> dict:
        deps = self._req("GET", "/v6/deployments", params={**_team(team), "projectId": project["id"],
                                                           "target": "production", "limit": 1})
        latest = (deps.get("deployments") or [None])[0]
        if not latest:
            raise SetupError("No production deployment to redeploy yet: push to main once, then rerun.")
        return self._req("POST", "/v13/deployments", params={**_team(team), "forceNew": "1"},
                         json={"name": project["name"], "deploymentId": latest["uid"],
                               "target": "production"})

    def wait_ready(self, dep_id: str, team: str | None, timeout: float = 600,
                   sleep=time.sleep) -> str:
        end = time.monotonic() + timeout
        state = "QUEUED"
        while time.monotonic() < end:
            d = self._req("GET", f"/v13/deployments/{dep_id}", params=_team(team))
            state = d.get("readyState") or d.get("state") or state
            if state in ("READY", "ERROR", "CANCELED"):
                return state
            sleep(5)
        return state


def _team(team: str | None) -> dict:
    return {"teamId": team} if team else {}


def site_check(url: str, password: str | None) -> dict:
    import httpx
    headers = {"Content-Type": "application/json"}
    if password:
        headers["X-Brendo-Password"] = password
    r = httpx.post(f"https://{url}/api/check", json={"ping": True}, headers=headers, timeout=120)
    return r.json()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="brendo.vercel_setup", description=__doc__.split("\n")[0])
    ap.add_argument("--project", default="sprinto", help="Vercel project name (default: sprinto)")
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env")
    ap.add_argument("--yes", action="store_true", help="don't stop on warnings")
    args = ap.parse_args(argv)

    try:
        env = read_env(args.env_file)
        print(f"Found in {args.env_file.name}: {', '.join(sorted(env)) or 'nothing'}")
        warn = problems(env)
        for w in warn:
            print("  ! " + w)
        if warn and not args.yes:
            ans = input("Fix .env first? Press Enter to stop, or type 'go' to continue anyway: ")
            if ans.strip().lower() != "go":
                return 1
        token = os.environ.get("VERCEL_TOKEN") or getpass.getpass("Vercel token (input hidden): ").strip()
        if not token:
            raise SetupError("No token given.")
        v = Vercel(token)
        project, team = v.find_project(args.project)
        print(f"Project: {project['name']} (root directory: {project.get('rootDirectory') or '/'})")
        v.save_env(project["id"], team, env)
        print(f"Saved {len(env)} encrypted variable(s) to Production + Preview.")
        dep = v.redeploy(project, team)
        print(f"Redeploying ({dep.get('url')}) ... this takes about a minute.")
        state = v.wait_ready(dep["id"], team)
        print(f"Deployment: {state}")
        if state != "READY":
            return 1
        aliases = (project.get("targets", {}).get("production", {}) or {}).get("alias") or [dep.get("url")]
        url = aliases[0]
        chk = site_check(url, env.get("BRENDO_WEB_PASSWORD"))
        print(f"\nLive site: https://{url}")
        for k in ("llm", "poc", "send", "recipient", "ping", "note"):
            if chk.get(k) is not None:
                print(f"  {k:10} {chk[k]}")
        if chk.get("error"):
            print("  error     " + str(chk["error"]))
        print("\nDone. Revoke the Vercel token now (Account Settings -> Tokens).")
        return 0
    except SetupError as exc:
        print(f"Stopped: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
