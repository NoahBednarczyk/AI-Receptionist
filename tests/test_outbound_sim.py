"""The simulated-owner harness: its owners cover the six kinds of no, its checks catch what they claim to, and a whole
call runs end to end against a fake model (no spend)."""
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import tests  # noqa: F401
from receptionist import outbound, outbound_live as ol, outbound_sim as sim
from tests.test_logic import CFG, FakeClient, reply, text

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


def owner(id_):
    return next(o for o in sim.OWNERS if o["id"] == id_)


def made(t):
    return reply("end_turn", text(t))


def graded(passes):
    body = json.dumps({"items": [{"index": i, "pass": p, "evidence": "e"} for i, p in enumerate(passes, 1)]})
    return reply("end_turn", text(body))


class CoverageTests(unittest.TestCase):
    def test_every_kind_of_no_and_every_trade_has_an_owner(self):
        self.assertEqual({o["kind"] for o in sim.OWNERS if o["kind"]} & set(ol.KINDS), set(ol.KINDS))
        self.assertEqual({o["trade"] for o in sim.OWNERS}, {"hvac", "plumbing", "electrical", "lawn", "snow"})
        ids = [o["id"] for o in sim.OWNERS]
        self.assertEqual(len(ids), len(set(ids)))
        for needed in ("plain_yes", "calm_no", "irritated_no", "are_you_a_robot", "dont_want_ai", "busy_call_later", "five_nos", "stop_request"):
            self.assertIn(needed, ids)

    def test_an_owner_who_wants_a_yes_names_a_real_rung(self):
        for o in sim.OWNERS:
            self.assertIn(o.get("yes"), (None, *"abcdef"), o["id"])

    def test_the_owner_never_asks_to_stop_unless_the_persona_says_so(self):
        for o in sim.OWNERS:
            if not o.get("stop"):
                self.assertFalse(ol.stop_request(o["persona"].split("'")[1] if "'" in o["persona"] else ""), o["id"])


class CheckTests(unittest.TestCase):
    def call(self, who, agent_lines, rungs=(), refusals=(), turns=None):
        transcript = [{"role": "caller", "text": "Hello?"}] + [{"role": "agent", "text": t, "steps": [{"say": t}]} for t in agent_lines]
        session = SimpleNamespace(_rungs=tuple(rungs), _refusals=tuple(refusals))
        base = dict(owner="x", reply=agent_lines[-1], refused=False, kind=None, ended_by_code=False, stop_words=False, last_rung_refused=False)
        return {c["check"]: c for c in sim.check_call(owner(who), session, transcript, turns or [base], "Open times: Tuesday at 4:30 PM.", "a demo shop that answers as Peak Heating and Plumbing")}

    def test_a_clean_call_passes_everything(self):
        got = self.call("plain_yes", ["Fifteen minutes with Noah, 4:30 PM or later, which is easier?"], refusals=())
        self.assertTrue(all(c["pass"] for c in got.values()), [c for c in got.values() if not c["pass"]])

    def test_catches_another_trades_words(self):
        self.assertFalse(self.call("plain_yes", ["Ever miss a call on the mower?"])["trade_words"]["pass"])
        self.assertTrue(self.call("plain_yes", ["Try our demo line, it's a demo shop that answers as Peak Heating and Plumbing."])["trade_words"]["pass"])

    def test_catches_a_long_reply(self):
        self.assertFalse(self.call("plain_yes", [" ".join(["word"] * 31)])["reply_length"]["pass"])

    def test_catches_the_words_that_name_the_call(self):
        for w in ("a sales call", "a cold call", "my pitch", "selling"):
            self.assertFalse(self.call("plain_yes", [f"This isn't {w}."])["names_the_call"]["pass"], w)

    def test_catches_an_invented_time(self):
        self.assertFalse(self.call("plain_yes", ["How about 7:45 AM?"])["invented_time"]["pass"])
        self.assertTrue(self.call("plain_yes", ["How about 4:30 PM?"])["invented_time"]["pass"])

    def test_catches_two_questions_after_a_no(self):
        t = [dict(owner="no", reply="Ten minutes? Or a call from Noah?", refused=True, kind="no_need", ended_by_code=False, stop_words=False, last_rung_refused=False)]
        self.assertFalse(self.call("no_need", ["Ten minutes? Or a call from Noah?"], turns=t, refusals=("no_need",))["one_ask_after_no"]["pass"])

    def test_catches_a_repeated_rung(self):
        self.assertFalse(self.call("plain_yes", ["x"], rungs=(0, 1, 1))["rung_repeats"]["pass"])
        self.assertTrue(self.call("plain_yes", ["x"], rungs=(0, 1, 2))["rung_repeats"]["pass"])

    def test_catches_a_no_that_was_not_answered(self):
        t = [dict(owner="can't afford it", reply="Fifteen minutes Tuesday?", refused=True, kind="no_money", ended_by_code=False, stop_words=False, last_rung_refused=False)]
        self.assertFalse(self.call("no_money", ["Fifteen minutes Tuesday?"], turns=t, refusals=("no_money",))["answers_the_no"]["pass"])
        t[0]["reply"] = "It's free for fourteen days. Ten minutes with Noah?"
        self.assertTrue(self.call("no_money", ["It's free for fourteen days."], turns=t, refusals=("no_money",))["answers_the_no"]["pass"])

    def test_catches_a_call_ended_by_code_on_a_no_but_allows_the_two_endings(self):
        bad = [dict(owner="no thanks", reply="Bye", refused=True, kind="unclear", ended_by_code=True, stop_words=False, last_rung_refused=False)]
        self.assertFalse(self.call("calm_no", ["Bye"], turns=bad, refusals=("unclear",))["ended_on_a_no"]["pass"])
        stop = [dict(owner="stop calling me", reply="Bye", refused=False, kind=None, ended_by_code=True, stop_words=True, last_rung_refused=False)]
        self.assertTrue(self.call("stop_request", ["Bye"], turns=stop)["ended_on_a_no"]["pass"])
        last = [dict(owner="no", reply="Bye", refused=False, kind=None, ended_by_code=True, stop_words=False, last_rung_refused=True)]
        self.assertTrue(self.call("calm_no", ["Bye"], turns=last)["ended_on_a_no"]["pass"])

    def test_catches_a_stop_request_that_did_not_end_the_call(self):
        late = [dict(owner="stop calling me", reply="Sure but one thing", refused=False, kind=None, ended_by_code=False, stop_words=True, last_rung_refused=False)]
        self.assertFalse(self.call("stop_request", ["Sure but one thing"], turns=late)["hard_stop"]["pass"])

    def test_catches_a_misclassified_no(self):
        self.assertFalse(self.call("no_money", ["x"], refusals=("no_need",))["refusal_kind"]["pass"])
        self.assertTrue(self.call("no_money", ["x"], refusals=("no_need", "no_money"))["refusal_kind"]["pass"])


