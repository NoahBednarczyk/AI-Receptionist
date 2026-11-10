import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import tests  # noqa: F401  (runs tests/__init__.py: the older tests use the <say> channel's rollback setting)
from receptionist import outbound, outbound_live as ol
from receptionist.store import Store
from tests.test_outbound_live import CFG, LEAD, NOW

TZ = ZoneInfo("America/Los_Angeles")


def live_cfg(d, **extra):
    return {**CFG, "data_dir": d, "outbound": {"mode": "warm-only", **extra}}


class DialRecordTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = live_cfg(self.dir.name)
        Path(self.dir.name, "retell_outbound.json").write_text(json.dumps({"agent_id": "a", "number": "+12085550100"}))
        self.state = ol.State(Path(self.dir.name) / "outbound.json")
        self.lead = {**LEAD, "consent": "yes"}
        self.env = {"OUTBOUND_LIVE": "1"}

    def dial(self, when=NOW, call_id="c1"):
        return ol.place_call(self.cfg, self.lead, when, self.state, lambda *a: {"call_id": call_id}, env=self.env)

    def test_an_unanswered_dial_blocks_a_same_day_redial_and_counts_toward_the_week(self):
        self.dial()
        self.assertEqual(len(self.state.attempts("+12083043883")), 1)
        with self.assertRaisesRegex(RuntimeError, "already called today"):
            self.dial(NOW + timedelta(hours=2), "c2")
        self.dial(NOW + timedelta(days=1), "c3")
        with self.assertRaisesRegex(RuntimeError, "already tried 2 times this week"):
            self.dial(NOW + timedelta(days=2), "c4")

    def test_the_webhook_row_appears_once(self):
        self.dial()
        call = {"call_id": "c1", "disconnection_reason": "dial_no_answer", "duration_ms": 0}
        row = ol.webhook_result(self.state, self.cfg, call, NOW)
        self.assertEqual((row["outcome"], row["callId"]), ("No answer", "c1"))
        self.assertIsNone(ol.webhook_result(self.state, self.cfg, call, NOW))
        self.assertEqual(len(ol.State(self.state.path).data["calls"]), 1)

    def test_a_machine_is_a_voicemail_and_an_unknown_reason_is_no_answer(self):
        self.dial()
        row = ol.webhook_result(self.state, self.cfg, {"call_id": "c1", "disconnection_reason": "voicemail_reached", "duration_ms": 9000}, NOW)
        self.assertEqual(row["outcome"], "Voicemail")
        self.dial(NOW + timedelta(days=1), "c2")
        row = ol.webhook_result(self.state, self.cfg, {"call_id": "c2", "disconnection_reason": "something_new"}, NOW)
        self.assertEqual(row["outcome"], "No answer")

    def test_a_call_we_never_dialed_gets_no_row(self):
        self.assertIsNone(ol.webhook_result(self.state, self.cfg, {"call_id": "zzz"}, NOW))

    def test_the_sessions_own_result_replaces_the_webhook_row_and_adds_no_second_attempt(self):
        self.dial()
        store = Store(str(Path(self.dir.name) / "db.sqlite"))
        s = ol.OutboundSession(None, self.cfg, self.lead, self.state, store, NOW, call_id="retell-c1")
        ol.webhook_result(self.state, self.cfg, {"call_id": "c1", "disconnection_reason": "user_hangup"}, NOW)
        s.finish(NOW + timedelta(minutes=1), 1.0, "x")
        data = ol.State(self.state.path).data
        self.assertEqual(len(data["calls"]), 1)
        self.assertEqual(len(data["attempts"]["+12083043883"]), 1)
        self.assertEqual(data["calls"][0]["callId"], "c1")

    def test_an_outcome_blocks_next_week(self):
        for outcome in ("Not interested", "Demo booked", "Pilot signed"):
            ok, why = outbound.can_call(self.lead, NOW, self.cfg, [], set(), [{"outcome": outcome}])
            self.assertFalse(ok)
            self.assertIn(f"already has an outcome: {outcome}", why)
        self.assertTrue(outbound.can_call(self.lead, NOW, self.cfg, [], set(), [{"outcome": "No answer"}])[0])

    def test_a_callback_waits_for_its_date(self):
        row = [{"outcome": "Callback requested", "followDate": "2026-11-05"}]
        self.assertFalse(outbound.can_call(self.lead, NOW, self.cfg, [], set(), row)[0])
        self.assertTrue(outbound.can_call(self.lead, NOW + timedelta(days=2), self.cfg, [], set(), row)[0])

    def test_total_attempts_cap_and_test_leads_skip_it(self):
        tries = [NOW - timedelta(days=d) for d in (10, 12, 14, 16)]
        ok, why = outbound.can_call(self.lead, NOW, self.cfg, tries, set(), [])
        self.assertFalse(ok)
        self.assertIn("in all", why)
        self.assertTrue(outbound.can_call({**self.lead, "test": True}, NOW, self.cfg, tries, set(), [{"outcome": "Not interested"}])[0])

    def test_the_queue_reads_results(self):
        results = {"+12083043883": [{"outcome": "Not interested"}]}
        rows = outbound.plan_queue([self.lead], NOW, self.cfg, {}, set(), results)
        self.assertFalse(rows[0]["would_call"])

    def test_two_state_objects_on_one_file_keep_both_writes(self):
        a, b = ol.State(self.state.path), ol.State(self.state.path)   # both opened before either wrote
        a.add_dnc("+12085550111")
        b.set_slots(["2026-11-04T10:00:00-08:00"], NOW)
        b.add_attempt("+12085550122", NOW)
        c = ol.State(self.state.path)
        self.assertIn("+12085550111", c.dnc())
        self.assertIsNotNone(c.manager_slots(NOW))
        self.assertEqual(len(c.attempts("+12085550122")), 1)

    def test_spent_today_counts_an_unanswered_call_at_its_row_estimate(self):
        s = outbound.settings(self.cfg)
        self.dial()
        before = outbound.spent_today(self.state.data["calls"], self.state.data["attempts"], NOW, s)
        self.assertEqual(before, s["budget_reserve_usd"])
        ol.webhook_result(self.state, self.cfg, {"call_id": "c1", "disconnection_reason": "dial_no_answer"}, NOW)
        st = ol.State(self.state.path)
        after = outbound.spent_today(st.data["calls"], st.data["attempts"], NOW, s)
        self.assertEqual(after, outbound.est_cost(0, s))
        self.assertLess(after, before)


