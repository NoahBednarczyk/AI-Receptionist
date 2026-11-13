"""Part B/C: the caller shares the receptionist's character, adds its own guest-in-their-day mode, never names the kind of
call, holds replies to 30 words in code, and its stable prompt is shorter than it was."""
import re
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from receptionist import callreview, outbound, outbound_live as ol, prompts
from receptionist.agent import Sentences

NOW = datetime(2026, 11, 3, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
STARTING_WORDS = 2481   # the stable outbound prompt before this work (2026-11-12)
BANNED = re.compile(r"\b(sales call|cold call|pitch|selling)\b", re.I)


def stable():
    return outbound.build_prompt({}, NOW, {})[0]["text"]


class SharedCharacterTests(unittest.TestCase):
    def test_the_two_voices_import_the_same_bullets(self):
        text = stable()
        self.assertIn(prompts.CHARACTER_NOTICE.format(reactions=outbound.OUTBOUND_REACTIONS).strip(), text)
        self.assertIn(prompts.CHARACTER_DRY.format(easy=outbound.OUTBOUND_EASY).strip(), text)
        self.assertIn(prompts.CHARACTER_EXAMPLES.strip(), text)
        self.assertIn("dry way", text)
        self.assertIn("No joke in place of an answer", text)       # the "say the thing a script wouldn't" sentence was trimmed 2026-11-13

    def test_the_outbound_mode_is_a_guest_in_their_day(self):
        text = stable()
        for needle in ("You are a guest in their day", "you ask rather than tell", "leave the moment you are not wanted",
                       "only at your own expense or about the situation", "never about their business, a missed call, a lost job or a no",
                       "fresh phrasing, never a surprise claim"):
            self.assertIn(needle, text)

    def test_three_behaviours_not_a_technique_list(self):
        text = stable()
        self.assertIn("Three things you do", text)
        for gone in ("SPIN", "mirroring", "labeling", "Need-payoff", "Turn a no into a yes", "calibrated questions", "loss framing", "reciprocity",
                     "alternative close", "that's a real cost", "let's get that sorted", "warm, attentive, encouraging", "Repeat their last two or three words"):
            self.assertNotIn(gone.lower(), text.lower())

    def test_rising_tone_stays_removed(self):
        self.assertNotRegex(stable().lower(), r"lift the end|rising|upward")


class NeverNamesTheCallTests(unittest.TestCase):
    def test_no_such_word_in_the_prompt_the_notes_or_the_code_spoken_lines(self):
        s = outbound.settings({})
        texts = [stable(), outbound.build_prompt({"type": "Plumbing"}, NOW, {})[1]["text"], ol.INTRO_NOTE, ol.INTRO_NOTE_AI.format(**s), ol.AI_NOTE,
                 ol.NOTE_TIMING, callreview.tone_note("we missed a call"), " ".join(ol.ANSWER.values()), " ".join(r["ask"] for r in ol.RUNGS),
                 " ".join(ol.STOP_LINES + ol.GREETINGS + ol.INTRO + ol.INTRO_AI), " ".join(sum(ol.FIRST_QUESTIONS.values(), []))]
        for kind in ol.KINDS + ("unclear",):
            texts.append(ol.refusal_note(kind, 0, ("a",), False))
            texts.append(ol.refusal_note(kind, 0, ("a",), True))
        for t in texts:
            self.assertIsNone(BANNED.search(t), BANNED.search(t) and t[max(0, BANNED.search(t).start() - 40):BANNED.search(t).end() + 20])

    def test_the_review_flags_the_words_even_when_the_owner_said_them_first(self):
        for said in ("It's a sales call.", "Just a cold call, sorry.", "I'm not here to pitch you.", "I'm selling nothing, honest."):
            found = callreview.lint(f"Caller: Is this a sales call?\nAgent: {said}")
            self.assertTrue(any("named the kind of call" in f for f in found), said)
        self.assertEqual([f for f in callreview.lint("Caller: What is this?\nAgent: Noah wants to hear how your calls work.") if "kind of call" in f], [])


class LengthTests(unittest.TestCase):
    def test_the_outbound_session_caps_a_reply_at_30_words_in_code(self):
        cfg = ol.session_cfg({"models": {"agent": "a", "agent_effort": "low"}, "agent": {"reply_word_budget": 40}, "outbound": {}})
        self.assertEqual(cfg["agent"]["reply_word_budget"], 30)
        said = []
        w = Sentences(said.append, cfg["agent"]["reply_word_budget"])
        for sentence in ("Fifteen minutes with Noah, ten words in all, which is easier for you?", "He shows exactly what it would catch on your own missed calls.",
                         "He can do Tuesday morning or Thursday afternoon, whichever is better.", "And one more thing about the free pilot."):
            w.add(sentence + " ")
        w.close()
        self.assertLessEqual(sum(len(x.split()) for x in said), 30)
        self.assertTrue(w.held)   # what was held back goes to the model with the next turn, as on the inbound side

    def test_the_inbound_cap_is_untouched(self):
        import json
        self.assertEqual(json.load(open("config.json"))["agent"]["reply_word_budget"], 40)


class ShorterPromptTests(unittest.TestCase):
    def test_the_stable_prompt_ends_shorter_than_it_started(self):
        words = len(stable().split())
        self.assertLess(words, STARTING_WORDS)
        self.assertLess(words, 1800)


if __name__ == "__main__":
    unittest.main()
