import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import tests  # noqa: F401
from receptionist import keepwarm, latency
from receptionist.server import prewarm_targets

CALL = {"call_id": "call_abcdef123456", "agent_id": "agent_1", "agent_name": "Cedar Ridge receptionist",
        "latency": {"e2e": {"p50": 2100, "p90": 3300}, "llm": {"p50": 1100, "p90": 1700}, "tts": {"p50": 300, "p90": 450}}}


class LatencyTests(unittest.TestCase):
    def test_a_row_shows_e2e_llm_tts_and_the_remainder(self):
        calls = {"POST": {"items": [CALL]}, "GET": {"voice_model": "eleven_v3_conversational", "voice_id": "11labs-Grace"}}
        http = lambda method, url, headers, body=None: calls[method]
        table = latency.rows([CALL], "k", None, http)
        r = table[0]
        self.assertEqual((r["e2e_p50"], r["llm_p50"], r["tts_p50"]), (2100, 1100, 300))
        self.assertEqual(r["remainder_p50"], 700)       # e2e - llm - tts
        self.assertIn("eleven_v3_conversational", r["voice"])
        out = latency.render(table)
        self.assertIn("2100/3300", out)
        self.assertIn("remainder", out)

    def test_it_asks_for_ended_calls_newest_first_and_changes_nothing(self):
        seen = []
        def http(method, url, headers, body=None):
            seen.append((method, url, body))
            return {"items": []}
        latency.fetch_calls("k", 10, http=http)
        method, url, body = seen[0]
        self.assertEqual((method, url.endswith("/v3/list-calls")), ("POST", True))
        self.assertEqual(body["limit"], 10)
        self.assertEqual(body["sort_order"], "descending")
        self.assertEqual(body["filter_criteria"]["call_status"]["value"], ["ended"])

    def test_advice_names_the_voice_model_and_dynamic_responsiveness_but_changes_nothing(self):
        slow = {**CALL, "latency": {"e2e": {"p50": 3000}, "llm": {"p50": 1000}, "tts": {"p50": 600}}}
        notes = latency.advice(latency.rows([slow], None))
        self.assertTrue(any("voice model" in n for n in notes))
        self.assertTrue(any("enable_dynamic_responsiveness" in n for n in notes))
        ok = latency.advice(latency.rows([CALL], None))
        self.assertEqual(ok, [])

    def test_outbound_calls_do_not_count_as_the_receptionist_for_the_remainder_rule(self):
        out = {**CALL, "agent_name": "Ringback discovery caller",
               "latency": {"e2e": {"p50": 4000}, "llm": {"p50": 1000}, "tts": {"p50": 300}}}
        self.assertEqual(latency.advice(latency.rows([out], None)), [])

    def test_missing_fields_do_not_crash(self):
        table = latency.rows([{"call_id": "x"}], None)
        self.assertIsNone(table[0]["remainder_p50"])
        self.assertIn("-", latency.render(table))

    def test_our_first_words_come_from_a_stored_outbound_row(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "outbound.json").write_text(json.dumps({"calls": [{"callId": "c9", "latency": {"median_s": 1.4}}]}))
            self.assertEqual(latency.our_first_words({"data_dir": d}, "c9"), 1.4)
            self.assertIsNone(latency.our_first_words({"data_dir": d}, "other"))

    def test_the_admin_route_is_read_only_and_needs_the_admin_key(self):
        import os
        from fastapi.testclient import TestClient
        from receptionist.server import create_app
        from tests.test_logic import CFG
        token = "t" * 40
        with mock.patch.dict(os.environ, {"DASHBOARD_TOKEN": token, "RETELL_API_KEY": "rk"}), \
                mock.patch("receptionist.latency.report", return_value=[{"call_id": "c1"}]) as rep:
            client = TestClient(create_app(CFG, client=mock.Mock(), warm_cache=False))
            self.assertEqual(client.get("/admin/latency?last=3").status_code, 404)
            ok = client.get("/admin/latency?last=3", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(ok.json(), {"rows": [{"call_id": "c1"}]})
            self.assertEqual(rep.call_args[0][0], 3)


def at(h, m=0, day=10):
    """A UTC time: 2026-11-10 in Pacific time is UTC-8."""
    return datetime(2026, 11, day, h, m, tzinfo=timezone.utc)


class RefresherTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = {"data_dir": self.dir.name, "live": {"cache_refresh": True}}
        self.shop = {"id": "lawn_care", "timezone": "America/Los_Angeles", "phone_numbers": ["+12084286760"]}
        self.calls = []
        self.usage = {"input": 5, "output": 0, "cache_read": 16000, "cache_write": 0}

    def make(self, **cfg):
        self.cfg["live"].update(cfg)
        return keepwarm.CacheRefresher(self.cfg, object(), lambda c: [self.shop],
                                       lambda client, c, biz: self.calls.append(biz["id"]) or self.usage)

    def test_it_fires_at_fifty_minutes_not_before(self):
        r = self.make()
        r.mark("lawn_care", at(18))                      # 10:00 Pacific
        self.assertEqual(r.tick(at(18, 49)), [])
        self.assertEqual(len(r.tick(at(18, 50))), 1)
        self.assertEqual(r.tick(at(19, 30)), [])           # 40 minutes after the refresh
        self.assertEqual(len(r.tick(at(19, 40))), 1)
        self.assertEqual(self.calls, ["lawn_care", "lawn_care"])

    def test_not_outside_the_shops_day(self):
        r2 = self.make()
        self.assertEqual(r2.tick(at(6)), [])               # 22:00 Pacific
        self.assertEqual(r2.tick(at(13)), [])              # 05:00 Pacific
        self.assertEqual(self.calls, [])
        self.assertEqual(len(r2.tick(at(15))), 1)          # 07:00 Pacific

    def test_it_can_be_turned_off(self):
        r = self.make(cache_refresh=False)
        self.assertEqual(r.tick(at(18)), [])
        self.assertEqual(self.calls, [])

    def test_never_for_a_shop_without_a_live_number(self):
        cfg = {"default_business": "a", "live_businesses": ["a", "b"]}
        shops = [{"id": "a", "phone_numbers": ["+1"]}, {"id": "b", "phone_numbers": []}, {"id": "c", "phone_numbers": ["+2"]}]
        with mock.patch("receptionist.server.all_businesses", return_value=shops):
            self.assertEqual([b["id"] for b in prewarm_targets(cfg)], ["a"])

    def test_a_full_write_with_no_read_is_warned_about_and_every_refresh_is_recorded(self):
        self.usage = {"input": 5, "output": 0, "cache_read": 0, "cache_write": 16000}
        r = self.make()
        with self.assertLogs("receptionist", level="WARNING") as logged:
            r.tick(at(18))
        self.assertTrue(any("live.cache_refresh" in l for l in logged.output))
        line = json.loads(Path(self.dir.name, "cache_refresh.jsonl").read_text().splitlines()[0])
        self.assertEqual((line["business"], line["cache_write"]), ("lawn_care", 16000))

    def test_a_failed_refresh_does_not_stop_the_loop(self):
        r = keepwarm.CacheRefresher(self.cfg, object(), lambda c: [self.shop], mock.Mock(side_effect=RuntimeError("boom")))
        with self.assertLogs("receptionist", level="WARNING"):
            self.assertEqual(r.tick(at(18)), [])

    def test_config_json_has_the_refresh_on_at_fifty_minutes(self):
        live = json.loads(Path("config.json").read_text())["live"]
        self.assertTrue(live["cache_refresh"])
        self.assertEqual(live["cache_refresh_minutes"], 50)
