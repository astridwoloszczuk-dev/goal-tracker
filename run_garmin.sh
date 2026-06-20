#!/bin/bash
# Daily Garmin readiness sync on James — deterministic, on-device, no Claude.
# Pulls overnight metrics via garth, derives per-discipline readiness, writes
# garmin_status.js, then commits+pushes so the Golf Tracker PWA updates live.
cd "$HOME/Code/goal-tracker" || exit 1
PY="$HOME/Code/goal-tracker/.venv/bin/python"
LOG="$HOME/Code/goal-tracker/garmin_sync.log"
echo "=== $(date) ===" >> "$LOG"

# The PWA also commits to this repo (activity_log via GitHub API) — rebase first
# so our push is never rejected. autostash protects any local script edits.
git pull --rebase --autostash >> "$LOG" 2>&1

"$PY" garmin_sync.py >> "$LOG" 2>&1

# Publish only the generated status file (leave script edits for manual commits)
if ! git diff --quiet garmin_status.js 2>/dev/null; then
  git add garmin_status.js
  git commit -m "garmin: readiness $(date +%F)" >> "$LOG" 2>&1
  git push >> "$LOG" 2>&1 && echo "pushed garmin_status.js" >> "$LOG"
fi
