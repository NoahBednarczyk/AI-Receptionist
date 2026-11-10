"""The discovery caller on a live call: its session, tools, state file and the Retell calls that start it.

Nothing here runs unless Noah switches it on. A call is placed only by `place_call()`, which refuses unless
outbound.mode is not "dry-run", OUTBOUND_LIVE=1 is set, and the lead passes outbound.can_call().
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
import statistics
from pathlib import Path
from zoneinfo import ZoneInfo

from . import callreview, outbound, tools
from .agent import CallSession, Turn, cache_control, thinking_param, transcript_text
from .live import OUTBOUND_STILL_WORKING
from .speech import normalize_phone, spoken_phone
from .tools import ToolError

log = logging.getLogger("receptionist")

# The opener is only a short "is this the owner?" so the person answers first. Who is calling comes in the next reply
# (the model says it, see INTRO_NOTE); INTRO is the fixed fallback if noise cuts the exchange short.
GREETINGS = ["Hi, is this {who}?", "Hello, is this {who}?", "Hey there, am I speaking with {who}?"]
INTRO = ["Hi, I'm {agent} with {company} here in {town}.",
         "It's {agent} with {company} in {town}."]
INTRO_AI = ["Hi, I'm {agent}, an AI assistant calling for {founder} at {company} in {town}.",
            "It's {agent}, an AI assistant calling for {founder} at {company} in {town}."]


def trade_of(lead: dict) -> str:
    t = (lead.get("type") or "").lower()
    if any(w in t for w in ("snow", "plow")) and not any(w in t for w in ("lawn", "landscap", "mow")):
        return "snow"
    return "lawn" if any(w in t for w in ("lawn", "landscap", "mow", "yard", "grounds", "snow", "plow")) else "default"


def opener_parts(lead: dict, s: dict, rng=random) -> tuple[str, str]:
    """(greeting, intro): the greeting is all that is said when the person first answers; the intro is the fallback
    for who is calling, used only if noise cuts in before the model's first reply."""
    owner = (lead.get("contact") or "").strip().split(" ")[0]
    who = owner or f"the owner of {lead.get('name') or 'the shop'}"
    fill = dict(agent=s["agent_name"], company=s["company"], town=s["town"], founder=s["founder"])
    return rng.choice(GREETINGS).format(who=who), rng.choice(INTRO_AI if s["disclose_ai_in_intro"] else INTRO).format(**fill)


def opener(lead: dict, s: dict, rng=random) -> str:
    return opener_parts(lead, s, rng)[0]


STOP_LINES = ["Understood, I'll take you off our list. Sorry for the interruption.",
              "Got it, you're off our list. Sorry to bother you.",
              "No problem at all, I'll take you off the list. Have a good one."]
INTRO_NOTE = ("you just asked who you are speaking with and they have answered. In this reply, in the same short breath, say your name and that you "
              "are with Ringback in Sandpoint, and go straight to your first question about their calls, warmly. Do not say it is a sales call "
              "or a cold call, do not bring up being an AI unless they ask, and no lead-in about why you are calling. If they said it is a wrong "
              "person, a bad time or asked who you are, handle that first.")
INTRO_NOTE_AI = ("you just asked who you are speaking with and they have answered. In this reply, in the same short breath, say your name, that you "
                 "are an AI assistant calling for {founder} at {company} in {town}, then go straight to your first question about their calls, warmly. "
                 "Do not say it is a sales call or a cold call, and no lead-in about why you are calling. If they said it is a wrong "
                 "person, a bad time or asked who you are, handle that first.")


def intro_note(s: dict) -> str:
    return INTRO_NOTE_AI.format(**s) if s["disclose_ai_in_intro"] else INTRO_NOTE


AI_NOTE = ("the caller is asking whether you are a robot, an AI or a real person. Answer with one quick, funny line of your own "
           "that says yes, you are an AI assistant, unmistakably (the joke comes with the yes, never instead of it), then use it "
           "in the same breath to get back to your goal. Never claim to be human.")

# Words that end the call by themselves; the model is not asked. The softer "don't call again" is skipped when the
# caller is only picking another time ("don't call me today, try Thursday").
HARD_STOP = re.compile(r"\b(stop calling|quit calling|never call|do not call|take (me|us|this number|my number) off|"
                       r"remove (me|us|my number|this number)|(put|add) (me|us|this number) on (the |your )?(do[- ]not[- ]call|dnc)|"
                       r"leave (me|us) alone|(lose|delete|forget) (my|this|our) (number|info)|stop (bothering|harassing|texting)( me| us)?|"
                       r"(quit|stop) (it|that)|get off my phone|quit (bugging|pestering)( me| us)?|piss off|screw you|go to hell|f+u+c+k( you| off)|"
                       r"i said no\b.{0,20}\b(stop|leave|don'?t call)|(i|we)'?m on the (do[- ]not[- ]call|dnc))\b", re.I)
SOFT_STOP = re.compile(r"\b(don'?t|do not) call( me| us)?( here)?( again| back| anymore| any more)\b", re.I)
PICKING_A_TIME = re.compile(r"\b(until|after|tomorrow|next week|later|this (morning|afternoon|evening)|mon|tues|wednes|thurs|fri)(day)?\b|"
                            r"\b(at|around) \d", re.I)
