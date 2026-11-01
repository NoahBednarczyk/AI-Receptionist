# Outbound bot plan: discovery caller + self-tester

Status: PLAN ONLY. Nothing here is built, and nothing calls anyone yet.

## Two bots, one engine

Both reuse what the receptionist already has: `agent.py` (CallSession), `prompts.py` (character kit, human-in-small-ways, respond-don't-interview), `live.py` (Retell websocket), `safety.py`, `budget.py`.

### A. Self-tester (build first, no legal exposure)
Calls only our own Retell number (+1 208-428-6760) as a fake customer: "my sink is leaking", "price for a furnace tune-up", a rambler, an interrupter, a hostile caller. Replaces Noah's manual test calls.
- Scenarios come from `scenarios/*.json` (already exist) turned into caller personas.
- After each call: pull transcript + Retell cost, score it (asked for the number twice? repeated itself? booked correctly? ended the call?), write a one-line result to the dashboard.
- Cost: ~16 cents/min x 2 legs. Counts against the $1/day budget (`budget.record`); runs stop at the limit.

### B. Discovery caller (the cold-call bot)
Calls shops from the dashboard's pipeline ("On the list" stage), runs the Discovery Call Script (ten questions), offers the free 14-day pilot, books a meeting or logs the objection.
- Reads leads from the dashboard db (`leads`), writes back: call log (`calls`), stage move (`leads`/pipeline), follow-up date, notes, objection. Same fields the Cold Call Desk already uses.
- Runs on a Retell outbound agent with its own prompt built from the receptionist engine.

## What I will not build, and why

1. **No fake identity.** The idea of opening with "I'm whoever from whatever business" is a pretext. The bot says: "Hi, this is Riley, an AI assistant calling for Noah at Ringback, here in Sandpoint." If asked, it says it is an AI (existing hard rule). It still can ask the research questions and offer the free service; honesty does not hurt that pitch, and a "wait, was that a robot?" mid-call hurts it badly.
2. **No calls to numbers we have no basis to call.** In the US, an AI voice counts as an "artificial voice" under the TCPA (FCC ruling, Feb 2024). Calls to **cell phones** with an artificial voice need prior express consent, and many small-shop numbers are cell numbers. Business landlines are far less restricted; the National Do-Not-Call list and state rules still apply, and Idaho has its own telemarketing rules. I am not a lawyer. Before any live outbound call, someone needs to confirm this (a one-hour attorney check is the cheap route). Until then the bot runs in **dry-run** and **warm-only** modes (below).
3. **No calling outside 8am-6pm shop-local time, no repeat calls after "no" or "stop calling", no more than 2 attempts per number per week.** Enforced in code, not in the prompt.

## Safe modes

- **dry-run (default):** builds the call list, shows the script and who would be called, places nothing.
- **warm-only:** only calls numbers flagged `consent: yes` in the dashboard (the shop asked us to call, replied to a text, or Noah spoke with them first). This is the legally safest live mode and also the highest-converting.
- **cold:** off until the legal check is done. A config flag `outbound.cold_enabled`, false by default, plus a landline-only check (Retell/Twilio lookup) and a do-not-call list kept in `data/dnc.json`.

## Build steps

1. `outbound.py`: call list from dashboard export, eligibility gate (consent, hours, DNC, attempt cap, landline check), dry-run output. Tests.
2. `tester.py`: self-tester personas -> calls our own number -> scorecard. Tests with a fake Retell. First live run: 3 calls max, within the $1/day budget.
3. Outbound prompt + `retell-outbound-tune` command (creates/updates a second Retell agent). **Needs Noah's yes before any Retell account change.** Needs an outbound-capable number (the current one is the receptionist line; a second number is a Retell purchase Noah makes).
4. CRM write-back: after each call, a summary JSON (call log row, lead stage, follow-up, objection) that the scheduled Claude session writes into the dashboard db, same pattern as the finance sync (the server cannot write the artifact db itself). Calls to the dashboard appear within a day, or immediately if a Claude session runs the write.
5. Dashboard: "Outbound" panel on the Cold Call Desk: queue, results, cost per booked meeting (ties into the Finances page).

## Costs (rough)

Per outbound call ~1-3 min x ~16c = 16-50c. 25 calls/day = $4-12/day, far over the $1/day test budget, so live outbound needs a new, explicit budget from Noah. Self-tester: ~$0.30-0.60 per 1-2 minute call pair.

## Decisions needed from Noah

1. OK to disclose "AI assistant calling for Noah at Ringback" (recommended) instead of a made-up persona?
2. Start with the self-tester (A) plus outbound in dry-run/warm-only, and hold cold calling until the legal check?
3. Budget for the first live outbound batch (suggest 10 calls, ~$5).
