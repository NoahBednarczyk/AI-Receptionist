import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import tests  # noqa: F401  (runs tests/__init__.py: the older tests use the <say> channel's rollback setting)
from receptionist import outbound, outbound_live as ol, tools
from receptionist.store import Store

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 3, 10, 0, tzinfo=TZ)
LEAD = {"id": "L01", "name": "Case Lawn Care", "phone": "208-304-3883", "stage": "leads", "type": "Lawn care"}
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
    def test_opener_is_only_a_short_question_with_no_ai_or_sales_words(self):
        import random
        s = ol.outbound.settings(CFG)
        for seed in range(12):
            named = ol.opener({"name": "Case Lawn Care", "contact": "Dale Smith"}, s, random.Random(seed))
            self.assertIn("Dale", named)
            self.assertNotIn("Smith", named)
            anon = ol.opener({"name": "Case Lawn Care"}, s, random.Random(seed))
            self.assertIn("owner of Case Lawn Care", anon)
            for text in (named, anon):
                self.assertLess(len(text.split()), 14)
                self.assertNotRegex(text, r"(?i)sales|cold|selling|ringback|riley|\bai\b|robot")
                self.assertNotIn("{", text)
        self.assertGreater(len({ol.opener({"name": "X"}, s, random.Random(i)) for i in range(40)}), 2)

    def test_the_fallback_intro_names_the_company_and_does_not_volunteer_ai(self):
        import random
        s = ol.outbound.settings({**CFG, "outbound": {"disclose_ai_in_intro": False}})
        for seed in range(6):
            intro = ol.opener_parts({"name": "X"}, s, random.Random(seed))[1]
            self.assertIn("Ringback", intro)
            self.assertNotRegex(intro, r"(?i)sales|cold|\bai\b|robot")
        on = ol.outbound.settings(CFG)
        for seed in range(6):
            intro = ol.opener_parts({"name": "X"}, on, random.Random(seed))[1]
            self.assertIn("an AI assistant calling for Noah at Ringback", intro)

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
        self.assertEqual(p["voicemail_option"]["action"], {"type": "hangup"})
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


class AgentRecordFileTests(unittest.TestCase):
    def test_none_until_written_then_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"data_dir": d}
            self.assertIsNone(ol.load_agent_record(cfg))
            ol.save_agent_record(cfg, "agent_123", "208-215-2934")
            self.assertEqual(ol.load_agent_record(cfg), {"agent_id": "agent_123", "number": "+12082152934"})

    def test_number_can_be_left_unset(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"data_dir": d}
            ol.save_agent_record(cfg, "agent_123", None)
            self.assertEqual(ol.load_agent_record(cfg), {"agent_id": "agent_123", "number": None})


class LeadsFileTests(unittest.TestCase):
    def test_round_trips_and_is_empty_until_written(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"data_dir": d}
            self.assertEqual(ol.load_leads(cfg), [])
            ol.save_leads(cfg, [LEAD])
            self.assertEqual(ol.load_leads(cfg), [LEAD])


