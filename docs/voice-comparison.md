# Blind voice comparison (W4.5)

Why: Retell's own voice line is about 7 cents of a 16 to 18 cent minute (the current ElevenLabs voice). A voice at about
1.5 cents a minute saves roughly 6 cents a minute. The choice is yours; this makes it a fair test.

I could not make the audio myself: it needs each provider's playground or a Retell test call, and the price and voice
names change, so check the current voice list and price in the Retell dashboard before you start.

## Steps (about 40 minutes, no spend if you use the providers' free playgrounds)
1. Pick the three calls: `data/recordings/` has two; any simulated call works. Write the agent's lines out:
   `python -m receptionist voice-test lines --report data/runs/<report>.json <scenario> <scenario> <scenario>`
   (files land in `data/voice-test/`).
2. In Retell's voice picker, find two voices priced at about 1.5 cents a minute (the cheaper providers listed there).
   Keep the current voice as the third.
3. For each voice and each call, paste the lines into the provider's free text-to-speech playground (or run a Retell
   web test call with that voice) and save the audio as `data/voice-test/audio/<voice name>/<scenario>.mp3`.
4. `python -m receptionist voice-test blind` shuffles them into `data/voice-test/blind/<scenario>/sample-A.mp3` and so on.
   Listen without opening `key.json`. Note which you would put a real customer's callers through.
5. Open `key.json`, then tell me the winner. Switching is one line in the shop JSON's `voice` block plus `retell-tune`.

Judge on: does it sound like a person at a desk, are phone numbers and street names clear, does it keep up when the
caller talks fast. The cheap voice also has to survive latency: check tts p50 stays under 400 ms
(`python -m receptionist latency-report --last 10` after a few test calls).
