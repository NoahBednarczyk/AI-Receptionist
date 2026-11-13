"""Part E: a no is never the end. Code classifies each refusal, hands the model the answer and the next smaller ask, and
never ends a call on a no. Only a stop request, or a no to the smallest ask, ends it."""
import json
import re
import unittest

from receptionist import outbound, outbound_live as ol
from tests import test_outbound_live as _t
from tests.test_outbound_live import NOW
from tests.test_logic import reply, text


def note(client):
    return json.dumps(client.requests[-1]["messages"][-1])


class ClassifyTests(unittest.TestCase):
    SIX = {
        "bad_time": ["Not right now, I'm on a job.", "Call me back later", "no thanks, I'm busy"],
        "no_need": ["We answer our phones, we're good.", "No thanks, my wife answers the phone", "Not interested, no need"],
        "no_trust_ai": ["I don't want to talk to an AI.", "I don't trust that with my customers", "My customers want a real person"],
        "no_money": ["Can't afford it.", "That's too expensive for us", "We have no budget for that"],
        "tried_before": ["No thanks, we tried one of those, didn't work.", "No, already tried something like it before, waste of money"],
        "dislikes_calls": ["I hate cold calls", "I don't like being called at work", "How did you get my number?"],
    }

    def test_each_refusal_gets_its_kind(self):
        for kind, lines in self.SIX.items():
            for line in lines:
                self.assertEqual(ol.refusal_kind(line), kind, line)

    def test_a_bare_no_is_unclear_and_talk_that_is_not_a_no_is_none(self):
        for line in ("No thanks.", "Not interested.", "no", "We're good."):
            self.assertEqual(ol.refusal_kind(line), "unclear", line)
        for line in ("My wife answers the phone.", "We tried one once, it was ok", "We get ten calls a week", "What does it cost?",
                     "Who is this?", "Tell me more", "I need to pass it to my wife"):
            self.assertIsNone(ol.refusal_kind(line), line)

    def test_the_six_kinds_all_have_an_answer_and_an_entry_point(self):
        self.assertEqual(set(ol.KINDS) | {"unclear"}, set(ol.ANSWER))
        self.assertEqual(set(ol.KINDS) | {"unclear"}, set(ol.FLOOR))


class LadderTests(unittest.TestCase):
    def test_the_ladder_is_six_rungs_in_the_agreed_order(self):
        self.assertEqual([r["key"] for r in ol.RUNGS], list("abcdef"))
        asks = " | ".join(r["ask"] for r in ol.RUNGS)
        for word in ("15-minute", "ten minutes", "demo line", "14-day pilot", "text or email", "check"):
            self.assertIn(word.split()[0], asks)

    def test_the_rung_an_agent_reply_asks_for_is_read_from_its_words(self):
        for said, rung in (("Fifteen minutes with Noah, Tuesday or Thursday?", 0), ("Ten minutes then?", 1),
                           ("Try our demo line yourself, any time.", 2), ("It's free for fourteen days on missed calls.", 3),
                           ("Can I text you the number?", 4), ("Mind if I check back after the season?", 5), ("Got it, thanks.", -1)):
            self.assertEqual(ol.rung_offered(said), rung, said)

    def test_each_no_moves_down_one_rung_never_back_up(self):
        self.assertEqual(ol.next_rung(-1, "unclear", False), 0)
        self.assertEqual(ol.next_rung(0, "no_need", False), 1)
        self.assertEqual(ol.next_rung(1, "no_need", False), 2)
        self.assertEqual(ol.next_rung(2, "no_need", False), 3)
        self.assertEqual(ol.next_rung(5, "no_need", False), 5)
        for offered in range(-1, 6):
            self.assertGreaterEqual(ol.next_rung(offered, "no_money", False), min(offered + 1, 5))

    def test_the_kind_of_no_sets_where_it_enters(self):
        self.assertEqual(ol.next_rung(-1, "bad_time", False), 1)
        self.assertEqual(ol.next_rung(0, "no_money", False), 3)
        self.assertEqual(ol.next_rung(0, "dislikes_calls", False), 4)

    def test_irritation_goes_straight_to_the_two_smallest_rungs(self):
        for offered in (-1, 0, 1, 2):
            self.assertIn(ol.next_rung(offered, "no_need", True), (4, 5))

    def test_the_note_names_the_objection_the_next_ask_and_what_was_already_asked(self):
        n = ol.refusal_note("no_money", 0, ("a",), False)
        self.assertIn("no money", n)
        self.assertIn("free for fourteen days", n)
        self.assertIn(ol.RUNGS[3]["ask"], n)
        self.assertIn("under 20 words", n)
        self.assertIn("Already asked for: a", n)
        self.assertNotIn(ol.RUNGS[0]["ask"], n)

    def test_irritation_changes_the_tone_not_the_goal(self):
        n = ol.refusal_note("no_need", 0, ("a",), True)
        self.assertIn("no humor", n)
        self.assertIn(ol.RUNGS[4]["ask"], n)
        self.assertRegex(n, r"nothing untrue")

    def test_every_answer_is_short_and_makes_no_invented_claim(self):
        for kind, answer in ol.ANSWER.items():
            self.assertLess(len(answer.split()), 55, kind)
            self.assertNotRegex(answer, r"\$|\d+ ?%|other shops|guarantee", kind)


