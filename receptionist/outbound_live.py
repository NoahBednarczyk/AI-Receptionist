"""The discovery caller on a live call: its session, tools, state file and the Retell calls that start it.

Nothing here runs unless Noah switches it on. A call is placed only by `place_call()`, which refuses unless
outbound.mode is not "dry-run", OUTBOUND_LIVE=1 is set, and the lead passes outbound.can_call().
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import outbound, tools
from .agent import CallSession, Turn, cache_control
from .speech import normalize_phone, spoken_phone
from .tools import ToolError

INTRO = ["{agent} here, calling for {founder} at {company} in {town}. Fair warning, it's a sales call, but a short one. Got a minute?",
         "I'm {agent}, calling on behalf of {founder} at {company}, here in {town}. It's a sales call, I'll keep it quick. Okay?",
         "This is {agent} with {founder} at {company} in {town}. Heads up, I'm selling something, but I'm fast. Is now terrible?",
         "{agent} calling for {founder} at {company}, {town}. Yes, it's a sales call, and I'll be short. Can I steal a minute?"]
# One-liners that need nothing but the shop's name or trade. A lead's own `hook` field (written for that shop) beats these.
HOOKS = {
    "plumbing": ["Is {biz} the plumbing outfit that gets called at 2 AM, or the one that doesn't answer at 2 AM?",
                 "I'm told {biz} fixes things people have been ignoring for three weeks.",
                 "Quick one for {biz}: a pipe bursts at dinnertime, who answers your phone?",
                 "I was told to call {biz} because plumbers are the busiest people in {town}."],
    "hvac": ["Is {biz} the folks people call when it's minus ten and the furnace has opinions?",
             "I'm calling {biz} because nobody thinks about heating until it quits.",
             "Quick one for {biz}: furnace dies at 6 PM on a Sunday, who picks up?",
             "I hear {biz} keeps {town} warm, and I keep calling people who can't hear their phone."],
    "default": ["I'm calling {biz} with the strangest cold call you'll get today.",
                "Quick one for {biz}: when you're elbow-deep in a job and the phone rings, who gets it?",
                "I found {biz} in {town} and had to see what happens when the phone rings."],
}


def trade_of(lead: dict) -> str:
    t = (lead.get("type") or "").lower()
    return "plumbing" if "plumb" in t and "hvac" not in t else "hvac" if "hvac" in t or "heat" in t else "default"


def opener(lead: dict, s: dict, rng=random) -> str:
    """The first words of the call: a hook about this shop, then who is calling (always an AI), then a small ask."""
    owner = (lead.get("contact") or "").split(" ")[0]
    hook = lead.get("hook") or rng.choice(HOOKS[trade_of(lead)])
    fill = dict(biz=lead.get("name") or "your shop", town=s["town"], agent=s["agent_name"], founder=s["founder"], company=s["company"])
    start = f"Hi {owner}? " if owner else ""
    return start + hook.format(**fill) + " " + rng.choice(INTRO).format(**fill)


STOP_LINES = ["Understood, I'll take you off our list. Sorry for the interruption.",
              "Got it, you're off our list. Sorry to bother you.",
              "No problem at all, I'll take you off the list. Have a good one."]
AI_NOTE = ("the caller is asking whether you are a robot, an AI or a real person. Answer with one quick, funny line of your own "
           "that says yes, you are an AI assistant, unmistakably (the joke comes with the yes, never instead of it), then use it "
           "in the same breath to get back to your goal. Never claim to be human.")

# Words that end the call by themselves; the model is not asked. The softer "don't call again" is skipped when the
# caller is only picking another time ("don't call me today, try Thursday").
HARD_STOP = re.compile(r"\b(stop calling|quit calling|never call|do not call|take (me|us|this number|my number) off|"
                       r"remove (me|us|my number|this number)|(put|add) (me|us|this number) on (the |your )?(do[- ]not[- ]call|dnc)|"
                       r"leave (me|us) alone)\b", re.I)
SOFT_STOP = re.compile(r"\b(don'?t|do not) call( me| us)?( here)?( again| back| anymore| any more)\b", re.I)
PICKING_A_TIME = re.compile(r"\b(until|after|tomorrow|next week|later|this (morning|afternoon|evening)|mon|tues|wednes|thurs|fri)(day)?\b|"
                            r"\b(at|around) \d", re.I)
AI_QUESTION = re.compile(r"(\b(are|is) (you|this|that)\b.*\b(robot|a\.?i\.?|artificial|human|real (person|human)|bot|machine|computer|recording)\b|"
                         r"\b(am i|i'?m) (talking|speaking) (to|with)\b.*\b(robot|a\.?i\.?|bot|machine|computer|real|human|person)\b|"
                         r"\b(robot|a robot|an ai|a bot)\b.*\?)", re.I)


def stop_request(text: str) -> bool:
    return bool(HARD_STOP.search(text) or (SOFT_STOP.search(text) and not PICKING_A_TIME.search(text)))


def asked_if_ai(text: str) -> bool:
    return bool(AI_QUESTION.search(text))


# ---------------------------------------------------------------- state file

class State:
    """data/outbound.json: the do-not-call list, call attempts, bookings and finished calls. One file, written whole."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.data = {"dnc": [], "attempts": {}, "bookings": [], "calls": [], "lead_updates": {}}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text()))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    def dnc(self) -> set[str]:
        return set(self.data["dnc"])

    def add_dnc(self, phone: str) -> None:
        with self.lock:
            if phone and phone not in self.data["dnc"]:
                self.data["dnc"].append(phone)
            self.save()

    def attempts(self, phone: str) -> list[datetime]:
        return [datetime.fromisoformat(t) for t in self.data["attempts"].get(phone, [])]

    def add_attempt(self, phone: str, when: datetime) -> None:
        with self.lock:
            self.data["attempts"].setdefault(phone, []).append(when.isoformat())
            self.save()

    def booked_times(self) -> list[datetime]:
        return [datetime.fromisoformat(b["start"]) for b in self.data["bookings"]]

    def add_booking(self, booking: dict) -> None:
        with self.lock:
            self.data["bookings"].append(booking)
            self.save()

    def set_slots(self, slots: list[str], now: datetime) -> None:
        """Open meeting starts from the manager agent (ISO times). They replace the default windows while fresh."""
        with self.lock:
            self.data["slots"] = {"updated": now.isoformat(), "list": sorted(slots)}
            self.save()

    def manager_slots(self, now: datetime, max_age_h: int = 36) -> list[datetime] | None:
        got = self.data.get("slots")
        if not got or now - datetime.fromisoformat(got["updated"]) > timedelta(hours=max_age_h):
            return None
        return [datetime.fromisoformat(t) for t in got["list"]]

    def next_call_number(self) -> int:
        return len(self.data["calls"]) + 1

    def add_result(self, row: dict, lead_id: str | None, update: dict) -> None:
        with self.lock:
            self.data["calls"].append(row)
            if lead_id:
                self.data["lead_updates"][lead_id] = update
            self.save()


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


