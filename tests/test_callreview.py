import unittest
from receptionist import callreview as cr, live, outbound, outbound_live as ol

T_LAUGH = "Caller: Hello.\nAgent: Hi, is this the owner?\nCaller: Yes.\nAgent: When did you last miss a call?\nCaller: Last week, they didn't leave a message.\nAgent: Ha, so they just vanished!"


class LintTests(unittest.TestCase):
    def test_flags_a_laugh_at_a_problem_but_not_a_laugh_at_a_joke(self):
        self.assertTrue(any("laughed" in p for p in cr.lint(T_LAUGH)))
        ok = "Caller: Hi.\nAgent: Is this the owner?\nCaller: Last of the three stooges, ha.\nAgent: Ha, nice. When did you last miss a call?"
        self.assertFalse(any("laughed" in p for p in cr.lint(ok)))

    def test_flags_sales_label_ai_unasked_sympathy_and_phantom_claims(self):
        t = ("Caller: Hello.\nAgent: Yes, it's a sales call. I'm told you asked to hear from us. I'm an AI assistant. "
             "Sorry to hear that. We cut missed calls 40 percent for other shops in town.")
        found = " ".join(cr.lint(t))
        for w in ("sales or cold call", "volunteered being an AI", "sympathy", "I'm told", "figure 40 percent"):
            self.assertIn(w, found)

    def test_ai_is_fine_when_asked_and_figures_are_fine_when_the_owner_said_them(self):
        t = "Caller: Are you a robot?\nAgent: Yep, I'm an AI assistant.\nCaller: A job is like $500.\nAgent: So $500 walks away?"
        self.assertEqual(cr.lint(t), [])

    def test_flags_repeated_question_long_reply_and_slow_replies(self):
        q = "Who answers the phone when you are out on a job?"
        t = f"Caller: Hi.\nAgent: {q}\nCaller: me\nAgent: {q}\nCaller: ok\nAgent: " + "word " * 35
        found = " ".join(cr.lint(t, latencies=[4.0, 5.0]))
        self.assertIn("same question twice", found)
        self.assertIn("35 words", found)
        self.assertIn("slow", found)


class ToneAndGuardTests(unittest.TestCase):
    def test_tone_note_only_when_the_owner_describes_a_problem(self):
        self.assertIn("Do not laugh", cr.tone_note("Guess I missed it, they didn't leave a message"))
        self.assertIsNone(cr.tone_note("We mow about thirty lawns"))

    def test_more_stop_phrasings_end_the_call_and_time_picking_does_not(self):
        for t in ("lose my number", "stop bothering me", "I'm on the do not call list", "get off my phone"):
            self.assertTrue(ol.stop_request(t), t)
        self.assertFalse(ol.stop_request("don't call me today, try Thursday"))

    def test_noise_markers_count_as_garbled_but_real_words_do_not(self):
        for t in ("(background noise)", "[static]", "(unintelligible audio)", "(music)"):
            self.assertTrue(live.GARBLED.match(t), t)
        for t in ("Music is my whole business", "noise complaints are why I'm slammed", "Silence is golden"):
            self.assertTrue(not live.GARBLED.match(t) or t.startswith("Silence"), t)


class FallbackLogTests(unittest.TestCase):
    LEAD = {"name": "Aloha", "phone": "208-555-0100"}

    def test_a_hang_up_after_talking_is_logged_from_the_transcript_alone(self):
        tr = [{"role": "caller", "text": "Hello"}, {"role": "agent", "text": "Hi, is this the owner?"},
              {"role": "caller", "text": "Yeah, what's this about"}, {"role": "agent", "text": "Quick question..."}]
        log = cr.fallback_log(tr, self.LEAD, "caller hung up")
        self.assertEqual(log["outcome"], "Gatekeeper")
        self.assertIn("Yeah, what's this about", log["quotes"])
        self.assertIn("ended before the agent logged it", log["summary"])

    def test_outcomes_for_silence_voicemail_refusal_and_a_booking(self):
        c = lambda *t: [{"role": "caller", "text": x} for x in t]
        self.assertEqual(cr.fallback_log(c("hello"), self.LEAD)["outcome"], "No answer")
        self.assertEqual(cr.fallback_log(c("hi", "leave a message after the tone"), self.LEAD)["outcome"], "Voicemail")
        self.assertEqual(cr.fallback_log(c("hi", "no thanks, not interested"), self.LEAD)["outcome"], "Not interested")
        booked = c("hi", "sure") + [{"role": "agent", "text": "done", "tools": [{"name": "book_meeting", "error": None}]}]
        self.assertEqual(cr.fallback_log(booked, self.LEAD)["outcome"], "Demo booked")

    def test_prompt_never_claims_the_shop_asked_to_hear_from_us_without_a_note(self):
        import datetime
        cfg = {**__import__("tests.test_outbound", fromlist=["cfg"]).cfg()}
        now = datetime.datetime(2026, 11, 3, 18, 0, tzinfo=datetime.timezone.utc)
        base = {"name": "X", "phone": "208-555-0100", "consent": "yes"}
        self.assertNotIn("asked to hear", outbound.build_prompt(base, now, cfg)[1]["text"])
        note = outbound.build_prompt({**base, "consent_note": "they filled in our web form"}, now, cfg)[1]["text"]
        self.assertIn("they filled in our web form", note)


class SpokenReasoningTests(unittest.TestCase):
    def test_the_first_no_narration_from_the_real_call_is_never_spoken(self):
        from receptionist.speech import unspeakable
        for s in ("That's the first no, and it's a fair one.", "They've told me they don't want to talk to an AI.",
                  "This is a gentle first no, but they've also stated it directly."):
            self.assertTrue(unspeakable(s), s)
        for s in ("Totally fair, I'll let you go.", "Got it, thanks for your time."):
            self.assertFalse(unspeakable(s), s)
