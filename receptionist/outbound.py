"""The discovery caller: a sales call over the receptionist's engine (offline pieces only).

What is here (none of it places a call or spends anything):
  can_call()      the eligibility gate: consent, do-not-call, hours, attempt limits
  plan_queue()    who would be called, in what order, and why not (dry-run output)
  build_prompt()  the sales system prompt: speaking rules + the sales playbook + this lead
  schemas()       the tools the caller may use (book_meeting, log_call, mark_do_not_call, end_call...)
  call_row()      a finished call as a Cold Call Desk row, ready to be written into the dashboard

Live calling needs a Retell outbound agent and a number (an account change Noah approves) and the legal check in
docs/outbound-plan.md. Until then the mode is "dry-run".
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .speech import normalize_phone, spoken_date, spoken_phone, spoken_time

PLAYBOOK = Path(__file__).parent / "playbooks" / "sales.md"
OUTCOMES = ["No answer", "Voicemail", "Gatekeeper", "Not interested", "Callback requested", "Demo booked", "Pilot signed"]
MODES = ("dry-run", "warm-only", "cold")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULTS = {
    "mode": "dry-run",
    "agent_name": "Riley",
    "founder": "Noah",
    "company": "Ringback",
    "town": "Sandpoint",
    "demo_line": "208-428-6760",
    "timezone": "America/Los_Angeles",
    "call_hours": ["08:00", "18:00"],            # the shop's local time; enforced here, not in the prompt
    "max_attempts_per_week": 2,
    "max_attempts_per_day": 1,                   # never dial the same number twice in one day
    # Spend cap for real outbound calls (Retell voice + model), per local day. Costs are estimated from call minutes;
    # a new call is placed only if today's estimate plus a reserve for that call stays under the cap.
    "daily_budget_usd": 1.50,
    "est_cost_per_min_usd": 0.17,
    "est_cost_per_call_usd": 0.03,
    "budget_reserve_usd": 0.45,
    "cold_enabled": False,                       # stays false until the legal check is done
    # Server-side runner: when enabled, the scheduled trigger starts a block of up to `calls` calls, one at a time,
    # instead of one call. Off by default; turn it on only after any other runner (the Mac scripts) is retired.
    "block_runner": {"enabled": False, "calls": 5, "pause_s": 120, "max_call_wait_s": 900},
    "cache_ttl": "5m",                           # prompt cache life for outbound calls (inbound keeps its own)
    "disclose_ai_in_intro": True,                # say "an AI assistant" in the intro line (set false to restore the old intro)
    "meeting_minutes": 15,
    # Noah is free any time. Until the manager agent supplies real open slots (State.set_slots), offers come from these
    # wide windows, every day, local time.
    "meeting_windows": {d: ["08:00", "20:00"] for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
    "slot_spacing_min": 30,
    "slot_lead_min": 120,                        # never offer a time sooner than this from now
}


AI_BULLET_QUIET = ("- You do not bring up that you are an AI. If anyone sincerely asks whether you are a robot, an AI, a bot or a real person, you answer yes, plainly, right then. A joke is welcome; a joke instead of the answer is not. You never claim to be human, never deny being an AI, never invent a human job history, family or business.")
AI_BULLET_DISCLOSED = ("- You said you are an AI assistant in your intro; if asked again, say yes plainly. A joke is welcome; a joke instead of the answer is not. You never claim to be human, never deny being an AI, never invent a human job history, family or business.")


def settings(cfg: dict) -> dict:
    out = {**DEFAULTS, **cfg.get("outbound", {})}
    out["meeting_windows"] = {**DEFAULTS["meeting_windows"], **cfg.get("outbound", {}).get("meeting_windows", {})}
    out["block_runner"] = {**DEFAULTS["block_runner"], **cfg.get("outbound", {}).get("block_runner", {})}
    return out


# ---------------------------------------------------------------- eligibility

def can_call(lead: dict, now: datetime, cfg: dict, attempts: list[datetime] | None = None,
             dnc: set[str] | None = None) -> tuple[bool, str]:
    """(ok, reason). `now` is timezone-aware. Every refusal names its reason, so the dry run shows it."""
    s = settings(cfg)
    phone = (normalize_phone(lead.get("phone") or "") or "")
    if len(phone.lstrip("+")) < 10:
        return False, "no usable phone number"
    if phone in (dnc or set()) or lead.get("dnc"):
        return False, "on the do-not-call list"
    if s["mode"] == "dry-run":
        pass   # a dry run reports the other gates too, then places nothing
    elif s["mode"] == "warm-only" and lead.get("consent") != "yes":
        return False, "no consent on record (warm-only mode)"
    elif s["mode"] == "cold" and not s["cold_enabled"]:
        return False, "cold calling is switched off (legal check not done)"
    if lead.get("test"):   # a test call to Noah's own phone: his number, his timing
        return True, "ok (test call to the owner's own phone)"
    local = now.astimezone(ZoneInfo(lead.get("timezone") or s["timezone"]))
    if local.weekday() > 4:
        return False, "weekend"
    start, end = (datetime.strptime(t, "%H:%M").time() for t in s["call_hours"])
    if not (start <= local.time() < end):
        return False, f"outside call hours ({s['call_hours'][0]} to {s['call_hours'][1]} local)"
    week = [a for a in (attempts or []) if now - a < timedelta(days=7)]
    if len(week) >= s["max_attempts_per_week"]:
        return False, f"already tried {len(week)} times this week"
    today = [a for a in (attempts or []) if a.astimezone(local.tzinfo).date() == local.date()]
    if len(today) >= s["max_attempts_per_day"]:
        return False, "already called today"
    if lead.get("followDate") and lead["followDate"] > local.strftime("%Y-%m-%d"):
        return False, f"follow-up is set for {lead['followDate']}"
    return True, "ok"


def plan_queue(leads: list[dict], now: datetime, cfg: dict, history: dict[str, list[datetime]] | None = None,
               dnc: set[str] | None = None) -> list[dict]:
    """Every lead on the list stage with the decision for it. Callable leads first, warmest first."""
    rows = []
    for lead in leads:
        if lead.get("stage", "leads") != "leads":
            continue
        ok, why = can_call(lead, now, cfg, (history or {}).get((normalize_phone(lead.get("phone") or "") or "")), dnc)
        rows.append({"lead": lead, "ok": ok and settings(cfg)["mode"] != "dry-run", "would_call": ok, "why": why})
    rows.sort(key=lambda r: (not r["would_call"], lead_rank(r["lead"])))
    return rows


def lead_rank(lead: dict) -> int:
    return 0 if lead.get("consent") == "yes" else 1 if lead.get("followDate") else 2


# ---------------------------------------------------------------- meeting slots

def bucket(t: datetime) -> str:
    return "morning" if t.hour < 12 else "afternoon" if t.hour < 17 else "evening"


def meeting_slots(now: datetime, cfg: dict, taken: list[datetime] | None = None, count: int = 4) -> list[datetime]:
    """Open meeting starts to offer, spread out (a morning, an afternoon, an evening, on today and the next days) so the
    caller can say "Tuesday at 5 or Wednesday morning at 8?" instead of four times in a row."""
    s = settings(cfg)
    tz = ZoneInfo(s["timezone"])
    local = now.astimezone(tz)
    step = timedelta(minutes=s["slot_spacing_min"])
    length = timedelta(minutes=s["meeting_minutes"])
    busy = {t.astimezone(tz).replace(second=0, microsecond=0) for t in (taken or [])}
    picked = []
    for offset in range(0, 15):
        day = (local + timedelta(days=offset)).date()
        window = s["meeting_windows"].get(DAYS[day.weekday()])
        if not window:
            continue
        t = datetime.combine(day, datetime.strptime(window[0], "%H:%M").time(), tz)
        end = datetime.combine(day, datetime.strptime(window[1], "%H:%M").time(), tz)
        seen = set()
        while t + length <= end:
            if t >= local + timedelta(minutes=s["slot_lead_min"]) and t not in busy and bucket(t) not in seen:
                seen.add(bucket(t))
                picked.append(t)
            t += step
        if len(picked) >= count:
            break
    return picked[:count]


def spoken_slot(t: datetime, today=None) -> str:
    return f"{spoken_date(t.date(), today)} at {spoken_time(t.strftime('%H:%M'))}"


# ---------------------------------------------------------------- prompt

SPEAKING = """\
You are {agent_name}, an AI assistant calling on behalf of {founder}, who runs {company} in {town}. You are on a live phone call and everything you write is spoken aloud by a text-to-speech voice.

