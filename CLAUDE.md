# Receptionist: notes for Claude

An AI phone receptionist for small trades shops, sold as a monthly subscription. `README.md` has the commands, settings and
go-live checklist: read it first. `docs/HISTORY.md` is the dated changelog (unchanged from the old CLAUDE.md; look there
for why something is the way it is). Design doc: https://claude.ai/artifact/GtSqWJL89vevGzkp3oeMUN

## Rules the user set
- **Testing budget: $1 a day of API spend**, enforced by `receptionist/budget.py` (`python -m receptionist budget`). Estimate
  before any live run; prefer `simulate --for TOPIC` (the 1 to 4 scenarios that test that kind of change: `--for` alone
  lists topics and costs; defined per shop as `simulate_for`), `--quick` or named scenarios while iterating; the full suite only to confirm. Raise
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
- **Outbound sales caller: changes limited to three things (lifted 2026-11-12, spec
  `.internal/specs/2026-11-12-outbound-alignment-spec.md`):** (1) what it says matches the product as it is now (trade-aware
  pitch, demo line, verified capabilities), (2) its character (shared with the receptionist, plus the "guest in their day"
  mode; it never says "sales call", "cold call", "pitch" or "selling"), (3) Part E: code never ends a call because the owner
  said no; a refusal is classified, answered, and the ask moves down a ladder of smaller yeses. An explicit stop request
  (and refusing the last rung) still ends the call at once. **Still off limits, and not to be raised with the user:** outbound
  `mode`, `cold_enabled`, consent handling, `can_call` and its gates, call hours, attempt limits, the render cron, the block
  runner. The AI disclosure in the intro stays on and nothing untrue is ever said.

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

## Where things stand (2026-11-12, pushed to origin/main at 649f377 and deployed on Render)
- **Live line:** +1 208-428-6760 now runs the trades shop (`hvac.json`, "Peak Heating & Plumbing (demo)", agent Riley (was Sam);
  `live_businesses: ["hvac"]`). `lawn_care.json` keeps the placeholder +13125550200. Models: agent Claude Sonnet 5.5
  (`between_tools`), summary and grader Sonnet 5.5, caller sim Haiku 5.5.
- **Retell (verified against the real API 2026-11-12):** the number points at agent version 23 (22 added post-call analysis off; 23 the Riley keywords) and `post_call_analysis_model`
  is null on it. `retell-tune` was NOT run as written: the live agent's voice values (interruption 0.9, speed 1.22,
  temperature 1.1, dynamic speed off) differ from the `voice` block in the shop JSON files, and running it would have
  overwritten the user's dashboard tuning. Only `post_call_analysis_model` and `boosted_keywords` were changed. Do not run
  `retell-tune` until the JSON voice blocks are synced to the dashboard (or the user says to overwrite).
- **Email (verified on Render with real Gmail SMTP):** SMTP_* , ADMIN_EMAIL and DEMO_NOTIFY_EMAIL are set and working; a
  live-path call produced a per-call email and a missed-outcome email, both `sent` in `/admin/notifications`. Demo shops
  take their owner email from `DEMO_NOTIFY_EMAIL` (public repo, no address committed).
- **Live-path test call (verified):** a fake call over the Render websocket as Retell would send it (`to_number` the live
  number) got the trades greeting, answered a price question from the facts, ended cleanly, was saved (Claude cost $0.0376,
  shown in `/admin/costs`) and emailed. It was not a real phone call, so Retell audio and the phone path are unverified.
- **Greeting gap:** the hvac demo greeting has no recording notice and no AI disclosure (`demo: true` waives them). Before
  a real shop takes this number, use a non-demo shop file; `onboarding.check` refuses otherwise.
- **UptimeRobot** monitors `/health/deep` (user set it up 2026-11-12). The missed-outcome alarm also fires for a pure
  information call (no booking or message), which will be noisy once real callers ask price questions.
