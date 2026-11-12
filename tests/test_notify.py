"""W2: telling the owner about calls. Nothing here talks to a real mail or text service: senders are fakes."""
import json
import logging
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

import tests  # noqa: F401
from receptionist import notify
from receptionist.config import load_business, load_config
from receptionist.server import create_app
from receptionist.store import Store

CFG = load_config()
SHOP = {**load_business(CFG, "lawn_care"), "notify": {"email": ["owner@example.test"]}}
NOW = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
SUMMARY = {"summary": "Pat's sprinkler line is leaking; booked Friday morning.", "caller_name": "Pat",
           "callback_number": "2085550142", "intent": "new_job", "outcome": "booked", "urgency": "this_week",
           "follow_up": "", "flags": []}


class FakeEmail:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, to, subject, body):
        if self.fail:
            raise notify.SendError("boom")
        self.sent.append((to, subject, body))


def notifier(email=None, **kw):
    store = Store(":memory:")
    return notify.Notifier(store, email=email if email is not None else FakeEmail(), clock=lambda: NOW, **kw), store


def start(store, call_id="c1", caller="+12085550142"):
    store.start_call(call_id, SHOP["id"], "retell", caller, NOW)


class CallEmail(unittest.TestCase):
    def test_a_finished_call_emails_every_owner_address_with_the_facts(self):
        n, store = notifier(base_url="https://x.test")
        start(store)
        store.add_appointment(SHOP["id"], "c1", "2026-11-13T08:00", "mowing", "Pat", "2085550142", "1 Main", "Sandpoint", "leak", None, NOW)
        biz = {**SHOP, "notify": {"email": ["a@example.test", "b@example.test"]}}
        n.call_finished(biz, "c1", SUMMARY, "completed")
        self.assertEqual([m[0] for m in n.email.sent], ["a@example.test", "b@example.test"])
        subject, body = n.email.sent[0][1:]
        self.assertNotIn("URGENT", subject)
        for fact in ("Pat", "booked", "2026-11-13T08:00", "https://x.test/dashboard?business=lawn_care"):
            self.assertIn(fact, body + subject)
        self.assertIn("208", body)

    def test_the_link_carries_the_shops_key_when_there_is_one(self):
        n, store = notifier(base_url="https://x.test", shop_key=lambda b: "KEY123")
        self.assertIn("token=KEY123", n.link(SHOP))

    def test_an_urgent_message_or_emergency_says_urgent_in_the_subject(self):
        n, store = notifier()
        start(store)
        store.add_message(SHOP["id"], "c1", "Pat", "2085550142", "Basement is flooding", "owner", True, NOW)
        n.call_finished(SHOP, "c1", {**SUMMARY, "outcome": "message"}, "completed")
        self.assertTrue(n.email.sent[0][1].startswith("URGENT: "))
        n2, s2 = notifier()
        start(s2, "c2")
        n2.call_finished(SHOP, "c2", {**SUMMARY, "urgency": "emergency"}, "safety")
        self.assertTrue(n2.email.sent[0][1].startswith("URGENT: "))

    def test_a_failed_summary_still_tells_the_owner(self):
        n, store = notifier()
        start(store)
        n.call_finished(SHOP, "c1", None, "caller hung up")
        self.assertIn("transcript", n.email.sent[0][2])

    def test_a_shop_with_no_address_sends_nothing(self):
        n, store = notifier()
        start(store)
        n.call_finished({**SHOP, "notify": {}}, "c1", SUMMARY, "completed")
        self.assertEqual(n.email.sent, [])

    def test_a_sender_that_fails_is_recorded_and_never_raised(self):
        n, store = notifier(email=FakeEmail(fail=True))
        start(store)
        n.call_finished(SHOP, "c1", SUMMARY, "completed")   # no exception
        row = n.recent()[0]
        self.assertEqual((row["status"], row["error"]), ("failed", "boom"))
        self.assertEqual(n.failures_today(), 1)

    def test_no_sender_configured_is_recorded_as_skipped(self):
        store = Store(":memory:")
        n = notify.Notifier(store, email=None, clock=lambda: NOW)
        start(store)
        n.call_finished(SHOP, "c1", SUMMARY, "completed")
        self.assertEqual(n.recent()[0]["status"], "skipped")


