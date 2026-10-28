"""Per-shop keys, headers, log redaction, and keeping real client files out of the repo (spec items A and B)."""
import json
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from receptionist import access, config, onboarding
from receptionist.config import load_config
from receptionist.server import create_app

CFG = load_config()
ADMIN, SECRET = "admin-key-" + "a" * 24, "server-secret-" + "b" * 24


class FakeClient:
    pass


class Env:
    """A server over a temporary data dir holding two real shops, 'north' and 'south', beside the repo's demo shops."""

    def start(self, **extra_env):
        self.tmp = tempfile.TemporaryDirectory()
        clients = Path(self.tmp.name) / "clients"
        clients.mkdir()
        template = json.loads((Path(CFG["businesses_dir"]) / "hvac.json").read_text())
        for i, name in enumerate(("north", "south")):
            biz = {**template, "id": name, "name": name.title(), "phone_numbers": [f"+1208555010{i}"],
                   "recording_notice": "This call is recorded.", "ai_disclosure_in_greeting": True}
            (clients / f"{name}.json").write_text(json.dumps(biz))
        self.cfg = {**CFG, "data_dir": self.tmp.name, "clients_dir": str(clients)}
        env = {"DASHBOARD_TOKEN": ADMIN, "TOKEN_SECRET": SECRET, **extra_env}
        self.patch = mock.patch.dict(os.environ, env)
        self.patch.start()
        self.http = TestClient(create_app(self.cfg, client=FakeClient(), warm_cache=False))

    def stop(self):
        self.patch.stop()
        self.tmp.cleanup()

    def key(self, shop, purpose="dashboard", version=1):
        return access.shop_key(SECRET, {"id": shop, "token_version": version}, purpose)


class DashboardKeys(Env, unittest.TestCase):
    def setUp(self):
        self.start()

    def tearDown(self):
        self.stop()

    def get(self, key, business, **kw):
        return self.http.get("/dashboard", params={"token": key, "business": business}, follow_redirects=False, **kw)

    def test_a_shop_key_opens_only_its_own_shop(self):
        self.assertEqual(self.get(self.key("north"), "north").status_code, 303)
        self.assertEqual(self.get(self.key("north"), "south").status_code, 404)

    def test_a_missing_or_wrong_business_is_a_404_for_a_shop_key(self):
        r = self.http.get("/dashboard", params={"token": self.key("north")}, follow_redirects=False)
        self.assertEqual(r.status_code, 404)       # no fallback to the default shop
        self.assertEqual(self.get(self.key("north"), "nope").status_code, 404)
        self.assertEqual(self.get("wrong", "north").status_code, 404)

    def test_the_admin_key_opens_any_shop_and_may_use_the_default(self):
        self.assertEqual(self.get(ADMIN, "south").status_code, 303)
        self.assertEqual(self.http.get("/dashboard", params={"token": ADMIN}, follow_redirects=False).status_code, 303)

    def test_the_link_key_becomes_a_cookie_and_leaves_the_address(self):
        client = TestClient(self.http.app)
        r = client.get("/dashboard", params={"token": self.key("north"), "business": "north"}, follow_redirects=False)
        self.assertNotIn("token", r.headers["location"])
        cookie = r.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        page = client.get(r.headers["location"])                 # the cookie now carries the key
        self.assertEqual(page.status_code, 200)
        self.assertEqual(client.get("/dashboard?business=south").status_code, 404)    # and only for its own shop

    def test_the_cookie_is_secure_behind_https(self):
        r = self.http.get("/dashboard", params={"token": ADMIN, "business": "north"}, follow_redirects=False,
                          headers={"x-forwarded-proto": "https"})
        self.assertIn("Secure", r.headers["set-cookie"])

    def test_bumping_token_version_breaks_only_that_shops_links(self):
        path = Path(self.cfg["clients_dir"]) / "north.json"
        biz = json.loads(path.read_text())
        old_n, old_s = self.key("north"), self.key("south")
        path.write_text(json.dumps({**biz, "token_version": 2}))
        self.assertEqual(self.get(old_n, "north").status_code, 404)
        self.assertEqual(self.get(self.key("north", version=2), "north").status_code, 303)
        self.assertEqual(self.get(old_s, "south").status_code, 303)

    def test_every_response_carries_the_privacy_headers(self):
        for r in (self.http.get("/dashboard?business=north"), self.get(self.key("north"), "north")):
            self.assertEqual(r.headers["referrer-policy"], "no-referrer")
            self.assertEqual(r.headers["cache-control"], "no-store")
            self.assertEqual(r.headers["x-robots-tag"], "noindex")
        ok = self.http.get("/dashboard?business=north", headers={"cookie": f"rk={self.key('north')}"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.headers["cache-control"], "no-store")

    def test_too_many_failures_stop_all_checking_from_that_address(self):
        for _ in range(access.FAILS_PER_MINUTE):
            self.assertEqual(self.get("wrong", "north").status_code, 404)
        self.assertEqual(self.get(self.key("north"), "north").status_code, 404)     # even the right key waits a minute

    def test_the_dashboard_is_off_without_the_admin_key_and_secret(self):
        self.stop()
        self.start(DASHBOARD_TOKEN="", TOKEN_SECRET="")
        self.assertEqual(self.get("", "north").status_code, 404)


class FeedKeys(Env, unittest.TestCase):
    def setUp(self):
        self.start()

    def tearDown(self):
        self.stop()

    def feed(self, key, business):
        return self.http.get("/calendar.ics", params={"token": key, "business": business})

    def test_a_feed_key_opens_only_its_own_feed(self):
        self.assertEqual(self.feed(self.key("north", "calendar"), "north").status_code, 200)
        self.assertEqual(self.feed(self.key("north", "calendar"), "south").status_code, 404)

    def test_feed_and_dashboard_keys_do_not_cross(self):
        self.assertEqual(self.feed(self.key("north", "dashboard"), "north").status_code, 404)
        r = self.http.get("/dashboard", params={"token": self.key("north", "calendar"), "business": "north"},
                          follow_redirects=False)
        self.assertEqual(r.status_code, 404)

    def test_the_feed_carries_the_privacy_headers(self):
        r = self.feed(self.key("north", "calendar"), "north")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertEqual(r.headers["x-robots-tag"], "noindex")


class LogRedaction(unittest.TestCase):
    def test_the_key_never_reaches_the_access_log(self):
        rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                ("1.2.3.4:5", "GET", "/dashboard?business=a&token=SECRETKEY&x=1", "1.1", 200), None)
        access.redact_access_log(rec)
        self.assertNotIn("SECRETKEY", rec.getMessage())
        self.assertIn("token=REDACTED&x=1", rec.getMessage())


