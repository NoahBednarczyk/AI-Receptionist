"""Hours the shop is already busy, from its own calendars and from time the owner blocked on the dashboard.

A booking window is offered only while fewer technicians are busy than the shop has. Each calendar event that overlaps a
window uses one technician; a block uses the whole shop. A shop with one calendar per technician lists several.

The calendar link a shop gives us is a secret (anyone holding it can read the shop's schedule), so it is never logged or
shown, and the server fetches it only if it is https and points at a public address.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import icalendar
import recurring_ical_events

from . import gcal, schedule

log = logging.getLogger("receptionist")
TIMEOUT_S = 5
CACHE_S = 5 * 60
STALE_S = 30 * 60
MAX_BYTES = 3_000_000
MAX_REDIRECTS = 3

_cache: dict[str, dict] = {}   # keyed by a hash of the URL, so the URL itself is never a dictionary key in a traceback
_lock = threading.Lock()


class FetchError(Exception):
    pass


def _public(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise FetchError("host not found") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise FetchError("address is not public")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def fetch(url: str) -> bytes:
    """GET an https calendar link with a timeout and size cap. Each redirect is checked like the first address."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise FetchError("not an https link")
        _public(parts.hostname)
        try:
            with opener.open(urllib.request.Request(url, headers={"User-Agent": "receptionist-calendar"}), timeout=TIMEOUT_S) as r:
                body = r.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            raise FetchError(f"HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise FetchError(type(e).__name__) from None
        if len(body) > MAX_BYTES:
            raise FetchError("calendar too large")
        return body
    raise FetchError("too many redirects")


def normalize_url(url: str) -> str:
    return "https://" + url[len("webcal://"):] if url.lower().startswith("webcal://") else url


def _calendars(biz: dict) -> list[dict]:
    return [c for c in biz.get("calendars", []) if c.get("type", "ical") == "ical" and c.get("url")]


def _cached(key: str, label: str, now_mono: float, produce, ttl: float = CACHE_S):
    """`produce()` read at most once per `ttl` seconds. Returns (value, last read ok); the value is None when the source
    is unreadable and the last good read is older than STALE_S."""
    with _lock:
        entry = _cache.get(key)
    if entry and now_mono - entry["tried"] < ttl:
        return entry["cal"], entry["ok"]
    try:
        entry = {"tried": now_mono, "ok": now_mono, "cal": produce(), "error": None}
    except Exception as e:   # a bad link, a timeout or a broken file all mean the same: do not trust this calendar now
        log.warning("calendar %r not read: %s", label, type(e).__name__)
        entry = {**(entry or {"ok": None, "cal": None}), "tried": now_mono, "error": type(e).__name__}
    with _lock:
        _cache[key] = entry
    fresh = entry["ok"] is not None and now_mono - entry["ok"] < STALE_S
    return (entry["cal"] if fresh else None), entry["ok"]


def _load(cal: dict, now_mono: float, getter) -> tuple[icalendar.Calendar | None, float | None]:
    """The parsed calendar (None when unreadable and too old to trust) and when it was last read successfully."""
    key = hashlib.sha256(cal["url"].encode()).hexdigest()
    return _cached(key, cal.get("label", "calendar"), now_mono,
                   lambda: icalendar.Calendar.from_ical(getter(normalize_url(cal["url"]))))


def _as_dt(v, tz) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=tz)
    return None   # a date means an all-day event


def _interval(start, end, summary: str, tz, keywords: list[str]) -> tuple[datetime, datetime] | None:
    """The span an event blocks, or None. `start`/`end` are datetimes (timed) or dates (all day, end exclusive); an
    all-day event counts only when its title says the shop is off."""
    a = _as_dt(start, tz)
    if a is None:
        if not any(k in summary.lower() for k in keywords):
            return None
        last = end if end is not None else start + timedelta(days=1)
        return (datetime.combine(start, datetime.min.time(), tzinfo=tz), datetime.combine(last, datetime.min.time(), tzinfo=tz))
    return (a, _as_dt(end, tz) or a + timedelta(hours=1))


GCAL_CACHE_S = 60   # Google reads are cheap and a new meeting should block windows within a minute