# ---------------------------------------------------------------- session

class OutboundSession(CallSession):
    """A CallSession whose prompt, tools and opening line come from the sales playbook. The receptionist's reply
    machinery (streaming, sentence filters, dropped turns, cut-ins) is inherited unchanged."""

    def __init__(self, client, cfg: dict, lead: dict, state: State, store, now: datetime,
                 call_id: str | None = None, platform: str = "retell", rng=random):
        s = outbound.settings(cfg)
        m = cfg["models"]
        self.client, self.store = client, store
        self.cfg = {**cfg, "models": {**m, "agent": m.get("outbound", m["agent"]), "agent_effort": m.get("outbound_effort", m["agent_effort"])}}
        self.biz = {"id": "outbound", "vertical": "outbound", "name": s["company"], "spoken_name": s["company"],
                    "agent_name": s["agent_name"], "timezone": s["timezone"]}
        self.lead, self.state, self.started = lead, state, now
        self.greeting = ""   # an empty first message makes Retell wait for the person to say hello
        self.opener = opener(lead, s, rng)
        self._opened = False
        self.call_id = call_id or f"outbound-{int(now.timestamp())}"
        blocks = outbound.build_prompt(lead, now, cfg)
        self.system = [{**blocks[0], "cache_control": cache_control(cfg)}, blocks[1]]
        self.tools = [{**t, "strict": True} for t in outbound.schemas()]
        phone = normalize_phone(lead.get("phone") or "")
        self.ctx = tools.ToolContext(self.biz, store, now, phone, self.call_id)
        self.ctx.extra.update(cfg=cfg, state=state, lead=lead)
        self.messages: list[dict] = []
        self.transcript: list[dict] = []
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self._before = None
        self._said_once, self._recent_acks, self._held_back, self._interjections = set(), [], [], []
        store.start_call(self.call_id, "outbound", platform, phone, now)

    def respond(self, caller_text, on_text=None, control=None, heard_only=None, note=None):
        if not self.ctx.ended and stop_request(caller_text):
            return self._stop(caller_text, on_text)
        if not self._opened and not self.ctx.ended:   # their first words ("Hello?"): the opener answers, no model call
            self._opened = True
            if on_text:
                on_text(self.opener)
            messages = [{"role": "user", "content": caller_text}, {"role": "assistant", "content": self.opener}]
            self._save(messages, caller_text, {"role": "agent", "text": self.opener, "tools": [], "steps": [{"say": self.opener}]}, None, final=False)
            return Turn(self.opener, [], 0.0, 0.0, False)
        if asked_if_ai(caller_text):
            note = f"{note} {AI_NOTE}" if note else AI_NOTE
        return super().respond(caller_text, on_text, control, heard_only, note)

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
        self.state.add_attempt(phone, self.started)
        log = self.ctx.extra.get("log") or {"outcome": "No answer" if len(self.transcript) < 3 else "Gatekeeper",
                                            "summary": f"Call ended without a log ({reason or 'hung up'})."}
        row = outbound.call_row(self.lead, log, self.started, minutes, self.state.next_call_number(), self.cfg)
        self.state.add_result(row, self.lead.get("id"), outbound.lead_update(self.lead, log))
        self.store.end_call(self.call_id, self.transcript, log["outcome"], now)
        return row


