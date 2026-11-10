"""Sounding human: no 'the crew is out' greeting, varied fixed lines, the caller's tone, polite reactions to being cut
off, a polite cut-in on a rambling caller, and the one-hour prompt cache."""
import os
import random
import re
import tempfile
import time
import unittest

import tests  # noqa: F401  (runs tests/__init__.py: the older tests use the <say> channel's rollback setting)
from receptionist import live, prompts, schedule, tools
from receptionist.agent import (REPEAT_VARIANTS, CallSession, cache_control, cache_write_mult, prewarm, usage_cost)
from receptionist.config import all_businesses, load_business, load_config
from receptionist.store import Store
from tests.test_logic import FakeClient, reply, text

CFG = load_config()
LAWN = load_business(CFG, "lawn_care")
ACME = load_business(CFG, "acme")
NOW = schedule.local_now(LAWN, "2026-11-10T10:15")


def lawn_session(*script, created=(), cfg=CFG):
    return CallSession(FakeClient(*script, created=created), cfg, LAWN, Store(":memory:"), NOW, caller="+12085550177")


class PromptFeelTests(unittest.TestCase):
    def test_no_greeting_says_the_crew_is_out_or_nobody_answered(self):
        bad = re.compile(r"\bout on\b|\bout in\b|\bout working\b|tied up|\bcrews?\b|team'?s? out|nobody|no one|mowers are running",
                         re.I)
        for biz in all_businesses(CFG):
            for g in biz.get("greetings", []) + [biz["greeting"]]:
                self.assertNotRegex(g, bad, f"{biz['id']}: {g}")
                self.assertNotIn("?", g)

    def test_the_call_state_tells_the_model_not_to_announce_overflow(self):
        state = prompts.call_state(LAWN, NOW, "+12085550177", None, [])
        self.assertIn("Never say that", state)
        self.assertNotIn("the team is out on a", state)

    def test_the_new_speaking_rules_are_in_every_prompt(self):
        for biz in (LAWN, ACME):
            p = prompts.stable_prompt(biz)
            for needle in ("Contractions, everyday words, no script", "Know the business like a long-time employee",
                           "Read the caller's punctuation as tone", "answer in your first few words, plainly"):
                self.assertIn(needle, p, biz["id"])

    def test_the_ai_disclosure_rule_is_untouched_and_the_joke_comes_after_the_plain_answer(self):
        p = prompts.stable_prompt(LAWN)
        original = ("You are a virtual assistant. You don't need to bring that up yourself, but whenever a caller asks "
                    "whether you're a person, a robot or an AI, say plainly that you're Cedar Ridge Lawn and Snow's "
                    "virtual assistant. Never claim or suggest that you're human.")
        self.assertIn(original, p)
        witty = p[p.index("When someone asks whether they're talking to a person"):].split("\n")[0]
        self.assertLess(witty.index("plainly"), witty.index("witty"))      # honest first, then the joke
        self.assertIn("never implies you're a person", witty)
        self.assertIn("Never dodge the question", witty)

    def test_the_business_knowledge_reaches_the_prompt_and_stays_out_of_shops_without_it(self):
        p = prompts.stable_prompt(LAWN)
        self.assertIn("About the business. This is what you know from working here", p)
        self.assertIn("about fifteen years", p)
        self.assertNotIn("About the business.", prompts.stable_prompt(ACME))   # no `about`, no empty section

    def test_no_stock_acknowledgement_is_quoted_as_an_example_to_copy(self):
        rules = prompts.PLATFORM_RULES
        self.assertNotIn('"Yeah, we can do that.", "No problem.", "Got it."', rules)
        self.assertNotIn('quick "Perfect."', rules)


