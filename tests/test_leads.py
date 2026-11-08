"""The lead builder's checks (receptionist/leads.py): what is accepted, what is skipped and why, and that merging never
touches the leads already on the list."""
import unittest
from datetime import date

from receptionist import leads
from receptionist.config import load_config

TOWNS = ["Sandpoint", "Sagle", "Bonners Ferry"]
TODAY = date(2026, 11, 8)


def cand(**kw):
    base = {"name": "Pine Street Lawn Care", "phone": "(208) 555-0142".replace("0142", "7731"), "town": "Sandpoint",
            "type": "Lawn care service", "source": "https://example.com/pine-street"}
    return {**base, **kw}


class NormalizeTests(unittest.TestCase):
    def ok(self, **kw):
        lead, why = leads.normalize(cand(**kw), TOWNS, TODAY)
        self.assertIsNone(why, why)
        return lead

    def why(self, **kw):
        lead, why = leads.normalize(cand(**kw), TOWNS, TODAY)
        self.assertIsNone(lead)
        return why

    def test_a_good_candidate_becomes_a_lead(self):
        lead = self.ok()
        self.assertEqual(lead["phone"], "+12085557731")
        self.assertEqual((lead["type"], lead["town"], lead["stage"], lead["found"]),
                         ("Lawn care", "Sandpoint", "leads", "2026-11-08"))
        self.assertEqual(lead["id"], "pine-street-lawn-care-7731")
        self.assertNotIn("line", lead)   # never assumed

    def test_town_spelling_and_state_suffix(self):
        self.assertEqual(self.ok(town="sandpoint, Idaho")["town"], "Sandpoint")
        self.assertEqual(self.ok(town="Bonners Ferry, ID")["town"], "Bonners Ferry")

    def test_outside_the_area_is_skipped(self):
        self.assertIn("outside the service area", self.why(town="Spokane"))
        self.assertIn("outside the service area", self.why(town=""))

    def test_phone_must_be_a_real_us_number(self):
        self.assertIn("phone", self.why(phone=""))
        self.assertIn("phone", self.why(phone="555-7731"))
        self.assertIn("phone", self.why(phone="+44 20 7946 0958"))
        self.assertIn("placeholder", self.why(phone="208-555-0142"))
        self.assertEqual(self.ok(phone="1-208-555-7731")["phone"], "+12085557731")

    def test_a_source_page_is_required(self):
        self.assertIn("source", self.why(source=""))
        self.assertIn("source", self.why(source="google it"))

    def test_name_is_required(self):
        self.assertIn("name", self.why(name=" "))

    def test_trades(self):
        self.assertEqual(self.ok(name="Lakeview Grounds", type="Landscaper")["type"], "Landscaping")
        self.assertEqual(self.ok(name="Ridge Plowing", type="Snow removal service")["type"], "Snow removal")
        self.assertEqual(self.ok(name="North Idaho Lawn & Snow", type="")["type"], "Lawn care")
        self.assertEqual(self.ok(name="Evergreen", type="Landscaping and snow removal")["type"], "Landscaping")

    def test_other_trades_and_tree_only_are_out_of_niche(self):
        for name, kind in (("Acme Plumbing", "Plumber"), ("Big Timber Tree Service", "Tree service"),
                           ("Junkyard Dogs", "Salvage"), ("Bonner Heating", "HVAC contractor")):
            self.assertIn("not lawn care", self.why(name=name, type=kind), name)

    def test_line_type_is_kept_only_when_valid(self):
        self.assertEqual(self.ok(line="Landline")["line"], "landline")
        self.assertIn("line type", self.why(line="cell"))

    def test_extra_fields_are_kept_and_unknown_ones_dropped(self):
        lead = self.ok(contact="Dana Pike", notes="family run", stage="won", dnc=False, id="x")
        self.assertEqual((lead["contact"], lead["notes"], lead["stage"]), ("Dana Pike", "family run", "leads"))
        self.assertNotIn("dnc", lead)
        self.assertNotEqual(lead["id"], "x")


class MergeTests(unittest.TestCase):
    def test_appends_without_touching_existing_leads(self):
        old = [{"id": "me", "name": "Noah", "phone": "+12086105821", "stage": "booked", "notes": "keep", "test": True}]
        before = [dict(l) for l in old]
        merged, rep = leads.merge(old, [cand()], TOWNS, set(), TODAY)
        self.assertEqual(merged[0], before[0])
        self.assertEqual(len(merged), 2)
        self.assertEqual((rep["added"], rep["total"], rep["skipped"]), (1, 2, []))
        self.assertEqual(old, before)   # the input list is not changed either

    def test_known_numbers_and_do_not_call_are_skipped(self):
        old = [{"phone": "+12085557731", "name": "Already here"}]
        merged, rep = leads.merge(old, [cand(), cand(name="Other", phone="208-555-8800"),
                                        cand(name="Third", phone="208-555-9900")], TOWNS, {"+12085558800"}, TODAY)
        self.assertEqual(rep["added"], 1)
        self.assertEqual([m["name"] for m in merged], ["Already here", "Third"])
        self.assertEqual(len(rep["skipped"]), 2)
        self.assertTrue(all("already on the list" in s["why"] for s in rep["skipped"]))

    def test_duplicates_inside_one_batch_are_added_once(self):
        _, rep = leads.merge([], [cand(), cand(name="Same number, other name")], TOWNS, set(), TODAY)
        self.assertEqual(rep["added"], 1)

    def test_skips_report_their_reasons(self):
        _, rep = leads.merge([], [cand(town="Boise"), cand(source="")], TOWNS, set(), TODAY)
        self.assertEqual(rep["added"], 0)
        self.assertEqual(len(rep["skipped"]), 2)

    def test_service_towns_come_from_the_lawn_care_shop(self):
        towns = leads.service_towns(load_config())
        self.assertIn("Sandpoint", towns)
        self.assertEqual(len(towns), 24)
        self.assertEqual(leads.service_towns({"outbound": {"lead_towns": ["Sagle"]}}), ["Sagle"])


if __name__ == "__main__":
    unittest.main()
