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
        if "usage_report" in url:
            return {"data": [], "has_more": False}
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


class AnthropicDetailTests(unittest.TestCase):
    PRICES = {"claude-sonnet-5-5": [2.0, 10.0]}
    TODAY = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)

    def http(self, calls):
        def http(method, url, headers, body=None):
            calls.append(url)
            if "usage_report" in url:
                return {"data": [{"results": [
                    {"model": "claude-sonnet-5-5", "uncached_input_tokens": 1_000_000, "output_tokens": 100_000,
                     "cache_read_input_tokens": 1_000_000,
                     "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 1_000_000}},
                    {"model": "claude-mystery", "uncached_input_tokens": 5}]}], "has_more": False}
            if "cost_report" in url:
                return {"data": [{"starting_at": "2026-10-31T00:00:00Z", "results": [
                    {"amount": "250", "model": "claude-sonnet-5-5"}, {"amount": "50", "model": None}]}], "has_more": False}
            raise AssertionError(url)
        return http

    def test_today_is_priced_from_tokens_at_the_cache_rates(self):
        with mock.patch.object(spend_report, "_http", self.http([])):
            row = spend_report.anthropic_cost(datetime(2026, 10, 1, tzinfo=timezone.utc), "k", self.TODAY, self.PRICES)
        # 1M in at $2 + 0.1M out at $10 + 1M reads at $0.20 + 1M 5-minute writes at $2.50 + 1M one-hour writes at $4 = $9.70
        today = next(d for d in row["daily"] if d["day"] == "2026-11-01")
        self.assertAlmostEqual(today["usd"], 9.70, places=2)
        self.assertEqual(today["kind"], "estimate from token counts")
        self.assertAlmostEqual(row["usd"], 3.00 + 9.70, places=2)            # $3.00 of finished days
        self.assertIn("claude-mystery", row["detail"])                          # an unpriced model is called out, not guessed
        self.assertEqual(row["by_model"]["claude-sonnet-5-5"], 2.5 + 9.70)

    def test_the_first_day_of_a_month_has_no_finished_days_and_still_reports(self):
        calls = []
        with mock.patch.object(spend_report, "_http", self.http(calls)):
            row = spend_report.anthropic_cost(datetime(2026, 11, 1, tzinfo=timezone.utc), "k", self.TODAY, self.PRICES)
        self.assertEqual(row["status"], "ok")
        self.assertFalse(any("cost_report" in c for c in calls))                # nothing finished to ask for
        self.assertAlmostEqual(row["usd"], 9.70, places=2)

    def test_the_cost_report_is_asked_for_whole_utc_days_ending_before_today(self):
        calls = []
        with mock.patch.object(spend_report, "_http", self.http(calls)):
            spend_report.anthropic_cost(datetime(2026, 10, 1, 13, 5, tzinfo=timezone.utc), "k", self.TODAY, self.PRICES)
        url = next(c for c in calls if "cost_report" in c)
        self.assertIn("starting_at=2026-10-01T00%3A00%3A00Z", url)
        self.assertIn("ending_at=2026-11-01T00%3A00%3A00Z", url)

    def test_the_csv_export_has_totals_and_no_keys(self):
        import csv, tempfile
        from pathlib import Path
        with mock.patch.object(spend_report, "_http", self.http([])):
            rep = spend_report.build(CFG, env={"ANTHROPIC_ADMIN_KEY": "SECRET-a"}, now=self.TODAY)
        with tempfile.TemporaryDirectory() as d:
            files = spend_report.to_csv_files(rep, Path(d) / "spend-2026-11")
            text = "".join(f.read_text() for f in files)
            self.assertEqual({f.name for f in files}, {"spend-2026-11.csv", "spend-2026-11-anthropic.csv"})
            self.assertIn("2026-11-01,", text)
            self.assertIn("claude-sonnet-5-5", text)
            self.assertNotIn("SECRET", text)
            rows = list(csv.reader(files[0].open()))
            self.assertEqual(rows[1][:3], ["service", "status", "usd"])



class CacheCostTests(unittest.TestCase):
    def test_test_runs_and_chats_use_the_cheaper_five_minute_cache_but_the_server_keeps_the_hour(self):
        from receptionist.agent import cache_control, cache_write_mult
        self.assertEqual(cache_control(CFG), {"type": "ephemeral", "ttl": "1h"})        # the live server
        test_cfg = {**CFG, "agent": {**CFG["agent"], "cache_ttl": "5m"}}                  # what simulate and chat use
        self.assertEqual(cache_control(test_cfg), {"type": "ephemeral"})
        self.assertEqual(cache_write_mult(test_cfg), 1.25)

    def test_only_the_live_shops_are_prewarmed_at_startup(self):
        from receptionist.config import all_businesses
        from receptionist.server import prewarm_targets
        self.assertEqual([b["id"] for b in prewarm_targets(CFG)], ["lawn_care"])
        phoned = {b["id"] for b in all_businesses(CFG) if b.get("phone_numbers")}
        self.assertGreater(len(phoned), 1)                        # demo shops have numbers too, and are not warmed
        self.assertEqual([b["id"] for b in prewarm_targets({**CFG, "live_businesses": []})],
                         [b["id"] for b in all_businesses(CFG) if b["id"] == CFG["default_business"] and b.get("phone_numbers")])


if __name__ == "__main__":
    unittest.main()
