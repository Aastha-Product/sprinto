# Brendo

An automated, defensible **cold-outreach pipeline** for Sprinto (MESA AI Case 2 / "B2 — Cold Outreach, at Scale"). Give it a list of company domains; for each right-fit company it finds the decision-maker, drafts a personalised cold email, and sends it to **your own inbox** — recording *why* at every step.

> Implements the blueprint reviewed before build. Nothing sends to a real prospect: the recipient is hardcoded to you.

## The 7-step pipeline

1. **Scrape** the website (single-fetch; headless fallback only for JS pages)
2. **Extract** structured company fields from cleaned text
3. **Qualify** against Sprinto's ICP — with an **adaptive loop** that fetches a *named* source when a required fact is missing, bounded by a hard fetch cap
4. **Find PoC** (Hunter.io API *or* Apollo hardcode)
5. **Two pain points** for that person
6. **Draft** a by-name email with one specific hook
7. **Send** via Gmail (to your own address)

## Run it now (no keys, no network)

The whole pipeline runs **offline in mock mode** against bundled sample sites:

```bash
python -m brendo.cli --offline
```

> Run from the `src/` directory, or add it to `PYTHONPATH`:
> `PYTHONPATH=src python -m brendo.cli --offline` (bash) ·
> `$env:PYTHONPATH="src"; python -m brendo.cli --offline` (PowerShell).

Expected: `acme.example` → **sent** (qualified via the careers-page loop), `securedhq.example` → **disqualified** (already certified), `sunrisebakery.example` → **disqualified** (not B2B SaaS). Mock emails are written to `data/outbox/`.

Inspect the defensibility trail for any domain:

```bash
python -m brendo.cli --why acme.example
```

## Real mode

```bash
pip install -r requirements.txt
```

Edit `.env` (already created for you; `.env.example` is the template) with the keys you have, then confirm what is live vs mock:

```bash
python -m brendo.cli --check
```

Then run against real domains:

```bash
python -m brendo.cli --domains inputs/domains.csv
```

Each credential is optional and independent — any part with no key stays in mock mode:

| Key | Enables | Without it |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | LLM extraction/pains/draft | heuristic mock extractor |
| `HUNTER_API_KEY` | live PoC lookup | mock placeholder contact (flagged) |
| `BRENDO_APOLLO_CONTACT` | Apollo hardcode (`Name\|Role\|email`) | — |
| `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` | real Gmail send | writes to `data/outbox/` |
| `BRENDO_SEND_TO` | your inbox (recipient) | `you@example.com` |

**Where to get each key**

- `ANTHROPIC_API_KEY` — console.anthropic.com. Models default to Haiku for extraction and Sonnet for drafting (the deck's "cheap model for volume" cost dial).
- `HUNTER_API_KEY` — hunter.io (free tier: 25 domain searches/month, so the cache is on by default). Or skip it and set `BRENDO_APOLLO_CONTACT="Jane Doe|Head of Security|jane@acme.com"` after a manual Apollo lookup.
- Gmail — turn on 2-step verification, then create an **App Password** (not your normal password) at myaccount.google.com and put it in `GMAIL_APP_PASSWORD`, with your address in `GMAIL_ADDRESS`.
- `BRENDO_SEND_TO` — **your own inbox**. The build never emails a real prospect.

Partial setup is fine: an Anthropic key alone gives real extraction, qualification and drafting while PoC stays mock and the email lands in `data/outbox/`. Verify any time with `--check`.

## The ICP lives in config

`config/icp.json` is the deck's **Evidence Map**: the questions, which page answers each, what counts as sufficient, the per-question fetch limit, and the short-circuits. Change qualification by editing that file — not the code.

## Tests

```bash
pip install pytest
PYTHONPATH=src pytest           # or: python -m pytest
```

Covers the cleaner and all three ICP paths end-to-end offline.

## Layout

```
src/brendo/   cli · orchestrator · scraper · cleaner · extractor · qualifier
              poc · painpoints · emailer · store · llm · config · schemas
config/icp.json   Evidence Map (ICP)      samples/   offline demo sites
inputs/           domain lists            tests/     offline tests
data/             SQLite cache + decision log + mock outbox (gitignored)
```

## Design notes

- **Deterministic vs LLM** — the model extracts facts and writes prose; *code* decides whether fields are populated and renders the verdict and short-circuits. A model can be confident about an empty page; a field is populated or it isn't.
- **Bounded loop** — `BRENDO_MAX_FETCHES` (default 4) caps fetches beyond the homepage; at the cap it's a graceful handoff (flag for human), not "try harder".
- **Cost dials** — clean before reasoning, use a cheaper model for extraction, and cache (don't re-research within the TTL).
- **Defensibility** — every step appends a rationale to the decision log (`--why`).
- **Legality** — `robots.txt` is checked; single-fetch only; blocks/CAPTCHAs are logged and skipped, never bypassed.
