import tempfile
import unittest
from pathlib import Path

import yaml

import tests  # noqa: F401
from receptionist import onboarding
from receptionist.config import load_business, load_config

CFG = load_config()
INTAKE = yaml.safe_load((Path(".internal") / "intake.hvac.example.yaml").read_text())


class OwnerEmailAndSheet(unittest.TestCase):
    def test_a_shop_needs_an_owner_email_and_it_lands_in_notify(self):
        template = load_business(CFG, "hvac")
        biz = onboarding.build({**INTAKE, "owner_email": "a@shop.test, b@shop.test"}, template)
        self.assertEqual(biz["notify"], {"email": ["a@shop.test", "b@shop.test"]})
        for bad, why in (({"owner_email": ""}, "email"), ({"owner_email": "not-an-email"}, "look like")):
            with self.assertRaises(onboarding.IntakeError) as e:
                onboarding.build({**INTAKE, **bad}, template)
            self.assertIn(why, str(e.exception))

    def test_run_writes_a_printable_page_and_the_sheet_covers_what_the_owner_must_know(self):
        with tempfile.TemporaryDirectory() as d:
            lines = []
            path = onboarding.run({**CFG, "clients_dir": d}, dict(INTAKE), out=lines.append)
            html = path.with_suffix(".checklist.html").read_text()
            self.assertIn("@media print", html)
            self.assertIn("&#9744;", html)
            text = path.with_suffix(".checklist.txt").read_text()
            for needle in ("is recorded", "can make mistakes", "You own your shop's facts", "YOUR CALENDAR", "email", "writes your message first",
                           "208-555-0100"):
                self.assertIn(needle, text)
            self.assertNotIn("2085550100", text)


if __name__ == "__main__":
    unittest.main()
