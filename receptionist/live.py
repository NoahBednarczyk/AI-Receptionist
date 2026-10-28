"""Live phone calls through Retell's custom-LLM WebSocket.

Retell runs the phone line, speech recognition and the voice. Every reply comes from our CallSession, so the
guards in agent.py (fixed lines, the spoken safety hang-up, the short-number note, Spanish) apply on real calls.
Protocol: https://docs.retellai.com/api-references/llm-websocket

Two tasks share the socket: a reader that answers Retell's pings at once (Retell reconnects after 5 s without
one, and a turn with tool calls can take longer), and a worker that runs one caller turn at a time.

Retell often asks for a reply, then asks again a moment later because the caller kept talking or the transcript
was corrected ("Leaking room" -> "Leaking roof"). Only the newest request's reply is ever played, so the worker
drops a turn the moment a newer request arrives (unless it already booked or sent something), and takes back a
finished reply the caller never heard. Without that, stale turns delayed every reply and the agent asked twice.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable

from .agent import CallSession, TurnControl, TurnDropped

log = logging.getLogger("receptionist")

REMINDER = {"en": "Are you still there?", "es": "¿Sigue ahí?"}
NOT_HEARD = {"en": "Sorry, I didn't catch that. Could you say that again?",
             "es": "Perdón, no le entendí. ¿Me lo puede repetir?"}
STILL_WORKING = {"en": ["Hmm.", "Let's see.", "Okay, so.", "Mm, let me think.", "Alright, let's see here.",
                        "Bear with me.", "Okay, looking at that."],
                 "es": ["A ver.", "Mm, déjeme ver.", "Veamos.", "Bueno, a ver."]}
SLOW_REPLY_S = 3.0   # no words yet after this long: a short thinking sound, never the same one twice running
GARBLED = re.compile(r"^[\s(\[]*(unintelligible|inaudible|no speech|silence)[^)\]]*[)\]]?[\s.]*$", re.I)
TROUBLE = {"en": "I'm sorry, we're having a technical problem. Please call back in a few minutes.",
           "es": "Lo siento, tenemos un problema técnico. Por favor llame de nuevo en unos minutos."}


def new_caller_text(transcript: list[dict] | None) -> str:
    """What the caller said since the agent last spoke (several user entries in a row are joined)."""
    said = []
    for entry in reversed(transcript or []):
        if entry.get("role") == "agent":
            break
        if entry.get("role") == "user" and (entry.get("content") or "").strip():
            said.append(entry["content"].strip())
    return " ".join(reversed(said))


def caller_lines(transcript: list[dict] | None) -> list[str]:
    return [e["content"].strip() for e in transcript or [] if e.get("role") == "user" and (e.get("content") or "").strip()]


CONFIRMATION = re.compile(r"\b([A-Z])(\d{4})\b")


def for_voice(text: str) -> str:
    """Spoken form for the voice: a confirmation like A1007 is read digit by digit with pauses, not as a number."""
    return CONFIRMATION.sub(lambda m: f"{m.group(1)}, " + ", ".join(m.group(2)), text)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def cut_short(heard: str, said: str) -> str | None:
    """What the caller heard of our last reply if they talked over it partway, else None. Retell's transcript keeps
    only the part of an interrupted reply that was played (its last word may be cut mid-way)."""
    for line in [x for pool in STILL_WORKING.values() for x in pool]:   # played before the reply itself, so not part of what the session said
        if heard.strip().startswith(line):
            heard = heard.strip()[len(line):]
    h, s = _words(heard), _words(said)
    if not h or len(h) >= len(s) - 2 or h[:-1] != s[:len(h) - 1]:
        return None
    return heard.strip()


async def run_retell_call(receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]],
                          start_session: Callable[[dict], CallSession | None],
                          finish: Callable[[CallSession], None]) -> None:
    """Drive one call. `start_session` builds the CallSession from Retell's call details (None rejects the call);
    `finish` stores the transcript afterwards. Both run in a worker thread."""
    loop = asyncio.get_running_loop()
    lock = asyncio.Lock()
    requests: asyncio.Queue = asyncio.Queue()
    newer = asyncio.Event()   # a reply request arrived after the one being answered
    session: CallSession | None = None
    heard_before: int | None = None   # agent lines in Retell's transcript when the last kept turn was asked for
    consumed = 0   # caller lines in Retell's transcript that kept turns have already answered
    consumed_prev = 0   # the same, before the last kept turn (for taking it back)

    async def say(message: dict) -> None:
        async with lock:   # the reader and the worker both send
            await send(message)

    async def reader() -> None:
        nonlocal session
        while True:
            event = await receive()
            kind = event.get("interaction_type")
            if kind == "ping_pong":
                await say({"response_type": "ping_pong", "timestamp": event.get("timestamp")})
            elif kind == "call_details" and session is None:
                session = await loop.run_in_executor(None, start_session, event.get("call") or {})
                if session is None:
                    await say({"response_type": "response", "response_id": 0, "content": "", "content_complete": True,
                               "end_call": True})
                    return
                await say({"response_type": "response", "response_id": 0, "content": session.greeting,
                           "content_complete": True})
            elif kind in ("response_required", "reminder_required"):
                await requests.put(event)
                if kind == "response_required":
                    newer.set()

    async def worker() -> None:
        nonlocal heard_before, consumed, consumed_prev
        while True:
            event = await requests.get()
            while not requests.empty():   # Retell discards older requests, so answer only the newest
                event = requests.get_nowait()
            newer.clear()
            if session is None:
                continue
            rid = event.get("response_id", 0)
            if session.ended:   # already said goodbye: say nothing more, just hang up
                await say({"response_type": "response", "response_id": rid, "content": "", "content_complete": True,
                           "end_call": True})
                continue
            if event["interaction_type"] == "reminder_required":
                heard_before = None
                await say({"response_type": "response", "response_id": rid,
                           "content": REMINDER[session.ctx.language], "content_complete": True})
                continue
            transcript = event.get("transcript") or []
            heard = sum(1 for e in transcript if e.get("role") == "agent")
            heard_only = None
            if heard_before is not None and heard <= heard_before and session.revert_last():
                # Our last reply never played, so the caller's words since the last line they heard (which
                # new_caller_text joins) are all still unanswered.
                log.info("call %s: took back a reply the caller never heard", session.call_id)
                consumed, heard_before = consumed_prev, None
            elif heard_before is not None:
                last_line = next((e.get("content") or "" for e in reversed(transcript) if e.get("role") == "agent"), "")
                heard_only = cut_short(last_line, session.last_reply())
            lines = caller_lines(transcript)
            # Everything the caller said that no kept turn has answered: a dropped turn must not lose their words,
            # and Retell's transcript puts pieces of interrupted replies between them.
            said = " ".join(lines[consumed:]) or new_caller_text(transcript)
            if not said or GARBLED.match(said):   # answer at once instead of waiting on the model
                heard_before, consumed = None, len(lines)
                await say({"response_type": "response", "response_id": rid,
                           "content": NOT_HEARD[session.ctx.language], "content_complete": True})
                continue
            before = heard_before, consumed
            heard_before, consumed = heard, len(lines)
            if await answer(rid, said, heard_only):
                consumed_prev = before[1]
            else:   # dropped: the session is as it was, so are we
                heard_before, consumed = before

    last_still = []

    def still_working(lang: str) -> str:
        pool = [x for x in STILL_WORKING[lang] if x not in last_still] or STILL_WORKING[lang]
        line = random.choice(pool)
        last_still[:] = [line]
        return line

    async def answer(rid: int, caller_text: str, heard_only: str | None = None) -> bool:
        """Plays the reply to the caller's words. False when the turn was dropped because they kept talking."""
        chunks: asyncio.Queue = asyncio.Queue()
        control = TurnControl()
        turn = loop.run_in_executor(None, session.respond, caller_text,
                                    lambda chunk: loop.call_soon_threadsafe(chunks.put_nowait, chunk), control,
                                    heard_only)
        turn.add_done_callback(lambda f: f.cancelled() or f.exception())   # a dropped turn's error is expected
        started, spoke, first = loop.time(), False, None
        newer_wait = asyncio.ensure_future(newer.wait())
        try:
            while True:
                getter = asyncio.ensure_future(chunks.get())
                wait = None if spoke else max(0.0, SLOW_REPLY_S - (loop.time() - started))
                done, _ = await asyncio.wait({getter, turn, newer_wait}, timeout=wait,
                                             return_when=asyncio.FIRST_COMPLETED)
                if newer_wait in done and not turn.done():
                    getter.cancel()
                    if control.drop():   # the newer request gets a fresh turn right away
                        log.info("call %s: dropped reply %s, the caller kept talking", session.call_id, rid)
                        return False
                    await asyncio.wait({turn})   # it already booked or sent something: let it finish first
                    break
                if not done:
                    getter.cancel()
                    spoke = True
                    await say({"response_type": "response", "response_id": rid,
                               "content": still_working(session.ctx.language) + " ", "content_complete": False})
                    continue
                if getter in done:
                    if first is None:
                        first = loop.time() - started
                    spoke = True
                    await say({"response_type": "response", "response_id": rid, "content": for_voice(getter.result()),
                               "content_complete": False})
                    continue
                getter.cancel()
                if turn in done:
                    break
        finally:
            newer_wait.cancel()
        log.info("call %s: reply %s, first words %s, done %.2fs", session.call_id, rid,
                 f"{first:.2f}s" if first is not None else "none", loop.time() - started)
        while not chunks.empty():
            await say({"response_type": "response", "response_id": rid, "content": for_voice(chunks.get_nowait()),
                       "content_complete": False})
        final = {"response_type": "response", "response_id": rid, "content": "", "content_complete": True}
        try:
            turn.result()
        except TurnDropped:
            return False
        except Exception:
            log.exception("turn failed on call %s", session.call_id)
            final.update(content=TROUBLE[session.ctx.language], end_call=True)
        ctx = session.ctx
        if ctx.transferred_to:
            final["transfer_number"] = ctx.biz["transfer_targets"][ctx.transferred_to]["number"]
        elif ctx.ended:
            final["end_call"] = True
        await say(final)
        return True

    await say({"response_type": "config", "config": {"auto_reconnect": True, "call_details": True}})
    tasks = [asyncio.create_task(reader()), asyncio.create_task(worker())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)   # the reader ends when the call hangs up
    finally:
        for t in tasks:
            t.cancel()
        if session is not None:
            await loop.run_in_executor(None, finish, session)