class ClientFiles(Env, unittest.TestCase):
    def setUp(self):
        self.start()

    def tearDown(self):
        self.stop()

    def test_both_folders_load_and_a_duplicate_id_or_number_is_refused(self):
        ids = {b["id"] for b in config.all_businesses(self.cfg)}
        self.assertTrue({"north", "south", "hvac"} <= ids)
        clients = Path(self.cfg["clients_dir"])
        dup = json.loads((clients / "north.json").read_text())
        (clients / "again.json").write_text(json.dumps({**dup, "phone_numbers": []}))
        with self.assertRaises(ValueError):
            config.all_businesses(self.cfg)                 # same id in two files
        (clients / "again.json").write_text(json.dumps({**dup, "id": "again"}))
        with self.assertRaises(ValueError):
            config.all_businesses(self.cfg)                 # same phone number

    def test_load_business_finds_a_client_file(self):
        self.assertEqual(config.load_business(self.cfg, "south")["id"], "south")

    def test_new_client_writes_to_the_clients_folder_not_the_repo(self):
        intake = {**__import__("yaml").safe_load((Path(CFG["businesses_dir"]).parent / ".internal" / "intake.example.yaml").read_text()),
                  "shop_name": "Lakeview Heating", "our_number": "208-555-0177"}
        before = set(Path(CFG["businesses_dir"]).glob("*"))
        path = onboarding.run(self.cfg, intake, out=lambda *_: None)
        self.assertEqual(path.parent, Path(self.cfg["clients_dir"]))
        self.assertEqual(set(Path(CFG["businesses_dir"]).glob("*")), before)
        self.assertEqual(config.load_business(self.cfg, path.stem)["name"], "Lakeview Heating")

    def test_the_repo_ignores_the_clients_folder(self):
        self.assertIn("clients/", (Path(CFG["businesses_dir"]).parent / ".gitignore").read_text().split())

    def test_upload_needs_the_admin_key_and_a_good_file(self):
        biz = json.loads((Path(self.cfg["clients_dir"]) / "north.json").read_text())
        new = {**biz, "id": "west", "phone_numbers": ["+12085550142"]}
        self.assertEqual(self.http.put("/admin/clients", json=new).status_code, 404)
        self.assertEqual(self.http.put("/admin/clients", json=new, headers={"Authorization": "Bearer nope"}).status_code, 404)
        auth = {"Authorization": f"Bearer {ADMIN}"}
        self.assertEqual(self.http.put("/admin/clients", json={**new, "id": "../evil"}, headers=auth).status_code, 422)
        self.assertEqual(self.http.put("/admin/clients", json={**new, "phone_numbers": ["+12085550100"]}, headers=auth).status_code, 422)
        self.assertEqual(self.http.put("/admin/clients", json={"id": "broken"}, headers=auth).status_code, 422)
        self.assertEqual(self.http.put("/admin/clients", json=new, headers=auth).status_code, 200)
        self.assertEqual(config.load_business(self.cfg, "west")["id"], "west")
        self.assertEqual(self.http.put("/admin/clients", json={**new, "name": "West 2"}, headers=auth).status_code, 200)   # re-upload replaces
        self.assertEqual(self.http.put("/admin/clients", json={**new, "id": "hvac"}, headers=auth).status_code, 422)       # never a demo shop


class AdminKeyStaysOutOfFeeds(Env, unittest.TestCase):
    def test_the_admin_key_does_not_open_a_calendar_feed(self):
        self.start()
        try:
            self.assertEqual(self.http.get("/calendar.ics", params={"token": ADMIN, "business": "north"}).status_code, 404)
        finally:
            self.stop()
