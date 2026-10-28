"""Spend report: what the project's paid services have cost so far, from their APIs where they have one.

    python -m receptionist spend-report [--since 2026-10-01] [--json] [--out data/spend_report.json]

Keys come from environment variables and a missing key just skips that service (it is listed as "skipped"):
  RETELL_API_KEY       the key the project already uses. Per-call cost from the list-calls endpoint.
  ANTHROPIC_ADMIN_KEY  an Admin key (sk-ant-admin...), made by an organization admin in the Console. The normal
                       ANTHROPIC_API_KEY cannot read costs. Cost report endpoint.
  ELEVENLABS_API_KEY   optional. Only useful if the custom voice lives in your own ElevenLabs account: reports the plan
                       and the next invoice.
Render has no billing API, so Render and anything else without one comes from config.json's
spend_report.fixed_monthly_estimates, marked "estimate". Nothing here is an invoice: check the real billing pages
now and then. Read-only: every call is a GET or a list query, nothing is created or changed.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

RETELL = "https://api.retellai.com"
ANTHROPIC = "https://api.anthropic.com"
ELEVEN = "https://api.elevenlabs.io"
MAX_PAGES = 50   # a runaway pagination loop stops here instead of hammering an API


def _http(method: str, url: str, headers: dict, body: dict | None = None) -> dict:
    """One JSON request. Tests replace this function."""
    req = urllib.request.Request(url, method=method, headers={"Content-Type": "application/json", **headers},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {e.read().decode(errors='replace')[:200]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}") from None


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _row(service: str, status: str, usd: float | None = None, detail: str = "", source: str = "api") -> dict:
    return {"service": service, "status": status, "usd": None if usd is None else round(usd, 2),
            "detail": detail, "source": source}


def retell(since: datetime, key: str | None) -> dict:
    name = "Retell (platform, telephony, voice per minute)"
    if not key:
        return _row(name, "skipped", detail="RETELL_API_KEY not set")
    cents, calls, seconds, pagination_key = 0.0, 0, 0.0, None
    try:
        for _ in range(MAX_PAGES):
            body = {"filter_criteria": {"start_timestamp": {"lower_threshold": int(since.timestamp() * 1000)}},
                    "limit": 1000, "sort_order": "descending"}
            if pagination_key:
                body["pagination_key"] = pagination_key
            page = _http("POST", f"{RETELL}/v2/list-calls", {"Authorization": f"Bearer {key}"}, body)
            items = page if isinstance(page, list) else page.get("calls", [])
            for c in items:
                cost = c.get("call_cost") or {}
                cents += float(cost.get("combined_cost") or 0)
                seconds += float(cost.get("total_duration_seconds") or (c.get("duration_ms") or 0) / 1000)
                calls += 1
            if len(items) < 1000:
                break
            pagination_key = items[-1].get("call_id")
        else:
            return _row(name, "error", detail="stopped after too many pages; the total would be incomplete")
    except RuntimeError as e:
        return _row(name, "error", detail=str(e))
    return _row(name, "ok", cents / 100, f"{calls} calls, {seconds / 60:.1f} min since {since:%Y-%m-%d}")


def anthropic_cost(since: datetime, key: str | None, now: datetime | None = None) -> dict:
    name = "Anthropic API (agent, summaries, tests)"
    if not key:
        return _row(name, "skipped", detail="ANTHROPIC_ADMIN_KEY not set (the normal API key cannot read costs)")
    now = now or datetime.now(timezone.utc)
    q = {"starting_at": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "ending_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "bucket_width": "1d", "limit": "31"}
    cents, page = 0.0, None
    try:
        for _ in range(MAX_PAGES):
            params = dict(q, **({"page": page} if page else {}))
            data = _http("GET", f"{ANTHROPIC}/v1/organizations/cost_report?{urllib.parse.urlencode(params)}",
                         {"x-api-key": key, "anthropic-version": "2023-06-01"})
            for bucket in data.get("data", []):
                for r in bucket.get("results", []):
                    cents += float(r.get("amount") or 0)    # the API reports amounts in cents, as decimal strings
            if not data.get("has_more"):
                break
            page = data.get("next_page")
        else:
            return _row(name, "error", detail="stopped after too many pages; the total would be incomplete")
    except RuntimeError as e:
        return _row(name, "error", detail=str(e))
    return _row(name, "ok", cents / 100, f"organization-wide, since {since:%Y-%m-%d}")


def elevenlabs(key: str | None) -> dict:
    name = "ElevenLabs (own account, if any)"
    if not key:
        return _row(name, "skipped", detail="ELEVENLABS_API_KEY not set (fine if the voice is billed through Retell)")
    try:
        sub = _http("GET", f"{ELEVEN}/v1/user/subscription", {"xi-api-key": key})
    except RuntimeError as e:
        return _row(name, "error", detail=str(e))
    nxt = (sub.get("next_invoice") or {}).get("amount_due_cents")
    detail = f"plan {sub.get('tier', '?')}, status {sub.get('status', '?')}"
    if nxt is None:
        return _row(name, "ok", None, detail + ", no upcoming invoice shown")
    return _row(name, "ok", nxt / 100, detail + ", next invoice (not yet charged)")


def fixed_rows(cfg: dict) -> list[dict]:
    return [_row(f["service"], "estimate", float(f["usd"]), f.get("note", "monthly, from config.json"), "estimate")
            for f in cfg.get("spend_report", {}).get("fixed_monthly_estimates", [])]


def build(cfg: dict, since: datetime | None = None, env: dict | None = None, now: datetime | None = None) -> dict:
    env = os.environ if env is None else env
    since = since or month_start(now)
    rows = [retell(since, env.get("RETELL_API_KEY")),
            anthropic_cost(since, env.get("ANTHROPIC_ADMIN_KEY"), now),
            elevenlabs(env.get("ELEVENLABS_API_KEY")),
            *fixed_rows(cfg)]
    measured = sum(r["usd"] or 0 for r in rows if r["source"] == "api" and r["status"] == "ok"
                   and "not yet charged" not in r["detail"])
    estimated = sum(r["usd"] or 0 for r in rows if r["source"] == "estimate")
    low, high = cfg.get("spend_report", {}).get("project_budget_usd", [None, None])
    total = round(measured + estimated, 2)
    gaps = [r["service"] for r in rows if r["status"] in ("skipped", "error")]
    return {"since": since.strftime("%Y-%m-%d"), "generated": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%MZ"),
            "rows": rows, "measured_usd": round(measured, 2), "estimated_usd": round(estimated, 2), "total_usd": total,
            "budget_usd": [low, high], "left_of_max_usd": None if high is None else round(high - total, 2),
            "incomplete": gaps}


def render(rep: dict) -> str:
    out = [f"Spend since {rep['since']} (generated {rep['generated']})", ""]
    w = max(len(r["service"]) for r in rep["rows"])
    for r in rep["rows"]:
        usd = "     n/a" if r["usd"] is None else f"${r['usd']:>7.2f}"
        out.append(f"  {r['service']:<{w}}  {usd}  [{r['status']}] {r['detail']}")
    out += ["", f"  Measured from APIs:  ${rep['measured_usd']:.2f}", f"  Fixed estimates:     ${rep['estimated_usd']:.2f}",
            f"  Total:               ${rep['total_usd']:.2f}"]
    low, high = rep["budget_usd"]
    if high is not None:
        out.append(f"  Budget ${low:.0f} to ${high:.0f}: ${rep['left_of_max_usd']:.2f} left of the maximum")
    if rep["incomplete"]:
        out += ["", "  NOT COUNTED (no key or an error): " + "; ".join(rep["incomplete"]),
                "  So the total is a floor, not the real figure. Check those billing pages."]
    out += ["", "  Not an invoice. Estimates come from config.json; the Anthropic figure covers the whole organization."]
    return "\n".join(out)
