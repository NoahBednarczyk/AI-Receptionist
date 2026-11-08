# Handoff prompt: build the lead builder (paste this into the new session)

You're taking over the AI-Receptionist repo (GitHub `NoahBednarczyk/AI-Receptionist`). **Read `CLAUDE.md` first** (the running log; the last entries, dated 2026-11-08, matter most), then this file. Your job: **build the lead builder** for the outbound discovery caller.

## The product, so you aim at the right target
Noah sells an AI phone receptionist to **lawn care, landscaping and snow removal businesses in North Idaho** (Sandpoint and the towns around it). Nothing else: no heating, plumbing or HVAC (all of that was removed on 2026-11-08). The outbound "discovery caller" (agent Riley, company Ringback, founder Noah) cold-calls those businesses to book a 15-minute meeting with Noah.

## What exists and works (all on `main`, commit `1c23eef`, deployed from `main` on Render)
- `receptionist/outbound.py`: the eligibility gate `can_call()` (hours 08:00-18:00 shop-local, weekdays, 2 attempts a week, do-not-call, **in cold mode a number must have `line` = `landline` or `voip`, or `consent: "yes"`**), `plan_queue()` ordering, the sales prompt. `playbooks/sales.md` is the pitch.
- `receptionist/outbound_live.py`: `place_call`, `call_next` (picks the first eligible lead and dials it), `load_leads`/`save_leads` (file `outbound_leads.json` on the server disk `/var/data`), the agent record, `OutboundSession`.
- Server admin endpoints (all need `Authorization: Bearer <DASHBOARD_TOKEN>`, and answer 404 otherwise): `PUT/GET /admin/outbound/leads` (replace/read the whole list), `PUT/GET /admin/outbound/agent`, `POST /admin/outbound/call-next`, `GET /admin/outbound`.
- Render: web service `receptionist` (https://receptionist-575z.onrender.com) plus a cron service `outbound-caller-trigger` (`0 16 * * 1-5` UTC, curls `call-next`). Render deploys from **`main`**, not from feature branches.
- Retell: agent `agent_14910efc794dc441d72393f4c6` ("Ringback discovery caller"), from-number **+12082152934** (registered on the server via `PUT /admin/outbound/agent`). Retell has an **unpublished draft (v2)** with the lawn care voicemail and keywords; Noah has to publish it himself because it also contains his other dashboard edits.
- 287 tests pass except one old unrelated failure (`test_privacy.Dashboard.test_the_owner_can_block_and_remove_time`, a dashboard HTML-escaping assertion; it failed before this work).

## What to build
A lead builder that finds lawn care, landscaping and snow removal businesses in the service area and gets them into the lead list safely.

**Already started, uncommitted, untested: `receptionist/leads.py`** (a draft; read it, fix it, test it). Its design:
1. `normalize(candidate, towns)`: a candidate needs `name`, a real 10-digit `phone` (not 555-01xx), a `town` in the service area, a `source` URL (the listing page it came from) and a `type` that maps to lawn care / landscaping / snow removal; anything else is skipped with a reason. `line` (`landline`/`voip`/`mobile`) is only set when someone verified it.
2. `merge(existing, candidates, towns, dnc)`: appends new leads, never edits or reorders existing ones (their stage/notes/follow-up are the record of past calls), skips numbers already listed or on the do-not-call list, returns a report (added, skipped with reasons, total).
3. Still to do: remove `"tree"` from the Landscaping keywords (tree-only services are out of niche); add the endpoint `POST /admin/outbound/leads/merge` in `server.py` (body `{"candidates":[...]}`; use `_admin_only`, `leads.service_towns(cfg)`, the outbound `State().dnc()`; save only if something was added; return the report); write tests (a `tests/test_leads.py` plus endpoint tests next to `OutboundAdminTests` in `tests/test_logic.py`); create the skill `.claude/skills/build-leads/SKILL.md`; add a CLAUDE.md entry.
4. **The skill** tells a Claude session how to search: for each town in `businesses/lawn_care.json` `service_area.towns` (24 towns: Sandpoint, Ponderay, Kootenai, Dover, Sagle, Hope, East Hope, Clark Fork, Priest River, Oldtown, Laclede, Cocolalla, Careywood, Colburn, Samuels, Naples, Bonners Ferry, Priest Lake, Coolin, Nordman, Blanchard, Spirit Lake, Athol, Bayview), search for the three trades, read the business's own listing page, and post candidates to the merge endpoint. It must never invent or guess a phone number or a category, must record the source URL, and must never put anything on the list directly: only through the merge endpoint, so the server's checks apply. It should report counts (found, added, skipped and why) and show Noah a sample.

## The open problem you must raise with Noah, not paper over
In cold mode `can_call` refuses any lead without a confirmed `landline`/`voip` line, so **leads without a line type will never be dialed**. Nothing in this repo can check line type yet (it needs a carrier lookup such as Twilio Lookup, which has no credentials here). Options: (a) add a lookup step Noah funds, (b) Noah verifies numbers by hand, (c) he decides to relax the gate. That is Noah's call, and the legal question around AI cold calls is his to settle (he told us to disregard `docs/outbound-legal-notes.md`; the stop-request rule stays: a clear "stop" ends the call and marks do-not-call). Do not change the gate yourself.

## Things that can bite you
- **The cron is live.** Weekdays at 16:00 UTC (8 AM PST / 9 AM PDT) it calls `call-next`. The lead list currently holds **one test lead: Noah's own phone, 208-610-5821, `test: true`** (that flag bypasses the hours and weekday gate), so the cron will ring his phone every weekday morning until that lead is replaced or removed. Tell him, and ask before changing it. A call costs about $0.40.
- **Do not add real businesses to the live list without Noah seeing a sample first.** Build and test with candidate files locally; post to the live endpoint only after he says yes.
- **Branch rule:** you develop on the branch the session names. Render deploys `main`; pushing to `main` needs Noah's explicit permission each time.
- **Network:** a cloud session blocks unlisted hosts. To reach Retell or the Render server, use `request_credential` for the host (Noah types the key into the form); never put keys in the repo, in `CLAUDE.md` or in chat. Noah pasted his Retell API key and `DASHBOARD_TOKEN` into chat earlier in the previous session, so suggest he rotates both when convenient.
- **Budget:** $1 a day of API spend (`python -m receptionist budget`). Web searching for leads does not use the Anthropic API budget, but simulated calls do.
- Python: `.venv/bin/python -m unittest discover tests` (no pytest). `pip install -r requirements.txt` into `.venv` first if it is missing.
- Reply style for Noah: short, plain, say what you did and what you did not verify.
