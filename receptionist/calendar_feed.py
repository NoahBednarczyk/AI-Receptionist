"""An iCalendar (.ics) feed of a business's bookings, for subscribing in Google Calendar or Apple Calendar.

One-way: the feed shows what Sam booked. Calendar apps re-read a subscribed feed every few hours, so a new booking can
take a while to appear, and events added by hand in the calendar do not block availability.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import schedule

KEEP_CANCELLED_DAYS = 30


def _escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "")
            .replace("\n", "\\n"))


def _fold(line: str) -> list[str]:
    """Lines over 75 bytes are split, continuation lines starting with a space (RFC 5545)."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        if size + n > (75 if not out else 74):
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return [out[0]] + [" " + x for x in out[1:]]


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _title(service: dict | None, appt: dict) -> str:
    kind = (service["name"].split(":")[0] if service else appt["service_id"] or "Appointment").strip()
    return f"{kind} - {appt['name']}" if appt.get("name") else kind


def render(biz: dict, appointments: list[dict], now: datetime) -> str:
    tz = schedule.tz(biz)
    services = {s["id"]: s for s in biz["services"]}
    stamp = _utc(now)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Receptionist//Bookings//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_escape(biz['name'] + ' bookings')}", f"X-WR-TIMEZONE:{biz['timezone']}"]
    for a in appointments:
        parsed = schedule.parse_window_id(a["window_id"] or "")
        if not parsed or not a.get("confirmation"):
            continue
        day, start = parsed
        begin = datetime.combine(day, schedule._t(start), tzinfo=tz)
        end_hhmm = schedule.window_end(biz, a["window_id"])
        end = datetime.combine(day, schedule._t(end_hhmm), tzinfo=tz) if end_hhmm else begin + timedelta(hours=1)
        cancelled = a["status"] != "booked"
        desc = [f"Confirmation {a['confirmation']}"]
        if a.get("phone"):
            desc.append(f"Phone {a['phone']}")
        if a.get("problem"):
            desc.append(a["problem"])
        if a.get("access_notes"):
            desc.append(f"Notes: {a['access_notes']}")
        where = ", ".join(x for x in (a.get("address"), a.get("town")) if x)
        event = ["BEGIN:VEVENT", f"UID:{a['confirmation']}@receptionist", f"DTSTAMP:{stamp}",
                 f"DTSTART:{_utc(begin)}", f"DTEND:{_utc(end)}",
                 f"SUMMARY:{_escape(_title(services.get(a['service_id']), a))}",
                 f"DESCRIPTION:{_escape(chr(10).join(desc))}"]
        if where and not schedule.in_clinic(biz):
            event.append(f"LOCATION:{_escape(where)}")
        event += [f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}", "END:VEVENT"]
        lines += event
    lines.append("END:VCALENDAR")
    return "\r\n".join(part for line in lines for part in _fold(line)) + "\r\n"
