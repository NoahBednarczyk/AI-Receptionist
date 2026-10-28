"""The fictional lawn care and snow demo shop: it loads, its urgent lines and seasons work, and the live HVAC line is untouched."""
import datetime
import json
import unittest
from pathlib import Path

from receptionist import onboarding, prompts, safety, schedule
from receptionist.config import all_businesses, load_business, load_config

CFG = load_config()
LAWN = load_business(CFG, "lawn_care")
SCENARIOS = json.loads((Path(CFG["scenarios_dir"]) / "lawn_care.json").read_text())


class Shop(unittest.TestCase):
    def test_it_loads_like_any_shop_and_is_marked_demo(self):
        onboarding.check(LAWN)
        self.assertIn("(demo)", LAWN["name"])
        self.assertTrue(LAWN["demo"])
        self.assertEqual(LAWN["mode"], "overflow")
        self.assertEqual(LAWN["phone_numbers"], ["+12084286760"])     # the live number runs this demo for now
        self.assertNotEqual(LAWN["agent_name"], "Sam")

    def test_the_number_moved_from_hvac_without_clashing(self):
        hvac = load_business(CFG, "hvac")
        self.assertEqual(hvac["phone_numbers"], [])          # swap back: move the number here and retell-tune
        self.assertEqual(hvac["agent_name"], "Sam")
        all_businesses(CFG)                                  # no duplicate id or number across shops

    def test_services_cover_the_seasonal_calendar(self):
        ids = {s["id"] for s in LAWN["services"]}
        for needed in ("lawn_mowing", "spring_cleanup", "aeration_overseeding", "fertilizer_weed_control", "irrigation_startup",
                       "irrigation_winterization", "fall_leaf_cleanup", "snow_removal_seasonal", "estimate_visit", "urgent_visit"):
            self.assertIn(needed, ids)
        self.assertEqual([s["id"] for s in LAWN["services"] if s.get("emergency")], ["urgent_visit"])

    def test_winter_hours_replace_summer_hours_only_in_season(self):
        self.assertEqual(schedule.hours_for(LAWN, datetime.date(2026, 6, 16)), ("07:00", "17:00"))
        self.assertEqual(schedule.hours_for(LAWN, datetime.date(2026, 12, 15)), ("08:00", "16:30"))
        self.assertEqual(schedule.hours_for(LAWN, datetime.date(2027, 3, 31)), ("08:00", "16:30"))      # wraps the new year
        self.assertEqual(schedule.hours_for(LAWN, datetime.date(2027, 4, 1)), ("07:00", "17:00"))
        self.assertEqual(schedule.hours_for(LAWN, datetime.date(2026, 11, 13)), ("07:00", "17:00"))
        self.assertIn("November 15 through March 31", schedule.spoken_hours(LAWN))
        # A window that no longer fits the shorter winter day is not offered.
        winter = [w["start"] for w in schedule.open_windows(LAWN, schedule.local_now(LAWN, "2026-12-14T05:00"), {}, limit=100)
                  if w["date"] == "2026-12-15"]
        self.assertEqual(winter, ["09:00", "11:00", "13:00"])
        self.assertEqual(schedule.hours_for({"hours": {"tue": ["07:00", "17:00"]}}, datetime.date(2026, 6, 16)), ("07:00", "17:00"))

    def test_the_greeting_is_plain_but_the_agent_owns_up_when_asked(self):
        for g in LAWN["greetings"]:
            said = prompts.announce(LAWN, g.format(daypart="morning"))
            self.assertNotIn("virtual assistant", said.lower())
            self.assertNotIn("recorded", said.lower())
        p = prompts.stable_prompt(LAWN)
        self.assertIn("virtual assistant", p.lower())          # the standing rule: say so plainly when asked
        self.assertIn("never claim", p.lower())
        self.assertIn("Is this call recorded?", p)
        self.assertTrue(LAWN["notice_waived"])
        for needle in ("Never give advice about chemicals", "Never quote a price for a particular lot", "never claim",
                       "Season awareness", "Power line down", "urgent_visit", "How you sound", "never open with"):
            self.assertIn(needle.lower(), p.lower(), needle)
        for forbidden in ("gas smell", "carbon monoxide", "rotten-egg", "furnace"):
            self.assertNotIn(forbidden, p.lower(), forbidden)

    def test_no_texting_or_calendar_promises(self):
        self.assertFalse(LAWN["texting_enabled"])
        faq = " ".join(f["a"] for f in LAWN["faqs"]).lower()
        self.assertIn("can't send texts", faq)
        self.assertNotIn("calendar", faq)


