"""What the server needs from a Retell webhook: the signature check and the transcript. (The platform-hosted agent
paths that used to live in platforms.py were removed: only our own agent loop, with its guards, answers calls.)"""
from __future__ import annotations

import hashlib
import hmac
import re
import time


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
