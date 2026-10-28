"""Recording notice and AI disclosure, retention and purge (spec item C), calendar reading and blocked time (item D),
and the fail-closed server settings."""
import json
import logging
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from receptionist import calendar_busy, onboarding, prompts, schedule
from receptionist.config import load_business, load_config
from receptionist.server import create_app
from receptionist.store import Store
from tests.test_access import ADMIN, SECRET, Env

CFG = load_config()
HVAC = load_business(CFG, "hvac")


def tz_now(biz, at="2026-11-03T05:00"):      # a Tuesday morning
    return schedule.local_now(biz, at)


def ics(*events: str) -> bytes:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n" + "".join(events) + "END:VCALENDAR\r\n").encode()


def event(uid, start, end, extra="", summary="Job"):
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20261001T000000Z\r\nDTSTART:{start}\r\nDTEND:{end}\r\n"
            f"SUMMARY:{summary}\r\n{extra}END:VEVENT\r\n")


URL = "https://calendar.example.test/private/abc123secret/basic.ics"


def shop(**over):
    return {**HVAC, "calendars": [{"type": "ical", "url": URL, "label": "Shop calendar"}], **over}


class Greeting(unittest.TestCase):
    def test_every_variant_carries_the_notice_and_the_disclosure(self):
        biz = {**HVAC, "recording_notice": "This call is recorded.", "ai_disclosure_in_greeting": True}
        for g in biz["greetings"]:
            said = prompts.announce(biz, g.format(daypart="morning"))
            self.assertIn("This call is recorded.", said)
            self.assertIn("virtual assistant", said)

    def test_it_is_not_added_twice_and_the_demo_greeting_is_unchanged(self):
        said = prompts.announce({"vertical": "hvac"}, "Hi, this is Sam, the shop's virtual assistant. This call is recorded.")
        self.assertEqual(said.count("virtual assistant"), 1)
        self.assertEqual(said.count("recorded"), 1)
        import random
        self.assertNotIn("recorded", prompts.pick_greeting(HVAC, tz_now(HVAC), random.Random(1)))     # the live demo line keeps its choice

    def test_new_clients_start_with_both_on_and_cannot_turn_both_off_silently(self):
        intake = __import__("yaml").safe_load((Path(CFG["businesses_dir"]).parent / ".internal" / "intake.example.yaml").read_text())
        biz = onboarding.build(intake, HVAC)
        self.assertTrue(biz["recording_notice"] and biz["ai_disclosure_in_greeting"])
        self.assertEqual(biz["retention_days"], 90)
        onboarding.check(biz)
        off = onboarding.build({**intake, "recording_notice": "", "ai_disclosure_in_greeting": False}, HVAC)
        with self.assertRaises(onboarding.IntakeError):
            onboarding.check(off)
        onboarding.check({**off, "notice_waived": True})

    def test_a_bad_calendar_link_is_refused_at_intake(self):
        intake = __import__("yaml").safe_load((Path(CFG["businesses_dir"]).parent / ".internal" / "intake.example.yaml").read_text())
        with self.assertRaises(onboarding.IntakeError):
            onboarding.build({**intake, "calendar_url": "http://insecure.example/x.ics"}, HVAC)
        ok = onboarding.build({**intake, "calendar_url": "webcal://cal.example/x.ics"}, HVAC)
        self.assertEqual(len(ok["calendars"]), 1)