class CallNextTests(Fixture):
    def setUp(self):
        super().setUp()
        import os
        self.cfg = {**CFG, "data_dir": self.dir.name, "outbound": {"mode": "warm-only"}}
        (Path(self.dir.name) / "retell_outbound.json").write_text(json.dumps({"number": "+12085550100", "agent_id": "ag"}))
        self._env = dict(os.environ)
        os.environ["OUTBOUND_LIVE"] = "1"
        self.addCleanup(lambda: os.environ.clear() or os.environ.update(self._env))

    def test_calls_the_first_eligible_lead_and_reports_it(self):
        calls = []
        fn = lambda *a: calls.append(a) or {"call_id": "c1"}
        leads = [{**LEAD, "id": "L02", "consent": "no"}, {**LEAD, "id": "L01", "consent": "yes"}]
        result = ol.call_next(self.cfg, leads, self.state, fn, NOW)
        self.assertEqual(result["called"], "Case Lawn Care")
        self.assertEqual(len(calls), 1)   # only the eligible lead was dialed, not both

    def test_skips_a_lead_whose_call_attempt_fails_and_tries_the_next(self):
        # Both leads are equally eligible by plan_queue's own gate (consent=yes); the first one's dial attempt fails
        # (a Retell API error, say), and call_next should move on to the next candidate instead of giving up.
        calls = []

        def fn(method, path, body):
            if body["to_number"] == "+12084440000":
                raise RuntimeError("Retell POST /v2/create-phone-call failed: 500 boom")
            calls.append(body)
            return {"call_id": "c1"}

        bad = {**LEAD, "id": "L03", "consent": "yes", "phone": "208-444-0000"}
        good = {**LEAD, "id": "L01", "consent": "yes"}
        result = ol.call_next(self.cfg, [bad, good], self.state, fn, NOW)
        self.assertEqual(result["called"], "Case Lawn Care")
        self.assertEqual(len(calls), 1)

    def test_reports_why_when_nothing_is_eligible(self):
        result = ol.call_next(self.cfg, [{**LEAD, "consent": "no"}], self.state, lambda *a: {}, NOW)
        self.assertIsNone(result["called"])
        self.assertIn("consent", result["reason"])

    def test_empty_list_has_its_own_reason(self):
        result = ol.call_next(self.cfg, [], self.state, lambda *a: {}, NOW)
        self.assertEqual(result, {"called": None, "reason": "no leads on the list"})


if __name__ == "__main__":
    unittest.main()


class OpenerAndSlotsTests(Fixture):
    def test_first_words_get_the_opener_without_a_model_then_the_session_is_opened(self):
        said = []
        turn = self.s.respond("Hello?", said.append)
        self.assertEqual(turn.text, self.s.opener)
        self.assertEqual("".join(said), self.s.opener)
        self.assertEqual(said, [self.s.hook_text])
        self.assertFalse(self.s.protected(said[0]))
        self.assertEqual([m["role"] for m in self.s.messages], ["user", "assistant"])   # the model sees a normal history
        self.assertFalse(self.s.ended)

    def test_first_model_turn_says_who_is_calling_without_ai_or_sales_label(self):
        notes = []
        from unittest import mock
        with mock.patch("receptionist.agent.CallSession.respond", lambda self, text, on_text=None, control=None, heard_only=None, note=None: notes.append(note)):
            self.s.respond("Hello?")
            self.s.respond("Why did you call me?")
            self.s.respond("ok and how much")
        self.assertIn("Ringback", notes[0])
        self.assertIn("an AI assistant calling for Noah at Ringback in Sandpoint", notes[0])
        self.assertIn("sales call", notes[0])
        self.assertNotIn("Ringback", notes[1] or "")

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


