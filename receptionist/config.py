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
    # Real shops live beside the database (on Render, the mounted disk), never in the public repo. data/ is gitignored.
    cfg["clients_dir"] = str(Path(cfg["data_dir"]) / "clients")
    return cfg


def _folders(cfg: dict) -> list[Path]:
    """The repo's demo shops, then the real clients (a folder that does not exist yet is simply empty)."""
    return [Path(cfg["businesses_dir"])] + ([Path(cfg["clients_dir"])] if cfg.get("clients_dir") else [])


def load_business(cfg: dict, business_id: str | None = None) -> dict:
    name = f"{business_id or cfg['default_business']}.json"
    for folder in _folders(cfg):
        if (folder / name).exists():
            return json.loads((folder / name).read_text())
    raise SystemExit(f"No business config named {name} in {' or '.join(str(f) for f in _folders(cfg))}")


def all_businesses(cfg: dict) -> list[dict]:
    """Every shop from both folders. A repeated id or a repeated phone number would route one shop's calls, or one
    shop's data, to another, so it is an error rather than a quiet winner."""
    found, ids, numbers = [], {}, {}
    for folder in _folders(cfg):
        for p in sorted(folder.glob("*.json")) if folder.exists() else []:
            biz = json.loads(p.read_text())
            if biz["id"] in ids:
                raise ValueError(f"Business id {biz['id']!r} is in both {ids[biz['id']]} and {p}")
            ids[biz["id"]] = p
            for n in biz.get("phone_numbers", []):
                key = normalize_phone(n)
                if key in numbers:
                    raise ValueError(f"Phone number {n} is on both {numbers[key]} and {biz['id']}")
                numbers[key] = biz["id"]
            found.append(biz)
    return found


def business_for_number(cfg: dict, number: str | None) -> dict | None:
    """The business is chosen by the number that was dialed, never by anything the caller says."""
    dialed = normalize_phone(number)
    if not dialed:
        return None
    for biz in all_businesses(cfg):
        if dialed in {normalize_phone(n) for n in biz.get("phone_numbers", [])}:
            return biz
    return None