class Urgent(unittest.TestCase):
    def name(self, text, lang="en"):
        got = safety.match("lawn_care", text, lang)
        return got[0] if got else None

    def test_each_urgent_case_gets_its_line(self):
        cases = {
            "there's a power line down in my yard": "power_line",
            "a wire is on the ground and sparking": "power_line",
            "a tree fell on the power lines": "power_line",
            "my husband is hurt, a limb fell on him": "injury",
            "a tree came down across my driveway": "fallen_tree",
            "a big limb crashed onto the roof": "fallen_tree",
            "my sprinkler line broke and the yard is flooding": "irrigation_break",
            "the sprinkler system won't shut off": "irrigation_break",
        }
        for text, want in cases.items():
            self.assertEqual(self.name(text), want, text)

    def test_ordinary_calls_trigger_nothing(self):
        for text in ("I want weekly mowing", "can you aerate my lawn", "I'd like my driveway plowed this winter",
                     "do you trim trees", "my sprinkler heads need adjusting", "I need a spring cleanup", "I don't have a downed power line"):
            self.assertIsNone(self.name(text), text)

    def test_home_repair_lines_are_not_in_this_vertical(self):
        for text in ("I smell gas", "my carbon monoxide alarm is going off", "I smell something burning in the furnace"):
            self.assertIsNone(self.name(text), text)

    def test_spanish_lines(self):
        self.assertEqual(self.name("se cayó un árbol en mi entrada", "es"), "fallen_tree")
        self.assertEqual(self.name("hay un cable caído en el suelo", "es"), "power_line")

    def test_a_downed_line_is_not_a_leave_now_hang_up(self):
        self.assertFalse(safety.must_leave("power_line", "a power line is down"))


class Scenarios(unittest.TestCase):
    def test_between_eight_and_twelve_with_known_services(self):
        self.assertTrue(8 <= len(SCENARIOS) <= 12)
        ids = {s["id"] for s in LAWN["services"]}
        self.assertEqual(len({s["id"] for s in SCENARIOS}), len(SCENARIOS))
        for s in SCENARIOS:
            if "service" in s["expect"]:
                self.assertIn(s["expect"]["service"], ids, s["id"])
            self.assertTrue(s["rubric"] and s["persona"] and s["at"], s["id"])
        for quick in LAWN["simulate_quick"]:
            self.assertIn(quick, {s["id"] for s in SCENARIOS})


class Character(unittest.TestCase):
    def test_many_greetings_none_announce_the_ai_or_recording(self):
        self.assertGreaterEqual(len(LAWN["greetings"]), 12)
        for g in LAWN["greetings"]:
            self.assertIn("Cedar Ridge Lawn and Snow", g)
            self.assertIn("Riley", g)
            g.format(daypart="morning")
            self.assertNotRegex(g.lower(), r"virtual|assistant|recorded|\bai\b")

    def test_every_call_draws_a_different_character(self):
        import random
        now = schedule.local_now(LAWN, "2026-10-20T10:15")
        seen = {prompts.call_state(LAWN, now, None, None, [], rng=random.Random(i)) for i in range(40)}
        self.assertGreater(len(seen), 30)
        text = "\n".join(seen)
        self.assertIn("Your character on this call:", text)
        self.assertIn("answer most of the time with no acknowledgement word at all", text)
        self.assertIn("leaf drop", text)                                   # an October aside
        self.assertNotIn("snowbanks", text)                                # but no deep-winter aside in October
        self.assertNotIn("one sec", text.lower())

    def test_other_shops_get_no_character_kit(self):
        import random
        now = schedule.local_now(LAWN, "2026-10-20T10:15")
        self.assertNotIn("Your character", prompts.call_state(load_business(CFG, "hvac"), now, None, None, [], rng=random.Random(1)))

    def test_the_prompt_keeps_the_character_inside_the_rules(self):
        p = prompts.stable_prompt(LAWN).lower()
        for needle in ("riley has real character", "no body", "made-up personal history", "don't reuse a phrase twice"):
            self.assertIn(needle, p)

    def test_thinking_sounds_and_stall_lines_vary_and_never_say_one_sec(self):
        import random
        from receptionist import live, tools
        for lang, pool in live.STILL_WORKING.items():
            self.assertGreaterEqual(len(pool), 4)
            self.assertFalse(any("sec" in x.lower() for x in pool))
        for lang, tool_pools in tools.FILLER_VARIANTS.items():
            self.assertEqual(set(tool_pools), set(tools.FILLERS[lang]))
            for name, pool in tool_pools.items():
                self.assertIn(tools.FILLERS[lang][name], pool)
        ctx = tools.ToolContext(LAWN, None, None, None, "c")
        lines = [tools.pick_filler(ctx, "check_availability", random.Random(i)) for i in range(5)]
        ctx.last_filler = None
        run = []
        for _ in range(30):
            run.append(tools.pick_filler(ctx, "check_availability"))
        self.assertTrue(all(a != b for a, b in zip(run, run[1:])))          # never the same line twice running
        self.assertGreater(len(set(run)), 2)
        self.assertTrue(live.SLOW_REPLY_S >= 3.0)
