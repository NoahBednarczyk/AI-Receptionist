"""AI receptionist CLI.  python -m receptionist <command> --help"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_business, load_config

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def _store(cfg, pretend_time=None):
    """A pretend --at time gets a throwaway database, so fake dates never land in the real records."""
    from .store import Store
    return Store(":memory:" if pretend_time else str(Path(cfg["data_dir"]) / "receptionist.sqlite"))


def _client():
    import anthropic
    return anthropic.Anthropic()


def cmd_chat(cfg, a):
    from . import budget, postcall, schedule
    from .agent import CallSession, transcript_text, usage_cost
    if budget.remaining(cfg) <= 0:
        raise SystemExit(f"Today's ${budget.daily_limit(cfg):.2f} testing budget is used up; try again tomorrow.")
    biz = load_business(cfg, a.business)
    store = _store(cfg, a.at)
    now = schedule.local_now(biz, a.at)
    store.seed_demo(biz, now)
    client = _client()
    s = CallSession(client, cfg, biz, store, now, caller=a.caller, platform="text")
    note = " Pretend time, so nothing is saved." if a.at else ""
    print(f"{DIM}Call {s.call_id} at {now:%a %b %d %H:%M} ({biz['timezone']}).{note} Empty line or Ctrl-D hangs up.{RESET}\n")
    print(f"{BOLD}{biz['agent_name']}>{RESET} {biz['greeting']}")
    while not s.ended:
        try:
            said = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not said:
            break
        turn = s.respond(said)
        for t in turn.tools:
            mark = "!" if t["error"] else "·"
            print(f"{DIM}  {mark} {t['name']} {json.dumps(t['args'])} -> {json.dumps(t['result'])}{RESET}")
        first = f"{turn.first_text_s:.2f}s to first words, " if turn.first_text_s is not None else ""
        print(f"{BOLD}{biz['agent_name']}>{RESET} {turn.text}  {DIM}({first}{turn.total_s:.2f}s total){RESET}")
    reason = s.ctx.end_reason or "caller hung up"
    print(f"\n{DIM}[call ended: {reason}; agent cost ${s.cost():.4f}]{RESET}")
    store.end_call(s.call_id, s.transcript, s.ctx.end_reason, now)
    for sms in store.for_call("sms_outbox", s.call_id):
        print(f"{DIM}  text queued to {sms['to_number']}: {sms['body']}{RESET}")
    cost = s.cost()
    if not a.no_summary and len(s.transcript) > 1:
        usage = {}
        summary = postcall.summarize(client, cfg, biz, transcript_text(s.transcript), s.system[1]["text"], usage=usage)
        cost += usage_cost(cfg, cfg["models"]["postcall"], usage)
        store.save_summary(s.call_id, summary)
        print(f"\n{BOLD}Post-call summary{RESET}\n{json.dumps(summary, indent=2)}")
    budget.record(cfg, cost)
    print(f"{DIM}This chat cost ${cost:.4f}; today's testing spend is ${budget.spent_today(cfg):.2f} "
          f"of ${budget.daily_limit(cfg):.2f}.{RESET}")


def cmd_prompt(cfg, a):
    from . import prompts, schedule
    biz = load_business(cfg, a.business)
    store = _store(cfg, a.at)
    now = schedule.local_now(biz, a.at)
    store.seed_demo(biz, now)
    customer = store.get_customer(biz["id"], a.caller)
    upcoming = store.upcoming_appointments(biz["id"], a.caller, now) if customer else []
    print(prompts.stable_prompt(biz))
    print("\n---- (cache breakpoint) ----\n")
    print(prompts.call_state(biz, now, a.caller, customer, upcoming))


def cmd_availability(cfg, a):
    from . import schedule
    biz = load_business(cfg, a.business)
    store = _store(cfg, a.at)
    now = schedule.local_now(biz, a.at)
    store.seed_demo(biz, now)
    print(f"{'Open' if schedule.is_open(biz, now) else 'Closed'} at {now:%a %b %d %H:%M}")
    for w in schedule.open_windows(biz, now, store.booked_counts(biz["id"]), limit=a.limit):
        print(f"  {w['window_id']}  {schedule.spoken_window(biz, w['window_id'], now)}")


def cmd_log(cfg, a):
    biz = load_business(cfg, a.business)
    store = _store(cfg)
    for table, cols in [("appointments", ("confirmation", "window_id", "service_id", "name", "phone", "town")),
                        ("leads", ("created_at", "urgency", "name", "phone", "problem")),
                        ("messages", ("created_at", "urgent", "name", "phone", "message")),
                        ("sms_outbox", ("created_at", "to_number", "body")),
                        ("calls", ("id", "started_at", "caller", "outcome"))]:
        rows = store.recent(table, biz["id"], a.limit)
        print(f"{BOLD}{table}{RESET} ({len(rows)})")
        for r in rows:
            print("  " + " | ".join(str(r.get(c, "")) for c in cols))


def cmd_simulate(cfg, a):
    from .simulate import list_scenarios, run_all
    biz = load_business(cfg, a.business)
    if a.list:
        for s in list_scenarios(cfg, biz):
            print(f"  {s['id']:<28} {s['title']}")
        return
    if a.quick:
        a.ids = a.ids or cfg["simulate"]["quick"]
    if a.effort:
        cfg["models"]["agent_effort"] = a.effort
    if a.thinking:
        cfg["models"]["agent_thinking"] = a.thinking
    if a.model:
        cfg["models"]["agent"] = a.model
    run_all(cfg, biz, _client(), a.ids, a.report, verbose=a.verbose, jobs=a.jobs)


def cmd_budget(cfg, a):
    from . import budget
    from .simulate import list_scenarios
    if a.raise_today is not None:
        budget.raise_today(cfg, a.raise_today)
        print(f"Today's testing limit is now ${a.raise_today:.2f} (back to ${cfg['simulate']['budget_per_day']:.2f} tomorrow).")
    per = budget.per_scenario_estimate(cfg)
    total = len(list_scenarios(cfg, load_business(cfg, None)))
    print(f"Today's testing spend: ${budget.spent_today(cfg):.2f} of ${budget.daily_limit(cfg):.2f} "
          f"(${budget.remaining(cfg):.2f} left).")
    print(f"Estimated cost: ${per:.3f} per scenario, ${per * len(cfg['simulate']['quick']):.2f} for --quick "
          f"({len(cfg['simulate']['quick'])} scenarios), ${per * total:.2f} for a full run ({total}).")


def cmd_record(cfg, a):
    from .recording import record
    out = record(a.report, a.scenario, str(Path(cfg["data_dir"]) / "recordings"))
    print(f"Wrote {out}")


def cmd_retell_voices(cfg, a):
    from .retell_setup import list_voices
    for v in list_voices():
        print(f"  {v.get('voice_id', ''):<34} {v.get('voice_name', ''):<16} {v.get('provider', ''):<11} "
              f"{v.get('gender', ''):<7} {v.get('accent', '')}")


def cmd_retell_setup(cfg, a):
    from .retell_setup import setup
    biz_path = Path(cfg["businesses_dir"]) / f"{a.business or cfg['default_business']}.json"
    rec = setup(cfg, biz_path, a.base_url, a.number, a.voice, a.language)
    print(f"Created Retell agent {rec['agent_id']} and pointed {rec['number']} at it.")
    print(f"Start the server (python -m receptionist serve), then call {rec['number']}.")


def cmd_retell_tune(cfg, a):
    from .retell_setup import tune
    agent = tune(cfg, load_business(cfg, a.business), a.base_url)
    print(f"Updated and published Retell agent {agent['agent_id']} (version {agent.get('version')}): "
          f"voice {agent.get('voice_id')} {agent.get('voice_model') or ''}".rstrip())


def cmd_serve(cfg, a):
    import logging
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .server import create_app
    uvicorn.run(create_app(cfg), host=a.host, port=a.port)


def cmd_export(cfg, a):
    from .platforms import retell_llm, vapi_assistant
    biz = load_business(cfg, a.business)
    base = a.base_url.rstrip("/")
    payload = vapi_assistant(cfg, biz, base, call_state_text=None) if a.platform == "vapi" else retell_llm(cfg, biz, base)
    out = Path(cfg["data_dir"]) / f"{a.platform}-{biz['id']}.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {out}")


def main():
    cfg = load_config()
    p = argparse.ArgumentParser(prog="python -m receptionist")
    sub = p.add_subparsers(dest="cmd", required=True)

    def biz_args(sp, at=True, caller=False):
        sp.add_argument("--business", help=f"business id (default {cfg['default_business']})")
        if at:
            sp.add_argument("--at", help='pretend local time, e.g. "2026-10-27T10:15"')
        if caller:
            sp.add_argument("--caller", default="+13125550177", help="caller ID; +13125550142 is a returning customer")

    sp = sub.add_parser("chat", help="talk to the agent as a caller, in text")
    biz_args(sp, caller=True)
    sp.add_argument("--no-summary", action="store_true", help="skip the post-call summary")
    sp.set_defaults(fn=cmd_chat)

    sp = sub.add_parser("prompt", help="print the compiled system prompt")
    biz_args(sp, caller=True)
    sp.set_defaults(fn=cmd_prompt)

    sp = sub.add_parser("availability", help="list open arrival windows")
    biz_args(sp)
    sp.add_argument("--limit", type=int, default=12)
    sp.set_defaults(fn=cmd_availability)

    sp = sub.add_parser("log", help="recent bookings, leads, messages, texts and calls")
    biz_args(sp, at=False)
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(fn=cmd_log)

    sp = sub.add_parser("simulate", help="run scripted callers against the agent and grade the calls")
    biz_args(sp, at=False)
    sp.add_argument("ids", nargs="*", help="scenario ids (default: all)")
    sp.add_argument("--list", action="store_true", help="list scenarios and exit")
    sp.add_argument("--report", help="write the JSON report here (default data/runs/<time>.json)")
    sp.add_argument("-v", "--verbose", action="store_true", help="print each transcript")
    sp.add_argument("--jobs", type=int, default=4, help="scenarios to run at once")
    sp.add_argument("--quick", action="store_true", help="only the quick set in config.json (simulate.quick)")
    sp.add_argument("--effort", choices=["low", "medium", "high"], help="override the agent's effort for this run")
    sp.add_argument("--thinking", choices=["adaptive", "disabled", "between_tools"], help="override the agent's thinking for this run")
    sp.add_argument("--model", help="override the agent's model for this run (one priced in config.json)")
    sp.set_defaults(fn=cmd_simulate)

    sp = sub.add_parser("budget", help="today's testing spend and what runs cost")
    sp.add_argument("--raise-today", type=float, metavar="DOLLARS", help="set a higher limit for today only")
    sp.set_defaults(fn=cmd_budget)

    sp = sub.add_parser("record", help="turn a simulated call into audio (macOS voices, free)")
    sp.add_argument("report", help="a run report, e.g. data/runs/<time>.json")
    sp.add_argument("scenario", help="scenario id in that report")
    sp.set_defaults(fn=cmd_record)

    sp = sub.add_parser("retell-voices", help="list Retell's voices")
    sp.set_defaults(fn=cmd_retell_voices)

    sp = sub.add_parser("retell-setup", help="create the Retell agent and point your number at this server")
    sp.add_argument("--base-url", required=True, help="public https URL of `serve`, e.g. your ngrok domain")
    sp.add_argument("--number", required=True, help="the Retell phone number, e.g. +13125550123")
    sp.add_argument("--voice", help="a voice_id from retell-voices (default: the business's voice.voice_id)")
    sp.add_argument("--language", default="en-US", help="Retell speech-recognition language (default en-US)")
    biz_args(sp, at=False)
    sp.set_defaults(fn=cmd_retell_setup)

    sp = sub.add_parser("retell-tune", help="push the business's voice settings to the live Retell agent")
    sp.add_argument("--base-url", help="also point the agent at this server's https address (after moving hosts)")
    biz_args(sp, at=False)
    sp.set_defaults(fn=cmd_retell_tune)

    sp = sub.add_parser("serve", help="webhook server for Vapi and Retell")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("export", help="write the agent config a voice platform needs")
    sp.add_argument("platform", choices=["vapi", "retell"])
    sp.add_argument("--base-url", required=True, help="public https URL of `serve`, e.g. from ngrok")
    biz_args(sp, at=False)
    sp.set_defaults(fn=cmd_export)

    a = p.parse_args()
    a.fn(cfg, a)


if __name__ == "__main__":
    main()
