import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from receptionist import outbound, outbound_live as ol, tools
from receptionist.store import Store

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 3, 10, 0, tzinfo=TZ)
LEAD = {"id": "L01", "name": "Case Plumbing", "phone": "208-304-3883", "stage": "leads", "type": "Plumbing"}
CFG = {"data_dir": "", "models": {"agent": "a", "agent_effort": "low", "outbound": "b", "outbound_effort": "high"},
       "agent": {"cache_ttl": "1h"}, "outbound": {"mode": "warm-only"}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = ol.State(Path(self.dir.name) / "outbound.json")
        self.store = Store(str(Path(self.dir.name) / "db.sqlite"))
        self.s = ol.OutboundSession(None, {**CFG, "data_dir": self.dir.name}, LEAD, self.state, self.store, NOW, call_id="t1")


class StopTests(unittest.TestCase):
    def test_hard_and_soft_stops(self):
        for t in ("stop calling me", "Take me off your list", "remove my number please", "put us on the do not call list", "I said don't call again"):
            self.assertTrue(ol.stop_request(t), t)

    def test_picking_another_time_is_not_a_stop(self):
        for t in ("don't call me today, try Thursday", "Don't call back until after lunch", "not now, call me tomorrow"):
            self.assertFalse(ol.stop_request(t), t)

    def test_ai_question(self):
        for t in ("Is this a robot?", "Are you a real person?", "am I talking to an AI", "wait are you a bot"):
            self.assertTrue(ol.asked_if_ai(t), t)
        self.assertFalse(ol.asked_if_ai("we get about ten calls a week"))


class SessionTests(Fixture):
    def test_opener_has_a_hook_names_the_caller_and_says_it_is_a_sales_call_but_not_ai(self):
        import random
        s = ol.outbound.settings(CFG)
        for trade in ("Plumbing", "HVAC", "Other"):
            for seed in range(12):
                text = ol.opener({"name": "Case Plumbing", "type": trade, "contact": "Dale Smith"}, s, random.Random(seed))
                self.assertTrue(text.startswith("Hi Dale? "), text)
                self.assertIn("Case Plumbing", text)
                self.assertIn("sales call", text.lower().replace("selling something", "sales call"))
                self.assertIn("Ringback", text)
                self.assertNotRegex(text, r"(?i)\b(ai|robot|bot|artificial)\b")
                self.assertNotRegex(text, r"(?i)\bhuman\b")
                self.assertNotIn("{", text)

    def test_a_lead_written_hook_wins_and_hooks_vary(self):
        import random
        s = ol.outbound.settings(CFG)
        self.assertTrue(ol.opener({"name": "X", "hook": "Is {biz} the shop with the talking van?"}, s).startswith("Is X the shop with the talking van?"))
        many = {ol.opener({"name": "X", "type": "Plumbing"}, s, random.Random(i)) for i in range(40)}
        self.assertGreater(len(many), 10)

    def test_uses_the_outbound_model_and_cached_prompt(self):
        self.assertEqual(self.s.cfg["models"]["agent"], "b")
        self.assertEqual(self.s.cfg["models"]["agent_effort"], "high")
        self.assertIn("cache_control", self.s.system[0])
        self.assertNotIn("cache_control", self.s.system[1])

    def test_stop_request_ends_the_call_without_a_model_and_marks_dnc(self):
        said = []
        turn = self.s.respond("please stop calling me", said.append)
        self.assertTrue(turn.ended and self.s.ended)
        self.assertIn("+12083043883", self.state.dnc())
        self.assertTrue(said and "list" in said[0])
        self.assertEqual({c["name"] for c in turn.tools}, {"mark_do_not_call", "log_call", "end_call"})
        row = self.s.finish(NOW, 0.4, "asked to stop")
        self.assertEqual(row["outcome"], "Not interested")
        self.assertEqual(self.state.data["lead_updates"]["L01"]["stage"], "contacted")
        self.assertEqual(len(self.state.attempts("+12083043883")), 1)


class ToolTests(Fixture):
    def run_tool(self, name, args):
        return tools.run_tool(self.s.ctx, name, args)

    def test_booking_only_from_offered_slots_and_needs_ten_digits(self):
        res, err = self.run_tool("book_meeting", {"start": NOW.isoformat(), "owner_name": "Dale", "callback_number": "2083043883"})
        self.assertTrue(err)
        slots, _ = self.run_tool("check_meeting_slots", {})
        start = slots["open"][0]["start"]
        res, err = self.run_tool("book_meeting", {"start": start, "owner_name": "Dale", "callback_number": "304-3883"})
        self.assertTrue(err)
        res, err = self.run_tool("book_meeting", {"start": start, "owner_name": "Dale", "callback_number": "208-304-3883"})
        self.assertFalse(err)
        self.assertIn(slots["open"][0]["say"].split(" at ")[1], res["booked"])
        again, _ = self.run_tool("check_meeting_slots", {})
        self.assertNotIn(start, [s["start"] for s in again["open"]])   # booked times are gone

    def test_demo_booked_cannot_be_logged_without_a_booking(self):
        res, err = self.run_tool("log_call", {"outcome": "Demo booked", "summary": "x"})
        self.assertTrue(err)
        res, err = self.run_tool("log_call", {"outcome": "Callback requested", "summary": "wants a call Friday"})
        self.assertFalse(err)

    def test_state_survives_a_restart(self):
        self.state.add_dnc("+12085550100")
        self.assertIn("+12085550100", ol.State(self.state.path).dnc())


class RetellTests(Fixture):
    def test_agent_payload_uses_its_own_socket_and_names_the_caller_on_voicemail(self):
        p = ol.agent_payload(CFG, "https://x.onrender.com/", "SECRET", {"voice_id": "voice_1", "voice_speed": 1.15})
        self.assertEqual((p["voice_id"], p["voice_speed"]), ("voice_1", 1.15))
        self.assertEqual(p["response_engine"]["llm_websocket_url"], "wss://x.onrender.com/llm-websocket-out/SECRET")
        self.assertIn("Ringback", p["voicemail_option"]["action"]["text"])
        self.assertIsNone(p["post_call_analysis_model"])

    def test_place_call_is_refused_until_everything_allows_it(self):
        calls = []
        fn = lambda *a: calls.append(a) or {"call_id": "c"}
        cfg = {**CFG, "data_dir": self.dir.name}
        warm = {**LEAD, "consent": "yes"}
        with self.assertRaises(RuntimeError):   # switch not set
            ol.place_call(cfg, warm, NOW, self.state, fn, env={})
        with self.assertRaises(RuntimeError):   # dry-run mode
            ol.place_call({**cfg, "outbound": {"mode": "dry-run"}}, warm, NOW, self.state, fn, env={"OUTBOUND_LIVE": "1"})
        with self.assertRaises(RuntimeError):   # no consent in warm-only
            ol.place_call(cfg, LEAD, NOW, self.state, fn, env={"OUTBOUND_LIVE": "1"})
        (Path(self.dir.name) / "retell_outbound.json").write_text(json.dumps({"number": None, "agent_id": "ag"}))
        with self.assertRaises(RuntimeError):   # agent exists but the number is still pending
            ol.place_call(cfg, warm, NOW, self.state, fn, env={"OUTBOUND_LIVE": "1"})
        (Path(self.dir.name) / "retell_outbound.json").write_text(json.dumps({"number": "+12085550100", "agent_id": "ag"}))
        ol.place_call(cfg, warm, NOW, self.state, fn, env={"OUTBOUND_LIVE": "1"})
        self.assertEqual(calls[0][1], "/v2/create-phone-call")
        self.assertEqual(calls[0][2]["metadata"]["lead"]["id"], "L01")
        self.state.add_dnc("+12083043883")
        with self.assertRaises(RuntimeError):   # do-not-call wins over consent
            ol.place_call(cfg, warm, NOW, self.state, fn, env={"OUTBOUND_LIVE": "1"})


if __name__ == "__main__":
    unittest.main()


class OpenerAndSlotsTests(Fixture):
    def test_first_words_get_the_opener_without_a_model_then_the_session_is_opened(self):
        said = []
        turn = self.s.respond("Hello?", said.append)
        self.assertEqual(turn.text, self.s.opener)
        self.assertEqual(said, [self.s.opener])
        self.assertIn("Case Plumbing", self.s.opener)
        self.assertEqual([m["role"] for m in self.s.messages], ["user", "assistant"])   # the model sees a normal history
        self.assertFalse(self.s.ended)

    def test_manager_slots_replace_the_defaults_until_stale(self):
        slots = ["2026-11-03T13:15:00-08:00", "2026-11-03T09:00:00-08:00", "2026-11-04T10:00:00-08:00"]
        self.state.set_slots(slots, NOW)
        res, err = tools.run_tool(self.s.ctx, "check_meeting_slots", {})
        starts = [o["start"] for o in res["open"]]
        self.assertEqual(starts, ["2026-11-03T13:15:00-08:00", "2026-11-04T10:00:00-08:00"])   # 9:00 is under two hours away
        self.assertEqual(self.state.manager_slots(NOW + timedelta(hours=40)), None)

    def test_booking_removes_a_manager_slot(self):
        self.state.set_slots(["2026-11-03T13:15:00-08:00", "2026-11-04T10:00:00-08:00"], NOW)
        tools.run_tool(self.s.ctx, "check_meeting_slots", {})
        tools.run_tool(self.s.ctx, "book_meeting", {"start": "2026-11-03T13:15:00-08:00", "owner_name": "Dale", "callback_number": "208-304-3883"})
        res, _ = tools.run_tool(self.s.ctx, "check_meeting_slots", {})
        self.assertEqual([o["start"] for o in res["open"]], ["2026-11-04T10:00:00-08:00"])

    def test_place_call_sends_an_idempotency_key_and_honors_internal_dnc(self):
        calls = []
        cfg = {**CFG, "data_dir": self.dir.name}
        (Path(self.dir.name) / "retell_outbound.json").write_text(json.dumps({"number": "+12085550100", "agent_id": "ag"}))
        ol.place_call(cfg, {**LEAD, "consent": "yes"}, NOW, self.state, lambda *a: calls.append(a) or {}, env={"OUTBOUND_LIVE": "1"})
        body = calls[0][2]
        self.assertRegex(body["idempotency_key"], r"^[A-Za-z0-9_-]{5,255}$")
        self.assertTrue(body["honor_internal_dnc"])
        self.assertNotIn("enable_voicemail_detection", ol.agent_payload(CFG, "https://x.io", "S"))


class HandoffTests(unittest.TestCase):
    def test_crm_files_hold_calls_leads_and_meeting_tasks(self):
        data = {"calls": [{"n": 3, "business": "Case Plumbing", "outcome": "Demo booked"}], "lead_updates": {"L01": {"stage": "pain"}},
                "bookings": [{"start": "2026-11-03T16:30:00-08:00", "owner": "Dale", "phone": "+12083043883", "business": "Case Plumbing"}]}
        with tempfile.TemporaryDirectory() as d:
            files = ol.crm_files(data, d)
            by = {f.name: json.loads(f.read_text()) for f in files}
        self.assertEqual(list(by["outbound-calls.json"]), ["ai3"])
        self.assertEqual(by["outbound-leads.json"]["L01"]["stage"], "pain")
        self.assertEqual(by["outbound-tasks.json"]["ai-meet-0"]["due"], "2026-11-03")


class SocketTests(Fixture):
    def test_full_socket_flow_for_a_stop_request_needs_no_model(self):
        import asyncio
        from receptionist import live
        events = [{"interaction_type": "call_details", "call": {"call_id": "c1", "metadata": {"lead": LEAD}}},
                  {"interaction_type": "response_required", "response_id": 1,
                   "transcript": [{"role": "agent", "content": self.s.greeting}, {"role": "user", "content": "Yeah stop calling me."}]}]
        sent, finished = [], []

        async def run():
            queue = asyncio.Queue()
            for e in events:
                queue.put_nowait(e)

            async def receive():
                if queue.empty():
                    await asyncio.sleep(0.3)   # the call stays open until the worker has answered
                    raise ConnectionError("hung up")
                return await queue.get()

            async def send(m):
                sent.append(m)
            try:
                await live.run_retell_call(receive, send, lambda call: self.s, finished.append)
            except ConnectionError:
                pass
        asyncio.run(run())
        greeting = next(m for m in sent if m.get("response_id") == 0)
        self.assertEqual(greeting["content"], "")   # Retell waits for the person to say hello
        last = [m for m in sent if m.get("response_id") == 1]
        self.assertTrue(any(m.get("end_call") for m in last))
        self.assertTrue(any("list" in m.get("content", "") for m in last))
        self.assertEqual(finished, [self.s])
        self.assertIn("+12083043883", self.state.dnc())
