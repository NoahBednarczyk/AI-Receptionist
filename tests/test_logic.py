"""Logic tests (no API key or network needed):  python -m unittest discover tests

The agent loop runs against a scripted fake client, so these tests check our code, not the model.
"""
import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from receptionist import platforms, prompts, schedule
from receptionist.agent import REFUSAL_LINE, CallSession, transcript_text
from receptionist.config import business_for_number, load_business, load_config
from receptionist.simulate import run_checks
from receptionist.speech import normalize_phone, ordinal, spoken_date, spoken_time
from receptionist.store import Store
from receptionist.tools import ToolContext, run_tool, schemas

CFG = load_config()
BIZ = load_business(CFG, "acme")
TUE_1015 = schedule.local_now(BIZ, "2026-10-27T10:15")
SAT_2340 = schedule.local_now(BIZ, "2026-10-31T23:40")


def fresh_store(now=TUE_1015):
    store = Store(":memory:")
    store.seed_demo(BIZ, now)
    return store


TEXTING_BIZ = {**BIZ, "texting_enabled": True}


def ctx(now=TUE_1015, **kw):
    biz = kw.pop("biz", BIZ)
    return ToolContext(biz, kw.pop("store", None) or fresh_store(now), now, kw.pop("caller", "+13125550177"), "test-call", **kw)


# ---- fake Anthropic client --------------------------------------------------
def text(t):
    return SimpleNamespace(type="text", text=t)


def use(id_, name, args):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=args)


def reply(stop, *blocks):
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=50, cache_creation_input_tokens=0)
    return SimpleNamespace(stop_reason=stop, content=list(blocks), usage=usage)


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for i, b in enumerate(self.msg.content):   # the same events the API sends, deltas a few words at a time
            yield SimpleNamespace(type="content_block_start", index=i, content_block=b)
            if b.type == "text":
                for piece in re.findall(r"\S+\s*|\s+", b.text):
                    yield SimpleNamespace(type="content_block_delta", index=i,
                                          delta=SimpleNamespace(type="text_delta", text=piece))
            yield SimpleNamespace(type="content_block_stop", index=i, content_block=b)

    def get_final_message(self):
        return self.msg