from tests import test_outbound_live as _t   # noqa: E402  (module import: the base classes must not be collected twice)
from tests.test_logic import reply, text   # noqa: E402


def last_note(client):
    return json.dumps(client.requests[-1]["messages"][-1])


class CodeSpokenIntroTests(_t.ScriptedFixture):
    def test_a_plain_yes_hello_or_whos_this_gets_intro_and_question_with_no_model_call(self):
        for answer in ("Yes, this is Dave.", "What can I do for you?", "Who is this?", "Yeah", "Speaking.", "Hello?"):
            s, said = self.make(), []
            turn = s.respond(answer, said.append)
            self.assertEqual(self.client.requests, [], answer)
            self.assertEqual(len(said), 2, answer)
            self.assertEqual(said[0], s.intro_text)
            self.assertIn(said[1].strip(), sum(ol.FIRST_QUESTIONS.values(), []))
            self.assertEqual(turn.text, f"{s.intro_text} {said[1].strip()}")
            self.assertTrue(s.protected(said[0]))     # line noise cannot cut off who is calling
            self.assertFalse(s.protected(said[1]))
            self.assertFalse(s.ended)
            self.assertEqual([m["role"] for m in s.messages], ["user", "assistant", "user", "assistant"])
            self.assertEqual(s.transcript[-1]["text"], turn.text)

    def test_anything_else_goes_to_the_model_with_the_intro_note(self):
        for answer in ("No, wrong number", "I'm busy", "Is this a robot?", "Why are you calling?", "Not interested", "stop calling me"):
            s = self.make(reply("end_turn", text("Hm.")))
            s.respond(answer)
            if ol.stop_request(answer):
                self.assertEqual(self.client.requests, [])
                continue
            self.assertEqual(len(self.client.requests), 1, answer)
            self.assertIn("say your name", last_note(self.client), answer)

    def test_the_disclosure_flag_puts_an_ai_assistant_in_the_spoken_intro(self):
        s, said = self.make(), []
        s.respond("Yes.", said.append)
        self.assertIn("AI assistant", said[0])
        quiet = self.make(cfg={"outbound": {"mode": "warm-only", "disclose_ai_in_intro": False}})
        said = []
        quiet.respond("Yes.", said.append)
        self.assertNotIn("AI assistant", said[0])

    def test_the_question_fits_the_trade_and_makes_no_claim(self):
        snow = ol.OutboundSession(None, live_cfg(self.dir.name), {**LEAD, "type": "Snow removal"}, self.state, self.store, NOW, call_id="t9")
        snow.respond("Hello?")
        said = []
        snow.respond("Yes", said.append)
        self.assertIn(said[1].strip(), ol.FIRST_QUESTIONS["snow"])
        for pool in ol.FIRST_QUESTIONS.values():
            self.assertEqual(len(pool), 3)
            for q in pool:
                self.assertNotRegex(q, r"\d")

    def test_after_the_intro_the_next_turn_is_a_normal_model_turn(self):
        s = self.make(reply("end_turn", text("Ha, yeah.")))
        s.respond("Yeah, this is Dale.")
        s.respond("About two weeks ago.")
        self.assertEqual(len(self.client.requests), 1)
        self.assertNotIn("say your name", last_note(self.client))


