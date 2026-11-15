"""Steps of a trades-shop call that code runs itself (businesses/hvac.json "call_flow"), the way the outbound caller's
opener and intro are code: the read-back before a booking and the confirmation after it, what the caller hears when the
owner's cell rings out, an emergency's first question, and the goodbye. Asked for on 2026-11-15 after a live call where
the address was said three times and the number twice."""
import unittest

import tests  # noqa: F401
from receptionist import schedule
from receptionist.agent import CallSession, closing
from receptionist.store import Store
from receptionist.tools import ToolContext, run_tool
from tests.test_hvac import CFG, HVAC
from tests.test_logic import FakeClient, reply, text, use

TUE_1015 = schedule.local_now(HVAC, "2026-11-10T10:15")
CALLER = "+12085550177"


def session(*script, caller=CALLER, now=TUE_1015, **kw):
    store = Store(":memory:")
    store.seed_demo(HVAC, now)
    return CallSession(FakeClient(*script), CFG, HVAC, store, now, caller=caller, **kw)


def window(s):
    return schedule.open_windows(HVAC, s.ctx.now, {}, limit=1)[0]["window_id"]


def booking(s, **over):
    return {"window_id": window(s), "service_id": "furnace_repair", "customer_name": "Noah", "callback_number": CALLER,
            "street_address": "906 Alder Street", "town": "Sandpoint", "problem_summary": "Furnace short-cycling.",
            "access_notes": "", **over}


def heard(s, said):
    out = []
    turn = s.respond(said, on_text=out.append)
    return turn, "".join(out)


class ReadBackTests(unittest.TestCase):
    def test_the_first_booking_call_reads_back_and_books_nothing(self):
        s = session()
        s.client.script = [reply("tool_use", use("t1", "book_appointment", booking(s)))]
        turn, out = heard(s, "The nine to eleven, please.")
        self.assertTrue(out.startswith("That's "), out)
        self.assertIn("906 Alder Street in Sandpoint", out)
        self.assertTrue(out.endswith("Shall I book it?"))
        self.assertNotIn("208-555-0177", out)            # the caller ID was already confirmed: not said again
        self.assertNotIn("booking it", out.lower())      # no "booking it" stall line before a read-back
        self.assertEqual(s.store.appointments_for_call(s.call_id), [])
        self.assertEqual(len(s.client.requests), 1)      # code spoke; no second model round

    def test_yes_books_and_code_says_the_confirmation(self):
        s = session()
        s.client.script = [reply("tool_use", use("t1", "book_appointment", booking(s))),
                           reply("tool_use", use("t2", "book_appointment", booking(s)))]
        heard(s, "The nine to eleven, please.")
        turn, out = heard(s, "Yes.")
        self.assertIn("You're booked for", out)
        self.assertIn("calls or texts about 30 minutes before arriving", out)
        self.assertTrue(out.endswith("Anything else I can help with?"))
        self.assertEqual(len(s.store.appointments_for_call(s.call_id)), 1)

    def test_a_number_other_than_the_caller_id_is_read_back(self):
        s = session()
        s.client.script = [reply("tool_use", use("t1", "book_appointment", booking(s, callback_number="2085550199")))]
        _, out = heard(s, "Use my wife's cell.")
        self.assertIn("208-555-0199 as your callback number", out)

    def test_a_changed_detail_is_read_back_again(self):
        s = session()
        s.client.script = [reply("tool_use", use("t1", "book_appointment", booking(s))),
                           reply("tool_use", use("t2", "book_appointment", booking(s, street_address="906 Howard Street")))]
        heard(s, "The nine to eleven.")
        _, out = heard(s, "No, it's Howard Street.")
        self.assertIn("906 Howard Street", out)
        self.assertTrue(out.endswith("Shall I book it?"))
        self.assertEqual(s.store.appointments_for_call(s.call_id), [])

    def test_shops_without_the_flow_book_at_once(self):
        store = Store(":memory:")
        store.seed_demo(HVAC, TUE_1015)
        c = ToolContext(HVAC, store, TUE_1015, CALLER, "test-call")   # no call_flow: tests and other shops
        result, err = run_tool(c, "book_appointment", {**booking(session())})
        self.assertFalse(err)
        self.assertEqual(result["status"], "booked")


