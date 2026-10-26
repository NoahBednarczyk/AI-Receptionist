"""Business hours and arrival windows. Pure logic: every function takes `now` as an aware datetime in the business's time zone."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .speech import spoken_date, spoken_date_es, spoken_time, spoken_time_es

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def tz(biz: dict) -> ZoneInfo:
    return ZoneInfo(biz["timezone"])


def local_now(biz: dict, at: str | None = None) -> datetime:
    """Now in the business's time zone, or a fixed local time like "2026-10-27T10:15" for tests and simulations."""
    if at:
        dt = datetime.fromisoformat(at)
        return dt.astimezone(tz(biz)) if dt.tzinfo else dt.replace(tzinfo=tz(biz))
    return datetime.now(tz(biz))


def _t(hhmm: str) -> time:
    h, m = (int(x) for x in hhmm.split(":"))
    return time(h, m)


def hours_for(biz: dict, d: date) -> tuple[str, str] | None:
    span = biz["hours"].get(DAYS[d.weekday()])
    return tuple(span) if span else None


def is_open(biz: dict, now: datetime) -> bool:
    span = hours_for(biz, now.date())
    return bool(span) and _t(span[0]) <= now.time() < _t(span[1])


def next_opening(biz: dict, now: datetime) -> datetime | None:
    for offset in range(8):
        d = now.date() + timedelta(days=offset)
        span = hours_for(biz, d)
        if not span:
            continue
        start = datetime.combine(d, _t(span[0]), tzinfo=now.tzinfo)
        if start > now:
            return start
    return None


def window_id(d: date, start: str) -> str:
    return f"{d.isoformat()}T{start}"


def parse_window_id(wid: str) -> tuple[date, str] | None:
    try:
        d, start = wid.split("T")
        _t(start)
        return date.fromisoformat(d), start
    except (ValueError, AttributeError):
        return None


def windows_on(biz: dict, d: date) -> list[tuple[str, str]]:
    """Arrival windows that fit inside that day's opening hours."""
    span = hours_for(biz, d)
    if not span:
        return []
    return [(s, e) for s, e in biz["booking"]["windows"] if _t(s) >= _t(span[0]) and _t(e) <= _t(span[1])]


def open_windows(biz: dict, now: datetime, booked: dict[str, int], earliest: date | None = None, limit: int = 6) -> list[dict]:
    rules = biz["booking"]
    cutoff = now + timedelta(minutes=rules["min_lead_minutes"])
    out = []
    for offset in range(rules["days_ahead"] + 1):
        d = now.date() + timedelta(days=offset)
        if earliest and d < earliest:
            continue
        for start, end in windows_on(biz, d):
            wid = window_id(d, start)
            if datetime.combine(d, _t(start), tzinfo=now.tzinfo) < cutoff:
                continue
            if booked.get(wid, 0) >= rules["technicians"]:
                continue
            out.append({"window_id": wid, "date": d.isoformat(), "start": start, "end": end})
            if len(out) >= limit:
                return out
    return out


def window_end(biz: dict, wid: str) -> str | None:
    parsed = parse_window_id(wid)
    if not parsed:
        return None
    return next((e for s, e in biz["booking"]["windows"] if s == parsed[1]), None)


def spoken_window(biz: dict, wid: str, now: datetime, lang: str = "en") -> str:
    d, start = parse_window_id(wid)
    end = window_end(biz, wid) or start
    if lang == "es":
        return f"{spoken_date_es(d, now.date())}, entre {spoken_time_es(start)} y {spoken_time_es(end)}"
    return f"{spoken_date(d, now.date())}, between {spoken_time(start)} and {spoken_time(end)}"


def spoken_hours(biz: dict) -> str:
    parts = []
    for day in DAYS:
        span = biz["hours"].get(day)
        name = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
                "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}[day]
        parts.append(f"{name} {spoken_time(span[0])} to {spoken_time(span[1])}" if span else f"{name} closed")
    return "; ".join(parts)