AI_QUESTION = re.compile(r"(\b(are|is) (you|this|that)\b.*\b(robot|a\.?i\.?|artificial|human|real (person|human)|bot|machine|computer|recording)\b|"
                         r"\b(am i|i'?m) (talking|speaking) (to|with)\b.*\b(robot|a\.?i\.?|bot|machine|computer|real|human|person)\b|"
                         r"\b(robot|a robot|an ai|a bot)\b.*\?|"
                         r"\b(this is|you'?re|you are) (a |an )?(robot|bot|ai|a\.i\.|machine|recording)\b)", re.I)
# "I don't want to talk to an AI": the model answers honestly and pushes back playfully (see NOTE_AI_REFUSAL).
AI_REFUSAL = re.compile(r"\b(not interested in|don'?t want to|won'?t|not gonna|i don'?t)\b.{0,20}\b(talk|speak|deal)\w*\b.{0,12}\b(an? )?(a\.?i\.?|ai|bot|robot|machine|computer|recording)\b|"
                        r"\bi (hate|don'?t do) (robo ?calls|bots|robots|ai)\b", re.I)

# What the caller said that counts as a refusal, and what our own reply counts as a meeting ask. Code keeps these
# counts and hands the model the next move as a note, so the model has no state to reason about out loud.
NO_SIGNAL = re.compile(r"\b(not interested|no thanks|no thank you|not right now|we'?re (good|fine|all set)|i'?m (good|fine|all set)|"
                       r"don'?t need (it|that|this)|pass(?! (it|that|this|me|you|along|the)\b))\b|^\W*no\W*$", re.I)
IRRITATED = re.compile(r"\b(i (already )?(said|told you)|again|seriously|for the last time|quit|enough|waste of)\b|!", re.I)
MEETING_ASK = re.compile(r"\b(fifteen|15|ten|10)[- ]minutes?\b|\bwhich (is|works) (easier|better)\b", re.I)
PLAYFUL_RULES = ("Keep it light and warm, tease the objection never the person or their business, use fresh words (never a line already used on this call) "
                 "and one short question. Do not mention counting or the no itself.")
NOTE_NO_ONCE = ("they said no. Do not fold, and get curious about it: ask what is behind it, lightly and warmly (why is that? what's the "
                "catch? what have they tried?), with a quick playful touch if it fits. " + PLAYFUL_RULES)
NOTE_NO_AGAIN_CALM = ("they said no again, but they sound calm. Do not repeat yourself: ask what is really behind the no, or come at it from "
                      "a different angle (money, missed jobs, a much smaller ask), playful and short. " + PLAYFUL_RULES)
NOTE_LAST_TRY = ("their no sounds flat or irritated. Give exactly one last light try: one short, warm, playful line, with no pressure and "
                 "no new pitch (an easy out they can say yes to, or a one-sentence reason it costs them nothing). If they refuse after this, you will be told to leave.")
NOTE_NO_AGAIN = "they said no and it is time to leave. Thank them warmly with a light parting quip, call log_call, then end_call. No further ask."
NOTE_NO_MORE_ASKS = "No more meeting asks on this call."
NOTE_AI_REFUSAL = ("they don't want to talk to an AI. Say yes, you are an AI, in a few playful words (honest, once, never claim to be human), then "
                   "push back with a quip about what an AI that always picks up is worth to someone who can't answer on the mower, and one short question. "
                   "Fresh words every time, never a line you used before. Do not mention counting or the no itself.")


# Machines that answer before (or instead of) a person. Checked by code before the model ever sees them, and only
# until a person has spoken with us: later in a call these words are ordinary talk ("we get five voicemails a week").
VOICEMAIL = re.compile(r"\b(leave (a |your |me a |us a )?(message|name|number)|after the (beep|tone)|at the (beep|tone)|record your message|"
                       r"(not|unable to|can'?t) (available|come to the phone|get to the phone|take your call|answer)|mailbox|"
                       r"voice ?mail|get back to you|return your call|reached the (voice|mail))", re.I)
PHONE_MENU = re.compile(r"\b(press (zero|one|two|three|four|five|six|seven|eight|nine|pound|star|\d)|(enter|dial) (the |your |an? )?extension|"
                        r"extension (zero|one|two|three|\d)|para español|you have reached (the |our )?(office|offices|main line)|"
                        r"main menu|menu options|office hours are|(we are|we're|office is|office is currently) (currently )?closed)\b", re.I)
ON_HOLD = re.compile(r"\b(stay on the line|please hold|hold (on )?while|(while|as) (i|we) (try to )?(connect|transfer)|"
                     r"your call is (important|being (transferred|connected|recorded))|transferring your call|connecting you)\b", re.I)
MAX_NOS = 4          # the backstop: after this many refusals of any kind the call ends


def machine_kind(text: str) -> str | None:
    """"voicemail", "menu" (a phone tree or office recording), "hold", or None for what sounds like a person."""
    if VOICEMAIL.search(text):
        return "voicemail"
    if PHONE_MENU.search(text):
        return "menu"
    if ON_HOLD.search(text):
        return "hold"
    return None


def stop_request(text: str) -> bool:
    return bool(HARD_STOP.search(text) or (SOFT_STOP.search(text) and not PICKING_A_TIME.search(text)))


def asked_if_ai(text: str) -> bool:
    return bool(AI_QUESTION.search(text))


# ---------------------------------------------------------------- the agent record

def agent_record_path(cfg: dict) -> Path:
    return Path(cfg["data_dir"]) / "retell_outbound.json"