class OpenerCutOffTests(Fixture):
    """The real call: 'Hello?' then line noise '(unintelligible audio)' cut the opener after the hook, and the agent
    answered 'I didn't quite get that' without ever saying who was calling."""

    def run_socket(self, events):
        import asyncio
        from receptionist import live
        sent = []

        async def run():
            queue = asyncio.Queue()
            for e in events:
                queue.put_nowait(e)

            async def receive():
                if queue.empty():
                    await asyncio.sleep(0.4)
                    raise ConnectionError("hung up")
                if len(sent) > 1:
                    await asyncio.sleep(0.2)   # Retell's requests arrive some time apart
                else:
                    await asyncio.sleep(0.05)
                return await queue.get()

            async def send(m):
                sent.append(m)
            try:
                await live.run_retell_call(receive, send, lambda call: self.s, lambda s: None)
            except ConnectionError:
                pass
        asyncio.run(run())
        return sent

    def test_noise_after_hello_finishes_the_intro_instead_of_asking_to_repeat(self):
        heard = self.s.hook_text   # the greeting played, nobody has said who is calling
        sent = self.run_socket([
            {"interaction_type": "call_details", "call": {"call_id": "c1", "metadata": {"lead": LEAD}}},
            {"interaction_type": "response_required", "response_id": 1, "transcript": [{"role": "user", "content": "Hello?"}]},
            {"interaction_type": "response_required", "response_id": 2,
             "transcript": [{"role": "user", "content": "Hello?"}, {"role": "agent", "content": heard},
                            {"role": "user", "content": "(unintelligible audio)"}]}])
        first = [m for m in sent if m.get("response_id") == 1]
        self.assertFalse(any(m.get("no_interruption_allowed") for m in first))   # the greeting stays interruptible
        second = [m for m in sent if m.get("response_id") == 2]
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["content"], self.s.intro_text)
        self.assertTrue(second[0]["no_interruption_allowed"])

    def test_noise_after_the_whole_opener_was_heard_gets_the_normal_ask_to_repeat(self):
        sent = self.run_socket([
            {"interaction_type": "call_details", "call": {"call_id": "c1", "metadata": {"lead": LEAD}}},
            {"interaction_type": "response_required", "response_id": 1, "transcript": [{"role": "user", "content": "Hello?"}]},
            {"interaction_type": "response_required", "response_id": 2,
             "transcript": [{"role": "user", "content": "Hello?"}, {"role": "agent", "content": self.s.opener + " Hi, I'm Riley with Ringback."},
                            {"role": "user", "content": "(unintelligible audio)"}]}])
        second = [m for m in sent if m.get("response_id") == 2]
        self.assertNotEqual(second[0]["content"], self.s.intro_text)
        self.assertIn(second[0]["content"], sum(live_pools(), []))


def live_pools():
    from receptionist import live
    return [live.NOT_HEARD["en"]]


class HandoffTests(unittest.TestCase):
    def test_crm_files_hold_calls_leads_and_meeting_tasks(self):
        data = {"calls": [{"n": 3, "business": "Case Lawn Care", "outcome": "Demo booked"}], "lead_updates": {"L01": {"stage": "pain"}},
                "bookings": [{"start": "2026-11-03T16:30:00-08:00", "owner": "Dale", "phone": "+12083043883", "business": "Case Lawn Care"}]}
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


class VoiceTests(unittest.TestCase):
    def test_caller_takes_the_receptionists_voice_but_keeps_its_own_behaviour(self):
        import receptionist.config as c
        cfg = c.load_config()
        voice = ol.receptionist_voice(cfg)
        shop = json.loads((Path(cfg["businesses_dir"]) / f"{cfg['live_businesses'][0]}.json").read_text())["voice"]
        for k in ("voice_id", "voice_model", "voice_speed", "voice_temperature"):
            self.assertEqual(voice[k], shop[k])
        self.assertEqual(voice["interruption_sensitivity"], 0.9)
        self.assertNotIn("post_call_analysis_model", voice)


class BudgetAndRedialTests(unittest.TestCase):
    def test_the_same_number_is_never_dialed_twice_in_one_day(self):
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 11, 9, 16, 12, tzinfo=timezone.utc)   # Monday 8:12 AM Pacific
        cfg = {"outbound": {"mode": "cold", "cold_enabled": True}}
        lead = {"name": "Highcraft", "phone": "208-920-0024"}
        self.assertEqual(ol.outbound.can_call(lead, now, cfg, [now - timedelta(minutes=1)]), (False, "already called today"))
        self.assertTrue(ol.outbound.can_call(lead, now, cfg, [now - timedelta(days=1)])[0])

    def test_daily_budget_stops_new_calls_and_counts_calls_in_progress(self):
        s = ol.outbound.settings({})
        from datetime import datetime, timezone
        now = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)
        calls = [{"date": "2026-11-09", "duration": 0.5}, {"date": "2026-11-09", "duration": 0.5},
                 {"date": "2026-11-09", "duration": 0.7}, {"date": "2026-11-08", "duration": 9}]
        attempts = {"+1": ["2026-11-09T16:11:00+00:00", "2026-11-09T16:12:00+00:00"], "+2": ["2026-11-09T16:13:00+00:00"]}
        spent = ol.outbound.spent_today(calls, attempts, now, s)
        self.assertAlmostEqual(spent, 0.5 * 0.17 * 2 + 0.7 * 0.17 + 0.09, places=3)
        in_progress = {**attempts, "+3": ["2026-11-09T19:59:00+00:00"]}
        self.assertAlmostEqual(ol.outbound.spent_today(calls, in_progress, now, s), spent + 0.45, places=3)

    def test_call_next_refuses_once_the_budget_is_used(self):
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path
        now = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            state = ol.State(Path(d) / "o.json")
            state.data["calls"] = [{"date": "2026-11-09", "duration": 6.5}]   # about $1.14
            r = ol.call_next({"outbound": {"mode": "cold", "cold_enabled": True}, "data_dir": d},
                             [{"name": "X", "phone": "208-555-0100"}], state, lambda *a: self.fail("dialed"), now)
            self.assertIsNone(r["called"])
            self.assertIn("daily budget", r["reason"])


