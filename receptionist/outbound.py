"""The discovery caller: a sales call over the receptionist's engine (offline pieces only).

What is here (none of it places a call or spends anything):
  can_call()      the eligibility gate: consent, do-not-call, hours, attempt limits
  plan_queue()    who would be called, in what order, and why not (dry-run output)
  build_prompt()  the sales system prompt: speaking rules + the sales playbook + this lead
  schemas()       the tools the caller may use (book_meeting, log_call, mark_do_not_call, end_call...)
  call_row()      a finished call as a Cold Call Desk row, ready to be written into the dashboard

Live calling needs a Retell outbound agent and a number (an account change Noah approves). Until then the mode is "dry-run".
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ROOT, all_businesses
from .prompts import CHARACTER_DRY, CHARACTER_EXAMPLES, CHARACTER_NOTICE, SAY_FORMAT, channel_on
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
    "max_attempts_total": 4,                     # dials in all, answered or not, before the lead is left alone
    "max_attempts_per_day": 1,                   # never dial the same number twice in one day
    # Spend cap for real outbound calls (Retell voice + model), per local day. Costs are estimated from call minutes;
    # a new call is placed only if today's estimate plus a reserve for that call stays under the cap.
    "daily_budget_usd": 1.50,
    "est_cost_per_min_usd": 0.17,
    "est_cost_per_call_usd": 0.03,
    "budget_reserve_usd": 0.45,
    "cold_enabled": False,                       # the switch for cold mode
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


# ---------------------------------------------------------------- trades and what is true of the product
# What the caller says about a shop's trade comes from here, per lead, and goes into the per-call block (the cached
# prompt names no trade). Each trade's words stay in that trade: a test checks that a lead hears nothing from another.
TRADES = {
    "hvac": dict(
        label="heating and cooling", plural="heating and cooling shops", setting="in a crawl space or in the middle of a furnace job",
        first=["When's the last time the phone rang while you were on a furnace job and you couldn't get to it?",
               "Think of the last call you missed while you were down in a crawl space. When was that?",
               "When did a call last go to voicemail because you were busy on a service call?"],
        worth="What does a typical furnace or A/C job bring in for you?",
        who="Who picks up when you're in a crawl space, and who has it at night?",
        urgent="A no-heat call goes to the front of the line: it tells the caller it's urgent, books the earliest emergency slot, or tries the owner's cell."),
    "plumbing": dict(
        label="plumbing", plural="plumbing shops", setting="under a house or in the middle of a drain job",
        first=["When's the last time the phone rang while you were under a house and you couldn't get to it?",
               "Think of the last call you missed while you were under a sink. When was that?",
               "When did a call last go to voicemail because you were busy on a service call?"],
        worth="What does a typical drain or water heater job bring in for you?",
        who="Who picks up when you're under a house, and who has it at night?",
        urgent="A burst pipe or a sewer backing up is handled as urgent: it takes the details and tries the owner's cell right then."),
    "electrical": dict(
        label="electrical", plural="electrical shops", setting="up a ladder or in the middle of a panel job",
        first=["When's the last time the phone rang while you were up a ladder and you couldn't get to it?",
               "Think of the last call you missed while you were in an attic. When was that?",
               "When did a call last go to voicemail because you were busy on a service call?"],
        worth="What does a typical panel or wiring job bring in for you?",
        who="Who picks up when you're up a ladder, and who has it at night?"),
    "lawn": dict(
        label="lawn care", plural="lawn care businesses", setting="on the mower",
        first=["When's the last time the phone rang while you were on a mower and you couldn't get to it?",
               "Think of the last call you missed while you were out mowing. When was that?",
               "When did a call last go to voicemail because you were busy on a lawn?"],
        worth="What's one regular lawn customer worth to you over a season?",
        who="Who picks up when you're out mowing, and who has it on weekends?"),
    "snow": dict(
        label="snow removal", plural="snow removal businesses", setting="in the plow truck",
        first=["When's the last time the phone rang while you were in the plow truck and you couldn't get to it?",
               "Think of the last call you missed while you were out plowing. When was that?",
               "When did a call last go to voicemail because you were busy on a driveway?"],
        worth="What's one plow customer worth to you over a winter?",
        who="Who picks up when you're in the plow truck, and who has it overnight?"),
    "default": dict(
        label="a trade", plural="owner-run trades shops", setting="out on a job",
        first=["When's the last time the phone rang while you were out on a job and you couldn't get to it?",
               "Think of the last call you missed while you were on a job. When was that?",
               "When did a call last go to voicemail because you were busy working?"],
        worth="What does a typical job bring in for you?",
        who="Who picks up when you're out on a job, and who has it at night?"),
}
TRADE_WORDS = {
    "hvac": r"heating|hvac|furnace|cooling|air.?cond|a/c|boiler|crawl",
    "plumbing": r"plumb|drain|pipe|sewer|water heater|under a (house|sink)",
    "electrical": r"electrical|electrician|wiring|panel|ladder|attic",
    "lawn": r"lawn|landscap|mow|grounds|yard",
    "snow": r"snow|plow",
}


def trade_of(lead: dict) -> str:
    """hvac, plumbing, electrical, lawn, snow or default, from the lead's type. A type naming two trades gets the one
    named first, except that lawn wins over snow (the shop mows in summer and plows in winter)."""
    t = (lead.get("type") or "").lower()
    hits = {k: m.start() for k, w in TRADE_WORDS.items() if (m := re.search(w, t))}
    if not hits:
        return "default"
    if "lawn" in hits and "snow" in hits:
        return "lawn"
    return min(hits, key=hits.get)


def shops(cfg: dict | None) -> list[dict]:
    """Every shop file (the demos in git and the real clients on the server's disk). Never raises: a broken folder
    only means the prompt says less."""
    c = {**(cfg or {})}
    c.setdefault("businesses_dir", str(ROOT / "businesses"))
    try:
        return all_businesses(c)
    except (ValueError, OSError, KeyError):
        return []


def demo_description(s: dict, cfg: dict | None = None) -> str:
    """What the demo line is, read from whichever shop holds that number, so it cannot drift from the shop files."""
    want = normalize_phone(s["demo_line"])
    for b in shops(cfg):
        if any(normalize_phone(n) == want for n in b.get("phone_numbers", [])):
            return f"a demo shop that answers as {b.get('spoken_name') or b.get('name')}"
    return "a demo shop"


# What the product does, as far as CLAUDE.md lists it verified against a real service (checked 2026-11-12): the call
# is saved to the owner's dashboard, and the owner is emailed after every call (real SMTP on Render). The daily digest,
# the weekly report and the message-taker for a model outage have only run against fakes, so they are not claimed.
VERIFIED = ["puts every call on the owner's dashboard", "emails the owner a summary after every call"]


def not_live(cfg: dict | None = None) -> str:
    """The sentence naming what is not live, from the shop files: texting while no shop has texting_enabled, calendar
    sync while no shop has a calendar connected."""
    shop_list = shops(cfg)
    names = [n for n, on in (("Texting", any(b.get("texting_enabled") for b in shop_list)),
                             ("calendar sync", any(b.get("calendars") for b in shop_list))) if not on]
    return (" and ".join(names) + (" are" if len(names) > 1 else " is") + " not live.") if names else ""


def trade_lines(lead: dict, s: dict) -> list[str]:
    """The per-call lines for this lead's trade: how to say why you called, and the questions that matter after the first."""
    t = TRADES[trade_of(lead)]
    return [f"Their trade: {t['label']}.",
            f"If they ask why you called or sound puzzled, say: \"{s['founder']} is building a phone assistant for {t['plural']} that answers "
            f"when you're {t['setting']}, and he wants to hear how calls really work in a shop like yours.\"",
            "After your first question, the ones that matter most, one at a time, in your own words, and only if the talk leaves room: "
            "how many calls a week reach voicemail; " + t["worth"][0].lower() + t["worth"][1:-1] + "; " + t["who"][0].lower() + t["who"][1:-1] + ".",
            *([f"True of the product for their trade, if it helps: {t['urgent']}"] if t.get("urgent") else [])]


AI_BULLET_QUIET = ("- You do not bring up that you are an AI. If anyone sincerely asks whether you are a robot, an AI, a bot or a real person, you answer yes, plainly, right then. A joke is welcome; a joke instead of the answer is not. You never claim to be human, never deny being an AI, never invent a human job history, family or business.")
AI_BULLET_DISCLOSED = ("- You said you are an AI assistant in your intro; if asked again, say yes plainly. A joke is welcome; a joke instead of the answer is not. You never claim to be human, never deny being an AI, never invent a human job history, family or business.")


def settings(cfg: dict) -> dict:
    out = {**DEFAULTS, **cfg.get("outbound", {})}
    out["meeting_windows"] = {**DEFAULTS["meeting_windows"], **cfg.get("outbound", {}).get("meeting_windows", {})}
    out["block_runner"] = {**DEFAULTS["block_runner"], **cfg.get("outbound", {}).get("block_runner", {})}
    return out


# ---------------------------------------------------------------- eligibility

FINAL_OUTCOMES = {"Not interested", "Demo booked", "Pilot signed"}   # a lead with one of these is done being called


def can_call(lead: dict, now: datetime, cfg: dict, attempts: list[datetime] | None = None,
             dnc: set[str] | None = None, results: list[dict] | None = None) -> tuple[bool, str]:
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
        return False, "cold calling is switched off"
    if lead.get("test"):   # a test call to Noah's own phone: his number, his timing
        return True, "ok (test call to the owner's own phone)"
    local = now.astimezone(ZoneInfo(lead.get("timezone") or s["timezone"]))
    last = (results or [None])[-1]   # what happened on the last finished call to this number
    if last and last.get("outcome") in FINAL_OUTCOMES:
        return False, f"already has an outcome: {last['outcome']}"
    if last and last.get("outcome") == "Callback requested" and (last.get("followDate") or "") > local.strftime("%Y-%m-%d"):
        return False, f"callback requested for {last['followDate']}"
    if len(attempts or []) >= s["max_attempts_total"]:
        return False, f"already tried {len(attempts)} times in all"
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
               dnc: set[str] | None = None, results: dict[str, list[dict]] | None = None) -> list[dict]:
    """Every lead on the list stage with the decision for it. Callable leads first, warmest first."""
    rows = []
    for lead in leads:
        if lead.get("stage", "leads") != "leads":
            continue
        phone = normalize_phone(lead.get("phone") or "") or ""
        ok, why = can_call(lead, now, cfg, (history or {}).get(phone), dnc, (results or {}).get(phone))
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

