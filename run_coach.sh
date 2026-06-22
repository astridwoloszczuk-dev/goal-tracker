#!/bin/bash
# Astrid's twice-weekly executive-function coach (Mon/Thu). Opus → email.
cd "$HOME/Code/goal-tracker" || exit 1
MB="$HOME/Code/morning-briefing/.env"
export ANTHROPIC_API_KEY="$(grep '^ANTHROPIC_API_KEY=' "$MB" | cut -d= -f2-)"
export SMTP_PASSWORD="$(grep '^SMTP_PASSWORD=' "$MB" | cut -d= -f2-)"
export SMTP_FROM="$(grep '^SMTP_USER=' "$MB" | cut -d= -f2-)"
export COACHING_TO="astrid.woloszczuk@outlook.com"
# Do NOT export SUPABASE_* — golf data is in astrid-efficiency (garmin_sync's hardcoded
# default), not the family-apps project that morning-briefing/.env points at.
echo "=== $(date) ===" >> "$HOME/Code/goal-tracker/coach.log"
"$HOME/Code/goal-tracker/.venv/bin/python" coach.py "$@" 2>&1 | tee -a "$HOME/Code/goal-tracker/coach.log"