- **Character and no-heat fix (spec `.internal/specs/2026-11-12-character-and-no-heat-spec.md`, commits 62b2d0b,
  c1ff4fe, 78bc243, pushed 2026-11-12 at the user's word, NO scenario run on them: the user tests by ear):** `heating_season` (10-15 to 04-15) in `hvac.json` is read by
  `schedule.in_heating_season`; in season `call_state` says the weather is known and `safety.match(..., heating_season=True)`
  fires the no-heat line without a cold word. One shared character block with per-vertical example reactions
  (`TERMS[...]["reactions"]`); questions only when a booking needs them; dry humor in easy mode only; the call starts easy
  and code adds the problem line on a safety line or a service marked `problem`/`emergency`. Rising tone stays removed,
  no sarcasm. Hvac prompt 5,291 words (was 5,378). Five character lines were added to `STANDARD_RUBRIC` and scenarios
  `no_heat_no_weather`, `no_heat_no_weather_july`, `relaxed_tune_up`, `stressed_no_heat` exist. Still to do: the two
  no-heat scenarios twice, the full suite twice, then three test calls by the user (no heat, a price, "are you real").
  Weak point: the first reply on a broken-furnace call is still in easy mode. Retell agent is now version 23: only
  `boosted_keywords` changed (Riley instead of Sam), voice settings untouched; the number points at v23.
- **Haiku as grader, tested and NOT adopted (2026-11-12):** re-grading the 16 saved hvac transcripts with Haiku 5.5 cost
  $0.009 (Sonnet: $0.177) but agreed on only 57 of 63 criteria; 5 of the 6 differences were Haiku passing what Sonnet
  failed, including the invented recording opt-out (a gate failure). Summary on Haiku would save under a cent a call.
  Keep `models.grader` and `models.postcall` on Sonnet.
- **Outbound caller alignment (spec `.internal/specs/2026-11-12-outbound-alignment-spec.md`, commits after 220a6ae, PUSHED:
  origin/main = HEAD = 80ca1f5 on 2026-11-12 and Render redeployed it; NO simulated or live call run on them yet):** trade-aware pitch from the lead's `type`
  (`outbound.TRADES`), demo line described from the shop that holds the number, claims limited to `outbound.VERIFIED`
  (dashboard, per-call email) with the "not live" sentence read from the shop files; character shared with the receptionist
  (`prompts.CHARACTER_*`) plus a "guest in their day" mode; `sales`/`cold call`/`pitch`/`selling` banned and flagged; replies
  capped at 30 words through `Sentences`; stable prompt 2,481 to about 1,600 words. Part E replaced the two-no rule: code
  classifies each refusal (six kinds, `outbound_live.refusal_kind`) and moves the ask down a six-rung ladder
  (`outbound_live.RUNGS`); only a stop request or a no to the last rung ends a call. `python -m receptionist outbound-sim`
  runs 12 simulated owners (about $0.72, `--estimate` first); `outbound-agent --diff` shows the Retell outbound agent patch
  (keywords, post-call analysis model), `--apply` needs the user's yes. Still to do: the full 12-owner run, the same owners
  against the old code for call length, the live test call to the user's phone (about $0.45), leads for the trades (user's).
- **Outbound server state (read-only check 2026-11-12):** 113 leads (all landscaping, lawn or snow companies from local
  directories, so the pitch and the leads still mismatch the trades pilot), 7 real businesses dialed on Mon 11-09
  (08:11 to 12:10 PT), none since, plus two test calls to the user's phone. `outbound.mode` is `cold` with
  `cold_enabled: true`; the block runner is off. `render.yaml` defines the weekday 16:00 UTC cron
  `outbound-caller-trigger`, but the server shows NO dial on Tue 11-10, Wed 11-11 or Thu 11-12 although the gates would
  allow one (about 106 untouched leads, inside call hours), so the cron is either not deployed on Render, suspended, or
  failing (a `DASHBOARD_TOKEN` mismatch would give a 404). Render's dashboard (Cron Jobs, `outbound-caller-trigger`, Logs)
  is the only place to see which; the user decides. Never trigger `/admin/outbound/call-next` to test it: it dials.
- **Say channel is OFF** (`agent.speech_channel: false`). Full hvac run 2026-11-12: private notes up to 71 words, 77% of
  blocks tagged, so the stop rule (no private note over 12 words) failed; it passed only on lawn_care. The old blocklist
  is the active filter again. The flag is shared with the outbound caller's prompt builder, so the outbound prompt also
  went back to its pre-channel form (no outbound file was edited). Turn the channel on only after a full hvac run with no
  note over 12 words.
- **Tests:** 549 pass in about 13 s with no API key; CI in `.github/workflows/tests.yml` (never run on GitHub).
- **Simulation, hvac (the pilot), 2026-11-12:** first FULL run (16 scenarios): 10/16, gate (9/10 on two runs, zero
  failures on safety, invented prices or policies, AI disclosure) NOT met. Failures: a name/address question stacked on a
  time question (three scenarios), three windows offered, an invented recording opt-out, "Uh, getting that down"
  narrating a tool call, emergency windows offered before the town was checked, and a furnace-filter-tip grading that was
  grader noise (the tip is approved). Prompt rules were then edited (a time question ends the reply; exactly two times;
  no opt-outs or workarounds for stated policy; the confirmation number is no longer read out unless asked). Those edits
  are UNTESTED: the day's budget is spent ($1.81 of $2.00), and the raise to $4 for two full runs was blocked, so neither
  full run has happened. A full run costs about $0.95.

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
No real phone call has gone through the trades shop (only the websocket path); no bucket, Twilio or Google calendar run;
no real shop has used `new-client` end to end; the landing page and contact form have not been seen in a browser; the
weekly report only against fakes; GitHub CI never ran; the degraded mode only against fake clients; the prompt edits after
the 10/16 run; the new say-channel-off state against a full suite.

## Open items for the user (nothing below can be done without them)
1. **Budget:** raise today's limit (or wait for tomorrow's $1) so the full hvac suite can run, twice, for the W3 gate.
2. **Make one real phone call** to the live number and check the greeting, latency and hearing (an earlier test call was
   slow and hard to hear, probably during a redeploy; unconfirmed).
3. **Backup bucket** (B2/R2 free tier) and `BACKUP_S3_*` on Render.
4. **Retell voice settings:** decide whether the dashboard values or the JSON `voice` blocks are right, then sync the files.
5. **Google:** create the service account (free) and share one test calendar so `calendar-sync` can be proven.
6. **Decide:** a non-demo shop file with the recording notice and AI disclosure for the pilot; the voice swap
   (`docs/voice-comparison.md`, about 6 cents a minute); a lawyer for the pilot agreement; Stripe payment links (by hand);
   a domain (about $12 a year) for an HTTP email provider; the outbound caller's lawn-care pitch against the trades pilot.
