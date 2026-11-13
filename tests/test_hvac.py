"""The HVAC and plumbing shop: winter safety tier, emergency slots, overflow mode, new-client onboarding and the
owner's dashboard."""
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from receptionist import dashboard, onboarding, prompts, safety, schedule
from receptionist.agent import CallSession
from receptionist.config import all_businesses, business_for_number, load_business, load_config
from receptionist.store import Store
from receptionist.tools import ToolContext, run_tool
from tests.test_logic import FakeClient

CFG = load_config()
HVAC = load_business(CFG, "hvac")
ACME = load_business(CFG, "acme")
WINTER_NIGHT = schedule.local_now(HVAC, "2026-12-15T21:30")      # Tuesday night, shop closed
TUE_1015 = schedule.local_now(HVAC, "2026-11-10T10:15")


def ctx(now=TUE_1015, **kw):
    store = Store(":memory:")
    store.seed_demo(HVAC, now)
    return ToolContext(HVAC, store, now, kw.pop("caller", "+12085550177"), "test-call", **kw)


class SafetyTierTests(unittest.TestCase):
    # The lines as they stood before the winter tier was added. They must never change or weaken.
    GAS = "Please leave the house now, don't touch any switches, and once you're outside, call 911 or the gas company."
    CO = "Please get everyone out of the house now and call 911 from outside."
    BURST = ("If it's safe, shut off the main water valve, usually near the water meter, and stay away from anything "
             "electrical near the water.")
    SEWAGE = "Please stop using the sinks, toilets and showers until a technician has looked at it."
    SPARKS = "If it's safe, turn off the power or gas to the unit and keep everyone away from it."

    def test_every_existing_line_is_untouched_and_still_comes_first(self):
        base = safety.VERTICALS["home_services"]["en"]
        hvac = safety.VERTICALS["hvac"]["en"]
        self.assertEqual(hvac[:len(base)], base)                       # same entries, same order
        lines = {name: line for name, _, line, *_ in base}
        self.assertEqual((lines["gas"], lines["carbon_monoxide"]), (self.GAS, self.CO))
        self.assertEqual(lines["flooding"] if "flooding" in lines else lines["burst_pipe"], self.BURST)
        self.assertIn(self.SEWAGE, lines.values())
        self.assertIn(self.SPARKS, lines.values())
        es = safety.VERTICALS["hvac"]["es"]
        self.assertEqual(es[:len(safety.VERTICALS["home_services"]["es"])], safety.VERTICALS["home_services"]["es"])

    def test_the_playbook_still_carries_the_same_lines(self):
        playbook = (Path(CFG["businesses_dir"]).parent / "receptionist" / "playbooks" / "hvac.md").read_text()
        for line in (self.GAS, self.CO, self.BURST, self.SEWAGE, self.SPARKS):
            self.assertIn(line, playbook)

    def test_gas_and_co_beat_the_winter_tier(self):
        said = "My furnace is out, it's five degrees, and I smell gas."
        self.assertEqual(safety.match("hvac", said)[1], self.GAS)
        self.assertTrue(safety.must_leave("gas", said))
        self.assertEqual(safety.match("hvac", "No heat, it's freezing, and the carbon monoxide alarm is going off")[1], self.CO)

    def test_no_heat_in_the_cold(self):
        for said in ("My furnace just quit and it's six degrees out.", "We have no heat and a newborn in the house.",
                     "The boiler isn't working. It's below zero.", "Furnace won't start, it's freezing in here.",
                     "The heat pump stopped working and the house is cold. It's snowing."):
            got = safety.match("hvac", said)
            self.assertEqual(got and got[0], "no_heat_freezing", said)
        # The no-heat and frozen-pipe lines say it is urgent and give no advice (the caller didn't ask for any).
        for said in ("No heat, it's freezing", "I think I have a frozen pipe."):
            line = safety.match("hvac", said)[1]
            self.assertNotIn("911", line, said)
            self.assertNotIn("valve", line, said)

    def test_no_heat_said_as_dont_have(self):
        """The 2026-11-13 test call: "I don't have any heat" matched nothing, and the model filled in the burst-pipe line."""
        for said in ("Hey. I I don't have any heat.", "We don't have heat.", "I haven't got any heat.", "We're without heat."):
            got = safety.match("hvac", said, "en", True)
            self.assertEqual(got and got[0], "no_heat_freezing", said)
        self.assertEqual(safety.match("hvac", "no tengo calefacción", "es", True)[0], "no_heat_freezing")
        self.assertIsNone(safety.match("hvac", "I don't have any heat problems, I just want a tune-up.", "en", True))

    def test_frozen_pipe(self):
        for said in ("I think I have a frozen pipe.", "The pipe under the sink froze last night."):
            got = safety.match("hvac", said)
            self.assertEqual(got and got[0], "frozen_pipe", said)
        self.assertEqual(safety.match("hvac", "A pipe burst and it's flooding")[1], self.BURST)

    def test_no_false_alarms(self):
        for said in ("My furnace makes a clicking noise.", "I'd like a tune-up before winter.",
                     "The AC isn't cooling and it's 94 degrees.", "It's 72 degrees in here but the furnace short-cycles.",
                     "I have heat, I just want a quote.", "How much is a new furnace?", "My furnace won't start."):
            self.assertIsNone(safety.match("hvac", said), said)    # no cold cue, or not a failure

    def test_spanish(self):
        self.assertEqual(safety.match("hvac", "No hay calefacción y hace mucho frío", "es")[0], "no_heat_freezing")
        self.assertIsNone(safety.match("hvac", "Quiero una cita para mañana", "es"))

    def test_the_winter_line_is_spoken_by_code(self):
        s = CallSession(FakeClient(), CFG, HVAC, Store(":memory:"), WINTER_NIGHT, caller="+12085550177")
        self.assertIsNotNone(safety.match(s.biz["vertical"], "My furnace quit and it's six degrees out."))


