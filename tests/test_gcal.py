"""Google Calendar sync: the service-account login, reading events into availability, and pushing bookings."""
import base64
import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import tests  # noqa: F401  (runs tests/__init__.py: the older tests use the <say> channel's rollback setting)
from receptionist import calendar_busy, dashboard, gcal, onboarding, schedule, tools
from receptionist.config import load_business, load_config
from receptionist.store import Store

SHOP = load_business(load_config(), "lawn_care")
CAL = "owner@example.test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
CREDS = {"client_email": "bot@proj.iam.gserviceaccount.test", "private_key": PEM}
NOW = schedule.local_now(SHOP, "2026-11-03T05:00")   # a Tuesday morning


def shop(**cal):
    return {**SHOP, "calendars": [{"type": "google", "calendar_id": CAL, "label": "Shop", **cal}]}


class FakeGoogle:
    """Answers the token request and the few Calendar calls we make, and remembers every request."""

    def __init__(self, events=None, fail=None):
        self.sent, self.events, self.fail, self.stored = [], events or [], fail or {}, {}

    def __call__(self, method, url, headers, body):
        self.sent.append((method, url, headers, body))
        if url.startswith(gcal.TOKEN_URL):
            return 200, json.dumps({"access_token": "tok", "expires_in": 3600}).encode()
        key = method + " " + url.split("/calendars/")[1].split("?")[0].split("/", 1)[-1].split("/")[0]
        if method in self.fail:
            return self.fail[method], b'{"error": {"message": "nope"}}'
        if method == "GET":
            return 200, json.dumps({"items": self.events, "accessRole": "writer"}).encode()
        data = json.loads(body) if body else {}
        if method == "POST":
            if data["id"] in self.stored:
                return 409, b'{"error": {"message": "exists"}}'
            self.stored[data["id"]] = data
            return 200, json.dumps(data).encode()
        eid = url.split("/events/")[1]
        if method == "PATCH":
            self.stored[eid] = {**self.stored.get(eid, {}), **data}
            return 200, b"{}"
        if method == "DELETE":
            if eid not in self.stored:
                return 404, b"{}"
            del self.stored[eid]
            return 204, b""
        return 500, b""


def client(fake):
    return gcal.Client(CREDS, transport=fake)


def ev(start, end, summary="Meeting", **extra):
    return {"summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end}, **extra}


class Login(unittest.TestCase):
    def test_the_token_request_carries_a_valid_rs256_signature(self):
        fake = FakeGoogle()
        self.assertEqual(client(fake).token(), "tok")
        method, url, _, body = fake.sent[0]
        form = dict(p.split("=") for p in body.decode().split("&"))
        head, claims, sig = form["assertion"].split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(head)))["alg"], "RS256")
        c = json.loads(base64.urlsafe_b64decode(pad(claims)))
        self.assertEqual((c["iss"], c["aud"], c["scope"]), (CREDS["client_email"], gcal.TOKEN_URL, gcal.SCOPE))
        KEY.public_key().verify(base64.urlsafe_b64decode(pad(sig)), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())

    def test_the_token_is_reused_until_it_nearly_expires(self):
        fake, t = FakeGoogle(), [1000.0]
        c = gcal.Client(CREDS, transport=fake, clock=lambda: t[0])
        c.token(); c.token()
        self.assertEqual(len(fake.sent), 1)
        t[0] += 3560
        c.token()
        self.assertEqual(len(fake.sent), 2)

    def test_a_key_file_missing_its_private_key_is_refused(self):
        with self.assertRaises(gcal.GcalError):
            gcal.Client({"client_email": "x@y"})

    def test_no_environment_key_is_a_clear_error_and_leaks_nothing(self):
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(gcal.GcalError) as cm:
            gcal.default_client()
        self.assertNotIn("PRIVATE", str(cm.exception))


