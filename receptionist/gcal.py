"""Google Calendar sync through a service account, both ways.

The shop shares its Google Calendar with our service account's email address ("Make changes to events"); there is no
sign-in screen or Google approval process. Then:

* **Read:** `list_events` returns what is on the calendar, so a meeting or a day off blocks the booking windows at once
  (`calendar_busy` uses it; events we created are marked and skipped so a booking is never counted twice).
* **Write:** `push` puts each booking, move and cancellation on the calendar, with an event id made from the
  confirmation number, so repeating a push never makes a second event.

The key file is the `GOOGLE_SERVICE_ACCOUNT_JSON` environment variable (the whole JSON). It is never written to disk,
logged or shown. A shop's file lists the calendar as `{"type": "google", "calendar_id": "...", "label": "...",
"write": true}`.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from . import calendar_feed, schedule

log = logging.getLogger("receptionist")
API = "https://www.googleapis.com/calendar/v3/calendars/"
TOKEN_URL = "https://oauth2.googleapis.com/token"   # fixed here, never read from the key file
SCOPE = "https://www.googleapis.com/auth/calendar"
TIMEOUT_S = 6
MAX_PAGES = 4
ENV = "GOOGLE_SERVICE_ACCOUNT_JSON"
MARK = "receptionist"   # private event property on every event we create


class GcalError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _urllib(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise GcalError(f"network: {type(e).__name__}") from None


class Client:
    """One service account. `transport(method, url, headers, body) -> (status, bytes)` is replaceable for tests."""

    def __init__(self, credentials: dict, transport=None, clock=time.time):
        for field in ("client_email", "private_key"):
            if not credentials.get(field):
                raise GcalError(f"the service account key has no {field}")
        self.email = credentials["client_email"]
        self._pem = credentials["private_key"]
        self._send = transport or _urllib
        self._clock = clock
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def _assertion(self) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        now = int(self._clock())
        head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64(json.dumps({"iss": self.email, "scope": SCOPE, "aud": TOKEN_URL,
                                  "iat": now, "exp": now + 3600}).encode())
        try:
            key = serialization.load_pem_private_key(self._pem.encode(), password=None)
            sig = key.sign(f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
        except Exception:
            raise GcalError("the service account private key could not be used") from None
        return f"{head}.{claims}.{_b64(sig)}"

    def token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires - 60:
                return self._token
            body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                           "assertion": self._assertion()}).encode()
            status, raw = self._send("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body)
            if status != 200:
                raise GcalError(f"Google refused the service account (HTTP {status})")
            data = json.loads(raw)
            self._token, self._expires = data["access_token"], self._clock() + int(data.get("expires_in", 3600))
            return self._token

    def call(self, method: str, calendar_id: str, path: str = "", params: dict | None = None,
             body: dict | None = None, ok: tuple = (200,)) -> dict:
        url = API + urllib.parse.quote(calendar_id, safe="") + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Authorization": "Bearer " + self.token(), "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        status, raw = self._send(method, url, headers, data)
        if status not in ok:
            reason = ""
            try:
                reason = json.loads(raw)["error"].get("message", "")[:120]
            except Exception:
                pass
            err = GcalError(f"Google Calendar HTTP {status}" + (f": {reason}" if reason else ""))
            err.status = status
            raise err
        return json.loads(raw) if raw else {}


_default: tuple[str, Client] | None = None


def default_client() -> Client:
    """The client for the key in the environment; raises GcalError when there is none."""
    global _default
    raw = os.environ.get(ENV)
    if not raw:
        raise GcalError(f"{ENV} is not set")
    if _default is None or _default[0] != raw:
        try:
            _default = (raw, Client(json.loads(raw)))
        except ValueError:
            raise GcalError(f"{ENV} is not valid JSON") from None
    return _default[1]


# ---- which calendars a shop has -------------------------------------------------------------------------------

def calendars(biz: dict, writable: bool = False) -> list[dict]:
    return [c for c in biz.get("calendars", []) if c.get("type") == "google" and c.get("calendar_id")
            and (not writable or c.get("write", True))]


# ---- reading ---------------------------------------------------------------------------------------------------

def _when(v: dict) -> datetime | date | None:
    if "dateTime" in v:
        return datetime.fromisoformat(v["dateTime"].replace("Z", "+00:00"))
    if "date" in v:
        return date.fromisoformat(v["date"])
    return None


def list_events(calendar_id: str, start: datetime, end: datetime, client: Client | None = None) -> list[dict]:
    """Events between start and end that block time: [{"start", "end", "summary"}] with datetimes (timed events) or dates
    (all-day ones, end exclusive). Cancelled and "free" events are left out, and so are the ones we created."""
    client = client or default_client()
    out, token = [], None
    for _ in range(MAX_PAGES):
        params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                  "orderBy": "startTime", "maxResults": 250, "showDeleted": "false",
                  "fields": "nextPageToken,items(status,transparency,summary,start,end,extendedProperties/private)"}
        if token:
            params["pageToken"] = token
        page = client.call("GET", calendar_id, "/events", params)
        for ev in page.get("items", []):
            if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
                continue
            if (ev.get("extendedProperties") or {}).get("private", {}).get(MARK):
                continue   # ours: it is already in our own booking counts
            a, b = _when(ev.get("start", {})), _when(ev.get("end", {}))
            if a is not None:
                out.append({"start": a, "end": b, "summary": ev.get("summary", "")})
        token = page.get("nextPageToken")
        if not token:
            return out
    return out


# ---- writing ---------------------------------------------------------------------------------------------------

def event_id(confirmation: str) -> str:
    """Google event ids use only a-v and 0-9; one per confirmation number, so a repeated push updates, never doubles."""
    return "rcpt" + re.sub(r"[^a-v0-9]", "", confirmation.lower())


def _body(biz: dict, appt: dict) -> dict:
    tz = schedule.tz(biz)
    parsed = schedule.parse_window_id(appt["window_id"] or "")
    if not parsed:
        raise GcalError("the booking has no usable window")
    day, start = parsed
    begin = datetime.combine(day, schedule._t(start), tzinfo=tz)
    end_hhmm = schedule.window_end(biz, appt["window_id"])
    end = datetime.combine(day, schedule._t(end_hhmm), tzinfo=tz) if end_hhmm else begin + timedelta(hours=1)
    services = {s["id"]: s for s in biz["services"]}
    lines = [f"Confirmation {appt['confirmation']}"]
    for label, key in (("Phone", "phone"), ("", "problem"), ("Notes", "access_notes")):
        if appt.get(key):
            lines.append(f"{label}: {appt[key]}" if label else appt[key])
    lines.append("Booked by the phone assistant.")
    body = {"summary": calendar_feed._title(services.get(appt["service_id"]), appt),
            "description": "\n".join(lines), "status": "confirmed",
            "start": {"dateTime": begin.isoformat(), "timeZone": biz["timezone"]},
            "end": {"dateTime": end.isoformat(), "timeZone": biz["timezone"]},
            "extendedProperties": {"private": {MARK: "1", "confirmation": appt["confirmation"]}}}
    where = ", ".join(x for x in (appt.get("address"), appt.get("town")) if x)
    if where and not schedule.in_clinic(biz):
        body["location"] = where
    return body


def push(biz: dict, appt: dict, client: Client | None = None) -> None:
    """Make every writable Google calendar of the shop show this appointment as it is now (booked or cancelled)."""
    cals = calendars(biz, writable=True)
    if not cals:
        return
    client = client or default_client()
    eid = event_id(appt["confirmation"])
    errors = []
    for cal in cals:
        cid = cal["calendar_id"]
        try:
            if appt["status"] != "booked":
                try:
                    client.call("DELETE", cid, "/events/" + eid, ok=(200, 204))
                except GcalError as e:
                    if getattr(e, "status", None) not in (404, 410):   # already gone is the goal
                        raise
                continue
            body = _body(biz, appt)
            try:
                client.call("POST", cid, "/events", body={**body, "id": eid})
            except GcalError as e:
                if getattr(e, "status", None) != 409:   # 409: it exists already, so update it
                    raise
                client.call("PATCH", cid, "/events/" + eid, body=body)
        except GcalError as e:
            errors.append(f"{cal.get('label', 'calendar')}: {e}")
    if errors:
        raise GcalError("; ".join(errors))


_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gcal")


def _run(biz: dict, store, appt: dict, client: Client | None) -> None:
    try:
        push(biz, appt, client)
        store.set_sync(biz["id"], appt["confirmation"], "ok", None)
    except Exception as e:   # a calendar problem must never touch a call; the dashboard shows it and `calendar-sync` retries
        log.warning("calendar push %s failed: %s", appt.get("confirmation"), type(e).__name__)
        store.set_sync(biz["id"], appt["confirmation"], "failed", str(e)[:200])


def push_async(biz: dict, store, appt: dict, client: Client | None = None) -> None:
    """Fire and forget: the caller is never kept waiting on Google. Tests replace `submit` to run it at once."""
    if calendars(biz, writable=True) and appt:
        submit(_run, biz, store, dict(appt), client)


def submit(fn, *args) -> None:
    _pool.submit(fn, *args)


def resync(biz: dict, store, now: datetime, client: Client | None = None) -> dict:
    """Push every booking from yesterday on, and the recent cancellations. Safe to repeat."""
    counts = {"ok": 0, "failed": 0}
    since = (now - timedelta(days=1)).date().isoformat()
    for appt in store.appointments_since(biz["id"], since):
        if not appt.get("confirmation") or appt.get("call_id") == "seed":
            continue
        _run(biz, store, appt, client)
        counts["ok" if (store.get_sync(biz["id"], appt["confirmation"]) or {}).get("status") == "ok" else "failed"] += 1
    return counts


def check(biz: dict, client: Client | None = None) -> list[str]:
    """One line per calendar: can we read it and may we write to it? (writes nothing)"""
    client = client or default_client()
    now = schedule.local_now(biz)
    lines = []
    for cal in calendars(biz):
        try:
            list_events(cal["calendar_id"], now, now + timedelta(days=1), client)
            meta = client.call("GET", cal["calendar_id"], "/events", {"maxResults": 1, "fields": "accessRole"})
            role = meta.get("accessRole", "?")
            note = "" if role in ("writer", "owner") else " (can read but not change: share it with 'Make changes to events')"
            lines.append(f"{cal.get('label', 'calendar')}: reachable, access {role}{note}")
        except GcalError as e:
            lines.append(f"{cal.get('label', 'calendar')}: NOT reachable ({e}); share the calendar with {client.email}")
    return lines