class EmergencySlotTests(unittest.TestCase):
    def test_an_emergency_visit_can_be_booked_sooner_than_a_routine_one(self):
        now = schedule.local_now(HVAC, "2026-11-10T07:20")        # 20 minutes into the first window
        c = ctx(now)
        routine = run_tool(c, "check_availability", {"service_id": "furnace_repair"})[0]["windows"][0]["window_id"]
        urgent = run_tool(c, "check_availability", {"service_id": "emergency_visit"})[0]["windows"][0]["window_id"]
        self.assertGreater(routine, urgent)                       # earlier for the emergency

    def test_an_emergency_may_use_the_reserved_extra_slot(self):
        c = ctx()
        wid = run_tool(c, "check_availability", {"service_id": "estimate"})[0]["windows"][0]["window_id"]
        for n in range(2):
            c.call_id = f"fill{n}"
            run_tool(c, "book_appointment", {"window_id": wid, "service_id": "estimate", "customer_name": "A B",
                                             "callback_number": f"208-555-018{n}", "street_address": "1 Main St",
                                             "town": "Sandpoint", "problem_summary": "x"})
        c.call_id = "routine"
        _, routine_error = run_tool(c, "book_appointment", {
            "window_id": wid, "service_id": "estimate", "customer_name": "C D", "callback_number": "208-555-0189",
            "street_address": "2 Main St", "town": "Sandpoint", "problem_summary": "x"})
        self.assertTrue(routine_error)                            # two technicians, two bookings: full
        c.call_id = "urgent"
        result, urgent_error = run_tool(c, "book_appointment", {
            "window_id": wid, "service_id": "emergency_visit", "customer_name": "E F", "callback_number": "208-555-0188",
            "street_address": "3 Main St", "town": "Sandpoint", "problem_summary": "No heat"})
        self.assertFalse(urgent_error, result)

    def test_acme_has_no_emergency_slots(self):
        self.assertEqual(schedule.emergency_rules(ACME, ACME["services"][0]), {})