class FixedLineTests(unittest.TestCase):
    def test_goodbyes_vary_but_safety_and_spam_lines_are_fixed(self):
        ctx = tools.ToolContext(LAWN, Store(":memory:"), NOW, None, "c")
        seen = {tools.end_line(ctx, "completed", random.Random(i)) for i in range(60)}
        self.assertGreaterEqual(len(seen), 4)
        self.assertEqual(tools.end_line(ctx, "safety"), tools.END_LINES["en"]["safety"])
        self.assertEqual(tools.end_line(ctx, "spam"), tools.END_LINES["en"]["spam"])
        last = None
        for i in range(40):                                              # never the same goodbye twice running
            line = tools.end_line(ctx, "completed", random.Random(i))
            self.assertNotEqual(line, last)
            last = line

    def test_the_lines_the_code_speaks_on_its_own_come_in_pools_and_do_not_repeat_back_to_back(self):
        for pool in (live.REMINDER, live.NOT_HEARD, live.TROUBLE):
            for lang in ("en", "es"):
                self.assertGreaterEqual(len(pool[lang]), 2)
        live._last_line.clear()
        picks = [live.line(live.NOT_HEARD, "en", "not_heard") for _ in range(50)]
        self.assertTrue(all(a != b for a, b in zip(picks, picks[1:])))
        self.assertGreaterEqual(len(set(picks)), 4)
        self.assertGreaterEqual(len(REPEAT_VARIANTS), 4)


class ToneTests(unittest.TestCase):
    AGENT = "We can do snow removal on a seasonal contract. Want me to book an estimate?"

    def test_a_phrase_said_back_with_a_question_mark_is_a_question(self):
        note = live.echo_note("Snow removal?", self.AGENT)
        self.assertIn("rising, questioning tone", note)
        self.assertIn("Don't book", note)

    def test_the_same_words_with_a_period_are_an_answer(self):
        note = live.echo_note("Snow removal.", self.AGENT)
        self.assertIn("flat tone", note)
        self.assertIn("probably their answer", note)

    def test_other_replies_get_no_note(self):
        self.assertIsNone(live.echo_note("I need my lawn mowed on Thursdays please.", self.AGENT))   # too long, not an echo
        self.assertIsNone(live.echo_note("Mowing?", self.AGENT))                                       # not what we said
        self.assertIsNone(live.echo_note("", self.AGENT))

    def test_the_note_and_the_polite_reaction_reach_the_model(self):
        s = lawn_session(reply("end_turn", text("Sure, go ahead?")))
        s.respond("Snow removal?", note=live.echo_note("Snow removal?", self.AGENT), heard_only="We can do snow")
        last = s.client.requests[0]["messages"][-1]["content"]
        self.assertIn("System note: the caller repeated part of what you just said", last)
        self.assertIn("open with a tiny reaction of a few words", last)
        self.assertEqual(s.transcript[1]["text"], "Snow removal?")       # the notes never enter the saved transcript


class CutInTests(unittest.TestCase):
    def test_a_cut_in_is_one_short_line_with_tools_off_and_nothing_saved_to_history(self):
        s = lawn_session(created=[reply("end_turn", text("Sorry to jump in, I've got the lawn part. What's the address?"))])
        said = s.interject("I need my lawn mowed and then there's the hedge and my neighbor's tree and so on")
        self.assertEqual(said, "Sorry to jump in, I've got the lawn part. What's the address?")
        request = s.client.create_requests[0]
        self.assertEqual(request["tool_choice"], {"type": "none"})
        self.assertTrue(request["tools"])                          # tools stay listed so the cached prompt is reused
        self.assertEqual(s.messages, [])                           # not in the model's history
        s.note_interjection(said)
        self.assertEqual(s.transcript[-1], {"role": "agent", "text": said, "interjection": True})

    def test_the_next_turn_is_told_about_the_cut_in_once(self):
        s = lawn_session(reply("end_turn", text("Got it?")), reply("end_turn", text("And the town?")))
        s.note_interjection("Sorry to jump in, what's the address?")
        s.respond("It's 12 Pine Street.")
        self.assertIn("you cut in and said", s.client.requests[0]["messages"][-1]["content"])
        s.respond("Sandpoint.")
        self.assertNotIn("you cut in", s.client.requests[1]["messages"][-1]["content"])

    def test_unusable_cut_ins_are_dropped(self):
        too_long = " ".join(["word"] * 40)
        for out in ("", too_long):
            s = lawn_session(created=[reply("end_turn", text(out))])
            self.assertIsNone(s.interject("blah " * 70))


