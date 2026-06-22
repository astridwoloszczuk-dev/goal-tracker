#!/bin/bash
# Mid-week WhatsApp nudge check (Tue/Thu/Sat). Opus decides; silence is the default.
cd "$HOME/Code/goal-tracker" || exit 1
export ANTHROPIC_API_KEY="$(grep '^ANTHROPIC_API_KEY=' "$HOME/Code/morning-briefing/.env" | cut -d= -f2-)"
echo "=== $(date) ===" >> "$HOME/Code/goal-tracker/nudge.log"
"$HOME/Code/goal-tracker/.venv/bin/python" nudge.py "$@" >> "$HOME/Code/goal-tracker/nudge.log" 2>&1
