"""Daily testing budget. Every simulated call and text chat records what it cost in data/spend.json;
a run that would go over the day's budget refuses to start, and one that gets there stops early.

Costs are computed from token counts and config.json's prices, so they track the bill closely but not exactly.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date
from pathlib import Path

_lock = threading.Lock()


def _path(cfg: dict) -> Path:
    return Path(cfg["data_dir"]) / "spend.json"


def _read(cfg: dict) -> dict:
    try:
        return json.loads(_path(cfg).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"days": {}, "per_scenario": None}


def _write(cfg: dict, data: dict) -> None:
    tmp = _path(cfg).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, _path(cfg))   # atomic: readers see the old file or the new one, never half of one


def _limit(cfg: dict, data: dict) -> float:
    raised = data.get("limits", {}).get(date.today().isoformat())
    return float(raised if raised is not None else cfg["simulate"]["budget_per_day"])


def daily_limit(cfg: dict) -> float:
    """config.json's daily budget, unless the user raised today's limit with `budget --raise-today`."""
    return _limit(cfg, _read(cfg))


def raise_today(cfg: dict, dollars: float) -> None:
    """A one-day limit; tomorrow goes back to config.json's budget_per_day."""
    with _lock:
        data = _read(cfg)
        data.setdefault("limits", {})[date.today().isoformat()] = round(dollars, 2)
        _write(cfg, data)


def spent_today(cfg: dict) -> float:
    return _read(cfg)["days"].get(date.today().isoformat(), 0.0)


def remaining(cfg: dict) -> float:
    data = _read(cfg)   # one read, so the limit and the spend come from the same version of the file
    return max(0.0, _limit(cfg, data) - data["days"].get(date.today().isoformat(), 0.0))


def record(cfg: dict, dollars: float) -> float:
    """Adds to today's spend; returns the new total."""
    with _lock:
        data = _read(cfg)
        today = date.today().isoformat()
        data["days"][today] = round(data["days"].get(today, 0.0) + dollars, 6)
        _write(cfg, data)
        return data["days"][today]


def per_scenario_estimate(cfg: dict) -> float:
    """The larger of the configured estimate and the last run's measured average, so estimates err high."""
    measured = _read(cfg).get("per_scenario")
    return max(float(cfg["simulate"]["estimated_cost_per_scenario"]), measured or 0.0)


def remember_per_scenario(cfg: dict, dollars: float) -> None:
    with _lock:
        data = _read(cfg)
        data["per_scenario"] = round(dollars, 6)
        _write(cfg, data)


def check_affordable(cfg: dict, scenarios: int) -> float:
    """Raises SystemExit with a plain explanation if the run won't fit in today's budget; returns the estimate."""
    estimate = scenarios * per_scenario_estimate(cfg)
    left = remaining(cfg)
    if estimate > left:
        fits = int(left // per_scenario_estimate(cfg))
        if fits == 0:
            raise SystemExit(f"Today's ${daily_limit(cfg):.2f} testing budget is used up "
                             f"(${spent_today(cfg):.2f} spent). Try again tomorrow.")
        raise SystemExit(
            f"This run would cost about ${estimate:.2f}, and ${left:.2f} of today's ${daily_limit(cfg):.2f} testing "
            f"budget is left (about {fits} scenario{'s' if fits != 1 else ''}). Use --quick, name fewer scenarios, "
            "or wait until tomorrow.")
    return estimate
