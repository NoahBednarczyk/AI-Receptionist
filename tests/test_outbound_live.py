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
        s = ol.outbound.settings(CFG)
        for seed in range(6):
            intro = ol.opener_parts({"name": "X"}, s, random.Random(seed))[1]
            self.assertIn("Ringback", intro)
            self.assertNotRegex(intro, r"(?i)sales|cold|\bai\b|robot")

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


class LawnCareOnlyTests(unittest.TestCase):
    """The pitch is for lawn care and snow removal businesses. Nothing the caller says, or leaves on a voicemail, may
    mention the old heating and plumbing niche."""
    BAD = r"(?i)plumb|heating|furnace|hvac|water heater"

    def test_pitch_hooks_voicemail_and_keywords_are_lawn_care_only(self):
        import re
        s = outbound.settings(CFG)
        texts = [outbound.facts(s), outbound.PLAYBOOK.read_text(), json.dumps(ol.GREETINGS + ol.INTRO)]
        payload = ol.agent_payload(CFG, "https://x.io", "S")
        texts += [payload["voicemail_option"]["action"]["text"], " ".join(payload["boosted_keywords"])]
        for text in texts:
            self.assertIsNone(re.search(self.BAD, text), text[:80])

    def test_trades_pick_the_right_hooks(self):
        self.assertEqual(ol.trade_of({"type": "Lawn care"}), "lawn")
        self.assertEqual(ol.trade_of({"type": "Landscaping"}), "lawn")
        self.assertEqual(ol.trade_of({"type": "Lawn & Snow"}), "lawn")
        self.assertEqual(ol.trade_of({"type": "Snow removal"}), "snow")
        self.assertEqual(ol.trade_of({"type": "Other"}), "default")
        self.assertEqual(ol.trade_of({}), "default")


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
        self.assertIn("do not bring up being an AI unless they ask", notes[0])
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
