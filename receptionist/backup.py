"""Backups: the database and the shops' files in one timestamped archive, uploaded off the server.

    python -m receptionist backup [--local-only]        make an archive now (and upload it when a bucket is set)
    python -m receptionist restore --from FILE --dry-run   look inside an archive; nothing is changed
    python -m receptionist restore --from FILE --yes       put it back (the current files are kept as *.before-restore)

The server runs it twice a day when the bucket is configured. Any S3-compatible bucket works (Backblaze B2, Cloudflare R2,
AWS S3, Wasabi). Everything comes from environment variables, never the repo:

    BACKUP_S3_ENDPOINT   e.g. https://s3.us-west-004.backblazeb2.com   (omit for AWS: https://s3.<region>.amazonaws.com)
    BACKUP_S3_REGION     e.g. us-west-004
    BACKUP_S3_BUCKET     the bucket name
    BACKUP_S3_KEY_ID / BACKUP_S3_SECRET   an application key limited to that bucket
    BACKUP_KEEP_DAYS     optional, default 30

Render also snapshots the disk every 24 hours and keeps seven days; this is the longer, off-the-box copy.
Calls hold callers' names and numbers: use a private bucket, and a bucket-scoped key.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

log = logging.getLogger("receptionist")

DB_NAME = "receptionist.sqlite"
# What else on the disk must survive: the shops' files, and the outbound do-not-call list and attempts.
EXTRA_FILES = ["outbound.json", "outbound_leads.json", "retell.json", "retell_outbound.json"]
PREFIX = "backups/"
NAME = re.compile(r"receptionist-(\d{8}-\d{6})\.tar\.gz$")


# ---- the archive -------------------------------------------------------------
def make_archive(data_dir: str | Path, out_dir: str | Path, now: dt.datetime | None = None) -> Path:
    """One tar.gz: a consistent copy of the database (SQLite's own backup API, safe while the server writes), the
    shops' files under clients/, and the small state files. Returns its path."""
    data, now = Path(data_dir), now or dt.datetime.now(dt.timezone.utc)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"receptionist-{now:%Y%m%d-%H%M%S}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / DB_NAME
        src = sqlite3.connect(f"file:{data / DB_NAME}?mode=ro", uri=True) if (data / DB_NAME).exists() else None
        if src is not None:
            dst = sqlite3.connect(copy)
            with dst:
                src.backup(dst)
            dst.close()
            src.close()
        with tarfile.open(path, "w:gz") as tar:
            if copy.exists():
                tar.add(copy, arcname=DB_NAME)
            if (data / "clients").is_dir():
                tar.add(data / "clients", arcname="clients")
            for name in EXTRA_FILES:
                if (data / name).is_file():
                    tar.add(data / name, arcname=name)
            manifest = json.dumps({"made_at": now.isoformat(), "format": 1}).encode()
            info = tarfile.TarInfo("MANIFEST.json")
            info.size = len(manifest)
            tar.addfile(info, io.BytesIO(manifest))
    return path


def inspect(archive: str | Path) -> dict:
    """What is inside an archive, without touching anything: files, and per-table row counts of the database copy
    (after an integrity check)."""
    report: dict = {"files": [], "tables": {}, "integrity": None, "made_at": None}
    with tarfile.open(archive) as tar, tempfile.TemporaryDirectory() as tmp:
        for m in tar.getmembers():
            if m.isfile():
                report["files"].append(m.name)
        names = tar.getnames()
        if "MANIFEST.json" in names:
            report["made_at"] = json.load(tar.extractfile("MANIFEST.json")).get("made_at")
        if DB_NAME in names:
            tar.extract(DB_NAME, tmp, filter="data")
            db = sqlite3.connect(Path(tmp) / DB_NAME)
            report["integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                report["tables"][table] = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            db.close()
    return report


def restore(archive: str | Path, data_dir: str | Path, apply: bool = False) -> dict:
    """Dry run unless `apply`: then every file in the archive replaces the one on disk, and what was there is kept
    beside it as <name>.before-restore. Refuses an archive whose database fails its integrity check."""
    rep = inspect(archive)
    data = Path(data_dir)
    rep["would_replace"] = [n for n in rep["files"] if n != "MANIFEST.json" and (data / n).exists()]
    if rep["integrity"] not in (None, "ok"):
        raise ValueError(f"the database in this archive failed its integrity check: {rep['integrity']}")
    rep["applied"] = False
    if not apply:
        return rep
    with tarfile.open(archive) as tar:
        for top in {n.split("/")[0] for n in rep["files"] if n != "MANIFEST.json"}:
            target = data / top
            if target.exists():
                keep = target.with_name(target.name + ".before-restore")
                if keep.exists():
                    shutil.rmtree(keep) if keep.is_dir() else keep.unlink()
                target.rename(keep)
        members = [m for m in tar.getmembers() if m.name != "MANIFEST.json"]
        tar.extractall(data, members=members, filter="data")
    rep["applied"] = True
    return rep


# ---- S3-compatible storage, signed with AWS Signature V4 (no extra dependency) ----------------------------------------
def sigv4_headers(method: str, url: str, region: str, key_id: str, secret: str, payload_hash: str,
                  now: dt.datetime, extra: dict | None = None) -> dict:
    """The headers (Authorization, x-amz-date, x-amz-content-sha256, plus `extra`) that sign one S3 request."""
    u = urllib.parse.urlsplit(url)
    stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {"host": u.netloc, "x-amz-content-sha256": payload_hash, "x-amz-date": stamp,
               **{k.lower(): v.strip() for k, v in (extra or {}).items()}}
    signed = ";".join(sorted(headers))
    canon_query = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
                           for k, v in sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True)))
    canonical = "\n".join([method, urllib.parse.quote(u.path or "/", safe="/-_.~"), canon_query,
                           "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed, payload_hash])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])

    def h(k: bytes, msg: str) -> bytes:
        return hmac.new(k, msg.encode(), hashlib.sha256).digest()

    k = h(h(h(h(("AWS4" + secret).encode(), day), region), "s3"), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k_: v for k_, v in headers.items() if k_ != "host"}
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return out


class Bucket:
    def __init__(self, endpoint: str, region: str, bucket: str, key_id: str, secret: str, opener=None):
        self.endpoint, self.region, self.bucket = endpoint.rstrip("/"), region, bucket
        self.key_id, self.secret = key_id, secret
        self.opener = opener or urllib.request.urlopen

    @classmethod
    def from_env(cls, env=None) -> "Bucket | None":
        env = env if env is not None else os.environ
        if not (env.get("BACKUP_S3_BUCKET") and env.get("BACKUP_S3_KEY_ID") and env.get("BACKUP_S3_SECRET")):
            return None
        region = env.get("BACKUP_S3_REGION", "us-east-1")
        return cls(env.get("BACKUP_S3_ENDPOINT") or f"https://s3.{region}.amazonaws.com", region,
                   env["BACKUP_S3_BUCKET"], env["BACKUP_S3_KEY_ID"], env["BACKUP_S3_SECRET"])

    def _request(self, method: str, key: str = "", query: str = "", body: bytes = b"") -> bytes:
        path = f"/{self.bucket}/{urllib.parse.quote(key, safe='/-_.~')}" if key else f"/{self.bucket}"
        url = self.endpoint + path + (f"?{query}" if query else "")
        headers = sigv4_headers(method, url, self.region, self.key_id, self.secret, hashlib.sha256(body).hexdigest(),
                                dt.datetime.now(dt.timezone.utc))
        req = urllib.request.Request(url, method=method, data=body if method == "PUT" else None, headers=headers)
        with self.opener(req, timeout=120) as resp:
            return resp.read()

    def put(self, key: str, data: bytes) -> None:
        self._request("PUT", key, body=data)

    def get(self, key: str) -> bytes:
        return self._request("GET", key)

    def delete(self, key: str) -> None:
        self._request("DELETE", key)

    def list(self, prefix: str) -> list[str]:
        raw = self._request("GET", "", query=f"list-type=2&prefix={urllib.parse.quote(prefix, safe='')}")
        root = ET.fromstring(raw)
        return [el.text for el in root.iter() if el.tag.endswith("Key") and el.text]


def prune(bucket: Bucket, keep_days: int, now: dt.datetime) -> list[str]:
    """Deletes archives older than `keep_days` (by the timestamp in the name). Returns what it deleted."""
    gone = []
    for key in bucket.list(PREFIX):
        m = NAME.search(key)
        if not m:
            continue
        made = dt.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=dt.timezone.utc)
        if now - made > dt.timedelta(days=keep_days):
            bucket.delete(key)
            gone.append(key)
    return gone


def run_backup(data_dir: str | Path, bucket: Bucket | None, now: dt.datetime | None = None,
               keep_days: int = 30, local_dir: str | Path | None = None) -> dict:
    """Make the archive, upload it, prune old ones. Writes data/backup_status.json either way. Never raises."""
    now = now or dt.datetime.now(dt.timezone.utc)
    status = {"at": now.isoformat(), "ok": False, "uploaded": False, "error": None}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = make_archive(data_dir, local_dir or tmp, now)
            status["bytes"] = path.stat().st_size
            if bucket is not None:
                bucket.put(PREFIX + path.name, path.read_bytes())
                status["uploaded"] = True
                status["pruned"] = len(prune(bucket, keep_days, now))
            elif local_dir is None:
                status["error"] = "no bucket configured: the archive was made and discarded"
            status["ok"] = status["uploaded"] or local_dir is not None
            if local_dir is not None:
                status["file"] = str(path)
    except Exception as e:   # a failed backup must never take the server down; it shows in the log and the status file
        log.exception("backup failed")
        status["error"] = f"{type(e).__name__}: {e}"
    try:
        (Path(data_dir) / "backup_status.json").write_text(json.dumps(status, indent=2))
    except OSError:
        pass
    return status


def backup_loop(data_dir: str | Path, stop, every_hours: float = 12.0) -> None:
    """Server thread: one backup shortly after start-up, then every `every_hours`. Off unless a bucket is configured."""
    bucket = Bucket.from_env()
    if bucket is None:
        log.warning("backups to a bucket are off (BACKUP_S3_* is not set); Render's own disk snapshot is the only copy")
        return
    keep = int(os.environ.get("BACKUP_KEEP_DAYS", "30"))
    wait = 120.0
    while not stop.wait(wait):
        s = run_backup(data_dir, bucket, keep_days=keep)
        log.info("backup: %s", {k: v for k, v in s.items() if v is not None})
        wait = every_hours * 3600
