#!/usr/bin/env python3
"""
garmin_sync.py
1. Fetches Garmin health metrics + activities → writes garmin_status.js (readiness)
2. Sends a daily coaching brief email (Mon–Sat) or full weekly coaching email (Sun)
   pulling habit/round data from Supabase.

GitHub Actions secrets needed:
  GARMIN_TOKENS, ANTHROPIC_API_KEY,
  SMTP_FROM, SMTP_PASSWORD, COACHING_TO
  SUPABASE_URL, SUPABASE_KEY  (or falls back to hardcoded public anon key)
"""

import json
import os
import smtplib
import tempfile
import urllib.request
from datetime import date, timedelta, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    from garminconnect import Garmin
except ImportError:
    print("ERROR: pip install garminconnect")
    exit(1)

try:
    import anthropic
except ImportError:
    print("ERROR: pip install anthropic")
    exit(1)

TODAY     = date.today().isoformat()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── AUTH ─────────────────────────────────────────────────────────────────────

def load_garmin_client() -> Garmin:
    token_json = os.environ.get("GARMIN_TOKENS")
    tmpdir = tempfile.mkdtemp()

    if token_json:
        # GitHub Actions path: restore token files from secret
        token_files = json.loads(token_json)
        for fname, content in token_files.items():
            with open(os.path.join(tmpdir, fname), "w", encoding="utf-8") as f:
                f.write(content)
        print("Authenticated via GARMIN_TOKENS secret.")
    else:
        # Local path: use ~/.garth
        garth_dir = os.path.expanduser("~/.garth")
        if not os.path.isdir(garth_dir):
            print("ERROR: No GARMIN_TOKENS env var and no ~/.garth directory.")
            print("Run garmin_token_setup.py first.")
            exit(1)
        tmpdir = garth_dir
        print("Authenticated via ~/.garth")

    # Load tokens from the tokenstore directory
    client = Garmin()
    client.login(tokenstore=tmpdir)
    return client


# ── FETCH ────────────────────────────────────────────────────────────────────

