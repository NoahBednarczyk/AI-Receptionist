"""The running-cost snapshot for the owner's dashboard (the Finances page of "Claude's Business").

    python -m receptionist finance-sync [--out data/reports] [--since 2026-10-12]

It reads real billing data and writes two small JSON files, each the exact content of one dashboard document:
  finance-current.json  -> document finance/current  (this month by service, totals, per-call cost, budget, notes)
  finance-daily.json    -> document finance/daily    (one entry per day, for the chart and the to-date totals)

What counts, and why:
  - Retell: every call's real cost from Retell's API (voice, engine, phone line, noise cancellation).
  - Claude, live phone line: usage on the API key(s) named in config.json finance.live_key_names ("AI Receptionist" is the
    key the Render server uses), priced from token counts. That is exactly what answering calls costs, separate from
    every other project on the same Anthropic account.
  - Claude, tests: the project's own test ledger (data/spend.json). The key used on this Mac is shared with another
    project, so its total is shown as "not counted" instead of being blamed on the receptionist.
  - Fixed monthly costs (Render and the phone number) are estimates from config.json and are labelled as such.
Everything here is read-only. Nothing in the files is a key, a call transcript or a customer detail.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import budget, spend_report as sr

DEFAULT_START = "2026-10-12"


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def _midnight(dt: datetime) -> datetime:
    return _utc(dt).replace(hour=0, minute=0, second=0, microsecond=0)


def _day_of(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def retell_days(since: datetime, key: str) -> tuple[dict, dict]:
    """({UTC day: {usd, minutes, calls}}, {product: usd}) for every Retell call since `since`."""
    days, products = {}, {}
    pagination_key = None
    for _ in range(sr.MAX_PAGES):
        body = {"filter_criteria": {"start_timestamp": {"type": "number", "op": "ge", "value": int(since.timestamp() * 1000)}},
                "limit": 1000, "sort_order": "descending"}
        if pagination_key:
            body["pagination_key"] = pagination_key
        page = sr._http("POST", f"{sr.RETELL}/v3/list-calls", {"Authorization": f"Bearer {key}"}, body)
        for c in page.get("items", []):
            if not c.get("start_timestamp"):
                continue
            cost = c.get("call_cost") or {}
            d = days.setdefault(_day_of(c["start_timestamp"]), {"usd": 0.0, "minutes": 0.0, "calls": 0})
            d["usd"] += float(cost.get("combined_cost") or 0) / 100   # cents
            d["minutes"] += float(cost.get("total_duration_seconds") or (c.get("duration_ms") or 0) / 1000) / 60
            d["calls"] += 1
            for p in cost.get("product_costs") or []:
                products[p.get("product", "other")] = products.get(p.get("product", "other"), 0.0) + float(p.get("cost") or 0) / 100
        pagination_key = page.get("pagination_key")
        if not page.get("has_more") or not pagination_key:
            return days, products
    raise RuntimeError("Retell: stopped after too many pages; the total would be incomplete")


def api_key_names(admin_key: str) -> dict[str, str]:
    """{key id: the name it was given in the Console}. Names and ids only: the Admin API never returns the secret."""
    names, after = {}, None
    for _ in range(sr.MAX_PAGES):
        params = {"limit": "100", **({"after_id": after} if after else {})}
        data = sr._http("GET", f"{sr.ANTHROPIC}/v1/organizations/api_keys?{urllib.parse.urlencode(params)}",
                        {"x-api-key": admin_key, "anthropic-version": "2023-06-01"})
        for k in data.get("data", []):
            names[k["id"]] = k.get("name") or k["id"]
        after = data.get("last_id")
        if not data.get("has_more") or not after:
            return names
    raise RuntimeError("Anthropic: stopped after too many pages listing keys")


def claude_days(admin_key: str, first: datetime, today: datetime, prices: dict) -> tuple[list[dict], list[str]]:
    """Priced usage rows [{day, key_id, model, usd}] from the first day through today (UTC), plus models with no price."""
    buckets = sr._pages("/v1/organizations/usage_report/messages",
                        {"starting_at": sr._iso(first), "ending_at": sr._iso(today + timedelta(days=1)),
                         "bucket_width": "1d", "group_by[]": ["api_key_id", "model"], "limit": "31"}, admin_key)
    rows, unpriced = [], []
    for b in buckets:
        day = (b.get("starting_at") or "")[:10]
        for r in b.get("results", []):
            usd = sr.row_usd(r, prices)
            if usd is None:
                if (r.get("model") or "unknown") not in unpriced:
                    unpriced.append(r.get("model") or "unknown")
                continue
            rows.append({"day": day, "key_id": r.get("api_key_id") or "?", "model": r["model"], "usd": usd})
    return rows, unpriced


def billed_total(admin_key: str, first: datetime, today: datetime) -> float | None:
    """What Anthropic billed the whole organization for the finished days (cost report), to check our pricing against."""
    if first >= today:
        return None
    total = 0.0
    for b in sr._pages("/v1/organizations/cost_report", {"starting_at": sr._iso(first), "ending_at": sr._iso(today),
                                                          "bucket_width": "1d", "limit": "31"}, admin_key):
        total += sum(float(r.get("amount") or 0) for r in b.get("results", [])) / 100
    return total


def _r(x: float, n: int = 4) -> float:
    return round(x, n)


def build(cfg: dict, env: dict | None = None, now: datetime | None = None, since: datetime | None = None) -> dict:
    """{"current": ..., "daily": ...}: the two dashboard documents. A service without a key is reported as missing,
    never as zero."""
    env = os.environ if env is None else env
    now = _utc(now or datetime.now(timezone.utc))
    fin = cfg.get("finance", {})
    start = _midnight(since or datetime.strptime(fin.get("project_start", DEFAULT_START), "%Y-%m-%d").replace(tzinfo=timezone.utc))
    today = _midnight(now)
    month = today.replace(day=1)
    live_names, dev_names = set(fin.get("live_key_names", ["AI Receptionist"])), set(fin.get("dev_key_names", []))
    problems, notes = [], []

    # ---- Retell
    retell, products = {}, {}
    if env.get("RETELL_API_KEY"):
        try:
            retell, products = retell_days(start, env["RETELL_API_KEY"])
        except RuntimeError as e:
            problems.append(f"Retell: {e}")
    else:
        problems.append("Retell: RETELL_API_KEY not set")

    # ---- Claude, by key
    live, dev, other, models_live = {}, {}, {}, {}
    names, rows, unpriced, billed = {}, [], [], None
    if env.get("ANTHROPIC_ADMIN_KEY"):
        try:
            names = api_key_names(env["ANTHROPIC_ADMIN_KEY"])
            rows, unpriced = claude_days(env["ANTHROPIC_ADMIN_KEY"], start, today, cfg.get("prices_per_mtok", {}))
            billed = billed_total(env["ANTHROPIC_ADMIN_KEY"], start, today)
        except RuntimeError as e:
            problems.append(f"Anthropic: {e}")
    else:
        problems.append("Anthropic: ANTHROPIC_ADMIN_KEY not set")
    for r in rows:
        name = names.get(r["key_id"], r["key_id"])
        bucket = live if name in live_names else dev if name in dev_names else other
        bucket[r["day"]] = bucket.get(r["day"], 0.0) + r["usd"]
        if bucket is live and r["day"] >= month.strftime("%Y-%m-%d"):
            models_live[r["model"]] = models_live.get(r["model"], 0.0) + r["usd"]
    if billed is not None and rows and not unpriced:
        finished = [r for r in rows if r["day"] < today.strftime("%Y-%m-%d")]
        priced = sum(r["usd"] for r in finished)
        if billed and abs(priced - billed) / billed > 0.10:
            notes.append(f"Priced from tokens, the account's finished days come to ${priced:.2f}; Anthropic billed ${billed:.2f}. "
                         "The gap is usually a model with no price in config.json or a charge that is not tokens.")
    if unpriced:
        notes.append("Not priced (add to config.json prices_per_mtok): " + ", ".join(unpriced))

    # ---- the project's own test ledger (local dates)
    ledger = {d: float(v) for d, v in budget._read(cfg).get("days", {}).items()}

    # ---- daily document
    all_days = sorted(set(retell) | set(live) | set(ledger))
    daily = {}
    for d in all_days:
        if d < start.strftime("%Y-%m-%d"):
            continue
        daily[d] = {"r": _r(retell.get(d, {}).get("usd", 0.0)), "c": _r(live.get(d, 0.0)), "t": _r(ledger.get(d, 0.0)),
                    "m": _r(retell.get(d, {}).get("minutes", 0.0), 2), "n": retell.get(d, {}).get("calls", 0)}
    cut = (today - timedelta(days=89)).strftime("%Y-%m-%d")
    daily = {d: v for d, v in daily.items() if d >= cut}

    # ---- this month, by service
    ms = month.strftime("%Y-%m-%d")
    mon = lambda series: sum(v for d, v in series.items() if d >= ms)
    r_usd = sum(v["usd"] for d, v in retell.items() if d >= ms)
    r_min = sum(v["minutes"] for d, v in retell.items() if d >= ms)
    r_calls = sum(v["calls"] for d, v in retell.items() if d >= ms)
    c_usd, t_usd = mon(live), mon(ledger)
    fixed = [{"group": "fixed", "service": f["service"], "usd": _r(float(f["usd"]), 2), "status": "estimate",
              "detail": f.get("note", "monthly, from config.json")} for f in cfg.get("spend_report", {}).get("fixed_monthly_estimates", [])]
    service_rows = [
        {"group": "calls", "service": "Retell: voice, engine and phone line", "usd": _r(r_usd, 2),
         "status": "measured" if env.get("RETELL_API_KEY") and not any(p.startswith("Retell") for p in problems) else "missing",
         "detail": f"{r_calls} calls, {r_min:.1f} min this month, from Retell's billing data"},
        {"group": "claude", "service": "Claude: the live phone line", "usd": _r(c_usd, 2),
         "status": "measured" if rows or live else "missing",
         "detail": "key " + ", ".join(sorted(live_names)) + ", priced from token counts"},
        {"group": "claude", "service": "Claude: tests and simulations", "usd": _r(t_usd, 2), "status": "logged",
         "detail": "from the project's own test ledger"},
        *fixed]
    measured = sum(r["usd"] for r in service_rows if r["status"] in ("measured", "logged"))
    estimated = sum(r["usd"] for r in fixed)
    excluded = []
    if dev:
        dev_m = mon(dev)
        excluded.append({"service": "Claude: shared development key", "usd": _r(dev_m, 2),
                         "detail": "the key on this Mac is shared with another project, so only the ledger above is counted"})
    if other:
        excluded.append({"service": "Claude: other keys on the account", "usd": _r(mon(other), 2),
                         "detail": "not the receptionist"})

    # ---- to date (since the project start)
    to_r = sum(v["usd"] for v in retell.values())
    to_c, to_t = sum(live.values()), sum(v for d, v in ledger.items() if d >= start.strftime("%Y-%m-%d"))
    fixed_start = datetime.strptime(fin.get("fixed_start", "2026-10-26"), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    months_fixed = max(0.0, (today - fixed_start).days / 30.0)
    to_fixed = sum(f["usd"] for f in fixed) * months_fixed
    low, high = cfg.get("spend_report", {}).get("project_budget_usd", [None, None])
    current = {
        "v": 1, "updatedAt": int(now.timestamp() * 1000), "month": month.strftime("%Y-%m"), "since": start.strftime("%Y-%m-%d"),
        "rows": service_rows, "measuredUsd": _r(measured, 2), "estimatedUsd": _r(estimated, 2), "totalUsd": _r(measured + estimated, 2),
        "excluded": excluded,
        "calls": {"count": r_calls, "minutes": _r(r_min, 1), "retellUsd": _r(r_usd, 2), "claudeLiveUsd": _r(c_usd, 2),
                  "perCallUsd": _r((r_usd + c_usd) / r_calls, 3) if r_calls else None,
                  "perMinuteUsd": _r((r_usd + c_usd) / r_min, 3) if r_min else None},
        "retellProducts": {k: _r(v, 2) for k, v in sorted(products.items(), key=lambda x: -x[1])},
        "claudeModels": {k: _r(v, 2) for k, v in sorted(models_live.items(), key=lambda x: -x[1])},
        "toDate": {"retellUsd": _r(to_r, 2), "claudeLiveUsd": _r(to_c, 2), "testsUsd": _r(to_t, 2), "fixedEstimateUsd": _r(to_fixed, 2),
                   "measuredUsd": _r(to_r + to_c + to_t, 2), "totalUsd": _r(to_r + to_c + to_t + to_fixed, 2)},
        "budget": {"low": low, "high": high},
        "problems": problems, "notes": notes,
    }
    return {"current": current, "daily": {"v": 1, "updatedAt": current["updatedAt"], "days": daily}}


def write_files(snapshot: dict, out_dir) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, key in (("finance-current.json", "current"), ("finance-daily.json", "daily")):
        p = out / name
        p.write_text(json.dumps(snapshot[key], indent=1))
        paths.append(p)
    return paths


def render(snapshot: dict) -> str:
    c = snapshot["current"]
    lines = [f"Running costs, {c['month']} (project since {c['since']}), generated {datetime.fromtimestamp(c['updatedAt'] / 1000, timezone.utc):%Y-%m-%d %H:%MZ}", ""]
    for r in c["rows"]:
        lines.append(f"  {r['service']:<42} ${r['usd']:>8.2f}  [{r['status']}] {r['detail']}")
    lines += ["", f"  This month: ${c['totalUsd']:.2f}  (measured ${c['measuredUsd']:.2f} + fixed estimates ${c['estimatedUsd']:.2f})"]
    t = c["toDate"]
    lines.append(f"  To date:    ${t['totalUsd']:.2f}  (Retell ${t['retellUsd']:.2f}, Claude live ${t['claudeLiveUsd']:.2f}, tests ${t['testsUsd']:.2f}, fixed est. ${t['fixedEstimateUsd']:.2f})")
    if c["calls"]["perCallUsd"] is not None:
        lines.append(f"  Per call:   ${c['calls']['perCallUsd']:.3f}   per minute: ${c['calls']['perMinuteUsd']:.3f}   ({c['calls']['count']} calls this month)")
    for e in c["excluded"]:
        lines.append(f"  Not counted: {e['service']} ${e['usd']:.2f} ({e['detail']})")
    for p in c["problems"] + c["notes"]:
        lines.append(f"  ! {p}")
    return "\n".join(lines)


# ---- per shop, per call (admin only: never on a shop's dashboard) -----------------------------------------------

def retell_call_costs(since: datetime, key: str) -> dict[str, dict]:
    """{Retell call id: {usd, minutes}} for every Retell call since `since`."""
    out, pagination_key = {}, None
    for _ in range(sr.MAX_PAGES):
        body = {"filter_criteria": {"start_timestamp": {"type": "number", "op": "ge", "value": int(since.timestamp() * 1000)}},
                "limit": 1000, "sort_order": "descending"}
        if pagination_key:
            body["pagination_key"] = pagination_key
        page = sr._http("POST", f"{sr.RETELL}/v3/list-calls", {"Authorization": f"Bearer {key}"}, body)
        for c in page.get("items", []):
            cost = c.get("call_cost") or {}
            out[c.get("call_id")] = {
                "usd": float(cost.get("combined_cost") or 0) / 100,
                "minutes": float(cost.get("total_duration_seconds") or (c.get("duration_ms") or 0) / 1000) / 60}
        pagination_key = page.get("pagination_key")
        if not page.get("has_more") or not pagination_key:
            return out
    raise RuntimeError("Retell: stopped after too many pages; the table would be incomplete")


def _minutes(row: dict) -> float:
    try:
        return max(0.0, (datetime.fromisoformat(row["ended_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds() / 60)
    except (TypeError, ValueError, KeyError):
        return 0.0


def cost_table(rows: list[dict], retell: dict[str, dict] | None) -> dict:
    """Per shop and per call: Retell cost, Claude cost, minutes and cost per minute. `rows` come from Store.call_costs;
    `retell` maps Retell call ids to their billing (None when the key is missing: Retell cost is then reported as
    unknown, never as zero)."""
    shops: dict[str, dict] = {}
    for r in rows:
        rid = (r["id"] or "").removeprefix("retell-")
        billed = (retell or {}).get(rid) if r.get("platform") == "retell" else None
        minutes = billed["minutes"] if billed else _minutes(r)
        retell_usd = billed["usd"] if billed else None
        claude = r.get("claude_usd")
        known = retell_usd is not None and claude is not None
        call = {"id": r["id"], "started_at": r["started_at"], "minutes": _r(minutes, 2), "retell_usd": None if retell_usd is None else _r(retell_usd),
                "claude_usd": None if claude is None else _r(claude),
                "total_usd": _r(retell_usd + claude) if known else None,
                "per_minute_usd": _r((retell_usd + claude) / minutes, 3) if known and minutes > 0 else None}
        shops.setdefault(r["business_id"], {"calls": []})["calls"].append(call)
    for shop in shops.values():
        full = [c for c in shop["calls"] if c["total_usd"] is not None and c["minutes"] > 0]
        mins, usd = sum(c["minutes"] for c in full), sum(c["total_usd"] for c in full)
        shop.update({"calls_counted": len(full), "calls_total": len(shop["calls"]), "minutes": _r(mins, 2),
                     "retell_usd": _r(sum(c["retell_usd"] for c in full)), "claude_usd": _r(sum(c["claude_usd"] for c in full)),
                     "total_usd": _r(usd), "per_minute_usd": _r(usd / mins, 3) if mins else None})
    return {"shops": shops, "retell_known": retell is not None}
