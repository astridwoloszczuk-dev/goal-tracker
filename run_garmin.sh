#!/bin/bash
# Daily Garmin readiness sync on James — deterministic, on-device, no Claude.
# Runs every 30 min through the morning (launchd); GARMIN_RETRY=1 makes it WAIT for the
# watch to sync last night's sleep before finalising, then write once (.garmin_done sentinel).
# Manual force refresh:  GARMIN_FORCE=1 bash run_garmin.sh
cd "$HOME/Code/goal-tracker" || exit 1
export GARMIN_RETRY=1
PY="$HOME/Code/goal-tracker/.venv/bin/python"
LOG="$HOME/Code/goal-tracker/garmin_sync.log"
echo "=== $(date) ===" >> "$LOG"

git pull --rebase --autostash >> "$LOG" 2>&1
"$PY" garmin_sync.py >> "$LOG" 2>&1

# Publish only the generated status file (leave script edits for manual commits)
if ! git diff --quiet garmin_status.js 2>/dev/null; then
  git add garmin_status.js
  git commit -m "garmin: readiness $(date +%F)" >> "$LOG" 2>&1
  git push >> "$LOG" 2>&1 && echo "pushed garmin_status.js" >> "$LOG"
fi