class OwnerNoAnswerTests(unittest.TestCase):
    def test_code_says_it_and_the_caller_gets_to_answer(self):
        s = session(transfer_outcome="no_answer")
        msg = {"caller_name": "Noah", "callback_number": CALLER, "message": "Burst pipe.", "urgent": True}
        s.client.script = [reply("tool_use", use("t1", "take_message", msg)),
                           reply("tool_use", use("t2", "transfer_call", {"target": "owner", "reason": "Burst pipe"}))]
        turn, out = heard(s, "Noah, 906 Alder Street in Sandpoint.")
        self.assertIn("The owner isn't picking up, but your message is saved and they'll call you back within 15 "
                      "minutes.", out)
        self.assertTrue(out.endswith("Is there anything else they should know?"))
        self.assertFalse(turn.ended)                     # no hang-up in the same breath
        self.assertEqual(len(s.client.requests), 2)


class EmergencyFirstTurnTests(unittest.TestCase):
    def test_safety_line_then_the_one_question_with_no_model_call(self):
        s = session()
        turn, out = heard(s, "I've got a burst pipe and water everywhere.")
        self.assertTrue(out.startswith("If it's safe, shut off the main water valve"), out)
        self.assertTrue(out.endswith("Can I get your name and the address?"))
        self.assertEqual(s.client.requests, [])

    def test_the_next_turn_is_told_what_comes_next(self):
        s = session()
        heard(s, "I've got a burst pipe and water everywhere.")
        s.client.script = [reply("end_turn", text("Thanks, Noah."))]
        heard(s, "Noah, 906 Alder Street in Sandpoint.")
        sent = s.client.requests[0]["messages"][-1]["content"]
        self.assertIn("take an urgent message", sent)

    def test_an_address_already_given_goes_to_the_model(self):
        s = session()
        s.client.script = [reply("end_turn", text("Thanks, Noah."))]
        heard(s, "Burst pipe at 906 Alder Street, it's Noah.")
        self.assertEqual(len(s.client.requests), 1)

    def test_no_heat_is_not_this_step(self):
        s = session(now=schedule.local_now(HVAC, "2026-12-15T21:30"))
        s.client.script = [reply("end_turn", text("What's going on with it?"))]
        heard(s, "My furnace is out.")
        self.assertEqual(len(s.client.requests), 1)


class GoodbyeTests(unittest.TestCase):
    def test_closing_words(self):
        for said in ("No, that's it.", "Nope, thanks.", "No thank you, bye.", "I'm good, thanks.", "That's all."):
            self.assertTrue(closing(said), said)
        for said in ("No, but can you tell me the fee?", "Yeah.", "Yes, one more thing.", "Thanks."):
            self.assertFalse(closing(said), said)

    def test_after_anything_else_code_says_goodbye_and_ends(self):
        s = session()
        s.client.script = [reply("end_turn", text("The diagnostic fee is $95. Anything else I can help with?"))]
        heard(s, "What's the diagnostic fee?")
        turn, out = heard(s, "No, that's it, thanks.")
        self.assertTrue(turn.ended)
        self.assertTrue(out)
        self.assertEqual(len(s.client.requests), 1)

    def test_no_after_another_question_goes_to_the_model(self):
        s = session()
        s.client.script = [reply("end_turn", text("Do you want a visit?")), reply("end_turn", text("Alright."))]
        heard(s, "My faucet drips.")
        turn, _ = heard(s, "No.")
        self.assertFalse(turn.ended)
        self.assertEqual(len(s.client.requests), 2)


if __name__ == "__main__":
    unittest.main()


class HelpFirstTests(unittest.TestCase):
    """2026-11-15 live call: "No hot water is a rotten way to start a Sunday, Noah." The caller wanted help fixing it."""

    def test_problem_calls_skip_the_reaction_and_the_trades_shop_offers_the_tip(self):
        from receptionist import prompts, simulate
        from receptionist.config import load_business
        hvac, acme = prompts.stable_prompt(HVAC), prompts.stable_prompt(load_business(CFG, "acme"))
        for p in (hvac, acme):
            self.assertIn("When something is broken, skip the reaction and any sympathy", p)
            self.assertNotIn("in a few words of your own, before anything else", p)
        self.assertIn("give it in your first reply, unasked", hvac)
        self.assertIn("describing the problem is not asking", acme)        # other shops still wait to be asked
        self.assertIn("except one approved tip", simulate.standard_rubric(HVAC)[0])
        self.assertNotIn("except one approved tip", simulate.standard_rubric(load_business(CFG, "acme"))[0])
