"""The owner's page: every call with who called, why, what happened, the booked window and the full transcript, plus
bookings, messages and leads. Served by `serve` at /dashboard (a shop key in the link, then a cookie); it refreshes every 30 seconds
(not while a transcript is open) and works on a phone: calls are cards, and the tables stack into rows."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from html import escape

from . import calendar_busy, schedule
from .speech import spoken_phone
from .store import Store

STYLE = """
form label { display: inline-block; margin: 4px 10px 4px 0; }
input, button { font: inherit; padding: 6px 8px; }
:root { --bg: #fafaf9; --fg: #1c1917; --muted: #78716c; --line: #e7e5e4; --card: #fff; --accent: #2563eb; --warn: #b45309; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #1c1917; --fg: #f5f5f4; --muted: #a8a29e; --line: #44403c; --card: #292524; --accent: #60a5fa; --warn: #fbbf24; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 16px 16px 48px; background: var(--bg); color: var(--fg);
       font: 16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; -webkit-text-size-adjust: 100%; }
main { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 28px 0 8px; }
.muted { color: var(--muted); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; overflow-x: auto; }
.empty { padding: 14px 12px; color: var(--muted); }
.tag { display: inline-block; padding: 1px 8px; border-radius: 99px; font-size: 12px; border: 1px solid var(--line); white-space: nowrap; }
.urgent { color: var(--warn); border-color: var(--warn); }
.calls { display: grid; gap: 12px; }
.call { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.call.urgent-call { border-color: var(--warn); }
.call header { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; }
.call .who { font-weight: 600; }
.call .when { color: var(--muted); font-size: 14px; margin-left: auto; }
.call p { margin: 8px 0 0; }
.label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; display: block; }
.booked { border-left: 3px solid var(--accent); padding-left: 10px; }
details { margin-top: 10px; }
summary { cursor: pointer; color: var(--accent); min-height: 32px; display: flex; align-items: center; }
.line { margin: 6px 0; padding: 6px 10px; border-radius: 8px; background: var(--bg); }
.line .role { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; display: block; }
.line.agent { border-left: 3px solid var(--accent); }
.tool { color: var(--muted); font-size: 13px; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); font-weight: 600; }
tr:last-child td { border-bottom: none; }
@media (max-width: 640px) {
  body { padding: 12px 12px 40px; }
  thead { display: none; }
  table, tbody, tr, td { display: block; width: 100%; }
  tr { padding: 8px 12px; border-bottom: 1px solid var(--line); }
  tr:last-child { border-bottom: none; }
  td { border: none; padding: 2px 0; }
  td[data-label]:not([data-label=""])::before { content: attr(data-label) ": "; color: var(--muted); font-size: 13px; }
  td:empty { display: none; }
  .call .when { margin-left: 0; width: 100%; }
}
"""

URGENT_TAG = '<span class="tag urgent">urgent</span>'
INTENTS = {"new_job": "New job", "existing_appointment": "Existing appointment", "question": "Question",
           "emergency": "Emergency", "billing": "Billing", "spam": "Spam or sales", "other": "Other"}


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%b %d, %I:%M %p").replace(" 0", " ")
    except ValueError:
        return iso


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<div class="card"><div class="empty">Nothing yet.</div></div>'
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f'<td data-label="{escape(h)}">{cell}</td>' for h, cell in zip(headers, row))
                   + "</tr>" for row in rows)
    return f'<div class="card"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _transcript(raw: str | None) -> str:
    """The call as a conversation. Silent turns have no text and are left out; tool calls show as small notes."""
    try:
        turns = json.loads(raw) if raw else []
    except ValueError:
        return ""
    out = []
    for t in turns:
        text = (t.get("text") or "").strip()
        if text:
            who = "Sam" if t.get("role") == "agent" else "Caller"
            out.append(f'<div class="line {"agent" if t.get("role") == "agent" else "caller"}">'
                       f'<span class="role">{who}</span>{escape(text)}</div>')
        for call in t.get("tools") or []:
            out.append(f'<div class="tool">{escape(call.get("name", ""))}'
                       f'{" (error)" if call.get("error") else ""}</div>')
    return "".join(out)


def _call_card(biz: dict, store: Store, c: dict, now: datetime) -> str:
    summary = json.loads(c["summary"]) if c.get("summary") else {}
    outcome = summary.get("outcome") or c.get("outcome") or "in progress"
    caller = spoken_phone(c.get("caller")) if c.get("caller") else "Unknown number"
    name = summary.get("caller_name") or ""
    urgent = summary.get("urgency") == "emergency" or summary.get("intent") == "emergency"
    reason = " - ".join(x for x in (INTENTS.get(summary.get("intent") or "", ""), summary.get("summary", "")) if x)
    booked = "".join(
        f'<p class="booked"><span class="label">Booked</span>{escape(schedule.spoken_window(biz, a["window_id"], now))}, '
        f'{escape(a["service_id"] or "")} ({escape(a["confirmation"] or "")})'
        f'{" - " + escape(", ".join(x for x in (a["address"], a["town"]) if x)) if a["address"] else ""}</p>'
        for a in store.for_call("appointments", c["id"]) if a["status"] == "booked")
    flags = "".join(f'<span class="tag urgent">flag: {escape(f.get("type", ""))}</span> '
                    for f in summary.get("flags") or [])
    transcript = _transcript(c.get("transcript"))
    return (f'<article class="call{" urgent-call" if urgent else ""}"><header>'
            f'<span class="who">{escape(caller)}{" - " + escape(name) if name else ""}</span>'
            f'<span class="tag{" urgent" if urgent else ""}">{escape(outcome)}</span>'
            f'{URGENT_TAG if urgent else ""}'
            f'<span class="when">{escape(_when(c["started_at"]))}</span></header>'
            + (f'<p><span class="label">Why they called</span>{escape(reason)}</p>' if reason else "")
            + booked
            + (f'<p><span class="label">Follow up</span>{escape(summary["follow_up"])}</p>' if summary.get("follow_up") else "")
            + (f"<p>{flags}</p>" if flags else "")
            + (f"<details><summary>Transcript</summary>{transcript}</details>" if transcript else "")
            + "</article>")


REFRESH_JS = 'setInterval(function () { if (!document.querySelector("details[open], form:focus-within")) location.reload(); }, 30000);'
CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; base-uri 'none'; "
       "frame-ancestors 'none'; script-src 'sha256-" + base64.b64encode(hashlib.sha256(REFRESH_JS.encode()).digest()).decode() + "'")


def _blocked_time(biz: dict, store: Store, now, problems: list) -> str:
    bid = escape(biz["id"])
    warn = f'<p class="muted"><span class="tag urgent">calendar</span> {escape(calendar_busy.problem_line(problems))}</p>' if problems else ""
    rows = "".join(
        f'<tr><td data-label="From">{escape(_when(b["start_at"]))}</td><td data-label="To">{escape(_when(b["end_at"]))}</td>'
        f'<td data-label="Note">{escape(b["note"] or "")}</td><td><form method="post" action="/dashboard/block/delete">'
        f'<input type="hidden" name="business" value="{bid}"><input type="hidden" name="id" value="{b["id"]}">'
        f'<button>Remove</button></form></td></tr>' for b in store.blocks(biz["id"], now))
    table = (f'<table><thead><tr><th>From</th><th>To</th><th>Note</th><th></th></tr></thead><tbody>{rows}</tbody></table>'
             if rows else '<div class="card"><div class="empty">Nothing blocked.</div></div>')
    return (f'<h2>Block time</h2>{warn}<p class="muted">Hours you cannot take jobs. The assistant will not offer booking windows '
            f'that overlap them.</p>{table}<form method="post" action="/dashboard/block"><input type="hidden" name="business" value="{bid}">'
            f'<label>Date <input type="date" name="date" required></label> <label>From <input type="time" name="start" required></label> '
            f'<label>To <input type="time" name="end" required></label> <label>Note <input name="note" maxlength="80"></label> '
            f'<button>Block it</button></form>')


def render(biz: dict, store: Store, limit: int = 25, problems: list | None = None) -> str:
    now = schedule.local_now(biz)
    bid = biz["id"]
    calls = "".join(_call_card(biz, store, c, now) for c in store.recent("calls", bid, limit))
    calls_html = f'<div class="calls">{calls}</div>' if calls else '<div class="card"><div class="empty">Nothing yet.</div></div>'
    bookings = [[escape(a["confirmation"] or ""), escape(schedule.spoken_window(biz, a["window_id"], now)),
                 escape(a["service_id"] or ""), escape(a["name"] or ""), escape(a["phone"] or ""),
                 escape(", ".join(x for x in (a["address"], a["town"]) if x)),
                 escape(" ".join(x for x in (a["problem"], a["access_notes"]) if x)),
                 f'<span class="tag">{escape(a["status"])}</span>']
                for a in store.recent("appointments", bid, limit)]
    messages = [[escape(_when(m["created_at"])), '<span class="tag urgent">urgent</span>' if m["urgent"] else "",
                 escape(m["name"] or ""), escape(m["phone"] or ""), escape(m["message"] or "")]
                for m in store.recent("messages", bid, limit)]
    leads = [[escape(_when(x["created_at"])), escape(x["urgency"] or ""), escape(x["name"] or ""),
              escape(x["phone"] or ""), escape(x["problem"] or ""), escape(x["reason"] or "")]
             for x in store.recent("leads", bid, limit)]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(biz['name'])} calls</title><style>{STYLE}</style></head><body><main>
<h1>{escape(biz['name'])}</h1><p class="muted">Updated {escape(now.strftime('%a %b %d, %I:%M %p'))}. Refreshes every 30 seconds.</p>
<h2>Calls</h2>{calls_html}
<h2>Bookings</h2>{_table(["Confirmation", "Window", "Service", "Name", "Phone", "Address", "Notes", "Status"], bookings)}
<h2>Messages</h2>{_table(["When", "", "Name", "Phone", "Message"], messages)}
<h2>Leads</h2>{_table(["When", "Urgency", "Name", "Phone", "Problem", "Why not booked"], leads)}
{_blocked_time(biz, store, now, problems or [])}
</main>
<script>{REFRESH_JS}</script>
</body></html>"""
