# Receptionist: notes for Claude

An AI phone receptionist for small trades shops, sold as a monthly subscription. `README.md` has the commands, settings and
go-live checklist: read it first. `docs/HISTORY.md` is the dated changelog (unchanged from the old CLAUDE.md; look there
for why something is the way it is). Design doc: https://claude.ai/artifact/GtSqWJL89vevGzkp3oeMUN

## Rules the user set
- **Testing budget: $1 a day of API spend**, enforced by `receptionist/budget.py` (`python -m receptionist budget`). Estimate
  before any live run; prefer `simulate --quick` or named scenarios while iterating; the full suite only to confirm. Raise
  the limit only when the user says so in chat (`budget --raise-today N`). Ad-hoc scripts that call the API count too.
- **No push without the user's word.** A push deploys to the live phone line. Do not change Retell, Render, Twilio, Google or
  email accounts without a yes. Real keys never go in the repo, chat or memory.
- **AI disclosure:** the agent says plainly that it is the shop's virtual assistant when asked and never claims to be human.
  Real clients' greetings always carry the recording notice and the AI disclosure (`onboarding.check` refuses otherwise;
  only `demo: true` shops may waive them).
- **Advice:** approved tips plus simple, safe steps; never gas, electrical beyond tips, opening appliances, tools or
  chemicals; no diagnosis; emergencies stay on the safety lines in `safety.py` (spoken from code).
- **Money:** the user has close to none. Every item needs a zero-cost path; anything that costs is listed with its price.
- **Fix causes, not symptoms:** no new phrase filters; change the rule or the data that makes the model do it.
- **Frozen until three shops are live:** quirks, asides, greeting pools, stall lines, voice settings, the outbound block
  runner. Real-caller transcripts decide what gets tuned next.
- **Outbound sales caller: no change.** Do not edit `outbound.py`, `outbound_live.py`, `playbooks/sales.md`, the `outbound`
  config block or the render cron. Its pitch is still lawn care and snow while the pilot vertical is now emergency trades
  (a known mismatch, left as is).

## Architecture map
`live.py` Retell websocket per call and turn handling; `agent.py` call loop, `Sentences` (speech release), `SayChannel`
(`<say>` tags), `make_client` timeouts; `degraded.py` code-driven message taker when the model fails; `safety.py` emergency
lines by vertical; `tools.py` the tools; `prompts.py` rule layers (`PLATFORM_RULES`, `TERMS`, `call_state`);
`playbooks/{hvac,lawn_care,home_services,sales}.md`; `schedule.py` windows and capacity; `store.py` SQLite;
`postcall.py` summary; `notify.py` email/SMS/digest/missed-outcome alarm; `weekly.py` weekly owner report;
`server.py` all routes (`/health/deep`, `/admin/costs`, `/admin/notifications`, `/`, `/contact`); `backup.py`;
`keepwarm.py` cache refresher; `onboarding.py` `new-client`; `finance.py` costs; `retell_setup.py`/`retell_hooks.py`;
`simulate.py` test calls; `voicetest.py`. Shops: `businesses/{hvac,lawn_care,acme}.json` (demos, in git);
real clients in `data/clients/` (server disk, never in git). Scenarios in `scenarios/`, run reports in `data/runs/`.

## Where things stand (2026-11-12, local commits, NOT pushed)
- **Live line:** +1 208-428-6760 runs the lawn care demo (`lawn_care.json`, `live_businesses: ["lawn_care"]`). Models: agent
  Claude Sonnet 5.5 (`between_tools`), summary and grader Sonnet 5.5, caller sim Haiku 5.5. Voice and Retell settings are
  whatever the user set in the Retell dashboard; `lawn_care.json` mirrors them, except that `post_call_analysis_model` is
  now `null` (see open items). `default_business` is `hvac` (the pilot vertical); moving the live number to it needs the user's word.
- **Say channel is ON** (`agent.speech_channel: true`), decided on 2026-11-12 from the lawn quick run: 0 untagged blocks per
  call, longest private note 12 words, 48/56 text blocks tagged. On the hvac quick run the same day the longest private
  note was 35 words and 68 to 87% of blocks were tagged; the stop rule (no private note over 12 words) was NOT met there.
  Re-measure on the next full run; if it keeps failing, delete the channel (`speech_channel: false` is the rollback). The
  old blocklist still runs in shadow; delete it after a clean week of real calls.
