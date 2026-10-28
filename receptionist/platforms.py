"""Voice platform adapters: the agent config each platform needs, and parsing of what each one sends us.

Both platforms run the same prompt and the same tools against our webhook server. Two tools differ:
transfers and hang-ups are native platform tools, because only the platform can move or end the call.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time

from . import prompts, tools

NATIVE = {"transfer_call", "end_call"}   # the platform's own tools: only it can move or end the call

VAPI_NOTE = """\
# Tool names on this phone line
To transfer, use transferCall and choose the destination for the target you picked: {destinations}.
To hang up, use endCall. Every other tool works as described."""

RETELL_NOTE = """\
# Tool names on this phone line
To transfer, use {names}. To hang up, use end_call. Every other tool works as described."""


def _custom_tools(biz: dict) -> list[dict]:
    return [t for t in tools.schemas(biz) if t["name"] not in NATIVE]


# ---- Vapi ---------------------------------------------------------------------

def vapi_assistant(cfg: dict, biz: dict, base_url: str, call_state_text: str | None, token: str | None = None) -> dict:
    """Inline assistant for an assistant-request reply, built fresh for each call."""
    server = {"url": f"{base_url}/vapi/webhook"}
    if token:
        server["headers"] = {"Authorization": f"Bearer {token}"}
    destinations = ", ".join(f"{key} is {t['number']}" for key, t in biz["transfer_targets"].items())
    system = "\n\n".join(x for x in (prompts.stable_prompt(biz), VAPI_NOTE.format(destinations=destinations),
                                     call_state_text) if x)
    fillers = tools.FILLERS["en"]
    fn_tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": t["input_schema"]}, "server": server,
                 **({"messages": [{"type": "request-start", "content": fillers[t["name"]]}]} if t["name"] in fillers else {})}
                for t in _custom_tools(biz)]
    # Spoken by the platform as the transfer starts, so the caller never hears silence before the ringing.
    transfer = {"type": "transferCall", "destinations": [
        {"type": "number", "number": t["number"], "description": f"{key}: {t['label']}. {t['when']}",
         "message": tools.FILLERS["en"]["transfer_call"]}
        for key, t in biz["transfer_targets"].items()]}
    return {
        "name": f"{biz['name']} receptionist"[:40],
        "firstMessage": prompts.announce(biz, biz["greeting"]),
        "model": {"provider": "anthropic", "model": cfg["platforms"]["vapi_model"],
                  "messages": [{"role": "system", "content": system}],
                  "tools": fn_tools + [transfer, {"type": "endCall"}]},
        "server": server,
    }


def vapi_call_info(msg: dict) -> tuple[str | None, str | None, str | None]:
    """(call id, dialed number, caller number) from any Vapi server message."""
    call = msg.get("call") or {}
    phone = msg.get("phoneNumber") or call.get("phoneNumber") or {}
    customer = msg.get("customer") or call.get("customer") or {}
    return call.get("id"), phone.get("number"), customer.get("number")


def vapi_tool_calls(msg: dict) -> list[tuple[str, str, dict]]:
    out = []
    for tc in msg.get("toolCallList") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        out.append((tc.get("id"), fn.get("name"), args))
    return out


def vapi_transcript(msg: dict) -> list[dict]:
    out = []
    for m in (msg.get("artifact") or {}).get("messages") or []:
        role = {"assistant": "agent", "bot": "agent", "user": "caller"}.get(m.get("role"))
        if role and m.get("message"):
            out.append({"role": role, "text": m["message"]})
    return out


# ---- Retell -------------------------------------------------------------------

def retell_llm(cfg: dict, biz: dict, base_url: str) -> dict:
    """Body for POST https://api.retellai.com/create-retell-llm. The inbound webhook fills {{call_state}} per call."""
    transfer_names = [f"transfer_call_{key}" for key in biz["transfer_targets"]]
    general_tools = [{"type": "custom", "name": t["name"], "description": t["description"],
                      "url": f"{base_url}/retell/tool/{t['name']}", "method": "POST", "parameters": t["input_schema"],
                      "speak_during_execution": t["name"] in tools.FILLERS["en"], "speak_after_execution": True,
                      **({"execution_message_type": "static_text",
                          "execution_message_description": tools.FILLERS["en"][t["name"]]}
                         if t["name"] in tools.FILLERS["en"] else {}),
                      "timeout_ms": 10000}
                     for t in _custom_tools(biz)]
    general_tools += [{"type": "transfer_call", "name": f"transfer_call_{key}", "description": f"{t['label']}. {t['when']}",
                       "transfer_destination": {"type": "predefined", "number": t["number"]},
                       "transfer_option": {"type": "cold_transfer", "show_transferee_as_caller": True}}
                      for key, t in biz["transfer_targets"].items()]
    general_tools.append({"type": "end_call", "name": "end_call", "description": "Hang up after your goodbye."})
    prompt = "\n\n".join([prompts.stable_prompt(biz), RETELL_NOTE.format(names=" or ".join(transfer_names)),
                          "{{call_state}}"])
    return {
        "model_config": {"type": "cascading", "primary": {"model": cfg["platforms"]["retell_model"]}},
        "general_prompt": prompt,
        "begin_message": prompts.announce(biz, biz["greeting"]),
        "start_speaker": "agent",
        "general_tools": general_tools,
        "default_dynamic_variables": {"call_state": "# This call\nCall details are unavailable."},
    }


def retell_signature_ok(raw_body: bytes, api_key: str, signature: str | None, now_ms: int | None = None) -> bool:
    """Retell signs as v=<ms timestamp>,d=<hex HMAC-SHA256 of body + timestamp>, valid for 5 minutes."""
    m = re.fullmatch(r"v=(\d+),d=([0-9a-f]{64})", signature or "")
    if not m:
        return False
    stamp = int(m.group(1))
    if abs((now_ms if now_ms is not None else int(time.time() * 1000)) - stamp) > 5 * 60 * 1000:
        return False
    expected = hmac.new(api_key.encode(), raw_body + str(stamp).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, m.group(2))


def retell_transcript(call: dict) -> list[dict]:
    out = []
    for m in call.get("transcript_object") or []:
        role = {"agent": "agent", "user": "caller"}.get(m.get("role"))
        if role and m.get("content"):
            out.append({"role": role, "text": m["content"]})
    if not out and call.get("transcript"):
        for line in call["transcript"].splitlines():
            speaker, _, text = line.partition(":")
            if text.strip():
                out.append({"role": "agent" if speaker.strip().lower() == "agent" else "caller", "text": text.strip()})
    return out
