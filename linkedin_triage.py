"""
LinkedIn Job Auto-Triage Script (Playwright + OpenAI + SMTP)

High-level flow
---------------
1) Open LinkedIn job search URL in a persistent Chromium profile (Playwright).
2) Iterate job cards from the left-hand list (two-pane layout).
3) For each unseen job:
   - Click card -> extract job summary + full job description from right pane.
   - Score the job with an LLM using a fixed rubric (0..100), derive decision (APPLY/MAYBE/REJECT).
   - Append results to:
       - output/job_triage.md (full report)
       - output/job_triage.csv (structured)
       - output/shortlist_apply_YYYY-MM-DD.md (APPLY only)
   - Add job_id to seen cache to avoid re-processing.
4) After processing, send an email summary for APPLY jobs (optional; SMTP env vars).
5) If the script crashes, attempt to send a failure email (best-effort).

Notes on "no functionality changes"
-----------------------------------
This file is the same logic as your current version, but heavily commented.
I have NOT changed behaviour or data flow. I have only:
- added explanatory comments/docstrings
- grouped functions into clearer sections
- left function bodies and logic intact (except comment-only edits)
"""

import csv
import json
import random
import re
import sys
import time
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from openai import OpenAI
from playwright.sync_api import sync_playwright

# Email support (optional)
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


# =============================================================================
# Configuration
# =============================================================================

# Default LinkedIn search URL. You can override by passing a URL as argv[1].
DEFAULT_SEARCH_URL = (
    "https://www.linkedin.com/jobs/search/?distance=25.0&f_E=4&f_JT=F&f_TPR=r172800"
    "&f_WT=2%2C3&geoId=102257491&keywords=(%22platform%20engineer%22%20OR%20%22devops%20engineer%22%20OR%20%22site%20reliability%20engineer%22%20OR%20sre)"
    "&origin=JOB_SEARCH_PAGE_JOB_FILTER"
)

# Test-mode flag:
# If set, the first processed job will be forced to APPLY and we stop early.
FORCE_APPLY_FIRST_JOB = os.getenv("FORCE_APPLY_FIRST_JOB", "0") == "1"

# Input/output structure
INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")

# Logging
LOG_DIR = OUTPUT_DIR / "logs"
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "3").strip() or "3")

PROFILE_FILE = INPUT_DIR / "candidate_profile.txt"
LLM_CACHE_FILE = OUTPUT_DIR / "llm_cache.json"   # job_id -> cached LLM outputs
SEEN_FILE = OUTPUT_DIR / "seen_jobs.json"        # job_ids already processed

# Human-readable report & CSV
OUT_MD_ALL = OUTPUT_DIR / "job_triage.md"
OUT_CSV = OUTPUT_DIR / "job_triage.csv"

# Daily “apply” shortlist (one file per day).
TODAY_STR = datetime.now().date().isoformat()
OUT_MD_APPLY = OUTPUT_DIR / f"shortlist_apply_{TODAY_STR}.md"

# Crawl limits / safety caps
MAX_NEW = 30                 # Maximum new jobs to triage per run
# Safety cap on loop iterations (avoids infinite loops)
MAX_TOTAL_ACTIONS = 800
# Keep False for LinkedIn stability (UI interactions more reliable)
HEADLESS = False
# Adds human-like pauses (stability + lowers bot-like behaviour)
RANDOM_WAIT = True

# LLM tuning
LLM_MODEL = "gpt-4o-mini"
# 0.0 = deterministic-ish (still can vary if prompt differs)
LLM_TEMPERATURE = 0.0
LLM_MAX_JD_CHARS = 12000     # limit job description length sent to LLM
# additional retries (total attempts = 1 + retries)
LLM_RETRIES = 2
LLM_RETRY_BACKOFF_S = 1.5    # base backoff multiplier

# SMTP configuration via environment variables (optional)
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587").strip() or "587")
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER).strip()
SMTP_TO = os.getenv("SMTP_TO", "").strip()

# Email behaviour:
# - EMAIL_ENABLED=0 explicitly disables
# - EMAIL_ENABLED=1 explicitly enables (if SMTP config present)
# - EMAIL_ENABLED unset: default to sending if SMTP config is present
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "").strip()
EMAIL_SUBJECT_PREFIX = os.getenv(
    "EMAIL_SUBJECT_PREFIX", "[LinkedIn Triage]").strip()

# =============================================================================
# Logging (stdout/stderr -> timestamped daily files)
# =============================================================================

class TimestampedTee:
    def __init__(self, stream, file_handle):
        self.stream = stream
        self.file_handle = file_handle
        self.at_line_start = True

    def write(self, data: str) -> int:
        if not data:
            return 0
        written = 0
        for chunk in data.splitlines(True):
            if self.at_line_start:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                prefix = f"[{ts}] "
                self.stream.write(prefix)
                self.file_handle.write(prefix)
                written += len(prefix)
            self.stream.write(chunk)
            self.file_handle.write(chunk)
            written += len(chunk)
            self.at_line_start = chunk.endswith("\n")
        self.stream.flush()
        self.file_handle.flush()
        return written

    def flush(self) -> None:
        self.stream.flush()
        self.file_handle.flush()

    def isatty(self) -> bool:
        return self.stream.isatty()


def _cleanup_old_logs(log_dir: Path, retention_days: int) -> None:
    cutoff = datetime.now() - timedelta(days=retention_days)
    for path in log_dir.glob("*.log"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink()
        except OSError:
            # Best-effort cleanup; ignore files that disappear or are locked.
            pass


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_path = LOG_DIR / f"{run_ts}.out.log"
    err_path = LOG_DIR / f"{run_ts}.err.log"

    out_handle = out_path.open("a", encoding="utf-8")
    err_handle = err_path.open("a", encoding="utf-8")

    sys.stdout = TimestampedTee(sys.stdout, out_handle)
    sys.stderr = TimestampedTee(sys.stderr, err_handle)

    _cleanup_old_logs(LOG_DIR, LOG_RETENTION_DAYS)


setup_logging()

# OpenAI API key is mandatory for scoring.
if not os.getenv("OPENAI_API_KEY"):
    raise RuntimeError("OPENAI_API_KEY is not set")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# =============================================================================
# Console colours (ANSI)
# =============================================================================
ANSI_RESET = "\033[0m"
ANSI_RED = "\033[31m"
ANSI_YELLOW = "\033[33m"
ANSI_GREEN = "\033[32m"


def colour_for_decision(decision: str) -> str:
    """
    Map decision -> ANSI colour.
    - APPLY: green
    - REJECT: red
    - MAYBE: yellow
    """
    d = (decision or "").upper()
    if d == "APPLY":
        return ANSI_GREEN
    if d == "REJECT":
        return ANSI_RED
    return ANSI_YELLOW


def colourise(text: str, colour: str) -> str:
    """
    Apply ANSI colour codes if stdout is a real terminal.
    If logs are being captured (e.g., launchd), avoid writing ANSI escape sequences.
    """
    if not sys.stdout.isatty():
        return text
    return f"{colour}{text}{ANSI_RESET}"


# =============================================================================
# Outputs / files
# =============================================================================

def ensure_outputs_exist():
    """
    Create input/output directories and initialise output files if missing.

    Behaviour notes:
    - OUT_MD_ALL is created if missing and then appended to each run.
    - OUT_MD_APPLY is overwritten each run (fresh daily file header),
      to prevent duplicates if script re-runs multiple times on the same day.
    - OUT_CSV is created with a header row if missing.
    """
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not OUT_MD_ALL.exists():
        OUT_MD_ALL.write_text("# LinkedIn Job Triage\n\n", encoding="utf-8")

    # Daily file is reset each run (still same filename for same date).
    OUT_MD_APPLY.write_text(
        f"# Shortlist — Apply ({TODAY_STR})\n\n", encoding="utf-8")

    if not OUT_CSV.exists():
        with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    f.name for f in JobResult.__dataclass_fields__.values()],
            )
            writer.writeheader()