class BlockRunnerTests(Fixture):
    def setUp(self):
        super().setUp()
        import os
        self.cfg = {**CFG, "data_dir": self.dir.name,
                    "outbound": {"mode": "warm-only", "block_runner": {"enabled": True, "calls": 3, "pause_s": 1, "max_call_wait_s": 10}}}
        d = Path(self.dir.name)
        (d / "retell_outbound.json").write_text(json.dumps({"number": "+12085550100", "agent_id": "ag"}))
        ol.save_leads(self.cfg, [{**LEAD, "id": f"L{i}", "consent": "yes", "phone": f"208-555-01{i:02d}"} for i in range(5)])
        self._env = dict(os.environ)
        os.environ["OUTBOUND_LIVE"] = "1"
        self.addCleanup(lambda: os.environ.clear() or os.environ.update(self._env))
        self.dials, self.polls = [], []

    def runner(self, status="ended"):
        def fn(method, path, body):
            if method == "POST":
                self.dials.append(body["to_number"])
                return {"call_id": f"c{len(self.dials)}"}
            self.polls.append(path)
            return {"call_status": status}
        return ol.BlockRunner(self.cfg, Path(self.dir.name) / "outbound.json", fn, clock=lambda: NOW, sleep=lambda s: None)

    def go(self, r):
        self.assertTrue(r.start())
        return r.run()

    def test_dials_the_block_size_one_at_a_time_and_never_the_same_number_twice(self):
        out = self.go(self.runner())
        self.assertEqual(len(self.dials), 3)
        self.assertEqual(len(set(self.dials)), 3)
        self.assertEqual(len(self.polls), 3)   # waited for each call to end before the next dial
        self.assertEqual(out["stopped_because"], "reached the block size")
        self.assertFalse(out["running"])

    def test_stops_when_a_call_cannot_be_confirmed_finished(self):
        out = self.go(self.runner(status="ongoing"))
        self.assertEqual(len(self.dials), 1)   # never dials over a call that may still be live
        self.assertIn("could not confirm", out["stopped_because"])

    def test_stops_when_no_lead_is_eligible(self):
        ol.save_leads(self.cfg, [])
        out = self.go(self.runner())
        self.assertEqual(self.dials, [])
        self.assertEqual(out["stopped_because"], "no leads on the list")

    def test_only_one_block_at_a_time(self):
        r = self.runner()
        self.assertTrue(r.start())
        self.assertFalse(r.start())
        r.run()
        self.assertTrue(r.start())   # released after the block ends
        r.run()

    def test_stop_request_ends_the_block_before_the_next_dial(self):
        r = self.runner()
        r.sleep = lambda s: r.stop()
        out = self.go(r)
        self.assertEqual(len(self.dials), 1)
        self.assertEqual(out["stopped_because"], "stopped by request")

    def test_off_by_default(self):
        self.assertFalse(outbound.settings({})["block_runner"]["enabled"])