def load_agent_record(cfg: dict) -> dict | None:
    """{"agent_id": ..., "number": ...}: which Retell agent and from-number place_call() dials with. None until the
    CLI's outbound-agent --create writes it (normally on whatever machine ran that), or a PUT to
    /admin/outbound/agent writes it straight onto the server's own disk instead."""
    p = agent_record_path(cfg)
    return json.loads(p.read_text()) if p.exists() else None


def save_agent_record(cfg: dict, agent_id: str, number: str | None) -> dict:
    rec = {"agent_id": agent_id, "number": normalize_phone(number) if number else None}
    agent_record_path(cfg).write_text(json.dumps(rec, indent=2))
    return rec


# ---------------------------------------------------------------- the lead list

def leads_path(cfg: dict) -> Path:
    return Path(cfg["data_dir"]) / "outbound_leads.json"


def load_leads(cfg: dict) -> list[dict]:
    """The discovery caller's lead list, kept on the server's disk (never in git). Empty until something writes it:
    a manual PUT to /admin/outbound/leads, or the lead-building skill once it exists."""
    p = leads_path(cfg)
    return json.loads(p.read_text()) if p.exists() else []


def save_leads(cfg: dict, leads: list[dict]) -> None:
    tmp = leads_path(cfg).with_suffix(".tmp")
    tmp.write_text(json.dumps(leads, indent=1))
    tmp.replace(leads_path(cfg))


# ---------------------------------------------------------------- state file

_path_locks: dict[str, threading.RLock] = {}
_path_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    """One lock per state file for the whole process, so every State object on that file takes turns."""
    key = str(Path(path).resolve())
    with _path_locks_guard:
        return _path_locks.setdefault(key, threading.RLock())


class State:
    """data/outbound.json: the do-not-call list, call attempts, dials, bookings and finished calls. One file.
    Every change reloads the file under a per-file lock, applies itself, then saves, so a call that opened its State
    long ago cannot write back a stale copy over a do-not-call entry or slots written meanwhile."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = _lock_for(self.path)
        self.data = self._fresh()
        self._load()

    @staticmethod
    def _fresh() -> dict:
        return {"dnc": [], "attempts": {}, "bookings": [], "calls": [], "lead_updates": {}, "dials": {}}

    def _load(self) -> None:
        data = self._fresh()
        if self.path.exists():
            data.update(json.loads(self.path.read_text()))
        self.data = data

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    def _change(self, fn) -> None:
        with self.lock:
            self._load()
            fn(self.data)
            self.save()

    def dnc(self) -> set[str]:
        return set(self.data["dnc"])

    def add_dnc(self, phone: str) -> None:
        def go(d):
            if phone and phone not in d["dnc"]:
                d["dnc"].append(phone)
        self._change(go)

    def attempts(self, phone: str) -> list[datetime]:
        return [datetime.fromisoformat(t) for t in self.data["attempts"].get(phone, [])]

    def add_attempt(self, phone: str, when: datetime) -> None:
        self._change(lambda d: d["attempts"].setdefault(phone, []).append(when.isoformat()))

    def add_dial(self, call_id: str, info: dict) -> None:
        """Every call placed, keyed by Retell's call id, so an unanswered call still leaves a record."""
        self._change(lambda d: d["dials"].__setitem__(call_id, info))

    def dial(self, call_id: str) -> dict | None:
        with self.lock:
            self._load()
            return self.data["dials"].get(call_id)

    def has_result(self, call_id: str) -> bool:
        with self.lock:
            self._load()
            return any(r.get("callId") == call_id for r in self.data["calls"])

    def results_for(self, phone: str) -> list[dict]:
        """Finished-call rows for this phone number, oldest first."""
        return [r for r in self.data["calls"] if (normalize_phone(r.get("phone") or "") or "") == phone]

    def booked_times(self) -> list[datetime]:
        return [datetime.fromisoformat(b["start"]) for b in self.data["bookings"]]

    def add_booking(self, booking: dict) -> None:
        self._change(lambda d: d["bookings"].append(booking))

    def set_slots(self, slots: list[str], now: datetime) -> None:
        """Open meeting starts from the manager agent (ISO times). They replace the default windows while fresh."""
        self._change(lambda d: d.__setitem__("slots", {"updated": now.isoformat(), "list": sorted(slots)}))

    def manager_slots(self, now: datetime, max_age_h: int = 36) -> list[datetime] | None:
        got = self.data.get("slots")
        if not got or now - datetime.fromisoformat(got["updated"]) > timedelta(hours=max_age_h):
            return None
        return [datetime.fromisoformat(t) for t in got["list"]]

    def next_call_number(self) -> int:
        with self.lock:
            self._load()
            return len(self.data["calls"]) + 1

    def add_result(self, row: dict, lead_id: str | None, update: dict) -> None:
        def go(d):
            same = next((i for i, r in enumerate(d["calls"]) if row.get("callId") and r.get("callId") == row["callId"]), None)
            if same is None:
                d["calls"].append(row)
            else:   # the call's own result replaces the thin row the webhook wrote for it
                row["n"] = d["calls"][same].get("n", row.get("n"))
                d["calls"][same] = row
            if lead_id:
                d["lead_updates"][lead_id] = update
        self._change(go)

    def add_result_once(self, row: dict, lead_id: str | None, update: dict) -> bool:
        """Adds the row unless one for the same callId is there already. True when it was added."""
        with self.lock:
            self._load()
            if row.get("callId") and any(r.get("callId") == row["callId"] for r in self.data["calls"]):
                return False
            row["n"] = len(self.data["calls"]) + 1
            self.add_result(row, lead_id, update)
            return True