# =============================================================================
# Data model
# =============================================================================

@dataclass
class JobResult:
    """
    A single triaged job record written to both CSV and Markdown.

    Many fields are extracted from LinkedIn's two-pane job UI.
    'reasons' is a debug string for scraping selectors etc.
    LLM-provided detail is stored separately in llm_details blocks in Markdown and llm_cache.json.
    """
    job_id: str
    url: str
    title: str
    company: str
    location: str
    meta_line: str
    pills: str
    promoted_line: str
    apply_type: str
    contacts: str
    jd_length: int
    score: float
    decision: str
    reasons: str
    extracted_at: str


# =============================================================================
# General utility helpers
# =============================================================================

def clean_text(s: str) -> str:
    """Collapse all whitespace to single spaces, trim edges."""
    return re.sub(r"\s+", " ", (s or "")).strip()


def canonical_job_url(job_id: str) -> str:
    """Build a stable LinkedIn job view URL from a job ID."""
    return f"https://www.linkedin.com/jobs/view/{job_id}/" if job_id else ""


def human_wait(min_ms=250, max_ms=650):
    """
    Sleep for a short period to reduce UI flakiness and avoid bot-like interaction speed.
    If RANDOM_WAIT=False, uses a deterministic min wait.
    """
    if not RANDOM_WAIT:
        time.sleep(min_ms / 1000.0)
        return
    time.sleep(random.uniform(min_ms / 1000.0, max_ms / 1000.0))


def load_candidate_profile() -> str:
    """
    Load candidate profile text from input/candidate_profile.txt.
    If missing, return a basic fallback profile.
    """
    if PROFILE_FILE.exists():
        return PROFILE_FILE.read_text(encoding="utf-8").strip()

    # Fallback should be enough for rough triage but you normally keep a full profile file.
    return (
        "You are evaluating jobs for a UK-based Senior/Lead Platform/DevOps/SRE engineer.\n"
        "Strong: AWS/GCP, Kubernetes, Terraform, CI/CD, observability.\n"
        "Prefers: hybrid/remote London.\n"
        "Avoid: pure frontend/mobile, junior roles.\n"
    )


def load_llm_cache() -> dict:
    """
    LLM cache format: job_id -> stored rubric response details.

    This avoids paying again for the same job and also stabilises scoring across re-runs.
    """
    if LLM_CACHE_FILE.exists():
        try:
            return json.loads(LLM_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_llm_cache(cache: dict) -> None:
    """Persist LLM cache to disk (pretty-printed JSON)."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LLM_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def save_seen(seen: Set[str]) -> None:
    """Persist set of processed job IDs so we skip them on subsequent runs."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


def load_seen() -> Set[str]:
    """Load set of processed job IDs from disk; return empty set if file missing/corrupt."""
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return set(str(x) for x in data)
        except Exception:
            pass
    return set()


def append_csv(job: JobResult):
    """Append a single JobResult row to the CSV file."""
    with OUT_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                f.name for f in JobResult.__dataclass_fields__.values()],
        )
        writer.writerow(asdict(job))


def append_markdown(path: Path, job: JobResult, jd_text: str, llm_details: dict = None):
    """
    Append a single job section to a markdown file.

    llm_details is optional and expected to include:
      - reasons
      - matched_skills
      - missing_skills
      - red_flags
      - questions_to_ask
    and may include rubric fields (category_scores, penalties etc) depending on the run.
    """
    llm_details = llm_details or {}

    def as_bullets(val, fallback="None"):
        """
        Convert various value types into markdown bullet lists.
        Supports:
        - empty -> "- fallback"
        - string -> "- string" or split by ';'
        - list -> "- item" per entry
        """
        if not val:
            return f"- {fallback}\n"
        if isinstance(val, str):
            parts = [p.strip() for p in val.split(";") if p.strip()]
            if len(parts) <= 1:
                return f"- {val.strip()}\n"
            return "".join([f"- {p}\n" for p in parts])
        if isinstance(val, list):
            cleaned = [str(x).strip() for x in val if str(x).strip()]
            if not cleaned:
                return f"- {fallback}\n"
            return "".join([f"- {x}\n" for x in cleaned])
        return f"- {str(val).strip() or fallback}\n"

    with path.open("a", encoding="utf-8") as f:
        # Basic job header and metadata
        f.write(
            f"## {job.title or 'Unknown title'} — {job.company or 'Unknown company'}\n")
        f.write(f"- **Decision:** {job.decision} (**{job.score:.1f}/10**)\n")
        f.write(f"- **Job ID:** {job.job_id}\n")
        f.write(f"- **URL:** {job.url}\n")
        f.write(f"- **Location:** {job.location}\n")
        f.write(f"- **Meta:** {job.meta_line}\n")
        f.write(f"- **Pills:** {job.pills}\n")
        f.write(f"- **Apply type:** {job.apply_type}\n")
        f.write(f"- **Promoted/Process:** {job.promoted_line}\n")
        f.write(f"- **Contacts:** {job.contacts or 'None'}\n")
        f.write(f"- **Debug:** {job.reasons}\n")
        f.write(f"- **Extracted:** {job.extracted_at}\n")

        # LLM reasoning block (compact; detailed rubric can be added to llm_details if desired)
        f.write("\n### LLM triage\n")
        reasons_val = llm_details.get("reasons") or "No reasons provided"
        f.write("**Why this rating**\n")
        f.write(as_bullets(reasons_val, fallback="No reasons provided"))

        f.write("\n**Matched skills**\n")
        f.write(as_bullets(llm_details.get(
            "matched_skills"), fallback="Not specified"))

        f.write("\n**Missing / gaps**\n")
        f.write(as_bullets(llm_details.get(
            "missing_skills"), fallback="Not specified"))

        f.write("\n**Red flags / risks**\n")
        f.write(as_bullets(llm_details.get("red_flags"), fallback="None noted"))

        f.write("\n**Questions to ask**\n")
        f.write(as_bullets(llm_details.get("questions_to_ask"), fallback="None"))

        # Full job description (as scraped)
        f.write("\n### Job description\n")
        f.write(jd_text or "")
        f.write("\n\n---\n\n")


