import json
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401
from receptionist import voicetest


class VoiceTest(unittest.TestCase):
    def test_lines_are_the_agents_side_read_for_a_phone_voice(self):
        with tempfile.TemporaryDirectory() as d:
            rep = Path(d) / "r.json"
            rep.write_text(json.dumps({"results": [{"id": "s", "transcript": [
                {"role": "agent", "text": "Call 208-555-0142."}, {"role": "caller", "text": "ok"}]}]}))
            [out] = voicetest.write_lines(str(rep), ["s"], d)
            self.assertIn("2 0 8", out.read_text())
            self.assertNotIn("ok", out.read_text())

    def test_blind_hides_the_voice_names_and_keeps_a_key(self):
        with tempfile.TemporaryDirectory() as d:
            for v in ("cheap1", "cheap2", "current"):
                (Path(d) / "audio" / v).mkdir(parents=True)
                (Path(d) / "audio" / v / "call1.mp3").write_bytes(v.encode())
            key = voicetest.blind(d + "/audio", d + "/blind", seed=1)
            files = sorted(p.name for p in (Path(d) / "blind" / "call1").iterdir())
            self.assertEqual(files, ["sample-A.mp3", "sample-B.mp3", "sample-C.mp3"])
            for letter, voice in key["call1"].items():
                self.assertEqual((Path(d) / "blind" / "call1" / f"sample-{letter}.mp3").read_bytes(), voice.encode())
            self.assertTrue((Path(d) / "blind" / "key.json").exists())


if __name__ == "__main__":
    unittest.main()
