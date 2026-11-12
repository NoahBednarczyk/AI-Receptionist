import json
import random
import re
import unittest
from pathlib import Path

import tests  # noqa: F401
from receptionist import prompts, schedule
from receptionist.config import load_business
from tests.test_logic import CFG

LAWN = load_business(CFG, "lawn_care")


class SizeAndWordingTests(unittest.TestCase):
    def setUp(self):
        self.prompt = prompts.stable_prompt(LAWN)

    def test_the_lawn_care_prompt_is_at_most_6500_words_and_never_appears_at_most_60_times(self):
        self.assertLessEqual(len(self.prompt.split()), 6550)
        self.assertLessEqual(len(re.findall(r"\bnever\b", self.prompt, re.I)), 60)

    def test_no_sarcasm_and_no_plumbing_jokes_in_a_lawn_prompt(self):
        low = self.prompt.lower()
        self.assertNotIn("sarcastic", low)
        self.assertNotIn("plumbing in general", low)

    def test_the_ai_disclosure_and_never_claim_human_are_unchanged(self):
        self.assertIn("Never claim or suggest that you're human.", self.prompt)
        self.assertIn("whenever a caller asks whether you're a person, a robot or an AI, say plainly that you're "
                      "Cedar Ridge Lawn and Snow's virtual assistant.", self.prompt)

    def test_the_character_is_one_block_at_the_top(self):
        rules = self.prompt.split("# How you speak")[0]
        self.assertIn("# Who you are", rules)
        self.assertEqual(self.prompt.count("# Who you are"), 1)
        for habit in ("You notice.", "You get curious when something doesn't add up.", "You're funny the dry way"):
            self.assertIn(habit, rules)
        self.assertIn("Aeration in January?", rules)

    def test_the_old_character_bullets_are_gone(self):
        for gone in ("semi-sarcastic", "Be human in the small ways", "Sound inviting.", "End most of your lines on a rising note",
                     "Never offer sympathy for the caller's problem", "that's a bummer", "Riley has real character"):
            self.assertNotIn(gone, self.prompt)

    def test_sympathy_is_for_real_harm_only_and_short(self):
        self.assertIn("When something bad has happened to the caller (someone hurt, real damage, a loss), say one plain human "
                      "line of six words or fewer, then get them help. A routine job gets no sympathy at all.", self.prompt)

    def test_pushback_is_the_curious_form(self):
        self.assertIn("ask one real question about what they're trying to get done", self.prompt)
        self.assertIn("ask one real question about what they're trying to get done (the frozen lawn", self.prompt)

    def test_the_narration_pairs_are_one_sentence_now(self):
        self.assertNotIn("Wrong:", self.prompt)
        self.assertIn("Don't narrate what you're doing or why", self.prompt)

    def test_the_protected_sections_are_intact(self):
        for kept in ("Power line down:", "Please stay clear of the tree", "Never recommend, name, mix or measure a product",
                     "Use transfer_call with target owner only", "# Honesty and limits", "# Call control", "# Tools"):
            self.assertIn(kept, self.prompt)

    def test_the_other_shops_still_build(self):
        for shop in ("acme", "hvac"):
            biz = load_business(CFG, shop)
            self.assertIn("# Who you are", prompts.stable_prompt(biz))


class CallStateTests(unittest.TestCase):
    def test_a_call_draws_one_quirk_at_most_and_no_style_or_acknowledgement_list(self):
        now = schedule.local_now(LAWN, "2026-10-20T10:15")
        for seed in range(30):
            state = prompts.call_state(LAWN, now, None, None, [], rng=random.Random(seed))
            self.assertLessEqual(state.count("Your character on this call:"), 1)
            self.assertNotIn("Style for this call", state)
            self.assertNotIn("Acknowledgement words", state)
        self.assertFalse(hasattr(prompts, "FLAVORS"))
        self.assertFalse(hasattr(prompts, "ACKS"))
        self.assertNotIn("flavors", LAWN)

    def test_the_greeting_still_varies(self):
        now = schedule.local_now(LAWN, "2026-10-20T10:15")
        self.assertGreater(len({prompts.pick_greeting(LAWN, now, random.Random(i)) for i in range(40)}), 5)


class ScenarioTests(unittest.TestCase):
    def test_the_out_of_season_pushback_scenario_exists(self):
        sc = {s["id"]: s for s in json.loads(Path("scenarios/lawn_care.json").read_text())}["out_of_season_pushback"]
        self.assertTrue(sc["at"].startswith("2027-01"))
        self.assertIn("core aeration", sc["persona"])
        self.assertEqual(sc["expect"], {"booked": False})
        self.assertEqual(len(sc["rubric"]), 3)