class OverflowTests(unittest.TestCase):
    def test_greetings_never_say_the_crew_is_out_and_never_ask_anything(self):
        seen = {prompts.pick_greeting(HVAC, TUE_1015) for _ in range(200)}
        self.assertGreaterEqual(len(seen), 5)
        for g in seen:
            self.assertNotIn("?", g)
            self.assertIn("Peak Heating and Plumbing", g)
            self.assertNotRegex(g.lower(), r"\bout on\b|\bout in\b|\bout working\b|tied up|crews?\b|team'?s? out|nobody|no one")
            self.assertNotIn("record", g.lower())
            self.assertNotIn("(demo)", g)

    def test_the_call_context_explains_overflow_and_does_not_say_the_office_is_closed(self):
        state = prompts.call_state(HVAC, WINTER_NIGHT, "+12085550177", None, [])
        self.assertIn("Overflow mode", state)
        self.assertNotIn("the office is closed right now", state)
        self.assertIn("save their message quietly with take_message", state)

    def test_the_owner_cell_is_a_target_that_is_always_reachable_and_a_placeholder_never_rings(self):
        self.assertEqual(HVAC["transfer_targets"]["owner"]["available"], "always")
        for now in (WINTER_NIGHT, TUE_1015):
            live = ctx(now, real_transfers=True)
            result, _ = run_tool(live, "transfer_call", {"target": "owner", "reason": "emergency"})
            self.assertEqual(result["status"], "no_answer")        # 555-01xx: acts like nobody picked up
            self.assertIsNone(live.transferred_to)
        test = ctx(WINTER_NIGHT)
        self.assertEqual(run_tool(test, "transfer_call", {"target": "owner", "reason": "x"})[0]["status"], "connected")

    def test_the_prompt_is_for_a_heating_shop(self):
        p = prompts.stable_prompt(HVAC)
        for needle in ("# Heating, cooling and plumbing playbook", "Wood stoves, pellet stoves", "emergency_visit",
                       "no heat, and frozen pipes", "bucket or towel"):
            self.assertIn(needle.lower(), p.lower())
        self.assertNotIn("med spa", p.lower())
        self.assertIn("virtual assistant", p)                      # the AI-disclosure rule is intact

    def test_the_live_number_belongs_to_exactly_one_business(self):
        live = "+12084286760"
        self.assertEqual(business_for_number(CFG, live)["id"], "hvac")     # moved to the trades shop (the pilot) 2026-11-12; lawn care keeps a placeholder
        self.assertEqual(CFG["default_business"], "hvac")
        numbers = [n for b in all_businesses(CFG) for n in b.get("phone_numbers", [])]
        self.assertEqual(len(numbers), len(set(numbers)))          # no number routes to two businesses
        self.assertEqual({b["id"] for b in all_businesses(CFG)} >= {"acme", "hvac", "lawn_care"}, True)


class ScenarioFileTests(unittest.TestCase):
    WANTED = {"relaxed_tune_up", "stressed_no_heat", "no_heat_no_weather", "no_heat_no_weather_july", "owner_cell_no_answer", "washington_caller_recorded", "seven_digit_number_twice", "four_minute_rambler", "no_heat_night", "furnace_short_cycling", "ac_not_cooling", "burst_pipe", "water_heater_leaking",
              "price_shopper", "ai_question", "outside_service_area", "reschedule_existing", "wrong_number",
              "spouse_for_owner", "spanish_speaker"}

    def test_the_twenty_scenarios_are_there_and_consistent_with_the_shop(self):
        scenarios = json.loads((Path(CFG["scenarios_dir"]) / "hvac.json").read_text())
        self.assertEqual({s["id"] for s in scenarios}, self.WANTED)
        services = {s["id"] for s in HVAC["services"]}
        for s in scenarios:
            e = s["expect"]
            if "service" in e:
                self.assertIn(e["service"], services, s["id"])
            for key in ("transfer", "transfer_attempted"):
                if e.get(key):
                    self.assertIn(e[key], HVAC["transfer_targets"], s["id"])
            self.assertTrue(s["persona"] and s["rubric"] and s["caller_id"] is not None, s["id"])
        for sid in HVAC["simulate_quick"]:
            self.assertIn(sid, self.WANTED)

    def test_the_reschedule_scenario_matches_the_seeded_appointment(self):
        seeded = {a["confirmation"]: a for a in HVAC["demo_seed"]["customer_appointments"]}
        scenarios = {s["id"]: s for s in json.loads((Path(CFG["scenarios_dir"]) / "hvac.json").read_text())}
        sc = scenarios["reschedule_existing"]
        self.assertIn("P0901", seeded)
        self.assertEqual(seeded["P0901"]["phone"], sc["caller_id"])


