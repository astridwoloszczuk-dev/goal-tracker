#!/usr/bin/env python3
"""
coach.py — Astrid's twice-weekly executive-function coach (Mon + Thu, on James).

Opus. NOT just golf. Pulls everything together:
  • goals.txt (her spec + the "good week" scorecard + tone)
  • this week's ACTUALS — Garmin readiness, habits (protein/water/alcohol/mirror/stretch),
    cardio/gym slots, golf practice + rounds (with her scorecard notes), real Garmin runs
  • her CALENDAR (this week + a 2-week look-ahead) with done / scheduled-pending / pushed state
    (done = ticked in the diary; pushed = the diary move-trail)
Output = a direct, constraint-aware EMAIL that:
  - credits what's done, just says "do it" for what's already booked (NO nagging to "get PT in"
    when it's already in the calendar), and only suggests/flags what's genuinely not-planned or stuck
  - nudges items she's been pushing for days/weeks
  - looks ahead: book a tournament (1–2 wk lead, ~1/week goal), schedule a 5k time-trial
  - keeps the scorecard-derived golf drills she finds useful

Run:
  ~/Code/goal-tracker/.venv/bin/python coach.py            # build + email
  ~/Code/goal-tracker/.venv/bin/python coach.py --dry      # build + print, do not send
"""
import json, os, re, sys, urllib.request, urllib.parse, urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path

import anthropic
import garmin_sync as gs   # reuse the tested data layer (sb_get, tracker fetch, email, garmin)

HERE       = Path(__file__).resolve().parent
BRIEF      = Path.home() / "Code" / "morning-briefing"
DIARY      = Path.home() / "Code" / "diary"
TOKEN_PATH = BRIEF / "ms_token.json"
TOKEN_URL  = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
SCOPE      = "Calendars.ReadWrite User.Read offline_access"
GRAPH      = "https://graph.microsoft.com/v1.0/me"
TZ         = "Europe/Vienna"
TODAY      = date.today()
DRY        = "--dry" in sys.argv

# session-type keyword matchers (mirror the diary Progress checklist)
TOURNAMENT_RE = re.compile(r"turnier|meisterschaft|tournament|championship|trophy|trophäe|pokal|\bcup\b|\bopen\b|\bmatch\b", re.I)
ROUND_RE      = re.compile(r"18\s*loch|18\s*holes", re.I)
RUN_RE        = re.compile(r"dauerlauf|langer\s*lauf|speedrun|long\s*run|tempo|time\s*trial|zeitlauf", re.I)


# ── MS Graph (calendar read) ─────────────────────────────────────────────────
def load_env(path):
    d = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); d[k.strip()] = v.strip()
    return d