class WarmTests(Fixture):
    class Client:
        def __init__(self):
            from types import SimpleNamespace
            self.requests = []
            self.messages = self
            self._ns = SimpleNamespace

        def create(self, **kw):
            self.requests.append(kw)
            return self._ns(usage=self._ns(input_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=1800))

    def setUp(self):
        super().setUp()
        ol._last_warm = None
        self.addCleanup(setattr, ol, "_last_warm", None)
        self.cfg = {**CFG, "data_dir": self.dir.name}

    def test_warm_sends_zero_tokens_and_the_exact_prefix_a_call_uses(self):
        c = self.Client()
        usage = ol.warm(c, self.cfg, NOW)
        self.assertEqual(usage["cache_write"], 1800)
        req = c.requests[0]
        self.assertEqual(req["max_tokens"], 0)
        self.assertEqual(json.dumps(req["system"][0], sort_keys=True), json.dumps(self.s.system[0], sort_keys=True))
        self.assertEqual(json.dumps(req["tools"], sort_keys=True), json.dumps(self.s.tools, sort_keys=True))
        self.assertEqual(req["model"], "b")

    def test_outbound_cache_is_five_minutes_and_priced_that_way(self):
        self.assertEqual(self.s.system[0]["cache_control"], {"type": "ephemeral"})
        from receptionist.agent import cache_write_mult
        self.assertEqual(cache_write_mult(self.s.cfg), 1.25)

    def test_a_second_warm_within_the_gap_is_skipped(self):
        c = self.Client()
        self.assertIsNotNone(ol.warm(c, self.cfg, NOW))
        self.assertIsNone(ol.warm(c, self.cfg, NOW))
        self.assertEqual(len(c.requests), 1)

    def test_place_call_warms_before_dialing(self):
        order = []
        rec = Path(self.dir.name) / "retell_outbound.json"
        rec.write_text(json.dumps({"agent_id": "agent_1", "number": "+12085550000"}))
        cfg = {**self.cfg, "outbound": {"mode": "warm-only"}}
        done = threading.Event()

        def warm_fn():
            order.append("warm")
            done.set()

        def call_fn(*a):
            done.wait(2)   # the warm thread starts first; the dial must come after it began
            order.append("dial")
            return {}
        ol.place_call(cfg, {**LEAD, "consent": "yes"}, NOW, self.state, call_fn, env={"OUTBOUND_LIVE": "1"}, warm_fn=warm_fn)
        self.assertEqual(order, ["warm", "dial"])


class ScriptedFixture(Fixture):
    """An outbound session whose model is a scripted fake client; the opener exchange has already happened."""

    def make(self, *script, cfg=None):
        from tests.test_logic import CFG as FULL, FakeClient
        self.client = FakeClient(*script)
        s = ol.OutboundSession(self.client, {**FULL, "data_dir": self.dir.name, "outbound": {"mode": "warm-only"}, **(cfg or {})}, LEAD, self.state, self.store, NOW, call_id="t2")
        s.respond("Hello?")   # the opener, no model call
        return s


class CallStateTests(ScriptedFixture):
    def test_no_signals(self):
        for t in ("I'm not interested in talking to an AI bot.", "No thanks", "We're good", "No, I'm good.", "no", "I'll pass"):
            self.assertTrue(ol.NO_SIGNAL.search(t), t)
        for t in ("what can I do for you?", "I need to pass it to my wife", "we get ten calls a week", "I don't know"):
            self.assertFalse(ol.NO_SIGNAL.search(t), t)

    def test_anger_is_a_code_level_stop(self):
        for t in ("quit bugging me", "I said no, stop calling", "piss off", "leave me alone"):
            self.assertTrue(ol.stop_request(t), t)
        self.assertFalse(ol.stop_request("not right now, I'm busy"))

    def test_the_playbook_teaches_no_counting_labels(self):
        text = outbound.PLAYBOOK.read_text().lower()
        for w in ("ladder", "ask 1", "ask 2", "ask 3", "first time", "second firm no", "first no", "soft no", "gentle",
                  "playful pushback", "where the conversation starts", "stubborn", "naive", "tuesday?"):
            self.assertNotIn(w, text)

    def test_speaking_says_every_word_is_spoken_aloud(self):
        self.assertIn("Never describe the owner", outbound.SPEAKING)