class NewClientTests(unittest.TestCase):
    INTAKE = yaml.safe_load((Path(CFG["businesses_dir"]).parent / ".internal" / "intake.hvac.example.yaml").read_text())

    def build(self, **over):
        return onboarding.build({**self.INTAKE, **over}, HVAC)

    def test_the_example_intake_makes_a_working_business(self):
        biz = self.build()
        onboarding.check(biz)                                       # prompt, tools, safety, windows all load
        self.assertEqual(biz["id"], "example-heating-plumbing")
        self.assertEqual(biz["mode"], "overflow")
        self.assertEqual(biz["transfer_targets"]["owner"]["number"], "+12085550100")
        self.assertEqual(biz["service_area"]["towns"][0], "Sandpoint")
        self.assertEqual(biz["hours"]["sun"], None)
        self.assertEqual(biz["hours"]["sat"], ["08:00", "12:00"])
        self.assertTrue(any(s.get("emergency") for s in biz["services"]))   # winter emergencies are always covered
        self.assertTrue(all("Example Heating" in g or "Example" in g for g in biz["greetings"]))

    def test_nothing_from_the_demo_shop_leaks_into_a_new_shop(self):
        biz = self.build(prices=[], approved_tips=[], faqs=[])
        text = json.dumps(biz)
        for demo in ("$95", "$149", "$175", "Peak", "one year", "licensed and fully insured", "financing", "Pat Rivera"):
            self.assertNotIn(demo, text)
        self.assertEqual(len(biz["pricing"]), 1)                    # only the standing no-quote rule
        self.assertEqual(biz["approved_tips"], [])
        self.assertNotIn("demo_seed", biz)

    def test_hours_in_plain_words(self):
        h = onboarding.parse_hours("weekdays 7-5, sat 8-12")
        self.assertEqual((h["mon"], h["fri"], h["sat"], h["sun"]), (["07:00", "17:00"], ["07:00", "17:00"], ["08:00", "12:00"], None))
        self.assertEqual(onboarding.parse_hours("mon 9am-5pm")["mon"], ["09:00", "17:00"])
        for bad in ("whenever", "mon 18-17"):
            with self.assertRaises(onboarding.IntakeError):
                onboarding.parse_hours(bad)

    def test_bad_intakes_are_refused_with_a_reason(self):
        for over, reason in (({"owner_cell": "555"}, "10-digit"), ({"towns": []}, "town"), ({"shop_name": ""}, "name"),
                             ({"services": ["time_travel"]}, "Unknown service"),
                             ({"approved_tips": ["no colon here"]}, "should look like")):
            with self.assertRaises(onboarding.IntakeError, msg=str(over)) as cm:
                self.build(**over)
            self.assertIn(reason, str(cm.exception))

    def test_it_writes_the_file_and_the_checklist_and_will_not_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            shutil_dir = Path(d) / "businesses"
            shutil_dir.mkdir()
            (shutil_dir / "hvac.json").write_text(json.dumps(HVAC))
            cfg = {**CFG, "businesses_dir": str(shutil_dir), "clients_dir": str(Path(d) / "clients")}
            lines = []
            path = onboarding.run(cfg, {**self.INTAKE, "our_number": "208-555-0199"}, out=lines.append)
            self.assertEqual(json.loads(path.read_text())["phone_numbers"], ["+12085550199"])
            sheet = path.with_suffix(".checklist.txt").read_text()
            for needle in ("**61*2085550199**20#", "##004#", "TURN IT OFF", "TEST IT", "TURN IT ON"):
                self.assertIn(needle, sheet)
            with self.assertRaises(onboarding.IntakeError):
                onboarding.run(cfg, {**self.INTAKE, "our_number": "208-555-0199"}, out=lines.append)
            self.assertTrue(any("Heads up" in x for x in lines))     # the example owner cell, 555-0100, is a placeholder

    def test_a_555_01xx_owner_cell_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            bdir = Path(d)
            (bdir / "hvac.json").write_text(json.dumps(HVAC))
            lines = []
            onboarding.run({**CFG, "businesses_dir": str(bdir), "clients_dir": str(bdir / "clients")}, {**self.INTAKE, "owner_cell": "208-555-0142"}, out=lines.append)
            self.assertTrue(any("placeholder" in x for x in lines))
        with tempfile.TemporaryDirectory() as d2:
            (Path(d2) / "hvac.json").write_text(json.dumps(HVAC))
            lines = []
            onboarding.run({**CFG, "businesses_dir": d2, "clients_dir": d2 + "/clients"}, {**self.INTAKE, "owner_cell": "208-555-0200"}, out=lines.append)
            self.assertFalse(any("placeholder" in x for x in lines))   # a real-looking number raises no warning

    def test_the_checklist_never_contains_secrets_or_the_demo_shop(self):
        sheet = onboarding.checklist(self.build(), "+12085550199")
        for forbidden in ("RETELL", "DASHBOARD_TOKEN", "sk-", "Peak"):
            self.assertNotIn(forbidden, sheet)


