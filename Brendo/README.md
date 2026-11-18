# Brendo

An automated, defensible **cold-outreach pipeline** for Sprinto (MESA AI Case 2 / "B2 — Cold Outreach, at Scale"). Give it a list of company domains. For each right-fit company it finds the decision-maker, drafts a personalised cold email and sends it to **your own inbox**, recording *why* at every step.

> Nothing is ever sent to a real prospect: the recipient is your own address (`BRENDO_SEND_TO`), and live sends are refused while it is still a placeholder.

## The 7-step pipeline

1. **Scrape** the website: single fetch, `robots.txt` respected, a polite pause between requests, and a headless browser only for JS-rendered pages.
2. **Extract** structured company fields from cleaned text, then **verify** them: a fact is kept only if its quoted evidence is on the page.
3. **Qualify** against Sprinto's ICP with an **adaptive loop** that fetches a *named* page when a required fact is missing, under a hard fetch cap. Companies are then **ranked**: Series A–C and compliance pressure rank higher.
4. **Find the point of contact (PoC)**: `inputs/contacts.csv` (Apollo lookups) or the Hunter.io API, choosing by **ranked buyer personas**, with the reason logged.
5. **Two pain points** for that person, each tied to a verified fact.
6. **Draft** an email by name, with one hook resting on a fact. The draft is checked in code and redrafted once if it fails.
7. **Send** via Gmail to yourself, with a **review note** (why this company, person and email) under the draft.

## Run it now (no keys, no network)

```bash
pip install -e ".[dev]"      # or: pip install -r requirements.txt
brendo --offline             # or: PYTHONPATH=src python -m brendo.cli --offline
```

On Windows: `.\run.ps1 --offline`.

Expected results on the bundled sample sites (`inputs/domains.csv`):

| Domain | Result | Why |
| --- | --- | --- |
| `acme.example` | **drafted**, rank high | Series B and a Compliance Manager opening, both found on the careers page by the loop |
| `headerhero.example` | **drafted**, rank high | Hero copy sits inside `<header>`; Series A; answers enterprise security questionnaires |
| `lateco.example` | **drafted**, rank lower | Series E is outside Series A–C, so it is flagged |
| `thinsignal.example` | qualified_flagged | SaaS, but no stage signal and too few grounded pains, so not drafted |
| `securedhq.example` | disqualified | Already holds SOC 2 and ISO 27001 |
| `sunrisebakery.example` | disqualified | Not B2B SaaS |

In mock mode, emails are written to `data/outbox/` and the status is `drafted`. A live Gmail send shows `sent`.

## Commands

| Command | What it does |
| --- | --- |
| `brendo --domains inputs/domains.csv` | Run the pipeline (add `--offline`, `--browser`, `--force`) |
| `brendo --domain acme.com` | Run one domain |
| `brendo --why acme.com` | Decision log for the latest run of that domain (`--run ID` for an earlier one) |
| `brendo --export` | Campaign record for the latest run: `out/campaign-<run>.csv` + `.md` |
| `brendo --review out/campaign-<run>.csv` | Import the review columns; prints rewrite rate and PoC accuracy |
| `brendo --purge acme.com` | Delete everything stored about a domain (personal data) |
| `brendo --check` | Shows which services are live and which are mocked |

## The review loop (measuring it)

The deck's 5 pre-build decisions (slide 37) set the bar: under 2 minutes per company, fewer than 20% of emails rewritten, and PoC accuracy of at least 60%. Shut it down if more than 50% are rewritten or PoC accuracy falls below 60%.

1. A run prints the time per company and the tokens and cost.
2. `brendo --export` writes the campaign record. Fill in `review_outcome` (`approved`, `edited` or `rejected`) and `poc_correct` (`y` or `n`).
3. `brendo --review <csv>` stores the outcomes and prints the two kill-criterion numbers.

## Real mode

Fill in `.env` (the template is `.env.example`), then run `brendo --check`. Each key is optional, and any part without one stays in mock mode:

| Key | Enables | Without it |
| --- | --- | --- |
| `GEMINI_API_KEY` | Extraction, pain points and drafting with Gemini (`gemini-3.5-flash-lite`, `gemini-4-hydrogen`) | Offline keyword mock |
| `ANTHROPIC_API_KEY` | The same with Claude (Haiku 5.5, Sonnet 5.5) instead | Offline keyword mock |
| `HUNTER_API_KEY` | Live PoC lookup | Placeholder contact (flagged) |
| `inputs/contacts.csv` | Apollo lookups by hand: `domain,name,role,email` | Hunter, or mock |
| `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` | Real Gmail send | Writes to `data/outbox/` |
| `BRENDO_SEND_TO` | Your inbox (the only recipient) | Live sends are refused |

**Where to get each key**

- **`GEMINI_API_KEY`:** aistudio.google.com → Get API key (starts with `AIza`). Both default models have a free tier, which is rate-limited; Brendo waits and retries when it hits the limit. The cost line in the report assumes paid rates. If your key can't use `gemini-4-hydrogen`, drafting falls back to the extraction model; or set `BRENDO_GEMINI_MODEL_DRAFT`.
- **`ANTHROPIC_API_KEY`:** console.anthropic.com. Extraction and pain points use Haiku 5.5 at low effort; drafting uses Sonnet 5.5 with server-side refusal fallback. This is the deck's "cheap model for volume" cost dial.
- **`HUNTER_API_KEY`:** hunter.io. The free tier allows 25 domain searches a month, and results are cached. Alternatively, look people up in Apollo and add a row per domain to `inputs/contacts.csv`.
- **Gmail:** turn on 2-step verification, then create an **App Password** at myaccount.google.com. Put it in `GMAIL_APP_PASSWORD` and your address in `GMAIL_ADDRESS`.

> `BRENDO_APOLLO_CONTACT` (one global contact) has been removed because it gave every company the same person. Use `inputs/contacts.csv`.

## The ICP lives in config

`config/icp.json` holds the deck's **Evidence Map** (slide 40): the questions, which page answers each, the per-question fetch limit and the short-circuits. It also holds the **ranking** signals (target stages, compliance-pressure terms) and the ranked **buyer personas** used to choose a contact. To change who qualifies or who gets contacted, edit that file, not the code.

## Tests

```bash
pytest -q
```

There are 68 offline tests, with no keys and no network. They cover the cleaner, the scraper (robots, blocks, browser fallback, polite delay), evidence verification, both LLM clients against fake servers, every ICP path and ranking, persona choice, draft checks and redraft, the send guard, the cache and decision log, the DB upgrade, export and review, and the CLI. CI runs them on every push (`.github/workflows/tests.yml`).

## Layout

```
src/brendo/   cli · orchestrator · scraper · cleaner · extractor · qualifier
              poc · painpoints · emailer · exporter · store · llm · config · schemas
config/icp.json   Evidence Map + ranking + personas     samples/   offline demo sites
inputs/           domains.csv, contacts.csv             tests/     offline tests
data/             SQLite + mock outbox (gitignored)     out/       campaign records (gitignored)
```

## Design notes

- **Two providers:** `llm.py` has a Claude client (Anthropic SDK, structured outputs) and a Gemini client (REST, `responseJsonSchema`, falling back to schema-in-prompt if the API rejects it). Both return the same named fields, so the rest of the pipeline doesn't know which ran.
- **Deterministic vs LLM:** the model extracts facts and writes prose. *Code* checks that each fact's evidence is on the page, decides whether fields are filled, renders the verdict, picks the contact and fixes the recipient.
- **Bounded loop:** `BRENDO_MAX_FETCHES` (default 4) caps fetches beyond the homepage. At the cap the company is flagged for a human, which counts as a designed handoff, not a failure.
- **Failures:** a homepage that can't be reached ends the company as `failed`, with no further fetches. Failures are never cached, so the next run retries them. One failure never stops the batch.
- **Cost dials:** clean the page before the model reads it, use a cheaper model for extraction, and cache final results for `BRENDO_CACHE_TTL_DAYS`. Each run reports its tokens and cost.
- **Defensibility:** every step writes its reason to the decision log (`--why`, grouped by run). The self-sent copy of each email carries the same reasoning.
- **Legality:** `robots.txt` is checked; there is one fetch per page with a pause between requests; blocks and CAPTCHAs are logged and skipped, never bypassed. `--purge` deletes a company's personal data.
