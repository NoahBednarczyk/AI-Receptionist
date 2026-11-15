"""The barrier between the model's notes and the caller's ear: what the model writes before a tool call is a note to
itself, never speech. Only a line before transfer_call or end_call (which the rules ask for) is spoken. Found on the
live call of 2026-11-14 22:10, where "The record on file says Howard Street, but the caller said Olive Street. I'll
use what they gave me..." was read out before take_message."""
import unittest

import tests  # noqa: F401
from receptionist import tools
from tests.test_logic import reply, session, text, use

NOTE = ("The record on file says Howard Street, but the caller said Olive Street. I'll use what they gave me, and save "
        "the urgent message with the caller ID as the callback number.")
MESSAGE = {"caller_name": "Noah", "callback_number": "3125550177", "message": "Burst pipe, no water.", "urgent": True}
FINAL = "I've saved your message as urgent, and they'll call you back within 15 minutes."


def heard_of(s, said):
    heard = []
    turn = s.respond(said, on_text=heard.append)
    return turn, "".join(heard)


class PretoolBarrierTests(unittest.TestCase):
    def test_the_live_call_note_before_take_message_is_never_heard(self):
        s = session(reply("tool_use", text(NOTE), use("t1", "take_message", MESSAGE)), reply("end_turn", text(FINAL)))
        turn, heard = heard_of(s, "Noah at 906 Olive Street in Sandpoint.")
        for fragment in ("record on file", "the caller said", "I'll use what they gave me", "caller ID"):
            self.assertNotIn(fragment, heard)
            self.assertNotIn(fragment, turn.text)
        self.assertIn(FINAL, heard)
        self.assertIn(FINAL, turn.text)

    def test_the_stall_line_plays_in_place_of_the_note(self):
        s = session(reply("tool_use", text(NOTE), use("t1", "take_message", MESSAGE)), reply("end_turn", text(FINAL)))
        _, heard = heard_of(s, "Noah at 906 Olive Street in Sandpoint.")
        self.assertTrue(any(heard.startswith(line) for line in tools.FILLER_VARIANTS["en"]["take_message"]), heard)

    def test_the_note_is_kept_out_of_the_models_history_and_in_the_report(self):
        s = session(reply("tool_use", text(NOTE), use("t1", "take_message", MESSAGE)), reply("end_turn", text(FINAL)))
        heard_of(s, "Noah at 906 Olive Street in Sandpoint.")
        self.assertNotIn("record on file", repr(s.messages))
        self.assertIn("record on file", " ".join(s.transcript[-1]["dropped"]))
        self.assertTrue(any(c["name"] == "take_message" for c in s.transcript[-1]["tools"]))   # the tool still ran

    def test_a_filler_like_line_before_a_tool_is_replaced_not_doubled(self):
        s = session(reply("tool_use", text("Let me write that down."), use("t1", "take_message", MESSAGE)),
                    reply("end_turn", text(FINAL)))
        _, heard = heard_of(s, "Please have someone call me.")
        self.assertNotIn("Let me write that down.", heard)

    def test_a_goodbye_before_end_call_is_spoken(self):
        s = session(reply("tool_use", text("You're all set, bye."), use("t1", "end_call", {"reason": "completed"})))
        turn, heard = heard_of(s, "That's it, thanks.")
        self.assertEqual(heard, "You're all set, bye.")
        self.assertTrue(turn.ended)

    def test_a_line_before_transfer_call_is_spoken(self):
        s = session(reply("tool_use", text("Let me connect you now."), use("t1", "transfer_call", {"target": "office", "reason": "billing"})),
                    reply("end_turn", text("They're on the line.")))
        _, heard = heard_of(s, "Can I talk to the office about my bill?")
        self.assertTrue(heard.startswith("Let me connect you now."), heard)

    def test_a_plain_reply_is_spoken_whole_and_in_order(self):
        s = session(reply("end_turn", text("The diagnostic fee is $95. It's waived if you approve the repair.")))
        turn, heard = heard_of(s, "What's a service call cost?")
        self.assertEqual(heard, "The diagnostic fee is $95. It's waived if you approve the repair.")
        self.assertEqual(turn.text, heard)

    def test_a_note_between_two_tools_is_never_heard(self):
        s = session(reply("tool_use", use("t1", "take_message", MESSAGE)),
                    reply("tool_use", text("The message is saved, so now I'll try the owner's cell."),
                          use("t2", "take_message", {**MESSAGE, "message": "Second note."})),
                    reply("end_turn", text(FINAL)))
        _, heard = heard_of(s, "Noah at 906 Olive Street in Sandpoint.")
        self.assertNotIn("so now I'll try", heard)
        self.assertIn(FINAL, heard)

    def test_once_a_call_lines_in_a_dropped_note_are_not_counted_as_said(self):
        s = session(reply("tool_use", text("Is there anything else I can help with?"), use("t1", "take_message", MESSAGE)),
                    reply("end_turn", text("Saved. Is there anything else I can help with?")))
        _, heard = heard_of(s, "Please have someone call me.")
        self.assertIn("anything else", heard)   # the caller never heard the first one, so the second is not a repeat

    def test_the_outbound_caller_is_unchanged(self):
        from receptionist.agent import CallSession
        from receptionist.outbound_live import OutboundSession
        self.assertTrue(CallSession.notes_before_tools_are_private.fget(session()))
        self.assertFalse(OutboundSession.notes_before_tools_are_private.fget(object.__new__(OutboundSession)))


if __name__ == "__main__":
    unittest.main()
