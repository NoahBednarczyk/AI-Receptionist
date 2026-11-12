"""W1.1: when the model fails or stalls, the call becomes a code-driven message taker instead of a hang-up."""
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

import anthropic
import httpx

import tests  # noqa: F401
from receptionist import agent, schedule
from receptionist.agent import CallSession, make_client
from tests.test_logic import BIZ, CFG, TUE_1015, FakeClient, FakeStream, fresh_store, reply, text, use


def api_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


class DownClient(FakeClient):
    """Serves its script, then every call fails like an outage."""

    def stream(self, **kw):
        if not self.script:
            raise api_error()
        return super().stream(**kw)


class StallStream(FakeStream):
    def __iter__(self):
        time.sleep(3)   # the model has gone quiet
        return iter(())


class StallClient(FakeClient):
    def stream(self, **kw):
        self.requests.append(kw)
        return StallStream(None)


def take(client, store=None):
    return CallSession(client, CFG, BIZ, store or fresh_store(), TUE_1015, caller="+13125550177")


class MessageTakerTests(unittest.TestCase):
    def test_a_failed_model_call_ends_with_a_saved_urgent_message(self):
        s = take(DownClient())
        turn = s.respond("My sink is leaking.")
        self.assertIn("take a message", turn.text)
        self.assertFalse(turn.ended)
        self.assertIn("name", turn.text.lower())
        said = s.respond("Jordan Smith").text
        self.assertIn("Jordan", said)
        said = s.respond("three one two five five five zero one seven seven").text
        self.assertIn("312, 555, 0177", said)   # read back
        self.assertIn("what do you need", s.respond("yes that's right").text.lower())
        turn = s.respond("The kitchen sink is leaking under the cabinet")
        self.assertTrue(turn.ended)
        self.assertTrue(s.ctx.ended)
        [m] = s.store.for_call("messages", s.call_id)
        self.assertEqual((m["name"], m["phone"], m["urgent"]), ("Jordan Smith", "+13125550177", 1))
        self.assertIn("kitchen sink", m["message"])

    def test_a_short_number_is_asked_for_again_and_a_wrong_read_back_is_redone(self):
        s = take(DownClient())
        s.respond("hello")
        s.respond("it's Pat")
        self.assertIn("5 digits", s.respond("three one two five five").text)
        s.respond("three one two five five five zero one four two")
        again = s.respond("no, that's wrong").text
        self.assertIn("number", again)
        self.assertIn("555, 0199", s.respond("312 555 0199").text)

    def test_name_and_number_together_skip_a_step(self):
        s = take(DownClient())
        s.respond("hello")
        said = s.respond("This is Pat Jones, 312 555 0142").text
        self.assertIn("312, 555, 0142", said)

    def test_with_no_number_the_message_is_kept_with_the_caller_id(self):
        s = take(DownClient())
        for words in ("hi", "Pat", "uh", "I don't know it", "no idea", "burst valve"):
            turn = s.respond(words)
        self.assertTrue(turn.ended)
        [m] = s.store.for_call("messages", s.call_id)
        self.assertEqual(m["phone"], "+13125550177")   # the number the call came from

    def test_a_safety_line_comes_first_even_in_degraded_mode(self):
        s = take(DownClient())
        said = s.respond("My pipe just burst and water is everywhere!").text
        self.assertTrue(said.startswith("If it's safe, shut off the main water valve"))
        self.assertIn("message", said)
        # and again mid-flow: the line plays once, then the question they were on is repeated
        s2 = take(DownClient())
        s2.respond("hello")
        s2.respond("Pat")
        self.assertTrue(s2.respond("I smell gas, wait").ended)   # gas with the caller inside stays fully fixed

    def test_a_failure_after_something_was_saved_does_not_start_taking_a_message(self):
        msg = {"caller_name": "Pat", "callback_number": "3125550142", "message": "call me", "urgent": False}
        client = DownClient(reply("tool_use", use("t1", "take_message", msg)))
        s = take(client)
        turn = s.respond("Please leave a message, call me back.")
        self.assertTrue(turn.ended)
        self.assertEqual(len(s.store.for_call("messages", s.call_id)), 1)
        self.assertIn("saved", turn.text)

    def test_spanish_callers_get_spanish(self):
        s = take(DownClient())
        said = s.respond("Hola, tengo una fuga en la cocina, necesito ayuda por favor").text
        self.assertIn("mensaje", said)

    def test_the_outbound_style_subclass_keeps_its_own_failure_handling(self):
        class Sales(CallSession):
            pass
        self.assertFalse(Sales(DownClient(), CFG, BIZ, fresh_store(), TUE_1015).can_degrade)

    def test_the_client_has_explicit_timeouts_and_retries(self):
        c = make_client({"anthropic": {"timeout_s": 7, "connect_timeout_s": 2, "max_retries": 0}})
        self.assertEqual(c.max_retries, 0)
        self.assertEqual((c.timeout.read, c.timeout.connect), (7.0, 2.0))
        d = make_client(CFG)
        self.assertEqual(d.max_retries, 1)
        self.assertEqual(d.timeout.read, 20.0)


class StalledModelOnALiveCall(unittest.TestCase):
    def test_a_silent_model_hands_the_call_to_the_message_taker(self):
        from fastapi.testclient import TestClient
        from receptionist.server import create_app
        tmp = tempfile.TemporaryDirectory()
        os.environ.update(RETELL_WS_SECRET="s3cret", DASHBOARD_TOKEN="dash-admin-key-" + "x" * 16)
        try:
            cfg = {**CFG, "data_dir": tmp.name, "live": {**CFG.get("live", {}), "model_timeout_s": 0.4}}
            app = create_app(cfg, client=StallClient(), warm_cache=False)
            with TestClient(app).websocket_connect("/llm-websocket/s3cret/stall1") as ws:
                ws.receive_json()
                ws.send_json({"interaction_type": "call_details", "call": {
                    "call_id": "stall1", "from_number": "+13125550177", "to_number": "+13125550100"}})
                ws.receive_json()
                ws.send_json({"interaction_type": "response_required", "response_id": 1, "transcript": [
                    {"role": "agent", "content": BIZ["greeting"]}, {"role": "user", "content": "My sink is leaking."}]})
                parts, final = [], None
                while final is None:
                    m = ws.receive_json()
                    if m.get("response_type") == "response" and m.get("response_id") == 1:
                        parts.append(m["content"])
                        final = m if m["content_complete"] else None
                said = "".join(parts)
                self.assertIn("take a message", said)
                self.assertNotIn("end_call", final)   # the call goes on; the caller is not hung up on
        finally:
            for k in ("RETELL_WS_SECRET", "DASHBOARD_TOKEN"):
                os.environ.pop(k, None)
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