# ---------------------------------------------------------------- Retell

def receptionist_voice(cfg: dict) -> dict:
    """The live receptionist's voice block (voice id, model, speed, interruption settings), so the caller always sounds
    like it. Changing the voice in the shop's JSON changes both on the next create/tune."""
    live = (cfg.get("live_businesses") or [cfg["default_business"]])[0]
    return json.loads((Path(cfg["businesses_dir"]) / f"{live}.json").read_text())["voice"]


def agent_payload(cfg: dict, base_url: str, secret: str, voice: dict | None = None) -> dict:
    """The body for Retell's create-agent call for the outbound agent. It points at a different websocket path than
    the receptionist, so the two never share a session. Field names follow the receptionist's own agent setup; check
    the voicemail fields against Retell's current docs before the first call."""
    s = outbound.settings(cfg)
    base = base_url.rstrip("/")
    ws = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    voicemail = (f"Hi, this is {s['agent_name']}, calling for {s['founder']} at {s['company']} in {s['town']}. "
                 f"We're building a phone assistant for heating and plumbing shops and {s['founder']} would like ten minutes "
                 f"of your time. You can try our demo line any time at {spoken_phone(s['demo_line'])}. Thanks, and have a good day.")
    return {"agent_name": f"{s['company']} discovery caller",
            "response_engine": {"type": "custom-llm", "llm_websocket_url": f"{ws}/llm-websocket-out/{secret}"},
            **(voice or {}),
            "boosted_keywords": [s["agent_name"], s["company"], s["founder"], s["town"], "plumbing", "heating", "furnace", "voicemail"],
            "webhook_url": f"{base}/retell/webhook", "language": "en-US",
            "voicemail_option": {"action": {"type": "static_text", "text": voicemail}},
            "end_call_after_silence_ms": 15000, "max_call_duration_ms": 600000,
            "post_call_analysis_model": None}


def place_call(cfg: dict, lead: dict, now: datetime, state: State, call_fn, env=os.environ) -> dict:
    """Starts one outbound call, or raises. `call_fn(method, path, body)` is Retell's REST call (injected for tests)."""
    s = outbound.settings(cfg)
    if s["mode"] == "dry-run":
        raise RuntimeError("outbound.mode is dry-run: nothing is dialed.")
    if env.get("OUTBOUND_LIVE") != "1":
        raise RuntimeError("Set OUTBOUND_LIVE=1 to allow live outbound calls.")
    ok, why = outbound.can_call(lead, now, cfg, state.attempts(normalize_phone(lead.get("phone") or "") or ""), state.dnc())
    if not ok:
        raise RuntimeError(f"Not calling {lead.get('name')}: {why}")
    rec = json.loads((Path(cfg["data_dir"]) / "retell_outbound.json").read_text())
    if not rec.get("number"):
        raise RuntimeError("No outbound number yet: run `outbound-number --number +1...` once you have one from Retell.")
    return call_fn("POST", "/v2/create-phone-call", {
        "from_number": rec["number"], "to_number": normalize_phone(lead["phone"]), "override_agent_id": rec["agent_id"],
        "idempotency_key": f"{lead.get('id') or 'lead'}-{int(now.timestamp())}",   # a retry within an hour cannot dial twice
        "honor_internal_dnc": True,
        "metadata": {"lead": {k: lead.get(k, "") for k in ("id", "name", "phone", "type", "contact", "notes", "consent")}}})


# ---------------------------------------------------------------- the dashboard hand-off

def crm_files(data: dict, out_dir) -> list[Path]:
    """Writes what a Claude session copies into the dashboard database: each finished call as a Cold Call Desk row
    (docs `calls/ai<n>`) and each changed lead (docs `leads/<id>`). Bookings go in as follow-up tasks."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    calls = {f"ai{r['n']}": r for r in data.get("calls", [])}
    tasks = {f"ai-meet-{i}": {"title": f"Meeting with {b['owner']} at {b['business']}", "due": b["start"][:10], "stream": "outreach",
                              "priority": 1, "done": False, "doneAt": None, "createdAt": 0, "notes": f"Call {b['phone']} at {b['start']}"}
             for i, b in enumerate(data.get("bookings", []))}
    files = []
    for name, payload in (("outbound-calls.json", calls), ("outbound-leads.json", data.get("lead_updates", {})),
                          ("outbound-tasks.json", tasks)):
        p = out / name
        p.write_text(json.dumps(payload, indent=1))
        files.append(p)
    return files
