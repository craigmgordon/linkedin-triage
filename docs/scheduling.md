# Scheduling (macOS) — LinkedIn Job Triage

This project is designed to run unattended on macOS using **launchd** (preferred over cron on modern macOS).
The pattern is:

1. **Wake the Mac** on a schedule (Energy settings / `pmset` / “Schedule…”).
2. **Run the script** via a LaunchAgent at a defined time.
3. Optionally **let the Mac sleep again** after a window.

> Key constraint: LaunchAgents run in the **user GUI session** (`gui/<uid>`). If you are **not logged in**, a LaunchAgent may not have the UI/session access Playwright needs (Chromium launch, Keychain, etc.).  
> If you need “runs even when nobody is logged in”, you’ll likely need a different architecture (see “Unattended caveats” below).

### Environment variables via `.env` (recommended)

Your LaunchAgent runs non-interactively, so it will **not** inherit the same environment you see in an interactive Terminal session.  
The simplest approach is to keep secrets in a local `.env` file and have your wrapper script load it before invoking Python.

**Create a `.env` file (DO NOT commit it):**

```bash
# OpenAI
OPENAI_API_KEY="..."

# Email control (optional)
EMAIL_ENABLED="1"
EMAIL_SUBJECT_PREFIX="[LinkedIn Triage]"

# SMTP (required if you want email)
SMTP_HOST="smtp.example.com"
SMTP_PORT="587"
SMTP_USER="user@example.com"
SMTP_PASS="..."
SMTP_FROM="user@example.com"     # optional (defaults to SMTP_USER)
SMTP_TO="you@example.com"

# Optional test mode (don’t leave enabled long-term)
FORCE_APPLY_FIRST_JOB="0"
```

**Load it from your wrapper script (`run_linkedin_triage.sh`):**

```bash
set -euo pipefail

# Ensure we can find the right PATH/pyenv/etc (example only)
export PATH="$HOME/.pyenv/bin:$PATH"

# Load secrets for launchd runs
set -a
source "/Users/craig/dev/automated-linkedin/FINAL/.env"
set +a

# Run the job
cd "/Users/craig/dev/automated-linkedin/FINAL"
python3 "FINAL_linkedin_auto_triage_classic.py" >> "output/run.log" 2>&1
```

**Add `.env` to `.gitignore`:**

```gitignore
.env
```

Tip: you can also store the `.env` outside the repo (e.g. `~/.config/linkedin-triage/.env`) and reference that absolute path in the wrapper script.


---

## What you need

- Your script file, e.g.:
  - `FINAL_linkedin_auto_triage_classic.py`
- A wrapper shell script that:
  - activates your Python environment (e.g. `pyenv`, `venv`)
  - exports required environment variables (OpenAI + SMTP)
  - runs the Python script and logs output
- A LaunchAgent plist in:
  - `~/Library/LaunchAgents/com.craig.linkedintriage.plist`
- Log files configured in your plist, e.g.:
  - `.../output/launchd.out.log`
  - `.../output/launchd.err.log`

---

## 1) Create the wrapper runner script

Example: `~/bin/run_linkedin_triage.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

# --- adjust these paths for your machine ---
PROJECT_DIR="/Users/craig/dev/automated-linkedin/FINAL"
PYTHON="/Users/craig/.pyenv/versions/3.9.18/bin/python"

cd "$PROJECT_DIR"

# Optional: ensure predictable PATH for launchd environment
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

# --- required env vars ---
export OPENAI_API_KEY="YOUR_OPENAI_API_KEY"

# --- SMTP vars (only if you want email) ---
export SMTP_HOST="smtp.example.com"
export SMTP_PORT="587"
export SMTP_USER="user@example.com"
export SMTP_PASS="YOUR_SMTP_PASSWORD"
export SMTP_FROM="user@example.com"
export SMTP_TO="destination@example.com"
export EMAIL_SUBJECT_PREFIX="[LinkedIn Triage]"
# export EMAIL_ENABLED="1"   # optional; defaults to enabled when SMTP vars are present
# export EMAIL_ENABLED="0"   # to force-disable

# Optional: triage search URL override (or pass as argument)
# SEARCH_URL="https://www.linkedin.com/jobs/search/?..."
# "$PYTHON" FINAL_linkedin_auto_triage_classic.py "$SEARCH_URL"

"$PYTHON" FINAL_linkedin_auto_triage_classic.py
```

Make it executable:

```bash
chmod +x ~/bin/run_linkedin_triage.sh
```

Quick manual test (recommended):

```bash
~/bin/run_linkedin_triage.sh
echo $?
```

---

## 2) Create the LaunchAgent plist

File: `~/Library/LaunchAgents/com.craig.linkedintriage.plist`