class Retention(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.now = tz_now(HVAC)
        for cid, age in (("old", 100), ("new", 5)):
            self.store.start_call(cid, "hvac", "retell", "+12085550111", self.now - timedelta(days=age))
            self.store.end_call(cid, [{"role": "caller", "text": "hi"}], "booked", self.now)
            self.store.log_tool(cid, "x", {}, {}, self.now)

    def test_only_calls_past_the_retention_period_go(self):
        self.assertEqual(self.store.purge_calls_before("hvac", self.now - timedelta(days=90)), 1)
        self.assertIsNone(self.store.get_call("old"))
        self.assertEqual(self.store.tool_events("old"), [])
        self.assertIsNotNone(self.store.get_call("new"))

    def test_purge_a_shop_leaves_nothing_and_dry_run_deletes_nothing(self):
        self.store.add_message("hvac", "new", "Ann", "+12085550111", "call me", "owner", False, self.now)
        self.store.add_message("acme", "x", "Bob", "+12085550999", "other shop", "owner", False, self.now)
        self.store.add_block("hvac", self.now, self.now + timedelta(hours=1), "x", self.now)
        self.assertEqual(self.store.purge("hvac", dry_run=True)["calls"], 2)
        self.assertIsNotNone(self.store.get_call("old"))
        counts = self.store.purge("hvac")
        self.assertEqual((counts["calls"], counts["messages"], counts["blocks"]), (2, 1, 1))
        self.assertEqual(self.store.recent("calls", "hvac"), [])
        self.assertEqual(self.store.recent("messages", "hvac"), [])
        self.assertEqual(len(self.store.recent("messages", "acme")), 1)      # another shop is untouched

    def test_purge_one_caller_only(self):
        self.store.start_call("other", "hvac", "retell", "+12085550222", self.now)
        counts = self.store.purge("hvac", "208-555-0222")
        self.assertEqual(counts["calls"], 1)
        self.assertIsNotNone(self.store.get_call("old"))
        with self.assertRaises(ValueError):
            self.store.purge("hvac", "12")


class Calendars(unittest.TestCase):
    def setUp(self):
        calendar_busy._cache.clear()
        self.now = tz_now(HVAC)
        self.store = Store(":memory:")

    def counts(self, biz, getter, mono=0.0):
        return calendar_busy.booked_counts(biz, self.store, self.now, getter, mono)

    def wid(self, hhmm, day="2026-11-03"):
        return f"{day}T{hhmm}"

    def test_an_overlapping_event_uses_one_technician(self):
        cal = ics(event("a", "20261103T170000Z", "20261103T180000Z"))      # 9:00-10:00 Pacific
        counts, problems = self.counts(shop(), lambda u: cal)
        wid = schedule.window_id(self.now.date(), "09:00")
        self.assertEqual(counts[wid], 1)
        self.assertEqual(problems, [])
        self.assertNotIn(schedule.window_id(self.now.date(), "11:00"), counts)
        win = schedule.open_windows(shop(), self.now, counts, limit=100)
        self.assertIn(wid, {w["window_id"] for w in win})               # two technicians: one still free

    def test_two_events_block_two_technicians(self):
        cal = ics(event("a", "20261103T170000Z", "20261103T180000Z"), event("b", "20261103T173000Z", "20261103T183000Z"))
        counts, _ = self.counts(shop(), lambda u: cal)
        wid = schedule.window_id(self.now.date(), "09:00")
        self.assertEqual(counts[wid], 2)
        self.assertNotIn(wid, {w["window_id"] for w in schedule.open_windows(shop(), self.now, counts, limit=100)})

    def test_a_weekly_repeating_event(self):
        cal = ics(event("w", "20261027T170000Z", "20261027T180000Z", "RRULE:FREQ=WEEKLY;COUNT=3\r\n"))
        counts, _ = self.counts(shop(), lambda u: cal)
        self.assertEqual(counts[schedule.window_id(self.now.date(), "09:00")], 1)             # Nov 3 is the second occurrence
        self.assertEqual(counts[schedule.window_id(self.now.date() + timedelta(days=7), "09:00")], 1)
        self.assertNotIn(schedule.window_id(self.now.date() + timedelta(days=14), "09:00"), counts)   # only 3 times

    def test_cancelled_and_all_day_events_are_ignored_unless_off(self):
        cal = ics(event("c", "20261103T170000Z", "20261103T180000Z", "STATUS:CANCELLED\r\n"),
                  event("d", "20261104", "20261105", summary="Dentist"),
                  event("o", "20261105", "20261106", summary="Vacation"))
        counts, _ = self.counts(shop(), lambda u: cal)
        self.assertNotIn(schedule.window_id(self.now.date(), "09:00"), counts)
        self.assertNotIn(schedule.window_id(self.now.date() + timedelta(days=1), "09:00"), counts)
        self.assertEqual(counts[schedule.window_id(self.now.date() + timedelta(days=2), "09:00")], 1)

    def test_an_unreachable_calendar_falls_back_and_says_so(self):
        def down(url):
            raise calendar_busy.FetchError("boom")
        counts, problems = self.counts(shop(), down)
        self.assertEqual(counts, {})
        self.assertEqual(problems[0]["label"], "Shop calendar")
        self.assertIn("our own bookings only", calendar_busy.problem_line(problems))
        self.assertNotIn("abc123secret", calendar_busy.problem_line(problems))

    def test_a_calendar_read_a_while_ago_is_used_until_it_is_stale(self):
        cal = ics(event("a", "20261103T170000Z", "20261103T180000Z"))
        self.counts(shop(), lambda u: cal, mono=0.0)

        def down(url):
            raise calendar_busy.FetchError("boom")
        wid = schedule.window_id(self.now.date(), "09:00")
        counts, problems = self.counts(shop(), down, mono=600.0)       # cache expired, fetch fails, 10 minutes old
        self.assertEqual((counts[wid], problems), (1, []))
        counts, problems = self.counts(shop(), down, mono=2000.0)      # past 30 minutes: stop trusting it
        self.assertNotIn(wid, counts)
        self.assertEqual(len(problems), 1)

    def test_blocked_time_takes_the_whole_shop(self):
        self.store.add_block("hvac", datetime(2026, 11, 3, 9, 30, tzinfo=self.now.tzinfo),
                             datetime(2026, 11, 3, 10, 30, tzinfo=self.now.tzinfo), "dentist", self.now)
        counts, _ = self.counts(HVAC, None)
        wid = schedule.window_id(self.now.date(), "09:00")
        self.assertEqual(counts[wid], 2)
        self.assertNotIn(wid, {w["window_id"] for w in schedule.open_windows(HVAC, self.now, counts, limit=100)})

    def test_the_agent_is_told_when_the_calendar_could_not_be_read(self):
        from receptionist.tools import ToolContext, run_tool
        biz = shop()
        ctx = ToolContext(biz, self.store, self.now, "+12085550111", "c1")
        with mock.patch.object(calendar_busy, "fetch", side_effect=calendar_busy.FetchError("x")):
            result, _ = run_tool(ctx, "check_availability", {"service_id": biz["services"][0]["id"]})
        self.assertIn("never say you checked", result["calendar_note"].lower())
        self.assertTrue(result["windows"])                                # still offers our own slots

    def test_private_and_non_https_addresses_are_never_fetched(self):
        for url in ("http://calendar.example/x.ics", "https://127.0.0.1/x.ics", "https://localhost/x", "file:///etc/passwd",
                    "https://169.254.169.254/latest/meta-data"):
            with self.assertRaises(calendar_busy.FetchError, msg=url):
                calendar_busy.fetch(url)

    def test_the_link_never_reaches_the_log_or_the_dashboard(self):
        def down(url):
            raise calendar_busy.FetchError(url)       # even an error that carries the link must not be logged
        with self.assertLogs("receptionist", level="WARNING") as logs:
            self.counts(shop(), down)
        self.assertNotIn("abc123secret", "\n".join(logs.output))


class Dashboard(Env, unittest.TestCase):
    def setUp(self):
        self.start()
        self.client = TestClient(self.http.app)
        self.client.get("/dashboard", params={"token": self.key("north"), "business": "north"}, follow_redirects=True)

    def tearDown(self):
        self.stop()

    def post(self, path, data, client=None, **kw):
        return (client or self.client).post(path, data=data, follow_redirects=False, **kw)

    def test_the_owner_can_block_and_remove_time(self):
        r = self.post("/dashboard/block", {"business": "north", "date": "2026-11-05", "start": "13:00", "end": "15:00", "note": "<b>dentist</b>"})
        self.assertEqual(r.status_code, 303)
        page = self.client.get("/dashboard?business=north").text
        self.assertIn("&lt;b&gt;dentist&lt;/b&gt;", page)          # escaped
        block = self.http.app  # noqa: F841
        from receptionist.store import Store
        store = Store(str(Path(self.cfg["data_dir"]) / "receptionist.sqlite"))
        (b,) = store.blocks("north", datetime(2026, 1, 1, tzinfo=schedule.tz(HVAC)))
        self.assertEqual(self.post("/dashboard/block/delete", {"business": "north", "id": b["id"]}).status_code, 303)
        self.assertEqual(store.blocks("north", datetime(2026, 1, 1, tzinfo=schedule.tz(HVAC))), [])

    def test_the_form_needs_the_shops_own_cookie_and_our_own_page(self):
        good = {"business": "north", "date": "2026-11-05", "start": "13:00", "end": "15:00"}
        self.assertEqual(self.post("/dashboard/block", {**good, "business": "south"}).status_code, 404)    # north's cookie, south's shop
        self.assertEqual(self.post("/dashboard/block", good, client=TestClient(self.http.app)).status_code, 404)   # no cookie
        self.assertEqual(self.post("/dashboard/block", good, headers={"Origin": "https://evil.example"}).status_code, 404)
        self.assertEqual(self.post("/dashboard/block", {**good, "end": "12:00"}).status_code, 422)
        self.assertEqual(self.post("/dashboard/block", {**good, "date": "nope"}).status_code, 422)

    def test_dashboard_has_a_locked_down_content_policy_and_no_open_docs(self):
        r = self.client.get("/dashboard?business=north")
        csp = r.headers["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("script-src 'unsafe-inline'", csp)
        self.assertEqual(r.headers["x-frame-options"], "DENY")
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_health_names_no_shops(self):
        self.assertEqual(self.client.get("/health").json(), {"ok": True})

    def test_short_secrets_are_ignored(self):
        self.stop()
        self.start(DASHBOARD_TOKEN="short", TOKEN_SECRET="short")
        self.assertEqual(self.http.get("/dashboard", params={"token": "short", "business": "north"}, follow_redirects=False).status_code, 404)

    def test_oversize_uploads_are_refused(self):
        r = self.http.put("/admin/clients", content=b"x" * 1_200_000, headers={"Authorization": f"Bearer {ADMIN}"})
        self.assertEqual(r.status_code, 413)


class FailClosed(unittest.TestCase):
    def test_platform_routes_refuse_unsigned_requests_unless_dev_mode(self):
        class Fake: pass
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"ALLOW_UNVERIFIED_WEBHOOKS": "0"}):
            for k in ("VAPI_SERVER_TOKEN", "RETELL_API_KEY"):
                os.environ.pop(k, None)
            c = TestClient(create_app({**CFG, "data_dir": d, "clients_dir": d + "/c"}, client=Fake(), warm_cache=False))
            self.assertEqual(c.post("/retell/inbound", json={}).status_code, 401)
            self.assertEqual(c.post("/retell/tool/x", json={}).status_code, 401)
            self.assertEqual(c.post("/retell/webhook", json={}).status_code, 401)
            self.assertEqual(c.post("/vapi/webhook", json={"message": {}}).status_code, 401)