MACHINE_REASONS = {"voicemail_reached", "ivr_reached"}   # Retell's disconnection_reason values for a machine


def webhook_result(state: State, cfg: dict, call: dict, now: datetime) -> dict | None:
    """Retell's call_ended webhook for a call we dialed. If nothing wrote a result row for it (nobody answered, so our
    socket never ran), writes one: "Voicemail" when Retell says a machine answered, otherwise "No answer". Reasons and the
    duration come from Retell's call object (disconnection_reason, duration_ms, start_timestamp). Once per call id."""
    call_id = call.get("call_id") or ""
    dial = state.dial(call_id)
    if not dial or state.has_result(call_id):
        return None
    reason = call.get("disconnection_reason") or ""
    outcome = "Voicemail" if reason in MACHINE_REASONS else "No answer"
    log = {"outcome": outcome, "objection": "None",
           "summary": f"Dialed; no conversation ({reason or 'reason not given'})."}
    started = (datetime.fromtimestamp(call["start_timestamp"] / 1000, timezone.utc) if call.get("start_timestamp")
               else datetime.fromisoformat(dial["at"]))
    minutes = (call.get("duration_ms") or 0) / 60000
    lead = {"id": dial.get("leadId"), "name": dial.get("name", ""), "phone": dial.get("phone", ""),
            "type": dial.get("type", ""), "contact": dial.get("contact", "")}
    row = outbound.call_row(lead, log, started, minutes, state.next_call_number(), cfg, "")
    row["estCost"] = outbound.est_cost(minutes, outbound.settings(cfg))
    row["callId"] = call_id
    if not state.add_result_once(row, lead["id"], outbound.lead_update(lead, log, f"{row['date']} {row['time']}")):
        return None
    return row


# ---------------------------------------------------------------- tools

def _x(ctx):
    return ctx.extra


def check_meeting_slots(ctx, a):
    state, cfg = _x(ctx)["state"], _x(ctx)["cfg"]
    given = state.manager_slots(ctx.now)
    if given is not None:   # the manager agent's real open times, minus what is booked and anything too soon
        booked = {b.replace(second=0, microsecond=0) for b in state.booked_times()}
        soon = ctx.now + timedelta(minutes=outbound.settings(cfg)["slot_lead_min"])
        slots = [t for t in given if t >= soon and t.replace(second=0, microsecond=0) not in booked][:4]
    else:
        slots = outbound.meeting_slots(ctx.now, cfg, state.booked_times())
    _x(ctx)["offered"] = {s.isoformat() for s in slots}
    return {"open": [{"start": s.isoformat(), "say": outbound.spoken_slot(s, ctx.now.astimezone(s.tzinfo).date())} for s in slots]}


def book_meeting(ctx, a):
    x = _x(ctx)
    if a["start"] not in x.get("offered", set()):
        raise ToolError("That time was not on the open list. Call check_meeting_slots and offer one of those times.")
    phone = normalize_phone(a["callback_number"])
    if not phone or len(phone.lstrip("+")) < 10:
        raise ToolError("The callback number needs all 10 digits. Ask for it again.")
    start = datetime.fromisoformat(a["start"])
    booking = {"start": a["start"], "owner": a["owner_name"], "phone": phone, "business": x["lead"].get("name", "")}
    x["state"].add_booking(booking)
    x["booked"] = booking
    return {"booked": outbound.spoken_slot(start, ctx.now.astimezone(start.tzinfo).date()), "callback": spoken_phone(phone),
            "say_next": "Read back the day and time and the number Noah will call, then say goodbye."}


def log_call(ctx, a):
    if a["outcome"] not in outbound.OUTCOMES:
        raise ToolError(f"outcome must be one of {outbound.OUTCOMES}.")
    if a["outcome"] in ("Demo booked", "Pilot signed") and not _x(ctx).get("booked"):
        raise ToolError("No meeting is booked on this call. Use book_meeting first or pick another outcome.")
    _x(ctx)["log"] = a
    return {"logged": True}


def mark_do_not_call(ctx, a):
    phone = normalize_phone(_x(ctx)["lead"].get("phone") or "")
    _x(ctx)["state"].add_dnc(phone)
    _x(ctx)["dnc"] = True
    return {"marked": True}


tools.HANDLERS.update({f.__name__: f for f in (check_meeting_slots, book_meeting, log_call, mark_do_not_call)})


# ---------------------------------------------------------------- the cached prompt prefix

def session_cfg(cfg: dict) -> dict:
    """The cfg an outbound call runs with: the outbound model and effort, and its own cache life (five minutes: every
    dial re-warms it, and the block runner's gaps fit inside, so writes cost 1.25x instead of 2x)."""
    m, a = cfg["models"], cfg.get("agent", {})
    return {**cfg, "models": {**m, "agent": m.get("outbound", m["agent"]), "agent_effort": m.get("outbound_effort", m["agent_effort"])},
            "agent": {**a, "cache_ttl": outbound.settings(cfg)["cache_ttl"]}}


def _stable_system(cfg: dict, now: datetime) -> list[dict]:
    """The cached system block (the sales prompt, the same for every lead). OutboundSession and warm() both build it
    here: the cache only hits on a byte-identical prefix."""
    return [{**outbound.build_prompt({}, now, cfg)[0], "cache_control": cache_control(session_cfg(cfg))}]


def _tools() -> list[dict]:
    return [{**t, "strict": True} for t in outbound.schemas()]