class Limits(unittest.TestCase):
    def test_past_a_shops_daily_share_mail_is_held_but_urgent_still_goes(self):
        n, store = notifier()
        biz = {**SHOP, "notify": {"email": ["o@example.test"], "max_per_day": 2}}
        for i in range(4):
            start(store, f"c{i}")
            n.call_finished(biz, f"c{i}", SUMMARY, "completed")
        self.assertEqual(len(n.email.sent), 2)
        self.assertEqual([r["status"] for r in n.recent()][:2], ["held", "held"])
        start(store, "u")
        n.call_finished(biz, "u", {**SUMMARY, "urgency": "emergency"}, "safety")
        self.assertEqual(len(n.email.sent), 3)

    def test_near_the_providers_cap_only_urgent_mail_goes_and_at_half_a_warning_is_logged(self):
        n, store = notifier(daily_cap=10)
        with self.assertLogs("receptionist.notify", "WARNING") as logs:
            for i in range(5):
                n._send_email("s", f"c{i}", "call", "o@example.test", "s", "b")
        self.assertTrue(any("5 of the 10" in m for m in logs.output))
        for i in range(4):
            n._send_email("s", f"d{i}", "call", "o@example.test", "s", "b")
        self.assertEqual(len(n.email.sent), 9)     # the 10th would pass 90%: held
        self.assertFalse(n._send_email("s", "x", "call", "o@example.test", "s", "b"))
        self.assertTrue(n._send_email("s", "u", "urgent", "o@example.test", "s", "b", urgent=True))
        self.assertFalse(n._send_email("s", "v", "urgent", "o@example.test", "s", "b", urgent=True))   # the cap itself


class FakeSMTP:
    log = []

    def __init__(self, host, port, timeout=None):
        self.log.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.log.append(("tls",))

    def login(self, user, password):
        self.log.append(("login", user))

    def send_message(self, msg):
        self.log.append(("send", msg["To"], msg["Subject"], msg.get_content()))


class Senders(unittest.TestCase):
    def test_smtp_connects_with_tls_logs_in_and_sends(self):
        FakeSMTP.log = []
        notify.SmtpSender("smtp.example.test", "me@example.test", "pw", factory=FakeSMTP).send("o@example.test", "Hi", "Body")
        self.assertEqual([e[0] for e in FakeSMTP.log], ["connect", "tls", "login", "send"])
        self.assertEqual(FakeSMTP.log[-1][1:], ("o@example.test", "Hi", "Body\n"))

    def test_an_smtp_error_becomes_a_send_error(self):
        def refuse(*a, **k):
            raise ConnectionRefusedError("no")
        with self.assertRaises(notify.SendError):
            notify.SmtpSender("h", "u", "p", factory=refuse).send("a", "b", "c")

    def test_the_sender_comes_from_the_environment_and_is_absent_without_it(self):
        self.assertIsNone(notify.email_sender_from_env({}))
        s = notify.email_sender_from_env({"SMTP_HOST": "h", "SMTP_USER": "u", "SMTP_PASSWORD": "p"})
        self.assertIsInstance(s, notify.SmtpSender)
        h = notify.email_sender_from_env({"EMAIL_API_URL": "u", "EMAIL_API_KEY": "k", "EMAIL_FROM": "f@x"})
        self.assertIsInstance(h, notify.HttpEmailSender)

    def test_the_http_backend_posts_json_through_the_same_interface(self):
        seen = []
        notify.HttpEmailSender("https://api.test/send", "k", "from@x", post=lambda *a: seen.append(a)).send("o@x", "S", "B")
        url, headers, payload = seen[0]
        self.assertEqual((url, payload["to"], payload["subject"]), ("https://api.test/send", ["o@x"], "S"))
        self.assertEqual(headers["Authorization"], "Bearer k")