class AiRefusalTests(ScriptedFixture):
    HEARD = "I'm not interested in talking to an AI bot."

    def test_the_regex(self):
        for t in (self.HEARD, "I don't want to talk to a robot", "I hate robocalls", "I won't speak with a machine"):
            self.assertTrue(ol.AI_REFUSAL.search(t), t)
        for t in ("I'm interested in AI", "tell me about the AI", "I talk to customers all day"):
            self.assertFalse(ol.AI_REFUSAL.search(t), t)

    def test_statements_about_being_a_bot_count_as_the_ai_question(self):
        for t in ("this is a robot", "you're a bot", "You are an AI."):
            self.assertTrue(ol.asked_if_ai(t), t)

    def test_the_first_refusal_goes_to_the_model_with_an_honest_note_and_the_ladder(self):
        from tests.test_logic import reply, text
        s = self.make(reply("end_turn", text("Guilty, I'm an AI. But are you interested in money?")))
        s.respond(self.HEARD)
        note = json.dumps(self.client.requests[-1]["messages"][-1])
        self.assertIn("yes, you are one", note)
        self.assertIn("never claim to be human", note)
        self.assertIn("Noah himself calling them", note)
        self.assertEqual((s._nos, s._refusals), (1, ("no_trust_ai",)))


class DisclosureFlagTests(unittest.TestCase):
    def test_flag_off_keeps_todays_intro_note_and_fallback(self):
        s = outbound.settings({"outbound": {"disclose_ai_in_intro": False}})
        self.assertEqual(ol.intro_note(s), ol.INTRO_NOTE)
        self.assertIn(ol.INTRO[0].format(agent="Riley", company="Ringback", town="Sandpoint"), ["Hi, I'm Riley with Ringback here in Sandpoint."])

    def test_flag_on_says_an_ai_assistant_and_keeps_the_honesty_rules(self):
        s = outbound.settings({})
        self.assertTrue(s["disclose_ai_in_intro"])
        self.assertIn("an AI assistant calling for Noah at Ringback in Sandpoint", ol.intro_note(s))
        for flag in (True, False):
            text = outbound.build_prompt({}, NOW, {"outbound": {"disclose_ai_in_intro": flag}})[0]["text"]
            self.assertIn("never claim to be human", text)


class StallPoolTests(ScriptedFixture):
    def test_a_slow_outbound_reply_plays_a_plain_acknowledgement_and_logs_a_warning(self):
        import asyncio
        from receptionist import live
        from tests.test_logic import reply, text
        sess = self.make(reply("end_turn", text("Hi, Riley with Ringback.")))
        real = self.client.stream

        def slow(**kw):
            time.sleep(0.5)
            return real(**kw)
        self.client.stream = slow
        sent = []

        async def run():
            inbox = asyncio.Queue()
            await inbox.put({"interaction_type": "call_details", "call": {"call_id": "c1"}})
            await inbox.put({"interaction_type": "response_required", "response_id": 1, "transcript": [
                {"role": "agent", "content": "Hi, is this Dale?"}, {"role": "user", "content": "Why are you calling?"}]})

            async def send(m):
                sent.append(m)
                if m.get("response_id") == 1 and m.get("content_complete"):
                    raise asyncio.CancelledError

            task = asyncio.create_task(live.run_retell_call(inbox.get, send, lambda call: sess, lambda s: None))
            try:
                await asyncio.wait_for(task, 10)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        original = live.SLOW_REPLY_S
        live.SLOW_REPLY_S = 0.1
        try:
            with self.assertLogs("receptionist", level="WARNING") as logged:
                asyncio.run(run())
        finally:
            live.SLOW_REPLY_S = original
        first = next(m["content"] for m in sent if m.get("response_id") == 1 and m.get("content"))
        self.assertIn(first.strip(), live.OUTBOUND_STILL_WORKING["en"])
        self.assertTrue(any("call t2" in l and "stall line" in l for l in logged.output))