_warm_lock = threading.Lock()
_last_warm: float | None = None


def warm(client, cfg: dict, now: datetime, *, min_gap_s: int = 240) -> dict | None:
    """Writes the outbound prompt into the cache (prefill only, nothing generated) so the first model turn of a call
    reads it instead of waiting 3 to 4 seconds. Skipped when it ran less than `min_gap_s` ago. Returns the token
    usage, or None if skipped or it failed."""
    global _last_warm
    with _warm_lock:
        if _last_warm is not None and time.monotonic() - _last_warm < min_gap_s:
            return None
        _last_warm = time.monotonic()
    c = session_cfg(cfg)
    try:
        resp = client.messages.create(
            model=c["models"]["agent"], max_tokens=0, output_config={"effort": c["models"]["agent_effort"]},
            system=_stable_system(cfg, now), tools=_tools(),
            messages=[{"role": "user", "content": "warmup"}], **thinking_param(c["models"]))
    except Exception as e:   # a failed warm only costs the wait it would have saved
        with _warm_lock:
            _last_warm = None
        log.warning("outbound cache warm failed: %s", e)
        return None
    usage = {"input": resp.usage.input_tokens or 0, "output": 0,
             "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
             "cache_write": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0}
    log.info("outbound cache warm: %s", usage)
    return usage


def warm_in_background(warm_fn) -> threading.Thread | None:
    """Runs warm_fn on its own thread; never raises into the caller."""
    if warm_fn is None:
        return None
    t = threading.Thread(target=lambda: warm_fn(), daemon=True, name="outbound-warm")
    t.start()
    return t


# ---------------------------------------------------------------- session

