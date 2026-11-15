"""Found on the 2026-11-14 23:29 live call: "My water heater is not working" fired the no-heat emergency line, and the
model then argued with its own system note out loud. A water heater is plumbing (no hot water), not a cold house."""
import unittest

import tests  # noqa: F401
from receptionist import safety


class WaterHeaterTests(unittest.TestCase):
    def test_a_water_heater_is_not_a_heating_emergency(self):
        for said in ("My water heater is not working.", "My hot water heater died.", "The water heater's broken.",
                     "Hey. My water heater is not working."):
            self.assertIsNone(safety.match("hvac", said, "en", heating_season=True), said)
        self.assertIsNone(safety.match("hvac", "Mi calentador de agua no funciona.", "es", heating_season=True))

    def test_a_heater_or_furnace_still_is(self):
        for said in ("My heater is not working.", "My furnace is out.", "My heat's out.", "I've got no heat."):
            self.assertEqual(safety.match("hvac", said, "en", heating_season=True)[0], "no_heat_freezing", said)
        self.assertEqual(safety.match("hvac", "Mi calentador no funciona.", "es", heating_season=True)[0], "no_heat_freezing")

    def test_a_water_heater_on_fire_is_still_an_emergency(self):
        self.assertIsNotNone(safety.match("hvac", "There's smoke coming from my water heater.", "en", heating_season=True))


class OwnerNotPickingUpTests(unittest.TestCase):
    """The playbook makes her say the owner isn't picking up; the old phrase filter dropped that whole reply, and the
    caller heard "Sorry, could you say that again?" over and over (owner_cell_no_answer, 2026-11-14)."""

    def test_the_required_line_is_speech(self):
        from receptionist.speech import unspeakable
        for said in ("The owner isn't picking up.", "The owner isn't answering right now.",
                     "The owner isn't available, but your message is saved."):
            self.assertFalse(unspeakable(said), said)

    def test_reasoning_about_the_owner_is_still_caught(self):
        from receptionist.speech import unspeakable
        for said in ("The owner isn't interested.", "The owner said no, so I'll move on."):
            self.assertTrue(unspeakable(said), said)


if __name__ == "__main__":
    unittest.main()
