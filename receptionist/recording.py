"""Turn a simulated call's transcript into audio you can listen to (macOS `say` + ffmpeg, free and local).

    python -m receptionist record data/runs/<report>.json <scenario_id>

The agent's pause before each reply is the time to first words measured in that run, so the recording
sounds as fast or slow as the real agent was. On the phone line, the voice comes from the voice platform
(Retell or Vapi), which sounds far more natural than macOS voices; this is a preview of the conversation.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

VOICES = {"agent": ("Samantha", 185), "caller": ("Reed (English (US))", 180),
          "agent_es": ("Paulina", 180), "caller_es": ("Eddy (Spanish (Mexico))", 175)}
TOOL_PAUSE_S = 1.0     # after a "one moment" line: the tool runs and the model writes its reply
CALLER_PAUSE_S = 0.5   # people answer quickly


def speakable(text: str) -> str:
    """What a phone voice should say: digits read one by one, no stage directions."""
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\b(\d{3})-(\d{3})-(\d{4})\b", lambda m: ", ".join(" ".join(g) for g in m.groups()), text)
    text = re.sub(r"\b([A-Z])(\d{4})\b", lambda m: f"{m.group(1)} " + " ".join(m.group(2)), text)
    return re.sub(r"\s+", " ", text).strip()


def lines(transcript: str) -> list[tuple[str, str]]:
    out = []
    for raw in transcript.splitlines():
        if raw.startswith("Agent: "):
            out.append(("agent", raw[7:]))
        elif raw.startswith("Caller: "):
            out.append(("caller", raw[8:]))
        elif out and out[-1][0] == "caller" and raw.strip() and not raw.startswith("  [tool"):
            out[-1] = ("caller", out[-1][1] + " " + raw.strip())   # caller text that ran over several lines
    return [(who, speakable(t)) for who, t in out if speakable(t)]


def record(report: str, scenario_id: str, out_dir: str) -> Path:
    result = next(r for r in json.loads(Path(report).read_text())["results"] if r["id"] == scenario_id)
    convo = lines(result["transcript"])
    spanish = "spanish" in scenario_id
    delays = list(result.get("first_words_s") or [])
    out = Path(out_dir) / f"{scenario_id}.m4a"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        parts, prev = [], None
        for i, (who, text) in enumerate(convo):
            voice, rate = VOICES[f"{who}_es" if spanish else who]
            if prev is None:
                pause = 0.3
            elif who == "caller":
                pause = CALLER_PAUSE_S
            elif prev == "agent":
                pause = TOOL_PAUSE_S
            else:
                pause = min(delays.pop(0), 4.0) if delays else 1.0
            silence = Path(tmp) / f"{i:03d}s.wav"
            speech_aiff = Path(tmp) / f"{i:03d}.aiff"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
                            "-t", f"{pause:.2f}", str(silence)], check=True)
            subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", str(speech_aiff), text], check=True)
            speech = Path(tmp) / f"{i:03d}.wav"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(speech_aiff), "-ar", "22050", "-ac", "1",
                            str(speech)], check=True)
            parts += [silence, speech]
            prev = who
        listing = Path(tmp) / "parts.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts))
        # Narrow the band to 300-3400 Hz so it sounds like a phone line.
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                        "-af", "highpass=f=300,lowpass=f=3400,volume=1.4", "-c:a", "aac", "-b:a", "96k", str(out)],
                       check=True)
    return out
