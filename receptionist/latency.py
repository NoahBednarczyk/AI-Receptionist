"""latency-report: read-only. For the last N Retell calls, where the time goes between the caller finishing a sentence and
the voice starting. Field names are from Retell's v3 list-calls reference: each call has `latency` with e2e, llm and tts
objects (p50, p90, ... in milliseconds), `agent_id` and `agent_name`; the voice model is on the agent (GET /get-agent)."""
from __future__ import annotations

import json
import os
import statistics
from pathlib import Path

from . import spend_report as sr

TTS_SLOW_MS = 400          # above this, the voice model is the thing to look at
REMAINDER_SLOW_MS = 700    # above this on the receptionist, end-of-speech detection and network dominate


def fetch_calls(key: str, last: int, agent_id: str | None = None, http=None) -> list[dict]:
    http = http or sr._http
    criteria: dict = {"call_status": {"type": "enum", "op": "in", "value": ["ended"]}}
    if agent_id:
        criteria["agent"] = [{"agent_id": agent_id}]
    page = http("POST", f"{sr.RETELL}/v3/list-calls", {"Authorization": f"Bearer {key}"},
                {"filter_criteria": criteria, "sort_order": "descending", "limit": max(1, min(last, 1000))})
    return page.get("items", [])


def voice_model(key: str, agent_id: str, cache: dict, http=None) -> str:
    http = http or sr._http
    if agent_id not in cache:
        try:
            a = http("GET", f"https://api.retellai.com/get-agent/{agent_id}", {"Authorization": f"Bearer {key}"})
            cache[agent_id] = f"{a.get('voice_model') or '?'} / {a.get('voice_id') or '?'}"
        except RuntimeError:
            cache[agent_id] = "-"
    return cache[agent_id]


def _p(call: dict, part: str, q: str) -> float | None:
    v = ((call.get("latency") or {}).get(part) or {}).get(q)
    return float(v) if v is not None else None


def our_first_words(cfg: dict | None, call_id: str) -> float | None:
    """Our own first-words median (seconds), where we stored one: finished outbound calls keep it on their result row."""
    if not cfg:
        return None
    p = Path(cfg["data_dir"]) / "outbound.json"
    if not p.exists():
        return None
    for r in json.loads(p.read_text()).get("calls", []):
        if r.get("callId") == call_id and r.get("latency"):
            return r["latency"].get("median_s")
    return None


def rows(calls: list[dict], key: str | None = None, cfg: dict | None = None, http=None) -> list[dict]:
    out, voices = [], {}
    for c in calls:
        e2e, llm, tts = _p(c, "e2e", "p50"), _p(c, "llm", "p50"), _p(c, "tts", "p50")
        out.append({
            "call_id": c.get("call_id"), "agent": c.get("agent_name") or c.get("agent_id"),
            "e2e_p50": e2e, "e2e_p90": _p(c, "e2e", "p90"), "llm_p50": llm, "llm_p90": _p(c, "llm", "p90"),
            "tts_p50": tts, "tts_p90": _p(c, "tts", "p90"),
            "remainder_p50": None if None in (e2e, llm, tts) else round(e2e - llm - tts, 1),   # end-of-speech detection plus network
            "ours_first_words_s": our_first_words(cfg, c.get("call_id") or ""),
            "voice": voice_model(key, c["agent_id"], voices, http) if key and c.get("agent_id") else "-",
            "is_outbound": "discovery" in (c.get("agent_name") or "").lower() or c.get("direction") == "outbound",
        })
    return out


def _med(values) -> float | None:
    v = [x for x in values if x is not None]
    return statistics.median(v) if v else None


def advice(table: list[dict]) -> list[str]:
    """Report, never change: what the numbers point at."""
    notes = []
    tts = _med(r["tts_p50"] for r in table)
    if tts is not None and tts > TTS_SLOW_MS:
        notes.append(f"tts p50 is {tts:.0f} ms (over {TTS_SLOW_MS}): the voice model is the next lever. It is Noah's decision "
                     "(eleven_v3_conversational was never measured against a faster model).")
    rec = _med(r["remainder_p50"] for r in table if not r["is_outbound"])
    if rec is not None and rec > REMAINDER_SLOW_MS:
        notes.append(f"remainder p50 on the receptionist is {rec:.0f} ms (over {REMAINDER_SLOW_MS}): end-of-speech detection "
                     "and network dominate. enable_dynamic_responsiveness (on since 2026-11-02) is the likely cause. Noah's setting; not changed.")
    return notes


def render(table: list[dict]) -> str:
    def n(x):
        return "-" if x is None else f"{x:.0f}"
    head = f"{'call':<14}{'agent':<24}{'e2e p50/p90':>14}{'llm p50/p90':>14}{'tts p50/p90':>14}{'remainder':>11}{'ours s':>8}  voice"
    lines = [head]
    for r in table:
        lines.append(f"{str(r['call_id'])[-12:]:<14}{str(r['agent'])[:22]:<24}{n(r['e2e_p50']) + '/' + n(r['e2e_p90']):>14}"
                     f"{n(r['llm_p50']) + '/' + n(r['llm_p90']):>14}{n(r['tts_p50']) + '/' + n(r['tts_p90']):>14}"
                     f"{n(r['remainder_p50']):>11}{'-' if r['ours_first_words_s'] is None else r['ours_first_words_s']:>8}  {r['voice']}")
    if table:
        lines.append(f"median of medians: e2e {n(_med(r['e2e_p50'] for r in table))} ms, llm {n(_med(r['llm_p50'] for r in table))}, "
                     f"tts {n(_med(r['tts_p50'] for r in table))}, remainder {n(_med(r['remainder_p50'] for r in table))}")
    lines += advice(table)
    return "\n".join(lines)


def report(last: int, cfg: dict | None = None, env=os.environ, http=None) -> list[dict]:
    key = env.get("RETELL_API_KEY")
    if not key:
        raise RuntimeError("RETELL_API_KEY is not set here")
    return rows(fetch_calls(key, last, http=http), key, cfg, http)
