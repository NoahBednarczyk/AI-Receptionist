# Receptionist: notes for Claude

An AI phone receptionist for small home-services businesses, sold as a monthly subscription. Phase 0 prototype for a demo business, Acme Plumbing & Heating. `README.md` has the commands, results, cost model and go-live checklist; read it first.

- **Design doc** (product, pricing, go-to-market, roadmap): https://claude.ai/artifact/GtSqWJL89vevGzkp3oeMUN
- **Code:** `receptionist/` (`live.py` is the Retell live-call websocket, `dashboard.py` the owner page, `agent.py` the call loop, `tools.py` the tools, `prompts.py` the prompt layers, `simulate.py` the test calls). Business data is in `businesses/acme.json`, the vertical playbook in `receptionist/playbooks/home_services.md`, the scenarios in `scenarios/home_services.json`, and run reports with full transcripts in `data/runs/`.
- **Models (changed 2026-10-26 afternoon):** the agent is back on Claude Sonnet 5.5 (`between_tools`) because the user wanted it smarter and with more personality; Haiku 5.5 (thinking disabled) was tried for speed that morning and was less accurate. Switch via config.json models.agent and agent_thinking. The summary and grader use Sonnet 5.5; the test caller uses Haiku 5.5.
- **Guards that make thinking-off safe (2026-10-25):** `safety.py` speaks the emergency safety lines from code the moment a caller describes one (gas or CO with the caller inside is a fully fixed turn that hangs up); `agent.Sentences` releases speech a sentence at a time and drops thinking out loud or internals (`speech.unspeakable`), also from history; requests cache the whole conversation (top-level `cache_control`). Keep-warm between calls is a free model lookup; the prompt cache is written when each call starts.

## Rules the user set

- **Testing budget: $1 a day of API spend**, enforced by `receptionist/budget.py` (`python -m receptionist budget`). Estimate before any live run, prefer `simulate --quick` or named scenarios while iterating, and run the full suite only to confirm. Raise the limit only when the user says so in chat (`budget --raise-today N`, which lasts one day). Ad-hoc scripts that call the API count too.
- **AI disclosure:** the greeting doesn't announce "virtual assistant" (the user's choice), but the agent must always say plainly that it's the business's virtual assistant when asked, and never claim to be human. Don't change that second part.
- **Advice:** approved tips (`approved_tips` in the business JSON) plus simple, safe steps; never gas, electrical work beyond the approved tips, opening appliances, tools or chemicals; no diagnosis; emergencies stay on the playbook's safety lines.

## Where things stand (2026-10-26)

- **Live on Retell** (custom-LLM websocket through ngrok `spent-henna-rut.ngrok-free.dev`, number +1 208-428-6760, agent version 1). Start the server in a terminal: `PUBLIC_BASE_URL=https://spent-henna-rut.ngrok-free.dev .venv/bin/python -m receptionist serve --port 8000`, with ngrok running.
- **Service area:** Sandpoint, Idaho and 23 towns around it (Bonners Ferry to Athol, Priest Lake to Clark Fork), Pacific time. `_town` ignores spacing and a trailing state ("Sand Point", "Sandpoint, Idaho").
- **Voice:** ElevenLabs v4 Turbo `11labs-Cleo` (Cartesia Cleo as fallback), backchannels, interruption sensitivity 0.8, light call-center ambience, and boosted keywords (towns plus trade words). Settings live in the business JSON's `voice` block; `python -m receptionist retell-tune` makes a new agent version, publishes it and points the number at it (published versions are read-only).
- **Live-call turn handling (`live.py`):** a newer reply request drops the turn in progress unless it already did something irreversible (`agent.TurnControl`); a finished reply the caller never heard is taken back (`CallSession.revert_last`); after the goodbye, further requests just hang up. Before this, stale turns stacked up (3.1 s end to end) and the agent asked twice or said "the call has already ended".
- **Speech guards:** `agent.Sentences` caps a reply at 40 words (the rest waits for the next turn), says "anything else I can help with" once a call, and drops thinking out loud (`speech.unspeakable`).
- **Diagnostics 2026-10-26:** round 1, 14/29 (long replies, small invented reassurances, spoken reasoning). After the fixes: 6/8, then 6/9, then a final 21-scenario run at 15/21. The 8 left out had passed earlier that day, so the suite stands at about 23/29, with first words at a 1.5 s median and 2.3 s at the 95th percentile. Root causes fixed afterwards, verified 3/3:
  - The service list was ambiguous (toilets and faucets named only under installation), so the agent reasoned aloud about which to pick; services now say what they cover.
  - Predictable questions had no facts ("do I need to be home?", "what's your number?"); there's now an FAQ, and `call_state` gives the number dialed.
  - The tool's "no text is sent" clashed with the technician's call-or-text.
  - Tips said "until the technician arrives", which promised a visit to out-of-area callers; they're neutral now, and the conflicting town-before-tips rule is gone.
  - The tip rule ("the whole reply") beat the returning-customer address check; the address now comes first.
  - The 40-word cap held back questions silently; the model is now told what wasn't heard (`Sentences.held`).
  The being-home FAQ is a demo answer: each real client must set their own. Today's test budget ($3) is spent.