class Reading(unittest.TestCase):
    def setUp(self):
        calendar_busy._cache.clear()

    def test_events_block_windows_and_ours_are_not_counted_twice(self):
        events = [ev("2026-11-04T09:00:00-08:00", "2026-11-04T11:00:00-08:00"),
                  ev("2026-11-04T13:00:00-08:00", "2026-11-04T15:00:00-08:00", extendedProperties={"private": {"receptionist": "1"}}),
                  ev("2026-11-04T07:00:00-08:00", "2026-11-04T09:00:00-08:00", transparency="transparent"),
                  ev("2026-11-04T11:00:00-08:00", "2026-11-04T13:00:00-08:00", status="cancelled")]
        got = gcal.list_events(CAL, NOW, NOW + timedelta(days=2), client(FakeGoogle(events)))
        self.assertEqual([e["start"].hour for e in got], [9])

    def test_a_busy_calendar_removes_the_window_and_two_technicians_need_two_events(self):
        biz = shop()
        events = [ev("2026-11-04T09:00:00-08:00", "2026-11-04T11:00:00-08:00")]
        counts, problems = calendar_busy.booked_counts(biz, Store(":memory:"), NOW, mono=1.0,
                                                       events_getter=lambda cid, a, b: gcal.list_events(cid, a, b, client(FakeGoogle(events))))
        self.assertEqual((counts.get("2026-11-04T09:00"), problems), (1, []))   # 1 of 2 technicians busy: still open
        wins = schedule.open_windows(biz, NOW, counts, limit=500)
        self.assertIn("2026-11-04T09:00", {w["window_id"] for w in wins})

    def test_all_day_events_count_only_when_the_title_says_off(self):
        biz = shop()
        a = [{"start": datetime(2026, 11, 4).date(), "end": datetime(2026, 11, 5).date(), "summary": "Vacation"},
             {"start": datetime(2026, 11, 5).date(), "end": datetime(2026, 11, 6).date(), "summary": "Birthday"}]
        busy, _ = calendar_busy.busy_intervals(biz, NOW, NOW + timedelta(days=3), mono=2.0, events_getter=lambda *x: a)
        self.assertEqual(len(busy), 1)
        self.assertEqual(busy[0][0].day, 4)

    def test_an_unreachable_google_calendar_is_reported_and_windows_fall_back(self):
        biz = shop(label="Shop cal")

        def boom(*a):
            raise gcal.GcalError("Google Calendar HTTP 403")
        counts, problems = calendar_busy.booked_counts(biz, Store(":memory:"), NOW, mono=3.0, events_getter=boom)
        self.assertEqual(problems[0]["label"], "Shop cal")
        self.assertIn("Shop cal", calendar_busy.problem_line(problems))

    def test_the_read_is_cached_for_a_minute(self):
        biz = {**shop(calendar_id="cached@example.test")}
        calls = []

        def getter(cid, a, b):
            calls.append(cid)
            return []
        calendar_busy.busy_intervals(biz, NOW, NOW + timedelta(days=2), mono=10.0, events_getter=getter)
        calendar_busy.busy_intervals(biz, NOW, NOW + timedelta(days=2), mono=40.0, events_getter=getter)
        calendar_busy.busy_intervals(biz, NOW, NOW + timedelta(days=2), mono=80.0, events_getter=getter)
        self.assertEqual(len(calls), 2)


class Writing(unittest.TestCase):
    def appt(self, **over):
        return {"confirmation": "A1002", "window_id": "2026-11-04T09:00", "service_id": "lawn_mowing", "name": "Dana Whitfield",
                "phone": "+12085550142", "address": "412 Larch Ave", "town": "Sandpoint", "problem": "No heat",
                "access_notes": "Gate 4471", "status": "booked", **over}

    def test_a_booking_becomes_an_event_with_a_stable_id_and_our_mark(self):
        fake = FakeGoogle()
        gcal.push(shop(), self.appt(), client(fake))
        data = fake.stored["rcpta1002"]
        self.assertEqual(data["start"]["dateTime"], "2026-11-04T09:00:00-08:00")
        self.assertEqual(data["end"]["dateTime"], "2026-11-04T11:00:00-08:00")
        self.assertEqual(data["location"], "412 Larch Ave, Sandpoint")
        self.assertEqual(data["extendedProperties"]["private"]["receptionist"], "1")
        self.assertIn("Gate 4471", data["description"])

    def test_pushing_twice_updates_and_never_duplicates(self):
        fake = FakeGoogle()
        gcal.push(shop(), self.appt(), client(fake))
        gcal.push(shop(), self.appt(window_id="2026-11-04T13:00"), client(fake))
        self.assertEqual(len(fake.stored), 1)
        self.assertEqual(fake.stored["rcpta1002"]["start"]["dateTime"], "2026-11-04T13:00:00-08:00")

    def test_a_cancellation_deletes_and_a_missing_event_is_fine(self):
        fake = FakeGoogle()
        gcal.push(shop(), self.appt(), client(fake))
        gcal.push(shop(), self.appt(status="cancelled"), client(fake))
        self.assertEqual(fake.stored, {})
        gcal.push(shop(), self.appt(status="cancelled"), client(fake))   # already gone: no error

    def test_a_refused_write_raises_with_the_calendar_name(self):
        with self.assertRaises(gcal.GcalError) as cm:
            gcal.push(shop(), self.appt(), client(FakeGoogle(fail={"POST": 403})))
        self.assertIn("Shop", str(cm.exception))

    def test_read_only_calendars_are_never_written(self):
        fake = FakeGoogle()
        gcal.push(shop(write=False), self.appt(), client(fake))
        self.assertEqual(fake.sent, [])