class OutboundSession(CallSession):
    """A CallSession whose prompt, tools and opening line come from the sales playbook. The receptionist's reply
    machinery (streaming, sentence filters, dropped turns, cut-ins) is inherited unchanged."""

    still_working_pool = OUTBOUND_STILL_WORKING   # live.py plays one of these if the model is slow

    def __init__(self, client, cfg: dict, lead: dict, state: State, store, now: datetime,
                 call_id: str | None = None, platform: str = "retell", rng=random, warm_cache: bool = False):
        s = outbound.settings(cfg)
        self.client, self.store = client, store
        self.cfg = session_cfg(cfg)
        self.biz = {"id": "outbound", "vertical": "outbound", "name": s["company"], "spoken_name": s["company"],
                    "agent_name": s["agent_name"], "timezone": s["timezone"]}
        self.lead, self.state, self.started = lead, state, now
        self.company_name = s["company"]
        self.greeting = ""   # an empty first message makes Retell wait for the person to say hello
        self.hook_text, self.intro_text = opener_parts(lead, s, rng)
        self.opener = self.hook_text
        self.first_word_times: list[float] = []   # seconds to first words, per reply (set by live.py)
        self._resumed = False
        self._opened = False
        self._intro_checked = False
        self._asks = 0            # meeting asks we have made
        self._nos = 0             # refusals the caller has given
        self._person = False      # a person has talked with us (not a recording)
        self._last_try = False    # the one extra try after a flat or irritated no has been used
        self.call_id = call_id or f"outbound-{int(now.timestamp())}"
        blocks = outbound.build_prompt(lead, now, cfg)
        self.system = [*_stable_system(cfg, now), blocks[1]]
        self.tools = _tools()
        phone = normalize_phone(lead.get("phone") or "")
        self.ctx = tools.ToolContext(self.biz, store, now, phone, self.call_id)
        self.ctx.extra.update(cfg=cfg, state=state, lead=lead)
        self.messages: list[dict] = []
        self.transcript: list[dict] = []
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self._before = None
        self._said_once, self._recent_acks, self._held_back, self._interjections = set(), [], [], []
        store.start_call(self.call_id, "outbound", platform, phone, now)
        if warm_cache and client is not None:   # a safety net: place_call normally warmed it while the phone rang
            def _warm():
                got = warm(client, cfg, now)
                for k in ("input", "output", "cache_read", "cache_write"):
                    self.usage[k] += (got or {}).get(k, 0)
            warm_in_background(_warm)

    def respond(self, caller_text, on_text=None, control=None, heard_only=None, note=None):
        if not self.ctx.ended and stop_request(caller_text):
            return self._stop(caller_text, on_text)
        kind = None if self._person or self.ctx.ended else machine_kind(caller_text)
        if kind:
            return self._machine(kind, caller_text, on_text)
        if not self._opened and not self.ctx.ended:   # their first words ("Hello?"): the opener answers, no model call
            self._opened = True
            if on_text:
                on_text(self.hook_text)
            messages = [{"role": "user", "content": caller_text}, {"role": "assistant", "content": self.opener}]
            self._save(messages, caller_text, {"role": "agent", "text": self.opener, "tools": [], "steps": [{"say": self.opener}]}, None, final=False)
            return Turn(self.opener, [], 0.0, 0.0, False)
        refusal = AI_REFUSAL.search(caller_text)
        if not self._intro_checked:   # the first model turn after the opener
            self._intro_checked = True
            self._person = True   # someone answered our "is this the owner?": from here machine words are ordinary talk
            intro = intro_note(outbound.settings(self.cfg))
            note = f"{note} {intro}" if note else intro
        if asked_if_ai(caller_text):
            note = f"{note} {AI_NOTE}" if note else AI_NOTE
        directive = None
        if NO_SIGNAL.search(caller_text) or refusal:
            self._nos += 1
            directive = self._no_directive(caller_text, bool(refusal))
        elif self._asks >= 3:
            directive = NOTE_NO_MORE_ASKS
        if directive:
            note = f"{note} {directive}" if note else directive
        tone = callreview.tone_note(caller_text)
        if tone:
            note = f"{note} {tone}" if note else tone
        turn = super().respond(caller_text, on_text, control, heard_only, note)
        if MEETING_ASK.search(getattr(turn, "text", "") or ""):
            self._asks += 1
        return turn

    def protected(self, chunk: str) -> bool:
        """Nothing is protected: the greeting is one short question."""
        return False

    def resume_line(self, heard: str) -> str | None:
        """Noise right after the greeting can leave the person never hearing who is calling. If so, say the intro (once) instead
        of "I didn't catch that"."""
        if self._resumed or self.company_name in (heard or ""):
            return None
        self._resumed = True
        self.transcript.append({"role": "agent", "text": self.intro_text, "resumed": True})
        return self.intro_text

    def _no_directive(self, caller_text: str, ai_refusal: bool) -> str:
        """Curious and playful on a calm no. A flat, irritated or repeated no gets exactly one last light try; after that, or
        after the fourth no of any kind, the call ends warmly."""
        irritated = bool(IRRITATED.search(caller_text))
        if self._nos >= MAX_NOS or ((irritated or self._nos >= 3) and self._last_try):
            return NOTE_NO_AGAIN
        if irritated or self._nos >= 3:
            self._last_try = True
            return NOTE_LAST_TRY
        if self._nos == 1:
            return NOTE_AI_REFUSAL if ai_refusal else NOTE_NO_ONCE
        return NOTE_NO_AGAIN_CALM

    def _machine(self, kind: str, caller_text: str, on_text) -> Turn:
        """A recording, not a person. Handled by code, so the model never has to work out (out loud) what it is hearing.
        Voicemail and any key-press menu ("press one...") are the same thing to us: a message costs call minutes and reaches
        nobody, so hang up at once without a word and log it. A hold message ("please hold") is waited out in silence; the first
        person who picks up still gets the opener."""
        if kind == "hold":
            self.transcript += [{"role": "caller", "text": caller_text, "machine": kind}]
            self._before = None   # nothing to take back: the next words are a new turn, not joined to the recording
            return Turn("", [], None, 0.0, False)
        outcome, summary = "Voicemail", ("Reached voicemail; hung up without leaving a message." if kind == "voicemail"
                                         else "Reached a phone menu or recording; hung up without leaving a message.")
        used = []
        for name, args in (("log_call", {"outcome": outcome, "objection": "None", "summary": summary}),
                           ("end_call", {"reason": kind})):
            result, err = tools.run_tool(self.ctx, name, args)
            used.append({"name": name, "args": args, "result": result, "error": err})
        self.transcript += [{"role": "caller", "text": caller_text, "machine": kind},
                            {"role": "agent", "text": "", "tools": used, "steps": [{"tool": c} for c in used]}]
        self._before = None
        return Turn("", used, None, 0.0, True)

    def _stop(self, caller_text: str, on_text) -> Turn:
        """A clear request to stop: said and done by code, no model call. Marks the number and hangs up."""
        line = random.choice(STOP_LINES)
        if on_text:
            on_text(line)
        used = []
        for name, args in (("mark_do_not_call", {"reason": caller_text[:80]}),
                           ("log_call", {"outcome": "Not interested", "objection": "Not interested",
                                         "summary": "Asked not to be called again. Number added to the do-not-call list."}),
                           ("end_call", {"reason": "asked to stop"})):
            result, err = tools.run_tool(self.ctx, name, args)
            used.append({"name": name, "args": args, "result": result, "error": err})
        messages = [*self.messages, {"role": "user", "content": caller_text}, {"role": "assistant", "content": line}]
        self._save(messages, caller_text, {"role": "agent", "text": line, "tools": used,
                                           "steps": [{"say": line}, *({"tool": c} for c in used)]}, None, final=True)
        return Turn(line, used, 0.0, 0.0, True)

    def finish(self, now: datetime, minutes: float, reason: str | None = None) -> dict:
        """Called when the line closes: records the attempt, the result row and the lead update."""
        phone = self.ctx.caller or ""
        raw_id = self.call_id.removeprefix("retell-")
        if not self.state.dial(raw_id):   # a call placed outside place_call() has no attempt on record yet
            self.state.add_attempt(phone, self.started)
        log = self.ctx.extra.get("log") or callreview.fallback_log(self.transcript, self.lead, reason)
        row = outbound.call_row(self.lead, log, self.started, minutes, self.state.next_call_number(), self.cfg,
                                transcript_text(self.transcript, with_tools=False))
        row["estCost"] = outbound.est_cost(minutes, outbound.settings(self.cfg))
        row["callId"] = raw_id
        if self.first_word_times:
            row["latency"] = {"median_s": round(statistics.median(self.first_word_times), 2),
                              "max_s": round(max(self.first_word_times), 2), "replies": len(self.first_word_times)}
        self.state.add_result(row, self.lead.get("id"), outbound.lead_update(self.lead, log, f"{row['date']} {row['time']}"))
        self.store.end_call(self.call_id, self.transcript, log["outcome"], now)
        return row


# ---------------------------------------------------------------- Retell

VOICE_IDENTITY = ("voice_id", "voice_model", "fallback_voice_ids", "voice_speed", "voice_temperature", "denoising_mode")


