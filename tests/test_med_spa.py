"""The med spa business: fixed-time appointments at the clinic, no street address, provider transfers and
medical safety lines. Runs on the same engine as the home-services business."""
import json
import unittest
from pathlib import Path

from receptionist import prompts, safety, schedule
from receptionist.agent import CallSession
from receptionist.config import load_business, load_config
from receptionist.store import Store
from receptionist.tools import ToolContext, ToolError, run_tool, schemas
from tests.test_logic import FakeClient, reply, text, use

CFG = load_config()
GLOW = load_business(CFG, "glow")
ACME = load_business(CFG, "acme")
TUE_1015 = schedule.local_now(GLOW, "2026-10-27T10:15")
WED_2030 = schedule.local_now(GLOW, "2026-10-28T20:30")
MON_1100 = schedule.local_now(GLOW, "2026-10-26T11:00")


def ctx(now=TUE_1015, **kw):
    store = Store(":memory:")
    store.seed_demo(GLOW, now)
    return ToolContext(GLOW, store, now, kw.pop("caller", "+13125550177"), "test-call", **kw)


class ClinicBookingTests(unittest.TestCase):
    def test_times_are_spoken_as_a_single_time(self):
        c = ctx()
        wins = run_tool(c, "check_availability", {"service_id": "hydrafacial"})[0]["windows"]
        self.assertTrue(wins)
        for w in wins:
            self.assertRegex(w["spoken"], r" at \d+(:\d\d)? (AM|PM)$|at noon$")
            self.assertNotIn("between", w["spoken"])

    def test_booking_needs_no_address_or_town(self):
        props = {t["name"]: t for t in schemas(GLOW)}["book_appointment"]["input_schema"]
        self.assertNotIn("street_address", props["properties"])
        self.assertNotIn("town", props["properties"])
        self.assertNotIn("street_address", props["required"])
        home = {t["name"]: t for t in schemas(ACME)}["book_appointment"]["input_schema"]
        self.assertIn("street_address", home["required"])   # house calls still need one

    def test_a_booking_is_saved_and_can_be_updated(self):
        c = ctx()
        wid = run_tool(c, "check_availability", {"service_id": "hydrafacial"})[0]["windows"][0]["window_id"]
        args = {"window_id": wid, "service_id": "hydrafacial", "customer_name": "Riley Chen",
                "callback_number": "312-555-0177", "problem_summary": "HydraFacial."}
        result, is_error = run_tool(c, "book_appointment", args)
        self.assertFalse(is_error, result)
        self.assertEqual(result["status"], "booked")
        self.assertIsNone(c.store.appointments_for_call("test-call")[0]["address"])
        again, _ = run_tool(c, "book_appointment", {**args, "access_notes": "Sensitive skin."})
        self.assertEqual((again["status"], again["confirmation"]), ("updated", result["confirmation"]))

    def test_two_providers_means_a_time_fills_after_two_bookings(self):
        c = ctx()
        wid = run_tool(c, "check_availability", {"service_id": "hydrafacial"})[0]["windows"][0]["window_id"]
        for phone in ("312-555-0181", "312-555-0182"):
            c.call_id = phone
            run_tool(c, "book_appointment", {"window_id": wid, "service_id": "hydrafacial", "customer_name": "A B",
                                             "callback_number": phone, "problem_summary": "Facial."})
        c.call_id = "third"
        result, is_error = run_tool(c, "book_appointment", {"window_id": wid, "service_id": "hydrafacial",
                                                            "customer_name": "C D", "callback_number": "312-555-0183",
                                                            "problem_summary": "Facial."})
        self.assertTrue(is_error)

    def test_closed_days_have_no_times(self):
        self.assertFalse(schedule.is_open(GLOW, MON_1100))
        self.assertEqual(schedule.windows_on(GLOW, MON_1100.date()), [])
        saturday = schedule.local_now(GLOW, "2026-10-31T10:00")
        self.assertEqual(len(schedule.windows_on(GLOW, saturday.date())), 6)   # 9 AM to 3 PM


class ProviderTransferTests(unittest.TestCase):
    def test_provider_is_reachable_any_hour_and_the_front_desk_only_when_open(self):
        for now in (TUE_1015, WED_2030):
            result, is_error = run_tool(ctx(now, transfer_outcome="connected"), "transfer_call",
                                        {"target": "provider", "reason": "x"})
            self.assertFalse(is_error, result)
        _, is_error = run_tool(ctx(WED_2030), "transfer_call", {"target": "office", "reason": "x"})
        self.assertTrue(is_error)
        _, is_error = run_tool(ctx(TUE_1015), "transfer_call", {"target": "office", "reason": "x"})
        self.assertFalse(is_error)

    def test_acme_transfer_rules_are_unchanged(self):
        acme = ToolContext(ACME, Store(":memory:"), WED_2030, None, "c")
        self.assertTrue(run_tool(acme, "transfer_call", {"target": "office", "reason": "x"})[1])