**Example**: run Mon–Fri at 08:05

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.craig.linkedintriage</string>

    <key>ProgramArguments</key>
    <array>
      <string>/Users/craig/bin/run_linkedin_triage.sh</string>
    </array>

    <key>RunAtLoad</key>
    <false/>

    <!-- Logs -->
    <key>StandardOutPath</key>
    <string>/Users/craig/dev/automated-linkedin/FINAL/output/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/craig/dev/automated-linkedin/FINAL/output/launchd.err.log</string>

    <!-- Mon–Fri 08:05 -->
    <key>StartCalendarInterval</key>
    <array>
      <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>5</integer></dict>
      <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>5</integer></dict>
      <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>5</integer></dict>
      <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>5</integer></dict>
      <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>5</integer></dict>
    </array>
  </dict>
</plist>
```

Validate:

```bash
plutil -lint ~/Library/LaunchAgents/com.craig.linkedintriage.plist
```

Load it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.craig.linkedintriage.plist
launchctl enable gui/$(id -u)/com.craig.linkedintriage
```

Confirm it is loaded:

```bash
launchctl print gui/$(id -u)/com.craig.linkedintriage | head -n 80
```

---

## 3) Run it immediately (manual trigger)

```bash
launchctl kickstart -k gui/$(id -u)/com.craig.linkedintriage
```

Then inspect logs:

```bash
tail -n 200 /Users/craig/dev/automated-linkedin/FINAL/output/launchd.out.log
tail -n 200 /Users/craig/dev/automated-linkedin/FINAL/output/launchd.err.log
```

---

## 4) “When will it run?”

From your plist:

- `StartCalendarInterval` with `Hour=8`, `Minute=5`, `Weekday=1..5`
- That means **08:05 local time** on **Mon–Fri**.

Launchd will show its next scheduled run in the system log (and sometimes in `launchctl print` output),
but the source of truth is the `StartCalendarInterval` entries.

---

## 5) Waking the Mac and going back to sleep

### Wake schedule (recommended)

Use macOS settings:

- **System Settings → Battery → Options / Schedule** (label varies by macOS version)
- Set a **Wake** schedule for Mon–Fri at e.g. **08:00**.

### Sleep schedule
macOS can schedule sleep the same way (if supported), or you can use `pmset`.

Example `pmset` commands (requires admin):

```bash
sudo pmset repeat wakeorpoweron MTWRF 08:00:00 sleep MTWRF 08:45:00
pmset -g sched
```

> Note: Apple Silicon + newer macOS versions sometimes interpret “sleep” schedules differently depending on power state and lid state.

---

## 6) Clearing / resetting the schedule

Unload/remove:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.craig.linkedintriage.plist 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.craig.linkedintriage.plist
```

Then recreate plist and re-bootstrap (as above).

---

## 7) Unattended caveats (important for Playwright)

Your script launches a real Chromium profile (`launch_persistent_context`).
That can fail in unattended scenarios for reasons like:

- no active GUI session (not logged in)
- keychain prompts / access restrictions
- the browser is still running from a previous session (lock/profile in use)
- background “Login Items” / background task controls
- waking into a partially-ready state (network not up; display server not ready)

If your **08:05** run fails but a manual run works later, it often indicates the machine wasn’t fully ready
at wake-time. Workarounds that keep *functionality* the same include:

- set LaunchAgent to run a few minutes after wake (e.g. wake 08:00, run 08:10)
- add a short “readiness” delay in the wrapper script (e.g. `sleep 60`)
- ensure Chrome is not left running and that the profile isn’t locked
- ensure the machine is actually logged into the user account at run time

(If you want, we can document a “known issues + mitigations” page separately.)

---

## 8) Troubleshooting checklist

### Verify the agent exists and is valid
```bash
plutil -lint ~/Library/LaunchAgents/com.craig.linkedintriage.plist
```

### Check that launchd has it loaded
```bash
launchctl print gui/$(id -u)/com.craig.linkedintriage
```

### Run on-demand
```bash
launchctl kickstart -k gui/$(id -u)/com.craig.linkedintriage
```

### View logs
```bash
tail -n 200 /Users/craig/dev/automated-linkedin/FINAL/output/launchd.out.log
tail -n 200 /Users/craig/dev/automated-linkedin/FINAL/output/launchd.err.log
```

### Show launchd-related system events around the run time
```bash
log show --style syslog --last 2h | egrep -i "linkedintriage|launchd|xpc|backgroundtask"
```

---

## Appendix: Why launchd over cron?

- cron has limited visibility into the user GUI environment
- launchd is macOS-native and integrates with power management, background task management, and logging
- launchd supports structured schedules (`StartCalendarInterval`) cleanly
