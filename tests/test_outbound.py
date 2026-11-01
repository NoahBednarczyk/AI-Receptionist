import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from receptionist import outbound

TZ = ZoneInfo("America/Los_Angeles")
TUE_10AM = datetime(2026, 11, 3, 10, 0, tzinfo=TZ)
LEAD = {"name": "Case Plumbing", "phone": "208-304-3883", "stage": "leads", "type": "Plumbing"}


def cfg(**kw):
    return {"outbound": kw}


class GateTests(unittest.TestCase):
    def test_default_mode_is_dry_run_and_never_dials(self):
        rows = outbound.plan_queue([LEAD], TUE_10AM, cfg())
        self.assertTrue(rows[0]["would_call"])
        self.assertFalse(rows[0]["ok"])

    def test_warm_only_needs_consent(self):
        c = cfg(mode="warm-only")
        self.assertFalse(outbound.can_call(LEAD, TUE_10AM, c)[0])
        self.assertTrue(outbound.can_call({**LEAD, "consent": "yes"}, TUE_10AM, c)[0])

    def test_cold_is_off_until_enabled_and_needs_a_landline(self):
        self.assertIn("switched off", outbound.can_call(LEAD, TUE_10AM, cfg(mode="cold"))[1])
        c = cfg(mode="cold", cold_enabled=True)
        self.assertIn("landline", outbound.can_call(LEAD, TUE_10AM, c)[1])
        self.assertTrue(outbound.can_call({**LEAD, "line": "landline"}, TUE_10AM, c)[0])

    def test_hours_weekend_dnc_attempts_followup(self):
        c = cfg()
        self.assertIn("call hours", outbound.can_call(LEAD, TUE_10AM.replace(hour=19), c)[1])
        self.assertIn("weekend", outbound.can_call(LEAD, datetime(2026, 11, 7, 10, tzinfo=TZ), c)[1])
        self.assertIn("do-not-call", outbound.can_call(LEAD, TUE_10AM, c, dnc={"+12083043883"})[1])
        self.assertIn("do-not-call", outbound.can_call({**LEAD, "dnc": True}, TUE_10AM, c)[1])
        two = [TUE_10AM - timedelta(days=1), TUE_10AM - timedelta(days=2)]
        self.assertIn("tried 2 times", outbound.can_call(LEAD, TUE_10AM, c, attempts=two)[1])
        self.assertTrue(outbound.can_call(LEAD, TUE_10AM, c, attempts=[TUE_10AM - timedelta(days=9)])[0])
        self.assertIn("follow-up", outbound.can_call({**LEAD, "followDate": "2026-11-10"}, TUE_10AM, c)[1])
        self.assertIn("phone", outbound.can_call({**LEAD, "phone": ""}, TUE_10AM, c)[1])

    def test_only_list_stage_is_queued_and_consented_first(self):
        leads = [LEAD, {**LEAD, "name": "Warm", "phone": "208-555-0111", "consent": "yes"}, {**LEAD, "name": "Done", "stage": "client"}]
        rows = outbound.plan_queue(leads, TUE_10AM, cfg())
        self.assertEqual([r["lead"]["name"] for r in rows], ["Warm", "Case Plumbing"])


class SlotTests(unittest.TestCase):
    def test_slots_stay_in_windows_and_skip_taken(self):
        slots = outbound.meeting_slots(TUE_10AM, cfg())
        self.assertEqual(slots[0], datetime(2026, 11, 3, 16, 30, tzinfo=TZ))
        self.assertTrue(all(s.weekday() < 5 for s in slots))
        again = outbound.meeting_slots(TUE_10AM, cfg(), taken=[slots[0]])
        self.assertNotIn(slots[0], again)


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.blocks = outbound.build_prompt({**LEAD, "contact": "Dale"}, TUE_10AM, cfg())
        self.text = "\n".join(b["text"] for b in self.blocks)

    def test_discloses_ai_and_never_pretends(self):
        self.assertIn("answer yes, plainly", self.text)
        self.assertIn("Never claim to be human", self.text)
        self.assertIn("never say you are researching for a different company", self.text.replace("You never", "never"))

    def test_has_structure_pushback_and_hard_stop(self):
        for needle in ("Call structure", "insistence ladder", "up to **three** asks", "mark_do_not_call", "Hard stops", "208-428-6760",
                       "cheerfully naive", "say yes in the same breath", "Persuade; never mislead", "Angry, rude or hostile"):
            self.assertIn(needle, self.text)

    def test_call_block_names_the_lead_and_offers_times(self):
        self.assertIn("Case Plumbing", self.blocks[1]["text"])
        self.assertIn("Dale", self.blocks[1]["text"])
        self.assertIn("4:30", self.blocks[1]["text"])
        self.assertNotIn("{", self.text.replace("{owner}", ""))

    def test_tools_include_stop_and_booking(self):
        names = {t["name"] for t in outbound.schemas()}
        self.assertTrue({"book_meeting", "log_call", "mark_do_not_call", "end_call", "check_meeting_slots"} <= names)


class CrmTests(unittest.TestCase):
    def test_call_row_and_lead_update(self):
        log = {"outcome": "Demo booked", "summary": "Misses ~6 calls a week.", "objection": "", "follow_up_date": "2026-11-10"}
        row = outbound.call_row(LEAD, log, TUE_10AM, 4.26, 12, cfg())
        self.assertEqual((row["date"], row["time"], row["outcome"], row["duration"], row["source"]), ("2026-11-03", "10:00", "Demo booked", 4.3, "ai-caller"))
        self.assertEqual(row["objection"], "None")
        up = outbound.lead_update(LEAD, log)
        self.assertEqual((up["stage"], up["followDate"]), ("pain", "2026-11-10"))


if __name__ == "__main__":
    unittest.main()


class PromptLintTests(unittest.TestCase):
    def test_no_banned_phrases_and_reasonable_size(self):
        text = "\n".join(b["text"] for b in outbound.build_prompt(LEAD, TUE_10AM, cfg()))
        # these may appear only inside "never say" instructions
        for phrase in ("I'd be happy to assist", "How may I assist"):
            self.assertNotIn(phrase, text)
        self.assertLess(len(text) / 4, 6000)   # roughly tokens: a bigger prompt costs more on every cache write
        self.assertEqual(text.count("# Hard stops"), 1)

    def test_every_objection_has_an_answer_and_ladder_has_three_asks(self):
        text = outbound.PLAYBOOK.read_text()
        for o in ("Not interested", "Send me some info", "already have someone", "How much", "Is this a robot", "don't trust AI", "bad time", "how'd you get my number"):
            self.assertIn(o.lower(), text.lower())
        for ask in ("Ask 1", "Ask 2", "Ask 3"):
            self.assertIn(ask, text)
