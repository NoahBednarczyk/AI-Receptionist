import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from receptionist import finance, spend_report as sr

NOW = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)
CFG = {"prices_per_mtok": {"claude-sonnet-5-5": [2, 10]},
       "finance": {"live_key_names": ["AI Receptionist"], "dev_key_names": ["dev"], "project_start": "2026-10-30", "fixed_start": "2026-10-30"},
       "spend_report": {"project_budget_usd": [100, 300],
                        "fixed_monthly_estimates": [{"service": "Render", "usd": 7.0}]},
       "budget": {}}


def fake_http(method, url, headers=None, body=None):
    if "list-calls" in url:
        ts = int(datetime(2026, 11, 1, 10, tzinfo=timezone.utc).timestamp() * 1000)
        return {"items": [{"start_timestamp": ts, "duration_ms": 120000,
                           "call_cost": {"combined_cost": 33.0, "total_duration_seconds": 120,
                                         "product_costs": [{"product": "tts", "cost": 20.0}, {"product": "llm", "cost": 13.0}]}}],
                "has_more": False}
    if "api_keys" in url:
        return {"data": [{"id": "k1", "name": "AI Receptionist"}, {"id": "k2", "name": "dev"}], "has_more": False}
    if "usage_report" in url:
        row = lambda k: {"api_key_id": k, "model": "claude-sonnet-5-5", "uncached_input_tokens": 1_000_000, "output_tokens": 100_000,
                         "cache_read_input_tokens": 0, "cache_creation": {}}
        return {"data": [{"starting_at": "2026-11-01T00:00:00Z", "results": [row("k1"), row("k2")]}], "has_more": False}
    if "cost_report" in url:
        return {"data": [], "has_more": False}
    raise AssertionError(url)


class FinanceTests(unittest.TestCase):
    def build(self, env):
        with mock.patch.object(sr, "_http", fake_http), mock.patch("receptionist.finance.budget._read", return_value={"days": {"2026-11-01": 0.5}}):
            return finance.build(CFG, env=env, now=NOW)

    def test_splits_live_dev_and_retell(self):
        snap = self.build({"RETELL_API_KEY": "r", "ANTHROPIC_ADMIN_KEY": "a"})
        c = snap["current"]
        self.assertEqual(c["calls"]["retellUsd"], 0.33)
        self.assertEqual(c["calls"]["claudeLiveUsd"], 3.0)          # 1M in at $2 + 0.1M out at $10, live key only
        self.assertEqual(c["excluded"][0]["usd"], 3.0)             # dev key shown, not counted
        self.assertEqual(c["totalUsd"], round(0.33 + 3.0 + 0.5 + 7.0, 2))
        self.assertEqual(snap["daily"]["days"]["2026-11-01"]["t"], 0.5)

    def test_missing_keys_are_reported_not_zeroed(self):
        c = self.build({})["current"]
        self.assertEqual(len(c["problems"]), 2)
        self.assertEqual(c["rows"][0]["status"], "missing")

    def test_files_hold_no_secrets(self):
        snap = self.build({"RETELL_API_KEY": "key_secret", "ANTHROPIC_ADMIN_KEY": "sk-ant-admin01-secret"})
        with tempfile.TemporaryDirectory() as d:
            for p in finance.write_files(snap, d):
                text = p.read_text()
                json.loads(text)
                self.assertNotIn("secret", text)


if __name__ == "__main__":
    unittest.main()
