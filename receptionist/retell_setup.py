"""One-command Retell setup for a live line, using Retell's REST API.

    python -m receptionist retell-voices
    python -m receptionist retell-setup --base-url https://<your-domain> --number +13125550123 [--voice <voice_id>]
    python -m receptionist retell-tune      # push the business's "voice" settings to the live agent

Needs RETELL_API_KEY and RETELL_WS_SECRET in the environment (the server needs the same RETELL_WS_SECRET).
API reference: https://docs.retellai.com/api-references/create-agent
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .speech import normalize_phone

API = "https://api.retellai.com"


def _call(method: str, path: str, body: dict | None = None) -> dict | list:
    key = os.environ.get("RETELL_API_KEY")
    if not key:
        raise SystemExit("Set RETELL_API_KEY first (from the API Keys page of the Retell dashboard).")
    req = urllib.request.Request(f"{API}{path}", method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Retell {method} {path} failed: {e.code} {e.read().decode(errors='replace')[:500]}")


# Words speech recognition should favor: trade words it mishears ("sync" for "sink") and, per business, the towns.
TRADE_WORDS = ["sink", "faucet", "drain", "clog", "toilet", "leak", "drip", "pipe", "water heater", "tankless",
               "furnace", "boiler", "thermostat", "sump pump", "garbage disposal", "shutoff valve"]


def agent_settings(biz: dict) -> dict:
    """The business's voice and conversation settings (the "voice" block of its JSON) plus speech-recognition hints."""
    settings = dict(biz.get("voice", {}))
    settings["boosted_keywords"] = [biz["agent_name"], biz["spoken_name"],
                                    *(biz.get("service_area") or {}).get("towns", []),
                                    *biz.get("speech_keywords", TRADE_WORDS)]
    return settings


def tune(cfg: dict, biz: dict, base_url: str | None = None) -> dict:
    """Applies agent_settings(biz) to the live Retell agent: published versions are read-only, so this makes a new
    draft from the latest version, edits and publishes it, and points the phone number at it."""
    rec = json.loads((Path(cfg["data_dir"]) / "retell.json").read_text())
    agent_id = rec["agent_id"]
    latest = max(_call("GET", f"/list-agent-versions/{agent_id}")["items"], key=lambda v: v["version"])
    if latest.get("is_published"):
        _call("POST", f"/create-agent-version/{agent_id}", {"base_version": latest["version"]})
    changes = agent_settings(biz)
    if base_url:   # moving the server (for example from ngrok to a host): calls and webhooks go to the new address
        base = base_url.rstrip("/")
        secret = os.environ.get("RETELL_WS_SECRET")
        if not secret:
            raise SystemExit("Set RETELL_WS_SECRET first (the same value the server uses).")
        ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        changes["response_engine"] = {"type": "custom-llm", "llm_websocket_url": f"{ws_base}/llm-websocket/{secret}"}
        changes["webhook_url"] = f"{base}/retell/webhook"
        rec["base_url"] = base
        (Path(cfg["data_dir"]) / "retell.json").write_text(json.dumps(rec, indent=2))
    agent = _call("PATCH", f"/update-agent/{agent_id}", changes)   # edits the latest draft
    _call("POST", f"/publish-agent-version/{agent_id}", {"version": agent["version"],
                                                          "version_title": "Voice and speech settings"})
    _call("PATCH", f"/update-phone-number/{rec['number']}",
          {"inbound_agents": [{"agent_id": agent_id, "agent_version": agent["version"], "weight": 1}]})
    return agent


def list_voices() -> list[dict]:
    voices = _call("GET", "/list-voices")
    return [v for v in voices if "en" in (v.get("accent", "") + v.get("language", "") + "en").lower()]


def setup(cfg: dict, biz_path: Path, base_url: str, number: str, voice_id: str, language: str) -> dict:
    secret = os.environ.get("RETELL_WS_SECRET")
    if not secret:
        raise SystemExit('Set RETELL_WS_SECRET first, e.g.: echo "export RETELL_WS_SECRET=$(openssl rand -hex 16)" '
                         '>> ~/.zshrc && source ~/.zshrc')
    e164 = normalize_phone(number)
    if not e164:
        raise SystemExit(f"{number} isn't a full phone number.")
    base = base_url.rstrip("/")
    ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    biz = json.loads(biz_path.read_text())
    agent = _call("POST", "/create-agent", {
        "agent_name": f"{biz['name']} receptionist",
        "response_engine": {"type": "custom-llm", "llm_websocket_url": f"{ws_base}/llm-websocket/{secret}"},
        **agent_settings(biz),
        "voice_id": voice_id or biz.get("voice", {}).get("voice_id"),
        "webhook_url": f"{base}/retell/webhook",
        "language": language,
        "end_call_after_silence_ms": 30000,
        "max_call_duration_ms": 1200000,
    })
    # New agents are drafts; a phone number only routes calls to a published version.
    _call("POST", f"/publish-agent-version/{agent['agent_id']}", {"version": agent.get("version", 0),
                                                                    "version_title": "Initial setup"})
    _call("PATCH", f"/update-phone-number/{e164}", {"inbound_agents": [{"agent_id": agent["agent_id"], "weight": 1}]})
    if e164 not in biz.get("phone_numbers", []):   # the server picks the business by the number dialed
        biz["phone_numbers"] = [*biz.get("phone_numbers", []), e164]
        biz_path.write_text(json.dumps(biz, indent=2, ensure_ascii=False) + "\n")
    record = {"agent_id": agent["agent_id"], "number": e164, "voice_id": voice_id, "base_url": base}
    (Path(cfg["data_dir"]) / "retell.json").write_text(json.dumps(record, indent=2))
    return record
