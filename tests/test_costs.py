"""W4.1: per shop, per call cost table for the admin. Fakes only; no Retell or Claude call."""
import unittest

import tests  # noqa: F401
from receptionist import finance
from receptionist.store import Store
from tests import test_notify


class CostTableTests(unittest.TestCase):
    ROWS = [
        {"id": "retell-a", "business_id": "s1", "platform": "retell", "started_at": "2026-11-12T10:00:00+00:00",
         "ended_at": "2026-11-12T10:03:00+00:00", "claude_usd": 0.06},
        {"id": "retell-b", "business_id": "s1", "platform": "retell", "started_at": "2026-11-12T11:00:00+00:00",
         "ended_at": "2026-11-12T11:01:00+00:00", "claude_usd": None},
        {"id": "retell-c", "business_id": "s2", "platform": "retell", "started_at": "2026-11-12T12:00:00+00:00",
         "ended_at": None, "claude_usd": 0.02},
    ]

    def test_per_minute_uses_retell_billing_and_skips_calls_with_a_missing_part(self):
        t = finance.cost_table(self.ROWS, {"a": {"usd": 0.54, "minutes": 3.0}, "b": {"usd": 0.18, "minutes": 1.0}})
        s1 = t["shops"]["s1"]
        self.assertEqual(s1["calls_total"], 2)
        self.assertEqual(s1["calls_counted"], 1)          # b has no Claude cost (an old row)
        self.assertAlmostEqual(s1["per_minute_usd"], 0.2, places=3)   # (0.54 + 0.06) / 3
        self.assertIsNone(s1["calls"][1]["total_usd"])
        self.assertIsNone(t["shops"]["s2"]["per_minute_usd"])         # Retell has no bill for c: unknown, not zero

    def test_without_a_retell_key_retell_cost_is_unknown_not_zero(self):
        t = finance.cost_table(self.ROWS, None)
        self.assertFalse(t["retell_known"])
        self.assertIsNone(t["shops"]["s1"]["calls"][0]["retell_usd"])
        self.assertAlmostEqual(t["shops"]["s1"]["calls"][0]["minutes"], 3.0)

    def test_the_store_keeps_the_claude_cost(self):
        s = Store(":memory:")
        from datetime import datetime, timezone
        s.start_call("retell-x", "s1", "retell", "+1", datetime(2026, 11, 12, tzinfo=timezone.utc))
        s.save_cost("retell-x", 0.0412, {"agent": {}, "postcall": {}})
        self.assertEqual(s.call_costs("2026-11-01")[0]["claude_usd"], 0.0412)


class AdminCostsTests(unittest.TestCase):
    setUp = test_notify.Through_the_server.setUp      # the app with a fake summary client
    tearDown = test_notify.Through_the_server.tearDown
    end_call = test_notify.Through_the_server.end_call

    def test_a_finished_call_shows_its_claude_cost_to_the_admin_only(self):
        from datetime import datetime, timezone
        Store(f"{self.tmp.name}/receptionist.sqlite").start_call("retell-zzz", "north", "retell", "+12085550142", datetime.now(timezone.utc))
        self.end_call("zzz")
        self.assertEqual(self.http.get("/admin/costs").status_code, 404)
        r = self.http.get("/admin/costs", headers={"Authorization": "Bearer " + "a" * 32})
        self.assertEqual(r.status_code, 200)
        calls = r.json()["shops"]["north"]["calls"]
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(calls[0]["claude_usd"])


if __name__ == "__main__":
    unittest.main()