class CutInHarness:
    """A simulated Retell sends live transcript updates while the caller talks. Shared by the cut-in test classes."""

    RAMBLE = " ".join(["so", "my", "lawn", "is", "a", "mess", "and", "the", "neighbor"] * 8)      # 72 words

    def start(self, cutin_cfg=None, created=None):
        from fastapi.testclient import TestClient
        from receptionist.server import create_app
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(RETELL_WS_SECRET="s3cret", DASHBOARD_TOKEN="dash-admin-key-" + "x" * 16)
        cfg = {**CFG, "data_dir": self.tmp.name}
        if cutin_cfg is not None:
            cfg["live"] = {"interject": cutin_cfg}
        made = created if created is not None else [reply("end_turn", text("Sorry to jump in, I've got the lawn. What's the address?"))]
        self.fake = FakeClient(created=made)
        return TestClient(create_app(cfg, client=self.fake, warm_cache=False))

    def tearDown(self):
        for k in ("RETELL_WS_SECRET", "DASHBOARD_TOKEN"):
            os.environ.pop(k, None)
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def call(self, client, updates):
        """Open a call, send each transcript as an update, then ping; return every message seen up to the pong."""
        seen = []
        with client.websocket_connect("/llm-websocket/s3cret/cutin") as ws:
            ws.receive_json()
            ws.send_json({"interaction_type": "call_details", "call": {
                "call_id": "cutin", "from_number": "+12085550177", "to_number": "+12084286760"}})
            greeting = ws.receive_json()["content"]
            for user_text in updates:
                ws.send_json({"interaction_type": "update_only", "transcript": [
                    {"role": "agent", "content": greeting}, {"role": "user", "content": user_text}]})
                time.sleep(0.4)
            ws.send_json({"interaction_type": "ping_pong", "timestamp": 7})
            while True:
                m = ws.receive_json()
                if m.get("response_type") == "ping_pong":
                    return seen
                seen.append(m)

    def interrupts(self, seen):
        return [m for m in seen if m.get("response_type") == "agent_interrupt"]


class LiveCutInTests(CutInHarness, unittest.TestCase):
    def test_a_rambling_caller_gets_one_polite_cut_in(self):
        seen = self.interrupts(self.call(self.start(), [self.RAMBLE, self.RAMBLE + " and also the fence"]))
        self.assertEqual(len(seen), 1)                              # once per turn, even as the transcript keeps growing
        m = seen[0]
        self.assertEqual(m["content"], "Sorry to jump in, I've got the lawn. What's the address?")
        self.assertTrue(m["content_complete"] and m["no_interruption_allowed"])
        self.assertEqual(self.fake.create_requests[0]["tool_choice"], {"type": "none"})

    def test_a_short_turn_gets_no_cut_in(self):
        self.assertEqual(self.interrupts(self.call(self.start(), ["I need my lawn mowed on Thursdays."])), [])

    def test_it_can_be_turned_off(self):
        client = self.start({"enabled": False})
        self.assertEqual(self.interrupts(self.call(client, [self.RAMBLE])), [])
        self.assertEqual(self.fake.create_requests, [])             # and costs nothing

    def test_an_emergency_is_never_cut_in_on(self):
        said = self.RAMBLE + " and now a power line is down in my yard sparking"
        self.assertEqual(self.interrupts(self.call(self.start(), [said])), [])
        self.assertEqual(self.fake.create_requests, [])

    def test_no_cut_in_if_the_model_has_nothing_usable_to_say(self):
        client = self.start(created=[reply("end_turn", text(" ".join(["blah"] * 50)))])
        self.assertEqual(self.interrupts(self.call(client, [self.RAMBLE])), [])


