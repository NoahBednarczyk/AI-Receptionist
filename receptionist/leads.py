"""The discovery caller's lead list: what counts as a lead, and how new finds are merged into the list.

A Claude session (the `build-leads` skill) searches the web for lawn care, landscaping and snow removal businesses around
Sandpoint and posts what it found as *candidates* to /admin/outbound/leads/merge. Everything is checked here, on the
server, so a careless search can never put a bad row in the list:

  - a lead needs a business name, a real 10-digit phone number, a town in our service area and the page it came from;
  - only lawn care, landscaping and snow removal businesses are kept (anything else is skipped with a reason);
  - a number already on the list, or on the do-not-call list, is never added twice or re-added;
  - `line` (landline / voip / mobile) is only set when someone checked it. A candidate with no line type is kept, but
    outbound.can_call() will not dial it in cold mode until the type is confirmed, which is the intended behavior.

Nothing here calls the network or places a call.
"""
from __future__ import annotations

import re
from datetime import date

from .speech import fictional_phone, normalize_phone

TRADES = {
    "Snow removal": ("snow", "plow", "plowing"),
    "Landscaping": ("landscap", "hardscape", "grounds", "garden", "nursery", "tree", "irrigation", "sprinkler"),
    "Lawn care": ("lawn", "mow", "turf", "yard", "weed", "fertiliz", "aerat"),
}
LINES = {"landline", "voip", "mobile"}
# Fields a candidate may carry through to the lead; anything else is dropped.
KEEP = ("contact", "notes", "hook", "line", "timezone")


def service_towns(cfg: dict) -> list[str]:
    """Towns we search and accept: outbound.lead_towns if set, else the lawn care demo shop's service area."""
    from .config import load_business
    custom = (cfg.get("outbound") or {}).get("lead_towns")
    if custom:
        return list(custom)
    return list(load_business(cfg, "lawn_care")["service_area"]["towns"])


def trade_of(text: str) -> str | None:
    """Which of our three trades a business is, from its category and name. Snow wins over lawn only when lawn is absent,
    so "Lawn & Snow" counts as lawn care. None means it is not in our niche (a plumber, a lawyer, a tree-only service...)."""
    t = (text or "").lower()
    found = [name for name, words in TRADES.items() if any(w in t for w in words)]
    if not found:
        return None
    if "Lawn care" in found:
        return "Lawn care"
    return "Snow removal" if found == ["Snow removal"] else found[0]


def slug(name: str, phone: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "lead"
    return f"{base}-{phone[-4:]}"


def normalize(candidate: dict, towns: list[str], today: date | None = None) -> tuple[dict | None, str | None]:
    """(lead, None) for a good candidate, (None, reason) for one that is skipped."""
    name = (candidate.get("name") or "").strip()
    if not name:
        return None, "no business name"
    phone = normalize_phone(str(candidate.get("phone") or ""))
    if not phone or len(phone.lstrip("+")) < 10:
        return None, "no usable 10-digit phone number"
    if fictional_phone(phone):
        return None, "555-01xx is a placeholder number"
    town = (candidate.get("town") or "").strip()
    match = next((t for t in towns if t.lower() == re.sub(r",?\s*(id|idaho)\.?$", "", town, flags=re.I).strip().lower()), None)
    if not match:
        return None, f"town {town or '(missing)'!r} is outside the service area"
    source = (candidate.get("source") or "").strip()
    if not re.match(r"^https?://\S+$", source):
        return None, "no source page (a business listing URL is required)"
    trade = trade_of(f"{candidate.get('type', '')} {name}")
    if not trade:
        return None, f"not lawn care, landscaping or snow removal ({candidate.get('type') or 'no category'})"
    line = (candidate.get("line") or "").strip().lower() or None
    if line and line not in LINES:
        return None, f"line type {line!r} is not one of {sorted(LINES)}"
    lead = {"id": slug(name, phone), "name": name, "phone": phone, "type": trade, "town": match, "source": source,
            "stage": "leads", "found": (today or date.today()).isoformat()}
    for k in KEEP:
        if candidate.get(k):
            lead[k] = line if k == "line" else candidate[k]
    return lead, None


def merge(existing: list[dict], candidates: list[dict], towns: list[str], dnc: set[str] | None = None,
          today: date | None = None) -> tuple[list[dict], dict]:
    """The new list and a report. Existing leads are never changed or reordered (their stage, notes and follow-up dates
    are the record of calls already made); new ones are appended. A number already listed, or on do-not-call, is skipped."""
    seen = {normalize_phone(l.get("phone") or "") for l in existing} | set(dnc or ())
    merged = list(existing)
    added, skipped = [], []
    for c in candidates:
        lead, why = normalize(c, towns, today)
        if lead and lead["phone"] in seen:
            lead, why = None, "already on the list or the do-not-call list"
        if not lead:
            skipped.append({"name": c.get("name"), "phone": c.get("phone"), "why": why})
            continue
        seen.add(lead["phone"])
        merged.append(lead)
        added.append(lead["id"])
    return merged, {"added": len(added), "added_ids": added, "skipped": skipped, "total": len(merged)}
