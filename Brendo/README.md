# Brendo

An automated, defensible **cold-outreach pipeline** for Sprinto (MESA AI Case 2 / "B2 — Cold Outreach, at Scale"). Give it a list of company domains. For each right-fit company it finds the decision-maker, drafts a personalised cold email and sends it to **your own inbox**, recording *why* at every step.

> Nothing is ever sent to a real prospect: the recipient is your own address (`BRENDO_SEND_TO`), and live sends are refused while it is still a placeholder.

## The 7-step pipeline

1. **Scrape** the website: single fetch, `robots.txt` respected, a polite pause between requests, and a headless browser only for JS-rendered pages.
2. **Extract** structured company fields from cleaned text, then **verify** them: a fact is kept only if its quoted evidence is on the page.
3. **Qualify** against Sprinto's ICP with an **adaptive loop** that fetches a *named* page when a required fact is missing, under a hard fetch cap. The shortlist rules follow the Evals deck:
   - **Funding must be Series A–C.** Seed, Series D+ and bootstrapped companies are rejected; an unknown stage never rejects on its own.
   - **Size has a soft 50–200 employee band.** 35–49 and 201–350 are accepted, noted as near the band and flagged; further out is rejected; an unknown size is flagged.
   - **Already certified** or **not B2B SaaS** → rejected.
   - **Stale or wrong data isn't trusted:** funding news more than 2 years old doesn't count as the stage, and a domain that redirects to another company's site is handed to a human.
   - **Hostile page text** ("ignore your instructions…") is removed before the model reads the page, and the company is handed to a human.
   - **At the fetch cap** with a required question unanswered, the company is **escalated** to a human; there's never a silent extra fetch.

   Qualified companies are **ranked**: compliance pressure ranks higher.
4. **Find the point of contact (PoC)**: `inputs/contacts.csv` (Apollo lookups) or the Hunter.io API, choosing by **ranked buyer personas**, with the reason logged.
5. **Two pain points** for that person, each tied to a verified fact.
6. **Draft** an email by name, with one hook resting on a fact, following the Growth Lead's past corrections where they apply (the **feedback memory**). The draft is checked in code (first name, length, a grounded hook, no placeholders, no links or contact details) and redrafted once if it fails.
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
| `lateco.example` | disqualified | Series E is outside Series A–C |
| `thinsignal.example` | escalated | SaaS, but the fetch cap is reached with the stage still unanswered, so a human decides |
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
| `brendo --web` | The website on this PC at http://localhost:8000 (see below) |

## The website (local, or on Vercel)

The whole workflow also runs from a web page: unlock → setup check → paste domains → **Run** → read each company's verdict, contact, pains, email and send result as it finishes → **edit a draft** and **send your version** → mark the review → download the campaign record (CSV / Markdown).

**Editing and the feedback memory.** Edit any draft in place:
- **Save my edit** asks the model whether the change is a real correction (a new hook, a different claim, a tone change, even punctuation only) or just a typo. Real corrections are kept as one-line lessons in "What Brendo has learned", in your browser.
- **Send this version to my inbox** sends your edited text, still only to `BRENDO_SEND_TO`.
- On later runs, each lesson is applied where it fits:
  - a lesson that needs a fact (like a funding round) applies only to companies that have it;
  - a lesson written for one kind of prospect applies only to that kind;
  - on the same topic, the newest lesson wins.
- The decision log says which lessons were applied and why the others weren't. **Download memory** / **Import memory** move it between browsers.
- The CLI reads the same lessons from `data/corrections.json`. It calls the same tested pipeline as the CLI, one company per request, so the page shows progress company by company. `/blueprint.html` serves the technical blueprint.

**On your PC** (uses `Brendo/.env`; reachable only from this computer):

```bash
brendo --web            # or: PYTHONPATH=src python -m brendo.web   ·   Windows: .\run.ps1 --web
```

Then open http://localhost:8000.

**On Vercel** (a public link, protected by a site password):