### Voice, 2026-10-26 (morning)
Live voice is ElevenLabs v4 Turbo `11labs-Kate`, agent version 3. Backchannel and call-center ambience are OFF: together they made Retell treat noise as caller speech, so every reply was superseded and discarded ("took back a reply the caller never heard" six times in one call) and the agent said "mm-hmm". Do not turn them back on. Live model first-words time is 1.8-2.8 s (1.5 s in simulation, extra is network); faster means Haiku, which costs accuracy.

### Full review, 2026-10-26 (late)
Fixed:
- Live transfers never dial 555-01xx placeholder numbers; they act like no answer (`ToolContext.real_transfers`).
- `lookup_customer` reveals a record only to the caller ID's owner, or after the name on the account matches.
- No texts are queued while `texting_enabled` is false (`tools._text`, and the owner summary text in `server.py`).
- `take_message` needs a full 10-digit number.
- Card numbers are replaced with "[card number removed]" before the model, transcript or database sees them, and they no longer trigger the short-phone-number note (which would have asked for more card digits).
- Safety negation is clause-aware ("I don't know, I smell gas" now triggers); Spanish safety lines are spoken by code too.
- On live calls the agent is told what the caller heard of an interrupted reply (`live.cut_short`).
- A reply that was filtered out entirely gets one retry instead of "Sorry, could you say that again?".
- Demo data re-seeds daily, compared in the business's time zone.
- The dialed number reaches every platform path, and the post-call summary gets the call details.

Three deep test calls (`deep_*` scenarios): the returning customer on another phone and the Spanish adversarial call passed. The late-night flood failed once because the agent tried `take_message` with "unknown" before it had a number. After the fix (tool description plus playbook) it passed on re-run, so all three pass. The caller never asked about price in either flood run, so that trap is untested.

## Where things stood (2026-10-23)

- The harder 28-scenario suite passes about two thirds of calls (64–68% per pair of runs). Outcome-level problems were all fixed in code (dates, silent transfers and hang-ups, guessed digits, double bookings). What's left is wording: replies over 45 words, small promises, the odd stacked question. Runs vary by about two calls in 15, so judge changes on two runs.
- Listen to calls with `python -m receptionist record data/runs/<report>.json <scenario>` (macOS voices; free). Examples are in `data/recordings/`.
- Time to first words: median about 1.4 s. The fixed "One moment..." lines (`tools.FILLERS`) cover tool calls.
- **Texting is pinned** (`texting_enabled: false`) until the phone line works; the owner uses the dashboard.
- **Not live yet.** See "Going live: what's left" in `README.md`: a voice platform account, hosting, a Twilio SMS sender with 10DLC, calendar sync or a dashboard, a real client's config, and legal review.
- **Recommended architecture for going live:** run our own agent loop in production through the platform's custom-LLM option (Retell Custom LLM websocket or Vapi custom-llm), not the platform-hosted model. Only then do the thinking setting, language-aware fixed lines, the short-phone-number note and the other helpers in `agent.py` apply on real calls.

### Greeting and voice, 2026-10-26 (afternoon)
The greeting no longer says "This call may be recorded" (the user's choice); it is now "Thanks for calling Acme Plumbing and Heating, this is Sam. How can I help you today?". Legal review of the recording notice is still open (README "Going live"). Voice temperature raised 1.0 to 1.3 to sound less robotic; agent version 4 is live. Later the same day the voice changed to ElevenLabs Cimo (`11labs-Cimo`, agent version 5). Unverified by ear: ask the user how it sounds, and if it is unstable or odd, lower it toward 1.1.

### Repeated questions, 2026-10-26 (afternoon)
Cause (found in a live call where Sam asked "is it leaking?" three times): `live.py` lost context two ways. (1) After a turn was dropped because the caller kept talking, `heard_before` was still advanced, so the next request "took back" the previous reply the caller had already heard; the agent forgot its own question. (2) `new_caller_text` only kept caller lines after the last agent line, but Retell's transcript has fragments of interrupted replies between caller lines, so earlier lines were never given to the agent. Now the worker counts caller lines that kept turns have answered (`consumed`) and gives the agent every unanswered one; a dropped turn restores `heard_before` and `consumed`. Regression test: `test_a_dropped_turn_loses_neither...`. Unverified on a real call. The greeting is now just "Thanks for calling Acme Plumbing and Heating, this is Sam." (no question; the user's choice).

