"""One phone call as an agent loop: the caller speaks, the agent calls tools as needed, then replies.

The same prompt, tools and model run here (text chat and simulations) and on the voice platforms,
so a scenario that passes here is testing the production agent's brain.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import anthropic

from . import prompts, safety, tools
from .speech import looks_spanish, redact_card, short_phone_numbers, unspeakable
from .store import Store

# Tools whose effects stay after the call (or end it): a turn that has run one is kept, never dropped or reverted.
IRREVERSIBLE = {"book_appointment", "reschedule_appointment", "cancel_appointment", "create_lead", "take_message",
                "transfer_call", "end_call"}
REFUSAL_LINE = "I'm sorry, I can't help with that on this line. I can take a message for the office."
REPEAT_LINE = "Sorry, could you say that again?"
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
SILENT = re.compile(r"^\W*\(?\s*listening\s*\)?\W*$", re.I)
# Sent once when everything the model said this turn was held back, so the caller isn't left with silence.
NUDGE = ("(System note: the caller heard nothing from you this turn. Reply to them now in one or two short "
         "sentences, without explaining what you're doing or why.)")


class Sentences:
    """Holds the model's words until each sentence is complete, then passes it on unless it's thinking out loud
    or names internals (speech.unspeakable), or would make the reply too long. Text-to-speech starts on whole
    sentences anyway, so holding one back costs little, and a dropped sentence is never heard.

    `budget` caps the words in one reply (the speech between tool calls): the first sentence always goes, and
    once a sentence would pass the cap, it and the rest of that reply wait, so the agent answers and lets the
    caller talk instead of stacking an answer, a read-back and a question. History keeps only what was said,
    and `held` (the sentences cut for length) goes to the model with the caller's next words, so it knows what
    it still has to say instead of assuming it asked."""

    def __init__(self, say: Callable[[str], None], budget: int | None = None, said_once: set | None = None):
        self.say, self.buf, self.kept, self.dropped, self.held = say, "", [], [], []
        self.silent = False   # the model chose to stay quiet (LISTENING)
        self.budget, self.used, self.cut = budget, 0, False
        self.said_once = set(said_once or ())

    def new_reply(self) -> None:
        """A tool call starts a new reply: the caller hears a pause in between."""
        self.used, self.cut = 0, False

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
        self._release(self.buf)
        said, self.buf, self.kept = " ".join(self.kept), "", []
        return said

    def _release(self, sentence: str) -> None:
        sentence = sentence.strip()
        if not sentence:
            return
        if SILENT.match(sentence):
            self.silent = True
            return
        words = len(sentence.split())
        repeat = bool(ANYTHING_ELSE.search(sentence)) and "anything_else" in self.said_once
        if self.budget and self.used and self.used + words > self.budget:
            self.cut = True
        if self.cut or repeat or unspeakable(sentence):
            self.dropped.append(sentence)
            if self.cut and not repeat and not unspeakable(sentence):
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
    def __init__(self, client: anthropic.Anthropic, cfg: dict, biz: dict, store: Store, now: datetime,
                 caller: str | None = None, call_id: str | None = None, platform: str = "text",
                 transfer_outcome: str = "connected", dialed: str | None = None, real_transfers: bool = False):
        self.client, self.cfg, self.biz, self.store = client, cfg, biz, store
        self.greeting = prompts.pick_greeting(biz, now)   # varies from call to call when the business lists several
        self.call_id = call_id or f"{platform}-{uuid.uuid4().hex[:10]}"
        customer = store.get_customer(biz["id"], caller)
        upcoming = store.upcoming_appointments(biz["id"], caller, now) if customer else []
        # Stable layers first with the cache breakpoint; the per-call state comes after it.
        self.system = [
            {"type": "text", "text": prompts.stable_prompt(biz), "cache_control": {"type": "ephemeral"}},
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
        self._held_back: list[str] = []   # sentences of the last reply cut for length (see Sentences)
        store.start_call(self.call_id, biz["id"], platform, caller, now)

    @property
    def ended(self) -> bool:
        return self.ctx.ended

    def last_reply(self) -> str:
        """What the agent said in its last turn (the greeting before the caller first speaks)."""
        return next((t["text"] for t in reversed(self.transcript) if t["role"] == "agent"), "")

    def respond(self, caller_text: str, on_text: Callable[[str], None] | None = None,
                control: TurnControl | None = None, heard_only: str | None = None) -> Turn:
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
        emergency = None if self.ctx.safety_given else safety.match(self.biz.get("vertical", ""), caller_text,
                                                                    self.ctx.language)
        short = short_phone_numbers(caller_text)
        if short:   # speech recognition often drops a digit; the model tends to fill it in, so say so plainly
            content += (f"\n\n(System note: the phone number the caller just said has only {len(short[0])} digits, "
                        f"{short[0]}. Say you missed part of it and ask for the full number.)")
        if heard_only:   # the caller talked over the last reply, so they missed the rest of it
            content += (f"\n\n(System note: the caller cut in while you were talking; they heard only \"{heard_only}\" "
                        "Anything after that, they didn't hear.)")
        elif self._held_back:   # the end of the last reply was cut for length; the caller never heard it
            content += ("\n\n(System note: your last reply ran long, so the caller didn't hear this part of it: \""
                        + " ".join(self._held_back) + "\" If it's still needed, say it now, briefly.)")
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
            messages += [{"role": "user", "content": caller_text}, {"role": "assistant", "content": line}]
            self._save(messages, caller_text, {"role": "agent", "text": line, "tools": [call],
                                               "steps": [{"say": line}, {"tool": call}]}, emergency, final=True)
            return Turn(line, [call], 0.0, 0.0, True)
        messages.append({"role": "user", "content": content})
        spoken, used, steps = [], [], []   # steps keeps speech and tool calls in the order they happened
        start, first = time.monotonic(), None

        def speak(sentence: str) -> None:
            nonlocal first
            if first is None:
                first = time.monotonic() - start   # a whole sentence is ready for text-to-speech
            emit(sentence, True)

        words = Sentences(speak, a.get("reply_word_budget"), self._said_once)
        if emergency:
            first = 0.0
            emit(emergency[1], True)
            spoken.append(emergency[1])
            steps.append({"say": emergency[1]})
        extra = thinking_param(m)
        nudged = False
        for _ in range(a["max_tool_rounds"]):
            go_on()
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
                        words.add(ev.delta.text)
                    elif ev.type == "content_block_stop" and ev.content_block.type == "text":
                        said_in[ev.index] = words.close()
                go_on()
                msg = stream.get_final_message()
            self._count(msg.usage)
            if words.buf.strip():   # cut off mid-block
                said_in[max((i for i, b in enumerate(msg.content) if b.type == "text"), default=0)] = words.close()
            text = " ".join(t for t in said_in.values() if t)

            if msg.stop_reason in ("refusal", "max_tokens"):
                # A refusal has nothing to say, and a cut-off tool call can't be answered: keep only speech.
                line = REFUSAL_LINE if msg.stop_reason == "refusal" else (text or REPEAT_LINE)
                if line != text:
                    emit(line, True)
                messages.append({"role": "assistant", "content": line})
                spoken.append(line)
                steps.append({"say": line})
                break

            content = msg.content
            if len(words.dropped) > dropped_before:   # history holds only what the caller heard
                content = [{"type": "text", "text": said_in[i]} if b.type == "text" else b
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
                        line = tools.END_LINES[self.ctx.language].get((b.input or {}).get("reason"), tools.END_LINES[self.ctx.language]["completed"])
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
                if last["role"] == "user":   # nothing kept of the reply: add the note to the last user message
                    body = last["content"]
                    messages[-1] = {"role": "user", "content": (body + "\n\n" + NUDGE if isinstance(body, str)
                                                                else [*body, {"type": "text", "text": NUDGE}])}
                else:
                    messages.append({"role": "user", "content": NUDGE})
                continue
            messages.append({"role": "user", "content": results})
            words.new_reply()
            if self.ctx.ended:
                break

        reply = " ".join(spoken)
        silent = words.silent and not reply   # the caller wasn't talking to us: say nothing, not even a retry line
        if not reply and not self.ctx.ended and not silent:
            reply = REPEAT_LINE
            emit(reply, True)
            steps.append({"say": reply})
        if messages[-1]["role"] == "user":   # keep the history alternating, ending on what was said
            messages.append({"role": "assistant", "content": reply or (LISTENING if silent else REPEAT_LINE)})
        entry = {"role": "agent", "text": reply, "tools": used, "steps": steps}
        if words.dropped:
            entry["dropped"] = words.dropped   # for tests and reports; never spoken
        final = any(c["name"] in IRREVERSIBLE for c in used)
        go_on(irreversible=True)
        self._save(messages, caller_text, entry, emergency, final)
        self._said_once, self._held_back = words.said_once, words.held
        return Turn(reply, used, first, time.monotonic() - start, self.ctx.ended)

    def _save(self, messages: list[dict], caller_text: str, entry: dict, emergency, final: bool) -> None:
        # The state before this turn, kept so revert_last() can take back a reply the caller never heard.
        self._before = None if final else (len(self.messages), len(self.transcript), self.ctx.safety_given,
                                           self._held_back, self._said_once)
        self.messages = messages
        self.transcript += [{"role": "caller", "text": caller_text}, entry]
        if emergency:
            self.ctx.safety_given = True

    def revert_last(self) -> bool:
        """Takes back the last turn when the caller never heard it (they talked over it before it played).
        Only turns that changed nothing outside the conversation (no booking, message, transfer or hang-up)."""
        if not self._before:
            return False
        n_messages, n_transcript, self.ctx.safety_given, self._held_back, self._said_once = self._before
        del self.messages[n_messages:], self.transcript[n_transcript:]
        self._before = None
        return True

    def _count(self, usage) -> None:
        self.usage["input"] += usage.input_tokens or 0
        self.usage["output"] += usage.output_tokens or 0
        self.usage["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.usage["cache_write"] += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def cost(self) -> float:
        return usage_cost(self.cfg, self.cfg["models"]["agent"], self.usage)


def thinking_param(models: dict) -> dict:
    """"adaptive" (the API default) lets the model think before it speaks; "disabled" (Haiku) or "between_tools"
    (Sonnet 5.5, which only writes short notes between tool calls) skip that wait for speed."""
    setting = models.get("agent_thinking", "adaptive")
    return {} if setting == "adaptive" else {"thinking": {"type": setting}}


def prewarm(client: anthropic.Anthropic, cfg: dict, biz: dict) -> dict:
    """Writes the agent's cached prefix (tools + stable prompt) once, before calls start in parallel, so they
    don't all pay the cold-cache wait together. max_tokens=0 runs prefill only: nothing is generated or billed
    as output. Returns the token usage for cost tracking."""
    m = cfg["models"]
    extra = thinking_param(m)
    resp = client.messages.create(
        model=m["agent"], max_tokens=0, output_config={"effort": m["agent_effort"]},
        system=[{"type": "text", "text": prompts.stable_prompt(biz), "cache_control": {"type": "ephemeral"}}],
        tools=[{**t, "strict": True} for t in tools.schemas(biz)],
        messages=[{"role": "user", "content": "warmup"}], **extra,
    )
    return {"input": resp.usage.input_tokens or 0, "output": 0,
            "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0}


def usage_cost(cfg: dict, model: str, usage: dict) -> float:
    """Dollars, with cache writes at 1.25x and cache reads at 0.1x the input rate."""
    price_in, price_out = cfg["prices_per_mtok"].get(model, (0.0, 0.0))
    return (usage["input"] * price_in + usage["cache_write"] * price_in * 1.25
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
