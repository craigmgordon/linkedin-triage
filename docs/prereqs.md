# Prerequisites

This document lists everything required to run the LinkedIn auto-triage script locally (Python + Playwright + OpenAI + optional SMTP email).

---

## 1) System requirements

- **macOS** (developed/tested on macOS; should work on Linux with minor adjustments)
- **Stable internet connection**
- **A LinkedIn account** that can access job search results
- **A working Python runtime** (recommended: 3.9+)

> Note: Playwright drives a real Chromium instance. The first run typically needs a visible browser window to establish the logged-in session.

---

## 2) Repository layout

Expected directories/files (created automatically if missing unless noted):

- `input/`
  - `candidate_profile.txt` *(recommended; required for good scoring)*
- `output/`
  - `job_triage.md` *(created if missing)*
  - `job_triage.csv` *(created if missing)*
  - `shortlist_apply_YYYY-MM-DD.md` *(created per run, overwritten each run for same day)*
  - `seen_jobs.json` *(created/updated)*
  - `llm_cache.json` *(created/updated)*
- `pw_profile/` *(created by Playwright; stores LinkedIn session/cookies)*

---

## 3) Python dependencies

Install via a virtual environment (venv, pyenv, conda — any is fine). Example:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install playwright openai
python -m playwright install chromium
```

### Why Playwright browser install is required

Playwright uses a bundled “Chrome for Testing” build. `playwright install chromium` downloads the browser binaries into your user cache.

---

## 4) OpenAI requirements

### Required environment variable

The script will fail fast unless this is set:

- `OPENAI_API_KEY` — OpenAI API key used for job scoring

Example:

```bash
export OPENAI_API_KEY="sk-..."
```

### Model selection

The script currently uses:

- `LLM_MODEL = "gpt-4o-mini"`

You can change the model in the script if desired, but do so knowingly (cost/speed/quality).

---

## 5) LinkedIn login prerequisites (Playwright persistent profile)

The script uses a persistent browser profile folder:

- `./pw_profile`

This means:
- Once you log in successfully in the Playwright browser window, subsequent runs reuse that session.
- You generally **do not** need to re-login unless cookies expire or LinkedIn challenges the session.

**First-time setup / refresh session**
1. Run the script manually from Terminal so you can see the browser.
2. If LinkedIn shows a login page, log in.
3. Close the browser window (or let the script proceed if it continues).
4. Future runs should reuse the saved session in `pw_profile/`.

---

## 6) Optional email (SMTP) prerequisites

Email is **optional**. If SMTP is configured, the script sends an “APPLY shortlist” email at the end of each run, and attempts to send a failure email if the script crashes.

### Required SMTP environment variables

All of these must be set for email to work:

- `SMTP_HOST` — e.g. `smtp.gmail.com`
- `SMTP_PORT` — typically `587`
- `SMTP_USER` — SMTP username (often your email address)
- `SMTP_PASS` — SMTP password or app password
- `SMTP_TO` — recipient email address

Optional:
- `SMTP_FROM` — defaults to `SMTP_USER` if not set
- `EMAIL_SUBJECT_PREFIX` — defaults to `[LinkedIn Triage]`
- `EMAIL_ENABLED` — controls sending behaviour:
  - `EMAIL_ENABLED=0` disables email even if SMTP is configured
  - `EMAIL_ENABLED=1` enables email (still requires SMTP vars)
  - unset => email is enabled if SMTP vars are present

Example:

```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="you@gmail.com"
export SMTP_PASS="your_app_password"
export SMTP_TO="you@gmail.com"
export SMTP_FROM="you@gmail.com"           # optional
export EMAIL_SUBJECT_PREFIX="[LinkedIn]"   # optional
export EMAIL_ENABLED="1"                   # optional
```

---

## 7) Optional test-mode flags

### Force APPLY on first job + stop early

Useful for validating email/outputs quickly:

- `FORCE_APPLY_FIRST_JOB=1`

Behaviour:
- First processed job is forced to `APPLY`
- Score is forced to at least `8.0/10`
- Script stops after that one job

Example:

```bash
export FORCE_APPLY_FIRST_JOB=1
```

---

## 8) Running the script

### Default search URL

If you run with no arguments, the script uses the embedded `DEFAULT_SEARCH_URL`. Note this url should be from the LinkedIn classic view NOt the AI assisted view.

```bash
python linkedin_triage.py
```

### Custom search URL

Pass a LinkedIn job search URL as the first argument:

```bash
python linkedin_triage.py "https://www.linkedin.com/jobs/search/?..."
```

---

## 9) Common failure modes and quick checks

### “You appear to be logged out”

- Run manually with `HEADLESS=False` (default)
- Log into LinkedIn in the opened browser
- Re-run

### Playwright browser install missing

Run:

```bash
python -m playwright install chromium
```

### Email not sending

- Confirm SMTP vars are actually present in the environment used to launch the script
- Check output logs for: `Email: skipped (SMTP not configured or disabled).`

---

## 10) Security notes

- Do **not** commit `.env` files, API keys, or SMTP credentials to git.
- Treat `pw_profile/` as sensitive: it contains session cookies.
- Use an “app password” for SMTP where possible (rather than your primary password).
