import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