class FakeClient:
    """messages.stream() serves the agent from `script`; messages.create() (caller, grader) serves `created`."""

    def __init__(self, *script, created=()):
        self.script, self.requests = list(script), []
        self.created, self.create_requests = list(created), []
        self.messages = self

    def stream(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        return FakeStream(self.script.pop(0))

    def create(self, **kw):
        self.create_requests.append(kw)
        return self.created.pop(0)


def session(*script, now=TUE_1015, store=None, **kw):
    return CallSession(FakeClient(*script), CFG, BIZ, store or fresh_store(now), now, caller="+13125550177", **kw)


# ---- tests --------------------------------------------------------------------
class SpeechTests(unittest.TestCase):
    def test_phone_normalization(self):
        self.assertEqual(normalize_phone("(312) 555-0142"), "+13125550142")
        self.assertEqual(normalize_phone("1 312 555 0142"), "+13125550142")
        self.assertIsNone(normalize_phone("555-0142"))
        self.assertIsNone(normalize_phone(None))

    def test_spoken_forms(self):
        self.assertEqual([spoken_time(t) for t in ("08:00", "12:00", "14:30", "00:00")],
                         ["8 AM", "noon", "2:30 PM", "midnight"])
        self.assertEqual([ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22)],
                         ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd"])
        self.assertEqual(spoken_date(TUE_1015.date().replace(day=28), TUE_1015.date()), "tomorrow, Wednesday, October 28th")


class ScheduleTests(unittest.TestCase):
    def test_open_and_next_opening(self):
        self.assertTrue(schedule.is_open(BIZ, TUE_1015))
        self.assertFalse(schedule.is_open(BIZ, SAT_2340))
        nxt = schedule.next_opening(BIZ, SAT_2340)   # Sunday is closed
        self.assertEqual((nxt.strftime("%a"), nxt.hour), ("Mon", 7))

    def test_windows_respect_hours_lead_time_and_capacity(self):
        store = fresh_store()
        wins = schedule.open_windows(BIZ, TUE_1015, store.booked_counts("acme"), limit=50)
        ids = [w["window_id"] for w in wins]
        self.assertEqual(ids[0], "2026-10-27T14:00")            # 10:00 and 12:00 are inside the 2-hour lead time
        self.assertNotIn("2026-10-28T08:00", ids)              # both technicians already booked
        self.assertIn("2026-10-28T10:00", ids)                 # one of two booked
        self.assertNotIn("2026-11-01T08:00", ids)              # Sunday closed
        self.assertNotIn("2026-10-31T14:00", ids)              # Saturday closes at 2 PM
        self.assertIn("2026-10-31T12:00", ids)

    def test_window_ids_round_trip(self):
        self.assertEqual(schedule.parse_window_id("2026-10-28T10:00")[1], "10:00")
        self.assertIsNone(schedule.parse_window_id("tomorrow morning"))
        self.assertEqual(schedule.spoken_window(BIZ, "2026-10-28T10:00", TUE_1015),
                         "tomorrow, Wednesday, October 28th, between 10 AM and noon")


class ToolTests(unittest.TestCase):
    def booking(self, **overrides):
        args = {"window_id": "2026-10-28T10:00", "service_id": "leak_repair", "customer_name": "Jordan Lee",
                "callback_number": "312-555-0177", "street_address": "482 Maple Street", "town": "sand point",
                "problem_summary": "Kitchen sink supply line dripping."}
        return {**args, **overrides}

    def test_booking_saves_customer_and_texts_both_sides(self):
        c = ctx(biz=TEXTING_BIZ)
        result, err = run_tool(c, "book_appointment", self.booking())
        self.assertFalse(err, result)
        self.assertEqual(result["status"], "booked")
        self.assertEqual(c.store.get_customer("acme", "+13125550177")["town"], "Sandpoint")
        texts = c.store.for_call("sms_outbox", "test-call")
        self.assertEqual({t["to_number"] for t in texts}, {"+13125550177", BIZ["owner_sms"]})
        # One seeded job plus this one fill both technicians, so another caller can't take the window.
        other = ToolContext(BIZ, c.store, TUE_1015, "+13125550111", "other-call")
        result, err = run_tool(other, "book_appointment", self.booking(customer_name="Other", callback_number="3125550111"))
        self.assertTrue(err)
        self.assertIn("isn't available", result["error"])

    def test_booking_again_on_the_same_call_updates_it(self):
        c = ctx()
        first, _ = run_tool(c, "book_appointment", self.booking())
        again, _ = run_tool(c, "book_appointment", self.booking(access_notes="Friendly dog"))
        moved, _ = run_tool(c, "book_appointment", self.booking(window_id="2026-10-28T14:00"))
        self.assertEqual((again["status"], moved["status"]), ("updated", "moved"))
        self.assertEqual({first["confirmation"], again["confirmation"], moved["confirmation"]}, {first["confirmation"]})
        appts = c.store.appointments_for_call("test-call")
        self.assertEqual(len(appts), 1)
        self.assertEqual((appts[0]["window_id"], appts[0]["access_notes"]), ("2026-10-28T14:00", "Friendly dog"))

    def test_booking_guardrails(self):
        for bad, needle in [({"town": "Springfield"}, "outside the service area"),
                            ({"callback_number": "555-0177"}, "10-digit"),
                            ({"window_id": "2026-10-28T08:00"}, "isn't available"),
                            ({"window_id": "next tuesday"}, "isn't available"),
                            ({"service_id": "septic"}, "Unknown service_id")]:
            result, err = run_tool(ctx(), "book_appointment", self.booking(**bad))
            self.assertTrue(err, bad)
            self.assertIn(needle, result["error"])

    def test_missing_argument_is_a_tool_error_not_a_crash(self):
        result, err = run_tool(ctx(), "book_appointment", {"window_id": "2026-10-28T10:00"})
        self.assertTrue(err)
        self.assertIn("Missing or malformed", result["error"])

    def test_transfer_rules_follow_business_hours(self):
        _, err = run_tool(ctx(TUE_1015), "transfer_call", {"target": "on_call", "reason": "x"})
        self.assertTrue(err)
        _, err = run_tool(ctx(SAT_2340), "transfer_call", {"target": "office", "reason": "x"})
        self.assertTrue(err)
        c = ctx(SAT_2340)
        result, err = run_tool(c, "transfer_call", {"target": "on_call", "reason": "burst pipe"})
        self.assertEqual((err, result["status"], c.ended, c.transferred_to), (False, "connected", True, "on_call"))

    def test_unanswered_transfer_keeps_the_call_going(self):
        c = ctx(transfer_outcome="no_answer")
        result, _ = run_tool(c, "transfer_call", {"target": "office", "reason": "leak"})
        self.assertEqual(result["status"], "no_answer")
        self.assertFalse(c.ended)

    def test_lookup_and_message(self):
        c = ctx()
        # Someone else's number: nothing comes back until the caller names the account holder.
        hidden = run_tool(c, "lookup_customer", {"phone": "312 555 0142"})[0]
        self.assertEqual((hidden["found"], hidden["verified"]), (True, False))
        self.assertNotIn("Pat", json.dumps(hidden))
        self.assertNotIn("name", run_tool(c, "lookup_customer", {"phone": "312 555 0142", "name": "Jordan"})[0])
        self.assertEqual(run_tool(c, "lookup_customer", {"phone": "312 555 0142", "name": "pat rivera"})[0]["name"], "Pat Rivera")
        own = ctx(caller="+13125550142")   # calling from the number on file
        self.assertEqual(run_tool(own, "lookup_customer", {"phone": "312 555 0142"})[0]["name"], "Pat Rivera")
        self.assertFalse(run_tool(c, "lookup_customer", {"phone": "312 555 0199"})[0]["found"])
        result, _ = run_tool(c, "take_message", {"caller_name": "Lee", "callback_number": "3125550148",
                                                 "message": "Water heater flooding", "urgent": True})
        self.assertEqual(result["callback"], "within 15 minutes")
        self.assertTrue(c.store.for_call("messages", "test-call")[0]["urgent"])

    def test_no_texts_pile_up_while_texting_is_off(self):
        c = ctx()
        run_tool(c, "book_appointment", self.booking())
        run_tool(c, "take_message", {"caller_name": "Lee", "callback_number": "3125550148", "message": "x", "urgent": False})
        self.assertEqual(c.store.for_call("sms_outbox", "test-call"), [])
        self.assertNotIn("Texts the caller", next(t for t in schemas(BIZ) if t["name"] == "book_appointment")["description"])

    def test_a_message_needs_a_full_callback_number(self):
        result, err = run_tool(ctx(), "take_message", {"caller_name": "Lee", "callback_number": "555-0148",
                                                       "message": "Call me", "urgent": True})
        self.assertTrue(err)
        self.assertIn("10-digit", result["error"])

    def test_a_live_call_never_rings_a_placeholder_number(self):
        live = ctx(real_transfers=True)   # the demo's transfer numbers are 555-01xx placeholders
        result, _ = run_tool(live, "transfer_call", {"target": "office", "reason": "x"})
        self.assertEqual(result["status"], "no_answer")
        self.assertFalse(live.ended)
        self.assertEqual(run_tool(ctx(), "transfer_call", {"target": "office", "reason": "x"})[0]["status"], "connected")

    def test_callback_promise_follows_business_hours(self):
        msg = {"caller_name": "Jamie", "callback_number": "3125550147", "message": "Invoice question", "urgent": False}
        self.assertEqual(run_tool(ctx(TUE_1015), "take_message", msg)[0]["callback"], "later today, during business hours")
        self.assertEqual(run_tool(ctx(SAT_2340), "take_message", msg)[0]["callback"],
                         "the office is closed right now; someone will call back after it opens Monday, November 2nd at 7 AM")

    def test_schemas_are_strict_compatible(self):
        for t in schemas(BIZ):
            s = t["input_schema"]
            self.assertFalse(s["additionalProperties"], t["name"])
            self.assertTrue(set(s["required"]) <= set(s["properties"]), t["name"])


class PromptTests(unittest.TestCase):
    def test_stable_prompt_has_every_layer_and_no_placeholders(self):
        p = prompts.stable_prompt(BIZ)
        for needle in ("You are Sam", "# Home services playbook", "# Business facts", "$89 diagnostic fee"):
            self.assertIn(needle, p)
        self.assertNotIn("{agent_name}", p)
        self.assertNotIn("It is now", p)                       # time belongs to the per-call block

    def test_call_state(self):
        store = fresh_store(SAT_2340)
        pat = store.get_customer("acme", "+13125550142")
        state = prompts.call_state(BIZ, SAT_2340, "+13125550142", pat, [])
        self.assertIn("closed now", state)
        self.assertIn("Monday, November 2nd at 7 AM", state)
        self.assertIn("Pat Rivera, 14 Birch Lane, Sagle", state)
        self.assertIn("open now", prompts.call_state(BIZ, TUE_1015, None, None, []))


class AgentLoopTests(unittest.TestCase):
    def test_tool_round_trip(self):
        s = session(reply("tool_use", text("One moment while I check."),
                          use("t1", "check_availability", {"service_id": "leak_repair"})),
                    reply("end_turn", text("I have tomorrow between 10 AM and noon.")))
        turn = s.respond("My sink is leaking.")
        self.assertEqual(turn.text, "One moment while I check. I have tomorrow between 10 AM and noon.")
        self.assertEqual([t["name"] for t in turn.tools], ["check_availability"])
        self.assertEqual([m["role"] for m in s.messages], ["user", "assistant", "user", "assistant"])
        result = s.messages[2]["content"][0]
        self.assertEqual(result["tool_use_id"], "t1")
        self.assertIn("2026-10-27T14:00", result["content"])
        self.assertFalse(turn.ended)
        self.assertIsNotNone(turn.first_text_s)

    def test_speech_not_meant_for_the_agent_gets_no_reply(self):
        s = session(reply("end_turn", text("(listening)")))
        turn = s.respond("Hold on, what's the address, babe?")
        self.assertEqual(turn.text, "")   # nothing spoken, not even the "say that again" line
        self.assertEqual(s.messages[-1]["role"], "assistant")   # the history keeps alternating
        self.assertTrue(s.revert_last())   # and the muttering joins the caller's next words
        self.assertEqual(s.messages, [])

    def test_filler_line_covers_a_silent_tool_call(self):
        s = session(reply("tool_use", use("t1", "check_availability", {"service_id": "leak_repair"})),
                    reply("end_turn", text("I have 2 PM today.")))
        turn = s.respond("Can someone come today?")
        self.assertEqual(turn.text, "Let me take a look at the schedule. I have 2 PM today.")
        self.assertIsNotNone(turn.first_text_s)
        self.assertEqual(s.messages[1]["content"][0].type, "tool_use")   # the filler never enters model history
        # No filler when the agent already spoke; a silent transfer gets its own line.
        s = session(reply("tool_use", text("Sure."), use("t1", "check_availability", {"service_id": "leak_repair"})),
                    reply("end_turn", text("I have 2 PM today.")))
        self.assertEqual(s.respond("Today?").text, "Sure. I have 2 PM today.")
        s = session(reply("tool_use", use("t1", "transfer_call", {"target": "office", "reason": "x"})))
        self.assertEqual(s.respond("A person please.").text, "Let me connect you now.")
        s = session(reply("tool_use", use("t1", "create_lead", {"customer_name": "x", "callback_number": "3125550100",
                                                               "problem_summary": "x", "urgency": "flexible",
                                                               "reason_not_booked": "x"})),
                    reply("end_turn", text("Thanks for calling.")))
        self.assertEqual(s.respond("Bye.").text, "Thanks for calling.")    # a lead is an internal note: no line

    def test_no_call_ends_in_silence(self):
        s = session(reply("tool_use", use("t1", "end_call", {"reason": "safety"})))
        turn = s.respond("I smell gas by the furnace and I'm in the basement.")
        self.assertTrue(turn.text.startswith("Please leave the house now"))
        self.assertTrue(turn.ended)
        s = session(reply("tool_use", use("t1", "end_call", {"reason": "spam"})))
        self.assertEqual(s.respond("Press 1 to keep your listing.").text, "Thanks for calling. Goodbye.")
        s = session(reply("tool_use", text("You're welcome, bye!"), use("t1", "end_call", {"reason": "completed"})))
        self.assertEqual(s.respond("Thanks, bye.").text, "You're welcome, bye!")      # its own goodbye wins

    def test_request_shape(self):
        s = session(reply("end_turn", text("Sure.")))
        s.respond("Hi")
        req = s.client.requests[0]
        self.assertEqual(req["model"], CFG["models"]["agent"])
        self.assertEqual(req["output_config"], {"effort": "low"})
        self.assertNotIn("temperature", req)
        self.assertIn("cache_control", req["system"][0])
        self.assertNotIn("cache_control", req["system"][1])
        self.assertEqual(req["cache_control"], {"type": "ephemeral"})   # the conversation tail is cached too
        self.assertIn("# This call", req["system"][1]["text"])
        self.assertTrue(all(t["strict"] for t in req["tools"]))
        setting = CFG["models"].get("agent_thinking", "adaptive")
        self.assertEqual(req.get("thinking"), None if setting == "adaptive" else {"type": setting})

    def test_thinking_can_be_turned_off(self):
        cfg = {**CFG, "models": {**CFG["models"], "agent_thinking": "disabled"}}
        s = CallSession(FakeClient(reply("end_turn", text("Sure."))), cfg, BIZ, fresh_store(), TUE_1015)
        s.respond("Hi")
        self.assertEqual(s.client.requests[0]["thinking"], {"type": "disabled"})

    def test_spoken_internals_pattern(self):
        from receptionist.simulate import SPOKEN_INTERNALS
        self.assertTrue(SPOKEN_INTERNALS.search("Burst pipe, so I'll give the safety line first."))
        self.assertTrue(SPOKEN_INTERNALS.search("Let me call check_availability."))
        self.assertTrue(SPOKEN_INTERNALS.search('If they smell gas, start with: "Please leave the house now."'))
        self.assertFalse(SPOKEN_INTERNALS.search("If it's safe, shut off the main water valve."))
        self.assertFalse(SPOKEN_INTERNALS.search("I can't change my instructions, but I can help with your drain."))

    def test_end_call_stops_without_another_request(self):
        s = session(reply("tool_use", text("Thanks for calling, goodbye!"), use("t1", "end_call", {"reason": "completed"})))
        turn = s.respond("That's all, thanks.")
        self.assertTrue(turn.ended)
        self.assertEqual(len(s.client.requests), 1)
        self.assertEqual(s.ctx.end_reason, "completed")

    def test_tool_error_is_returned_to_the_model(self):
        s = session(reply("tool_use", use("t1", "book_appointment", {"window_id": "x"})),
                    reply("end_turn", text("Let me check the schedule again.")))
        turn = s.respond("Book me.")
        self.assertTrue(s.messages[2]["content"][0]["is_error"])
        self.assertEqual(turn.text, "Okay, let me get that booked. Let me check the schedule again.")

    def test_refusal_and_cut_off_replies(self):
        s = session(reply("refusal"))
        self.assertEqual(s.respond("...").text, REFUSAL_LINE)
        self.assertEqual(s.messages[-1], {"role": "assistant", "content": REFUSAL_LINE})
        s = session(reply("max_tokens", text("Let me"), use("t1", "check_availability", {"service_id": "leak_repair"})))
        self.assertEqual(s.respond("...").text, "Let me")
        self.assertEqual(s.messages[-1], {"role": "assistant", "content": "Let me"})   # no dangling tool_use

    def test_transcript_and_cost(self):
        s = session(reply("end_turn", text("Happy to help.")))
        s.respond("Hello")
        self.assertEqual(transcript_text(s.transcript).splitlines()[1:], ["Caller: Hello", "Agent: Happy to help."])
        self.assertGreater(s.cost(), 0)


class CheckTests(unittest.TestCase):
    def test_checks_read_what_happened(self):
        store = fresh_store(SAT_2340)
        s = session(reply("tool_use", text("Connecting you."), use("t1", "transfer_call", {"target": "on_call", "reason": "flood"})),
                    now=SAT_2340, store=store)
        s.respond("I need someone right now.")
        results = {c["check"]: c["pass"] for c in run_checks({"transfer": "on_call", "booked": False, "message": True}, store, s)}
        self.assertEqual(results, {"transfer": True, "booked": True, "message": False})
        # The grader must see the words spoken before the transfer ran, in that order.
        lines = transcript_text(s.transcript).splitlines()
        self.assertEqual(lines[2], "Agent: Connecting you.")
        self.assertTrue(lines[3].startswith("  [tool transfer_call"))


class UpgradeTests(unittest.TestCase):
    """Appointment changes, several services per call, Spanish, tips and the AI-honesty rule."""

    def test_reschedule_and_cancel_check_the_callers_number(self):
        morgan = ctx(caller="+13125550160")
        self.assertEqual(morgan.store.get_appointment("acme", "a0901")["window_id"], "2026-10-29T12:00")
        result, err = run_tool(morgan, "reschedule_appointment", {"confirmation": "A0901", "new_window_id": "2026-10-30T08:00"})
        self.assertFalse(err, result)
        self.assertEqual(morgan.store.get_appointment("acme", "A0901")["window_id"], "2026-10-30T08:00")
        neighbor = ToolContext(BIZ, morgan.store, TUE_1015, "+13125550161", "neighbor-call")
        result, err = run_tool(neighbor, "cancel_appointment", {"confirmation": "A0901", "reason": "out of town"})
        self.assertTrue(err)
        self.assertIn("doesn't match", result["error"])
        self.assertNotIn("Willow", json.dumps(result))                     # no details leak
        result, err = run_tool(neighbor, "cancel_appointment", {"confirmation": "A0901", "reason": "out of town",
                                                                "callback_number": "312-555-0160"})
        self.assertFalse(err, result)
        self.assertEqual(morgan.store.get_appointment("acme", "A0901")["status"], "cancelled")
        _, err = run_tool(morgan, "reschedule_appointment", {"confirmation": "A0901", "new_window_id": "2026-10-30T10:00"})
        self.assertTrue(err)                                                 # cancelled appointments stay cancelled

    def test_two_services_make_two_bookings(self):
        c = ctx()
        base = {"customer_name": "Sam Taylor", "callback_number": "3125550164", "street_address": "12 Oak Lane",
                "town": "Ponderay", "problem_summary": "x"}
        run_tool(c, "book_appointment", {**base, "window_id": "2026-10-28T12:00", "service_id": "drain_cleaning"})
        run_tool(c, "book_appointment", {**base, "window_id": "2026-10-28T14:00", "service_id": "tune_up"})
        self.assertEqual(sorted(a["service_id"] for a in c.store.appointments_for_call("test-call")),
                         ["drain_cleaning", "tune_up"])

    def test_spanish_callers_get_spanish_lines_dates_and_texts(self):
        s = session(reply("tool_use", use("t1", "check_availability", {"service_id": "drain_cleaning"})),
                    reply("end_turn", text("Tengo mañana por la tarde.")))
        turn = s.respond("Hola, necesito una cita para el lavabo, por favor.")
        self.assertEqual(s.ctx.language, "es")
        self.assertTrue(turn.text.startswith("Un momento, reviso la agenda."))
        self.assertIn("mañana, miércoles 28 de octubre, entre las 10 de la mañana y el mediodía",
                      s.messages[2]["content"][0]["content"])
        c = ctx(biz=TEXTING_BIZ)
        c.language = "es"
        run_tool(c, "book_appointment", {"window_id": "2026-10-28T12:00", "service_id": "drain_cleaning",
                                         "customer_name": "Rosa Martínez", "callback_number": "3125550163",
                                         "street_address": "33 Elm Court", "town": "Dover", "problem_summary": "x"})
        texts = c.store.for_call("sms_outbox", "test-call")
        self.assertIn("su cita está reservada", texts[0]["body"])
        self.assertIn("New booking", texts[1]["body"])                     # the owner's text stays in English

    def test_prompt_has_tips_and_the_honesty_rule(self):
        p = prompts.stable_prompt(BIZ)
        self.assertIn("turn off the valve on the wall behind the toilet", p)
        self.assertIn("Never claim or suggest that you're human", p)
        self.assertNotIn("virtual assistant", BIZ["greeting"])
        from receptionist.tools import FILLERS
        self.assertEqual(set(FILLERS["en"]), set(FILLERS["es"]))
        self.assertEqual({t["name"] for t in schemas(BIZ)} - set(FILLERS["en"]), {"create_lead", "end_call"})

    def test_dates_must_be_this_year_and_in_range(self):
        result, err = run_tool(ctx(), "check_availability", {"service_id": "leak_repair", "earliest_date": "2025-10-30"})
        self.assertTrue(err)
        self.assertIn("in the past; today is 2026-10-27", result["error"])
        result, err = run_tool(ctx(), "check_availability", {"service_id": "leak_repair", "earliest_date": "2026-12-01"})
        self.assertIn("only through 2026-11-06", result["error"])
        result, err = run_tool(ctx(), "check_availability", {"service_id": "leak_repair", "earliest_date": "2026-10-30"})
        self.assertEqual(result["windows"][0]["window_id"], "2026-10-30T08:00")
        self.assertIn("2026-10-27", prompts.call_state(BIZ, TUE_1015, None, None, []))


class SafetyTests(unittest.TestCase):
    def test_emergencies_are_matched_and_negations_ignored(self):
        from receptionist.safety import match
        self.assertEqual(match("home_services", "I smell gas by the furnace")[0], "gas")
        self.assertEqual(match("home_services", "A pipe burst and water is spraying!")[0], "flooding")
        self.assertIsNone(match("home_services", "I don't smell gas, the pilot just went out"))
        self.assertIsNone(match("home_services", "My gas water heater has no hot water"))
        self.assertIsNone(match("home_services", "My sink is dripping"))
        for said in ("the pipe just burst", "my pipes have burst", "water is spraying everywhere", "I smell some gas"):
            self.assertIsNotNone(match("home_services", said), said)
        self.assertIsNone(match("home_services", "there's no flooding, just a drip"))

    def test_safety_line_is_said_by_code_first_and_once(self):
        s = session(reply("end_turn", text("Can I get your name and address?")),
                    reply("end_turn", text("Thanks.")))
        turn = s.respond("Help, a pipe just burst in my basement!")
        self.assertTrue(turn.text.startswith("If it's safe, shut off the main water valve"))
        self.assertEqual(turn.first_text_s, 0.0)
        self.assertIn("already said the flooding safety line", s.client.requests[0]["messages"][-1]["content"])
        s.respond("It's still flooding, hurry.")
        self.assertNotIn("System note", s.client.requests[1]["messages"][-1]["content"])   # only once per call

    def test_everyday_problems_get_no_safety_line(self):
        from receptionist.safety import match
        for said in ("My toilet keeps running and won't stop.", "my water heater won't stop making noise",
                     "do you guys handle flooding?", "my basement flooded last year and I want a sump pump",
                     "the CO detector keeps chirping, I think it's the battery", "there are water stains all over the ceiling",
                     "my faucet is leaking and I can't get it to stop", "the toilet is backing up"):
            self.assertIsNone(match("home_services", said), said)
        for said in ("Water's coming out of the bottom of my water heater and it's all over the floor.",
                     "my basement is flooding", "the pipe under the sink broke", "my CO alarm is going off"):
            self.assertIsNotNone(match("home_services", said), said)

    def test_gas_inside_is_a_fixed_turn_that_hangs_up(self):
        s = session()   # no scripted model replies: calling the model would fail
        turn = s.respond("There's a really strong rotten-egg smell down in my basement.")
        self.assertTrue(turn.text.startswith("Please leave the house now"))
        self.assertTrue(turn.text.endswith("Once you're safe outside, you can call us back."))
        self.assertTrue(turn.ended)
        self.assertEqual(s.ctx.end_reason, "safety")
        self.assertEqual(s.client.requests, [])

    def test_gas_caller_already_outside_goes_to_the_model(self):
        s = session(reply("end_turn", text("Good. Can I get your name?")))
        turn = s.respond("I smell gas, so I'm outside in the driveway now.")
        self.assertFalse(turn.ended)
        self.assertEqual(len(s.client.requests), 1)


class SpokenFilterTests(unittest.TestCase):
    def test_thinking_out_loud_is_never_said_or_kept(self):
        heard = []
        s = session(reply("end_turn", text("Is the water still spreading?\n\nWait, I should just ask directly: "
                                           "is it safe to reach the valve?")))
        turn = s.respond("Something's wrong with my water heater.", on_text=heard.append)
        self.assertEqual(turn.text, "Is the water still spreading?")
        self.assertEqual("".join(heard), "Is the water still spreading?")
        self.assertEqual(s.messages[-1]["content"], [{"type": "text", "text": "Is the water still spreading?"}])
        self.assertTrue(s.transcript[-1]["dropped"][0].startswith("Wait, I should"))

    def test_a_long_reply_stops_after_the_answer(self):
        from receptionist.agent import Sentences
        heard = []
        words = Sentences(heard.append, budget=40)
        words.add("The technician arrives sometime within that two-hour window, and they call or text about 30 "
                  "minutes before heading over. I can't promise a narrower time than that. To confirm: that's today "
                  "between 2 PM and 4 PM at 14 Birch Lane in Sagle, with 312-555-0142 as your callback number. "
                  "Shall I book it? ")   # a real 53-word reply
        words.close()
        self.assertEqual(len(heard), 2)   # the answer goes; the read-back and its question wait for the caller
        self.assertTrue(words.dropped[0].startswith("To confirm"))
        words.new_reply()
        words.add("You're booked. ")
        words.close()
        self.assertEqual(heard[-1], "You're booked.")

    def test_the_model_hears_what_the_length_cap_held_back(self):
        long = ("Oh no, that's no fun, Pat, and I'm sorry you're without hot water today. Please write down the error "
                "code; you can also turn the unit off and back on once at its power switch. "
                "Is the address on file, 14 Birch Lane in Sagle, still the right one?")
        s = session(reply("end_turn", text(long)), reply("end_turn", text("Is 14 Birch Lane still right?")))
        s.respond("My tankless heater shows an error code.")
        s.respond("Okay, I wrote it down.")
        note = s.client.requests[1]["messages"][-1]["content"]
        self.assertIn("didn't hear this part of it", note)
        self.assertIn("14 Birch Lane in Sagle, still the right one?", note)
        self.assertNotIn("System note", s.messages[-2]["content"][:30])   # the caller's own words come first

    def test_anything_else_is_asked_once_a_call(self):
        s = session(reply("end_turn", text("You're booked. Is there anything else I can help with?")),
                    reply("end_turn", text("Yes, it's A1007. Is there anything else I can help with?")))
        s.respond("Book it.")
        self.assertEqual(s.respond("What was the number?").text, "Yes, it's A1007.")

    def test_text_is_released_a_sentence_at_a_time(self):
        heard = []
        s = session(reply("end_turn", text("Thanks, Pat. Is 14 Birch Lane still right?")))
        s.respond("Hi, it's Pat.", on_text=heard.append)
        self.assertEqual(heard, ["Thanks, Pat.", " Is 14 Birch Lane still right?"])


class SpeechHelperTests(unittest.TestCase):
    def test_short_phone_numbers(self):
        from receptionist.speech import short_phone_numbers
        self.assertEqual(short_phone_numbers("It's three one two, five five five, oh one seven."), ["312555017"])
        self.assertEqual(short_phone_numbers("three one two, five five five, oh one seven one"), [])
        self.assertEqual(short_phone_numbers("312-555-017 please"), ["312555017"])
        self.assertEqual(short_phone_numbers("I live at 482 Maple Street, confirmation A1006"), [])
        said = "three one two, five five five, oh one four... uh, four two? No, wait. Three one two, five five five, oh one four two."
        self.assertEqual(short_phone_numbers(said), [])   # corrected in the same breath

    def test_card_numbers_are_removed_and_never_mistaken_for_a_phone_number(self):
        from receptionist.speech import redact_card, short_phone_numbers
        said = "Quiero pagar ahora con tarjeta: cuatro uno uno uno, cuatro uno uno uno"
        self.assertEqual(short_phone_numbers(said), [])
        self.assertEqual(redact_card(said), "Quiero pagar ahora con tarjeta: [card number removed]")
        self.assertEqual(redact_card("My card is 4111 1111 1111 1111, exp 12/29"), "My card is [card number removed], exp 12/29")
        self.assertEqual(redact_card("Pay by card, and my number is 312-555-0177"), "Pay by card, and my number is 312-555-0177")
        self.assertEqual(redact_card("My number is 312 555 0177 1234"), "My number is 312 555 0177 1234")   # no card talk
        s = session(reply("end_turn", text("Please don't read me your card.")))
        s.respond("Let me read my credit card: 4111 1111 1111 1111")
        self.assertNotIn("4111", json.dumps(s.client.requests[0]["messages"]))
        self.assertNotIn("4111", json.dumps(s.transcript))

    def test_agent_is_told_about_a_short_number(self):
        s = session(reply("end_turn", text("Sorry, I missed part of that.")))
        s.respond("My number is three one two five five five oh one seven.")
        self.assertIn("only 9 digits", s.client.requests[0]["messages"][-1]["content"])
        self.assertEqual(s.transcript[-2]["text"], "My number is three one two five five five oh one seven.")  # transcript stays as said


class RecordingTests(unittest.TestCase):
    def test_speakable_reads_digits_and_drops_stage_directions(self):
        from receptionist.recording import lines, speakable
        self.assertEqual(speakable("Call 312-555-0142 about A1006. [pauses]"),
                         "Call 3 1 2, 5 5 5, 0 1 4 2 about A 1 0 0 6.")
        convo = lines("Agent: Hi.\nCaller: Hello,\nstill me.\n  [tool x {} -> {}]\nAgent: Okay.")
        self.assertEqual(convo, [("agent", "Hi."), ("caller", "Hello, still me."), ("agent", "Okay.")])


class UtteranceTests(unittest.TestCase):
    def test_every_tool_call_splits_a_reply(self):
        from receptionist.simulate import utterances
        turn = {"steps": [{"say": "I'm sorry."}, {"say": "One moment."}, {"tool": {"name": "check_availability"}},
                          {"say": "I have 2 PM."}, {"tool": {"name": "transfer_call"}}, {"say": "Nobody picked up."}]}
        self.assertEqual(utterances(turn), ["I'm sorry. One moment.", "I have 2 PM.", "Nobody picked up."])


class TestCostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {**CFG, "data_dir": self.tmp.name,
                    "simulate": {**CFG["simulate"], "budget_per_day": 0.05, "estimated_cost_per_scenario": 0.015}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_budget_ledger(self):
        from receptionist import budget
        self.assertEqual(budget.remaining(self.cfg), 0.05)
        budget.record(self.cfg, 0.03)
        self.assertAlmostEqual(budget.remaining(self.cfg), 0.02)
        self.assertAlmostEqual(budget.check_affordable(self.cfg, 1), 0.015)
        with self.assertRaises(SystemExit):
            budget.check_affordable(self.cfg, 2)
        budget.remember_per_scenario(self.cfg, 0.004)            # a cheap run never lowers the estimate
        self.assertEqual(budget.per_scenario_estimate(self.cfg), 0.015)
        budget.remember_per_scenario(self.cfg, 0.02)             # an expensive one raises it
        self.assertEqual(budget.per_scenario_estimate(self.cfg), 0.02)

    def test_run_refuses_before_spending_anything(self):
        from receptionist.simulate import run_all
        fake = FakeClient()
        with self.assertRaises(SystemExit) as e:
            run_all(self.cfg, BIZ, fake, None, None)
        self.assertIn("testing budget", str(e.exception))
        self.assertEqual((fake.requests, fake.create_requests), ([], []))

    def test_grader_request_is_cached_and_cheap(self):
        from receptionist.simulate import grade
        fake = FakeClient(created=[reply("end_turn", text(json.dumps({"items": [{"index": 1, "evidence": "Said so.", "pass": True}]})))])
        items, usage = grade(fake, CFG, BIZ, "Agent: Hi", ["Was polite."], "# This call")
        self.assertEqual(items[0]["pass"], True)
        req = fake.create_requests[0]
        self.assertEqual((req["model"], req["output_config"]["effort"]), (CFG["models"]["grader"], CFG["models"]["grader_effort"]))
        self.assertIn("cache_control", req["system"][1])
        self.assertIn("<agent_was_given>", req["system"][1]["text"])
        self.assertNotIn("<agent_was_given>", req["messages"][0]["content"])   # per-call text stays after the cache
        self.assertEqual(usage["cache_read"], 50)

    def test_failed_call_is_not_sent_to_the_grader(self):
        from receptionist.simulate import run_scenario
        fake = FakeClient(reply("end_turn", text("Happy to help.")),
                          created=[reply("end_turn", text("Hi, I need a plumber. [HANGUP]"))])
        sc = {"id": "t", "title": "t", "at": "2026-10-27T10:15", "caller_id": "+13125550177", "persona": "x",
              "expect": {"booked": True}, "rubric": ["Booked it."]}
        r = run_scenario(fake, CFG, BIZ, sc)
        self.assertFalse(r["passed"])
        self.assertEqual(len(fake.create_requests), 1)            # the caller only, no grader
        self.assertEqual(r["grader_cost"], 0)
        self.assertTrue(all(i["pass"] is None for i in r["rubric"]))
        self.assertEqual(fake.create_requests[0]["model"], "claude-haiku-5-5")


class PlatformTests(unittest.TestCase):
    def test_business_is_chosen_by_dialed_number(self):
        self.assertEqual(business_for_number(CFG, "(312) 555-0100")["id"], "acme")
        self.assertIsNone(business_for_number(CFG, "+13125559999"))

    def test_vapi_parsing(self):
        msg = {"type": "tool-calls", "call": {"id": "c1", "customer": {"number": "+13125550177"},
                                              "phoneNumber": {"number": "+13125550100"}},
               "toolCallList": [{"id": "tc1", "function": {"name": "lookup_customer", "arguments": '{"phone": "3125550142"}'}}],
               "artifact": {"messages": [{"role": "system", "message": "x"}, {"role": "assistant", "message": "Hi"},
                                         {"role": "user", "message": "Hello"}]}}
        self.assertEqual(platforms.vapi_call_info(msg), ("c1", "+13125550100", "+13125550177"))
        self.assertEqual(platforms.vapi_tool_calls(msg), [("tc1", "lookup_customer", {"phone": "3125550142"})])
        self.assertEqual([t["role"] for t in platforms.vapi_transcript(msg)], ["agent", "caller"])

    def test_vapi_assistant(self):
        a = platforms.vapi_assistant(CFG, BIZ, "https://x.test", "# This call\nnow", token="tok")
        names = [t.get("function", {}).get("name") or t["type"] for t in a["model"]["tools"]]
        self.assertIn("book_appointment", names)
        self.assertNotIn("transfer_call", names)
        self.assertIn("transferCall", names)
        self.assertEqual(a["model"]["model"], "claude-haiku-5-5")
        self.assertTrue(a["model"]["messages"][0]["content"].endswith("now"))
        self.assertEqual(a["server"]["headers"]["Authorization"], "Bearer tok")
        book = next(t for t in a["model"]["tools"] if t.get("function", {}).get("name") == "check_availability")
        self.assertEqual(book["messages"][0]["content"], "Let me take a look at the schedule.")

    def test_retell_llm(self):
        llm = platforms.retell_llm(CFG, BIZ, "https://x.test")
        names = {t["name"]: t["type"] for t in llm["general_tools"]}
        self.assertEqual(names["transfer_call_on_call"], "transfer_call")
        self.assertEqual(names["end_call"], "end_call")
        self.assertEqual(names["book_appointment"], "custom")
        lookup = next(t for t in llm["general_tools"] if t["name"] == "lookup_customer")
        self.assertEqual((lookup["execution_message_type"], lookup["execution_message_description"]),
                         ("static_text", "Let me pull that up."))
        self.assertIn("{{call_state}}", llm["general_prompt"])
        self.assertEqual(llm["model_config"]["primary"]["model"], "claude-5.5-haiku")

    def test_retell_signature(self):
        import hashlib, hmac
        body, key, ms = b'{"event":"call_ended"}', "key_123", 1_800_000_000_000
        sig = f"v={ms},d=" + hmac.new(key.encode(), body + str(ms).encode(), hashlib.sha256).hexdigest()
        self.assertTrue(platforms.retell_signature_ok(body, key, sig, now_ms=ms + 1000))
        self.assertFalse(platforms.retell_signature_ok(body + b" ", key, sig, now_ms=ms))
        self.assertFalse(platforms.retell_signature_ok(body, key, sig, now_ms=ms + 6 * 60 * 1000))
        self.assertFalse(platforms.retell_signature_ok(body, key, None))

    def test_retell_transcript_fallback(self):
        call = {"transcript": "Agent: Hi there\nUser: My drain is slow\n"}
        self.assertEqual(platforms.retell_transcript(call),
                         [{"role": "agent", "text": "Hi there"}, {"role": "caller", "text": "My drain is slow"}])


class ServerTests(unittest.TestCase):
    """The webhook server against a temporary data folder, without network or API calls."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from receptionist.server import create_app
        self.tmp = tempfile.TemporaryDirectory()
        cfg = {**CFG, "data_dir": self.tmp.name}
        os.environ["VAPI_SERVER_TOKEN"] = "tok"
        os.environ.pop("RETELL_API_KEY", None)
        os.environ["PUBLIC_BASE_URL"] = "https://example.test"
        self.client = TestClient(create_app(cfg))
        self.auth = {"Authorization": "Bearer tok"}

    def tearDown(self):
        for k in ("VAPI_SERVER_TOKEN", "PUBLIC_BASE_URL"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def vapi(self, message, headers=None):
        return self.client.post("/vapi/webhook", json={"message": message}, headers=self.auth if headers is None else headers)

    def test_vapi_requires_token(self):
        self.assertEqual(self.vapi({"type": "status-update"}, headers={}).status_code, 401)

    def test_vapi_assistant_request_and_tool_call(self):
        call = {"id": "c9", "customer": {"number": "+13125550177"}, "phoneNumber": {"number": "+13125550100"}}
        r = self.vapi({"type": "assistant-request", "call": call}).json()
        self.assertEqual(r["assistant"]["firstMessage"], BIZ["greeting"])
        self.assertIn("https://example.test/vapi/webhook", json.dumps(r))
        r = self.vapi({"type": "tool-calls", "call": call, "toolCallList": [
            {"id": "tc1", "function": {"name": "lookup_customer", "arguments": {"phone": "nope"}}},
            {"id": "tc2", "function": {"name": "lookup_customer", "arguments": {"phone": "3125550199"}}}]}).json()
        self.assertIn("error", r["results"][0])
        self.assertEqual(json.loads(r["results"][1]["result"]), {"found": False})

    def test_vapi_unknown_number(self):
        call = {"id": "c1", "phoneNumber": {"number": "+13125559999"}}
        self.assertIn("error", self.vapi({"type": "assistant-request", "call": call}).json())

    def test_retell_inbound_and_tool(self):
        r = self.client.post("/retell/inbound", json={"event": "call_inbound", "call_inbound": {
            "call_id": "r1", "from_number": "+13125550142", "to_number": "+13125550100"}}).json()
        self.assertIn("Pat Rivera", r["call_inbound"]["dynamic_variables"]["call_state"])
        r = self.client.post("/retell/tool/check_availability", json={
            "name": "check_availability", "args": {"service_id": "drain_cleaning"},
            "call": {"call_id": "r1", "from_number": "+13125550142", "to_number": "+13125550100"}}).json()
        self.assertTrue(r["windows"])
        r = self.client.post("/retell/inbound", json={"call_inbound": {"to_number": "+13125559999"}}).json()
        self.assertEqual(r, {"call_inbound": {"reject": True}})


class LiveCallTests(unittest.TestCase):
    """A simulated Retell on the other end of the custom-LLM websocket, with a scripted fake Claude."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from receptionist.server import create_app
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(RETELL_WS_SECRET="s3cret", DASHBOARD_TOKEN="dash")
        summary = {"summary": "Booked a leak repair.", "caller_name": "Jordan", "callback_number": "3125550177",
                   "intent": "new_job", "outcome": "booked", "urgency": "this_week", "follow_up": "", "flags": []}
        now = schedule.local_now(BIZ)
        self.target = "office" if schedule.is_open(BIZ, now) else "on_call"
        self.fake = FakeClient(
            reply("tool_use", use("t1", "check_availability", {"service_id": "leak_repair"})),
            reply("end_turn", text("I have an opening soon.")),
            reply("tool_use", text("Let me connect you now."), use("t2", "transfer_call", {"target": self.target, "reason": "x"})),
            reply("end_turn", text("Nobody picked up. Can I take a message?")),
            created=[reply("end_turn", text(json.dumps(summary)))])
        self.app = create_app({**CFG, "data_dir": self.tmp.name}, client=self.fake, warm_cache=False)
        self.client = TestClient(self.app)

    def tearDown(self):
        for k in ("RETELL_WS_SECRET", "DASHBOARD_TOKEN"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def until_complete(self, ws, rid):
        parts = []
        while True:
            m = ws.receive_json()
            if m.get("response_type") == "response" and m.get("response_id") == rid:
                parts.append(m["content"])
                if m["content_complete"]:
                    return "".join(parts), m

    def test_a_live_call_end_to_end(self):
        greeting = BIZ["greeting"]
        with self.client.websocket_connect("/llm-websocket/s3cret/call123") as ws:
            self.assertEqual(ws.receive_json()["response_type"], "config")
            ws.send_json({"interaction_type": "call_details", "call": {
                "call_id": "call123", "from_number": "+13125550177", "to_number": "+13125550100"}})
            first = ws.receive_json()
            self.assertEqual((first["response_id"], first["content"]), (0, greeting))
            ws.send_json({"interaction_type": "ping_pong", "timestamp": 42})
            self.assertEqual(ws.receive_json(), {"response_type": "ping_pong", "timestamp": 42})
            ws.send_json({"interaction_type": "response_required", "response_id": 1, "transcript": [
                {"role": "agent", "content": greeting}, {"role": "user", "content": "My sink is leaking."}]})
            said, final = self.until_complete(ws, 1)
            self.assertEqual(said, "Let me take a look at the schedule. I have an opening soon.")
            self.assertNotIn("end_call", final)
            ws.send_json({"interaction_type": "response_required", "response_id": 2, "transcript": [
                {"role": "agent", "content": "I have an opening soon."}, {"role": "user", "content": "I want a person."}]})
            said, final = self.until_complete(ws, 2)
            # The demo's transfer numbers are 555-01xx placeholders: a live call never dials them.
            self.assertEqual(said, "Let me connect you now. Nobody picked up. Can I take a message?")
            self.assertNotIn("transfer_number", final)
            self.assertIn("no_answer", self.fake.requests[-1]["messages"][-1]["content"][0]["content"])
        # The agent saw exactly what the caller said, and the call was stored with its summary.
        self.assertEqual(self.fake.requests[0]["messages"][-1], {"role": "user", "content": "My sink is leaking."})
        page = self.client.get("/dashboard", params={"token": "dash", "business": "acme"}).text
        self.assertIn("Booked a leak repair.", page)
        self.assertEqual(self.client.get("/dashboard", params={"token": "nope"}).status_code, 404)

    def test_wrong_secret_and_unknown_number(self):
        from starlette.websockets import WebSocketDisconnect as Closed
        with self.assertRaises(Closed):
            with self.client.websocket_connect("/llm-websocket/wrong/call1") as ws:
                ws.receive_json()
        with self.client.websocket_connect("/llm-websocket/s3cret/call2") as ws:
            ws.receive_json()
            ws.send_json({"interaction_type": "call_details", "call": {"call_id": "call2", "to_number": "+13125559999"}})
            self.assertTrue(ws.receive_json()["end_call"])

    def open_call(self, ws, call_id):
        ws.receive_json()
        ws.send_json({"interaction_type": "call_details", "call": {
            "call_id": call_id, "from_number": "+13125550177", "to_number": "+13125550100"}})
        ws.receive_json()

    def ask(self, ws, rid, *lines):
        """A response_required whose transcript alternates agent and user lines, starting with the greeting."""
        transcript = [{"role": "agent", "content": BIZ["greeting"]}]
        transcript += [{"role": "user" if n % 2 == 0 else "agent", "content": t} for n, t in enumerate(lines)]
        ws.send_json({"interaction_type": "response_required", "response_id": rid, "transcript": transcript})

    def test_after_the_goodbye_nothing_more_is_said(self):
        self.fake.script[:] = [reply("tool_use", text("Take care, goodbye."), use("t1", "end_call", {"reason": "completed"}))]
        with self.client.websocket_connect("/llm-websocket/s3cret/call7") as ws:
            self.open_call(ws, "call7")
            self.ask(ws, 1, "That's all, thanks.")
            said, final = self.until_complete(ws, 1)
            self.assertEqual((said, final.get("end_call")), ("Take care, goodbye.", True))
            self.ask(ws, 2, "That's all, thanks.", "Take care, goodbye.", "You too.")   # said during the goodbye
            said, final = self.until_complete(ws, 2)
            self.assertEqual((said, final.get("end_call")), ("", True))
        self.assertEqual(len(self.fake.requests), 1)   # no model call after the hang-up

    def test_a_reply_the_caller_talked_over_is_dropped(self):
        gate = threading.Event()
        first_stream = self.fake.stream

        def stream(**kw):   # the first turn hangs until released, like a slow model
            s = first_stream(**kw)
            return _Gated(s, gate) if len(self.fake.requests) == 1 else s
        self.fake.stream = stream
        self.fake.script[:] = [reply("end_turn", text("Is it leaking now?")), reply("end_turn", text("Got it, a roof leak."))]
        with self.client.websocket_connect("/llm-websocket/s3cret/call8") as ws:
            self.open_call(ws, "call8")
            self.ask(ws, 1, "Leaking room.")
            self.ask(ws, 2, "Leaking roof.")   # the transcript was corrected before the first reply played
            said, _ = self.until_complete(ws, 2)
            gate.set()
        self.assertEqual(said, "Got it, a roof leak.")
        self.assertEqual(self.fake.requests[1]["messages"], [{"role": "user", "content": "Leaking roof."}])

    def test_a_finished_reply_the_caller_never_heard_is_taken_back(self):
        self.fake.script[:] = [reply("end_turn", text("What's going on?")), reply("end_turn", text("A drip, got it."))]
        with self.client.websocket_connect("/llm-websocket/s3cret/call9") as ws:
            self.open_call(ws, "call9")
            self.ask(ws, 1, "It's leaking.")
            self.until_complete(ws, 1)
            # The caller kept going before "What's going on?" played: Retell's transcript has no new agent line.
            self.ask(ws, 2, "It's leaking. Just a slow drip.")
            said, _ = self.until_complete(ws, 2)
        self.assertEqual(said, "A drip, got it.")
        self.assertEqual(self.fake.requests[1]["messages"], [{"role": "user", "content": "It's leaking. Just a slow drip."}])


    def test_a_dropped_turn_loses_neither_the_caller_s_words_nor_the_last_heard_reply(self):
        gate = threading.Event()
        first_stream = self.fake.stream

        def stream(**kw):   # the second model call hangs until released: the caller talks over it
            s = first_stream(**kw)
            return _Gated(s, gate) if len(self.fake.requests) == 2 else s
        self.fake.stream = stream
        self.fake.script[:] = [reply("end_turn", text("What's going on with the sink?")),
                               reply("end_turn", text("Dropped.")), reply("end_turn", text("Oh no, how bad?"))]
        greeting = BIZ["greeting"]
        heard = [{"role": "agent", "content": greeting}, {"role": "user", "content": "My sink needs fixing."},
                 {"role": "agent", "content": "What's going on with the sink?"}]
        with self.client.websocket_connect("/llm-websocket/s3cret/call10") as ws:
            self.open_call(ws, "call10")
            ws.send_json({"interaction_type": "response_required", "response_id": 1, "transcript": heard[:2]})
            self.until_complete(ws, 1)
            ws.send_json({"interaction_type": "response_required", "response_id": 2, "transcript": heard + [
                {"role": "user", "content": "It's leaking."}]})
            time.sleep(0.4)
            ws.send_json({"interaction_type": "response_required", "response_id": 3, "transcript": heard + [
                {"role": "user", "content": "It's leaking."}, {"role": "user", "content": "Really bad."}]})
            said, _ = self.until_complete(ws, 3)
            gate.set()
        self.assertEqual(said, "Oh no, how bad?")
        messages = self.fake.requests[2]["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])   # the heard reply stays
        self.assertEqual((messages[0]["content"], messages[2]["content"]),
                         ("My sink needs fixing.", "It's leaking. Really bad."))

    def test_garbled_audio_gets_an_instant_reply(self):
        with self.client.websocket_connect("/llm-websocket/s3cret/garbled") as ws:
            self.open_call(ws, "garbled")
            ws.send_json({"interaction_type": "response_required", "response_id": 1, "transcript": [
                {"role": "agent", "content": "Hi"}, {"role": "user", "content": "(unintelligible audio)"}]})
            said, _ = self.until_complete(ws, 1)
        self.assertEqual(said, "Sorry, I didn't catch that. Could you say that again?")
        self.assertEqual(self.fake.requests, [])                       # no model call needed

    def test_a_slow_reply_never_leaves_dead_air(self):
        import time
        from receptionist import live
        original_stream, original_wait = self.fake.stream, live.SLOW_REPLY_S
        def slow_stream(**kw):
            time.sleep(0.6)
            return original_stream(**kw)
        self.fake.stream, live.SLOW_REPLY_S = slow_stream, 0.2
        try:
            with self.client.websocket_connect("/llm-websocket/s3cret/slow") as ws:
                self.open_call(ws, "slow")
                ws.send_json({"interaction_type": "response_required", "response_id": 1, "transcript": [
                    {"role": "agent", "content": "Hi"}, {"role": "user", "content": "My sink is leaking."}]})
                said, _ = self.until_complete(ws, 1)
        finally:
            self.fake.stream, live.SLOW_REPLY_S = original_stream, original_wait
        self.assertTrue(said.startswith("Okay, one sec. Let me take a look at the schedule."), said)

    def test_new_caller_text(self):
        from receptionist.live import new_caller_text
        t = [{"role": "agent", "content": "Hi"}, {"role": "user", "content": "My sink"}, {"role": "user", "content": "is leaking."}]
        self.assertEqual(new_caller_text(t), "My sink is leaking.")
        self.assertEqual(new_caller_text([{"role": "agent", "content": "Hi"}]), "")



class VoiceFormTests(unittest.TestCase):
    def test_confirmation_numbers_are_read_digit_by_digit(self):
        from receptionist.live import for_voice
        self.assertEqual(for_voice("Your confirmation is A1007."), "Your confirmation is A, 1, 0, 0, 7.")
        self.assertEqual(for_voice("Call 312-555-0142 at 2 PM."), "Call 312-555-0142 at 2 PM.")


class InterruptionTests(unittest.TestCase):
    def test_what_the_caller_heard_of_an_interrupted_reply(self):
        from receptionist.live import cut_short
        said = "That's today between 2 PM and 4 PM at 14 Birch Lane in Sagle. Shall I book it?"
        self.assertEqual(cut_short("That's today between 2 PM and 4 PM at 14 Bir", said),
                         "That's today between 2 PM and 4 PM at 14 Bir")
        self.assertIsNone(cut_short(said, said))                       # heard it all
        self.assertIsNone(cut_short("Something else entirely was said", said))
        self.assertEqual(cut_short("Okay, one sec. That's today between 2", said), "That's today between 2")

    def test_the_agent_is_told_what_the_caller_missed(self):
        s = session(reply("end_turn", text("Sure, what's the address?")))
        s.respond("Actually it's Thursday, not today.", heard_only="That's today between 2 PM")
        self.assertIn("they heard only \"That's today between 2 PM\"", s.client.requests[0]["messages"][-1]["content"])

    def test_a_reply_that_was_all_held_back_gets_one_retry(self):
        s = session(reply("end_turn", text("The caller wants a price, so I should explain the policy.")),
                    reply("end_turn", text("The technician gives a firm price on site.")))
        turn = s.respond("How much is it?")
        self.assertEqual(turn.text, "The technician gives a firm price on site.")
        self.assertIn("heard nothing from you", s.client.requests[1]["messages"][-1]["content"])
        roles = [m["role"] for m in s.messages]
        self.assertEqual(roles, ["user", "assistant"])   # history still alternates

    def test_spanish_emergencies_get_the_safety_line_from_code(self):
        s = session(reply("end_turn", text("¿Me da su nombre y dirección?")))
        turn = s.respond("¡Ayuda! Se reventó un tubo en el sótano y se está inundando.")
        self.assertTrue(turn.text.startswith("Si es seguro, cierre la llave de paso principal"))
        s = session()
        turn = s.respond("Hola, huele mucho a gas en la cocina.")
        self.assertTrue(turn.ended)
        self.assertTrue(turn.text.endswith("Cuando esté a salvo afuera, puede volver a llamarnos."))


class _Gated:
    """A fake stream that waits for `gate` before each event."""

    def __init__(self, stream, gate):
        self.stream, self.gate = stream, gate

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for ev in self.stream:
            self.gate.wait(5)
            yield ev

    def get_final_message(self):
        return self.stream.get_final_message()


class TurnStateTests(unittest.TestCase):
    def test_a_dropped_turn_leaves_the_session_unchanged(self):
        from receptionist.agent import TurnControl, TurnDropped
        s = session(reply("end_turn", text("Hello there.")))
        control = TurnControl()
        control.drop()
        with self.assertRaises(TurnDropped):
            s.respond("Hi", control=control)
        self.assertEqual((s.messages, len(s.transcript)), ([], 1))

    def test_only_turns_without_side_effects_can_be_taken_back(self):
        s = session(reply("end_turn", text("What's the address?")))
        s.respond("I need a plumber.")
        self.assertTrue(s.revert_last())
        self.assertEqual((s.messages, len(s.transcript)), ([], 1))
        s = session(reply("tool_use", use("t1", "take_message", {"caller_name": "Jo", "callback_number": "3125550177",
                                                                  "message": "Call me.", "urgent": False})),
                    reply("end_turn", text("I've passed that on.")))
        s.respond("Please have someone call me.")
        self.assertFalse(s.revert_last())

    def test_drop_is_refused_once_a_turn_is_locked_in(self):
        from receptionist.agent import TurnControl
        control = TurnControl()
        self.assertTrue(control.proceed(irreversible=True))
        self.assertFalse(control.drop())


if __name__ == "__main__":
    unittest.main()