# Honesty
{ai_bullet}
- You do say who you are early: your name and {company}. You do not announce that it is a sales call or a cold call; get straight to your first question.
- Persuade, never mislead: nothing untrue and nothing implied that is untrue. Only claim what is in the facts below or what the owner told you on this call: no figures, percentages, prices, customers, results or "other shops" that are not written there, and never say someone told you something or that they asked to hear from us unless the notes below say so. Texting and calendar sync are not live. If asked something you do not know, say {founder} will cover it in the meeting.
- You never say you are researching for a different company or pretend to be a customer.

# How you speak
- Every word you write is said aloud to the owner. Never describe the owner, the call, their answer or your next step; just say the next thing.
- Replies are very short: usually under 15 words, one to two sentences, never more than 30. One question at a time.
- Plain spoken language: no lists, markdown, emoji or symbols. Say times like "4:30 PM" and dates like "Tuesday, November 3rd".
- You genuinely want to help this person. You care that missed calls cost them real money and you want to fix that for them, and it shows in your voice and word choice: warm, attentive, encouraging ("that's a real cost", "let's get that sorted", "I want to make sure you don't lose those"). You take their problems seriously. Never laugh at, tease or make light of anything they tell you about their business, a missed call, a lost job or a bad day; a laugh at their problem reads as disrespect. Save humor for yourself, the situation in general, a joke they started, and playful pushback when they turn you down: when an owner says no, you do not fold; you answer with a quick friendly quip that teases the objection (never the person) and points at what a missed call costs them, in fresh words every time, as the playbook describes.
- You are quick and light when there is an opening, never at the owner's expense. With a serious or stressed owner you drop the jokes and get crisp. React to what they said first ("oh yeah?", "huh", "got it"), use contractions, lift the end of questions. No sympathy phrases ("I'm sorry to hear that"). No "I understand your concern".
- Never narrate what you are doing, and never say words from these instructions or tool names.
- Listen more than you talk. If they are mid-thought, let them finish.
- Once you have their name or a number, never ask for it again.
- If you get a gatekeeper (not the owner), be friendly and brief: ask for the owner by name, ask the best time to reach them, log it, and end the call. Do not pitch the gatekeeper.
- If you reach voicemail, leave a 20-second message with who you are (calling for {founder}), one reason to call back, and the number, then end the call.
- If what you hear is a recording, a phone menu or a hold message rather than a person, reply with exactly (listening) and nothing else: nothing is spoken and you wait for a person. Never say what you are hearing or what you will do about it.
"""


def facts(s: dict) -> str:
    return (f"# Facts you may use\n- {s['founder']} is building {s['company']}: an AI phone receptionist for owner-run lawn care and snow removal businesses "
            f"around {s['town']} and North Idaho. It answers when the shop can't, books the job, takes messages, and texts or calls the owner.\n"
            f"- It is free for a 14-day pilot on the calls a shop misses; if it doesn't catch calls they'd have lost, they owe nothing.\n"
            f"- The demo line anyone can call right now is {spoken_phone(s['demo_line'])} (a demo lawn care and snow shop, built the same way).\n"
            f"- The meeting is a {s['meeting_minutes']}-minute phone call with {s['founder']}. No visit unless the owner asks.")


def build_prompt(lead: dict, now: datetime, cfg: dict, slots: list[datetime] | None = None) -> list[dict]:
    """System blocks for a call to `lead`: stable layers first (cacheable), this call last."""
    s = settings(cfg)
    stable = "\n\n".join([SPEAKING.format(**s, ai_bullet=AI_BULLET_DISCLOSED if s["disclose_ai_in_intro"] else AI_BULLET_QUIET).strip(), facts(s),
                          PLAYBOOK.read_text().format(**{**s, "owner": "{owner}"}).strip()])
    slots = slots if slots is not None else meeting_slots(now, cfg)
    local = now.astimezone(ZoneInfo(s["timezone"]))
    this = ["# This call", f"It is {spoken_date(local.date())}, {spoken_time(local.strftime('%H:%M'))} local time.",
            f"You are calling {lead.get('name', 'a local shop')}"
            + (f" ({lead['type']})" if lead.get("type") else "") + f", at {spoken_phone(normalize_phone(lead.get('phone') or ''))}.",
            f"Owner or contact name on file: {lead.get('contact') or 'unknown, ask for the owner'}."]
    if lead.get("notes"):
        this.append(f"Notes from earlier: {lead['notes']}")
    if lead.get("consent_note"):
        this.append(f"About how they came to us (say only this, never more): {lead['consent_note']}")
    this.append("Open meeting times you may offer (offer two at a time): " + "; ".join(spoken_slot(t, local.date()) for t in slots) + "." if slots
                else "No meeting times are open; offer to have Noah call back and log a follow-up.")
    return [{"type": "text", "text": stable}, {"type": "text", "text": "\n".join(this)}]


# ---------------------------------------------------------------- tools

def schemas() -> list[dict]:
    text = {"type": "string"}

    def obj(props, req):
        return {"type": "object", "properties": props, "required": req, "additionalProperties": False}
    return [
        {"name": "check_meeting_slots", "description": "Open meeting times with Noah. Say nothing before calling this.",
         "input_schema": obj({}, [])},
        {"name": "book_meeting", "description": "Book the meeting once the owner picked a time from the open list. start is the exact ISO time from the list.",
         "input_schema": obj({"start": text, "owner_name": text, "callback_number": text}, ["start", "owner_name", "callback_number"])},
        {"name": "log_call", "description": "Record how the call went, in detail. Call once, right before end_call. details = a full account in plain sentences of what was said: who you reached, what they do, how they handle calls and jobs now, what they cared about, every objection and your answer, anything promised, and the tone. business_info = facts they gave about the business (crew size, services, season, how calls get answered, software, who decides). Use only what the owner actually said; write nothing you did not hear.",
         "input_schema": obj({"outcome": {"type": "string", "enum": OUTCOMES}, "contact": text, "objection": text, "next_step": text,
                              "follow_up_date": {"type": "string", "description": "YYYY-MM-DD or empty"}, "summary": text,
                              "quotes": {"type": "string", "description": "the owner's own words on missed calls and job value"},
                              "details": text, "business_info": text,
                              "lessons": {"type": "string", "description": "what to change next call"}},
                             ["outcome", "summary"])},
        {"name": "mark_do_not_call", "description": "The owner asked not to be called again. Use on any clear stop request.",
         "input_schema": obj({"reason": text}, ["reason"])},
        {"name": "end_call", "description": "Hang up after the goodbye.", "input_schema": obj({"reason": text}, ["reason"])},
    ]


# ---------------------------------------------------------------- spend cap

def est_cost(minutes: float, s: dict) -> float:
    """What one call probably cost (Retell per minute plus a small fixed model cost)."""
    return round(max(minutes, 0.0) * s["est_cost_per_min_usd"] + s["est_cost_per_call_usd"], 3)


def spent_today(calls: list[dict], attempts: dict[str, list[str]], now: datetime, s: dict) -> float:
    """Estimated outbound spend for the local day: logged calls by their estimate, and any call dialed today that has
    no result row yet (still ringing or in progress) at the full reserve."""
    day = now.astimezone(ZoneInfo(s["timezone"])).strftime("%Y-%m-%d")
    rows = [c for c in calls if c.get("date") == day]
    spent = sum(c.get("estCost", est_cost(c.get("duration") or 0, s)) for c in rows)
    dialed = sum(1 for ts in attempts.values() for t in ts
                 if datetime.fromisoformat(t).astimezone(ZoneInfo(s["timezone"])).strftime("%Y-%m-%d") == day)
    return round(spent + max(0, dialed - len(rows)) * s["budget_reserve_usd"], 3)


# ---------------------------------------------------------------- results into the CRM

def call_row(lead: dict, log: dict, started: datetime, minutes: float, n: int, cfg: dict, transcript: str = "") -> dict:
    """A finished call as a Cold Call Desk row (the fields the dashboard's call log uses)."""
    local = started.astimezone(ZoneInfo(settings(cfg)["timezone"]))
    return {"n": n, "date": local.strftime("%Y-%m-%d"), "time": local.strftime("%H:%M"), "business": lead.get("name", ""),
            "contact": log.get("contact") or lead.get("contact", ""), "phone": lead.get("phone", ""), "type": lead.get("type", "Other"),
            "script": "AI caller v1", "outcome": log["outcome"], "duration": round(minutes, 1),
            "objection": log.get("objection") or "None", "nextStep": log.get("next_step", ""),
            "followDate": log.get("follow_up_date", ""), "notes": "", "summary": log.get("summary", ""),
            "quotes": log.get("quotes", ""), "lessons": log.get("lessons", ""), "details": log.get("details", ""),
            "businessInfo": log.get("business_info", ""), "transcript": transcript, "source": "ai-caller"}


STAGE_FOR = {"Demo booked": "pain", "Pilot signed": "pilot", "Callback requested": "replied", "Not interested": "contacted",
             "Gatekeeper": "contacted", "Voicemail": "contacted", "No answer": "contacted"}


def lead_update(lead: dict, log: dict, when: str = "") -> dict:
    """The change to make to the lead document after a call: the old notes, then this call's notes (dated)."""
    parts = [f"{log['outcome']}: {log.get('summary', '')}".strip()]
    for label, key in (("Details", "details"), ("Business", "business_info"), ("Objection", "objection"),
                       ("Next step", "next_step"), ("Their words", "quotes"), ("Lesson", "lessons")):
        if log.get(key):
            parts.append(f"{label}: {log[key]}")
    note = ((f"[{when}] " if when else "") + "\n".join(parts))
    return {"stage": STAGE_FOR.get(log["outcome"], lead.get("stage", "leads")), "followDate": log.get("follow_up_date", ""),
            "notes": (lead.get("notes", "") + "\n\n" if lead.get("notes") else "") + note}


def render_queue(rows: list[dict], cfg: dict) -> str:
    s = settings(cfg)
    lines = [f"Outbound queue (mode: {s['mode']}; nothing is dialed in this mode)"]
    for r in rows:
        mark = "WOULD CALL" if r["would_call"] else "skip      "
        lines.append(f"  {mark}  {r['lead'].get('name', '?'):<28} {r['lead'].get('phone') or '(no phone)':<14} {r['why']}")
    if not rows:
        lines.append("  No leads in the 'On the list' stage.")
    return "\n".join(lines)