def receptionist_voice(cfg: dict) -> dict:
    """What makes the caller sound like the receptionist: the live shop's voice id, model, speed, temperature and
    noise setting, read from its JSON. How the caller behaves on the line (how easily it can be interrupted, how fast it
    reacts) is its own: it is the one who has to keep talking through a cold pick-up."""
    live = (cfg.get("live_businesses") or [cfg["default_business"]])[0]
    block = json.loads((Path(cfg["businesses_dir"]) / f"{live}.json").read_text())["voice"]
    return {**{k: block[k] for k in VOICE_IDENTITY if k in block},
            "enable_backchannel": False, "ambient_sound": None, "responsiveness": 1.0, "interruption_sensitivity": 0.9,
            "enable_dynamic_voice_speed": False, "enable_dynamic_responsiveness": False}


def agent_payload(cfg: dict, base_url: str, secret: str, voice: dict | None = None) -> dict:
    """The body for Retell's create-agent call for the outbound agent. It points at a different websocket path than
    the receptionist, so the two never share a session. Field names follow the receptionist's own agent setup; check
    the voicemail fields against Retell's current docs before the first call."""
    s = outbound.settings(cfg)
    base = base_url.rstrip("/")
    ws = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return {"agent_name": f"{s['company']} discovery caller",
            "response_engine": {"type": "custom-llm", "llm_websocket_url": f"{ws}/llm-websocket-out/{secret}"},
            **(voice or {}),
            "boosted_keywords": [s["agent_name"], s["company"], s["founder"], s["town"], "lawn care", "landscaping", "snow removal", "plowing", "voicemail"],
            "webhook_url": f"{base}/retell/webhook", "language": "en-US",
            "voicemail_option": {"action": {"type": "hangup"}},   # a message reaches nobody and costs minutes
            "end_call_after_silence_ms": 15000, "max_call_duration_ms": 600000,
            "post_call_analysis_model": None}


