"""Phase B: nothing is speech unless the model put it inside <say>...</say>."""
import copy
import unittest
from types import SimpleNamespace

import tests  # noqa: F401
from receptionist import prompts
from receptionist.agent import NUDGE, NUDGE_SAY, CallSession, Sentences, SayChannel, say_text
from receptionist.speech import channel_guard, unspeakable
from tests.test_logic import BIZ, CFG, TUE_1015, FakeClient, FakeStream, fresh_store, reply, text, use

CHANNEL_CFG = {**CFG, "agent": {**CFG["agent"], "speech_channel": True}}


class PieceStream(FakeStream):
    """Streams a text block in exactly the pieces given (to split tags across deltas)."""
    def __iter__(self):
        for i, b in enumerate(self.msg.content):
            yield SimpleNamespace(type="content_block_start", index=i, content_block=b)
            if b.type == "text":
                for piece in getattr(b, "pieces", [b.text]):
                    yield SimpleNamespace(type="content_block_delta", index=i, delta=SimpleNamespace(type="text_delta", text=piece))
            yield SimpleNamespace(type="content_block_stop", index=i, content_block=b)


class PieceClient(FakeClient):
    def stream(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        return PieceStream(self.script.pop(0))


def pieces(*parts):
    return SimpleNamespace(type="text", text="".join(parts), pieces=list(parts))


def chan_session(*script):
    return CallSession(PieceClient(*script), CHANNEL_CFG, BIZ, fresh_store(), TUE_1015, caller="+13125550177")


def spoke(s, caller="hi"):
    out = []
    turn = s.respond(caller, out.append)
    return turn, [c.strip() for c in out if c.strip()]


class ChannelTests(unittest.TestCase):
    def test_a_tag_split_across_deltas_speaks_once(self):
        s = chan_session(reply("end_turn", pieces("<s", "ay>Hi.", " </sa", "y>")))
        turn, out = spoke(s)
        self.assertEqual(out, ["Hi."])
        self.assertEqual(turn.text, "Hi.")
        self.assertEqual(s.messages[-1]["content"][0]["text"], "<say>Hi.</say>")

    def test_private_note_is_not_spoken_and_not_kept_in_history(self):
        s = chan_session(reply("end_turn", text("They said no once. <say>Fair enough, is it the timing?</say>")))
        turn, out = spoke(s, "no thanks")
        self.assertEqual(" ".join(out), "Fair enough, is it the timing?")
        self.assertEqual(s.messages[-1]["content"][0]["text"], "<say>Fair enough, is it the timing?</say>")
        self.assertNotIn("They said no", str(s.messages))
        entry = s.transcript[-1]
        self.assertEqual(entry["private"], ["They said no once."])

    def test_sentences_the_old_blocklist_dropped_are_spoken_inside_say_with_a_shadow_entry(self):
        lines = ["The owner isn't picking up right now.", "You'll hear from them within thirty minutes."]
        for line in lines:
            self.assertTrue(unspeakable(line))
            self.assertFalse(channel_guard(line))
        s = chan_session(reply("end_turn", text("<say>" + " ".join(lines) + "</say>")))
        turn, out = spoke(s)
        self.assertEqual(" ".join(out), " ".join(lines))
        self.assertEqual(s.transcript[-1]["shadow"], lines)

    def test_a_block_with_no_tag_speaks_nothing_nudges_once_then_falls_back(self):
        s = chan_session(reply("end_turn", text("Sure, happy to help.")), reply("end_turn", text("Still no tags.")))
        turn, out = spoke(s)
        self.assertEqual(len(s.client.requests), 2)
        self.assertIn(NUDGE_SAY, str(s.client.requests[1]["messages"][-1]))
        self.assertTrue(NUDGE_SAY.endswith("Reply now inside <say>...</say>.)"))
        self.assertEqual(len(out), 1)
        self.assertIn(turn.text, __import__("receptionist.agent", fromlist=["x"]).REPEAT_VARIANTS)
        self.assertNotIn("happy to help", turn.text)

    def test_listening_with_no_tags_is_silent_and_not_nudged(self):
        s = chan_session(reply("end_turn", text("(listening)")))
        turn, out = spoke(s, "hold on babe")
        self.assertEqual((turn.text, out), ("", []))
        self.assertEqual(len(s.client.requests), 1)
        self.assertEqual(s.messages[-1]["content"], "(listening)")

    def test_text_after_the_closing_tag_is_private_and_a_tool_call_after_a_say_works(self):
        s = chan_session(reply("tool_use", text("<say>One moment.</say> and then check the slots"),
                               use("t1", "check_availability", {"service_id": "leak_repair"})),
                         reply("end_turn", text("<say>I have 2 PM today.</say> done")))
        turn, out = spoke(s, "can someone come today")
        self.assertEqual(" ".join(out), "One moment. I have 2 PM today.")
        self.assertEqual([t["name"] for t in turn.tools], ["check_availability"])
        for m in s.messages:
            self.assertNotIn("and then check", str(m))

    def test_an_unclosed_say_is_closed_by_the_end_of_the_block(self):
        s = chan_session(reply("end_turn", text("<say>Tuesday works.")))
        self.assertEqual(spoke(s)[1], ["Tuesday works."])

    def test_several_says_in_one_block(self):
        s = chan_session(reply("end_turn", text("<say>Got it.</say> note <say>What town?</say>")))
        self.assertEqual(" ".join(spoke(s)[1]), "Got it. What town?")

    def test_the_caller_note_inside_say_is_still_caught(self):
        self.assertTrue(channel_guard("The caller wants a quote."))
        self.assertTrue(channel_guard("Let me use check_availability."))
        self.assertTrue(channel_guard("The playbook says to ask the address."))
        self.assertFalse(channel_guard("Wait, did you say Sagle or Sandpoint?"))
        self.assertFalse(channel_guard("That's a better fit for spring."))

    def test_code_spoken_turns_are_saved_in_the_same_form(self):
        s = chan_session(reply("end_turn", text("<say>Ok.</say>")))
        self.assertEqual(s.tag("Hello."), "<say>Hello.</say>")
        off = CallSession(PieceClient(), CFG, BIZ, fresh_store(), TUE_1015)
        self.assertEqual(off.tag("Hello."), "Hello.")

    def test_the_channel_parser_directly(self):
        got = []
        w = Sentences(got.append)
        ch = SayChannel(w)
        for piece in ("note ", "<", "say", ">One. Two", "</", "say> tail"):
            ch.feed(piece)
        self.assertEqual(ch.close(), "One. Two")
        self.assertEqual(ch.privates, ["note  tail"])


class LegacyTests(unittest.TestCase):
    def test_speech_channel_false_keeps_todays_sentence_behaviour(self):
        for sentence, kept in (("Wait, I should ask for the address.", False), ("The caller wants a quote.", False),
                               ("Sure, what town are you in?", True)):
            got = []
            w = Sentences(got.append)
            w.add(sentence + " ")
            w.close()
            self.assertEqual(bool(got), kept, sentence)
            self.assertFalse(w.channel)
        legacy = CallSession(FakeClient(reply("end_turn", text("Sure, what town?"))), CFG, BIZ, fresh_store(), TUE_1015)
        turn = legacy.respond("hi")
        self.assertEqual(turn.text, "Sure, what town?")
        self.assertEqual(legacy.messages[-1]["content"][0].text, "Sure, what town?")
        self.assertIn(NUDGE, NUDGE)


class PromptTests(unittest.TestCase):
    def test_only_our_own_loop_gets_the_say_format(self):
        self.assertNotIn("<say>", prompts.stable_prompt(BIZ))
        self.assertIn("<say>", prompts.stable_prompt(BIZ, channel=True))
        self.assertTrue(prompts.stable_prompt(BIZ, True).rstrip().endswith("(listening) with no tags."))

    def test_session_warm_and_prewarm_build_the_identical_prefix(self):
        s = chan_session(reply("end_turn", text("<say>x</say>")))
        self.assertEqual(s.system[0]["text"], prompts.stable_prompt(BIZ, prompts.channel_on(CHANNEL_CFG)))
        self.assertTrue(prompts.channel_on(CHANNEL_CFG))
        self.assertFalse(prompts.channel_on({"agent": {}}))

    def test_config_json_ships_the_channel_on_after_the_retest_passed(self):
        import json
        from pathlib import Path
        self.assertIs(json.loads(Path("config.json").read_text())["agent"]["speech_channel"], True)

    def test_both_prompts_agree_on_how_to_stay_silent(self):
        from receptionist import outbound
        self.assertIn("write (listening) with no tags", prompts.stable_prompt(BIZ))
        self.assertIn("write (listening) with no tags", outbound.SPEAKING)

    def test_outbound_prefix_gets_the_format_only_when_the_channel_is_on(self):
        from receptionist import outbound
        from tests.test_outbound_live import NOW
        on = outbound.build_prompt({}, NOW, {"outbound": {}, "agent": {"speech_channel": True}})[0]["text"]
        off = outbound.build_prompt({}, NOW, {"outbound": {}})[0]["text"]
        self.assertIn("<say>", on)
        self.assertNotIn("<say>", off)
        self.assertEqual(on[:len(off)], off)


class SayTextTests(unittest.TestCase):
    def test_extracts_only_what_is_said(self):
        self.assertEqual(say_text("note <say>Hi there.</say> more <say>Bye</say>"), "Hi there. Bye")
        self.assertEqual(say_text("no tags at all"), "")
        self.assertEqual(say_text("<say>open ended"), "open ended")


class SummaryTests(unittest.TestCase):
    def test_the_report_line_handles_per_turn_lists(self):
        from receptionist.simulate import say_channel_summary
        ran = [{"began_with_say": [[True], [True, False]], "private": [[], ["note to self"]], "shadow": ["x"]},
               {"began_with_say": [[True]], "private": [[]], "shadow": []}]
        line = say_channel_summary(ran)
        self.assertIn("3/4 text blocks began with <say> (75%)", line)
        self.assertIn("1 private notes, longest 3 words", line)
        self.assertIn("1 shadow", line)
        self.assertEqual(say_channel_summary([{"first_words_s": []}]), "")


class FormatWordingTests(unittest.TestCase):
    def test_the_format_says_every_turn_including_after_a_tool_result_goes_inside_say(self):
        self.assertIn("the reply right after a tool result", prompts.SAY_FORMAT)
        self.assertIn("never heard", prompts.SAY_FORMAT)
        self.assertTrue(prompts.SAY_FORMAT.rstrip().endswith("(listening) with no tags."))