def busy_intervals(biz: dict, start: datetime, end: datetime, getter=None, mono=None,
                   events_getter=None) -> tuple[list[tuple[datetime, datetime]], list[dict]]:
    """(busy intervals overlapping start..end, problems). A problem is {'label', 'last_read'} for a calendar that could
    not be read recently; its events are left out, so the shop's windows are offered from our own bookings alone."""
    getter = getter or fetch
    events_getter = events_getter or gcal.list_events
    now_mono = mono if mono is not None else time.monotonic()
    tz = schedule.tz(biz)
    keywords = [k.lower() for k in biz.get("busy_keywords", ["off", "vacation", "closed"])]
    busy, problems = [], []
    for cal in _calendars(biz):
        parsed, ok = _load(cal, now_mono, getter)
        if parsed is None:
            problems.append({"label": cal.get("label", "calendar"), "ago_s": None if ok is None else int(now_mono - ok)})
            continue
        for ev in recurring_ical_events.of(parsed).between(start, end):
            if str(ev.get("STATUS", "")).upper() == "CANCELLED" or str(ev.get("TRANSP", "")).upper() == "TRANSPARENT":
                continue
            a = ev.decoded("DTSTART")
            if "DTEND" in ev:
                b = ev.decoded("DTEND")
            elif "DURATION" in ev and isinstance(a, datetime):
                b = a + ev.decoded("DURATION")
            else:
                b = None
            span = _interval(a, b, str(ev.get("SUMMARY", "")), tz, keywords)
            if span:
                busy.append(span)
    for cal in gcal.calendars(biz):
        key = hashlib.sha256(f"g|{cal['calendar_id']}|{start.date()}|{end.date()}".encode()).hexdigest()
        events, ok = _cached(key, cal.get("label", "calendar"), now_mono,
                             lambda: events_getter(cal["calendar_id"], start, end), GCAL_CACHE_S)
        if events is None:
            problems.append({"label": cal.get("label", "calendar"), "ago_s": None if ok is None else int(now_mono - ok)})
            continue
        for ev in events:
            span = _interval(ev["start"], ev["end"], ev["summary"], tz, keywords)
            if span:
                busy.append(span)
    return busy, problems


def booked_counts(biz: dict, store, now: datetime, getter=None, mono=None, events_getter=None) -> tuple[dict[str, int], list[dict]]:
    """What `schedule.open_windows` calls `booked`: our own bookings plus calendar events overlapping each window, with
    blocked time counted as the whole shop. Also returns the calendars that could not be read."""
    counts = dict(store.booked_counts(biz["id"]))
    blocks = [(datetime.fromisoformat(b["start_at"]), datetime.fromisoformat(b["end_at"])) for b in store.blocks(biz["id"], now)]
    has_calendars = bool(_calendars(biz) or gcal.calendars(biz))
    if not blocks and not has_calendars:
        return counts, []
    horizon = now + timedelta(days=biz["booking"]["days_ahead"] + 1)
    events, problems = busy_intervals(biz, now - timedelta(days=1), horizon, getter, mono, events_getter) if has_calendars else ([], [])
    tz = schedule.tz(biz)
    for offset in range(biz["booking"]["days_ahead"] + 1):
        d = now.date() + timedelta(days=offset)
        for s, e in schedule.windows_on(biz, d):
            lo, hi = datetime.combine(d, schedule._t(s), tzinfo=tz), datetime.combine(d, schedule._t(e), tzinfo=tz)
            wid = schedule.window_id(d, s)
            used = schedule.capacity(biz) if any(a < hi and b > lo for a, b in blocks) else 0
            used += sum(1 for a, b in events if a < hi and b > lo)
            if used:
                counts[wid] = counts.get(wid, 0) + used
    return counts, problems


def problem_line(problems: list[dict]) -> str:
    """One sentence for the dashboard and for the agent, naming no link."""
    parts = [p["label"] + (" (never read)" if p["ago_s"] is None else f" (last read {p['ago_s'] // 60} minutes ago)")
             for p in problems]
    return "Calendar not read: " + ", ".join(parts) + ". Windows are being offered from our own bookings only."
