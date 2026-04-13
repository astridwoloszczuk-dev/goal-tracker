#!/usr/bin/env python3
"""
garmin_sync.py
Fetches today's Garmin health metrics + last 7 days of activities,
reads manually-logged activities from activity_log.json,
calls Claude API for a smart readiness analysis, and writes garmin_status.js.

In GitHub Actions: uses GARMIN_TOKENS and ANTHROPIC_API_KEY env vars.
Locally (first-time setup): run garmin_token_setup.py instead.
"""

import json
import os
import tempfile
from datetime import date, timedelta, datetime

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
    if token_json:
        # GitHub Actions path: restore token files from secret
        token_files = json.loads(token_json)
        tmpdir = tempfile.mkdtemp()
        for fname, content in token_files.items():
            with open(os.path.join(tmpdir, fname), "w", encoding="utf-8") as f:
                f.write(content)
        client = Garmin()
        client.garth.load(tmpdir)
        print(f"Authenticated via GARMIN_TOKENS secret.")
    else:
        # Local path: use ~/.garth if it exists
        garth_dir = os.path.expanduser("~/.garth")
        if not os.path.isdir(garth_dir):
            print("ERROR: No GARMIN_TOKENS env var and no ~/.garth directory.")
            print("Run garmin_token_setup.py first.")
            exit(1)
        client = Garmin()
        client.garth.load(garth_dir)
        print(f"Authenticated via ~/.garth")
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
        max_tokens=600,
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

def write_output(metrics: dict, readiness: dict):
    out_path = os.path.join(SCRIPT_DIR, "garmin_status.js")
    payload = {
        "date":      TODAY,
        "metrics":   metrics,
        "readiness": readiness,
    }
    js = f"""// Auto-generated by garmin_sync.py on {TODAY}
// Do not edit manually — re-run garmin_sync.py to refresh
window.GARMIN = {json.dumps(payload, indent=2, ensure_ascii=False)};
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(js)
    print(f"\nWrote: {out_path}")


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\nGarmin Sync — {TODAY}\n")

    client = load_garmin_client()

    metrics      = fetch_health_metrics(client)
    garmin_acts  = fetch_recent_activities(client, days=7)
    manual_acts  = load_manual_activities(days=7)

    print(f"\nManual activities (last 7 days): {len(manual_acts)}")
    for a in manual_acts:
        print(f"  {a['date']}: {a['activity']}")

    readiness = call_claude(metrics, garmin_acts, manual_acts)
    write_output(metrics, readiness)

    print("\nDone.")


if __name__ == "__main__":
    main()