class DashboardTests(unittest.TestCase):
    def store_with_a_call(self):
        store = Store(":memory:")
        now = schedule.local_now(HVAC, "2026-11-10T10:15")
        store.seed_demo(HVAC, now)
        store.start_call("c1", "hvac", "retell", "+12085550177", now)
        store.end_call("c1", [{"role": "agent", "text": "Peak Heating and Plumbing, the crew's out on a job right now. This is Riley."},
                              {"role": "caller", "text": "My furnace quit <script>alert(1)</script>"},
                              {"role": "agent", "text": "Silent turn", "tools": [{"name": "check_availability"}]}],
                       "booked", now)
        store._write("UPDATE calls SET summary=? WHERE id=?", (json.dumps({
            "summary": "Furnace quit; booked tomorrow morning.", "outcome": "booked", "intent": "emergency",
            "urgency": "emergency", "caller_name": "Jordan Lee", "follow_up": "Call before arriving.", "flags": []}), "c1"))
        store.add_appointment("hvac", "c1", "2026-11-11T07:00", "emergency_visit", "Jordan Lee", "+12085550177",
                              "482 Maple Street", "Sandpoint", "Furnace out", None, now)
        return store

    def test_each_call_shows_caller_reason_outcome_window_and_transcript(self):
        html = dashboard.render(HVAC, self.store_with_a_call())
        for needle in ("208-555-0177", "Jordan Lee", "Furnace quit; booked tomorrow morning.", "Emergency",
                       "booked", "Wednesday, November 11th, between 7 AM and 9 AM", "emergency_visit",
                       "<details><summary>Transcript</summary>", "the crew&#x27;s out on a job", "Call before arriving."):
            self.assertIn(needle, html)

    def test_it_works_on_a_phone_and_escapes_what_callers_say(self):
        html = dashboard.render(HVAC, self.store_with_a_call())
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', html)
        self.assertIn("@media (max-width: 640px)", html)
        self.assertIn('data-label="Confirmation"', html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn('http-equiv="refresh"', html)              # an open transcript must not collapse

    def test_an_empty_dashboard_still_renders(self):
        self.assertIn("Nothing yet.", dashboard.render(HVAC, Store(":memory:")))


class ToolDescriptionTests(unittest.TestCase):
    def test_tools_with_a_spoken_filler_tell_the_model_to_write_nothing_first(self):
        from receptionist.tools import QUIET_TOOLS, schemas
        described = {t["name"]: t["description"] for t in schemas(HVAC)}
        for name in QUIET_TOOLS:
            self.assertIn("without writing any words first", described[name], name)
        self.assertNotIn("without writing any words first", described["transfer_call"])   # it needs its own line
        self.assertNotIn("without writing any words first", described["end_call"])


class RetellSettingsTests(unittest.TestCase):
    def test_retells_duplicate_post_call_analysis_is_off_by_default_and_can_be_turned_back_on(self):
        from receptionist.retell_setup import agent_settings
        self.assertIsNone(agent_settings(HVAC)["post_call_analysis_model"])
        on = {**HVAC, "voice": {**HVAC["voice"], "post_call_analysis_model": "gpt-5.6-terra"}}
        self.assertEqual(agent_settings(on)["post_call_analysis_model"], "gpt-5.6-terra")


class HeatingSeasonTests(unittest.TestCase):
    """The season is decided in code, so the model is never left to ask how cold it is."""
    from datetime import datetime as _dt
    WINTER = _dt(2026, 12, 15, 19, 20)
    JULY = _dt(2026, 7, 15, 19, 20)
    NO_HEAT = "Hey. I ain't got no heat right now."

    def state(self, now):
        return prompts.call_state(HVAC, now, "+12085550100", None, [])

    def test_the_season_runs_from_october_15_to_april_15(self):
        for d, want in (((2026, 10, 14), False), ((2026, 10, 15), True), ((2027, 1, 3), True), ((2027, 4, 15), True),
                        ((2027, 4, 16), False), ((2026, 7, 15), False)):
            self.assertIs(schedule.in_heating_season(HVAC, __import__("datetime").date(*d)), want, d)
        self.assertFalse(schedule.in_heating_season({}, __import__("datetime").date(2026, 12, 1)))   # a shop with no season

    def test_call_state_says_which_season_it_is_in_winter_and_in_july(self):
        winter, july = self.state(self.WINTER), self.state(self.JULY)
        self.assertIn("It is heating season.", winter)
        self.assertIn("never asked about", winter)
        self.assertNotIn("not heating season", winter)
        self.assertIn("It is not heating season.", july)
        self.assertIn("routine furnace repair", july)

    def test_no_heat_needs_no_weather_word_in_season_but_does_out_of_season(self):
        in_season = safety.match("hvac", self.NO_HEAT, "en", heating_season=True)
        self.assertEqual(in_season[0], "no_heat_freezing")
        self.assertIsNone(safety.match("hvac", self.NO_HEAT, "en", heating_season=False))
        self.assertIsNone(safety.match("hvac", self.NO_HEAT, "en"))                   # the old call shape is unchanged
        self.assertEqual(safety.match("hvac", "My furnace is out and it's freezing.", "en")[0], "no_heat_freezing")
        self.assertEqual(safety.match("hvac", "No heat and I have a baby here.", "en")[0], "no_heat_freezing")

    def test_gas_and_the_other_first_tiers_still_win_in_season(self):
        self.assertEqual(safety.match("hvac", "I smell gas and I have no heat.", "en", heating_season=True)[0], "gas")
        self.assertEqual(safety.match("hvac", "No heat and a burst pipe flooding the basement", "en", heating_season=True)[0],
                         "flooding")

    def test_the_spanish_tier_follows_the_season_too(self):
        self.assertEqual(safety.match("hvac", "La calefacción no funciona", "es", heating_season=True)[0], "no_heat_freezing")
        self.assertIsNone(safety.match("hvac", "La calefacción no funciona", "es", heating_season=False))

    def test_a_contraction_does_not_hide_no_heat(self):
        """Found on the 1:45 AM live call: "My heat's out" matched nothing, so no safety line and no problem mode."""
        for said in ("My heat's out.", "my furnace's out", "Hey man. He's out. My heat's out.", "the boiler\u2019s dead", "heat's not working"):
            got = safety.match("hvac", said, "en", heating_season=True)
            self.assertIsNotNone(got, said)
            self.assertEqual(got[0], "no_heat_freezing", said)
        self.assertIsNone(safety.match("hvac", "My heat's out.", "en", heating_season=False))   # out of season it still needs a cold word
        self.assertEqual(safety.match("hvac", "My heat's out and it's freezing.", "en")[0], "no_heat_freezing")

    def test_the_service_is_not_defined_by_the_weather(self):
        emergency = [s for s in HVAC["services"] if s["id"] == "emergency_visit"][0]
        self.assertNotIn("freezing", emergency["name"])
        self.assertNotIn("freezing weather", (Path("receptionist/playbooks/hvac.md").read_text()))


class CallModeTests(unittest.TestCase):
    """Rolled back 2026-11-13: no call-mode lines. The receptionist's character is the same on every call."""
    def test_there_are_no_call_mode_lines_in_the_call_state_or_the_tool_notes(self):
        from datetime import datetime
        self.assertNotIn("Call mode", prompts.call_state(HVAC, datetime(2026, 11, 10, 10), None, None, []))
        self.assertFalse(hasattr(prompts, "MODE_EASY") or hasattr(prompts, "MODE_PROBLEM"))
        store = Store(":memory:")
        ctx = ToolContext(HVAC, store, datetime(2026, 11, 10, 10, 15), "+12085550100", "c-mode", None)
        for sid in ("emergency_visit", "furnace_repair", "hvac_tune_up"):
            self.assertNotIn("Call mode", run_tool(ctx, "check_availability", {"service_id": sid})[0]["note"], sid)

    def test_each_vertical_gets_its_own_example_reactions(self):
        lawn = prompts.stable_prompt(load_business(CFG, "lawn_care"))
        trades = prompts.stable_prompt(HVAC)
        self.assertIn("the old boiler, the crawl space, the third service call this winter", trades)
        self.assertNotIn("Aeration in January", trades)
        self.assertNotIn("three-hundred-foot driveway", trades)
        self.assertIn("three-hundred-foot driveway", lawn)
        self.assertNotIn("the old boiler", lawn)


class SimulateTopicTests(unittest.TestCase):
    """`simulate --for TOPIC` runs only the scenarios that test one kind of change."""
    def test_every_topic_names_real_scenarios_and_stays_small(self):
        from receptionist.simulate import list_scenarios
        real = {s["id"] for s in list_scenarios(CFG, HVAC)}
        for topic, ids in HVAC["simulate_for"].items():
            self.assertTrue(ids and set(ids) <= real, topic)
            self.assertLessEqual(len(ids), 4, topic)            # the point is a cheap run

    def test_the_no_heat_topic_covers_both_seasons_and_the_stressed_caller(self):
        from receptionist.simulate import topic_ids
        self.assertEqual(set(topic_ids(HVAC, ["no-heat"])),
                         {"no_heat_no_weather", "no_heat_no_weather_july", "no_heat_night", "stressed_no_heat"})

    def test_two_topics_are_joined_without_repeats_and_an_unknown_one_is_refused(self):
        from receptionist.simulate import topic_ids
        ids = topic_ids(HVAC, ["no-heat", "safety"])
        self.assertEqual(len(ids), len(set(ids)))
        with self.assertRaises(SystemExit) as e:
            topic_ids(HVAC, ["no-such-topic"])
        self.assertIn("Topics:", str(e.exception))

    def test_a_new_client_does_not_inherit_this_shops_scenario_names(self):
        import inspect
        self.assertIn('"simulate_for"', inspect.getsource(onboarding))


if __name__ == "__main__":
    unittest.main()
