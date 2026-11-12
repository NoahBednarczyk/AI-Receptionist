"""Blind voice comparison for the owner (W4.5). No accounts, no spend: it prepares the agent's lines and hides which
voice is which. The audio itself is made by hand in each provider's free playground or a Retell test call (docs/voice-comparison.md).

    python -m receptionist voice-test lines --report data/runs/<report>.json a_scenario b_scenario c_scenario
    python -m receptionist voice-test blind --dir data/voice-test/audio     # audio/<voice name>/<scenario>.mp3
"""
from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

from .recording import speakable


def agent_lines(report: dict, scenario_id: str) -> list[str]:
    """The agent's side of a simulated call, as a phone voice should say it (digits one by one, no stage directions)."""
    result = next(r for r in report["results"] if r["id"] == scenario_id)
    return [speakable(t["text"]) for t in result["transcript"] if isinstance(t, dict) and t.get("role") == "agent" and t.get("text")]


def write_lines(report_path: str, scenarios: list[str], out_dir: str) -> list[Path]:
    report = json.loads(Path(report_path).read_text())
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for sid in scenarios:
        path = out / f"{sid}.txt"
        path.write_text("\n\n".join(agent_lines(report, sid)) + "\n")
        written.append(path)
    return written


def blind(audio_dir: str, out_dir: str, seed: int | None = None) -> dict:
    """Copies audio/<voice>/<scenario>.* into out_dir/<scenario>/sample-<letter>.<ext>, shuffled per scenario, and writes
    key.json (letter -> voice) next to it. Listen first, open the key after."""
    rng = random.Random(seed)
    root, out = Path(audio_dir), Path(out_dir)
    voices = sorted(p.name for p in root.iterdir() if p.is_dir())
    if len(voices) < 2:
        raise SystemExit("Need audio/<voice>/<scenario>.mp3 for at least two voices.")
    scenarios = sorted({f.stem for v in voices for f in (root / v).iterdir() if f.is_file()})
    key = {}
    for sid in scenarios:
        have = [v for v in voices if any((root / v).glob(f"{sid}.*"))]
        rng.shuffle(have)
        key[sid] = {}
        for letter, v in zip("ABCDEFGH", have):
            src = next((root / v).glob(f"{sid}.*"))
            dest = out / sid / f"sample-{letter}{src.suffix}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dest)
            key[sid][letter] = v
    (out / "key.json").write_text(json.dumps(key, indent=2))
    return key
