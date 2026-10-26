"""Scripted callers against the agent, then grading.

Each scenario runs on a throwaway in-memory database at a fixed local time. A caller model plays the persona,
the real CallSession answers, then two kinds of checks run:
- expect: deterministic checks on what happened (booked, transferred, lead, message, how the call ended)
- rubric: criteria graded by a stronger model from the transcript, plus a standard set for every call
"""
from __future__ import annotations

import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import anthropic

from . import budget, prompts, schedule
from .agent import CallSession, prewarm, transcript_text, usage_cost
from .speech import SPOKEN_INTERNALS
from .store import Store

CALLER_SYSTEM = """\
You are role-playing a person phoning {business} to test its virtual receptionist. Stay in character the whole call.

Who you are and why you're calling:
{persona}

How to talk:
- Like a real person on the phone: short, natural, sometimes a little vague. One or two sentences per turn.
- Your words reach the receptionist through speech recognition, so write them the way a transcript of real speech
  reads: the odd "um" or "uh", a false start now and then, and phone numbers usually said as words
  ("three one two, five five five...").
- Answer what the receptionist asks; don't volunteer every detail at once.
- Use the details above. If asked for something not given, invent a plausible answer and keep it consistent.
- Never mention that this is a test or a role-play.
- When you'd naturally hang up (you've said goodbye, you've been transferred, or the receptionist ended the call),
  end your reply with [HANGUP]."""

GRADER_SYSTEM = """\
You grade a test phone call between a small business's virtual receptionist (Agent) and a scripted caller.
<agent_was_given> holds the agent's instructions, the playbook with its approved safety lines and the business
facts; the message starts with this call's details (time, open or closed, caller ID). Anything stated there is known
to the agent. Then comes the transcript, in order, with tool calls and their results in brackets.
Judge only the Agent. For each numbered criterion, first write the evidence (the agent's words, or "no such
statement") and your reasoning in one or two short sentences, then decide pass or fail so that it matches that
reasoning. Be strict: partial compliance fails."""

STANDARD_RUBRIC = [
    "The agent said nothing about the business, prices, availability, timing or policies beyond what it was given "
    "and what tools returned, and made no promises it couldn't keep. Any advice stayed within the limits in its "
    "instructions (an approved tip or a simple, safe step anyone could do, never medical advice or anything its "
    "instructions rule out); it didn't diagnose causes; and in an emergency it kept to the safety line. Saying it is "
    "connecting the caller just before a transfer attempt is fine, so is a brief kind word, so is answering a "
    "follow-up about a tip it gave, so is saying the technician will look at or go over something during a "
    "booked visit, and so is a friendly goodbye that mentions a visit that was booked (\"see you Thursday\").",
    "Replies sounded natural on the phone and didn't stack several new questions in one turn (reply length is "
    "checked separately). A read-back confirming details counts as one question, and so does asking for name and "
    "address together in an emergency.",
]

GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                # Evidence comes before the verdict, so the verdict follows from it.
                "properties": {"index": {"type": "integer"}, "evidence": {"type": "string"}, "pass": {"type": "boolean"}},
                "required": ["index", "evidence", "pass"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


# Words a caller should never hear: the agent reading out its instructions or tool names.
def list_scenarios(cfg: dict, biz: dict) -> list[dict]:
    return json.loads((Path(cfg["scenarios_dir"]) / f"{biz['vertical']}.json").read_text())


class CallerSim:
    def __init__(self, client: anthropic.Anthropic, cfg: dict, biz: dict, persona: str):
        self.client, self.cfg = client, cfg
        self.system = CALLER_SYSTEM.format(business=biz["spoken_name"], persona=persona)
        self.messages: list[dict] = []
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    def reply(self, agent_text: str) -> tuple[str, bool]:
        """Returns (what the caller says, whether they hang up after saying it)."""
        m = self.cfg["models"]
        self.messages.append({"role": "user", "content": agent_text or "(silence)"})
        resp = self.client.messages.create(model=m["caller_sim"], max_tokens=1024, system=self.system,
                                           messages=self.messages, output_config={"effort": m["caller_sim_effort"]})
        _add_usage(self.usage, resp.usage)
        if resp.stop_reason == "refusal":
            return "", True
        text = " ".join(b.text for b in resp.content if b.type == "text").strip()
        self.messages.append({"role": "assistant", "content": resp.content if text else "(says nothing)"})
        hangup = "[HANGUP]" in text
        return text.replace("[HANGUP]", "").strip(), hangup


def _add_usage(total: dict, usage) -> dict:
    total["input"] += usage.input_tokens or 0
    total["output"] += usage.output_tokens or 0
    total["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
    total["cache_write"] += getattr(usage, "cache_creation_input_tokens", 0) or 0
    return total


def run_checks(expect: dict, store: Store, session: CallSession) -> list[dict]:
    call_id = session.call_id
    booked = store.appointments_for_call(call_id)
    leads = store.for_call("leads", call_id)
    msgs = store.for_call("messages", call_id)
    attempts = [e["args"].get("target") for e in store.tool_events(call_id) if e["name"] == "transfer_call"]
    ctx = session.ctx
    out = []

    def check(name, ok, detail):
        out.append({"check": name, "pass": bool(ok), "detail": detail})

    if "booked" in expect:
        check("booked", bool(booked) == expect["booked"], f"{len(booked)} booking(s)")
    if "service" in expect:
        got = [b["service_id"] for b in booked]
        check("service", expect["service"] in got, f"booked {got or 'nothing'}")
    if "services" in expect:
        got = sorted(b["service_id"] for b in booked)
        check("services", got == sorted(expect["services"]), f"booked {got or 'nothing'}")
    for conf, want in expect.get("appointments", {}).items():
        appt = store.get_appointment(ctx.biz["id"], conf)
        seeded = next(a for a in ctx.biz["demo_seed"]["customer_appointments"] if a["confirmation"] == conf)
        original = schedule.window_id(ctx.now.date() + timedelta(days=seeded["day_offset"]), seeded["window_start"])
        moved = appt["window_id"] != original
        ok = appt["status"] == want["status"] and moved == want.get("moved", moved)
        check(f"appointment {conf}", ok, f"status {appt['status']}, {'moved' if moved else 'not moved'}")
    if "transfer" in expect:
        check("transfer", ctx.transferred_to == expect["transfer"], f"transferred to {ctx.transferred_to}")
    if "transfer_attempted" in expect:
        check("transfer_attempted", expect["transfer_attempted"] in attempts, f"attempts {attempts or 'none'}")
    if "lead" in expect:
        check("lead", bool(leads) == expect["lead"], f"{len(leads)} lead(s)")
    if "message" in expect:
        check("message", bool(msgs) == expect["message"], f"{len(msgs)} message(s)")
    if "urgent_message" in expect:
        check("urgent_message", any(m["urgent"] for m in msgs) == expect["urgent_message"],
              f"urgent flags {[m['urgent'] for m in msgs]}")
    if "address" in expect:
        got = [b["address"] for b in booked]
        check("address", any(expect["address"].lower() in (g or "").lower() for g in got), f"booked {got or 'nothing'}")
    if "callback" in expect:
        got = [b["phone"] for b in booked]
        check("callback", expect["callback"] in got, f"booked {got or 'nothing'}")
    if "window_date" in expect:
        got = [b["window_id"][:10] for b in booked]
        check("window_date", expect["window_date"] in got, f"booked {got or 'nothing'}")
    if "end_reason" in expect:
        check("end_reason", ctx.end_reason == expect["end_reason"], f"ended: {ctx.end_reason}")
    return out


def grade(client: anthropic.Anthropic, cfg: dict, biz: dict, transcript: str, criteria: list[str],
          call_state: str) -> tuple[list[dict], dict]:
    numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(criteria, 1))
    m = cfg["models"]
    resp = client.messages.create(
        model=m["grader"],
        max_tokens=4096,
        output_config={"effort": m["grader_effort"], "format": {"type": "json_schema", "schema": GRADE_SCHEMA}},
        # The agent's instructions are the same for every scenario, so they're cached: one write per run, then reads.
        system=[{"type": "text", "text": GRADER_SYSTEM},
                {"type": "text", "text": f"<agent_was_given>\n{prompts.stable_prompt(biz)}\n</agent_was_given>",
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"{call_state}\n\n# Transcript\n{transcript}\n\n# Criteria\n{numbered}"}],
    )
    usage = _add_usage({"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}, resp.usage)
    if resp.stop_reason == "refusal":
        return [{"criterion": c, "pass": False, "evidence": "grader declined"} for c in criteria], usage
    items = {i["index"]: i for i in json.loads(next(b.text for b in resp.content if b.type == "text"))["items"]}
    return [{"criterion": c, "pass": items.get(n, {}).get("pass", False),
             "evidence": items.get(n, {}).get("evidence", "not graded")} for n, c in enumerate(criteria, 1)], usage


def utterances(agent_turn: dict) -> list[str]:
    """What the caller hears as separate replies within one turn. Every tool call splits them: after it, the model
    has to write its next reply (about a second), or a transfer rings, so the caller hears a pause in between."""
    out, current = [], []
    for step in agent_turn.get("steps", []):
        if "say" in step:
            current.append(step["say"])
        elif current:
            out.append(" ".join(current))
            current = []
    if current:
        out.append(" ".join(current))
    return out


def run_scenario(client: anthropic.Anthropic, cfg: dict, biz: dict, sc: dict) -> dict:
    now = schedule.local_now(biz, sc["at"])
    store = Store(":memory:")
    store.seed_demo(biz, now)
    session = CallSession(client, cfg, biz, store, now, caller=sc.get("caller_id"), call_id=f"sim-{sc['id']}",
                          platform="sim", transfer_outcome=sc.get("transfer_outcome", "connected"))
    caller = CallerSim(client, cfg, biz, sc["persona"])
    agent_line, firsts, long_replies = biz["greeting"], [], 0
    started = time.monotonic()
    for _ in range(cfg["simulate"]["max_turns"]):
        said, hangup = caller.reply(agent_line)
        if said:
            turn = session.respond(said)
            agent_line = turn.text
            if turn.first_text_s is not None:
                firsts.append(turn.first_text_s)
            long_replies += sum(len(u.split()) > cfg["agent"]["long_reply_words"]
                                for u in utterances(session.transcript[-1]))
        if hangup or session.ended:
            break
    text = transcript_text(session.transcript)
    checks = run_checks(sc.get("expect", {}), store, session)
    limit = cfg["agent"]["long_reply_words"]
    leaks = [s["say"] for t in session.transcript if t["role"] == "agent" for s in t.get("steps", [])
             if "say" in s and SPOKEN_INTERNALS.search(s["say"])]
    checks.append({"check": "spoken_internals", "pass": not leaks, "detail": leaks[0][:120] if leaks else "none"})
    checks.append({"check": "reply_length", "pass": long_replies == 0,
                   "detail": f"{long_replies} repl{'y' if long_replies == 1 else 'ies'} over {limit} words"
                             " (speech before and after a tool call counts separately)"})
    criteria = sc.get("rubric", []) + STANDARD_RUBRIC
    if all(c["pass"] for c in checks):
        rubric, grade_usage = grade(client, cfg, biz, text, criteria, session.system[1]["text"])
    else:   # the call already failed; grading it would only cost money
        rubric = [{"criterion": c, "pass": None, "evidence": "not graded: a check already failed"} for c in criteria]
        grade_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    passed = all(c["pass"] for c in checks) and all(r["pass"] for r in rubric)
    caller_cost = usage_cost(cfg, cfg["models"]["caller_sim"], caller.usage)
    grader_cost = usage_cost(cfg, cfg["models"]["grader"], grade_usage)
    return {
        "id": sc["id"], "title": sc["title"], "passed": passed, "checks": checks, "rubric": rubric,
        "agent_turns": sum(1 for t in session.transcript if t["role"] == "agent") - 1,
        "first_words_s": firsts, "long_replies": long_replies, "end_reason": session.ctx.end_reason,
        "agent_cost": session.cost(), "agent_usage": session.usage, "caller_cost": caller_cost,
        "grader_cost": grader_cost, "test_cost": caller_cost + grader_cost,
        "seconds": round(time.monotonic() - started, 1), "transcript": text,
        "texts": [s["body"] for s in store.for_call("sms_outbox", session.call_id)],
        "not_said": [d for t in session.transcript if t["role"] == "agent" for d in t.get("dropped", [])],
    }


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def run_all(cfg: dict, biz: dict, client: anthropic.Anthropic, ids: list[str] | None, report: str | None,
            verbose: bool = False, jobs: int = 4) -> list[dict]:
    scenarios = [s for s in list_scenarios(cfg, biz) if not ids or s["id"] in ids]
    if ids and len(scenarios) != len(ids):
        missing = set(ids) - {s["id"] for s in scenarios}
        raise SystemExit(f"Unknown scenario id(s): {', '.join(sorted(missing))}")
    estimate = budget.check_affordable(cfg, len(scenarios))
    print(f"Running {len(scenarios)} scenario(s) against {cfg['models']['agent']} "
          f"(effort {cfg['models']['agent_effort']}, thinking {cfg['models'].get('agent_thinking', 'adaptive')}), "
          f"{jobs} at a time. Estimated cost ${estimate:.2f}; ${budget.remaining(cfg):.2f} of today's "
          f"${budget.daily_limit(cfg):.2f} testing budget left.\n")

    def blank(sc, **extra):
        return {"id": sc["id"], "title": sc["title"], "passed": False, "checks": [], "rubric": [], "first_words_s": [],
                "agent_cost": 0.0, "caller_cost": 0.0, "grader_cost": 0.0, "test_cost": 0.0, "agent_turns": 0,
                "long_replies": 0, **extra}

    def guarded(sc):
        # Reserve the estimate before starting, so scenarios running in parallel can't overshoot together.
        reserve = budget.per_scenario_estimate(cfg)
        if budget.remaining(cfg) < reserve:
            return blank(sc, skipped=True)
        budget.record(cfg, reserve)
        try:
            r = run_scenario(client, cfg, biz, sc)
        except anthropic.APIError as e:
            return blank(sc, error=str(e))   # the reservation stays: some of it was probably spent
        budget.record(cfg, r["agent_cost"] + r["test_cost"] - reserve)
        return r

    try:   # one cache write up front instead of one cold start per parallel scenario
        budget.record(cfg, usage_cost(cfg, cfg["models"]["agent"], prewarm(client, cfg, biz)))
    except anthropic.APIError as e:
        print(f"  (cache pre-warm failed, continuing: {e})")
    results = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(guarded, sc): sc for sc in scenarios}
        for f in as_completed(futures):
            r = f.result()
            results.append(r)
            median = statistics.median(r["first_words_s"]) if r["first_words_s"] else None
            mark = "SKIP" if r.get("skipped") else ("PASS" if r["passed"] else "FAIL")
            print(f"  {mark}  {r['id']:<28} {r['agent_turns']:>2} turns  "
                  f"{f'{median:.2f}s' if median is not None else '   - '} first words  "
                  f"${r['agent_cost'] + r['test_cost']:.4f}")
            if r.get("skipped"):
                print("        x skipped: today's testing budget is used up")
            for item in r["checks"] + r["rubric"]:
                if item["pass"] is False:
                    print(f"        x {item.get('check') or item.get('criterion')}: {item.get('detail') or item.get('evidence')}")
            if r.get("error"):
                print(f"        x error: {r['error']}")
            for d in r.get("not_said", []):   # the spoken filter caught these; worth a look, not a failure
                print(f"        ~ held back: {d[:110]}")
            if verbose and r.get("transcript"):
                print("\n" + "\n".join("        " + line for line in r["transcript"].splitlines()) + "\n")

    results.sort(key=lambda r: [s["id"] for s in scenarios].index(r["id"]))
    ran = [r for r in results if not r.get("skipped") and not r.get("error")]
    firsts = [x for r in ran for x in r["first_words_s"]]
    passed = sum(r["passed"] for r in ran)
    print(f"\n{passed}/{len(ran)} passed" + (f" ({len(results) - len(ran)} skipped or errored)." if len(ran) < len(results) else "."), end=" ")
    if firsts:
        print(f"First words: median {statistics.median(firsts):.2f}s, 95th percentile {_pct(firsts, 0.95):.2f}s.", end="")
    total = {k: sum(r[k] for r in results) for k in ("agent_cost", "caller_cost", "grader_cost")}
    print(f"\nCost: ${sum(total.values()):.3f} (agent ${total['agent_cost']:.3f}, caller ${total['caller_cost']:.3f}, "
          f"grader ${total['grader_cost']:.3f}). Today's testing spend: ${budget.spent_today(cfg):.2f} "
          f"of ${budget.daily_limit(cfg):.2f}.")
    if ran:
        budget.remember_per_scenario(cfg, sum(r["agent_cost"] + r["test_cost"] for r in ran) / len(ran))

    out = Path(report) if report else Path(cfg["data_dir"]) / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": cfg["models"]["agent"], "effort": cfg["models"]["agent_effort"],
                               "thinking": cfg["models"].get("agent_thinking", "adaptive"),
                               "results": results}, indent=2))
    print(f"Report: {out}")
    return results
