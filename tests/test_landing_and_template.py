import json, tempfile, unittest
from pathlib import Path
from fastapi.testclient import TestClient
from receptionist import onboarding, server

ROOT = Path(__file__).resolve().parent.parent


class LandingAndTemplate(unittest.TestCase):
    def test_landing_describes_the_shop_that_holds_the_demo_number(self):
        cfg = json.loads((ROOT / "config.json").read_text())
        cfg["data_dir"] = tempfile.mkdtemp()
        text = TestClient(server.create_app(cfg, client=object(), warm_cache=False)).get("/").text
        self.assertIn("Peak Heating and Plumbing", text)
        self.assertNotIn("{{DEMO_SHOP}}", text)
        self.assertNotIn("lawn care and snow shop, and it tells", text)

    def test_default_agent_name_follows_the_template(self):
        for name in ("hvac", "lawn_care"):
            tpl = json.loads((ROOT / "businesses" / f"{name}.json").read_text())
            biz = onboarding.build({"shop_name": "Test Shop", "owner_email": "o@example.com", "owner_cell": "208-555-0101", "towns": "Sandpoint"}, tpl)
            self.assertEqual(biz["agent_name"], tpl["agent_name"])
            self.assertNotEqual(biz["agent_name"], "Sam")


if __name__ == "__main__":
    unittest.main()
