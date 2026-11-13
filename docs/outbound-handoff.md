# Handoff to the Manager: what changed, and the new cold caller

> **Status as of 2026-11-13.** This is the 2026-11-01 handoff, kept as written. Since then: the trades shop (`hvac.json`,
> "Peak Heating & Plumbing (demo)", agent Riley) holds the live number and `live_businesses` is `["hvac"]`; everything
> below was pushed and is deployed; the caller's pitch follows the lead's trade (heating, cooling and plumbing first, lawn
> and snow second) and describes the demo line from the shop that holds the number; refusals move down a ladder of
> smaller asks instead of ending the call (only a stop request, or a no to the last rung, ends it); it never says "sales
> call". The 113 leads on the server are still lawn and snow companies. See `CLAUDE.md` for the current state and
> `docs/HISTORY.md` for the dated changes. Where this file says "lawn_care" is live, or "sales call", read it as history.
Written 2026-11-01 by the Claude Code session. Repo: `/Users/noahbednarczyk/Documents/receptionist` (GitHub NoahBednarczyk/AI-Receptionist, deployed on Render). 269 tests pass. **Everything below is committed locally; nothing from this stretch is pushed, and the cold caller has never placed a call or been run against the model.**

## 1. Other changes this session (not the cold caller)

**Cost work**
- `spend-report` now reads Anthropic's Admin API: finished days from the cost report, today priced from token counts (`prices_per_mtok`; cache reads 0.1x, 5-minute writes 1.25x, 1-hour writes 2x), split by model, plus CSV export.
- Found two self-inflicted cost drivers and fixed them locally: test/simulate runs now use the 5-minute cache (the live server keeps 1 hour), and the server only pre-warms shops in `live_businesses` (then just `lawn_care`; now `hvac`).
- Retell's own post-call summary (about 2 cents a call, duplicated by ours) is switched off in code (`post_call_analysis_model: None`) but **not applied to the live agent** until `retell-tune` is run with Noah's yes.
- Retell costs about 16.3 cents a minute (voice 44%, engine 34%, phone 9%).