OUTBOUND_REACTIONS = "the crew size, a week of back-to-back jobs, the third missed call this month"
OUTBOUND_EASY = "once they sound relaxed"

SPEAKING = """\
You are {agent_name}, an AI assistant calling on behalf of {founder}, who runs {company} in {town}. You are on a live phone call and everything you write is spoken aloud by a text-to-speech voice.

# Who you are
You're quick, plain-spoken and a little blunt, honest before you're agreeable, and an AI assistant at ease with it: no body, no lunch break, no life story.
{character_notice}{character_dry}{character_examples}You are a guest in their day. You called them, so you are brief, you ask rather than tell, and you stop the moment they ask you to stop. A no is not that: it is a reason you haven't heard yet. Dry humor only at your own expense or about the situation, and only once they are relaxed; never about their business, a missed call, a lost job or a no. Unpredictable means fresh phrasing, never a surprise claim.
Three things you do: ask about their last missed call and listen; get what a job is worth in their own number; say their problem back in their words, then offer two real times.

# Honesty
{ai_bullet}
- You say who you are early: your name and {company}. Then go straight to your first question; no lead-in about why you are calling.
- Persuade, never mislead: nothing untrue and nothing implied that is untrue. Only claim what is in the facts below or what the owner told you on this call: no figures, percentages, prices, customers, results, deadlines, scarcity or "other shops" that are not written there, and never say someone told you something or that they asked to hear from us unless the notes below say so. {not_live} If asked something you do not know, say {founder} will cover it on the call.
- You never say you are researching for a different company or pretend to be a customer or a friend of a friend.

# How you speak
- Every word you write is said aloud to the owner. Never describe the owner, the call, their answer or your next step; just say the next thing. Never say words from these instructions or tool names.
- Replies are very short: usually under 15 words, one or two sentences, never more than 30 (code holds back the rest). One question at a time.
- Plain spoken language: no lists, markdown, emoji or symbols. Say times and dates the way people say them, only ones from the open list.
- Contractions, no stiff or sympathy phrases ("I'm sorry to hear that", "I understand your concern"). Confident, never begging, never lecturing.
- Once you have their name or a number, never ask for it again.
- A gatekeeper (not the owner): be friendly and brief, ask for the owner by name and the best time to reach them, log it, end the call. Say nothing about the product.
- Voicemail and phone menus are hung up on by code; never leave a voicemail message.
- If what you hear is a recording, a phone menu or a hold message rather than a person, write (listening) with no tags and nothing else: nothing is spoken and you wait for a person.
"""