class InTheCall(unittest.TestCase):
    """The booking tools push to Google without waiting, and a failure never reaches the caller."""

    def setUp(self):
        calendar_busy._cache.clear()
        self.store = Store(":memory:")
        self.fake = FakeGoogle()
        self.pushed = []
        patcher = mock.patch.object(gcal, "submit", lambda fn, biz, store, appt, c: fn(biz, store, appt, client(self.fake)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def ctx(self, biz):
        return tools.ToolContext(biz=biz, store=self.store, now=NOW, caller="+12085550142", call_id="c1")

    def book(self, ctx):
        wid = schedule.open_windows(ctx.biz, NOW, calendar_busy.booked_counts(ctx.biz, self.store, NOW, events_getter=lambda *a: [])[0])[0]["window_id"]
        return tools.book_appointment(ctx, {"window_id": wid, "service_id": "lawn_mowing", "customer_name": "Dana Whitfield",
                                            "callback_number": "2085550142", "street_address": "412 Larch Ave", "town": "Sandpoint",
                                            "problem_summary": "No heat"})

    def test_booking_moving_and_cancelling_reach_the_calendar(self):
        biz = shop(calendar_id="flow@example.test")
        with mock.patch.object(calendar_busy, "gcal", gcal), mock.patch.object(gcal, "list_events", lambda *a, **k: []):
            ctx = self.ctx(biz)
            made = self.book(ctx)
            eid = gcal.event_id(made["confirmation"])
            self.assertIn(eid, self.fake.stored)
            self.assertEqual(self.store.get_sync("lawn_care", made["confirmation"])["status"], "ok")
            new = [w for w in schedule.open_windows(biz, NOW, calendar_busy.booked_counts(biz, self.store, NOW)[0], limit=20)
                   if w["window_id"] != self.store.get_appointment("lawn_care", made["confirmation"])["window_id"]][0]["window_id"]
            tools.reschedule_appointment(ctx, {"confirmation": made["confirmation"], "new_window_id": new})
            self.assertEqual(self.fake.stored[eid]["start"]["dateTime"][:13], new[:13])
            tools.cancel_appointment(ctx, {"confirmation": made["confirmation"], "reason": "changed mind"})
            self.assertNotIn(eid, self.fake.stored)

    def test_a_google_outage_leaves_the_booking_intact_and_shows_on_the_dashboard(self):
        biz = shop(calendar_id="down@example.test")
        self.fake.fail = {"POST": 403}
        with mock.patch.object(gcal, "list_events", lambda *a, **k: []):
            made = self.book(self.ctx(biz))
        self.assertEqual(made["status"], "booked")
        self.assertEqual(self.store.get_appointment("lawn_care", made["confirmation"])["status"], "booked")
        self.assertEqual(self.store.get_sync("lawn_care", made["confirmation"])["status"], "failed")
        html = dashboard.render(biz, self.store)
        self.assertIn("did not reach", html)
        self.assertIn(made["confirmation"], html)

    def test_shops_without_google_never_touch_it(self):
        with mock.patch.object(gcal, "default_client", side_effect=AssertionError("must not be called")):
            made = self.book(self.ctx(SHOP))
        self.assertEqual(made["status"], "booked")
        self.assertEqual(self.fake.sent, [])

    def test_resync_sends_everything_again_without_doubling(self):
        biz = shop(calendar_id="resync@example.test")
        with mock.patch.object(gcal, "list_events", lambda *a, **k: []):
            self.book(self.ctx(biz))
        self.fake.stored.clear()
        out = gcal.resync(biz, self.store, NOW, client(self.fake))
        self.assertEqual(out, {"ok": 1, "failed": 0})
        self.assertEqual(len(self.fake.stored), 1)


class Intake(unittest.TestCase):
    def test_the_intake_accepts_a_google_calendar_id_and_refuses_junk(self):
        import yaml
        from pathlib import Path
        intake = yaml.safe_load((Path(load_config()["businesses_dir"]).parent / ".internal" / "intake.example.yaml").read_text())
        ok = onboarding.build({**intake, "google_calendar_id": "owner@gmail.com"}, SHOP)
        self.assertEqual(ok["calendars"][-1]["type"], "google")
        with self.assertRaises(onboarding.IntakeError):
            onboarding.build({**intake, "google_calendar_id": "not an id"}, SHOP)

    def test_check_names_the_account_to_share_with(self):
        fake = FakeGoogle(fail={"GET": 404})
        line = gcal.check(shop(), client(fake))[0]
        self.assertIn("NOT reachable", line)
        self.assertIn(CREDS["client_email"], line)


if __name__ == "__main__":
    unittest.main()