# =============================================================================
# Email helpers (optional)
# =============================================================================

def smtp_config_ok() -> bool:
    """
    Validate SMTP environment configuration.
    Email is only attempted if all required fields are present.
    """
    if not SMTP_HOST or not SMTP_PORT or not SMTP_USER or not SMTP_PASS or not SMTP_TO:
        return False
    if not SMTP_FROM:
        return False
    return True


def should_send_email() -> bool:
    """
    Decide whether emailing is enabled:
    - EMAIL_ENABLED=0 => always disabled
    - EMAIL_ENABLED=1 => enabled if SMTP config ok
    - EMAIL_ENABLED unset => enabled if SMTP config ok
    """
    if EMAIL_ENABLED == "0":
        return False
    if EMAIL_ENABLED == "1":
        return smtp_config_ok()
    return smtp_config_ok()


def send_email(
    subject: str,
    text_body: str,
    html_body: str = None,
    apply_jobs_count: Optional[int] = None,
) -> None:
    """
    Send an email using STARTTLS and SMTP auth.

    This is used both for normal end-of-run “APPLY summary” emails and for failure alerts.
    Any send failure is logged but does not crash the script.
    """
    if not should_send_email():
        print("Email: skipped (SMTP not configured or disabled).")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = SMTP_TO

    # Always include a plain text part; optionally include HTML.
    msg.attach(MIMEText(text_body or "", "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [SMTP_TO], msg.as_string())
        if apply_jobs_count is None:
            print(f"Email: sent to {SMTP_TO}")
        else:
            print(f"Email: sent to {SMTP_TO} (APPLY jobs: {apply_jobs_count})")
    except Exception as e:
        print(f"Email: FAILED to send: {e}")


def build_apply_email(apply_jobs: List[Tuple[JobResult, dict, str]]) -> Tuple[str, str]:
    """
    Build both a plain text and HTML email body for “APPLY jobs” in this run.

    apply_jobs contains tuples:
      (JobResult, llm_data_dict, jd_snippet)
    """
    if not apply_jobs:
        text = "No APPLY jobs today."
        html = "<p><b>No APPLY jobs today.</b></p>"
        return text, html

    def score100_for(job: JobResult, llm_data: dict) -> float:
        score100 = llm_data.get("total_score_100", None) if isinstance(llm_data, dict) else None
        if score100 is None:
            return float(job.score) * 10.0
        return float(score100)

    apply_jobs_sorted = sorted(
        apply_jobs,
        key=lambda t: score100_for(t[0], t[1]),
        reverse=True,
    )

    # ---- Plain text format ----
    lines = [f"APPLY jobs for {TODAY_STR}:"]
    for job, llm_data, snippet in apply_jobs_sorted:
        lines.append(f"- {job.score:.1f}/10 — {job.title} @ {job.company}")
        lines.append(f"  {job.url}")
        if job.location:
            lines.append(f"  Location: {job.location}")
        if job.meta_line:
            lines.append(f"  Meta: {job.meta_line}")
        if snippet:
            lines.append(f"  Snippet: {snippet}")

        reasons = llm_data.get("reasons", [])
        if isinstance(reasons, list) and reasons:
            lines.append(
                f"  Reasons: {', '.join(str(x) for x in reasons[:3])}")

        lines.append("")

    text_body = "\n".join(lines).strip()

    # ---- HTML format ----
    items_html = []
    for job, llm_data, snippet in apply_jobs_sorted:
        reasons = llm_data.get("reasons", [])
        if isinstance(reasons, list):
            reasons_html = "".join(
                f"<li>{clean_text(str(r))}</li>" for r in reasons[:4] if str(r).strip()
            )
        else:
            reasons_html = f"<li>{clean_text(str(reasons))}</li>" if reasons else ""

        snippet_html = f"<p><i>{clean_text(snippet)}</i></p>" if snippet else ""

        items_html.append(
            f"""
            <div style="margin-bottom:16px;">
              <div><b>{job.score:.1f}/10</b> — <b>{clean_text(job.title)}</b> @ <b>{clean_text(job.company)}</b></div>
              <div><a href="{job.url}">Open on LinkedIn</a></div>
              <div style="color:#444;">{clean_text(job.location)}{(" · " + clean_text(job.meta_line)) if job.meta_line else ""}</div>
              {snippet_html}
              {"<ul>" + reasons_html + "</ul>" if reasons_html else ""}
            </div>
            """
        )

    html_body = f"""
    <html><body>
      <h3>APPLY jobs for {TODAY_STR}</h3>
      {''.join(items_html)}
      <hr/>
      <p>Shortlist file: {OUT_MD_APPLY.resolve()}</p>
    </body></html>
    """.strip()

    return text_body, html_body


# =============================================================================
# LLM triage (scoring + caching)
# =============================================================================

def llm_triage(job_id: str, summary: Dict[str, str], jd_text: str) -> Tuple[float, str, str, dict]:
    """
    Score and classify a job using an LLM + a deterministic rubric, with caching.

    Returns:
      (score_0_to_10, decision APPLY|MAYBE|REJECT, reasons_text, llm_details_dict)

    Key behaviour:
    - If job_id is found in llm_cache.json, do NOT call the model again.
    - The decision is derived locally based on:
        - auto_reject_flags
        - total_score_100 thresholds: APPLY >= 75, MAYBE 55..74, else REJECT
    - For backward compatibility, we can derive score100 from older payload keys.
    """
    cache = load_llm_cache()

    def compute_decision(score100: float, auto_flags: dict) -> str:
        """
        Compute APPLY/MAYBE/REJECT deterministically from numeric score and auto flags.
        This prevents inconsistencies if the model labels decisions incorrectly.
        """
        auto_flags = auto_flags or {}
        if auto_flags.get("on_site_only") or auto_flags.get("commute_over_90"):
            return "REJECT"
        if score100 >= 75:
            return "APPLY"
        if score100 >= 55:
            return "MAYBE"
        return "REJECT"

    def derive_scores(payload: dict) -> Tuple[float, float, dict]:
        """
        Derive score100/score10 from any of:
        - total_score_100 (preferred)
        - score_10
        - legacy score (0..10) -> converted to 0..100

        Also returns auto_reject_flags.
        """
        auto_flags = payload.get("auto_reject_flags", {}) or {}
        score100 = payload.get("total_score_100", None)

        if score100 is None:
            score10 = payload.get("score_10", None)
            if score10 is not None:
                score100 = float(score10) * 10.0

        if score100 is None:
            old_score10 = payload.get("score", 5.0)
            score100 = float(old_score10) * 10.0

        score100 = float(score100)
        score10 = round(score100 / 10.0, 1)
        return score100, score10, auto_flags

    # -------------------------------------------------------------------------
    # Cache hit: return cached score/decision/reasons without an API call
    # -------------------------------------------------------------------------
    if job_id in cache:
        c = cache[job_id]

        score100 = float(c.get("total_score_100", 50.0))
        score = round(score100 / 10.0, 1)

        auto = c.get("auto_reject_flags", {}) or {}
        decision = compute_decision(score100, auto)

        reasons_val = c.get("reasons", [])
        if isinstance(reasons_val, list):
            reasons_text = "; ".join(str(x).strip()
                                     for x in reasons_val if str(x).strip())
        else:
            reasons_text = str(reasons_val).strip()

        return score, decision, reasons_text, c

    # -------------------------------------------------------------------------
    # Cache miss: call the LLM using a strict JSON schema + rubric prompt
    # -------------------------------------------------------------------------
    profile = load_candidate_profile()
    jd_trim = (jd_text or "")[:LLM_MAX_JD_CHARS]

    # System prompt: keep the model “mechanical” and discourage narrative output.
    system = (
        "You are a scoring engine. Follow the rubric exactly. Output numeric subscores per category. "
        "No narrative. Be consistent."
    )

    # User payload: all context and the exact JSON requirements.
    user_payload = {
        "candidate_profile": profile,
        "scoring_rubric": {
            "thresholds_score_100": {"APPLY": 75, "MAYBE_MIN": 55},
            "category_max_points": {
                "role_fit_title_and_seniority": 15,
                "work_arrangement_hybrid_remote": 15,
                "commute": 5,
                "company_stage_stability": 5,
                "core_tech_match_gcp_k8s_terraform_jenkins": 30,
                "responsibilities_match_platform_automation_ops": 20,
                "culture_stress_oncall": 10,
            },
            "hard_penalties": {
                "heavy_oncall_or_24x7": -25,
                "startup_early_stage": -15,
                "no_iac_or_low_automation": -20,
                "staff_management_responsibilities": -15,
            },
            "auto_reject_flags": ["on_site_only", "commute_over_90"],
            "notes": [
                "Sum category_scores (must equal 0-100 before penalties).",
                "Apply penalties after summing categories.",
                "If any auto_reject_flag is true => decision must be REJECT regardless of score.",
                "score_10 must equal total_score_100 / 10 with 1 decimal place.",
                "Use fine-grained scoring within each category. Avoid coarse buckets like only 0/5/10.",
                "Prefer 1-point increments within the category max unless the evidence is truly binary.",
                "Apply staff_management_responsibilities penalty ONLY for line management duties (direct reports, performance reviews, hiring/firing, people management). Do NOT penalize for mentoring, coaching, or tech leadership without formal people management.",
            ],
        },
        "job_summary": {
            "title": summary.get("title", ""),
            "company": summary.get("company", ""),
            "location": summary.get("location", ""),
            "meta_line": summary.get("meta_line", ""),
            "pills": summary.get("pills", ""),
            "apply_type": summary.get("apply_type", ""),
            "promoted_line": summary.get("promoted_line", ""),
        },
        "job_description": jd_trim,
        "instructions": (
            "Return ONLY valid JSON with the following keys:\n"
            "- total_score_100 (number 0-100 after penalties)\n"
            "- score_10 (number with 1 decimal; score_10 = total_score_100 / 10)\n"
            "- decision (APPLY if total_score_100>=75; MAYBE if 55-74; REJECT if <55 OR any auto_reject flag true)\n"
            "- category_scores (object with exactly these keys; values 0..max_points; must sum to 100 BEFORE penalties):\n"
            "  role_fit_title_and_seniority,\n"
            "  work_arrangement_hybrid_remote,\n"
            "  commute,\n"
            "  company_stage_stability,\n"
            "  core_tech_match_gcp_k8s_terraform_jenkins,\n"
            "  responsibilities_match_platform_automation_ops,\n"
            "  culture_stress_oncall\n"
            "- penalties_applied (array of strings from: heavy_oncall_or_24x7, startup_early_stage, no_iac_or_low_automation, staff_management_responsibilities)\n"
            "- auto_reject_flags (object with boolean keys: on_site_only, commute_over_90)\n"
            "- reasons (array of short bullet strings, max 6)\n"
            "- questions_to_ask (array)\n"
            "- red_flags (array)\n"
            "- matched_skills (array)\n"
            "- missing_skills (array)\n\n"
            "Important:\n"
            "- Follow the rubric mechanically.\n"
            "- Base scores only on candidate_profile + job_summary + job_description.\n"
            "- Use the full range of points in each category; prefer 1-point increments.\n"
            "- Do not default to small fixed buckets (e.g., 0/5/10) unless evidence is binary.\n"
            "- Do not include any extra keys or any non-JSON text."
        ),
    }

    last_err = None
    for attempt in range(1 + LLM_RETRIES):
        try:
            # Use structured JSON output mode where supported by the OpenAI client.
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                temperature=LLM_TEMPERATURE,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(user_payload)},
                ],
                response_format={"type": "json_object"},
            )

            # The assistant content should be a JSON object (string), parse it.
            data = json.loads(resp.choices[0].message.content)

            # Convert model output to the score10/score100 we use everywhere.
            score100, score10, auto_flags = derive_scores(data)
            decision = compute_decision(score100, auto_flags)

            # Build a short “reasons” summary string for console output / debug.
            reasons_list = data.get("reasons", [])
            if isinstance(reasons_list, list):
                reasons_text = "; ".join(str(x).strip()
                                         for x in reasons_list if str(x).strip())
            else:
                reasons_text = str(reasons_list).strip()

            # Cache the entire output (plus computed decision) for stable re-runs.
            cache[job_id] = {
                "total_score_100": score100,
                "score_10": score10,
                "decision": decision,  # store computed decision, not model label
                "category_scores": data.get("category_scores", {}),
                "penalties_applied": data.get("penalties_applied", []),
                "auto_reject_flags": auto_flags,
                "reasons": data.get("reasons", []),
                "questions_to_ask": data.get("questions_to_ask", []),
                "red_flags": data.get("red_flags", []),
                "matched_skills": data.get("matched_skills", []),
                "missing_skills": data.get("missing_skills", []),
            }
            save_llm_cache(cache)

            return score10, decision, reasons_text, cache[job_id]

        except Exception as e:
            last_err = e
            if attempt < LLM_RETRIES:
                # Backoff between retries to tolerate transient API issues.
                time.sleep(LLM_RETRY_BACKOFF_S * (attempt + 1))
                continue

    # If we reach here, the model call failed on all attempts.
    print(f"LLM triage failed for job_id={job_id}: {last_err}")
    fallback = {
        "total_score_100": 50.0,
        "score_10": 5.0,
        "decision": "MAYBE",
        "reasons": ["LLM error - fallback decision"],
        "questions_to_ask": [],
        "red_flags": ["LLM call failed"],
        "matched_skills": [],
        "missing_skills": [],
        "category_scores": {},
        "penalties_applied": [],
        "auto_reject_flags": {},
    }
    cache[job_id] = fallback
    save_llm_cache(cache)
    return 5.0, "MAYBE", "LLM error - fallback decision", fallback


