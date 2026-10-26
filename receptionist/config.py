"""Config loading. Set RECEPTIONIST_CONFIG=/path/to/other.json to run against a separate config and data folder."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .speech import normalize_phone

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | None = None) -> dict:
    p = Path(path or os.environ.get("RECEPTIONIST_CONFIG") or ROOT / "config.json")
    cfg = json.loads(p.read_text())
    for key in ("data_dir", "businesses_dir", "scenarios_dir"):
        cfg[key] = str((p.parent / cfg[key]).resolve())
    if os.environ.get("RECEPTIONIST_DATA_DIR"):   # a hosted server keeps its database on a mounted disk
        cfg["data_dir"] = str(Path(os.environ["RECEPTIONIST_DATA_DIR"]).resolve())
    Path(cfg["data_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg


def load_business(cfg: dict, business_id: str | None = None) -> dict:
    path = Path(cfg["businesses_dir"]) / f"{business_id or cfg['default_business']}.json"
    if not path.exists():
        raise SystemExit(f"No business config at {path}")
    return json.loads(path.read_text())


def all_businesses(cfg: dict) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(Path(cfg["businesses_dir"]).glob("*.json"))]


def business_for_number(cfg: dict, number: str | None) -> dict | None:
    """The business is chosen by the number that was dialed, never by anything the caller says."""
    dialed = normalize_phone(number)
    if not dialed:
        return None
    for biz in all_businesses(cfg):
        if dialed in {normalize_phone(n) for n in biz.get("phone_numbers", [])}:
            return biz
    return None
