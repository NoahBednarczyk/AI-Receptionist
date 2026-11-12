import os
import unittest
from unittest import mock

import tests  # noqa: F401
from tests import test_notify


class Site(unittest.TestCase):
    setUp = test_notify.Through_the_server.setUp
    tearDown = test_notify.Through_the_server.tearDown

    def post(self, **over):
        body = {"name": "Pat", "shop": "Pat's Plumbing", "contact": "pat@x.test", "message": "call me", **over}
        return self.http.post("/contact", json=body)

    def test_the_page_names_the_demo_number_and_what_it_will_not_do(self):
        r = self.http.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("(208) 428-6760", r.text)
        self.assertIn("never pretends to be a person", r.text)

    def test_the_form_emails_the_admin_and_nobody_else(self):
        with mock.patch.dict(os.environ, {"ADMIN_EMAIL": "noah@admin.test"}):
            self.assertEqual(self.post().status_code, 200)
        to, subject, body = self.email.sent[-1]
        self.assertEqual(to, "noah@admin.test")
        self.assertIn("pat@x.test", body)

    def test_a_bot_that_fills_the_hidden_field_sends_nothing_and_missing_fields_are_refused(self):
        with mock.patch.dict(os.environ, {"ADMIN_EMAIL": "noah@admin.test"}):
            self.assertEqual(self.post(website="http://spam").status_code, 200)
            self.assertEqual(self.email.sent, [])
            self.assertEqual(self.post(contact="").status_code, 400)

    def test_it_says_so_when_email_is_not_set_up_and_limits_a_flood(self):
        with mock.patch.dict(os.environ, {"ADMIN_EMAIL": ""}):
            self.assertEqual(self.post().status_code, 503)
        with mock.patch.dict(os.environ, {"ADMIN_EMAIL": "noah@admin.test"}):
            codes = [self.post().status_code for _ in range(5)]
        self.assertIn(429, codes)


if __name__ == "__main__":
    unittest.main()