# =============================================================================
# DOM selectors & scraping helpers (LinkedIn)
# =============================================================================

# Candidate selectors for job cards in the left pane.
# LinkedIn changes markup frequently; we try several options.
CARD_SELECTORS = [
    "div.job-card-container[data-job-id]",
    "div.job-card-container--clickable[data-job-id]",
    "div.job-card-job-posting-card-wrapper[data-job-id]",
    "li[data-occludable-job-id]",
]

# Candidate selectors to find pagination UI.
PAGINATION_ROOT_SELECTORS = [
    "div.jobs-search-pagination",
    "nav[aria-label*='Page navigation']",
    "div.artdeco-pagination",
]

# Candidate selectors for “Next page” buttons.
NEXT_IN_PAGINATION_SELECTORS = [
    "button[aria-label='View next page']",
    "button.jobs-search-pagination__button--next",
    "button.artdeco-pagination__button--next",
]


def detect_card_selector(page) -> str:
    """
    Detect which job card selector matches the current LinkedIn layout.
    Returns the first selector that finds at least 1 element.
    """
    for sel in CARD_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                return sel
        except Exception:
            continue
    return ""


def wait_for_job_list(page, card_sel: str, timeout_ms=45000) -> None:
    """Wait until at least one job card exists in the DOM."""
    page.wait_for_selector(card_sel, timeout=timeout_ms)