class FakeSms:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, to, body):
        if self.fail:
            raise notify.SendError("down")
        self.sent.append((to, body))


class Texts(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.sms = FakeSms()
        self.n = notify.Notifier(self.store, email=FakeEmail(), sms=self.sms, clock=lambda: NOW)
        self.biz = {**SHOP, "texting_enabled": True, "owner_sms": "+12085550102"}

    def queue(self, to, body):
        self.store.queue_sms(self.biz["id"], "c1", to, body, NOW)

    def test_nothing_is_sent_while_texting_is_off_and_the_rows_wait(self):
        self.queue("+12085550102", "New booking")
        self.assertEqual(self.n.drain_sms({**self.biz, "texting_enabled": False}), 0)
        self.assertEqual(self.sms.sent, [])
        self.assertEqual(self.store.recent("sms_outbox", self.biz["id"])[0]["status"], "queued")
        self.assertEqual(self.n.drain_sms(self.biz), 1)      # the day it is switched on

    def test_owner_alerts_go_to_the_owners_number_and_caller_texts_get_the_stop_line(self):
        self.queue("+12085550102", "New booking A1001")
        self.queue("+12085550142", "Your visit is Friday.")
        self.assertEqual(self.n.drain_sms(self.biz), 2)
        self.assertEqual(self.sms.sent[0], ("+12085550102", "New booking A1001"))
        self.assertTrue(self.sms.sent[1][1].endswith("Reply STOP to opt out."))

    def test_a_caller_who_texted_stop_gets_nothing_until_start(self):
        self.assertEqual(self.n.handle_inbound_sms("+1 (208) 555-0142", "Stop"), notify.STOP_REPLY)
        self.queue("+12085550142", "Reminder")
        self.assertEqual(self.n.drain_sms(self.biz), 0)
        self.assertEqual(self.store.recent("sms_outbox", self.biz["id"])[0]["status"], "blocked_optout")
        self.assertEqual(self.n.handle_inbound_sms("2085550142", "START"), notify.START_REPLY)
        self.queue("+12085550142", "Reminder 2")
        self.assertEqual(self.n.drain_sms(self.biz), 1)

    def test_other_inbound_text_is_left_alone(self):
        self.assertIsNone(self.n.handle_inbound_sms("+12085550142", "thanks!"))

    def test_a_failing_sender_marks_the_row_failed_and_does_not_raise(self):
        self.n.sms = FakeSms(fail=True)
        self.queue("+12085550102", "x")
        self.assertEqual(self.n.drain_sms(self.biz), 0)
        self.assertEqual(self.store.recent("sms_outbox", self.biz["id"])[0]["status"], "failed")
        self.assertEqual(self.n.failures_today(), 1)

    def test_twilio_posts_to_the_messages_api_through_the_messaging_service(self):
        seen = []
        notify.TwilioSender("AC1", "tok", "MG1", post=lambda *a: seen.append(a) or 201).send("+12085550142", "Hi")
        url, auth, form = seen[0]
        self.assertEqual(url, "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages.json")
        self.assertEqual(form, {"To": "+12085550142", "MessagingServiceSid": "MG1", "Body": "Hi"})
        self.assertTrue(auth.startswith("Basic "))
        with self.assertRaises(notify.SendError):
            notify.TwilioSender("AC1", "tok", "MG1", post=lambda *a: 400).send("+1", "x")

    def test_the_twilio_sender_needs_all_three_settings(self):
        self.assertIsNone(notify.sms_sender_from_env({"TWILIO_ACCOUNT_SID": "AC"}))
        self.assertIsNotNone(notify.sms_sender_from_env({"TWILIO_ACCOUNT_SID": "a", "TWILIO_AUTH_TOKEN": "b",
                                                         "TWILIO_MESSAGING_SERVICE_SID": "c"}))


class Digest(unittest.TestCase):
    TUE_EVENING = datetime(2026, 11, 3, 18, 30)   # shop time, after closing

    def setUp(self):
        from receptionist import schedule
        self.biz = {**SHOP, "texting_enabled": False}
        self.n, self.store = notifier()
        self.at = schedule.local_now(self.biz, "2026-11-03T18:30")
        self.noon = schedule.local_now(self.biz, "2026-11-03T12:00")
        self.store.start_call("c1", self.biz["id"], "retell", "+12085550142", self.at)
        self.store.save_summary("c1", {**SUMMARY, "flags": [{"type": "price_quoted", "quote": "q", "explanation": "quoted $50"}]})
        self.store.add_message(self.biz["id"], "c1", "Pat", "2085550142", "Call me about the gate", "owner", False, self.at)

    def test_it_waits_for_closing_time_and_goes_once_a_day(self):
        self.assertFalse(self.n.digest_due(self.biz, self.noon))
        self.assertTrue(self.n.digest_due(self.biz, self.at))
        self.assertEqual(self.n.run_digests([self.biz], self.at), 1)
        self.assertEqual(self.n.run_digests([self.biz], self.at), 0)
        self.assertEqual(len(self.n.email.sent), 1)

    def test_it_counts_the_days_calls_messages_and_review_flags(self):
        self.assertTrue(self.n.send_digest(self.biz, self.at))
        subject, body = self.n.email.sent[0][1:]
        self.assertIn("1 calls", subject)
        for fact in ("Calls answered: 1", "Messages for you: 1", "Call me about the gate", "price_quoted", "quoted $50"):
            self.assertIn(fact, body)

    def test_a_failed_digest_is_retried_not_marked_done(self):
        self.n.email = FakeEmail(fail=True)
        self.assertEqual(self.n.run_digests([self.biz], self.at), 0)
        self.assertTrue(self.n.digest_due(self.biz, self.at))

    def test_a_shop_can_turn_it_off(self):
        biz = {**self.biz, "notify": {"email": ["o@example.test"], "digest": False}}
        self.assertFalse(self.n.digest_due(biz, self.at))


class Through_the_server(unittest.TestCase):
    """A call ended through the Retell webhook emails the owner and shows on the admin view."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clients = Path(self.tmp.name) / "clients"
        clients.mkdir()
        (clients / "north.json").write_text(json.dumps({**SHOP, "id": "north", "name": "North", "phone_numbers": ["+12085550100"],
                                                         "demo": False}))
        self.cfg = {**CFG, "data_dir": self.tmp.name, "clients_dir": str(clients), "live_businesses": []}
        self.email = FakeEmail()
        env = {"ALLOW_UNVERIFIED_WEBHOOKS": "1", "DASHBOARD_TOKEN": "a" * 32, "TOKEN_SECRET": "b" * 32,
               "PUBLIC_BASE_URL": "https://x.test"}
        self.p = [mock.patch.dict(os.environ, env), mock.patch.dict(os.environ, {"RETELL_API_KEY": ""}), mock.patch.object(notify, "email_sender_from_env", lambda: self.email)]
        for p in self.p:
            p.start()
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **k: SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(SUMMARY))],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1))))
        self.http = TestClient(create_app(self.cfg, client=client, warm_cache=False))

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()

    def end_call(self, call_id="abc"):
        call = {"call_id": call_id, "to_number": "+12085550100", "from_number": "+12085550142",
                "transcript_object": [{"role": "agent", "content": "Hi"}, {"role": "user", "content": "My sprinkler leaks"}],
                "disconnection_reason": "user_hangup"}
        self.assertEqual(self.http.post("/retell/webhook", json={"event": "call_ended", "call": call}).status_code, 200)

    def test_the_owner_is_emailed_after_the_call_and_the_admin_sees_it(self):
        self.end_call()
        self.assertEqual(len(self.email.sent), 1)
        self.assertIn("token=", self.email.sent[0][2])
        r = self.http.get("/admin/notifications", headers={"Authorization": "Bearer " + "a" * 32})
        self.assertEqual(r.json()["sent_today"], 1)
        self.assertEqual(self.http.get("/admin/notifications").status_code, 404)


if __name__ == "__main__":
    unittest.main()
