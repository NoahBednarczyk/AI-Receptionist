"""Checks on the discovery caller's calls, free to run: no model, no API spend.

  fallback_log()   the call record written by code when the agent hung up without calling log_call
  tone_note()      a one-turn instruction when the owner has just described a problem (never laugh at it)
  unsupported_claims()  things the agent said that are not in its facts or the owner's words
  lint()           every problem found in one stored call, for `review-calls` and the tests

Nothing here changes what is said to an owner except tone_note(), which only adds a note for the model.
"""
from __future__ import annotations

import re
import statistics

PROBLEM = re.compile(r"\b(missed|miss(es|ing)?|lost|lose|losing|didn'?t (leave|call|answer)|no (message|voicemail)|never (called|came|got)|"
                     r"slammed|buried|behind|burned|burnt|broke|broken|hate|sick|hurt|injur\w+|died|passed away|struggl\w+|nightmare|"
                     r"can'?t keep up|walked away|moved on|hung up|ghost\w*)\b", re.I)
LAUGH = re.compile(r"^\W*(ha(ha)*|heh|hehe|lol|hah)\b", re.I)
LAUGH_ANYWHERE = re.compile(r"\b(ha ha|haha|hehe|lol)\b|\bha[,!.]", re.I)
SYMPATHY = re.compile(r"\b(sorry to hear|oh no|that'?s a bummer|that sounds (frustrating|rough|tough)|i understand your concern)\b", re.I)
SALES_LABEL = re.compile(r"\b(sales call|cold call|selling something)\b", re.I)
AI_SELF = re.compile(r"\b(i'?m an? (ai|robot|bot)|ai assistant|virtual assistant|an ai\b)", re.I)
PHANTOM = re.compile(r"\b(i'?m told|you asked to hear|you (signed|opted) up|other (shops|companies|owners|businesses) (in|around|are|have)|"
                     r"(many|most|several|a lot of) (shops|owners|businesses|companies)|we'?ve (helped|worked with)|guarantee[sd]?|"
                     r"award|rated|number one|#1|best in)\b", re.I)
FIGURE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d+(?:\.\d+)?\s?(?:%|percent)")
MAX_WORDS = 30


def tone_note(caller_text: str) -> str | None:
    """A note for the model's next reply when the owner has just described a problem or a loss."""
    if caller_text and PROBLEM.search(caller_text):
        return ("the owner just described a problem or a loss in their business. Take it seriously and show you want to help: a short, "
                "warm, attentive reply that picks up what they said. Do not laugh, joke, tease or say 'ha' about it.")
    return None


def unsupported_claims(agent_text: str, allowed: str) -> list[str]:
    """Claims in `agent_text` that nothing in `allowed` (the facts, the lead's record and the owner's own words) backs up."""
    out = [m.group(0) for m in PHANTOM.finditer(agent_text)]
    known = {re.sub(r"[\s,]", "", f.lower()) for f in FIGURE.findall(allowed)}
    for f in FIGURE.findall(agent_text):
        if re.sub(r"[\s,]", "", f.lower()) not in known:
            out.append(f"figure {f}")
    return out


def parse(transcript: str) -> list[tuple[str, str]]:
    """The stored "Caller: ...\nAgent: ..." text as (role, text) pairs. Tool lines are skipped."""
    out: list[tuple[str, str]] = []
    for line in transcript.splitlines():
        m = re.match(r"\s*(Caller|Agent):\s*(.*)", line)
        if m:
            out.append((m.group(1).lower(), m.group(2).strip()))
        elif out and line.strip() and not line.lstrip().startswith("[tool"):
            out[-1] = (out[-1][0], out[-1][1] + " " + line.strip())
    return out


def _norm(q: str) -> str:
    return re.sub(r"[^a-z ]", "", q.lower()).strip()


def lint(transcript: str, allowed: str = "", latencies: list[float] | None = None) -> list[str]:
    """Problems in one call: ["agent turn 3: laughed at a problem", ...]. Empty means nothing found."""
    turns = parse(transcript)
    found: list[str] = []
    caller_said = ""
    asked: list[str] = []
    openers: list[str] = []
    ai_asked = False
    for i, (role, text) in enumerate(turns, 1):
        if role == "caller":
            caller_said += " " + text
            ai_asked = ai_asked or bool(re.search(r"\b(robot|a\.?i\.?|bot|real person|human|machine)\b", text, re.I))
            continue
        where = f"agent turn {i}"
        if PROBLEM.search(turns[i - 2][1] if i > 1 and turns[i - 2][0] == "caller" else "") and (LAUGH.match(text) or LAUGH_ANYWHERE.search(text)):
            found.append(f"{where}: laughed after the owner described a problem: {text[:60]!r}")
        if SYMPATHY.search(text):
            found.append(f"{where}: sympathy boilerplate: {text[:60]!r}")
        if SALES_LABEL.search(text) and not re.search(r"sales call|cold call|selling", caller_said, re.I):
            found.append(f"{where}: called itself a sales or cold call: {text[:60]!r}")
        if AI_SELF.search(text) and not ai_asked:
            found.append(f"{where}: volunteered being an AI: {text[:60]!r}")
        if len(text.split()) > MAX_WORDS:
            found.append(f"{where}: {len(text.split())} words (over {MAX_WORDS})")
        for claim in unsupported_claims(text, allowed + " " + caller_said):
            found.append(f"{where}: claim with no source: {claim!r}")
        for q in re.findall(r"[^.?!]*\?", text):
            n = _norm(q)
            if len(n.split()) >= 4 and n in asked:
                found.append(f"{where}: asked the same question twice: {q.strip()[:60]!r}")
            asked.append(n)
        first = _norm(text).split(" ")[0] if _norm(text) else ""
        if first and openers[-2:] == [first, first]:
            found.append(f"{where}: three replies in a row opened with {first!r}")
        openers.append(first)
    if latencies and len(latencies) >= 2 and statistics.median(latencies) > 3.0:
        found.append(f"slow: median first words {statistics.median(latencies):.1f}s over {len(latencies)} replies")
    return found


def fallback_log(transcript: list[dict], lead: dict, reason: str | None = None) -> dict:
    """The record for a call the agent never logged (the owner hung up first). Only what the transcript shows: no guessing
    at what the owner thought. The owner's own words are kept as quotes."""
    said = [t["text"] for t in transcript if t.get("role") == "caller" and t.get("text")]
    tools = [c for t in transcript for c in (t.get("tools") or [])]
    booked = any(c.get("name") == "book_meeting" and not c.get("error") for c in tools)
    text = " ".join(said)
    if booked:
        outcome = "Demo booked"
    elif len(said) <= 1 and len(text.split()) <= 3:
        outcome = "No answer"
    elif re.search(r"(leave|at the tone|after the beep|voicemail|not available|mailbox)", text, re.I):
        outcome = "Voicemail"
    elif re.search(r"\bnot (interested|the owner|here)|no thanks|no thank you|wrong number|take me off\b", text, re.I):
        outcome = "Not interested"
    else:
        outcome = "Gatekeeper"
    quotes = " | ".join(s[:100] for s in said[:6])
    return {"outcome": outcome,
            "summary": f"The call ended before the agent logged it ({reason or 'hung up'}). Logged automatically from the transcript; "
                       f"the owner said {len(said)} thing(s).",
            "details": "No call log was written during the call. Read the full transcript for what was said.",
            "quotes": quotes, "lessons": "Ended before the agent could log it: check the transcript for where the owner hung up.",
            "objection": "None"}