class Calls(_t.ScriptedFixture):
    def talk(self, s, *owner):
        for line in owner:
            s.respond(line)

    def test_no_number_of_refusals_ends_the_call_in_code(self):
        s = self.make(*[reply("end_turn", text("Fifteen minutes with Noah, Tuesday or Thursday?"))] * 6)
        s.respond("Why are you calling?")
        for line in ("No thanks.", "Not interested.", "We're good.", "I said no!", "No."):
            s.respond(line)
            self.assertFalse(s.ctx.ended, line)
            self.assertNotIn("end_call", note(self.client).split("they said no")[-1][:400] if "they said no" in note(self.client) else "")
        self.assertEqual(s._nos, 5)

    def test_a_calm_no_after_each_ask_gets_the_next_rung_down(self):
        s = self.make(reply("end_turn", text("Fifteen minutes with Noah, Tuesday or Thursday?")), reply("end_turn", text("Fair. Ten minutes, then?")),
                      reply("end_turn", text("Try the demo line yourself.")), reply("end_turn", text("Free for fourteen days.")))
        s.respond("Tell me more")
        self.assertEqual(s._rung, 0)
        s.respond("We answer our phones, we're good.")
        self.assertIn(ol.RUNGS[1]["ask"], note(self.client))
        self.assertEqual(s._rung, 1)
        s.respond("No thanks, we're covered.")
        self.assertIn(ol.RUNGS[2]["ask"], note(self.client))
        self.assertEqual(s._rung, 2)
        s.respond("No thanks, we're covered, really.")
        self.assertIn(ol.RUNGS[3]["ask"], note(self.client))
        self.assertEqual(s._rungs, (0, 1, 2, 3))

    def test_a_bare_no_gets_one_plain_question_about_the_reason_then_it_moves_on(self):
        s = self.make(*[reply("end_turn", text("What's the no about?"))] * 3)
        s.respond("Tell me more")
        s.respond("No thanks.")
        self.assertIn("what the no is about", note(self.client))
        s.respond("No.")
        self.assertIn("don't ask why again", note(self.client))
        self.assertIn(ol.RUNGS[0]["ask"], note(self.client))

    def test_a_no_to_the_last_rung_is_a_stop_request_and_ends_it_at_once(self):
        s = self.make(reply("end_turn", text("Mind if I check back after the season?")))
        s.respond("Tell me more")
        self.assertEqual(s._rung, 5)
        said = []
        turn = s.respond("No, don't.", said.append)
        self.assertTrue(turn.ended and s.ctx.ended)
        self.assertIn(said[0], ol.STOP_LINES)
        self.assertEqual(len(self.client.requests), 1)   # no model call for the stop
        self.assertEqual([c["name"] for c in turn.tools], ["mark_do_not_call", "log_call", "end_call"])
        self.assertIn("last offer", turn.tools[0]["args"]["reason"])

    def test_an_explicit_stop_request_still_ends_the_call_at_once(self):
        s = self.make()
        said = []
        turn = s.respond("Stop calling me.", said.append)
        self.assertTrue(turn.ended)
        self.assertEqual(self.client.requests, [])

    def test_a_bad_time_does_not_walk_the_ladder_or_count_as_a_no(self):
        s = self.make(reply("end_turn", text("When works?")))
        s.respond("Not right now, I'm on a job.")
        self.assertEqual((s._nos, s._rung, s._refusals), (0, -1, ("bad_time",)))
        self.assertIn("Noah calls then", note(self.client))

    def test_the_counters_are_taken_back_with_a_dropped_turn(self):
        from receptionist.agent import TurnControl, TurnDropped
        s = self.make(*[reply("end_turn", text("Ten minutes?"))] * 2)
        s.respond("Tell me more")
        control = TurnControl()
        control.drop()
        with self.assertRaises(TurnDropped):
            s.respond("No thanks, we're good.", None, control)
        self.assertEqual((s._nos, s._refusals, s._why), (0, (), False))

    def test_the_call_row_logs_refusals_rungs_and_the_rung_each_yes_came_from(self):
        from datetime import timedelta
        s = self.make(reply("end_turn", text("Fifteen minutes with Noah, Tuesday or Thursday?")), reply("end_turn", text("Ten minutes, then?")))
        s.respond("Tell me more")
        s.respond("No thanks, we're covered.")
        log = {"outcome": "Callback requested", "summary": "Said yes to ten minutes.", "yes_rung": "b"}
        s.ctx.extra["log"] = log
        row = s.finish(NOW + timedelta(minutes=2), 2.0)
        self.assertEqual((row["refusals"], row["rungs"], row["yesRung"]), (["no_need"], ["a", "b"], "b"))
        self.assertIn("yes_rung", [t for t in outbound.schemas() if t["name"] == "log_call"][0]["input_schema"]["properties"])

    def test_the_removed_two_no_rule_is_gone(self):
        for gone in ("MAX_NOS", "NOTE_NO_AGAIN", "NOTE_NO_MORE_ASKS", "NOTE_NO_ONCE", "MEETING_ASK", "_no_directive"):
            self.assertFalse(hasattr(ol, gone), gone)
            self.assertFalse(hasattr(ol.OutboundSession, gone), gone)
        src = open(ol.__file__).read()
        self.assertNotIn("MAX_NOS", src)


if __name__ == "__main__":
    unittest.main()
