#!/usr/bin/env python3
"""
nudge.py — mid-week WhatsApp nudges between the Sunday coach emails (Tue/Thu/Sat evening).

Reads coach_plan.json (Sunday's prescriptions) + this week's actuals, asks Opus whether ONE
short nudge is worth sending right now — a FIRM chase on a prescribed fix she skipped while its
weakness keeps biting (her example: still 3-putting Thursday having skipped the lag drill), a
SLIP worth catching (alcohol over 3/wk, a key session clearly missed), or a genuine WIN worth
reinforcing. Silence is the default. Dedupes within the week. Queues to Astrid's WhatsApp via
family-apps outbound_messages (whatsapp-server delivers).

  python3 nudge.py          # decide + (maybe) send
  python3 nudge.py --dry    # decide + print, never send
"""
import json, os, sys, urllib.request
from datetime import date
from pathlib import Path

import anthropic
import garmin_sync as gs

HERE       = Path(__file__).resolve().parent
DRY        = "--dry" in sys.argv
TODAY      = date.today()
ASTRID_LID = "89245662314662@lid"   # her WhatsApp LID (see whatsapp-delivery-fix)

def load_env(path):
    d = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); d[k.strip()] = v.strip()
    return d

def load_json(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default

def queue_whatsapp(text):
    env = load_env(Path.home() / "Code" / "todo-list" / "scripts" / ".env")   # family-apps
    url = env["SUPABASE_URL"].rstrip("/")
    key = env.get("SUPABASE_SERVICE_KEY") or env.get("SUPABASE_KEY")
    req = urllib.request.Request(
        f"{url}/rest/v1/outbound_messages", method="POST",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json", "Prefer": "return=minimal"},
        data=json.dumps({"to_number": ASTRID_LID, "message": text, "status": "pending"}).encode())
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status

NUDGE_SYSTEM = """You are Astrid's golf/fitness coach sending an OPTIONAL mid-week WhatsApp between your Sunday emails. She has explicitly asked you to be FIRM with her — she responds to a coach who pushes her, not one who softens to be liked.
SILENCE IS THE DEFAULT — most checks send nothing. Fire ONLY when there is one clearly worth-interrupting signal:
 • FIRM CHASE — she skipped a drill you prescribed AND the weakness it fixes is still happening (e.g. still 3-putting). Call it directly: name the avoidance, tell her to do it, no wriggle-room. A little cheeky is good; soft is not — you've earned the right to be blunt.
 • SLIP worth catching while fixable — alcohol over her limit, or a key planned session clearly missed. State it plainly, no hedging.
 • genuine WIN worth reinforcing — a good round, a streak, a hard session nailed. Warm, brief, specific.
Never nag about things merely scheduled. Never repeat a nudge already sent this week. Respect day-of-week on weekly targets.
Output is a WhatsApp: ONE message, under ~280 characters, your coach voice — a DECLARATION, not a suggestion. No email formatting, no greeting line."""

def main():
    ws    = gs.week_start_for(TODAY.isoformat())
    plan  = load_json(HERE / "coach_plan.json", {}) or {}
    active = plan.get("week_start") == ws

    tracker = gs.fetch_tracker_data(ws, TODAY.isoformat())
    actuals = gs.format_tracker_for_prompt(tracker, TODAY.isoformat(), ws)

    state = load_json(HERE / "nudge_state.json", {}) or {}
    if state.get("week_start") != ws:
        state = {"week_start": ws, "fired": []}
    fired = state.get("fired", [])

    prompt = f"""Today is {TODAY.isoformat()} ({TODAY.strftime('%A')}), mid-week.

SUNDAY'S PRESCRIPTIONS (what you told her to do this week){'' if active else ' — STALE/none, ignore'}:
{json.dumps(plan.get('prescriptions', []), indent=2) if active else 'none'}
watch_alcohol: {plan.get('watch_alcohol') if active else 'n/a'}

WHAT SHE HAS ACTUALLY DONE SO FAR THIS WEEK:
{actuals}

NUDGES YOU ALREADY SENT THIS WEEK (do NOT repeat the same point):
{json.dumps(fired) if fired else 'none yet'}

Decide now. Output EXACTLY 'NONE' if nothing is worth sending right now, otherwise output ONLY the WhatsApp message text."""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-opus-4-8", max_tokens=2000,
        thinking={"type": "adaptive"}, system=NUDGE_SYSTEM,
        messages=[{"role": "user", "content": prompt}])
    out = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    u = msg.usage
    _in, _out = getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0
    _cost = _in * 5 / 1e6 + _out * 25 / 1e6
    try:
        (HERE / "cost.log").open("a").write(
            f"{TODAY.isoformat()}\tnudge\tin={_in}\tout={_out}\t${_cost:.4f}\n")
    except Exception:
        pass
    print(f"  [usage] nudge: in={_in} out={_out}  ≈ ${_cost:.4f}")

    if out.upper().startswith("NONE") or len(out) < 3:
        print("No nudge warranted."); return
    print("NUDGE:", out)
    if DRY:
        print("(--dry: not sent)"); return
    queue_whatsapp(out)
    fired.append(out[:90])
    state["fired"] = fired
    (HERE / "nudge_state.json").write_text(json.dumps(state, indent=2))
    print("Queued to WhatsApp.")

if __name__ == "__main__":
    main()
