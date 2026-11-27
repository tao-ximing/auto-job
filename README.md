# auto job

English | [中文](README.zh-CN.md)

A personal job tracker. It scrapes SEEK and LinkedIn, filters and deduplicates the postings, stores them in SQLite (`jobs.db`), scores each one against my resume with an LLM, and serves a mobile-first web app for reviewing jobs and tracking applications, with push notifications to my phone.

- How the project works, data dictionary, API, rules (Chinese): [ARCHITECTURE.md](ARCHITECTURE.md)
- Change log (Chinese): [CHANGELOG.md](CHANGELOG.md)
- Rules for AI coding agents: [CLAUDE.md](CLAUDE.md)

## Access

- Local: http://127.0.0.1:8765
- Phone (inside the tailnet, with Tailscale on): https://&lt;host&gt;.&lt;tailnet&gt;.ts.net:8443/
  - iPhone: open in Safari → Share → Add to Home Screen → open from the icon → turn on notifications

## Scheduled runs

Everything runs as systemd user units, with lingering enabled so they start at boot:

| Unit | What it does |
|---|---|
| `auto-job.timer` → `auto-job.service` | Hourly from 8:00 to 22:00: scrape SEEK + LinkedIn, score new jobs, sync Gmail; notify on new jobs, application updates or errors |
| `auto-job-mail.timer` → `auto-job-mail.service` | Every minute, all day: check the Gmail historyId and sync only when there is new mail (including email lookup and push) |
| `auto-job-web.service` | The web app on 127.0.0.1:8765 |
| `tailscale serve --https=8443` | Publishes the web app to the tailnet |

```bash
systemctl --user list-timers auto-job.timer       # when the next run is
systemctl --user start auto-job.service           # run once now
journalctl --user -u auto-job.service -n 50       # logs
systemctl --user restart auto-job-web.service     # restart the web app after changing web.py (static files are read per request)
```

LinkedIn's logged-in mode runs Chrome on an Xvfb virtual display (no window pops up; `sudo apt install xvfb`), but it needs a logged-in desktop session so the keyring is unlocked and cookies can be read. If the login has expired, or no desktop session is logged in, it falls back to LinkedIn's public guest endpoints and shows a red banner in the web app.
To log the dedicated profile in again: `node browser.mjs login`.

## Match scoring

Each new job that passes the rule filters is scored 0-100 by the `agy` (Antigravity) CLI (Gemini, default `gemini-4-neon-medium`) using my subscription, not an API key.

- The resume and scoring rules live outside this repo, in `~/job-match/AGENTS.md`. The key question is whether the resume, as it is, would get past ATS and a recruiter screen, not whether I could do the job.
- Each job gets a track (AI / general SWE / needs a different resume / cross-stack), a reason, gaps and red flags.
- It runs a minimal custom agent (`~/job-match/.agents/agents/scorer/agent.md`, `excludeDefaultComponents: true`) to drop agy's built-in system prompt: about 5k tokens per job instead of 15k.
- Jobs are scored in the background while scraping, so a score appears seconds after a job is ingested.

## Common commands

```bash
python3 run.py                       # scrape once by hand (SEEK last 3 days + LinkedIn last 24 hours)
python3 ingest.py --refilter         # re-run filters after editing filters.toml (keeps manual statuses)
python3 score.py --rescore           # re-score pending jobs after editing ~/job-match/AGENTS.md
python3 push.py test "title" "body"  # send a test push to the phone
python3 -m unittest test_core        # core tests (status rules, background scorer, concurrent writes)
python3 -m unittest test_rules       # tests for filtering and dedup (need local filters.toml)
python3 docs_check.py                # docs match the code
node checks/ui_check.mjs             # web page check against the running server
python3 gmail.py test                # test the read-only Gmail connection (re-authorise: python3 gmail.py auth)
python3 run.py --only gmail          # sync Gmail only
```

## Files

| File | Purpose |
|---|---|
| `searches.toml` | 8 searches (2 roles × 2 locations × 2 sites); not in git, template in `searches.example.toml` |
| `filters.toml` | Filter rules; not in git, template in `filters.example.toml` |
| `seek.py` | SEEK JSON API |
| `linkedin.mjs` + `browser.mjs` | LinkedIn logged-in mode (dedicated Chrome profile) |
| `linkedin_guest.py` | LinkedIn guest mode (automatic fallback) |
| `seek_links.mjs` | Company-site apply links for SEEK link-out jobs (opens the apply page in the logged-in profile and records the redirect) |
| `rules.py` | Normalisation, filters, dedup scoring (pure functions) |
| `ingest.py` | Ingest, filter, dedup, merge |
| `score.py` | Match score: agy CLI scores jobs against `~/job-match/AGENTS.md` (resume + rules, not in git) |
| `run.py` | Runs all searches + notifications |
| `db.py` | Schema |
| `web.py` + `static/` | Web app + PWA (manifest, service worker, icons) |
| `push.py` | Web Push (VAPID private key in `push_keys.json`, not in git) |
| `gmail.py` | Read-only Gmail auth and fetching (`credentials.json` + `token.json`, not in git) |
| `gmail_sync.py` | Mail sync: prefilter, classify, match to jobs, advance statuses |
| `email_lookup.py` | Email lookup: when an email matches no job in the database, search SEEK / LinkedIn and create the job |
| `llm.py` | Gemini email classification (with a monthly budget cap) |
| `test_rules.py` | Tests (not in git: they depend on my personal rules) |
| `test_core.py` | Core tests that don't depend on personal rules |
| `docs_check.py` | Checks ARCHITECTURE.md / CHANGELOG.md against the code (run by the pre-commit hook) |
| `checks/ui_check.mjs` | Web page check: script syntax, page boot, every apply-link case |
| `hooks/` | Git hooks (`git config core.hooksPath hooks`): require a CHANGELOG entry and a passing docs check |
