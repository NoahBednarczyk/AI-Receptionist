"""The weekly owner report (W6.5): the renewal argument. Monday morning, shop time, one email per owner address:
calls answered, calls that came after hours, jobs booked, messages, and what the booked jobs are worth at the shop's own
average job value (`avg_job_value` in the shop's file). The value is an estimate (jobs booked x the owner's own number),
and says so; it is never presented as money earned."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta

from . import schedule

log = logging.getLogger("receptionist")
SEND_AFTER_HOUR = 8   # Monday, shop time
SCHEMA = "CREATE TABLE IF NOT EXISTS weekly_sent (business_id TEXT, week TEXT, PRIMARY KEY (business_id, week));"


def week_key(now: datetime) -> str:
    """The ISO week the report is sent in (it covers the seven days before this moment)."""
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def numbers(store, biz: dict, now: datetime) -> dict:
    since = now - timedelta(days=7)
    calls = []
    for c in store.recent("calls", biz["id"], 2000):
        if not c.get("started_at") or c["id"] == "seed":
            continue
        started = datetime.fromisoformat(c["started_at"]).astimezone(now.tzinfo)
        if since <= started <= now:
            calls.append((c, started))
    ids = {c["id"] for c, _ in calls}
    booked = [a for a in store.recent("appointments", biz["id"], 2000) if a.get("call_id") in ids and a.get("status") != "cancelled"]
    msgs = [m for m in store.recent("messages", biz["id"], 2000) if m.get("call_id") in ids]
    leads = [x for x in store.recent("leads", biz["id"], 2000) if x.get("call_id") in ids]
    value = biz.get("avg_job_value")
    return {"calls": len(calls), "after_hours": sum(1 for _, s in calls if not schedule.is_open(biz, s)),
            "booked": len(booked), "messages": len(msgs), "urgent_messages": sum(1 for m in msgs if m.get("urgent")),
            "leads": len(leads), "avg_job_value": value,
            "estimated_value": round(len(booked) * float(value)) if value else None}


def report_text(biz: dict, n: dict, now: datetime, link: str = "") -> tuple[str, str]:
    first = (now - timedelta(days=7)).strftime("%b %d")
    lines = [f"{biz['name']}: your week with {biz['agent_name']} ({first} to {now.strftime('%b %d')})", "",
             f"Calls answered: {n['calls']}",
             f"  of those, after hours or when you were closed: {n['after_hours']}",
             f"Jobs booked: {n['booked']}",
             f"Messages for you: {n['messages']} ({n['urgent_messages']} urgent)",
             f"Leads to follow up: {n['leads']}", ""]
    if n["estimated_value"] is not None:
        lines.append(f"At your average job of ${float(n['avg_job_value']):,.0f}, the {n['booked']} booked job(s) are worth about "
                     f"${n['estimated_value']:,}. That is an estimate from your own number, not what you invoiced.")
    else:
        lines.append("Tell us your average job value and this report will show what the booked jobs are worth.")
    if link:
        lines += ["", f"Every call: {link}"]
    return f"{biz['name']}: your week ({n['calls']} calls, {n['booked']} jobs booked)", "\n".join(lines)


def due(store, biz: dict, now: datetime) -> bool:
    emails = (biz.get("notify") or {}).get("email")
    if not emails or (biz.get("notify") or {}).get("weekly", True) is False or biz.get("demo"):
        return False
    if now.weekday() != 0 or now.hour < SEND_AFTER_HOUR:
        return False
    return not store._rows("SELECT 1 FROM weekly_sent WHERE business_id=? AND week=?", (biz["id"], week_key(now)))


def send(notifier, biz: dict, now: datetime | None = None) -> bool:
    """Never raises. Marks the week done only when an email went."""
    try:
        now = now or schedule.local_now(biz)
        notifier.store.db.executescript(SCHEMA)
        subject, body = report_text(biz, numbers(notifier.store, biz, now), now, notifier.link(biz))
        ok = False
        for to in biz["notify"]["email"]:
            ok |= notifier._send_email(biz["id"], None, "weekly", to, subject, body, urgent=True)
        if ok:
            notifier.store._write("INSERT OR IGNORE INTO weekly_sent (business_id, week) VALUES (?, ?)", (biz["id"], week_key(now)))
        return ok
    except Exception:
        log.exception("weekly report failed for %s", biz.get("id"))
        return False


def run(notifier, businesses: list[dict], now: datetime | None = None) -> int:
    sent = 0
    notifier.store.db.executescript(SCHEMA)
    for biz in businesses:
        try:
            at = now.astimezone(schedule.tz(biz)) if now else schedule.local_now(biz)
            if due(notifier.store, biz, at) and send(notifier, biz, at):
                sent += 1
        except Exception:
            log.exception("weekly check failed for %s", biz.get("id"))
    return sent


def weekly_loop(notifier, businesses, stop: threading.Event, every: int = 900) -> None:
    while not stop.wait(every):
        try:
            run(notifier, businesses())
        except Exception:
            log.exception("weekly loop")
