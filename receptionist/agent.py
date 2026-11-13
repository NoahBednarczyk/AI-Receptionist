"""One phone call as an agent loop: the caller speaks, the agent calls tools as needed, then replies.

The same prompt, tools and model run here (text chat and simulations) and on the voice platforms,
so a scenario that passes here is testing the production agent's brain.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import anthropic
import httpx

from . import degraded, prompts, safety, schedule, tools
from .speech import channel_guard, looks_spanish, redact_card, short_phone_numbers, unspeakable
from .store import Store

# What a failed or stalled model call looks like (the SDK wraps most of it as APIError; a dropped stream is raw httpx).
try:
    import httpx2   # newer SDKs bring their own HTTP layer
    MODEL_FAILURES = (anthropic.APIError, httpx.HTTPError, httpx2.HTTPError)
except ImportError:
    MODEL_FAILURES = (anthropic.APIError, httpx.HTTPError)

# Tools whose effects stay after the call (or end it): a turn that has run one is kept, never dropped or reverted.
IRREVERSIBLE = {"book_appointment", "reschedule_appointment", "cancel_appointment", "create_lead", "take_message",
                "transfer_call", "end_call"}
REFUSAL_LINE = "I'm sorry, I can't help with that on this line. I can take a message for the office."
REPEAT_LINE = "Sorry, could you say that again?"
REPEAT_VARIANTS = [REPEAT_LINE, "Sorry, say that one more time?", "I lost you there, could you repeat that?",
                   "Come again?", "Sorry, what was that?"]
# The first short clause of a reply ("Got it,") can go to the voice before the sentence ends, which starts the speech
# a few hundred milliseconds sooner. Two to eight words up to a comma, with more text already following.
FIRST_CLAUSE = re.compile(r"^((?:\S+\s+){1,7}\S+,)\s+(?=\S)")
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\s*\n+\s*")
# Said once a call; later repeats are dropped ("Is there anything else I can help with?" after every answer).
ANYTHING_ELSE = re.compile(r"\banything else (?:i|we) can (?:help|do)|^anything else\b|"
                           r"algo más en (?:que|lo que) (?:le )?pueda|(?:ayudarle|ayudar) (?:con|en) algo más", re.I)
# Speech that isn't meant for the agent (the caller talking to someone next to them): the model answers with this
# marker instead of words, and nothing is spoken.
LISTENING = "(listening)"
# A short acknowledgement ("Perfect.", "Alright,", "Sure thing.") at the start of a sentence. It must end in punctuation
# so that "Right now we have..." is not mistaken for one.
ACK = re.compile(r"^(?P<ack>al(?:l ?|)right|okay|ok|sure thing|sure|perfect|sounds good|you bet|gotcha|got it|right|makes sense|"
                 r"yep|yeah|done|will do|understood|great|awesome|absolutely|of course|no problem|solid|nice|cool|excellent|"
                 r"wonderful|fair enough|thanks|thank you|mm-?hmm|mm|ah|huh)(?:[,.!;]+|$)\s*(?P<rest>.*)$", re.I | re.S)
RECENT_ACKS = 3   # an acknowledgement can't come back until three others have been used

log = logging.getLogger("receptionist")
SILENT = re.compile(r"^\W*\(?\s*listening\s*\)?\W*$", re.I)
# Sent once when everything the model said this turn was held back, so the caller isn't left with silence.
NUDGE = ("(System note: the caller heard nothing from you this turn. Reply to them now in one or two short "
         "sentences, without explaining what you're doing or why.)")
NUDGE_SAY = ("(System note: the caller heard nothing from you this turn. Reply to them now in one or two short "
             "sentences, without explaining what you're doing or why. Reply now inside <say>...</say>.)")
SAY_TAG = re.compile(r"<say>(.*?)(?:</say>|\Z)", re.S)


def say_text(raw: str) -> str:
    """What a reply written in the <say> format actually says (for the one-shot calls that do not stream)."""
    return " ".join(m.strip() for m in SAY_TAG.findall(raw or "") if m.strip())


class SayChannel:
    """Splits the model's streamed text into what is said (inside <say>...</say>) and what is private (everything else).
    Only the said part reaches `Sentences`; the private part is thrown away. Tags may arrive split across deltas, so a
    buffer tail that could be the start of the tag being looked for is held back. An unclosed <say> ends with the block."""
    OPEN, CLOSE = "<say>", "</say>"

    def __init__(self, words: "Sentences"):
        self.words, self.privates, self.began = words, [], []   # began: per text block, did it open with <say>
        self._reset()

    def _reset(self) -> None:
        self.inside, self.buf, self.private, self.spoke, self.head = False, "", "", False, ""

    @property
    def dirty(self) -> bool:
        return bool(self.inside or self.buf or self.private)

    def _out(self, text: str) -> None:
        if not text:
            return
        if self.inside:
            self.spoke = self.spoke or bool(text.strip())
            self.words.add(text)
        else:
            self.private += text

    def feed(self, text: str) -> None:
        if len(self.head) < 12:
            self.head += text
        self.buf += text
        while True:
            tag = self.CLOSE if self.inside else self.OPEN
            i = self.buf.find(tag)
            if i >= 0:
                self._out(self.buf[:i])
                self.buf = self.buf[i + len(tag):]
                if self.inside:
                    self.words.add("\n")   # the end of a <say> is the end of its last sentence
                self.inside = not self.inside
                continue
            keep = next((k for k in range(min(len(tag) - 1, len(self.buf)), 0, -1) if tag.startswith(self.buf[-k:])), 0)
            self._out(self.buf[:len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            return

    def close(self) -> str:
        """End of a text block: flush, note the private part, and return what was said of the block."""
        self._out(self.buf)
        self.buf = ""
        note = self.private.strip()
        if note:
            if not self.spoke and SILENT.match(note):
                self.words.silent = True
            else:
                self.privates.append(note)
                log.info("private (not spoken): %s", note)
        spoke = self.spoke
        if self.head.strip():
            self.began.append(self.head.lstrip().startswith(self.OPEN))
        self._reset()
        said = self.words.close()
        return said if spoke else ""


class Sentences:
    """Holds the model's words until each sentence is complete, then passes it on unless it's thinking out loud
    or names internals (speech.unspeakable), or would make the reply too long. Text-to-speech starts on whole
    sentences anyway, so holding one back costs little, and a dropped sentence is never heard.

    `budget` caps the words in one reply (the speech between tool calls): the first sentence always goes, and
    once a sentence would pass the cap, it and the rest of that reply wait, so the agent answers and lets the
    caller talk instead of stacking an answer, a read-back and a question. History keeps only what was said,
    and `held` (the sentences cut for length) goes to the model with the caller's next words, so it knows what
    it still has to say instead of assuming it asked."""

    def __init__(self, say: Callable[[str], None], budget: int | None = None, said_once: set | None = None,
                 recent: list | None = None):
        self.say, self.buf, self.kept, self.dropped, self.held = say, "", [], [], []
        self.recent = list(recent or ())   # acknowledgements used lately this call, oldest first
        self.silent = False   # the model chose to stay quiet (LISTENING)
        self.tainted = False  # a sentence of this text block was thinking out loud: the rest of the block is dropped too
        self.budget, self.used, self.cut = budget, 0, False
        self.said_once = set(said_once or ())
        self.channel = False   # speech is inside <say>: only speech.channel_guard drops a sentence (see SayChannel)
        self.shadow: list[str] = []   # channel mode: sentences the old blocklist would have dropped but were spoken

    def new_reply(self) -> None:
        """A tool call starts a new reply: the caller hears a pause in between."""
        self.used, self.cut = 0, False
        self.tainted = False

    def add(self, text: str) -> None:
        *done, self.buf = SENTENCE_BREAK.split(self.buf + text)
        for sentence in done:
            self._release(sentence)
        if not done and not self.used and not self.cut:   # nothing said yet this reply: start with the first clause
            m = FIRST_CLAUSE.match(self.buf)
            if m:
                self.buf = self.buf[m.end():]
                self._release(m.group(1))

    def close(self) -> str:
        """End of a text block: release the rest and return what was said of the block."""
        self._release(self.buf, final=True)
        said, self.buf, self.kept = " ".join(self.kept), "", []
        self.tainted = False
        return said

    def _fresh(self, sentence: str, final: bool) -> str | None:
        """Drops an acknowledgement the caller heard a moment ago ("Perfect." again) so a call doesn't repeat itself.
        A lone repeat goes (unless it is all there is to say); a repeat leading a longer sentence is stripped."""
        m = ACK.match(sentence)
        if not m:
            return sentence
        ack, rest = re.sub(r"\W", "", m.group("ack").lower()), m.group("rest").strip()
        if ack not in self.recent:
            self.recent = (self.recent + [ack])[-RECENT_ACKS:]
            return sentence
        if rest:
            self.dropped.append(sentence)      # history then keeps only what was said
            return rest[0].upper() + rest[1:]
        return sentence if final and not self.kept and not self.used else None

    def _release(self, sentence: str, final: bool = False) -> None:
        sentence = sentence.strip()
        if not sentence:
            return
        if self.tainted:   # reasoning comes in runs: once one sentence of a block was caught, the rest of it is dropped unheard
            self.dropped.append(sentence)
            return
        fresh = self._fresh(sentence, final)
        if fresh is None:
            self.dropped.append(sentence)
            return
        sentence = fresh
        if SILENT.match(sentence):
            self.silent = True
            return
        words = len(sentence.split())
        repeat = bool(ANYTHING_ELSE.search(sentence)) and "anything_else" in self.said_once
        if self.budget and self.used and self.used + words > self.budget:
            self.cut = True
        if self.channel:
            caught = channel_guard(sentence)
            if not caught and unspeakable(sentence) and not self.cut and not repeat:
                self.shadow.append(sentence)
                log.warning("shadow: blocklist would drop %r", sentence)
        else:
            caught = unspeakable(sentence)
        if caught:
            self.tainted = True
        if self.cut or repeat or caught:
            self.dropped.append(sentence)
            if self.cut and not repeat and not caught:
                self.held.append(sentence)
            return
        if ANYTHING_ELSE.search(sentence):
            self.said_once.add("anything_else")
        self.used += words
        self.kept.append(sentence)
        self.say(sentence)


class TurnDropped(Exception):
    """The live call dropped this turn before it did anything irreversible; the session is unchanged."""


class TurnControl:
    """Lets a live call drop a turn the caller talked over. Dropping works until the turn does something
    irreversible (IRREVERSIBLE tools, or saving itself at the end); from then on the turn finishes and is kept."""

    def __init__(self):
        self._lock = threading.Lock()
        self.dropped = False
        self.locked_in = False

    def drop(self) -> bool:
        with self._lock:
            if not self.locked_in:
                self.dropped = True
            return self.dropped

    def proceed(self, irreversible: bool = False) -> bool:
        with self._lock:
            if self.dropped:
                return False
            self.locked_in = self.locked_in or irreversible
            return True


@dataclass
class Turn:
    text: str
    tools: list
    first_text_s: float | None   # time until the first word could reach text-to-speech
    total_s: float
    ended: bool


class CallSession:
    taker: degraded.MessageTaker | None = None   # set when the model fails: code takes messages (degraded.py)

    def __init__(self, client: anthropic.Anthropic, cfg: dict, biz: dict, store: Store, now: datetime,
                 caller: str | None = None, call_id: str | None = None, platform: str = "text",
                 transfer_outcome: str = "connected", dialed: str | None = None, real_transfers: bool = False):
        self.client, self.cfg, self.biz, self.store = client, cfg, biz, store
        self.channel = prompts.channel_on(cfg)   # the model's speech goes inside <say> tags
        self.greeting = prompts.pick_greeting(biz, now)   # varies from call to call when the business lists several
        self.call_id = call_id or f"{platform}-{uuid.uuid4().hex[:10]}"
        customer = store.get_customer(biz["id"], caller)
        upcoming = store.upcoming_appointments(biz["id"], caller, now) if customer else []
        # Stable layers first with the cache breakpoint; the per-call state comes after it.
        self.system = [
            {"type": "text", "text": prompts.stable_prompt(biz, self.channel), "cache_control": cache_control(cfg)},
            {"type": "text", "text": prompts.call_state(biz, now, caller, customer, upcoming, dialed,
                                                         self.greeting)},
        ]
        self.tools = [{**t, "strict": True} for t in tools.schemas(biz)]
        self.ctx = tools.ToolContext(biz, store, now, caller, self.call_id, transfer_outcome,
                                     real_transfers=real_transfers)
        self.messages: list[dict] = []
        self.transcript: list[dict] = [{"role": "agent", "text": self.greeting}]
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self._before = None   # see revert_last()
        self._said_once: set = set()   # once-a-call lines already said (see Sentences)
        self._recent_acks: list = []   # acknowledgements used lately, so a call doesn't keep saying "Perfect."
        self._held_back: list[str] = []   # sentences of the last reply cut for length (see Sentences)
        self._interjections: list[str] = []   # lines we cut in with while the caller was talking (see interject)
        store.start_call(self.call_id, biz["id"], platform, caller, now)

    @property
    def ended(self) -> bool:
        return self.ctx.ended

    @property
    def can_degrade(self) -> bool:
        """Only the inbound receptionist falls back to taking a message; subclasses (the sales caller) keep their own
        failure handling."""
        return type(self) is CallSession

    def degrade(self) -> None:
        """The model can't be used for the rest of this call: code takes a message instead."""
        if self.taker is None:
            log.error("call %s: switching to the code-driven message taker", self.call_id)
            self.taker = degraded.MessageTaker(self.ctx.language)

    def _taker_step(self, caller_text: str, emergency, go_on) -> tuple[list[str], list[dict], bool]:
        """What the message taker says to `caller_text`, plus the tool calls it made. The safety line, if the caller
        just described an emergency, comes first."""
        t = self.taker
        t.lang = self.ctx.language
        parts, calls = [], []
        if emergency:
            parts.append(emergency[1])
        if emergency and t.state not in ("start",):
            line, done = t.last, False   # the safety line interrupts; then the question they were on again
        else:
            line, done = t.step(caller_text)
        parts.append(line)
        if done:
            go_on(irreversible=True)
            number = t.number or self.ctx.caller
            args = {"caller_name": t.name or "Unknown caller", "callback_number": number or "", "message": t.message(),
                    "urgent": True}
            if number:
                result, err = tools.run_tool(self.ctx, "take_message", args)
            else:   # no number at all: keep what we have, it still tells the office who rang and why
                self.store.add_message(self.biz["id"], self.call_id, args["caller_name"], "no number given",
                                       args["message"], "office", True, self.ctx.now)
                result, err = {"status": "saved"}, False
                self.store.log_tool(self.call_id, "take_message", args, result, self.ctx.now)
            calls.append({"name": "take_message", "args": args, "result": result, "error": err})
            result, err = tools.run_tool(self.ctx, "end_call", {"reason": "completed"})
            calls.append({"name": "end_call", "args": {"reason": "completed"}, "result": result, "error": err})
        return parts, calls, done

    def _taker_turn(self, messages: list[dict], caller_text: str, emergency, emit, go_on) -> Turn:
        parts, calls, done = self._taker_step(caller_text, emergency, go_on)
        if not done:
            go_on()
        for p in parts:
            emit(p, True)
        line = " ".join(parts)
        steps = [{"say": p} for p in parts] + [{"tool": c} for c in calls]
        messages += [{"role": "user", "content": caller_text}, {"role": "assistant", "content": self.tag(line)}]
        self._save(messages, caller_text, {"role": "agent", "text": line, "tools": calls, "steps": steps,
                                           "degraded": True}, emergency, final=done)
        return Turn(line, calls, 0.0, 0.0, self.ctx.ended)

    def tag(self, text: str) -> str:
        """Lines our own code speaks and saves as the agent's turns are saved in the same <say> form the model writes, so
        the model never sees an untagged example of itself."""
        return f"<say>{text}</say>" if self.channel else text

    def last_reply(self) -> str:
        """What the agent said in its last turn (the greeting before the caller first speaks)."""
        return next((t["text"] for t in reversed(self.transcript) if t["role"] == "agent"), "")

    def respond(self, caller_text: str, on_text: Callable[[str], None] | None = None,
                control: TurnControl | None = None, heard_only: str | None = None, note: str | None = None) -> Turn:
        """One caller turn. `on_text` receives the reply as it's produced (fixed lines and the model's words),
        so a live call can start speaking before the turn is over. The turn works on its own copy of the history
        and saves it at the end, so a live call can drop it (`control`) if the caller talks over it first; a
        dropped turn raises TurnDropped and leaves the session as it was. `heard_only` is what the caller actually
        heard of the last reply when they cut it off partway (live calls)."""
        if self.ctx.ended:   # the goodbye is playing; anything said now gets no answer
            return Turn("", [], None, 0.0, True)
        caller_text = redact_card(caller_text)
        m, a = self.cfg["models"], self.cfg["agent"]
        said = False   # anything streamed to on_text this turn

        def emit(chunk: str, new_segment: bool) -> None:
            nonlocal said
            if not on_text or not chunk:
                return
            on_text((" " if new_segment and said else "") + (chunk.lstrip() if new_segment else chunk))
            said = said or bool(chunk.strip())

        def go_on(irreversible: bool = False) -> None:
            if control and not control.proceed(irreversible):
                raise TurnDropped

        if self.ctx.language == "en" and looks_spanish(caller_text):
            self.ctx.language = "es"   # sticky: fixed lines, spoken dates and texts switch to Spanish
        fillers = tools.FILLERS[self.ctx.language]
        messages = list(self.messages)
        content = caller_text
        emergency = None if self.ctx.safety_given else safety.match(
            self.biz.get("vertical", ""), caller_text, self.ctx.language,
            schedule.in_heating_season(self.biz, self.ctx.now.date()))
        short = short_phone_numbers(caller_text)
        if short:   # speech recognition often drops a digit; the model tends to fill it in, so say so plainly
            content += (f"\n\n(System note: the phone number the caller just said has only {len(short[0])} digits, "
                        f"{short[0]}. Say you missed part of it and ask for the full number.)")
        if heard_only:   # the caller talked over the last reply, so they missed the rest of it
            content += (f"\n\n(System note: the caller cut in while you were talking; they heard only \"{heard_only}\" "
                        "Anything after that, they didn't hear. Like a polite person who was just interrupted, open with "
                        "a tiny reaction of a few words in your own words, such as a short 'oh' or 'sorry, go ahead' "
                        "(a different one each time), unless what they said already answers you or it would be awkward; "
                        "then deal with what they said, and don't repeat what they already heard.)")
        elif self._held_back:   # the end of the last reply was cut for length; the caller never heard it
            content += ("\n\n(System note: your last reply ran long, so the caller didn't hear this part of it: \""
                        + " ".join(self._held_back) + "\" If it's still needed, say it now, briefly.)")
        if note:   # extra context from the live call, such as the caller echoing a phrase back
            content += f"\n\n(System note: {note})"
        if self._interjections:   # we cut in while the caller was still talking; the model should know
            content += ("\n\n(System note: while the caller was still talking you cut in and said: \""
                        + " ".join(self._interjections) + "\" Take it into account and don't say it again.)")
            self._interjections = []
        if emergency:   # said by code, instantly and word for word; the model continues the emergency flow
            content += (f"\n\n(System note: you have already said the {emergency[0].replace('_', ' ')} safety line to "
                        f"the caller: \"{emergency[1]}\" Don't repeat it or describe the emergency; go straight to the "
                        "playbook's next step.)")
        if emergency and safety.must_leave(emergency[0], caller_text):
            # Gas or CO with the caller still inside: the playbook's whole turn is fixed, so no model call to wait on.
            go_on(irreversible=True)
            line = f"{emergency[1]} {safety.closing(emergency[0], self.ctx.language)}"
            emit(line, True)
            result, is_error = tools.run_tool(self.ctx, "end_call", {"reason": "safety"})
            call = {"name": "end_call", "args": {"reason": "safety"}, "result": result, "error": is_error}
            messages += [{"role": "user", "content": caller_text}, {"role": "assistant", "content": self.tag(line)}]
            self._save(messages, caller_text, {"role": "agent", "text": line, "tools": [call],
                                               "steps": [{"say": line}, {"tool": call}]}, emergency, final=True)
            return Turn(line, [call], 0.0, 0.0, True)
        if self.taker:   # the model failed earlier in this call
            return self._taker_turn(messages, caller_text, emergency, emit, go_on)
        messages.append({"role": "user", "content": content})
        spoken, used, steps = [], [], []   # steps keeps speech and tool calls in the order they happened
        start, first = time.monotonic(), None

        def speak(sentence: str) -> None:
            nonlocal first
            if first is None:
                first = time.monotonic() - start   # a whole sentence is ready for text-to-speech
            emit(sentence, True)

        words = Sentences(speak, a.get("reply_word_budget"), self._said_once, self._recent_acks)
        words.channel = self.channel
        chan = SayChannel(words) if self.channel else None
        if emergency:
            first = 0.0
            emit(emergency[1], True)
            spoken.append(emergency[1])
            steps.append({"say": emergency[1]})
        extra = thinking_param(m)
        nudged = False
        failed = None   # the model error that ended this turn, if any
        for _ in range(a["max_tool_rounds"]):
            go_on()
            try:
                with self.client.messages.stream(
                    model=m["agent"],
                    max_tokens=a["max_tokens"],
                    system=self.system,
                    tools=self.tools,
                    messages=messages,
                    output_config={"effort": m["agent_effort"]},
                    cache_control={"type": "ephemeral"},   # caches the call so far, so each turn reads only what's new
                    **extra,
                ) as stream:
                    said_in = {}   # text block index -> the part of it the caller heard
                    dropped_before = len(words.dropped)
                    self._filler_line = None   # the stall line this round actually played, if any
                    for ev in stream:
                        if control and control.dropped:
                            break   # closing the stream stops generation; go_on() below raises
                        if ev.type == "content_block_start" and ev.content_block.type == "tool_use":
                            if ev.content_block.name in fillers and not said and not spoken:
                                if first is None:
                                    first = time.monotonic() - start   # the filler line starts playing here
                                self._filler_line = tools.pick_filler(self.ctx, ev.content_block.name)
                                emit(self._filler_line, True)
                        elif ev.type == "content_block_delta" and ev.delta.type == "text_delta":
                            if chan:
                                chan.feed(ev.delta.text)
                            else:
                                words.add(ev.delta.text)
                        elif ev.type == "content_block_stop" and ev.content_block.type == "text":
                            said_in[ev.index] = (chan or words).close()
                    go_on()
                    msg = stream.get_final_message()
            except TurnDropped:
                raise
            except MODEL_FAILURES as e:   # the model is down or too slow: code takes the call from here (degraded.py)
                if not self.can_degrade:
                    raise
                failed = e
                break
            self._count(msg.usage)
            if words.buf.strip() or (chan and chan.dirty):   # cut off mid-block
                said_in[max((i for i, b in enumerate(msg.content) if b.type == "text"), default=0)] = (chan or words).close()
            text = " ".join(t for t in said_in.values() if t)

            if msg.stop_reason in ("refusal", "max_tokens"):
                # A refusal has nothing to say, and a cut-off tool call can't be answered: keep only speech.
                line = REFUSAL_LINE if msg.stop_reason == "refusal" else (text or random.choice(REPEAT_VARIANTS))
                if line != text:
                    emit(line, True)
                messages.append({"role": "assistant", "content": self.tag(line)})
                spoken.append(line)
                steps.append({"say": line})
                break

            content = msg.content
            if chan or len(words.dropped) > dropped_before:   # history holds only what the caller heard
                content = [{"type": "text", "text": self.tag(said_in[i])} if b.type == "text" else b
                           for i, b in enumerate(msg.content) if b.type != "text" or said_in.get(i)]
            if content:
                messages.append({"role": "assistant", "content": content})
            results = []
            for i, b in enumerate(msg.content):
                if b.type == "text" and said_in.get(i):
                    spoken.append(said_in[i])
                    steps.append({"say": said_in[i]})
                elif b.type == "tool_use" and msg.stop_reason == "tool_use":
                    go_on(irreversible=b.name in IRREVERSIBLE)
                    if not spoken and b.name == "end_call":
                        line = tools.end_line(self.ctx, (b.input or {}).get("reason"))
                        emit(line, True)
                        spoken.append(line)
                        steps.append({"say": line})
                    elif not spoken and b.name in fillers:
                        # The phone platforms play a fixed line while a tool runs; so do we, so the caller
                        # never waits through a tool call in silence. It's speech only, not model history.
                        line = self._filler_line or tools.pick_filler(self.ctx, b.name)
                        spoken.append(line)
                        steps.append({"say": line})
                    result, is_error = tools.run_tool(self.ctx, b.name, b.input)
                    call = {"name": b.name, "args": b.input, "result": result, "error": is_error}
                    used.append(call)
                    steps.append({"tool": call})
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(result, ensure_ascii=False), "is_error": is_error})
            if not results:
                if spoken or self.ctx.ended or nudged or words.silent:
                    break
                # Everything it said was held back (thinking out loud, a repeat): ask once for words to say.
                nudged = True
                last = messages[-1]
                nudge = NUDGE_SAY if chan else NUDGE
                if last["role"] == "user":   # nothing kept of the reply: add the note to the last user message
                    body = last["content"]
                    messages[-1] = {"role": "user", "content": (body + "\n\n" + nudge if isinstance(body, str)
                                                                else [*body, {"type": "text", "text": nudge}])}
                else:
                    messages.append({"role": "user", "content": nudge})
                continue
            messages.append({"role": "user", "content": results})
            words.new_reply()
            if self.ctx.ended:
                break

        if failed is not None:
            log.error("call %s: the model failed (%s: %s)", self.call_id, type(failed).__name__, failed)
            if any(c["name"] in IRREVERSIBLE for c in used) and not self.ctx.ended and not self.ctx.transferred_to:
                line = degraded.SAVED_ALREADY.get(self.ctx.language) or degraded.SAVED_ALREADY["en"]
                result, err = tools.run_tool(self.ctx, "end_call", {"reason": "completed"})
                used.append({"name": "end_call", "args": {"reason": "completed"}, "result": result, "error": err})
                parts = [line]
            elif self.ctx.ended or self.ctx.transferred_to:
                parts = []
            else:   # nothing irreversible happened: take a message
                self.degrade()
                parts, calls, _ = self._taker_step("", emergency if emergency and not spoken else None, go_on)
                used += calls
            for p in parts:
                emit(p, True)
                spoken.append(p)
                steps.append({"say": p})
        reply = " ".join(spoken)
        silent = words.silent and not reply   # the caller wasn't talking to us: say nothing, not even a retry line
        if not reply and not self.ctx.ended and not silent:
            reply = random.choice(REPEAT_VARIANTS)
            emit(reply, True)
            steps.append({"say": reply})
        if messages[-1]["role"] == "user":   # keep the history alternating, ending on what was said
            messages.append({"role": "assistant", "content": self.tag(reply) if reply else (LISTENING if silent else self.tag(REPEAT_LINE))})
        entry = {"role": "agent", "text": reply, "tools": used, "steps": steps}
        if words.dropped:
            entry["dropped"] = words.dropped   # for tests and reports; never spoken
        if chan and chan.privates:
            entry["private"] = chan.privates   # what the model wrote outside <say>; never spoken, kept for the report
        if chan:
            entry["began_with_say"] = chan.began
        if words.shadow:
            entry["shadow"] = words.shadow
        final = any(c["name"] in IRREVERSIBLE for c in used)
        go_on(irreversible=True)
        self._save(messages, caller_text, entry, emergency, final)
        self._said_once, self._held_back, self._recent_acks = words.said_once, words.held, words.recent
        return Turn(reply, used, first, time.monotonic() - start, self.ctx.ended)

    def _save(self, messages: list[dict], caller_text: str, entry: dict, emergency, final: bool) -> None:
        # The state before this turn, kept so revert_last() can take back a reply the caller never heard.
        self._before = None if final else (len(self.messages), len(self.transcript), self.ctx.safety_given,
                                           self._held_back, self._said_once, self._recent_acks)
        self.messages = messages
        self.transcript += [{"role": "caller", "text": caller_text}, entry]
        if emergency:
            self.ctx.safety_given = True

    def interject(self, caller_text: str) -> str | None:
        """A short line to cut in with while the caller is still going: the gist, then the one thing we need, or a
        direct answer to what they clearly asked. It never books or changes anything (tools are off) and it isn't saved
        to the model's history; the next turn is told about it instead (note_interjection)."""
        if self.ctx.ended:
            return None
        m = self.cfg["models"]
        ask = (f"{caller_text}\n\n(System note: the caller has been talking for a while and is still going. Cut in "
               "politely with ONE short spoken line of under 20 words, in your own words: show you caught the gist in a "
               "few words, then either answer what they clearly asked from the business facts or ask the single next "
               "thing you need. Don't use tools, don't book, don't promise anything, and don't mention this note."
               + (" Put the line inside <say>...</say>." if self.channel else "") + ")")
        try:
            resp = self.client.messages.create(
                model=m["agent"], max_tokens=120, system=self.system, tools=self.tools,
                tool_choice={"type": "none"},   # tools stay in the request so the cached prompt is reused
                messages=[*self.messages, {"role": "user", "content": ask}],
                output_config={"effort": m["agent_effort"]}, **thinking_param(m))
        except anthropic.APIError:
            return None
        self._count(resp.usage)
        said = " ".join(b.text for b in resp.content if b.type == "text").strip()
        if self.channel:
            said = say_text(said)
        if not said or unspeakable(said) or len(said.split()) > 30:
            return None
        return said

    def note_interjection(self, said: str) -> None:
        self._interjections.append(said)
        self.transcript.append({"role": "agent", "text": said, "interjection": True})

    def revert_last(self) -> bool:
        """Takes back the last turn when the caller never heard it (they talked over it before it played).
        Only turns that changed nothing outside the conversation (no booking, message, transfer or hang-up)."""
        if not self._before:
            return False
        n_messages, n_transcript, self.ctx.safety_given, self._held_back, self._said_once, self._recent_acks = self._before
        del self.messages[n_messages:], self.transcript[n_transcript:]
        self._before = None
        return True

    def _count(self, usage) -> None:
        self.usage["input"] += usage.input_tokens or 0
        self.usage["output"] += usage.output_tokens or 0
        self.usage["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.usage["cache_write"] += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def cost(self) -> float:
        return usage_cost(self.cfg, self.cfg["models"]["agent"], self.usage, cache_write_mult(self.cfg))


def make_client(cfg: dict | None = None) -> anthropic.Anthropic:
    """The Anthropic client with explicit limits: a model call that hangs must fail in seconds, not minutes, so a live
    call can fall back to the message taker (degraded.py). One retry covers a blip; the live socket adds its own cap."""
    c = (cfg or {}).get("anthropic", {})
    return anthropic.Anthropic(timeout=anthropic.Timeout(float(c.get("timeout_s", 20)), connect=float(c.get("connect_timeout_s", 5))),
                               max_retries=int(c.get("max_retries", 1)))


def thinking_param(models: dict) -> dict:
    """"adaptive" (the API default) lets the model think before it speaks; "disabled" (Haiku) or "between_tools"
    (Sonnet 5.5, which only writes short notes between tool calls) skip that wait for speed."""
    setting = models.get("agent_thinking", "adaptive")
    return {} if setting == "adaptive" else {"thinking": {"type": setting}}


def cache_control(cfg: dict) -> dict:
    """The breakpoint after the shop's stable prompt. One hour, so a call after a quiet stretch still finds it warm
    instead of waiting 3 to 4 seconds for the prompt to be read again (the default life is five minutes)."""
    ttl = cfg.get("agent", {}).get("cache_ttl", "1h")
    return {"type": "ephemeral"} if ttl == "5m" else {"type": "ephemeral", "ttl": ttl}


def cache_write_mult(cfg: dict) -> float:
    """Cache writes cost 1.25x the input rate for five minutes of life and 2x for an hour."""
    return 1.25 if cfg.get("agent", {}).get("cache_ttl", "1h") == "5m" else 2.0


def prewarm(client: anthropic.Anthropic, cfg: dict, biz: dict) -> dict:
    """Writes the agent's cached prefix (tools + stable prompt) once, before calls start in parallel, so they
    don't all pay the cold-cache wait together. max_tokens=0 runs prefill only: nothing is generated or billed
    as output. Returns the token usage for cost tracking."""
    m = cfg["models"]
    extra = thinking_param(m)
    resp = client.messages.create(
        model=m["agent"], max_tokens=0, output_config={"effort": m["agent_effort"]},
        system=[{"type": "text", "text": prompts.stable_prompt(biz, prompts.channel_on(cfg)), "cache_control": cache_control(cfg)}],
        tools=[{**t, "strict": True} for t in tools.schemas(biz)],
        messages=[{"role": "user", "content": "warmup"}], **extra,
    )
    return {"input": resp.usage.input_tokens or 0, "output": 0,
            "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0}


def usage_cost(cfg: dict, model: str, usage: dict, write_mult: float = 1.25) -> float:
    """Dollars, with cache writes at `write_mult` (1.25x for five minutes, 2x for an hour) and reads at 0.1x."""
    price_in, price_out = cfg["prices_per_mtok"].get(model, (0.0, 0.0))
    return (usage["input"] * price_in + usage["cache_write"] * price_in * write_mult
            + usage["cache_read"] * price_in * 0.1 + usage["output"] * price_out) / 1e6


def transcript_text(transcript: list[dict], with_tools: bool = True) -> str:
    def tool_line(c):
        return f"  [tool {c['name']} {json.dumps(c['args'])} -> {json.dumps(c['result'])}]"

    lines = []
    for t in transcript:
        if "steps" in t:   # our own calls: speech and tools in the order they happened
            for s in t["steps"]:
                if "say" in s:
                    lines.append(f"Agent: {s['say']}")
                elif with_tools:
                    lines.append(tool_line(s["tool"]))
            continue
        if with_tools:
            lines.extend(tool_line(c) for c in t.get("tools", []))
        if t["text"]:
            lines.append(f"{'Agent' if t['role'] == 'agent' else 'Caller'}: {t['text']}")
    return "\n".join(lines)