def live_call_fn(api_key: str):
    """A real call_fn for place_call(): Retell's REST API over plain urllib, the same request `retell_setup._call`
    makes. Unlike that helper (a CLI tool, fine to SystemExit), this raises RuntimeError so a server request can turn
    a failure into a clean error response instead of killing the process."""
    import urllib.error
    import urllib.request

    def call(method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(f"https://api.retellai.com{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Retell {method} {path} failed: {e.code} {e.read().decode(errors='replace')[:300]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Retell {method} {path} failed to connect: {e}")
    return call


def call_next(cfg: dict, leads: list[dict], state: State, call_fn, now: datetime, warm_fn=None) -> dict:
    """Finds the first callable lead in `outbound.plan_queue`'s own order (consent-yes first, then a due follow-up,
    then the rest) and places one call to it. Dials nothing and says why if no lead is eligible right now (outside
    call hours, the week's attempt limit, on the do-not-call list, cold calling still switched off...). A lead that
    turns out ineligible at the moment of dialing (place_call's own re-check; a second request landed first, or the
    outbound number/agent isn't set up yet) is skipped in favor of the next one rather than failing the whole run."""
    history = {phone: state.attempts(phone) for phone in
               {normalize_phone(l.get("phone") or "") or "" for l in leads} if phone}
    s = outbound.settings(cfg)
    spent = outbound.spent_today(state.data["calls"], state.data["attempts"], now, s)
    if spent + s["budget_reserve_usd"] > s["daily_budget_usd"]:
        return {"called": None, "reason": f"daily budget: about ${spent:.2f} of ${s['daily_budget_usd']:.2f} spent today"}
    results = {phone: state.results_for(phone) for phone in history}
    rows = outbound.plan_queue(leads, now, cfg, history, state.dnc(), results)
    candidates = [r for r in rows if r["would_call"]]
    if not candidates:
        return {"called": None, "reason": rows[0]["why"] if rows else "no leads on the list"}
    last_reason = None
    for r in candidates:
        lead = r["lead"]
        try:
            result = place_call(cfg, lead, now, state, call_fn, warm_fn=warm_fn)
        except RuntimeError as e:
            last_reason = str(e)
            continue
        return {"called": lead.get("name"), "phone": lead.get("phone"), "retell": result}
    return {"called": None, "reason": last_reason or "every eligible lead was refused when dialing"}


ENDED = {"ended", "error", "not_connected"}   # Retell call_status values that mean the line is closed (unverified: check Retell's docs)


class BlockRunner:
    """Runs one block of outbound calls on the server: dial the next eligible lead, wait for that call to end, pause,
    repeat, up to `calls` calls. One block at a time. It stops early at the daily budget, outside call hours, when no
    lead is eligible, when a call can't be confirmed finished, or when `stop()` is called. Every gate is call_next's
    own (and so place_call's); this only sequences them. A number dialed in this block is never dialed again in it,
    even if it never answered (an unanswered call leaves no attempt on record)."""

    def __init__(self, cfg: dict, state_path, call_fn, clock=None, sleep=time.sleep, warm_fn=None):
        self.cfg, self.state_path, self.call_fn, self.sleep = cfg, Path(state_path), call_fn, sleep
        self.warm_fn = warm_fn
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.status: dict = {"running": False}

    def start(self) -> bool:
        """Claims the runner; False if a block is already running."""
        if not self.lock.acquire(blocking=False):
            return False
        self._stop.clear()
        self.status = {"running": True, "started": self.clock().isoformat(), "calls": [], "stopped_because": None}
        return True

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> dict:
        """Call start() first. Always releases the runner, and always leaves a reason in status."""
        s = outbound.settings(self.cfg)["block_runner"]
        reason = "reached the block size"
        try:
            dialed: set[str] = set()
            while len(self.status["calls"]) < int(s["calls"]):
                if self._stop.is_set():
                    reason = "stopped by request"
                    break
                state = State(self.state_path)   # fresh each time: live calls write their own results to this file
                leads = [l for l in load_leads(self.cfg) if (normalize_phone(l.get("phone") or "") or "") not in dialed]
                out = call_next(self.cfg, leads, state, self.call_fn, self.clock(), warm_fn=self.warm_fn)
                if not out.get("called"):
                    reason = out.get("reason") or "nothing to call"
                    break
                dialed.add(normalize_phone(out["phone"]) or "")
                self.status["calls"].append({"lead": out["called"], "phone": out["phone"], "at": self.clock().isoformat()})
                if not self._wait_for_end((out.get("retell") or {}).get("call_id"), int(s["max_call_wait_s"])):
                    reason = ("stopped by request" if self._stop.is_set()
                              else "could not confirm the last call ended; no more calls this block")
                    break
                if len(self.status["calls"]) < int(s["calls"]):
                    self._pause(int(s["pause_s"]))
        except Exception as e:   # never leave the runner locked or the status blank
            reason = f"error: {e}"
        finally:
            self.status.update(running=False, stopped_because=reason, finished=self.clock().isoformat())
            self.lock.release()
        return self.status

    def _pause(self, seconds: int) -> None:
        for _ in range(max(1, seconds)):
            if self._stop.is_set():
                return
            self.sleep(1)

    def _wait_for_end(self, call_id: str | None, max_wait_s: int) -> bool:
        if not call_id:
            return False
        waited = 0
        while waited <= max_wait_s:
            if self._stop.is_set():
                return False
            try:
                if (self.call_fn("GET", f"/v2/get-call/{call_id}", None).get("call_status") or "") in ENDED:
                    return True
            except RuntimeError:
                pass   # a failed status check is retried until max_wait_s runs out
            self.sleep(5)
            waited += 5
        return False


def place_call(cfg: dict, lead: dict, now: datetime, state: State, call_fn, env=os.environ, warm_fn=None) -> dict:
    """Starts one outbound call, or raises. `call_fn(method, path, body)` is Retell's REST call (injected for tests)."""
    s = outbound.settings(cfg)
    if s["mode"] == "dry-run":
        raise RuntimeError("outbound.mode is dry-run: nothing is dialed.")
    if env.get("OUTBOUND_LIVE") != "1":
        raise RuntimeError("Set OUTBOUND_LIVE=1 to allow live outbound calls.")
    phone = normalize_phone(lead.get("phone") or "") or ""
    ok, why = outbound.can_call(lead, now, cfg, state.attempts(phone), state.dnc(), state.results_for(phone))
    if not ok:
        raise RuntimeError(f"Not calling {lead.get('name')}: {why}")
    rec = json.loads((Path(cfg["data_dir"]) / "retell_outbound.json").read_text())
    if not rec.get("number"):
        raise RuntimeError("No outbound number yet: run `outbound-number --number +1...` once you have one from Retell.")
    warm_in_background(warm_fn)   # the ringing time pays for the prompt prefill
    result = call_fn("POST", "/v2/create-phone-call", {
        "from_number": rec["number"], "to_number": normalize_phone(lead["phone"]), "override_agent_id": rec["agent_id"],
        "idempotency_key": f"{lead.get('id') or 'lead'}-{int(now.timestamp())}",   # a retry within an hour cannot dial twice
        "honor_internal_dnc": True,
        "metadata": {"lead": {k: lead.get(k, "") for k in ("id", "name", "phone", "type", "contact", "notes", "consent")}}})
    # Every dial leaves a record, even one nobody answers: the attempt counts toward the limits and the dial lets the
    # webhook write a "No answer" row later.
    state.add_attempt(phone, now)
    state.add_dial((result or {}).get("call_id") or f"dial-{int(now.timestamp())}",
                   {"leadId": lead.get("id"), "name": lead.get("name", ""), "phone": lead.get("phone", ""), "type": lead.get("type", ""),
                    "contact": lead.get("contact", ""), "at": now.isoformat()})
    return result


# ---------------------------------------------------------------- the dashboard hand-off

def crm_files(data: dict, out_dir) -> list[Path]:
    """Writes what a Claude session copies into the dashboard database: each finished call as a Cold Call Desk row
    (docs `calls/ai<n>`) and each changed lead (docs `leads/<id>`). Bookings go in as follow-up tasks."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    calls = {f"ai{r['n']}": r for r in data.get("calls", [])}
    tasks = {f"ai-meet-{i}": {"title": f"Meeting with {b['owner']} at {b['business']}", "due": b["start"][:10], "stream": "outreach",
                              "priority": 1, "done": False, "doneAt": None, "createdAt": int(time.time() * 1000), "notes": f"Call {b['phone']} at {b['start']}"}
             for i, b in enumerate(data.get("bookings", []))}
    files = []
    for name, payload in (("outbound-calls.json", calls), ("outbound-leads.json", data.get("lead_updates", {})),
                          ("outbound-tasks.json", tasks)):
        p = out / name
        p.write_text(json.dumps(payload, indent=1))
        files.append(p)
    return files