def safe_fetch(label, fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
        print(f"  ✓ {label}")
        return result
    except Exception as e:
        print(f"  ✗ {label}: {e}")
        return None


def fetch_health_metrics(client) -> dict:
    print("Fetching health metrics...")
    sleep    = safe_fetch("sleep",        client.get_sleep_data,        TODAY)
    hrv      = safe_fetch("hrv",          client.get_hrv_data,          TODAY)
    body_bat = safe_fetch("body_battery", client.get_body_battery,      TODAY)
    stats    = safe_fetch("daily_stats",  client.get_stats,             TODAY)

    m = {}

    try: m["sleep_score"]  = sleep["dailySleepDTO"]["sleepScores"]["overall"]["value"]
    except: m["sleep_score"] = None

    try:
        secs = sleep["dailySleepDTO"]["sleepTimeSeconds"]
        m["sleep_hours"] = round(secs / 3600, 1)
    except: m["sleep_hours"] = None

    try:
        s = hrv["hrvSummary"]
        m["hrv_status"]        = s["status"]
        m["hrv_last_night"]    = s["lastNightAvg"]
        m["hrv_baseline_low"]  = s["baseline"]["lowUpper"]
        m["hrv_baseline_high"] = s["baseline"]["balancedUpper"]
    except:
        m["hrv_status"] = m["hrv_last_night"] = m["hrv_baseline_low"] = m["hrv_baseline_high"] = None

    try:
        vals = [v[1] for v in body_bat[0]["bodyBatteryValuesArray"] if v[1] is not None]
        m["body_battery"] = max(vals) if vals else None
    except: m["body_battery"] = None

    try: m["resting_hr"]  = stats["restingHeartRate"]
    except: m["resting_hr"] = None

    try: m["avg_stress"]  = stats["averageStressLevel"]
    except: m["avg_stress"] = None

    return m


def fetch_recent_activities(client, days=7) -> list:
    """Fetch Garmin activities from the last N days."""
    print(f"Fetching last {days} days of activities...")
    try:
        acts = client.get_activities(0, 20)  # fetch more, filter by date
    except Exception as e:
        print(f"  ✗ activities: {e}")
        return []

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    result = []
    for a in acts:
        start = a.get("startTimeLocal", "")[:10]
        if start < cutoff:
            continue
        result.append({
            "date":         start,
            "name":         a.get("activityName", ""),
            "type":         a.get("activityType", {}).get("typeKey", "unknown"),
            "duration_min": round(a.get("duration", 0) / 60),
            "distance_km":  round(a.get("distance", 0) / 1000, 1) if a.get("distance") else None,
            "avg_hr":       a.get("averageHR"),
            "max_hr":       a.get("maxHR"),
        })

    result.sort(key=lambda x: x["date"])
    print(f"  ✓ {len(result)} activities in last {days} days")
    return result


def load_manual_activities(days=7) -> list:
    """Read activity_log.json — manually logged activities (weights, golf)."""
    log_path = os.path.join(SCRIPT_DIR, "activity_log.json")
    if not os.path.exists(log_path):
        return []
    with open(log_path, encoding="utf-8") as f:
        entries = json.load(f)
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return [e for e in entries if e.get("date", "") >= cutoff]


# ── CLAUDE API ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a sports science assistant helping an athlete plan their training.
You understand training load, recovery, and periodisation. You give honest, specific advice
rather than generic caution. Zone 2 running at correct intensity (65-75% max HR) is restorative
and can be done frequently — do NOT treat it like a hard session. VO2 Max intervals carry the
highest neuromuscular cost (48-72h recovery needed). The athlete doesn't always wear their
Garmin watch during weights sessions or golf, so these are provided separately. Golf (any format)
counts as active recovery — it's always a net positive. The athlete is frustrated by Garmin
mislabelling their sessions (Zone 2 called 'exhausting', VO2 Max called 'light effort') —
your analysis should make more sense than Garmin's."""

def format_activities_for_prompt(garmin_acts: list, manual_acts: list) -> str:
    lines = []

    if garmin_acts:
        lines.append("GARMIN-TRACKED ACTIVITIES (last 7 days):")
        for a in garmin_acts:
            hr_info = ""
            if a["avg_hr"]: hr_info += f", avg HR {a['avg_hr']} bpm"
            if a["max_hr"]: hr_info += f", max HR {a['max_hr']} bpm"
            dist_info = f", {a['distance_km']} km" if a["distance_km"] else ""
            lines.append(f"  {a['date']}: {a['type']} — {a['duration_min']} min{dist_info}{hr_info} ('{a['name']}')")
    else:
        lines.append("GARMIN-TRACKED ACTIVITIES: none in last 7 days")

    lines.append("")
    if manual_acts:
        lines.append("MANUALLY LOGGED (not tracked by Garmin):")
        for a in manual_acts:
            lines.append(f"  {a['date']}: {a['activity']}")
    else:
        lines.append("MANUALLY LOGGED: none in last 7 days")

    return "\n".join(lines)


def call_claude(metrics: dict, garmin_acts: list, manual_acts: list) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("WARNING: ANTHROPIC_API_KEY not set — using fallback score only")
        return build_fallback_readiness(metrics)

    client = anthropic.Anthropic(api_key=api_key)

    hrv_info = ""
    if metrics.get("hrv_last_night") and metrics.get("hrv_baseline_low"):
        hrv_info = f"{metrics['hrv_last_night']} ms (baseline {metrics['hrv_baseline_low']}–{metrics['hrv_baseline_high']} ms, status: {metrics['hrv_status']})"
    else:
        hrv_info = str(metrics.get("hrv_status", "unknown"))

    health_block = f"""TODAY: {TODAY} ({date.today().strftime('%A')})

GARMIN HEALTH METRICS (from overnight):
  Sleep score:    {metrics.get('sleep_score', 'N/A')}/100
  Sleep duration: {metrics.get('sleep_hours', 'N/A')} hours
  HRV:            {hrv_info}
  Body Battery:   {metrics.get('body_battery', 'N/A')}/100  (peak after sleep)
  Resting HR:     {metrics.get('resting_hr', 'N/A')} bpm
  Avg stress:     {metrics.get('avg_stress', 'N/A')}/100
"""

    activities_block = format_activities_for_prompt(garmin_acts, manual_acts)

    user_prompt = f"""{health_block}
{activities_block}

Based on all the above, provide a readiness analysis.

Session types to assess:
- Zone 2 (easy aerobic run, 65-75% max HR — low fatigue, restorative)
- Long Run (structural load, distance-based fatigue — needs 48-72h)
- Tempo (lactate threshold, moderate-high cost — needs 36-48h)
- VO2 Max (high-intensity intervals, highest cost — needs 48-72h, prioritise freshness)
- Weights (neuromuscular, 48h same muscle group — Garmin doesn't track these)
- Mobility (always fine — active recovery)
- Golf (active recovery, walking + low-intensity — always a net positive)

Consider: how recent was each session type? HRV vs baseline? Sleep quality? Body battery?
Be specific about WHY in each reason (e.g. "VO2 Max 2 days ago + low HRV = poor").

Respond with ONLY valid JSON, no markdown, no commentary:
{{
  "score": <integer 0-100>,
  "summary": "<2-3 sentences: overall readiness + top recommendation for today>",
  "sessions": {{
    "Zone 2":   {{"status": "good|caution|poor", "reason": "<concise>"}},
    "Long Run": {{"status": "good|caution|poor", "reason": "<concise>"}},
    "Tempo":    {{"status": "good|caution|poor", "reason": "<concise>"}},
    "VO2 Max":  {{"status": "good|caution|poor", "reason": "<concise>"}},
    "Weights":  {{"status": "good|caution|poor", "reason": "<concise>"}},
    "Mobility": {{"status": "good|caution|poor", "reason": "<concise>"}},
    "Golf":     {{"status": "good|caution|poor", "reason": "<concise>"}}
  }}
}}"""

    print("Calling Claude API...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = message.content[0].text.strip()
    # Strip markdown code fences if Claude adds them despite instructions
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    result = json.loads(raw)
    print(f"  ✓ Readiness score: {result.get('score')}")
    return result


def build_fallback_readiness(metrics: dict) -> dict:
    """Simple score when Claude API is unavailable."""
    score = 50
    bb = metrics.get("body_battery")
    ss = metrics.get("sleep_score")
    hrv_map = {"BALANCED": 10, "UNBALANCED": 0, "LOW": -10, "POOR": -15}

    if bb is not None: score += (bb - 50) * 0.6
    if ss is not None: score += (ss - 60) * 0.3
    if metrics.get("hrv_status") in hrv_map: score += hrv_map[metrics["hrv_status"]]
    score = max(0, min(100, round(score)))

    thresholds = {"VO2 Max": 75, "Tempo": 68, "Long Run": 62, "Zone 2": 40, "Weights": 55, "Mobility": 20, "Golf": 0}
    sessions = {}
    for name, thr in thresholds.items():
        if score >= thr:          st = "good"
        elif score >= thr - 15:   st = "caution"
        else:                     st = "poor"
        sessions[name] = {"status": st, "reason": "Based on recovery metrics (Claude API unavailable)"}

    return {"score": score, "summary": "Fallback score based on raw Garmin metrics.", "sessions": sessions}


# ── WRITE OUTPUT ─────────────────────────────────────────────────────────────

def write_output(metrics: dict, readiness: dict, brief: str = ""):
    out_path = os.path.join(SCRIPT_DIR, "garmin_status.js")
    payload = {
        "date":      TODAY,
        "metrics":   metrics,
        "readiness": readiness,
    }
    if brief:
        payload["brief"] = brief
    js = f"""// Auto-generated by garmin_sync.py on {TODAY}
// Do not edit manually — re-run garmin_sync.py to refresh
window.GARMIN = {json.dumps(payload, indent=2, ensure_ascii=False)};
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(js)
    print(f"\nWrote: {out_path}")


# ── SUPABASE ─────────────────────────────────────────────────────────────────

# Public anon key — same as in goal_tracker.html (read-only, safe)
SB_URL = os.environ.get("SUPABASE_URL", "https://ykbabwkyojlwculjmbrw.supabase.co")
SB_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InlrYmFid2t5b2psd2N1bGptYnJ3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY1MTg0NzMsImV4cCI6MjA5MjA5NDQ3M30.4lYnVcgv1o9OvXGMtrbjwKhXZ8Yybqln08aci_ZM9mI")

def sb_get(table: str, params: str = "") -> list:
    url = f"{SB_URL}/rest/v1/{table}?{params}"
    req = urllib.request.Request(url, headers={
        "apikey": SB_KEY,
        "Authorization": f"Bearer {SB_KEY}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ✗ Supabase {table}: {e}")
        return []


def fetch_tracker_data(week_start: str, week_end: str) -> dict:
    """Pull this week's habit, cardio/gym, golf sessions and recent rounds."""
    print("Fetching tracker data from Supabase...")

    habits  = sb_get("daily_habits",  f"date=gte.{week_start}&date=lte.{week_end}&select=*")
    cardgym = sb_get("cardio_gym",    f"date=gte.{week_start}&date=lte.{week_end}&select=*")
    sessions= sb_get("golf_sessions", f"date=gte.{week_start}&date=lte.{week_end}&select=*")
    # Last 6 rounds for trend context
    rounds  = sb_get("golf_rounds",   f"order=date.desc&limit=6&select=*")

    print(f"  ✓ habits:{len(habits)} cardgym:{len(cardgym)} golf_sessions:{len(sessions)} rounds:{len(rounds)}")
    return dict(habits=habits, cardgym=cardgym, sessions=sessions, rounds=rounds)


def format_tracker_for_prompt(data: dict, today_str: str, week_start: str) -> str:
    """Build a compact text block describing this week's tracking data."""
    from datetime import date as _date

    today_d  = _date.fromisoformat(today_str)
    start_d  = _date.fromisoformat(week_start)
    days_elapsed = (today_d - start_d).days + 1   # Mon=1 … Sun=7

    lines = [f"TRACKING DATA — week of {week_start} (today = day {days_elapsed}/7 = {today_d.strftime('%A')})"]
    lines.append(f"NOTE: For daily targets (protein, hydration, mirror putting, stretch) the max achievable by end of today is {days_elapsed}/7. Only flag as behind if CURRENT total < days_elapsed.")
    lines.append(f"IMPORTANT: Do NOT evaluate performance before 2026-04-20 — that was the first full tracking day.")
    lines.append("")

    # Habits
    mirror = protein = stretch = hydration = alcohol = 0
    for h in data["habits"]:
        if h.get("mirror", 0): mirror += 1
        if h.get("protein", 0): protein += 1
        if h.get("stretch", 0): stretch += 1
        if h.get("water_glasses", 0) >= 6: hydration += 1
        if h.get("alcohol", 0): alcohol += 1

    lines.append(f"DAILY HABITS (this week so far, max possible today = {days_elapsed}/7):")
    lines.append(f"  Mirror putting: {mirror}/{days_elapsed} (target 7/7)")
    lines.append(f"  Protein target: {protein}/{days_elapsed} (target 7/7)")
    lines.append(f"  Stretching:     {stretch}/{days_elapsed} (target 5/7)")
    lines.append(f"  Hydration:      {hydration}/{days_elapsed} (target 7/7)")
    lines.append(f"  Alcohol days:   {alcohol} this week (target ≤3)")
    lines.append("")

    # Cardio / Gym
    cardio_slots = {}   # slot_index → total taps this week
    gym_slots    = {}
    for row in data["cardgym"]:
        if row["type"] == "cardio":
            cardio_slots[row["slot_index"]] = cardio_slots.get(row["slot_index"], 0) + (row["value"] or 0)
        else:
            gym_slots[row["slot_index"]] = gym_slots.get(row["slot_index"], 0) + (row["value"] or 0)

    CARDIO_NAMES = ["Zone 2", "Long Run", "Tempo", "VO2 Max"]
    GYM_NAMES    = ["Weights #1", "Weights #2", "Mobility"]
    lines.append("CARDIO THIS WEEK (1=done, 2=extra):")
    for i, name in enumerate(CARDIO_NAMES):
        v = cardio_slots.get(i, 0)
        lines.append(f"  {name}: {'✓' if v >= 1 else '–'}{' (extra)' if v >= 2 else ''}")
    lines.append("GYM THIS WEEK:")
    weights_done = sum(1 for i in [0,1] if gym_slots.get(i,0) >= 1)
    mobility_done = gym_slots.get(2, 0) >= 1
    lines.append(f"  Weights sessions: {weights_done}/2 (target 2)")
    lines.append(f"  Mobility:         {'✓' if mobility_done else '–'} (target 1+)")
    lines.append("")

    # Golf sessions
    cats = {"round": "Rounds", "range": "Range", "sga": "SGA", "putt": "Putt/Chip"}
    sess_counts = {}
    for s in data["sessions"]:
        sess_counts[s["cat"]] = sess_counts.get(s["cat"], 0) + 1
    lines.append("GOLF SESSIONS THIS WEEK:")
    for cat, label in cats.items():
        lines.append(f"  {label}: {sess_counts.get(cat,0)}")
    lines.append("")

    # Recent rounds
    if data["rounds"]:
        lines.append("RECENT ROUNDS (last 6):")
        for r in data["rounds"]:
            hd = r.get("holes_data") or []
            played = [h for h in hd if h.get("par") not in (None,"") and h.get("score") not in (None,"")]
            n = len(played)
            if n:
                delta = sum(int(h["score"]) - int(h["par"]) for h in played)
                gir   = sum(1 for h in played if h.get("gir"))
                p3    = sum(1 for h in played if h.get("p3"))
                db    = sum(1 for h in played if int(h["score"]) >= int(h["par"])+2)
                fw_eligible = [h for h in played if int(h.get("par",4)) != 3]
                fw    = sum(1 for h in fw_eligible if h.get("fw"))
                lines.append(f"  {r['date']} {r.get('course','')} {'[Comp]' if r.get('comp') else ''}: "
                              f"{n} holes, {'+' if delta>0 else ''}{delta} par, "
                              f"GIR:{gir} FW:{fw}/{len(fw_eligible)} 3P:{p3} Dbl:{db}")
            else:
                lines.append(f"  {r['date']} — no hole data")
    else:
        lines.append("RECENT ROUNDS: none logged yet")

    return "\n".join(lines)


# ── COACHING EMAIL ────────────────────────────────────────────────────────────

GOALS_CONTEXT = """
ATHLETE GOALS & CONTEXT:
- COO, 3 teenage children, husband travels Mon–Thu. Genuine time constraints — acknowledge them.
- GOLF: Lower handicap 10→9 by Nov 2026. Key tournaments: NÖ Meisterschaften May 14-17, Staatsmeisterschaften May 22-25.
  Weekly golf targets: 27+ holes, ≥1 drill per category (Range/Score, Range/Drive, SGA, Putt+Chip), ≥1 game per category, mirror putting 5×/week.
- RUNNING: Beat 5k PB 24:30 & VO2 Max 45→48 by Nov 2026. Back to full training from April 13 2026 after injury.
  Target: 3 runs/week (Zone 2 ~7.5km, 1 long run, 1 tempo/interval), total ~20-25km.
- STRENGTH: Muscle mass. 2× weights/week with PT Martin + protein ≥90g/day.
- HABITS: Hydration 6 glasses/day, alcohol ≤3×/week, stretching/foam rolling 5×/week, mobility 1×/week.
TONE: Direct and honest. Push her. No sugarcoating. Acknowledge constraints but don't use them as excuses.
"""

COACHING_SYSTEM = """You are a direct, no-nonsense personal coach. You have full visibility of the athlete's
tracking data and training history. You give honest, specific feedback — not generic encouragement.
If something is behind, say so clearly. If something is good, acknowledge it briefly. You know about
her injury recovery (back to full training April 13 2026) and upcoming golf tournaments (May 14-17 and 22-25).
Keep responses exactly as requested in terms of length and format."""


def build_daily_brief(metrics: dict, tracker: dict, today_str: str, week_start: str, is_sunday: bool) -> str:
    """Ask Claude for a short daily coaching note (3-4 sentences)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "(ANTHROPIC_API_KEY not set — skipping coaching brief)"

    client = anthropic.Anthropic(api_key=api_key)
    tracker_text = format_tracker_for_prompt(tracker, today_str, week_start)

    readiness_line = ""
    if metrics:
        readiness_line = (f"Today's Garmin: sleep {metrics.get('sleep_score','?')}/100, "
                         f"HRV {metrics.get('hrv_last_night','?')}ms ({metrics.get('hrv_status','?')}), "
                         f"Body Battery {metrics.get('body_battery','?')}.")

    prompt = f"""{GOALS_CONTEXT}

{tracker_text}

{readiness_line}

Write a DAILY COACHING BRIEF for {today_str} ({date.fromisoformat(today_str).strftime('%A')}).
Format: 3-4 sentences maximum. No headers, no bullet points. Pure plain text.
Cover: (1) one thing that's going well or behind this week so far, (2) one specific action for today.
Be direct. Start with her name 'Astrid,'."""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=COACHING_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def build_weekly_review(metrics: dict, tracker: dict, today_str: str, week_start: str) -> str:
    """Ask Claude for a full weekly coaching email (Sunday only)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "(ANTHROPIC_API_KEY not set)"

    client = anthropic.Anthropic(api_key=api_key)
    tracker_text = format_tracker_for_prompt(tracker, today_str, week_start)

    prompt = f"""{GOALS_CONTEXT}

{tracker_text}

Garmin today: sleep {metrics.get('sleep_score','?')}/100, HRV {metrics.get('hrv_last_night','?')}ms ({metrics.get('hrv_status','?')}), Body Battery {metrics.get('body_battery','?')}.

Write a WEEKLY COACHING REVIEW for the week ending {today_str}.

Structure (plain text, use dashes not markdown):
1. WEEK SUMMARY (2-3 sentences: honest overall verdict)
2. GOLF (what went well, what didn't, specific practice priorities for next week)
3. FITNESS (running + weights, honest assessment)
4. HABITS (protein, hydration, mirror putting, alcohol — only flag what matters)
5. NEXT WEEK FOCUS (3 specific, actionable priorities in priority order)

Be direct, specific, and honest. Acknowledge constraints but don't use them as excuses.
If she missed targets without good reason, say so. She explicitly asked to be pushed."""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=COACHING_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def send_coaching_email(subject: str, body: str):
    """Send plain-text coaching email via Gmail SMTP."""
    smtp_from = os.environ.get("SMTP_FROM", "claude.w.lowndes@gmail.com")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    smtp_to   = os.environ.get("COACHING_TO",  "astrid.woloszczuk@outlook.com")

    if not smtp_pass:
        print("  ✗ SMTP_PASSWORD not set — skipping email")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_from
    msg["To"]      = smtp_to
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(smtp_from, smtp_pass)
            server.sendmail(smtp_from, smtp_to, msg.as_string())
        print(f"  ✓ Coaching email sent to {smtp_to}")
    except Exception as e:
        print(f"  ✗ Email send failed: {e}")


# ── MAIN ─────────────────────────────────────────────────────────────────────

def week_start_for(today_str: str) -> str:
    """Return ISO date of the Monday of the given date's week."""
    d = date.fromisoformat(today_str)
    return (d - timedelta(days=d.weekday())).isoformat()


def main():
    print(f"\nGarmin Sync — {TODAY}\n")

    garmin_client = load_garmin_client()

    metrics      = fetch_health_metrics(garmin_client)
    garmin_acts  = fetch_recent_activities(garmin_client, days=7)
    manual_acts  = load_manual_activities(days=7)

    print(f"\nManual activities (last 7 days): {len(manual_acts)}")
    for a in manual_acts:
        print(f"  {a['date']}: {a['activity']}")

    readiness = call_claude(metrics, garmin_acts, manual_acts)

    # ── Daily brief → written into garmin_status.js for the app ──
    wstart  = week_start_for(TODAY)
    tracker = fetch_tracker_data(wstart, TODAY)

    weekly_only = os.environ.get("WEEKLY_REVIEW", "").lower() == "true"

    if weekly_only:
        # Sunday evening run: send weekly email, don't overwrite garmin_status.js
        print("\nWeekly review run — building email...")
        body = build_weekly_review(metrics, tracker, TODAY, wstart)
        print(f"\n--- WEEKLY REVIEW ---\n{body}\n--- END ---\n")
        send_coaching_email(f"Weekly Coaching Review — w/e {TODAY}", body)
        print("\nDone.")
        return

    # Normal morning run: generate brief and embed in garmin_status.js
    print("\nBuilding daily brief...")
    brief = build_daily_brief(metrics, tracker, TODAY, wstart, False)
    print(f"\n--- DAILY BRIEF ---\n{brief}\n--- END ---\n")

    write_output(metrics, readiness, brief)

    print("\nDone.")


if __name__ == "__main__":
    main()