def get_cards(page, card_sel: str):
    """Return a Playwright locator for all job cards."""
    return page.locator(card_sel)


def extract_job_id_from_card(page, card_locator, card_sel: str) -> str:
    """
    Extract a LinkedIn job ID from a job card element.
    LinkedIn uses different attributes depending on layout.
    """
    # Preferred attribute
    try:
        jid = (card_locator.get_attribute("data-job-id") or "").strip()
        if jid.isdigit():
            return jid
    except Exception:
        pass

    # Alternative attribute used on list items
    try:
        jid = (card_locator.get_attribute(
            "data-occludable-job-id") or "").strip()
        if jid.isdigit():
            return jid
    except Exception:
        pass

    # Fallback: walk up to nearest li[data-occludable-job-id]
    try:
        handle = card_locator.element_handle()
        if handle:
            jid = page.evaluate(
                """(el) => {
                    const li = el.closest('li[data-occludable-job-id]');
                    return li ? li.getAttribute('data-occludable-job-id') : '';
                }""",
                handle,
            )
            jid = (jid or "").strip()
            if jid.isdigit():
                return jid
    except Exception:
        pass

    return ""


def safe_click(card) -> bool:
    """
    Attempt to click a card. If direct click fails, try clicking the first anchor within it.
    Returns True on success, False on repeated failure.
    """
    try:
        card.click(timeout=6000)
        return True
    except Exception:
        try:
            card.locator("a").first.click(timeout=6000)
            return True
        except Exception:
            return False


def safe_scroll_into_view(card):
    """Try to scroll the card into view; ignore failures (non-fatal)."""
    try:
        card.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass


# =============================================================================
# Left list scrolling (job list virtualisation)
# =============================================================================

def find_real_left_scroll_container(page, card_sel: str):
    """
    Find the actual scrollable container that holds the job cards.

    LinkedIn uses nested divs with virtualised lists. The “real” scroll container
    is the first ancestor with overflow-y: auto/scroll and a scrollHeight > clientHeight.
    """
    cards = page.locator(card_sel)
    if cards.count() == 0:
        return None

    first = cards.nth(0).element_handle()
    if first is None:
        return None

    handle = page.evaluate_handle(
        """(el) => {
            function isScrollable(node) {
              if (!node) return false;
              const st = window.getComputedStyle(node);
              const oy = st.overflowY;
              const scrollable = (oy === 'auto' || oy === 'scroll');
              const tall = node.scrollHeight > (node.clientHeight + 20);
              return scrollable && tall;
            }
            let cur = el;
            while (cur) {
              if (isScrollable(cur)) return cur;
              cur = cur.parentElement;
            }
            return null;
        }""",
        first,
    )
    return handle.as_element() if handle else None


def scroll_metrics(container):
    """Return scrollTop, clientHeight, and scrollHeight for a container."""
    if container is None:
        return None
    try:
        return container.evaluate(
            "el => ({ top: el.scrollTop, height: el.clientHeight, scrollHeight: el.scrollHeight })"
        )
    except Exception:
        return None


def scroll_by(page, container, pixels: int):
    """
    Scroll the left list by a fixed pixel amount.
    If we can’t find a container, fallback to mouse wheel scrolling.
    """
    if container is None:
        try:
            page.mouse.wheel(0, pixels)
        except Exception:
            pass
        return

    try:
        container.evaluate(
            "(el, px) => { el.scrollTop = el.scrollTop + px; }", pixels)
    except Exception:
        try:
            page.mouse.wheel(0, pixels)
        except Exception:
            pass


def at_bottom(metrics) -> bool:
    """Heuristic: determine if we've scrolled close to the bottom."""
    if not metrics:
        return False
    return (metrics["top"] + metrics["height"]) >= (metrics["scrollHeight"] - 10)


def scroll_to_bottom_of_left_list(page, left_scroll):
    """Jump to the bottom of the left list to encourage pagination controls to show."""
    if left_scroll is None:
        try:
            page.keyboard.press("End")
        except Exception:
            pass
        return
    try:
        left_scroll.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    except Exception:
        try:
            page.keyboard.press("End")
        except Exception:
            pass


# =============================================================================
# Pagination support
# =============================================================================