1. vercel.com → **Add New… → Project** → import the GitHub repo `Aastha-Product/sprinto`.
2. **Root Directory:** `Brendo`. **Framework Preset:** Other. Leave the build settings as they are (`vercel.json` sets them).
3. **Environment Variables** (Settings → Environment Variables): the same names as `.env`, typed into Vercel, never committed:
   `GEMINI_API_KEY` (or `ANTHROPIC_API_KEY`), `HUNTER_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `BRENDO_SEND_TO`, and **`BRENDO_WEB_PASSWORD`**, a password you choose for the site. Without `BRENDO_WEB_PASSWORD` the site refuses to run, so a stranger with the link can't spend your quotas.
4. **Deploy.** Open the `….vercel.app` link, enter the site password, and click **Test the AI key**.

Things that differ on Vercel:

- Each company gets up to 300 seconds (`maxDuration` in `vercel.json`); a company normally takes about a minute.
- The cache lives in the server's temporary storage, so it may be gone on the next visit. A company may simply be researched again, which uses another Hunter search.
- The headless-browser fallback is off. A site that only renders with JavaScript ends as thin or flagged.
- Results stay in your browser (refresh-safe) until the next run. Download the CSV to keep them.
- Sending is unchanged: only to `BRENDO_SEND_TO`.

## Evaluation harness (`evals/`)

Brendo's own test table, from the Evals deck. `evals/cases/*.json` holds one row per partition of every decision the system makes. Each row has an **Input**, the **Expected Behaviour**, **Assertions** and a **Severity**.

| File | Decision | Rows |
| --- | --- | --- |
| `d1_shortlist.json` | D1 · does the company match the ICP? (funding, size, SaaS, certified) | 12 |
| `d2_sufficiency.json` | D2 · has the loop found enough? (completeness, recency, identity) | 4 |
| `d3_loop_cap.json` | D3 · when does the loop stop? (below the cap, at the cap) | 2 |
| `d4_d5_feedback.json` | D4 · which corrections apply · D5 · which edits count | 6 |
| `email_done_bar.json` | the brief's "done" bar: by name, grounded hook, no contact → no email | 3 |
| `adversarial.json` | hostile page text: change the verdict, email someone else, fake system prompt | 3 |
| `canary.json` | the test that should fail | 1 |

`evals/harness.py` runs the table against the **deployed** site over HTTP; it never imports Brendo's code. The site serves each row's fixed pages instead of the web, never calls Hunter and never sends. Run it from `Brendo/`:

```bash
python evals/harness.py --url https://<your-site>.vercel.app
```

It reads the site password from `BRENDO_WEB_PASSWORD` and judge keys from `Brendo/.env`. Use `--only D1` for one decision and `--repeat 1` for a quick run.

- **Hard assertions** are checked in code. **Soft assertions** (does the hook read human? does the reasoning say why 45 employees was accepted?) are graded by an **LLM judge from a different model family**: if Brendo drafts with Gemini, the judge is Claude (it needs `ANTHROPIC_API_KEY`), and the other way round. Without a judge key, soft rows are reported as *not judged*, never as passed.
- **Severity decides shipping:**
  - one **Critical** failure → **DO NOT SHIP**;
  - **High** rows must pass at 90% or more;
  - **Low** rows are logged and never block.
- **Self-checks** run every time. If any of them fails, the run is **VOID**:
  - every row runs **twice**, and a row whose verdict changes is **flaky**;
  - the **canary** row must fail;
  - every row must **reject a deliberately corrupted output**.
- **Human audit:** about 10% of judged rows are written to `evals/out/audit-<stamp>.csv` for a person to grade **blind**. `python evals/harness.py --audit-check evals/out/audit-<stamp>.csv` then logs every judge-vs-human disagreement.
- **Reports** go to `evals/out/report-<stamp>.md` and `.json`: totals by severity, a one-line diagnosis per failure, and the verdict.

| Exit code | Verdict |
| --- | --- |
| 0 | SHIP |
| 1 | HOLD (High pass rate below 90%) |
| 2 | DO NOT SHIP (a Critical row failed) |
| 3 | VOID (a self-check failed) |
| 4 | INCOMPLETE (soft rows not judged) |

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

There are 123 offline tests, with no keys and no network. They cover:
- the cleaner, and the scraper (robots, blocks, browser fallback, polite delay);
- evidence verification, and both LLM clients against fake servers;
- every shortlist partition: funding, the size band, certified, SaaS, recency, identity;
- the injection guard, persona choice, draft checks and redraft, and the send guard;
- the feedback memory (what counts as a correction, relevance, conflicts);
- the cache and decision log, the DB upgrade, export and review, and the CLI;
- the website's API over real HTTP;
- the evaluation harness itself: the whole table runs against a local Brendo, plus the judge, canary and audit paths. CI runs them on every push (`.github/workflows/tests.yml`).

## Layout

```
src/brendo/   cli · orchestrator · scraper · cleaner · extractor · qualifier
              poc · painpoints · emailer · exporter · store · llm · config · schemas · web
              guard (prompt injection) · feedback (corrections memory)
public/       the website (index.html)                  api/       Vercel functions: check · run · export · edit · send
evals/        test table (cases/*.json) + harness.py    evals/out/ reports and audit sheets (gitignored)
vercel.json   Vercel settings (root directory: Brendo)
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
