"""Spend report: what the project's paid services have cost so far, from their APIs where they have one.

    python -m receptionist spend-report [--since 2026-10-01] [--json] [--out data/spend_report.json]

Keys come from environment variables and a missing key just skips that service (it is listed as "skipped"):
  RETELL_API_KEY       the key the project already uses. Per-call cost from the v3 list-calls endpoint.
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
from datetime import datetime, timedelta, timezone

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
            # v3 list-calls (the v2 list endpoints were removed in June 2026). combined_cost is in cents.
            body = {"filter_criteria": {"start_timestamp": {"type": "number", "op": "ge",
                                                           "value": int(since.timestamp() * 1000)}},
                    "limit": 1000, "sort_order": "descending"}
            if pagination_key:
                body["pagination_key"] = pagination_key
            page = _http("POST", f"{RETELL}/v3/list-calls", {"Authorization": f"Bearer {key}"}, body)
            for c in page.get("items", []):
                cost = c.get("call_cost") or {}
                cents += float(cost.get("combined_cost") or 0)
                seconds += float(cost.get("total_duration_seconds") or (c.get("duration_ms") or 0) / 1000)
                calls += 1
            pagination_key = page.get("pagination_key")
            if not page.get("has_more") or not pagination_key:
                break
        else:
            return _row(name, "error", detail="stopped after too many pages; the total would be incomplete")
    except RuntimeError as e:
        return _row(name, "error", detail=str(e))
    return _row(name, "ok", cents / 100, f"{calls} calls, {seconds / 60:.1f} min since {since:%Y-%m-%d}")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pages(path: str, params: dict, key: str) -> list[dict]:
    """Every bucket of an Admin API report, following pagination (read-only GETs)."""
    out, page = [], None
    for _ in range(MAX_PAGES):
        q = dict(params, **({"page": page} if page else {}))
        data = _http("GET", f"{ANTHROPIC}{path}?{urllib.parse.urlencode(q, doseq=True)}",
                     {"x-api-key": key, "anthropic-version": "2023-06-01"})
        out += data.get("data", [])
        if not data.get("has_more"):
            return out
        page = data.get("next_page")
    raise RuntimeError("stopped after too many pages; the total would be incomplete")


def priced_usage_usd(results: list[dict], prices: dict) -> tuple[dict, list[str]]:
    """Dollars by model for usage-report rows, priced from config.json: cache reads at 0.1x the input rate, five-minute
    cache writes at 1.25x and one-hour writes at 2x. Models with no price in the config are returned in the second list."""
    by_model, unpriced = {}, []
    for r in results:
        model = r.get("model") or "unknown"
        if model not in prices:
            if model not in unpriced:
                unpriced.append(model)
            continue
        p_in, p_out = prices[model]
        write = r.get("cache_creation") or {}
        tokens = (r.get("uncached_input_tokens", 0) * p_in + r.get("output_tokens", 0) * p_out
                  + r.get("cache_read_input_tokens", 0) * p_in * 0.1
                  + write.get("ephemeral_5m_input_tokens", 0) * p_in * 1.25
                  + write.get("ephemeral_1h_input_tokens", 0) * p_in * 2.0)
        by_model[model] = by_model.get(model, 0.0) + tokens / 1e6
    return by_model, unpriced


def anthropic_cost(since: datetime, key: str | None, now: datetime | None = None, prices: dict | None = None) -> dict:
    """Organization-wide Claude spend. The cost report only covers finished UTC days, so today comes from the usage
    report (tokens by model) priced from `prices`, and is labelled as an estimate in the detail."""
    name = "Anthropic API (agent, summaries, tests)"
    if not key:
        return _row(name, "skipped", detail="ANTHROPIC_ADMIN_KEY not set (the normal API key cannot read costs)")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    first = since.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily, by_model, finished = [], {}, 0.0
    try:
        if first < today:
            for bucket in _pages("/v1/organizations/cost_report", {"starting_at": _iso(first), "ending_at": _iso(today),
                                                                    "bucket_width": "1d", "limit": "31"}, key):
                day = 0.0
                for r in bucket.get("results", []):
                    cents = float(r.get("amount") or 0)    # the API reports amounts in cents, as decimal strings
                    day += cents
                    m = r.get("model") or "other (not tokens)"
                    by_model[m] = by_model.get(m, 0.0) + cents / 100
                finished += day
                daily.append({"day": (bucket.get("starting_at") or "")[:10], "usd": round(day / 100, 4), "kind": "cost report"})
        so_far, unpriced = 0.0, []
        if prices:
            buckets = _pages("/v1/organizations/usage_report/messages",
                             {"starting_at": _iso(today), "ending_at": _iso(today + timedelta(days=1)),
                              "bucket_width": "1h", "group_by[]": ["model"], "limit": "48"}, key)
            today_by_model, unpriced = priced_usage_usd([r for b in buckets for r in b.get("results", [])], prices)
            so_far = sum(today_by_model.values())
            for m, usd in today_by_model.items():
                by_model[m] = by_model.get(m, 0.0) + usd
            daily.append({"day": today.strftime("%Y-%m-%d"), "usd": round(so_far, 4), "kind": "estimate from token counts"})
    except RuntimeError as e:
        return _row(name, "error", detail=str(e))
    detail = (f"organization-wide, since {since:%Y-%m-%d}: ${finished / 100:.2f} from finished days (cost report) + about "
              f"${so_far:.2f} so far today (priced from token counts)")
    if unpriced:
        detail += f"; not priced, add to config.json prices_per_mtok: {', '.join(unpriced)}"
    row = _row(name, "ok", finished / 100 + so_far, detail)
    row["daily"], row["by_model"] = daily, {m: round(v, 4) for m, v in sorted(by_model.items(), key=lambda x: -x[1])}
    return row


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
            anthropic_cost(since, env.get("ANTHROPIC_ADMIN_KEY"), now, cfg.get("prices_per_mtok")),
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
            "incomplete": gaps,
            "anthropic_daily": next((r.get("daily", []) for r in rows if r["service"].startswith("Anthropic")), []),
            "anthropic_by_model": next((r.get("by_model", {}) for r in rows if r["service"].startswith("Anthropic")), {})}


def to_csv_files(rep: dict, path) -> list:
    """Two plain CSV files for whoever does the books: every service with its total, and Anthropic by day and by model.
    Contains costs only: no keys, no call content, no customer details."""
    import csv
    from pathlib import Path
    base = Path(path)
    base.parent.mkdir(parents=True, exist_ok=True)
    summary = base.with_suffix(".csv")
    with summary.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["since", rep["since"], "generated", rep["generated"]])
        w.writerow(["service", "status", "usd", "source", "detail"])
        for r in rep["rows"]:
            w.writerow([r["service"], r["status"], "" if r["usd"] is None else f"{r['usd']:.2f}", r["source"], r["detail"]])
        w.writerow([])
        w.writerow(["measured_usd", rep["measured_usd"], "estimated_usd", rep["estimated_usd"], "total_usd", rep["total_usd"]])
        if rep["incomplete"]:
            w.writerow(["NOT COUNTED (total is a floor)", "; ".join(rep["incomplete"])])
    detail = base.with_name(base.stem + "-anthropic.csv")
    with detail.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "usd", "kind"])
        for d in rep.get("anthropic_daily", []):
            w.writerow([d["day"], f"{d['usd']:.4f}", d["kind"]])
        w.writerow([])
        w.writerow(["model or line", "usd"])
        for m, usd in rep.get("anthropic_by_model", {}).items():
            w.writerow([m, f"{usd:.4f}"])
    return [summary, detail]


def render(rep: dict) -> str:
    out = [f"Spend since {rep['since']} (generated {rep['generated']})", ""]
    w = max(len(r["service"]) for r in rep["rows"])
    for r in rep["rows"]:
        usd = "     n/a" if r["usd"] is None else f"${r['usd']:>7.2f}"
        out.append(f"  {r['service']:<{w}}  {usd}  [{r['status']}] {r['detail']}")
    if rep.get("anthropic_by_model"):
        out += ["", "  Anthropic by model (organization-wide):"] + [f"    {m:<28} ${usd:>8.2f}" for m, usd in rep["anthropic_by_model"].items()]
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