### Speed, intelligence, personality, 2026-10-26 (afternoon)
User: slow, talks slowly, dull. Changes: agent back to Sonnet 5.5 `between_tools`; `voice_speed` 1.15 (agent version 6); a personality bullet in `prompts.py` (warm, playful, matches the caller's energy, keeps the call moving). Unverified on a real call, and the prompt change is untested by simulation (budget). If it feels slow, first words are about 1.3-2.4 s; the next lever is a faster voice provider, not the model.

Personality updated again (afternoon): now "interesting, a little unpredictable, semi-sarcastic" (dry, aimed at situations not the caller; off for emergencies or upset callers; facts and safety unchanged). Unverified; if it gets in the way of accuracy, tone it down in prompts.py.

No sympathy phrases (afternoon): prompts.py bans "sorry to hear that", "oh no", "bummer" etc.; get straight to booking. "I'm sorry" only for our own mistakes.

Personality guard (afternoon): jokes must never imply the agent is human (found live: "just a Monday morning voice"). Latency, measured by Retell on 3 calls: end to end 2.1-3.3 s, model 1.4-2.0 s, voice 0.2 s; Haiku was no faster (2.5 s), so the model is not the bottleneck. The ngrok + home-internet path likely is; real hosting (Railway) is the next speed lever.

### Memory, hang-up, Render, 2026-10-26 (afternoon)
Found in a live call: the callback number was confirmed, then asked again after a failed transfer; the call never ended after the booking ("Okay, copy that"); a tip came before the booking; the transfer filler said "I'm sorry for the trouble". Fixed in `prompts.py` (settled facts are never asked again; after the confirmation, an acknowledgement means say goodbye and call end_call; tips only after the booking unless asked) and `tools.py` (filler, take_message description). Not yet verified on a call. Render: `render.yaml`, `.gitignore`, `RECEPTIONIST_DATA_DIR`, and `retell-tune --base-url` (repoints the Retell agent's websocket and webhook); steps in README "Hosting on Render". Not deployed yet (the user creates the Render account and secrets).

Render (2026-10-26): deployed at https://receptionist-575z.onrender.com (service from render.yaml; repo github.com/NoahBednarczyk/AI-Receptionist). Retell agent version 7 points its websocket and webhook there (retell-tune --base-url). The Mac server and ngrok are now a fallback only. To switch back: retell-tune --base-url https://spent-henna-rut.ngrok-free.dev. Next: verify on a real call; check latency on Retell vs the earlier 2.1-3.3 s.

Noise and side-talk (2026-10-26): Retell agent version 8 has interruption_sensitivity 0.6 and denoising_mode noise-and-background-speech-cancellation. Speech not meant for the agent (caller talking to a friend) makes the model answer "(listening)": agent.py drops it, nothing is spoken, and live.py takes the silent turn back on the next request so the muttering joins the next real words. Transfer failure wording is now blunt ("The office isn't picking up.") and "straightforward" is part of the personality. These prompt/code changes only reach live calls after a push to GitHub (Render redeploys); the Retell settings are already live. Unverified on a call.

### Med spa script (2026-10-26, evening)
The user asked to gear the script to a local med spa. New business `businesses/glow.json` (Lakeside Glow Med Spa, Sandpoint, demo facts: services, prices, hours Tue-Sat, FAQs, aftercare tips), playbook `receptionist/playbooks/med_spa.md` (consultation-first for first injectables/lasers, provider questions deferred, privacy, after-treatment problems), 18 scenarios in `scenarios/med_spa.json`, med spa emergency lines in `safety.py` (allergic reaction hangs up for 911; vision/blockage, heavy bleeding, urgent reaction go to the provider). Engine changes so one codebase serves both: `visit_type: "in_clinic"` (fixed appointment times spoken "at 2 PM", no street address or town, `booking.providers` capacity, `arrival_note`), transfer targets have `available` (open/closed/always; defaults keep Acme's office/on_call rules), prompts are vertical-aware (`TERMS`, `HOME_ADVICE`, `CLINIC_ADVICE` in `prompts.py`), `speech_keywords` for Retell. Live number +1 208-428-6760 moved from acme to glow in the JSON files; `default_business` is now glow (Acme's dashboard: `?business=acme`). Acme still has +13125550100 for tests.
A bug found and fixed on the way: a bad string splice put the literal text `+ repr(home_advice) +` into Acme's prompt where its tip rules belong; caught by a new test (`tests/test_med_spa.py`), never pushed.
NOT yet verified: no simulation of the med spa scenarios (the $1/day budget was spent; `simulate --business glow --quick` is about $0.30), no real call. Transfer numbers (front desk, provider) are 555-01xx placeholders, so every live transfer becomes "no answer" plus a message until the user gives real numbers. All med spa facts (prices, policies, aftercare tips, providers) are demo content a real client must approve; the medical-safety lines and the "no medical advice" rules need review by the client's medical director before real use.

Faster and interruptible (2026-10-26, night): interruption_sensitivity 0.9 (was 0.6; raised so the caller can cut in quickly, noise cancellation still on), voice_speed 1.25, reply_word_budget 28 and prompt target ~15 words with the answer first. If background noise starts cutting replies off again, lower interruption_sensitivity toward 0.75.
