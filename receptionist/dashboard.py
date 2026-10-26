"""The owner's page: recent calls with summaries, bookings, messages and leads. Served by `serve` at
/dashboard?token=<DASHBOARD_TOKEN>; it refreshes itself every 30 seconds."""
from __future__ import annotations

import json
from datetime import datetime
from html import escape

from . import schedule
from .store import Store

STYLE = """
:root { --bg: #fafaf9; --fg: #1c1917; --muted: #78716c; --line: #e7e5e4; --card: #fff; --accent: #2563eb; --warn: #b45309; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #1c1917; --fg: #f5f5f4; --muted: #a8a29e; --line: #44403c; --card: #292524; --accent: #60a5fa; --warn: #fbbf24; }
}
body { margin: 0; padding: 24px 16px 48px; background: var(--bg); color: var(--fg);
       font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 28px 0 8px; }
.muted { color: var(--muted); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); font-weight: 600; }
tr:last-child td { border-bottom: none; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 99px; font-size: 12px; border: 1px solid var(--line); }
.urgent { color: var(--warn); border-color: var(--warn); }
.empty { padding: 14px 12px; color: var(--muted); }
"""


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
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="card"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def render(biz: dict, store: Store, limit: int = 25) -> str:
    now = schedule.local_now(biz)
    bid = biz["id"]
    calls = []
    for c in store.recent("calls", bid, limit):
        summary = json.loads(c["summary"]) if c.get("summary") else {}
        calls.append([escape(_when(c["started_at"])), escape(c.get("caller") or "unknown"),
                      f'<span class="tag">{escape(summary.get("outcome") or c.get("outcome") or "in progress")}</span>',
                      escape(summary.get("summary", "")), escape(summary.get("follow_up", ""))])
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
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="30">
<title>{escape(biz['name'])} calls</title><style>{STYLE}</style></head><body><main>
<h1>{escape(biz['name'])}</h1><p class="muted">Updated {escape(now.strftime('%a %b %d, %I:%M %p'))}. Refreshes every 30 seconds.</p>
<h2>Calls</h2>{_table(["When", "Caller", "Outcome", "Summary", "Follow-up"], calls)}
<h2>Bookings</h2>{_table(["Confirmation", "Window", "Service", "Name", "Phone", "Address", "Notes", "Status"], bookings)}
<h2>Messages</h2>{_table(["When", "", "Name", "Phone", "Message"], messages)}
<h2>Leads</h2>{_table(["When", "Urgency", "Name", "Phone", "Problem", "Why not booked"], leads)}
</main></body></html>"""
