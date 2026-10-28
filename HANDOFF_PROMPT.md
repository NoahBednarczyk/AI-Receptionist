# Handoff prompt: AI phone receptionist (paste this into the new session)

You're taking over a project in `/Users/noahbednarczyk/Documents/receptionist`: an AI phone receptionist for small businesses, starting with home services. The demo business is "Acme Plumbing & Heating", set in Sandpoint, Idaho. **Read `CLAUDE.md` in that folder first**; it is the detailed handoff and is kept current. Then read `README.md` for the design, costs and go-live list. The design doc is a Claude Doc: https://claude.ai/artifact/GtSqWJL89vevGzkp3oeMUN

## State as of 2026-10-26
- **It is live.** Retell answers +1 208-428-6760 and talks to our server over a custom-LLM websocket. Server: `PUBLIC_BASE_URL=https://spent-henna-rut.ngrok-free.dev .venv/bin/python -m receptionist serve --port 8000` (ngrok must also be running; the free static domain is `spent-henna-rut.ngrok-free.dev`). It runs in Terminal-panel tab `c1`, ngrok in `c2`.
- **Agent:** Claude Haiku 5.5, thinking disabled, effort low (`config.json`). It was Sonnet 5.5 (`agent_thinking: between_tools`) until the very last request, which asked for more speed. Haiku has NOT been verified since the guards below were added. Earlier it passed only 3/10 hard scenarios (narrated its reasoning, stacked questions). Sonnet passed about 23/29 of the suite, plus all three `deep_*` calls. Check the first real Haiku call's transcript and the server log ("first words" lines) before deciding which to keep.
- **Voice:** ElevenLabs v4 Turbo, `11labs-Kate`, agent version 3 (set in `businesses/acme.json` under `voice`; apply with `python -m receptionist retell-tune`, which makes a new Retell version, publishes it and repoints the number). **Backchannel and call-center ambience must stay OFF**: they made Retell treat noise as caller speech, so every reply was superseded and the agent said "mm-hmm".
- **Texting is pinned** (`texting_enabled: false`). Nothing is queued while it's off. Twilio sender + A2P 10DLC come later.
- **Tests:** `.venv/bin/python -m unittest discover tests` (80 pass, free). Simulations: `python -m receptionist simulate [ids] [-v] [--jobs N]`; 32 scenarios; 3 are the hard `deep_*` calls.

## Rules from the user (do not break)
1. **Testing budget is $1/day of API spend** (`data/spend.json`, `python -m receptionist budget`). Raise it only when the user says so in chat, via `budget --raise-today N` (one day only). Ad-hoc API scripts count. Today's was raised to $3.40 and $3.29 is spent; it resets to $1 tomorrow.
2. API keys live in `~/.zshrc`, never in project files. Never print secret values. Note: the Retell websocket URL contains `RETELL_WS_SECRET`; don't echo it.
3. The agent must say plainly it's the business's virtual assistant when sincerely asked, and never claim to be human (the greeting no longer announces it; that was the user's choice).
4. Advice limits: approved tips plus simple, safe steps only. Never gas, electrical beyond the approved tips, opening appliances, tools or chemicals. No diagnosis. Emergencies stay on the safety lines (spoken by code in `safety.py`).
5. You can't create accounts or enter payment details; the user does. Confirm before actions that change their Retell account (retell-tune is fine when asked).
6. Auto-mode may block guarded actions (like ledger resets) until the user approves. Don't work around guards.

## How it works (so you can navigate)
- `agent.py`: `CallSession.respond()` is the turn loop. `Sentences` filters speech (drops thinking-out-loud and internals via `speech.unspeakable`, caps replies at 40 words, says "anything else" once). `TurnControl`/`revert_last` let live calls drop or take back replies the caller never heard.
- `live.py`: Retell websocket handler (drops stale turns, hangs up after goodbye, tells the agent what an interrupted caller heard, reads confirmation numbers digit by digit).
- `safety.py`: emergency detection (English + Spanish), clause-aware negation; gas/CO with the caller inside is a fixed turn that hangs up.
- `tools.py`: tools; a live call never dials 555-01xx placeholder numbers (acts like no answer); `lookup_customer` hides records from other numbers until the name matches; card numbers are redacted in `speech.redact_card`.
- `prompts.py`, `playbooks/home_services.md`, `businesses/acme.json`: all rules and facts the model sees.

## Open items, in rough priority
1. **Verify Haiku on a real call** (ask the user how it went; read the call from `data/receptionist.sqlite` table `calls`, and Retell `GET /v2/get-call/{id}` for latency). If it narrates, stacks questions or invents facts, switch back to Sonnet (two lines in `config.json`) or add guards. The user wants it "lightning fast and accurate".
2. Transfers use fake 555 numbers, so every live transfer currently becomes "no answer" plus an urgent message. Ask the user for a real cell number for `transfer_targets` in `acme.json`.
3. Untested traps: late-night price question; the "do I need to be home" FAQ is a demo policy each real client must set.
4. Go-live list (README "Going live"): always-on hosting (Railway; ngrok is for tonight only), calendar sync, a real client's config, legal review (call recording disclosure, AI disclosure laws), monitoring and backups, billing, texting.
5. Margins: ElevenLabs voice is $0.08/min, which drops plan margins to 31-61% (README cost table). Cheaper voices ($0.015) give 57-74%. Raise this with the user when pricing comes up.
6. Keep `CLAUDE.md`, `README.md` and the memory folder (`/Users/noahbednarczyk/.claude/projects/-Users-noahbednarczyk-Movies-claude-videos-clipengine/memory/`) current.

## Working style the user likes
Short, plain answers; say what changed and what is still unverified; run tests after changes; restart the server (tab `c1`) after code changes so the live line gets them; make fixes at the root cause, not by adding phrase filters.