def find_next_button(page):
    """
    Try to locate the “Next page” button by checking common pagination roots and button selectors.
    Returns a Playwright locator (button) or None.
    """
    for root_sel in PAGINATION_ROOT_SELECTORS:
        root = page.locator(root_sel).first
        try:
            if root.count() == 0 or not root.is_visible():
                continue
        except Exception:
            continue

        for sel in NEXT_IN_PAGINATION_SELECTORS:
            try:
                btn = root.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    return btn
            except Exception:
                continue

    return None


def is_next_disabled(btn) -> bool:
    """
    Determine if “Next” is disabled based on common attributes/classnames.
    """
    if btn is None:
        return True
    try:
        if not btn.is_visible():
            return True

        aria_disabled = (btn.get_attribute("aria-disabled") or "").lower()
        disabled_attr = btn.get_attribute("disabled")
        tabindex = (btn.get_attribute("tabindex") or "").strip()
        classes = (btn.get_attribute("class") or "").lower()

        if aria_disabled == "true" or disabled_attr is not None:
            return True
        if tabindex == "-1":
            return True
        if "disabled" in classes or "artdeco-button--disabled" in classes:
            return True
    except Exception:
        pass
    return False


def get_first_job_id(page, card_sel: str) -> str:
    """
    Used for “did the page change?” checks after clicking Next.
    Reads the first visible job ID from the list.
    """
    try:
        first = page.locator(card_sel).first
        if first.count() == 0:
            return ""
        jid = (first.get_attribute("data-job-id") or "").strip()
        if jid.isdigit():
            return jid
        return (first.get_attribute("data-occludable-job-id") or "").strip()
    except Exception:
        return ""


def go_to_next_page(page, left_scroll, card_sel: str, attempts: int = 3) -> bool:
    """
    Click “Next page” in LinkedIn job search results.

    Strategy:
    - Scroll to bottom of the left list (pagination sometimes only appears then).
    - Record a signature of the first few job IDs.
    - Click Next and wait until we detect that the list changed.
    - Retry a few times if needed.

    Returns:
      True if we believe the page advanced, otherwise False.
    """
    scroll_to_bottom_of_left_list(page, left_scroll)
    page.wait_for_timeout(400)

    before_first = get_first_job_id(page, card_sel)

    # Build a signature of first N job IDs; helps detect page advance even if first ID is missing.
    before_sig = ""
    try:
        ids = []
        cards = page.locator(card_sel)
        for i in range(min(cards.count(), 6)):
            ids.append(
                (
                    cards.nth(i).get_attribute("data-job-id")
                    or cards.nth(i).get_attribute("data-occludable-job-id")
                    or ""
                ).strip()
            )
        before_sig = "|".join([x for x in ids if x])
    except Exception:
        pass

    for n in range(1, attempts + 1):
        btn = find_next_button(page)
        if btn is None or is_next_disabled(btn):
            return False

        print(f"Paging: clicking Next... (attempt {n}/{attempts})")
        try:
            btn.click(timeout=8000)
        except Exception:
            page.wait_for_timeout(500)
            continue

        try:
            page.wait_for_timeout(800)
            page.wait_for_selector(card_sel, timeout=15000)

            def advanced_now():
                after_first = get_first_job_id(page, card_sel)
                if before_first and after_first and after_first != before_first:
                    return True
                try:
                    ids2 = []
                    cards2 = page.locator(card_sel)
                    for i in range(min(cards2.count(), 6)):
                        ids2.append(
                            (
                                cards2.nth(i).get_attribute("data-job-id")
                                or cards2.nth(i).get_attribute("data-occludable-job-id")
                                or ""
                            ).strip()
                        )
                    sig2 = "|".join([x for x in ids2 if x])
                    return bool(before_sig and sig2 and sig2 != before_sig)
                except Exception:
                    return False

            t0 = time.time()
            while time.time() - t0 < 8.0:
                if advanced_now():
                    return True
                page.wait_for_timeout(400)

            btn2 = find_next_button(page)
            if btn2 is None or is_next_disabled(btn2):
                print("Paging: Next became disabled/missing; likely last page.")
                return False

            print(
                "Paging: click happened but page-advance signals didn't change in time.")
        except Exception:
            print("Paging: error waiting for page advance.")

    return False


# =============================================================================
# Right pane extraction (job summary + description)
# =============================================================================

def try_get_text(page, selectors: List[str], min_len: int = 1) -> Tuple[str, str]:
    """
    Try multiple selectors and return the first non-empty text.
    Returns (text, selector_used). If nothing matches, returns ("", "").
    """
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                txt = clean_text(loc.inner_text())
                if len(txt) >= min_len:
                    return txt, sel
        except Exception:
            continue
    return "", ""


def expand_see_more_if_present(page) -> None:
    """
    Expand truncated job descriptions if a “See more” button exists.
    LinkedIn uses a few different button variants.
    """
    for sel in [
        "button.inline-show-more-text__button",
        "button:has-text('See more')",
        "button[aria-label*='See more']",
    ]:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click()
                page.wait_for_timeout(200)
                return
        except Exception:
            continue


def extract_summary_two_pane(page) -> Dict[str, str]:
    """
    Extract key job summary fields from LinkedIn’s two-pane job view.

    Output keys:
      title, company, location, meta_line, pills, promoted_line, apply_type, contacts
    """
    page.wait_for_timeout(250)

    # Title
    title, _ = try_get_text(
        page,
        [
            "div.jobs-unified-top-card__content--two-pane h1",
            "div.job-details-jobs-unified-top-card__job-title h1",
            "h1",
        ],
        min_len=2,
    )

    # Company name
    company, _ = try_get_text(
        page,
        [
            "div.jobs-unified-top-card__company-name a",
            "div.job-details-jobs-unified-top-card__company-name a",
            "span.job-details-jobs-unified-top-card__company-name a",
            "span.job-details-jobs-unified-top-card__company-name",
        ],
        min_len=2,
    )

    # Meta line often contains location + time posted + applicants etc
    meta_line, _ = try_get_text(
        page,
        [
            "div.jobs-unified-top-card__primary-description",
            "div.job-details-jobs-unified-top-card__primary-description-container",
        ],
        min_len=5,
    )

    # Location is sometimes separate; fallback to parsing meta_line.
    location, _ = try_get_text(
        page,
        [
            "span.job-details-jobs-unified-top-card__bullet",
            "span.topcard__flavor--bullet",
        ],
        min_len=2,
    )
    if not location and meta_line:
        location = meta_line.split("·")[0].strip()

    # Pills: “Remote”, “Hybrid”, “Full-time” etc, detected by scanning span texts.
    pills = []
    try:
        spans = page.locator(
            "div.jobs-unified-top-card__content--two-pane span").all_inner_texts()
        for s in spans[:260]:
            t = clean_text(s)
            if t.lower() in {
                "hybrid",
                "remote",
                "on-site",
                "onsite",
                "full-time",
                "part-time",
                "contract",
                "temporary",
            }:
                if t not in pills:
                    pills.append(t)
    except Exception:
        pass

    # Promoted / responses managed / etc
    promoted_line, _ = try_get_text(
        page,
        [
            "span:has-text('Promoted')",
            "span:has-text('Responses managed')",
            "div:has-text('Promoted by hirer')",
        ],
        min_len=10,
    )

    # Apply type is inferred from the presence of “Easy Apply” or “Apply” buttons.
    apply_type = "Unknown"
    btn_text, _ = try_get_text(
        page, ["button:has-text('Easy Apply')", "button:has-text('Apply')"], min_len=4)
    if "easy apply" in (btn_text or "").lower():
        apply_type = "Easy Apply"
    elif "apply" in (btn_text or "").lower():
        apply_type = "Apply"

    # Hiring team / reach out section (if present).
    contacts = ""
    for sel in [
        "section:has-text('Meet the hiring team')",
        "section:has-text('People you can reach out to')",
    ]:
        try:
            sec = page.locator(sel).first
            if sec.count() > 0 and sec.is_visible():
                contacts = clean_text(sec.inner_text())[:300]
                break
        except Exception:
            continue

    return {
        "title": title or "",
        "company": company or "",
        "location": location or "",
        "meta_line": meta_line or "",
        "pills": " | ".join(pills) if pills else "",
        "promoted_line": promoted_line or "",
        "apply_type": apply_type,
        "contacts": contacts or "",
    }