class MedSpaSafetyTests(unittest.TestCase):
    CASES = [("I can't breathe and my lips are swelling after the filler", "allergic_reaction"),
             ("My face is swelling up and it's hard to breathe", "allergic_reaction"),
             ("Since yesterday my vision is blurry in one eye after the filler", "vision_or_blockage"),
             ("The skin by my nose is turning white after the injection", "vision_or_blockage"),
             ("It won't stop bleeding", "heavy_bleeding"),
             ("There's pus and a fever after my laser treatment", "urgent_reaction"),
             ("I want to book Botox next week", None), ("How much is lip filler?", None),
             ("I don't have any trouble breathing, I just have a question", None),
             ("I had no swelling after my facial", None)]

    def test_matches_and_non_matches(self):
        for said, want in self.CASES:
            got = safety.match("med_spa", said)
            self.assertEqual(got[0] if got else None, want, said)

    def test_spanish(self):
        self.assertEqual(safety.match("med_spa", "No puedo respirar", "es")[0], "allergic_reaction")

    def test_an_allergic_reaction_ends_the_call_with_a_911_line(self):
        s = CallSession(FakeClient(), CFG, GLOW, Store(":memory:"), TUE_1015, caller="+13125550144")
        turn = s.respond("I had filler and my face is swelling and I can't breathe.")
        self.assertIn("911", turn.text)
        self.assertIn("hang up", turn.text.lower())
        self.assertTrue(turn.ended)
        self.assertEqual(s.ctx.end_reason, "safety")


class PromptTests(unittest.TestCase):
    def test_med_spa_prompt_is_about_a_clinic(self):
        p = prompts.stable_prompt(GLOW)
        for needle in ("not a medical professional", "free consultation", "Location:", "Approved aftercare tips",
                       "That's Thursday at 2 PM for a HydraFacial"):
            self.assertIn(needle, p)
        for stale in ("technician", "street address", "bucket", "Service area", "main water valve"):
            self.assertNotIn(stale, p)

    def test_home_services_prompt_keeps_its_rules(self):
        p = prompts.stable_prompt(ACME)
        for needle in ("the street address and town", "bucket or towel", "gas, electrical work", "Service area:"):
            self.assertIn(needle, p)

    def test_the_returning_client_is_greeted_without_an_address_check(self):
        c = ctx(caller="+13125550143")
        state = prompts.call_state(GLOW, TUE_1015, "+13125550143", c.store.get_customer("glow", "+13125550143"), [])
        self.assertIn("Taylor Brooks", state)
        self.assertNotIn("address", state)


class VarietyAndSpeedTests(unittest.TestCase):
    def test_greetings_vary_but_always_name_the_business_and_never_ask_or_mention_recording(self):
        seen = set()
        for _ in range(200):
            g = prompts.pick_greeting(GLOW, TUE_1015)
            seen.add(g)
            self.assertIn("Lakeside Glow Med Spa", g)
            self.assertNotIn("{", g)
            self.assertNotIn("?", g)
            self.assertNotIn("record", g.lower())
        self.assertGreaterEqual(len(seen), 5)
        self.assertIn("Good morning", " ".join(prompts.pick_greeting({**GLOW, "greetings": ["Good {daypart}."]}, TUE_1015)
                                               for _ in range(1)))

    def test_a_business_with_one_greeting_keeps_it(self):
        self.assertEqual(prompts.pick_greeting(ACME, TUE_1015), ACME["greeting"])

    def test_the_greeting_and_style_are_per_call_not_in_the_cached_prompt(self):
        self.assertNotIn(GLOW["greeting"], prompts.stable_prompt(GLOW))
        s = CallSession(FakeClient(), CFG, GLOW, Store(":memory:"), TUE_1015, caller="+13125550177")
        self.assertIn(s.greeting, s.system[1]["text"])
        self.assertIn("Style for this call:", s.system[1]["text"])
        self.assertEqual(s.transcript[0]["text"], s.greeting)
        styles = {prompts.call_state(GLOW, TUE_1015, None, None, []).split("Style for this call: ")[1].split(" Change up")[0]
                  for _ in range(100)}
        self.assertGreaterEqual(len(styles), 4)

    def test_the_first_clause_is_spoken_before_the_sentence_ends(self):
        from receptionist.agent import Sentences
        said = []
        w = Sentences(said.append)
        for piece in ("Got", " it,", " a slow", " leak under the sink.", " Can I get", " your name?"):
            w.add(piece)
        self.assertEqual(said[0], "Got it,")                       # out as soon as the clause was complete
        self.assertEqual(said[1], "a slow leak under the sink.")
        w.close()
        self.assertEqual(said[-1], "Can I get your name?")

    def test_no_early_clause_for_one_word_or_after_something_was_said(self):
        from receptionist.agent import Sentences
        said = []
        w = Sentences(said.append)
        w.add("Ah, the classic mystery puddle. Is it dripping, or just damp?")
        w.close()
        self.assertEqual(said, ["Ah, the classic mystery puddle.", "Is it dripping, or just damp?"])