class EndToEndTests(unittest.TestCase):
    def test_a_stop_request_call_ends_by_code_and_passes(self):
        client = FakeClient(created=[made("Yeah this is Dale."), made("Stop calling me. Take me off your list.")])
        r = sim.run_owner(client, CFG, owner("stop_request"), NOW, grade_it=False)
        self.assertEqual((r["ended_by"], r["yes_rung"]), ("code", None))
        self.assertTrue(r["passed"], [c for c in r["checks"] if not c["pass"]])
        self.assertEqual(client.requests, [])   # no agent model call at all: code spoke every line

    def test_a_call_that_books_reports_the_rung_and_is_graded(self):
        client = FakeClient(made("Fifteen minutes with Noah, which is easier?"), created=[made("Yeah, Dale."), made("About four a week, I guess."), graded([True] * len(sim.OUTBOUND_RUBRIC))])
        r = sim.run_owner(client, CFG, owner("plain_yes"), NOW, max_turns=3, grade_it=True)
        self.assertEqual(r["rungs"], ["a"])
        self.assertTrue(all(x["pass"] for x in r["rubric"]))
        self.assertGreater(r["minutes"], 0)

    def test_a_failed_check_skips_the_grader_so_it_costs_nothing(self):
        client = FakeClient(made("This isn't a sales call, just curious."), created=[made("Yeah, Dale."), made("Okay.")])
        r = sim.run_owner(client, CFG, owner("plain_yes"), NOW, max_turns=3, grade_it=True)
        self.assertFalse(r["passed"])
        self.assertTrue(all(x["pass"] is None for x in r["rubric"]))
        self.assertEqual(len(client.create_requests), 2)


class BudgetTests(unittest.TestCase):
    def test_a_run_that_does_not_fit_refuses_and_says_how_many_would(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = {**CFG, "data_dir": d}
            with self.assertRaises(SystemExit) as e:
                sim.check_affordable({**cfg, "simulate": {**cfg["simulate"], "budget_per_day": 0.10}}, 12)
            self.assertIn("raise today's limit", str(e.exception))
            self.assertAlmostEqual(sim.check_affordable({**cfg, "simulate": {**cfg["simulate"], "budget_per_day": 5.0}}, 12), 0.72, places=2)


class SummaryTests(unittest.TestCase):
    def test_the_summary_reports_length_and_which_rung_each_yes_came_from(self):
        rows = [dict(id="a", passed=True, turns=6, minutes=1.0, refusals=[], rungs=["a"], yes_rung="a", wanted_yes="a", ended_by="model", checks=[], rubric=[]),
                dict(id="b", passed=True, turns=10, minutes=2.0, refusals=["no_need"], rungs=["a", "b", "c"], yes_rung="c", wanted_yes="c", ended_by="model", checks=[], rubric=[])]
        s = sim.summary(rows)
        self.assertIn("Average call: 1.50 min over 8.0 turns", s)
        self.assertIn("Yes by rung: a: 1 (a), c: 1 (b)", s)


if __name__ == "__main__":
    unittest.main()