- **Tests:** 485 pass in about 13 s with no API key; CI in `.github/workflows/tests.yml` (never run on GitHub).
- **Simulation, hvac (the pilot), 2026-11-12:** quick 6: 3/6; after root-cause prompt fixes the 3 failures passed on
  one rerun (3/3). The full 16-scenario suite has NOT been run twice; the gate (9/10 on two runs, zero failures on safety,
  invented prices, AI disclosure) is therefore unproven. The day's $1 is spent; a full run costs about $1.05.

## Built under the 2026-11-11 market-readiness spec (`.internal/specs/2026-11-11-market-readiness-spec.md`)
- **W1:** degraded mode, Anthropic client timeouts, `/health/deep`, `backup.py` + `restore --dry-run` (S3 upload tested
  against a fake bucket only), tests use temp dirs, stale privacy test fixed (no XSS), racy tests made deterministic, CI file.
- **W2:** email (SMTP now, HTTP provider stub) after every call, URGENT subjects, rate limits, daily digest, missed-outcome
  alarm to `ADMIN_EMAIL`, Twilio SMS code behind `texting_enabled` with STOP handling (fakes only), `docs/10dlc-registration.md`.
- **W3:** say channel retest, two prompt root-cause fixes from the suites, rubric/playbook mismatch fixed.
- **W4:** `/admin/costs` (cost per minute per shop, from a new per-call Claude cost column and Retell billing), cache
  refresh verified to read (see README), `post_call_analysis_model` null in `lawn_care.json`, prompt cut from 5,976 to
  5,334 words for hvac (target was 4,000; going lower means touching safety, honesty or tool rules or scenario-pinned
  behaviour), blind voice comparison kit (`docs/voice-comparison.md`).
- **W5:** trades shop restored on the current engine (`hvac.json`, playbook, 16 scenarios incl. owner-cell no answer,
  Washington caller, 7-digit number twice, rambler), `new-client --template hvac|lawn_care`.
- **W6:** `new-client` dry run fixed (owner email, printable checklist, calendar section), real shops cannot waive the
  notice or disclosure, transfers stay message-first (Retell's custom-LLM socket has `transfer_number` only and no outcome
  event, checked 2026-11-12), `docs/pilot-agreement-template.md` (lawyer draft), weekly report (`weekly.py`), landing
  page at `/` with a contact form to `ADMIN_EMAIL`.
- **W7:** platform-hosted paths, `HANDOFF_PROMPT.md` and the med spa removed (tag `med-spa-archive`), README rewritten.

## Not verified
Nothing here has run against a real SMTP server, bucket, Twilio, Retell number change or real Google calendar; no real
shop has used `new-client` end to end; the landing page and contact form have not been seen in a browser; the weekly
report and digest only against fakes; GitHub CI never ran; the degraded mode only against fake clients.

## Open items for the user (nothing below can be done without them)
1. **Budget:** ask to raise it by about $2 so the full hvac suite can run twice (W3 gate).
2. **Gmail app password** for SMTP; set `SMTP_HOST/USER/PASSWORD` and `ADMIN_EMAIL` on Render; add `notify.email` per shop.
3. **UptimeRobot** (free) on `/health/deep`; **backup bucket** (B2/R2 free tier) and `BACKUP_S3_*`.
4. **Retell:** apply `retell-tune` to set `post_call_analysis_model` null (Retell's `gpt_5_6_terra_text_testing` line was
   $1.25 of $5.06 in November; it is almost certainly that setting, unconfirmed without the key). Optional `inbound_fallback_number`.
5. **Google:** create the service account (free) and share one test calendar so `calendar-sync` can be proven.
6. **Decide:** move the live number to the trades shop; the voice swap (`docs/voice-comparison.md`, worth about 6 cents a
   minute); a lawyer for the pilot agreement; Stripe payment links (by hand); a domain (about $12 a year) for an HTTP email provider.
7. **Push** when ready: it deploys everything above to the live line.