class RespondDontInterviewTests(unittest.TestCase):
    """From the user's test calls: it asked for the address right away and steered the whole conversation."""

    def test_a_recognized_caller_is_not_asked_to_confirm_their_address_up_front(self):
        customer = {"name": "Noah", "address": "906 Alder Street", "town": "Sandpoint", "notes": None}
        for biz in (LAWN,):
            state = prompts.call_state(biz, NOW, "+12085550142", customer, [])
            self.assertIn("You recognize this caller", state, biz["id"])
            self.assertIn("never open by asking for or confirming their address", state)
            self.assertNotIn("confirm the address on file is still right", state)

    def test_every_vertical_tells_the_agent_to_respond_first_and_steer_only_at_the_end(self):
        for biz in (LAWN, ACME, load_business(CFG, "glow")):
            p = prompts.stable_prompt(biz)
            for needle in ("You respond; you don't run an interview", "Don't open by asking for their name, address",
                           "Once everything they wanted is dealt with, offer the next step",
                           "# Who you are", "Don't steer back to your own agenda",
                           "This is what a booking needs, not the order of the conversation"):
                self.assertIn(needle, p, biz["id"])
            self.assertNotIn("Ask one new question at a time and end your reply with it", p)

    def test_the_stock_intake_order_is_no_longer_commanded(self):
        playbook = (prompts.PLAYBOOKS / "lawn_care.md").read_text()
        self.assertNotIn("Collect these one at a time, in a natural order, skipping anything you already know:", playbook)


class CacheTests(unittest.TestCase):
    def test_the_stable_prompt_is_cached_for_an_hour_by_default(self):
        self.assertEqual(cache_control(CFG), {"type": "ephemeral", "ttl": "1h"})
        self.assertEqual(cache_control({"agent": {"cache_ttl": "5m"}}), {"type": "ephemeral"})
        s = lawn_session()
        self.assertEqual(s.system[0]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        self.assertNotIn("cache_control", s.system[1])              # the per-call part is never cached

    def test_prewarm_uses_the_same_breakpoint_so_the_first_call_hits_it(self):
        fake = FakeClient(created=[reply("end_turn", text(""))])
        prewarm(fake, CFG, LAWN)
        self.assertEqual(fake.create_requests[0]["system"][0]["cache_control"], {"type": "ephemeral", "ttl": "1h"})

    def test_an_hour_of_cache_is_billed_at_twice_the_input_rate(self):
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 1_000_000}
        self.assertAlmostEqual(usage_cost(CFG, "claude-sonnet-5-5", usage, cache_write_mult(CFG)), 4.0)
        self.assertAlmostEqual(usage_cost(CFG, "claude-sonnet-5-5", usage), 2.5)



class SlowRamblerTests(CutInHarness, unittest.TestCase):
    """A long, slow turn (the 28-second, 59-word ramble from a real call) gets cut in on by time, not just word count."""

    def test_a_slow_talker_is_cut_in_on_by_time_even_under_the_word_count(self):
        slow = {"min_words": 20, "min_seconds": 0.5, "fast_words": 1000}
        said = " ".join(["so", "the", "lawn", "is", "really", "kind", "of", "patchy"] * 3)      # 24 words
        seen = self.interrupts(self.call(self.start(slow), [said, said + " and", said + " and also"]))
        self.assertEqual(len(seen), 1)

    def test_a_fast_short_burst_is_not_cut_in_on(self):
        slow = {"min_words": 20, "min_seconds": 30, "fast_words": 1000}
        said = " ".join(["so", "the", "lawn", "is", "really", "kind", "of", "patchy"] * 3)
        self.assertEqual(self.interrupts(self.call(self.start(slow), [said, said + " and"])), [])


if __name__ == "__main__":
    unittest.main()