def extract_about_section_from_main(main_text: str) -> str:
    """
    Heuristic fallback: extract only the “About the job” section out of the <main> content.

    This is used if we can't locate the description with more targeted selectors.
    """
    if not main_text:
        return ""

    low = main_text.lower()
    start = low.find("about the job")
    if start == -1:
        for alt in ["about this job", "job description", "the role"]:
            start = low.find(alt)
            if start != -1:
                break
    if start == -1:
        return ""

    chunk = main_text[start: start + 20000]
    low_chunk = chunk.lower()

    # Stop at common “end-of-description” markers.
    for marker in [
        "about the company",
        "benefits",
        "people also viewed",
        "similar jobs",
        "more jobs",
        "report this job",
    ]:
        pos = low_chunk.find(marker)
        if pos != -1:
            chunk = chunk[:pos]
            break

    return clean_text(chunk)


def extract_full_description(page) -> Tuple[str, str]:
    """
    Extract the full job description text.

    Returns:
      (description_text, selector_used)

    Approach:
    1) Expand “See more” if present.
    2) Try common description selectors.
    3) If still missing, try larger containers.
    4) Finally, fallback to reading <main> text and carving out the description.
    """
    page.wait_for_timeout(350)
    expand_see_more_if_present(page)
    page.wait_for_timeout(250)

    desc, used = try_get_text(
        page,
        [
            "div.jobs-description__content",
            "div.jobs-description-content__text",
            "div.jobs-box__html-content",
            "article.jobs-description__container",
            "div#job-details",
            "section:has(h2:has-text('About the job'))",
        ],
        min_len=200,
    )

    if not desc:
        desc, used = try_get_text(
            page,
            [
                "div.jobs-search__job-details",
                "div.jobs-details__main-content",
                "div.scaffold-layout__detail",
            ],
            min_len=400,
        )

    # Last resort: capture main content and carve out relevant segment.
    if not desc:
        try:
            main_text = clean_text(page.locator(
                "main").inner_text(timeout=5000))
            carved = extract_about_section_from_main(main_text)
            if len(carved) >= 200:
                return carved, "main (carved)"
            return main_text[:20000], "main (raw)"
        except Exception:
            return "", "none"

    return desc, used


def normalise_url(url: str) -> str:
    """
    Normalise input URL:
    - If empty, return as-is
    - If already has http/https, return
    - Else prefix https://
    """
    url = (url or "").strip()
    if not url:
        return url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return "https://" + url.lstrip("/")


# =============================================================================
# Main runner
# =============================================================================

