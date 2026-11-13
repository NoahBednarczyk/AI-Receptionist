"""A demo shop gets its owner email from DEMO_NOTIFY_EMAIL, so no address is committed to the public repo."""
import os
import unittest
from unittest import mock

import tests  # noqa: F401
from receptionist import config


class DemoEmailTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load_config()

    def test_demo_shop_gets_the_address_from_the_environment(self):
        with mock.patch.dict(os.environ, {"DEMO_NOTIFY_EMAIL": "me@example.com"}):
            self.assertEqual(config.load_business(self.cfg, "lawn_care")["notify"]["email"], ["me@example.com"])
            shops = {b["id"]: b for b in config.all_businesses(self.cfg)}
            self.assertEqual(shops["lawn_care"]["notify"]["email"], ["me@example.com"])

    def test_nothing_is_added_without_the_variable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEMO_NOTIFY_EMAIL", None)
            self.assertFalse((config.load_business(self.cfg, "lawn_care").get("notify") or {}).get("email"))


if __name__ == "__main__":
    unittest.main()