class MachineTests(ScriptedFixture):
    """Replays of real calls from 2026-11-09 that reached a machine instead of a person."""
    ASPEN = ["You have reached the office for ask Landscaping. Small Lizards Lawn Care, and Dump and Run Hauling. To reach Aspen Ridge "
             "Landscaping, enter extension zero pound. To reach lawn mowers lawn care, enter extension one pound",
             "To reach dump and run calling, enter extension two. Pound. Thank you for calling.",
             "You have reached the offices for Aspen Ridge Landscaping Long Lizards lawn care, and Gump and Run hauling. To reach Aspen Ridge "
             "landscaping, enter extension zero pound."]
    THOMPSON = ["Thank you for calling Thompson Turf Services. Please stay on the line, or press zero to be transferred to the receptionist.",
                "Please hold while I try to connect you."]
    VOICEMAILS = ["Hi. This is Taylor Marsh with TCL Concrete Excavation. Leave your name and number, and I'll get back to you as soon as I can. Thanks.",
                  "Thank you for calling. Please leave a message or call two zero",
                  "All right, leave your message after the beep.",
                  "The person you are trying to reach is not available. At the tone, please record your message."]

    def fresh(self, *script):
        from tests.test_logic import CFG as FULL, FakeClient
        self.client = FakeClient(*script)
        return ol.OutboundSession(self.client, {**FULL, "data_dir": self.dir.name, "outbound": {"mode": "warm-only"}}, LEAD,
                                  self.state, self.store, NOW, call_id="m1")

    def test_kinds(self):
        for t in self.VOICEMAILS:
            self.assertEqual(ol.machine_kind(t), "voicemail", t)
        for t in self.ASPEN + self.THOMPSON[:1]:
            self.assertEqual(ol.machine_kind(t), "menu", t)
        self.assertEqual(ol.machine_kind(self.THOMPSON[1]), "hold")
        for t in ("Hello?", "This is On Track.", "Yeah, this is Dale.", "What can I do for you?", "Aspen Ridge, this is Mike."):
            self.assertIsNone(ol.machine_kind(t), t)

    def test_voicemail_and_menus_are_hung_up_on_in_silence_with_no_model_call(self):
        for greeting in self.VOICEMAILS + self.ASPEN + self.THOMPSON[:1]:
            s, said = self.fresh(), []
            turn = s.respond(greeting, said.append)
            self.assertEqual(said, [])
            self.assertEqual(turn.text, "")
            self.assertTrue(turn.ended and s.ended)
            self.assertEqual(s.ctx.extra["log"]["outcome"], "Voicemail")
            self.assertEqual(self.client.requests, [])

    def test_the_person_after_a_hold_message_gets_the_opener(self):
        s, said = self.fresh(), []
        for t in self.THOMPSON[1:]:
            s.respond(t, said.append)
        self.assertEqual(said, [])
        turn = s.respond("Thompson Turf, this is Jim.", said.append)
        self.assertEqual(turn.text, s.hook_text)
        self.assertEqual(self.client.requests, [])

    def test_after_a_person_has_talked_voicemail_words_are_ordinary_talk(self):
        from tests.test_logic import reply, text
        s = self.fresh(reply("end_turn", text("Five a week, huh?")))
        s.respond("Hello?")
        s.respond("Yeah, what's up?")   # code says the intro and the first question
        turn = s.respond("We get maybe five voicemails a week, people leave a message and I call back.")
        self.assertFalse(turn.ended)
        self.assertEqual(len(self.client.requests), 1)

    def test_machine_narration_is_never_spoken(self):
        from receptionist.speech import unspeakable
        for t in ("Sounds like a phone menu, so I'll hang on for the Aspen Ridge extension.",
                  "This is an automated menu, not a person, so I'll log it and hang up.",
                  "Oh, wait, this isn't actually a person.", "Oh wait, it's a recording."):
            self.assertTrue(unspeakable(t), t)
        for t in ("Yep, I'm an AI, not a person.", "Got it, so I'll put you down for Tuesday.", "Oh nice, that's great."):
            self.assertFalse(unspeakable(t), t)

    def test_the_prompt_says_to_stay_silent_for_recordings(self):
        self.assertIn("write (listening) with no tags", outbound.SPEAKING)