**Finances page (live)**
- New tab on the "Claude's Business" dashboard (link ends `#finance`): this month by service, spend since Oct 12 against the $100 released and $300 cap, cost per call and per minute, 30-day chart, Retell cost breakdown, Claude by model.
- Fed by `python -m receptionist finance-sync` (Retell per-call costs; Anthropic usage by API key and model; the project's test ledger; Render/phone number as labelled estimates). Writes `data/reports/finance-*.json`, copied into dashboard docs `finance/current` and `finance/daily`.
- Counts only the "AI Receptionist" API key as the live line. The dev key on this Mac is shared with the clip project, so it's shown as "not counted".
- A scheduled task, "Daily receptionist finance sync", runs about 7:25 AM local while the Claude app is open and refreshes both docs. On failure it posts a message to Noah and leaves old data alone.
- Reported to the dashboard: `sessions/code`, a message to Noah, and knowledge entry `code-finance`.
- Current figures (Nov 1): this month $11.33 (measured $2.08 + estimates $9.25); to date $19.31; $0.38 per call over only 3 calls.

**Open warning:** the Anthropic API returned "credit balance is too low" during a test run. Credits are account-wide, so the live receptionist may also be unable to answer until credits are added. Not confirmed. Noah says he can't add credits for a while.

## 2. The cold caller ("Ringback discovery caller")

**Purpose.** An outbound sales/discovery caller on the same engine as the receptionist. Goal order: (1) a 15-minute meeting with Noah, (2) get the ten Discovery questions answered in the owner's words, (3) leave the door open with a follow-up date. It also points curious owners to the demo line (208-428-6760) as proof.

**Persona (Noah's brief).** Cheerfully naive, fast, deadpan; short quips when the owner gives an opening, serious and crisp when they are serious; stubborn about getting answers and the meeting; handles angry or rude owners by agreeing with the fair part, answering honestly, giving a reason to stay 30 seconds, and making one small ask.

**Call structure** (`receptionist/playbooks/sales.md`):
1. Opener, said by code the moment the person speaks first (the agent's first message is empty, so Retell waits for "hello", then the opener plays with no model call). A hook about the shop, then who is calling (Riley, for Noah at Ringback), that it is a short sales call, a small ask. A lead's own `hook` field wins; otherwise a plumbing, HVAC or generic hook pool is used.
2. One-sentence reason for calling.
3. Discover (core): Noah's ten questions, priority Q1, Q4, Q2, Q6; SPIN questions, mirroring, labeling; get a dollar figure in the owner's words.
4. Bridge to the meeting with two concrete times (alternative close).
5. Book (`check_meeting_slots` then `book_meeting`), read back day, time and callback number.
6. Close.

**Pushing back.** Up to three asks per call, each from a different angle: plain ask, shrink it to 10 minutes, then "what's really in the way?". A dodged question is re-asked once, then logged unanswered. Scripted answers for: not interested, send info, already have someone, how much, are you a robot, don't trust AI, too busy, how'd you get my number. Persuasion tools: curiosity, small asks, reciprocity (free pilot), loss framing with the owner's own numbers, labeling, calibrated questions.

**Honesty rules in the prompt.** It does not volunteer that it is an AI. If sincerely asked it makes a quick joke and says yes plainly in the same breath; it never claims to be human or denies being an AI. It persuades but states nothing untrue and implies nothing untrue: no invented customers, results, prices, deadlines or scarcity. Only the facts given (14-day free pilot on missed calls, demo line, 15-minute call with Noah; texting and calendar sync are not live).

**Hard stop (in code, no model).** A plain request such as "stop calling", "take me off your list", "remove my number", "put us on the do-not-call list" is detected by regex. The call says a short goodbye, adds the number to the do-not-call list, logs "Not interested", and hangs up. "Don't call me today, try Thursday" does not trigger it. Anything softer (angry, "not interested") goes to the model to work.

**The brain.** Sonnet 5.5, effort medium, in the receptionist's cached-prompt path (`models.outbound`, `models.outbound_effort` in config.json). Chosen for phone speed and because it is already priced in the cost report. A stronger model is a config change, but its price must be added to `prices_per_mtok` first or the cost report cannot price it.

**Eligibility gate** (`outbound.can_call`): usable phone number; not on the do-not-call list; mode check; weekdays only; shop-local hours 8am-6pm; at most 2 attempts per number per 7 days; respects a `followDate` in the future. Modes (`outbound.mode` in config.json): `dry-run` (default; reports only), `warm-only` (needs `consent: yes` on the lead), `cold` (needs `outbound.cold_enabled` true and, unless consented, a landline flag).

**Files**
- `receptionist/outbound.py`: settings, gate, queue, meeting slots (Noah is free any time: default windows are 8am-8pm every day, offers spread across morning/afternoon/evening, never sooner than 2 hours out), prompt builder, tool schemas, CRM row and lead-update builders.
- `receptionist/outbound_live.py`: `OutboundSession` (a CallSession subclass), the four outbound tools, `State` (data/outbound.json: do-not-call list, attempts, bookings, results), opener/hook generator, stop and AI-question detection, Retell agent payload, guarded `place_call`, dashboard export.
- `receptionist/playbooks/sales.md`: the playbook.
- `receptionist/server.py`: websocket `/llm-websocket-out/<secret>/<call>` (refuses while mode is dry-run) and admin `GET /admin/outbound`.
- `receptionist/tools.py`: `ToolContext.extra` field added.
- `receptionist/__main__.py`: commands `outbound-plan`, `outbound-agent`, `outbound-number`, `outbound-sync`, `finance-sync`.
- Tests: `tests/test_outbound.py`, `tests/test_outbound_live.py` (including a full fake-socket run of a stop request), `tests/test_finance.py`.
- Docs: `docs/outbound-plan.md`, `docs/outbound-legal-notes.md`, CLAUDE.md sections.

**Retell.** Agent "Ringback discovery caller" (id agent_14910efc794dc441d72393f4c6) is created and published, using the live receptionist's exact `voice` block (custom voice, model eleven_v4_turbo, speed 1.15). The voicemail message is saved (names Riley, Noah, Ringback, gives the demo line). **Number is PENDING.** When Noah gets one: `python -m receptionist outbound-number --number +1...`. `enable_voicemail_detection` read back empty, so confirm voicemail detection in the Retell dashboard.

**Dashboard hand-off.** `outbound-sync` pulls results from the server into `data/reports/outbound-calls.json` (docs `calls/ai<n>`), `outbound-leads.json` (stage moves and notes for `leads/<id>`), `outbound-tasks.json` (follow-up tasks for booked meetings). A Claude session copies those into the dashboard database (neither Render nor the Mac can write it). Stage mapping: Demo booked to "Pain confirmed", Pilot signed to "Pilot agreed", Callback requested to "Owner reached", other outcomes to "Called".

**Live placement guard.** `place_call` refuses unless: mode is not dry-run, `OUTBOUND_LIVE=1`, the lead passes the gate, and an outbound number is set.

## 3. Status and what is blocked

Not done: any live or simulated call (no API spend allowed); the outbound panel on the dashboard (a newer dashboard version was published by someone else, so it needs a re-read before editing); the self-tester that would call the receptionist's own line; hooks written per lead (Claude can write them for free in a session).

Blocked on Noah:
1. Anthropic credits (nothing can call the model until then).
2. A Retell outbound number, then `outbound-number`.
3. A push to GitHub so Render has the new routes (not requested yet).
4. Manager agent hookup (meeting times): `PUT /admin/outbound/slots` with the admin key and `{"slots": ["2026-11-03T16:30:00-08:00", ...]}` replaces the default windows for 36 hours; the caller then offers only those real open times (minus booked ones and anything under 2 hours away). Results come back through `GET /admin/outbound` and `outbound-sync`.
5. Legal: research was done (docs/outbound-legal-notes.md: AI voice counts as an artificial voice under the TCPA; FTC rule against misleading statements now covers business-to-business calls; Idaho solicitor registration unverified). Noah says it is inaccurate and to disregard it. It is left in the repo as unverified research and is not acted on beyond honoring stop requests.
6. Retell details checked against Retell's docs (2026-11-01): `POST /v2/create-phone-call` (from_number, to_number required; override_agent_id, metadata, idempotency_key, honor_internal_dnc optional) is correct, and `call_details` includes `metadata`. Voicemail is controlled only by `voicemail_option` (the `enable_voicemail_detection` field I sent does not exist and was ignored; removed from the code). Still unverified until a real call: what Retell does with an empty first message on outbound calls (docs say it waits for the person to speak) and how voicemail detection and our greeting interact.

Other open items from earlier: apply `retell-tune` to switch off Retell's duplicate summary; voice cost options; run `simulate --business lawn_care --quick` after credits return (the human-feel prompt changes have never been simulated); real owner cell numbers for transfers are still placeholders; recording-notice items stay open.

## 4. Questions for the Manager and Noah

- Which shops go on the first warm list (who has said they want to hear from us)?
- Is Sonnet 5.5 at medium effort the right brain, or is a stronger model worth the price per call?
- Which scheduling signals should the manager agent use to produce open slots, and how often should it refresh them (36 hours is the staleness limit)?
- Do hooks get written per lead before the first call?
- Budget for the first live batch (suggest 10 calls, about $5), and for the self-tester.
