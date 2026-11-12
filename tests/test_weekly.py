import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import tests  # noqa: F401
from receptionist import notify, weekly
from receptionist.config import load_business, load_config
from receptionist.store import Store

CFG = load_config()
SHOP = {**load_business(CFG, "hvac"), "id": "north", "name": "North Heating", "demo": False,
        "notify": {"email": ["o@shop.test"]}, "avg_job_value": 400}
TZ = ZoneInfo(SHOP["timezone"])
MON = datetime(2026, 11, 16, 9, 0, tzinfo=TZ)   # a Monday


class Fake:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))


def seeded():
    store = Store(":memory:")
    # one call during hours that booked a job, one at 10 pm that left a message, one 9 days ago (outside the week)
    for cid, when in (("c1", MON - timedelta(days=3, hours=-1)), ("c2", MON - timedelta(days=2, hours=11)), ("old", MON - timedelta(days=9))):
        store.start_call(cid, "north", "retell", "+12085550142", when)
    store.add_appointment("north", "c1", "2026-11-14T09:00", "furnace_repair", "Pat", "2085550142", "1 A St", "Sandpoint", "no heat", None, MON)
    store.add_appointment("north", "old", "2026-11-05T09:00", "furnace_repair", "Old", "2085550143", "2 A St", "Sandpoint", "x", None, MON)
    store.add_message("north", "c2", "Sam", "2085550144", "wants a quote", "owner", True, MON)
    return store


class Weekly(unittest.TestCase):
    def test_the_numbers_cover_the_last_seven_days_and_count_after_hours(self):
        n = weekly.numbers(seeded(), SHOP, MON)
        self.assertEqual((n["calls"], n["booked"], n["messages"], n["urgent_messages"]), (2, 1, 1, 1))
        self.assertGreaterEqual(n["after_hours"], 1)
        self.assertEqual(n["estimated_value"], 400)

    def test_the_value_is_called_an_estimate_and_is_asked_for_when_unknown(self):
        store = seeded()
        _, text = weekly.report_text(SHOP, weekly.numbers(store, SHOP, MON), MON)
        self.assertIn("about $400", text)
        self.assertIn("estimate", text)
        _, none = weekly.report_text({**SHOP, "avg_job_value": None}, weekly.numbers(store, {**SHOP, "avg_job_value": None}, MON), MON)
        self.assertIn("Tell us your average job value", none)

    def test_it_goes_once_on_monday_morning_and_never_for_a_demo(self):
        store, fake = seeded(), Fake()
        n = notify.Notifier(store, email=fake, clock=lambda: MON.astimezone(ZoneInfo("UTC")))
        self.assertFalse(weekly.due(store, SHOP, MON.replace(hour=7)))
        self.assertFalse(weekly.due(store, SHOP, MON + timedelta(days=1)))
        self.assertFalse(weekly.due(store, {**SHOP, "demo": True}, MON))
        self.assertEqual(weekly.run(n, [SHOP], MON), 1)
        self.assertEqual(weekly.run(n, [SHOP], MON + timedelta(hours=3)), 0)    # already sent this week
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(weekly.run(n, [SHOP], MON + timedelta(days=7)), 1)     # next week it goes again


if __name__ == "__main__":
    unittest.main()
