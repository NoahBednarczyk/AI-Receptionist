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
from .speech import normalize_phone, spoken_phone
from .tools import ToolError

log = logging.getLogger("receptionist")

# The opener is only a short "is this the owner?" so the person answers first. Who is calling comes in the next reply
# (the model says it, see INTRO_NOTE); INTRO is the fixed fallback if noise cuts the exchange short.
GREETINGS = ["Hi, is this {who}?", "Hello, is this {who}?", "Hey there, am I speaking with {who}?"]
INTRO = ["Hi, I'm {agent} with {company} here in {town}.",
         "It's {agent} with {company} in {town}."]


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
    fill = dict(agent=s["agent_name"], company=s["company"], town=s["town"])
    return rng.choice(GREETINGS).format(who=who), rng.choice(INTRO).format(**fill)


def opener(lead: dict, s: dict, rng=random) -> str:
    return opener_parts(lead, s, rng)[0]


STOP_LINES = ["Understood, I'll take you off our list. Sorry for the interruption.",
              "Got it, you're off our list. Sorry to bother you.",
              "No problem at all, I'll take you off the list. Have a good one."]
INTRO_NOTE = ("you just asked who you are speaking with and they have answered. In this reply, in the same short breath, say your name and that you "
              "are with Ringback in Sandpoint, and go straight to your first question about their calls, warmly. Do not say it is a sales call "
              "or a cold call, do not bring up being an AI unless they ask, and no lead-in about why you are calling. If they said it is a wrong "
              "person, a bad time or asked who you are, handle that first.")
AI_NOTE = ("the caller is asking whether you are a robot, an AI or a real person. Answer with one quick, funny line of your own "
           "that says yes, you are an AI assistant, unmistakably (the joke comes with the yes, never instead of it), then use it "
           "in the same breath to get back to your goal. Never claim to be human.")

# Words that end the call by themselves; the model is not asked. The softer "don't call again" is skipped when the
# caller is only picking another time ("don't call me today, try Thursday").
HARD_STOP = re.compile(r"\b(stop calling|quit calling|never call|do not call|take (me|us|this number|my number) off|"
                       r"remove (me|us|my number|this number)|(put|add) (me|us|this number) on (the |your )?(do[- ]not[- ]call|dnc)|"
                       r"leave (me|us) alone|(lose|delete|forget) (my|this|our) (number|info)|stop (bothering|harassing|texting)( me| us)?|"
                       r"(quit|stop) (it|that)|get off my phone|(i|we)'?m on the (do[- ]not[- ]call|dnc))\b", re.I)
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
        if not self._opened and not self.ctx.ended:   # their first words ("Hello?"): the opener answers, no model call
            self._opened = True
            if on_text:
                on_text(self.hook_text)
            messages = [{"role": "user", "content": caller_text}, {"role": "assistant", "content": self.opener}]
            self._save(messages, caller_text, {"role": "agent", "text": self.opener, "tools": [], "steps": [{"say": self.opener}]}, None, final=False)
            return Turn(self.opener, [], 0.0, 0.0, False)
        if not self._intro_checked:   # the first model turn after the opener
            self._intro_checked = True
            note = f"{note} {INTRO_NOTE}" if note else INTRO_NOTE
        if asked_if_ai(caller_text):
            note = f"{note} {AI_NOTE}" if note else AI_NOTE
        tone = callreview.tone_note(caller_text)
        if tone:
            note = f"{note} {tone}" if note else tone
        return super().respond(caller_text, on_text, control, heard_only, note)

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
        log = self.ctx.extra.get("log") or callreview.fallback_log(self.transcript, self.lead, reason)
        row = outbound.call_row(self.lead, log, self.started, minutes, self.state.next_call_number(), self.cfg,
                                transcript_text(self.transcript, with_tools=False))
        row["estCost"] = outbound.est_cost(minutes, outbound.settings(self.cfg))
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
    voicemail = (f"Hi, this is {s['agent_name']}, calling for {s['founder']} at {s['company']} in {s['town']}. "
                 f"We're building a phone assistant for lawn care and snow removal businesses and {s['founder']} would like ten minutes "
                 f"of your time. You can try our demo line any time at {spoken_phone(s['demo_line'])}. Thanks, and have a good day.")
    return {"agent_name": f"{s['company']} discovery caller",
            "response_engine": {"type": "custom-llm", "llm_websocket_url": f"{ws}/llm-websocket-out/{secret}"},
            **(voice or {}),
            "boosted_keywords": [s["agent_name"], s["company"], s["founder"], s["town"], "lawn care", "landscaping", "snow removal", "plowing", "voicemail"],
            "webhook_url": f"{base}/retell/webhook", "language": "en-US",
            "voicemail_option": {"action": {"type": "static_text", "text": voicemail}},
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
    rows = outbound.plan_queue(leads, now, cfg, history, state.dnc())
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
    ok, why = outbound.can_call(lead, now, cfg, state.attempts(normalize_phone(lead.get("phone") or "") or ""), state.dnc())
    if not ok:
        raise RuntimeError(f"Not calling {lead.get('name')}: {why}")
    rec = json.loads((Path(cfg["data_dir"]) / "retell_outbound.json").read_text())
    if not rec.get("number"):
        raise RuntimeError("No outbound number yet: run `outbound-number --number +1...` once you have one from Retell.")
    warm_in_background(warm_fn)   # the ringing time pays for the prompt prefill
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
                              "priority": 1, "done": False, "doneAt": None, "createdAt": int(time.time() * 1000), "notes": f"Call {b['phone']} at {b['start']}"}
             for i, b in enumerate(data.get("bookings", []))}
    files = []
    for name, payload in (("outbound-calls.json", calls), ("outbound-leads.json", data.get("lead_updates", {})),
                          ("outbound-tasks.json", tasks)):
        p = out / name
        p.write_text(json.dumps(payload, indent=1))
        files.append(p)
    return files