def ms_access():
    cfg = load_env(BRIEF / ".env")
    tok = json.loads(TOKEN_PATH.read_text())
    data = urllib.parse.urlencode({
        "client_id": cfg["MS_CLIENT_ID"], "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token", "scope": SCOPE,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=20) as r:
        new = json.loads(r.read())
    if "refresh_token" not in new:
        new["refresh_token"] = tok["refresh_token"]
    TOKEN_PATH.write_text(json.dumps(new, indent=2))
    return new["access_token"]

def fetch_calendar(start, end):
    access = ms_access()
    qs = urllib.parse.urlencode({
        "startDateTime": f"{start}T00:00:00", "endDateTime": f"{end}T23:59:59",
        "$select": "id,subject,start,end,showAs,categories,isAllDay",
        "$orderby": "start/dateTime", "$top": "500",
    })
    req = urllib.request.Request(f"{GRAPH}/calendarView?{qs}",
                                 headers={"Authorization": f"Bearer {access}",
                                          "Prefer": f'outlook.timezone="{TZ}"'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("value", [])


# ── local diary state (done / pushed) ────────────────────────────────────────
def load_json(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return {}

def read_readiness():
    try:
        txt = (HERE / "garmin_status.js").read_text()
        i, j = txt.find("{"), txt.rfind("}")
        return json.loads(txt[i:j + 1])
    except Exception:
        return None


# ── format the calendar for the prompt ───────────────────────────────────────
def fmt_event_line(e, ticks, moves, now):
    allday = e.get("isAllDay")
    subj   = (e.get("subject") or "(no title)").strip()
    cat    = (e.get("categories") or [None])[0]
    show   = e.get("showAs")
    sdt    = e["start"]["dateTime"]
    day    = sdt[:10]
    tm     = "" if allday else sdt[11:16]
    tk     = ticks.get(e["id"]) or {}
    done   = bool(tk.get("done"))
    mv     = moves.get(e["id"]) or {}
    pushed_from = mv.get("from")
    try:
        end_dt = datetime.fromisoformat(e["end"]["dateTime"][:19])
    except Exception:
        end_dt = None
    past = (end_dt is not None and end_dt < now)
    if done:        status = "DONE ✓"
    elif past:      status = "MISSED (past, not ticked)"
    else:           status = "scheduled — still to come"
    bits = [f"{day} {tm}".strip(), f"[{cat or '—'}]", subj, f"→ {status}"]
    if show and show != "busy":
        bits.append(f"(showAs={show}: FYI/other-person)")
    if pushed_from and pushed_from != day:
        bits.append(f"⚠ PUSHED from {pushed_from} (originally planned then)")
    return "  " + "  ".join(bits)

def build_calendar_block(this_week, lookahead, ticks, moves, now):
    lines = ["THIS WEEK (Mon–Sun) — your committed time, with done/scheduled/missed/pushed status:"]
    for e in this_week:
        if e.get("showAs") in ("free",) and not (e.get("categories")):
            continue  # skip pure FYI noise
        lines.append(fmt_event_line(e, ticks, moves, now))
    lines.append("")
    lines.append("NEXT 14 DAYS (look-ahead — to spot what still needs booking):")
    tour = [e for e in lookahead if TOURNAMENT_RE.search(e.get("subject") or "")]
    runs = [e for e in lookahead if RUN_RE.search(e.get("subject") or "")]
    rnds = [e for e in lookahead if ROUND_RE.search(e.get("subject") or "")]
    for e in lookahead:
        lines.append(fmt_event_line(e, ticks, moves, now))
    lines.append("")
    lines.append(f"LOOK-AHEAD SIGNALS: golf tournaments/matches booked in next 14 days = {len(tour)}"
                 f" ({', '.join((e.get('subject') or '').strip()+' '+e['start']['dateTime'][:10] for e in tour) or 'NONE — she wants ~1/week and these need 1–2 weeks lead time'}).")
    lines.append(f"  18-hole rounds booked next 14 days = {len(rnds)}. Runs booked next 14 days = {len(runs)}.")
    return "\n".join(lines)


# ── the coaching call ────────────────────────────────────────────────────────
SYSTEM = """You are Astrid's personal performance coach and executive-function governor — modelled on the one coach who actually got her there: the one who took her golf handicap from 32 to 9 in two years by being firm, decisive, and flatly refusing her excuses. You coach the WHOLE athlete — golf, running, strength, mobility, daily habits — not just golf.

YOUR STANCE (this is what works for her — she has explicitly asked to be coached this way):
- FIRM, not soft. You DECIDE and you DECLARE — "Do X." not "you might want to consider X." She responds to a coach who tells her; softness reads to her as you not taking her seriously.
- NO WRIGGLE-ROOM on what she controls. When she rationalises her way out of something you prescribed ("too hard, must be wrong, I changed it to this") — hold the line: getting good at exactly that is the point. Don't accept the excuse.
- EXPECT MORE of her. You push because you believe she can clear a higher bar; the firmness IS the respect. Never coddle.
- Hardest on AVOIDABLE misses (e.g. three-putts because she skipped the daily mirror-putting) — name them plainly, you've earned the right to be blunt. NEVER hard on things genuinely outside her control.
- Carrot AND stick, weighted to the STICK — that's the half missing from her life right now. Be the one who says "you said you have time. Now putting. It's raining? You play comps in the rain. Off you go."
- ALWAYS acknowledge her GENUINE constraints (COO, three teenagers, husband travels Mon–Thu, recent house move). Firmness is about effort and follow-through — never about pretending she has infinite time.

You write like the sharp human coach who knows her and will happily disappoint her in the short term to get her where she said she wants to go — never a generic, agreeable app."""

def build_prompt(goals, readiness, tracker_text, runs_text, cal_text, today_str, wstart):
    rdy = ""
    if readiness and readiness.get("readiness"):
        r = readiness["readiness"]; m = readiness.get("metrics", {})
        rdy = (f"GARMIN READINESS (latest): {r.get('score')}/100 — {r.get('summary','')}\n"
               f"  metrics: body battery {m.get('body_battery')}, sleep {m.get('sleep_score')}, "
               f"HRV {m.get('hrv_status')}, RHR {m.get('resting_hr')}")
    dname = date.fromisoformat(today_str).strftime("%A")
    return f"""{goals}

=========================================================
TODAY: {today_str} ({dname}) — Sunday evening. The week Mon {wstart}–today is now COMPLETE.
{rdy}

--- THIS WEEK'S TRACKED ACTUALS (the week just finished) ---
{tracker_text}

--- GARMIN RUNS (real cardio, last ~10 days; classify by HR not Garmin's label) ---
{runs_text}

--- CALENDAR (this week with done/missed status, then next ~2 weeks) ---
{cal_text}
=========================================================

Write Astrid's WEEKLY coaching email. It's Sunday evening — recap the week that just finished, then look ahead to next week. Plain text, start with "Astrid,".

WHAT MAKES YOU USEFUL, NOT ANNOYING:
- Do NOT narrate that routine items are "already scheduled / nothing to chase" — she KNOWS her PT, runs and mobility are in the calendar. Stay SILENT on booked routine items unless one was MISSED, is at risk, or had a quality problem (e.g. an "easy" run that ran too hot). Spending lines confirming booked PT/runs is the #1 way to lose her.
- The ONE scheduled-status worth stating is GOLF TOURNAMENTS in the look-ahead — those need 1–2 weeks' lead time and are easy to forget. So DO say whether one is booked for next week / the week after, and push her to book if not (she wants ~1/week).
- Be hard on AVOIDABLE misses (e.g. 3-putts after skipping mirror-putting), never on things outside her control. Acknowledge genuine constraints.
- PUSHED items (moved repeatedly / long ago) → call it plainly: she's avoiding it. No softening — she does it THIS week or she consciously bins it. There is no third option of letting it drift on the calendar forever.

STRUCTURE (tight, concrete):
1. THE WEEK — one honest verdict vs the "good week" scorecard (full week now assessable: golf holes/practice, 3 runs, 2 weights, mobility, habits). Credit real wins, name the real misses.
2. GOLF — what the rounds/notes reveal; the 2–3 SPECIFIC drills to prioritise NEXT week (derive from her scorecard weaknesses — concrete, e.g. "lag-putting ladder 20 min", never "practise putting"). Tournament look-ahead.
3. RUNNING — sessions vs 3/week; is a 5k time-trial scheduled or overdue?
4. STRENGTH / MOBILITY / HABITS — only what was actually OFF this week.
5. NEXT WEEK — SANITY-CHECK the upcoming calendar: (a) GAPS — key sessions not yet booked she should put in now (especially a tournament); (b) OVERLOADED / ill-sequenced days — e.g. a long/hard run stacked the SAME day as a tournament, or two hard sessions back-to-back — flag them NOW while there's time to re-jig.
6. DO THESE — the 2–3 highest-leverage moves for next week + anything to BOOK now.

Direct, decisive, FIRM — warm but never soft (soft loses her). Declarations, not suggestions. No # markdown; plain CAPS labels or dashes. Tight — she's busy.

After the email, on a new line, output a machine-readable plan for the mid-week nudge bot — EXACTLY this and nothing after the END marker:
<<<PLAN>>>
{{"prescriptions":[{{"what":"<short action you prescribed>","domain":"golf|running|strength|mobility|habit","detect":"<one lowercase stem that would appear in a logged session/note if she did it, e.g. putt, lag, knockdown, chip, mobility, tempo>","problem":"<the weakness it fixes, e.g. 3-putts>","firm":true}}],"watch_alcohol":true}}
<<<END>>>
Include only the 2–4 things you ACTUALLY prescribed in the email. "detect" must be a single lowercase word/stem."""

def log_usage(tag, usage):
    """Append real token usage + Opus 4.8 cost ($5/1M in, $25/1M out) to cost.log."""
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cost = inp * 5 / 1e6 + out * 25 / 1e6
    line = f"{datetime.now().isoformat(timespec='seconds')}\t{tag}\tin={inp}\tout={out}\t${cost:.4f}\n"
    try:
        (HERE / "cost.log").open("a").write(line)
    except Exception:
        pass
    print(f"  [usage] {tag}: in={inp} out={out}  ≈ ${cost:.4f}")

def call_opus(system, prompt):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    log_usage("coach", msg.usage)
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    today_str = TODAY.isoformat()
    wstart    = gs.week_start_for(today_str)
    now       = datetime.now()

    goals = (HERE / "goals.txt").read_text()
    readiness = read_readiness()

    # tracked actuals (Supabase) — reuse garmin_sync
    tracker = gs.fetch_tracker_data(wstart, today_str)
    tracker_text = gs.format_tracker_for_prompt(tracker, today_str, wstart)

    # real Garmin runs (best-effort)
    runs_text = "Garmin unavailable."
    try:
        gc = gs.load_garmin_client()
        acts = gs.fetch_recent_activities(gc, days=10)
        runs_text = gs.format_activities_for_prompt(acts, [])
    except Exception as e:
        runs_text = f"(Garmin fetch failed: {e})"

    # calendar: this week + 2-week look-ahead
    ticks = load_json(DIARY / "ticks.json")
    moves = load_json(DIARY / "moves.json")
    wend  = (date.fromisoformat(wstart) + timedelta(days=6)).isoformat()
    ahead_end = (TODAY + timedelta(days=14)).isoformat()
    try:
        this_week = fetch_calendar(wstart, wend)
        lookahead = fetch_calendar((TODAY + timedelta(days=1)).isoformat(), ahead_end)
        cal_text  = build_calendar_block(this_week, lookahead, ticks, moves, now)
    except Exception as e:
        cal_text = f"(Calendar fetch failed: {e})"

    prompt = build_prompt(goals, readiness, tracker_text, runs_text, cal_text, today_str, wstart)
    print("Calling Opus…")
    body = call_opus(SYSTEM, prompt)

    # Split the email prose from the machine-readable plan (for the mid-week nudge bot)
    email_body, plan = body, None
    if "<<<PLAN>>>" in body:
        pre, rest = body.split("<<<PLAN>>>", 1)
        email_body = pre.strip()
        try:
            plan = json.loads(rest.split("<<<END>>>")[0].strip())
        except Exception as e:
            print(f"(plan parse failed: {e})")
    if plan is not None:
        plan["week_start"] = wstart
        plan["generated"] = today_str
        (HERE / "coach_plan.json").write_text(json.dumps(plan, indent=2))
        print(f"Saved coach_plan.json ({len(plan.get('prescriptions', []))} prescriptions).")

    print("\n" + "=" * 60 + "\n" + email_body + "\n" + "=" * 60 + "\n")
    if DRY:
        print("(--dry: not sent)")
        return
    day_name = TODAY.strftime("%A")
    gs.send_coaching_email(f"Coach — {day_name} {today_str}", email_body)


if __name__ == "__main__":
    main()