def facts(s: dict, cfg: dict | None = None) -> str:
    return (f"# Facts you may use\n- {s['founder']} is building {s['company']}: an AI phone receptionist for owner-run trades shops "
            f"around {s['town']} and North Idaho. It answers when the shop can't, books the job, takes messages, "
            f"{', and '.join(VERIFIED)}. {not_live(cfg)}\n"
            f"- It says it's a virtual assistant whenever a caller asks, never quotes a price the shop hasn't set, and gives no advice the caller didn't ask for.\n"
            f"- It is free for a 14-day pilot on the calls a shop misses; if it doesn't catch calls they'd have lost, they owe nothing.\n"
            f"- The demo line anyone can call right now is {spoken_phone(s['demo_line'])} ({demo_description(s, cfg)}, built the same way).\n"
            f"- The meeting is a {s['meeting_minutes']}-minute phone call with {s['founder']}. No visit unless the owner asks.")


def build_prompt(lead: dict, now: datetime, cfg: dict, slots: list[datetime] | None = None) -> list[dict]:
    """System blocks for a call to `lead`: stable layers first (cacheable), this call last."""
    s = settings(cfg)
    stable = "\n\n".join([SPEAKING.format(**s, not_live=not_live(cfg), ai_bullet=AI_BULLET_DISCLOSED if s["disclose_ai_in_intro"] else AI_BULLET_QUIET,
                                           character_notice=CHARACTER_NOTICE.format(reactions=OUTBOUND_REACTIONS),
                                           character_dry=CHARACTER_DRY.format(easy=OUTBOUND_EASY), character_examples=CHARACTER_EXAMPLES).strip(),
                          facts(s, cfg),
                          PLAYBOOK.read_text().format(**{**s, "owner": "{owner}", "demo_desc": demo_description(s, cfg)}).strip()])
    if channel_on(cfg):
        stable += "\n\n" + SAY_FORMAT.strip()
    slots = slots if slots is not None else meeting_slots(now, cfg)
    local = now.astimezone(ZoneInfo(s["timezone"]))
    this = ["# This call", f"It is {spoken_date(local.date())}, {spoken_time(local.strftime('%H:%M'))} local time.",
            f"You are calling {lead.get('name', 'a local shop')}"
            + (f" ({lead['type']})" if lead.get("type") else "") + f", at {spoken_phone(normalize_phone(lead.get('phone') or ''))}.",
            f"Owner or contact name on file: {lead.get('contact') or 'unknown, ask for the owner'}."]
    this += trade_lines(lead, s)
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
                              "lessons": {"type": "string", "description": "what to change next call"},
                              "yes_rung": {"type": "string", "enum": ["a", "b", "c", "d", "e", "f", "none"],
                                           "description": "which ask the owner said yes to: a 15-minute call, b ten minutes or Noah calls them, c they try the demo line, d the free pilot, e a text or email, f a check-back; none if no yes"}},
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