class TwoNoRuleTests(_t.ScriptedFixture):
    def test_the_second_calm_no_ends_the_call(self):
        s = self.make(*[reply("end_turn", text("Hm."))] * 3)
        s.respond("Why are you calling?")
        s.respond("Not interested.")
        self.assertIn("one sincere question", last_note(self.client))
        s.respond("No thanks.")
        self.assertIn("end_call", last_note(self.client))
        self.assertIn("no parting quip", last_note(self.client))

    def test_an_irritated_first_no_ends_the_call(self):
        s = self.make(*[reply("end_turn", text("Hm."))] * 2)
        s.respond("Why are you calling?")
        s.respond("No! Not interested.")
        self.assertIn("end_call", last_note(self.client))

    def test_the_ai_refusal_offers_noah(self):
        s = self.make(*[reply("end_turn", text("Hm."))] * 2)
        s.respond("I don't want to talk to an AI.")
        note = last_note(self.client)
        self.assertIn("Noah can call them himself", note)
        self.assertIn("never claim to be human", note)
        self.assertEqual(s._nos, 1)

    def test_no_meeting_ask_after_two_and_none_after_the_second_no(self):
        s = self.make(*[reply("end_turn", text("Fifteen minutes Tuesday, which is easier?"))] * 3)
        s.respond("Why are you calling?")
        s.respond("Hm.")
        s.respond("Go on.")
        self.assertIn("No more meeting asks", last_note(self.client))
        self.assertEqual(ol.MAX_NOS, 2)
        for gone in ("NOTE_LAST_TRY", "NOTE_NO_AGAIN_CALM"):
            self.assertFalse(hasattr(ol, gone))

    def test_a_no_never_gets_a_joke_or_a_tease(self):
        self.assertIn("No joke about the no, no teasing", ol.NOTE_NO_ONCE)


class TimingTests(_t.ScriptedFixture):
    def test_a_bad_time_is_not_a_no(self):
        s = self.make(*[reply("end_turn", text("Sure."))] * 2)
        s.respond("Why are you calling?")
        s.respond("Not right now, I'm on a job.")
        self.assertEqual(s._nos, 0)
        self.assertIn("bad time for them", last_note(self.client))
        self.assertIn("Callback requested", last_note(self.client))

    def test_a_flat_no_still_counts_and_picking_a_day_is_not_a_stop(self):
        self.assertFalse(ol.stop_request("don't call me today, try Thursday"))
        self.assertIsNone(ol.NO_SIGNAL.search("not right now"))
        self.assertTrue(ol.TIMING.search("not right now"))
        self.assertIsNone(ol.TIMING.search("we get ten calls a week"))
        s = self.make(*[reply("end_turn", text("Ok."))] * 2)
        s.respond("Why are you calling?")
        s.respond("no thanks, I'm busy")
        self.assertEqual(s._nos, 1)
        self.assertNotIn("bad time for them", last_note(self.client))


class HonestPitchTests(unittest.TestCase):
    def test_the_stable_prompt_claims_no_texting(self):
        stable = outbound.build_prompt({}, NOW, {"outbound": {}})[0]["text"]
        stable = stable.replace("text-to-speech", "")
        stable = re.sub(r"Texting and calendar sync are not live\.", "", stable)
        stable = re.sub(r'\*"Send me some info\."\*[^\n]*', "", stable)
        self.assertIsNone(re.search(r"\btext(s|ing|ed)?\b", stable, re.I), re.search(r".{40}\btext(s|ing|ed)?\b.{40}", stable, re.I | re.S))
        self.assertIn("puts every call on the owner's dashboard", stable)
        self.assertIn("Noah will call from this number", stable)

    def test_no_playful_teasing_of_a_no_remains_in_the_prompt(self):
        stable = outbound.build_prompt({}, NOW, {"outbound": {}})[0]["text"].lower()
        for w in ("teases the objection", "playful pushback", "a no is where", "the one you missed"):
            self.assertNotIn(w, stable)
