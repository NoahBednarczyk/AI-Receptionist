"""The caller says what is true: its trade words match the lead, the demo line is whatever shop holds the number,
what it claims the product does comes from one verified list, and no example time is hard-coded."""
import json
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from receptionist import outbound, outbound_live as ol

NOW = datetime(2026, 11, 3, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
LEADS = {"hvac": "Heating and cooling", "plumbing": "Plumbing", "electrical": "Electrical", "lawn": "Lawn care", "snow": "Snow removal",
         "default": "Other"}


def lead(trade):
    return {"name": "Case & Sons", "phone": "208-304-3883", "type": LEADS[trade], "contact": "Dale Smith"}


class TradeOfTests(unittest.TestCase):
    def test_each_type_maps_to_its_trade(self):
        for t, kind in (("HVAC", "hvac"), ("Heating & Plumbing", "hvac"), ("Furnace repair", "hvac"), ("Plumber", "plumbing"),
                        ("Electrician", "electrical"), ("Lawn care", "lawn"), ("Landscaping", "lawn"), ("Lawn & Snow", "lawn"),
                        ("Snow & Lawn", "lawn"), ("Snow removal", "snow"), ("Plowing", "snow"), ("Other", "default"), ("", "default")):
            self.assertEqual(outbound.trade_of({"type": t}), kind, t)
        self.assertEqual(outbound.trade_of({}), "default")
        self.assertIs(ol.trade_of, outbound.trade_of)


class OnlyItsOwnTradeTests(unittest.TestCase):
    def heard(self, trade):
        """Everything trade-specific a lead of this trade can hear: the first questions and its per-call lines."""
        s = outbound.settings({})
        return "\n".join(ol.FIRST_QUESTIONS[trade] + outbound.trade_lines(lead(trade), s))

    def test_a_lead_of_each_trade_hears_only_its_own_trade_words(self):
        for trade in ("hvac", "plumbing", "electrical", "lawn", "snow"):
            text = self.heard(trade)
            self.assertRegex(text.lower(), outbound.TRADE_WORDS[trade], trade)
            for other, words in outbound.TRADE_WORDS.items():
                if other != trade:
                    self.assertIsNone(re.search(words, text.lower()), (trade, other, re.search(words, text.lower())))

    def test_the_per_call_block_carries_the_trade_and_the_cached_prompt_names_none(self):
        for trade in LEADS:
            blocks = outbound.build_prompt(lead(trade), NOW, {})
            self.assertIn(outbound.TRADES[trade]["plural"], blocks[1]["text"])
        stable = outbound.build_prompt({}, NOW, {})[0]["text"]
        stable = stable.replace(outbound.demo_description(outbound.settings({})), "")   # the demo line names the shop that holds it
        for trade, words in outbound.TRADE_WORDS.items():
            self.assertIsNone(re.search(words, stable.lower()), (trade, re.search(words, stable.lower())))

    def test_each_trade_has_three_first_questions_and_the_priority_questions(self):
        for trade, t in outbound.TRADES.items():
            self.assertEqual(len(t["first"]), 3, trade)
            self.assertTrue(t["worth"].endswith("?") and t["who"].endswith("?"), trade)
        text = "\n".join(b["text"] for b in outbound.build_prompt(lead("plumbing"), NOW, {}))
        self.assertIn("drain or water heater job", text)
        self.assertNotIn("Discovery script", text)
        self.assertNotRegex(text, r"\bQ[1-9]\b")


class DemoLineTests(unittest.TestCase):
    def shops_dir(self, name, number):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        (Path(d.name) / "x.json").write_text(json.dumps({"id": "x", "name": f"{name} (demo)", "spoken_name": name, "phone_numbers": [number]}))
        return d.name

    def test_the_description_comes_from_the_shop_that_holds_the_number(self):
        s = outbound.settings({})
        for name in ("Peak Heating and Plumbing", "Cedar Ridge Lawn and Snow"):
            cfg = {"businesses_dir": self.shops_dir(name, "+12084286760")}
            self.assertIn(f"answers as {name}", outbound.demo_description(s, cfg))
            self.assertIn(f"answers as {name}", outbound.facts(s, cfg))
            self.assertIn(f"answers as {name}", outbound.build_prompt({}, NOW, cfg)[0]["text"])

    def test_a_number_no_shop_holds_is_just_a_demo_shop(self):
        cfg = {"businesses_dir": self.shops_dir("Peak Heating and Plumbing", "+13125550999")}
        self.assertEqual(outbound.demo_description(outbound.settings({}), cfg), "a demo shop")

    def test_the_real_repo_demo_line_is_the_trades_shop(self):
        self.assertIn("Peak Heating and Plumbing", outbound.demo_description(outbound.settings({})))
        self.assertNotRegex(outbound.PLAYBOOK.read_text(), r"(?i)lawn care and snow shop")


class CapabilityTests(unittest.TestCase):
    def shops(self, **flags):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        (Path(d.name) / "x.json").write_text(json.dumps({"id": "x", "name": "X", "phone_numbers": ["+13125550999"], **flags}))
        return {"businesses_dir": d.name}

    def test_facts_claim_only_verified_things(self):
        facts = outbound.facts(outbound.settings({}))
        for claim in outbound.VERIFIED:
            self.assertIn(claim, facts)
        for unverified in ("weekly report", "daily digest", "text message", "texts you", "calendar"):
            self.assertNotIn(unverified, facts.lower().replace("calendar sync is not live", "").replace("texting and calendar sync are not live", ""))

    def test_texting_is_called_not_live_while_every_shop_has_it_off(self):
        cfg = self.shops(texting_enabled=False, calendars=[])
        self.assertEqual(outbound.not_live(cfg), "Texting and calendar sync are not live.")
        self.assertIn("Texting and calendar sync are not live.", outbound.facts(outbound.settings({}), cfg))
        self.assertIn("Texting and calendar sync are not live.", outbound.build_prompt({}, NOW, cfg)[0]["text"])

    def test_the_sentence_follows_the_shop_files(self):
        self.assertEqual(outbound.not_live(self.shops(texting_enabled=True, calendars=[])), "Calendar sync is not live.".replace("Calendar", "calendar sync is not live.").replace("calendar sync is not live. sync is not live.", "calendar sync is not live."))

    def test_no_text_claim_anywhere_in_the_prompt_while_texting_is_off(self):
        cfg = self.shops(texting_enabled=False, calendars=[])
        stable = outbound.build_prompt({}, NOW, cfg)[0]["text"]
        stable = stable.replace("text-to-speech", "").replace("Texting and calendar sync are not live.", "")
        stable = re.sub(r'\*"Send me some info\."\*[^\n]*', "", stable)
        self.assertIsNone(re.search(r"\btext(s|ing|ed)?\b", stable, re.I))


class NoHardCodedTimesTests(unittest.TestCase):
    def test_no_example_time_in_the_playbook_or_the_notes(self):
        text = outbound.PLAYBOOK.read_text() + outbound.SPEAKING
        self.assertIsNone(re.search(r"\b\d{1,2}:\d{2}\b", text), re.search(r".{30}\b\d{1,2}:\d{2}\b.{20}", text))
        self.assertIn("from the open list", text)
        for const in (ol.NOTE_TIMING, ol.INTRO_NOTE, ol.INTRO_NOTE_AI):
            self.assertIsNone(re.search(r"\b\d{1,2}:\d{2}\b", const))


if __name__ == "__main__":
    unittest.main()
