# LinkedIn Auto Triage — Overview

This document explains **what the script does**, **what it reads/writes**, and **where to look when something breaks**.

---

## What the script is for

The script automates a daily “first pass” on LinkedIn job search results:

- Opens a LinkedIn jobs search URL in a real Chromium browser (Playwright).
- Iterates through *new* job cards (not previously seen).
- Extracts summary fields + full job description text.
- Sends the job content to an LLM (OpenAI) using a **rubric-based scoring model**.
- Classifies each job into **APPLY / MAYBE / REJECT** based on thresholds and auto-reject flags.
- Writes a full report + CSV + per-day shortlist markdown.
- Emails you a daily “APPLY list” summary (optional, based on SMTP env vars).

It is designed to be run repeatedly (e.g., daily via launchd), without re-processing the same jobs.

---

## High-level flow (pseudo code)

```text
main():
  load search_url (argv[1] or DEFAULT_SEARCH_URL)
  ensure output directories and files exist
  load seen_jobs set (output/seen_jobs.json)
  apply_jobs_for_email = []

  with Playwright persistent browser context (pw_profile):
    open LinkedIn search page
    if login screen detected: exit with message

    detect job card selector
    detect left scroll container (for scrolling the job list)

    while processed < MAX_NEW and actions < MAX_TOTAL_ACTIONS:
      find next job card that is NOT in seen_jobs and NOT in processed_this_run
      if no unseen cards visible:
        scroll list down; if at bottom try pagination Next; else continue
        if cannot progress after multiple attempts: stop

      click job card to load right pane details
      extract summary fields (title/company/location/meta/pills/apply type/etc)
      extract full job description (expand “See more” if present)

      (score, decision, reasons, llm_details) = llm_triage(job_id, summary, jd_text)

      if FORCE_APPLY_FIRST_JOB=1 and this is the first job:
        force decision APPLY and stop after this job (test mode)

      write:
        - append job entry to output/job_triage.md
        - append job row to output/job_triage.csv
        - if APPLY: append to output/shortlist_apply_YYYY-MM-DD.md
                 and collect for email summary

      mark job_id as seen and processed
      print one-line status to stdout

  save seen_jobs to output/seen_jobs.json

  build email body from apply_jobs_for_email
  send email (if SMTP configured / enabled)
```

---

## Inputs

### 1) Candidate profile file

- **Path:** `input/candidate_profile.txt`
- **Purpose:** Your “source of truth” profile (skills, preferences, dealbreakers, weights, etc.).
- **Used by:** `load_candidate_profile()` → included in the LLM scoring payload.

If the file does not exist, the script uses a small built-in fallback profile (less accurate).

### 2) LinkedIn search URL

- Default: `DEFAULT_SEARCH_URL` (hard-coded)
- Note this should be from the LinkedIn Classic view NOT the AI assisted view
- Or pass a URL at runtime:

```bash
python linkedin_triage.py "https://www.linkedin.com/jobs/search/?..."
```

### 3) Environment variables

Two groups:

- **OpenAI**: `OPENAI_API_KEY` (required)
- **SMTP/email** (optional): `SMTP_*`, `EMAIL_*`
- Optional runtime switch: `FORCE_APPLY_FIRST_JOB=1`

(Full list is documented in `prereqs.md`.)

---

## Outputs

All outputs are written under:

- **Directory:** `output/`

### 1) Full markdown report (all jobs processed)

- **File:** `output/job_triage.md`
- Contains one section per job with:
  - Title/company/location/meta/pills/apply type
  - Decision + score
  - LLM triage details (reasons, matched/missing skills, red flags, questions)
  - Full captured job description text

### 2) CSV (machine-friendly log of all jobs processed)

- **File:** `output/job_triage.csv`
- One row per job with consistent fields (useful for filtering/sorting).

### 3) Daily “APPLY” shortlist markdown

- **File:** `output/shortlist_apply_YYYY-MM-DD.md`
- Recreated at the start of each run for that day (prevents duplicates on reruns).
- Includes only jobs whose computed decision is **APPLY**.

### 4) Seen jobs cache

- **File:** `output/seen_jobs.json`
- Stores job IDs already processed, so future runs skip them.

### 5) LLM response cache

- **File:** `output/llm_cache.json`
- Stores the rubric output per job_id, so re-running does not pay/token-spend again for the same job_id.
- **Important:** If you delete `output/`, you delete this cache and scores may vary between runs.

---

## Logs (where to look)

There are two distinct contexts:

### A) When you run manually in Terminal

- You will see progress printed to stdout, e.g.:  
  `"[3/30] MAYBE 6.1/10 (61.0/100) — Senior DevOps Engineer @ Company"`

If something fails, you’ll see the traceback in your Terminal.
The script also writes timestamped logs under `output/logs/`.

### B) When run via launchd (scheduled)

The script writes timestamped logs:

- **stdout:** `output/logs/YYYY-MM-DD_HH-MM-SS.out.log`
- **stderr:** `output/logs/YYYY-MM-DD_HH-MM-SS.err.log`

These are the first places to check after a scheduled run fails. The script
keeps only the last 3 days of logs by default.

---

## How decisions are determined (scoring model)

The LLM is asked to return:

- `total_score_100` (0–100)
- `category_scores` (subscores that sum to 100 before penalties)
- `penalties_applied`
- `auto_reject_flags` (booleans)

The script **computes** the decision locally from `total_score_100` and `auto_reject_flags`:

- If `auto_reject_flags.on_site_only == true` OR `commute_over_90 == true` ⇒ **REJECT**
- Else if score ≥ 75 ⇒ **APPLY**
- Else if score ≥ 55 ⇒ **MAYBE**
- Else ⇒ **REJECT**

This makes classification consistent even if the model’s `decision` field is imperfect.

---

## Browser/session model (why pw_profile matters)

The script uses a persistent Playwright profile directory:

- **Directory:** `./pw_profile`

This is how it stays logged into LinkedIn between runs. If LinkedIn logs you out, the script detects a password field and exits with a message to log in again.

---

## Common failure modes (quick triage)

### 1) “You appear to be logged out”
- LinkedIn session expired or cookie invalid.
- Fix: run interactively once, log in in the browser window, re-run.

### 2) Playwright launch timeout (scheduled runs)
Typical when:
- macOS woke but user session wasn’t fully unlocked / GUI not ready
- background task restrictions / permissions
- Playwright cannot open the browser UI in time

Fix approaches are covered in `scheduling.md` (especially around running in a user session and ensuring the Mac is awake/unlocked).

### 3) Everything becomes “5.0/10”
Usually indicates:
- Cache/field mismatch and fallback logic triggered
- Model output didn’t include expected keys and code fell back to defaults

Fix: inspect `output/llm_cache.json` for a few entries to confirm what keys are being stored.

---

## Minimal “where is everything” map

```text
input/
  candidate_profile.txt

output/
  job_triage.md
  job_triage.csv
  shortlist_apply_YYYY-MM-DD.md
  seen_jobs.json
  llm_cache.json
  logs/
    YYYY-MM-DD_HH-MM-SS.out.log
    YYYY-MM-DD_HH-MM-SS.err.log

pw_profile/   (Playwright persistent Chromium profile)
```

---

## Safe “test mode”

To do a fast end-to-end test (including email + outputs) without processing many jobs:

```bash
FORCE_APPLY_FIRST_JOB=1 python linkedin_triage.py
```

In this mode, the first processed job is forced to APPLY and the script exits immediately after that job.