def main():
    """
    Main program entry.

    Responsibilities:
    - determine search URL
    - initialise output files / load caches
    - run Playwright session + iterate job cards
    - call LLM scoring per job (cached)
    - write outputs
    - send end-of-run email summary (optional)
    """
    search_url = sys.argv[1] if len(sys.argv) >= 2 else DEFAULT_SEARCH_URL
    search_url = normalise_url(search_url)

    ensure_outputs_exist()

    seen = load_seen()
    new_count = 0
    actions = 0

    # Collect APPLY jobs so we can email a summary at end-of-run.
    apply_jobs_for_email: List[Tuple[JobResult, dict, str]] = []

    with sync_playwright() as p:
        # Persistent context means cookies/session persist under ./pw_profile
        # so you don't have to log into LinkedIn each run.
        context = p.chromium.launch_persistent_context(
            user_data_dir="./pw_profile",
            headless=HEADLESS,
            args=["--start-maximized"],
        )

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(search_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        # If LinkedIn prompts for password, we consider ourselves logged out.
        # This script does not attempt to automate login.
        if page.locator("input[type='password']").count() > 0:
            print(
                "\nYou appear to be logged out. Log in in the browser window, then re-run.")
            print("Tip: Once logged in, the session is stored in ./pw_profile.")
            context.close()
            sys.exit(2)

        # Detect current job-card selector based on LinkedIn layout.
        card_sel = detect_card_selector(page)
        if not card_sel:
            print(
                "Could not detect job card selector. Open devtools and confirm job card markup.")
            context.close()
            sys.exit(3)

        print(f"Detected job card selector: {card_sel}")

        # Wait for list to be present before scanning.
        wait_for_job_list(page, card_sel, timeout_ms=45000)
        human_wait(400, 900)

        # Identify the true scrolling container for the left list (optional).
        left_scroll = find_real_left_scroll_container(page, card_sel)
        if left_scroll is None:
            print(
                "WARNING: Could not detect left scroll container; scrolling may be less reliable.")
        else:
            print("Detected left scroll container ✅")

        print("\nStarting auto-triage...")

        # prevents duplicates due to LinkedIn virtualization
        processed_this_run: Set[str] = set()
        stuck_scroll_attempts = 0
        stop_after_this = False  # used for FORCE_APPLY_FIRST_JOB mode

        # Main loop: keep processing until MAX_NEW jobs or safety cap hit.
        while new_count < MAX_NEW and actions < MAX_TOTAL_ACTIONS:
            actions += 1

            cards = get_cards(page, card_sel)
            count = cards.count()
            if count == 0:
                print("No job cards found (unexpected). Exiting.")
                break

            # Find the first job card we haven't processed before.
            target_index = None
            target_id = None

            # scan only first N cards to limit DOM work
            scan_n = min(count, 140)
            for i in range(scan_n):
                jid = extract_job_id_from_card(page, cards.nth(i), card_sel)
                if not jid:
                    continue
                if jid in seen or jid in processed_this_run:
                    continue
                target_index = i
                target_id = jid
                break

            # If we didn't find an unprocessed card, scroll/paginate to load new ones.
            if target_index is None:
                m = scroll_metrics(left_scroll) if left_scroll else None

                # If we're at the bottom, attempt to go to the next page.
                if m and at_bottom(m):
                    advanced = go_to_next_page(
                        page, left_scroll, card_sel, attempts=3)
                    if advanced:
                        human_wait(900, 1600)
                        # Layout can change after paging, so re-detect selectors/container.
                        card_sel = detect_card_selector(page) or card_sel
                        left_scroll = find_real_left_scroll_container(
                            page, card_sel)
                        processed_this_run.clear()
                        stuck_scroll_attempts = 0
                        continue

                    # If next doesn't exist or is disabled, we're done.
                    btn = find_next_button(page)
                    if btn is None or is_next_disabled(btn):
                        print("No more pages (Next disabled/missing). Stopping.")
                    else:
                        print(
                            "Paging failed (Next exists but page did not advance). Stopping.")
                    break

                # Otherwise, keep scrolling to load more cards.
                scroll_by(page, left_scroll, pixels=950)
                human_wait(500, 900)

                stuck_scroll_attempts += 1
                if stuck_scroll_attempts >= 10:
                    # If scrolling isn't yielding new cards, attempt pagination as a fallback.
                    advanced = go_to_next_page(
                        page, left_scroll, card_sel, attempts=2)
                    if advanced:
                        human_wait(900, 1600)
                        card_sel = detect_card_selector(page) or card_sel
                        left_scroll = find_real_left_scroll_container(
                            page, card_sel)
                        processed_this_run.clear()
                        stuck_scroll_attempts = 0
                        continue
                    print(
                        "Could not find new jobs after multiple scroll attempts. Stopping.")
                    break

                continue

            # We found a new target job card.
            stuck_scroll_attempts = 0

            card = cards.nth(target_index)
            safe_scroll_into_view(card)
            human_wait(150, 350)

            # Click the card to load the right pane details.
            if not safe_click(card):
                # Try a small scroll and retry clicking.
                scroll_by(page, left_scroll, pixels=450)
                human_wait(250, 650)
                safe_scroll_into_view(card)
                if not safe_click(card):
                    print(
                        f"[{actions}] Could not click card jid={target_id} (skipping).")
                    processed_this_run.add(target_id)
                    continue

            # Wait for the right pane to update.
            human_wait(650, 1200)

            # Extract fields used for both reporting and scoring.
            summary = extract_summary_two_pane(page)
            jd_text, jd_sel = extract_full_description(page)

            # Score job via cached LLM triage.
            score, decision, reasons_text, llm_data = llm_triage(
                target_id, summary, jd_text)

            # Test mode: force first job to APPLY then stop.
            if os.getenv("FORCE_APPLY_FIRST_JOB", "0") == "1" and new_count == 0:
                decision = "APPLY"
                score = max(score, 8.0)
                stop_after_this = True

            # Debug reason includes which selectors were used for scraping.
            debug_reason = f"jd_sel={jd_sel}; card_sel={card_sel}"

            # Persist a stable record for this job.
            job = JobResult(
                job_id=target_id,
                url=canonical_job_url(target_id),
                title=summary.get("title") or "Unknown",
                company=summary.get("company") or "Unknown",
                location=summary.get("location") or "Unknown",
                meta_line=summary.get("meta_line") or "",
                pills=summary.get("pills") or "",
                promoted_line=summary.get("promoted_line") or "",
                apply_type=summary.get("apply_type") or "Unknown",
                contacts=summary.get("contacts") or "",
                jd_length=len(jd_text or ""),
                score=score,
                decision=decision,
                reasons=debug_reason,
                extracted_at=datetime.now().isoformat(timespec="seconds"),
            )

            # Write to full report + CSV.
            append_markdown(OUT_MD_ALL, job, jd_text, llm_details=llm_data)
            append_csv(job)

            # Write to daily shortlist if APPLY, and add to email queue.
            if decision == "APPLY":
                append_markdown(OUT_MD_APPLY, job, jd_text,
                                llm_details=llm_data)

                snippet = clean_text((jd_text or "")[:320])
                apply_jobs_for_email.append((job, llm_data or {}, snippet))

            # Mark this job as processed.
            seen.add(target_id)
            processed_this_run.add(target_id)
            new_count += 1

            # Compute 0..100 score for display (prefers llm_data total_score_100).
            score100 = None
            try:
                score100 = float((llm_data or {}).get("total_score_100"))
            except Exception:
                score100 = score * 10.0

            # Console status line.
            line = (
                f"[{new_count}/{MAX_NEW}] {decision} {score:.1f}/10"
                f" ({score100:.1f}/100) — {job.title} @ {job.company}"
            )
            print(colourise(line, colour_for_decision(decision)))

            if stop_after_this:
                print(
                    "FORCE_APPLY_FIRST_JOB=1: stopping after first processed job (test mode).")
                break

        # Persist “seen” cache and close browser context.
        save_seen(seen)
        context.close()

    # Summary of artifacts created/updated.
    print("\nDone.")
    print(f"- New jobs processed: {new_count}")
    print(f"- Full report: {OUT_MD_ALL.resolve()}")
    print(f"- Daily shortlist:  {OUT_MD_APPLY.resolve()}")
    print(f"- CSV:        {OUT_CSV.resolve()}")
    print(f"- Seen cache: {SEEN_FILE.resolve()}")
    print(f"- LLM cache:  {LLM_CACHE_FILE.resolve()}")

    # End-of-run email summary (optional; depends on SMTP env vars).
    text_body, html_body = build_apply_email(apply_jobs_for_email)
    subject = f"{EMAIL_SUBJECT_PREFIX} Apply list {TODAY_STR} ({len(apply_jobs_for_email)})"
    send_email(subject, text_body, html_body, apply_jobs_count=len(apply_jobs_for_email))


# =============================================================================
# Entrypoint + failure email
# =============================================================================

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Best-effort: send a failure email (if SMTP configured).
        err = f"{type(e).__name__}: {e}"
        print(f"\nFATAL: {err}")
        subject = f"{EMAIL_SUBJECT_PREFIX} FAILED {TODAY_STR}"
        body = f"Run failed.\n\n{err}\n"
        send_email(subject, body, f"<p><b>Run failed.</b></p><pre>{err}</pre>")
        raise
