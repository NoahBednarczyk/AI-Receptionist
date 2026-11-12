"""W1.4: backups and restore. Nothing here talks to a real bucket."""
import datetime as dt
import hashlib
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401
from receptionist import backup
from receptionist.store import Store

NOW = dt.datetime(2026, 11, 12, 8, 0, 0, tzinfo=dt.timezone.utc)


def make_data(root: Path) -> Path:
    data = root / "data"
    (data / "clients").mkdir(parents=True)
    store = Store(str(data / "receptionist.sqlite"))
    store.add_message("shop", "c1", "Pat", "+13125550142", "call me", "office", True, NOW)
    store.db.close()
    (data / "clients" / "shop.json").write_text('{"id": "shop"}')
    (data / "outbound.json").write_text('{"do_not_call": ["+12085550100"]}')
    return data


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = make_data(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_archive_holds_the_database_the_shop_files_and_the_do_not_call_list(self):
        path = backup.make_archive(self.data, self.root / "out", NOW)
        self.assertEqual(path.name, "receptionist-20261112-080000.tar.gz")
        rep = backup.inspect(path)
        self.assertEqual(rep["integrity"], "ok")
        self.assertEqual(rep["tables"]["messages"], 1)
        for name in ("receptionist.sqlite", "clients/shop.json", "outbound.json", "MANIFEST.json"):
            self.assertIn(name, rep["files"])

    def test_a_backup_made_while_the_server_holds_the_database_open_is_consistent(self):
        live = sqlite3.connect(self.data / "receptionist.sqlite")
        live.execute("BEGIN IMMEDIATE")
        live.execute("INSERT INTO health (id, checked_at) VALUES (9, 'x')")   # an uncommitted write in flight
        try:
            path = backup.make_archive(self.data, self.root / "out", NOW)
        finally:
            live.rollback()
            live.close()
        self.assertEqual(backup.inspect(path)["integrity"], "ok")

    def test_dry_run_changes_nothing_and_a_restore_keeps_what_it_replaces(self):
        path = backup.make_archive(self.data, self.root / "out", NOW)
        (self.data / "outbound.json").write_text('{"do_not_call": []}')   # the disk went wrong afterwards
        rep = backup.restore(path, self.data)
        self.assertFalse(rep["applied"])
        self.assertIn("outbound.json", rep["would_replace"])
        self.assertEqual((self.data / "outbound.json").read_text(), '{"do_not_call": []}')
        backup.restore(path, self.data, apply=True)
        self.assertEqual(json.loads((self.data / "outbound.json").read_text())["do_not_call"], ["+12085550100"])
        self.assertEqual((self.data / "outbound.json.before-restore").read_text(), '{"do_not_call": []}')
        db = sqlite3.connect(self.data / "receptionist.sqlite")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        db.close()

    def test_an_archive_with_a_corrupt_database_is_refused(self):
        bad = self.root / "bad.tar.gz"
        junk = self.root / "receptionist.sqlite"
        junk.write_bytes(b"not a database" * 100)
        with tarfile.open(bad, "w:gz") as tar:
            tar.add(junk, arcname="receptionist.sqlite")
        with self.assertRaises(Exception):
            backup.restore(bad, self.data)

    def test_the_command_line_restore_is_a_dry_run_unless_told(self):
        import subprocess, sys
        path = backup.make_archive(self.data, self.root / "out", NOW)
        out = subprocess.run([sys.executable, "-m", "receptionist", "restore", "--from", str(path), "--dry-run",
                              "--to", str(self.data)], capture_output=True, text=True, cwd=Path(__file__).parent.parent)
        self.assertIn("Nothing was changed", out.stdout, out.stderr)


class SigningTests(unittest.TestCase):
    def test_signature_matches_amazons_published_example(self):
        # https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html (GET Object with a Range header)
        h = backup.sigv4_headers(
            "GET", "https://examplebucket.s3.amazonaws.com/test.txt", "us-east-1", "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", hashlib.sha256(b"").hexdigest(),
            dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc), {"Range": "bytes=0-9"})
        self.assertIn("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41", h["Authorization"])


class FakeResponse:
    def __init__(self, body=b""):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


class FakeS3:
    def __init__(self):
        self.objects, self.calls = {}, []

    def __call__(self, req, timeout=0):
        path = req.full_url.split("/bucket/", 1)[-1]
        self.calls.append((req.get_method(), path))
        if req.get_method() == "PUT":
            self.objects[path] = req.data
            return FakeResponse()
        if req.get_method() == "DELETE":
            self.objects.pop(path, None)
            return FakeResponse()
        keys = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in sorted(self.objects))
        return FakeResponse(f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{keys}</ListBucketResult>'.encode())


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = make_data(Path(self.tmp.name))
        self.s3 = FakeS3()
        self.bucket = backup.Bucket("https://s3.example.com", "us-east-1", "bucket", "KEY", "SECRET", opener=self.s3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upload_then_thirty_day_pruning(self):
        old = "backups/receptionist-20261001-080000.tar.gz"
        recent = "backups/receptionist-20261110-080000.tar.gz"
        self.s3.objects.update({old: b"x", recent: b"y", "backups/other.txt": b"z"})
        status = backup.run_backup(self.data, self.bucket, NOW, keep_days=30)
        self.assertTrue(status["ok"] and status["uploaded"], status)
        self.assertIn("backups/receptionist-20261112-080000.tar.gz", self.s3.objects)
        self.assertNotIn(old, self.s3.objects)
        self.assertIn(recent, self.s3.objects)
        self.assertIn("backups/other.txt", self.s3.objects)   # only our own archives are ever deleted
        self.assertTrue((self.data / "backup_status.json").exists())

    def test_a_failing_upload_is_reported_not_raised(self):
        def boom(req, timeout=0):
            raise OSError("bucket unreachable")
        bad = backup.Bucket("https://s3.example.com", "us-east-1", "bucket", "K", "S", opener=boom)
        status = backup.run_backup(self.data, bad, NOW)
        self.assertFalse(status["ok"])
        self.assertIn("unreachable", status["error"])

    def test_no_bucket_means_no_upload_and_the_env_decides(self):
        self.assertIsNone(backup.Bucket.from_env({}))
        b = backup.Bucket.from_env({"BACKUP_S3_BUCKET": "b", "BACKUP_S3_KEY_ID": "k", "BACKUP_S3_SECRET": "s",
                                    "BACKUP_S3_ENDPOINT": "https://x.example.com", "BACKUP_S3_REGION": "r"})
        self.assertEqual((b.bucket, b.region), ("b", "r"))


if __name__ == "__main__":
    unittest.main()