class ScenarioTests(unittest.TestCase):
    def test_scenarios_only_expect_things_this_business_has(self):
        scenarios = json.loads((Path(CFG["scenarios_dir"]) / "med_spa.json").read_text())
        services = {s["id"] for s in GLOW["services"]}
        targets = set(GLOW["transfer_targets"])
        for sc in scenarios:
            e = sc["expect"]
            if "service" in e:
                self.assertIn(e["service"], services, sc["id"])
            for key in ("transfer", "transfer_attempted"):
                if e.get(key):
                    self.assertIn(e[key], targets, sc["id"])
            self.assertTrue(sc["rubric"] and sc["persona"], sc["id"])


if __name__ == "__main__":
    unittest.main()


class CalendarFeedTests(unittest.TestCase):
    def appt(self, **kw):
        return {"confirmation": "G0901", "window_id": "2026-10-29T14:00", "service_id": "hydrafacial",
                "name": "Taylor Brooks", "phone": "+13125550143", "address": None, "town": None,
                "problem": "HydraFacial.", "access_notes": "Sensitive skin; allergic, mild", "status": "booked", **kw}

    def test_an_event_has_the_right_time_title_and_escaped_notes(self):
        from receptionist import calendar_feed
        ics = calendar_feed.render(GLOW, [self.appt()], TUE_1015)
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n"))
        self.assertIn("UID:G0901@receptionist", ics)
        self.assertIn("SUMMARY:HydraFacial and signature facials - Taylor Brooks", ics)
        self.assertIn("DTSTART:20261029T210000Z", ics)     # 2 PM Pacific daylight time
        self.assertIn("DTEND:20261029T220000Z", ics)       # an hour long
        self.assertIn("Sensitive skin\\; allergic\\, mild", ics.replace("\r\n ", ""))   # unfolded
        self.assertIn("STATUS:CONFIRMED", ics)
        self.assertNotIn("LOCATION", ics)                  # clients come to the clinic
        self.assertTrue(all(len(line.encode()) <= 75 for line in ics.split("\r\n")))

    def test_cancelled_events_stay_in_the_feed_marked_cancelled(self):
        from receptionist import calendar_feed
        ics = calendar_feed.render(GLOW, [self.appt(status="cancelled")], TUE_1015)
        self.assertIn("STATUS:CANCELLED", ics)

    def test_house_calls_show_the_address_and_their_window(self):
        from receptionist import calendar_feed
        a = self.appt(service_id="leak_repair", window_id="2026-10-29T10:00", address="14 Birch Lane", town="Sagle")
        ics = calendar_feed.render(ACME, [a], schedule.local_now(ACME, "2026-10-27T10:15"))
        self.assertIn("LOCATION:14 Birch Lane\\, Sagle", ics)
        self.assertIn("DTSTART:20261029T170000Z", ics)
        self.assertIn("DTEND:20261029T190000Z", ics)       # the two-hour arrival window

    def test_tokens_differ_by_business_and_by_secret(self):
        from receptionist.access import shop_key
        glow, acme = {"id": "glow"}, {"id": "acme"}
        self.assertNotEqual(shop_key("s", glow, "calendar"), shop_key("s", acme, "calendar"))
        self.assertNotEqual(shop_key("s", glow, "calendar"), shop_key("t", glow, "calendar"))
        self.assertNotEqual(shop_key("s", glow, "calendar"), "s")
        self.assertEqual(len(shop_key("s", glow, "calendar")), 32)

    def test_the_route_needs_the_business_token(self):
        import os
        from unittest import mock
        from fastapi.testclient import TestClient
        from receptionist.access import shop_key
        from receptionist.server import create_app
        with mock.patch.dict(os.environ, {"DASHBOARD_TOKEN": "dash" * 7, "TOKEN_SECRET": "sec" * 9}):
            client = TestClient(create_app(CFG, client=FakeClient(), warm_cache=False))
            good = {"token": shop_key("sec" * 9, {"id": "glow"}, "calendar"), "business": "glow"}
            r = client.get("/calendar.ics", params=good)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.headers["content-type"].startswith("text/calendar"))
            self.assertEqual(client.get("/calendar.ics", params={**good, "token": "dash" * 7}).status_code, 404)   # not the dashboard token
            self.assertEqual(client.get("/calendar.ics", params={**good, "business": "acme"}).status_code, 404)
            self.assertEqual(client.get("/calendar.ics").status_code, 404)
