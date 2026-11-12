"""W1.3: /health/deep is what an uptime monitor polls."""
import tempfile
import unittest

import tests  # noqa: F401
from fastapi.testclient import TestClient

from receptionist.server import create_app
from tests.test_logic import CFG, FakeClient


class DeepHealth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {**CFG, "data_dir": self.tmp.name}
        self.app = create_app(self.cfg, client=FakeClient(), warm_cache=False)
        self.http = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_healthy_after_a_start(self):
        r = self.http.get("/health/deep")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "database": True, "model": True}))
        self.assertEqual(self.http.get("/health").json(), {"ok": True})

    def test_a_database_that_refuses_writes_is_a_503(self):
        import receptionist.store as st

        def broken(self, now_iso):
            raise OSError("disk full")
        orig, st.Store.write_check = st.Store.write_check, broken
        try:
            r = self.http.get("/health/deep")
        finally:
            st.Store.write_check = orig
        self.assertEqual((r.status_code, r.json()["database"]), (503, False))

    def test_a_model_that_has_not_answered_for_too_long_is_a_503(self):
        app = create_app({**self.cfg, "health": {"model_max_age_s": -1}}, client=FakeClient(), warm_cache=False)
        r = TestClient(app).get("/health/deep")
        self.assertEqual((r.status_code, r.json()["model"]), (503, False))

    def test_the_answer_names_no_shop(self):
        self.assertNotIn("acme", self.http.get("/health/deep").text.lower())


if __name__ == "__main__":
    unittest.main()
