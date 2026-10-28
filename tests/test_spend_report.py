"""The spend report: sums what the APIs return, skips services without a key, never counts an incomplete total as whole."""
import unittest
from datetime import datetime, timezone
from unittest import mock

from receptionist import spend_report
from receptionist.config import load_config

CFG = load_config()
NOW = datetime(2026, 10, 28, 13, 0, tzinfo=timezone.utc)


def fake_http(calls):
    def http(method, url, headers, body=None):
        calls.append((method, url, headers, body))
        if "/v3/list-calls" in url:
            return {"items": [{"call_id": "a", "call_cost": {"combined_cost": 250, "total_duration_seconds": 120}},
                              {"call_id": "b", "call_cost": {"combined_cost": 50.5, "total_duration_seconds": 30}}],
                    "has_more": False}
        if "cost_report" in url:
            return {"data": [{"results": [{"amount": "123.45"}, {"amount": "10"}]}], "has_more": False}
        if "subscription" in url:
            return {"tier": "starter", "status": "active", "next_invoice": {"amount_due_cents": 500}}
        raise AssertionError(url)
    return http


class SpendReportTests(unittest.TestCase):
    def test_sums_the_apis_and_adds_the_fixed_estimates(self):
        calls = []
        env = {"RETELL_API_KEY": "r", "ANTHROPIC_ADMIN_KEY": "a", "ELEVENLABS_API_KEY": "e"}
        with mock.patch.object(spend_report, "_http", fake_http(calls)):
            rep = spend_report.build(CFG, env=env, now=NOW)
        rows = {r["service"].split(" (")[0]: r for r in rep["rows"]}
        self.assertEqual(rows["Retell"]["usd"], 3.0)            # 300.5 cents
        self.assertEqual(rows["Anthropic API"]["usd"], 1.33)    # 133.45 cents
        self.assertEqual(rep["measured_usd"], 4.33)             # the upcoming ElevenLabs invoice is not counted
        self.assertEqual(rep["estimated_usd"], 9.25)
        self.assertEqual(rep["incomplete"], [])
        self.assertEqual(rep["since"], "2026-10-01")
        self.assertTrue(all(c[0] in ("GET", "POST") and ("list-calls" in c[1] or c[0] == "GET") for c in calls))

    def test_a_missing_key_skips_the_service_and_flags_the_total_as_a_floor(self):
        with mock.patch.object(spend_report, "_http", fake_http([])):
            rep = spend_report.build(CFG, env={"RETELL_API_KEY": "r"}, now=NOW)
        self.assertEqual(len(rep["incomplete"]), 2)
        self.assertIn("floor", spend_report.render(rep))

    def test_an_api_error_is_reported_not_raised(self):
        def boom(*a, **k):
            raise RuntimeError("401 unauthorized")
        with mock.patch.object(spend_report, "_http", boom):
            rep = spend_report.build(CFG, env={"RETELL_API_KEY": "r"}, now=NOW)
        self.assertEqual(rep["rows"][0]["status"], "error")
        self.assertIn("401", rep["rows"][0]["detail"])

    def test_pagination_stops_instead_of_looping_forever(self):
        def endless(method, url, headers, body=None):
            return {"items": [{"call_id": str(i), "call_cost": {"combined_cost": 1}} for i in range(1000)],
                    "has_more": True, "pagination_key": "next"}
        with mock.patch.object(spend_report, "_http", endless):
            row = spend_report.retell(NOW, "r")
        self.assertEqual(row["status"], "error")

    def test_the_retell_request_uses_v3_and_the_documented_filter(self):
        calls = []
        with mock.patch.object(spend_report, "_http", fake_http(calls)):
            spend_report.retell(NOW, "r")
        method, url, _, body = calls[0]
        self.assertEqual((method, url), ("POST", "https://api.retellai.com/v3/list-calls"))
        self.assertEqual(body["filter_criteria"]["start_timestamp"]["op"], "ge")

    def test_keys_never_appear_in_the_output(self):
        with mock.patch.object(spend_report, "_http", fake_http([])):
            rep = spend_report.build(CFG, env={"RETELL_API_KEY": "SECRET-r", "ANTHROPIC_ADMIN_KEY": "SECRET-a"}, now=NOW)
        self.assertNotIn("SECRET", spend_report.render(rep) + str(rep))


if __name__ == "__main__":
    unittest.main()
