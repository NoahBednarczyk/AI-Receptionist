# Receptionist: Phase 0

An AI phone receptionist for one demo home-services business, Acme Plumbing & Heating. It answers, books real arrival windows, reschedules and cancels existing ones, handles emergencies, transfers, takes messages, shares approved tips, speaks Spanish and texts confirmations. Phase 0 proves the agent on scripted test calls before any customer hears it.

```
caller -> voice platform (Retell or Vapi) -> Claude Sonnet 5.5 agent -> our tools (schedule, bookings, leads, messages)
                                                        \-> after hang-up: Claude Sonnet 5.5 summary and quality flags
```

The prompt, tools and business data live here; the voice platform only carries the audio. The same `CallSession` runs text chats and simulated calls, so a passing scenario tests the agent the phone line uses.

## Setup (once)

1. `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
2. `export ANTHROPIC_API_KEY=...` with credit on the account (Plans & Billing in the Claude Console).
3. `python -m unittest discover tests` should pass without a key.

## Try it

```bash
python -m receptionist chat                                    # a call right now, as a new caller
python -m receptionist chat --caller +13125550142              # Pat Rivera, a returning customer
python -m receptionist chat --at "2026-10-31T23:40"            # Saturday night: after-hours rules apply
python -m receptionist prompt --at "2026-10-27T10:15"          # the exact system prompt the agent gets
python -m receptionist availability                            # open arrival windows
python -m receptionist log                                     # bookings, leads, messages, queued texts, calls
```

`chat` prints each tool call, the time to the agent's first words, and the post-call summary. A pretend `--at` time uses a throwaway database, so fake dates never reach `data/receptionist.sqlite`.

## Scripted test calls

```bash
python -m receptionist simulate --list
python -m receptionist simulate                       # all 20 scenarios, 4 at a time
python -m receptionist simulate gas_smell_inside -v   # one scenario with its transcript
```

A Claude Haiku 5.5 caller plays each persona in `scenarios/home_services.json` against the real agent, on an in-memory copy of the demo data at the scenario's local time. Each call is then checked two ways. `expect` (booked, transferred, lead, message, how it ended) is read from the database, along with a length check that fails any reply over 45 words. `rubric` items plus two standard ones are graded by Claude Sonnet 5.5 from the transcript, with the agent's full instructions and call details in front of it. The run prints pass/fail, median and 95th-percentile time to first words, and agent cost per call, and writes a full report with transcripts to `data/runs/`. `--effort low|medium|high` overrides the agent's effort for one run.

**Phase 0 gate (from the design doc):** 9 of 10 scripted calls handled correctly. Results vary by about two calls in 15 from run to run, so judge a change on two runs, not one.

## Testing budget: $1 a day

```bash
python -m receptionist budget              # today's spend, and what --quick and a full run cost
python -m receptionist simulate --quick    # 6 core scenarios, for iterating
```

- Every simulated call and every `chat` writes its cost to `data/spend.json`. A run that wouldn't fit in what's left of `simulate.budget_per_day` refuses to start, and each scenario reserves its estimated cost before it starts, so parallel scenarios can't overshoot together. `chat` refuses once the day's budget is gone.
- Estimates use the larger of `simulate.estimated_cost_per_scenario` ($0.015) and the last run's measured average, so they err high: about $0.09 for `--quick` and $0.22 for all 15. Costs come from token counts and the prices in `config.json`, so check the Console's usage page now and then.
- What cut the cost, against about $0.30 per full run before: the caller is Claude Haiku 5.5 instead of Sonnet (about 20 times cheaper per token); the grader's copy of the agent's instructions is cached, so it's sent at a tenth of the price after the first scenario; the grader runs at low effort with one- or two-sentence evidence; and a call that already failed a deterministic check skips grading.
- Each run prints its cost split three ways (agent, caller, grader). The grader is now most of it.
- **Measured (2026-10-23):** a quick run costs $0.053–0.065, about $0.009 per call (down from $0.02): agent $0.011, caller $0.003, grader $0.04–0.05. The Haiku caller improvises a little more than Sonnet did, for example pressing on safety questions, which makes calls slightly harder but still realistic.

## What the agent can do

- **Existing appointments:** finds them by the caller's number, tells them the window on file, and reschedules or cancels with `reschedule_appointment` and `cancel_appointment`. Changes need the caller ID, or a number the caller gives, to match the appointment, so a neighbor with a confirmation number can't cancel someone else's visit or hear its details.
- **Advice, within limits:** shares the business's `approved_tips` (shut the valve behind a running toilet, no drain chemicals before a visit) and simple, safe steps of its own (a towel under a drip). It never advises on gas, electrical work beyond the approved tips, opening appliances, tools or chemicals, never diagnoses a cause, and keeps emergencies to the playbook's safety lines. The tips in `acme.json` are demo tips: each real business has to approve its own.
- **Spanish:** switches when the caller speaks Spanish (`speech.looks_spanish`). The fixed "one moment" lines, spoken dates and the caller's texts switch too; the owner's texts stay in English. On Retell and Vapi the fixed lines are static text, so they're still English there.
- **Conversation:** keeps track of what the caller already said, answers side questions before carrying on, handles several requests, and books two different services on one call (a second call for the same service updates the first booking).
- **AI disclosure:** the greeting no longer announces it ("this is Sam"), but whenever a caller asks, the agent says plainly that it's the business's virtual assistant, and it never claims to be human. Several states regulate AI disclosure in customer calls; check the rules where each customer operates before launch.

## Latest results (2026-10-23)

| Measure | Result |
|---|---|
| Original 15 scenarios | 27 of 30 calls (90%) across two full runs: met the Phase 0 gate |
| Harder 28-scenario suite (speech-like caller, corrections, garbled numbers, Spanish, appointments) | 64–68% per pair of full runs; the last full run passed 16 of 28, and its gas, robocall and angry-caller failures were fixed and re-checked afterwards |
| Time to the agent's first words (model only) | median 1.1–1.7 s per run; 95th percentile about 2.8 s |
| Agent cost per call | about $0.02 to $0.04 on Claude Sonnet 5.5 (was $0.002 on Haiku, which was too slow with thinking on) |
| Effort | low scored 13/15 and medium 11/15 at similar speed, so the agent stays at low |

Every outcome-level problem found was fixed in code: wrong-year dates, silent transfers, silent hang-ups (a safety hang-up now always speaks the safety line), guessed phone digits, double bookings, and booking a tenant only after landlord approval. The remaining failures are wording: replies over 45 words, small promises ("I can get a technician out"), a stacked question, and the odd extra sentence of advice.

## Speed work (2026-10-23)

Time to first words, model only, under full test load (`simulate --thinking adaptive|disabled`):

| Setup | Passed | Median | 90th percentile | Turns over 2 s |
|---|---|---|---|---|
| Thinking on | 26/30 | 1.31 s | 2.67 s | 25% |
| Thinking off | 22/30 | 1.29 s | 1.80 s | 5% |
| Thinking on, fixed line on every tool call (current) | 15/17* | 1.16 s | 2.01 s | 13% |

\*The API account ran out of credit partway through, so 13 of these 30 calls didn't run.

- **Update 2026-10-25: thinking is now off, on Sonnet 5.5.** Live Retell calls measured 3.8 s end to end with Haiku thinking on (3.5 s of it the model). Code now says the safety lines (`safety.py`) and holds back any sentence of thinking out loud (`agent.Sentences`), which removed the reasons thinking had to stay on. Haiku with thinking off still narrated and stacked questions (3/10); Sonnet 5.5 with thinking off passed 7/10 at a median 1.5 s, 95th percentile 1.8 s. The remaining failures are single replies over 45 words.
- **Thinking stays on.** Off is faster in a quiet one-turn benchmark (about 0.9 s against 1.7 s), but under load the median barely moves. It costs pass rate, and it makes the agent narrate its instructions aloud ("Burst pipe, so I'll give the safety line first"); no prompt wording fully stopped that.
- **The slowest turns were tool calls the agent made without speaking.** The caller waited through two model calls, a median 2.5 s. Now a fixed line ("One moment while I check the schedule.") plays the moment a tool call starts. `tools.FILLERS` holds the lines; the Retell export plays them as static text during execution and the Vapi export as tool start messages. The prompt tells the agent not to announce those tools itself, so callers don't hear it twice.
- **On the phone platforms, the platform runs the model**, probably with thinking off for speed. The prompt now tells the agent never to narrate or speak tool names, and `simulate` fails any call where it does. Before going live, check the platform's thinking setting and re-run these scenarios with `--thinking disabled`.

The quality test for the final setup is incomplete (17 of 30 calls) because the credit ran out.

## Live phone line (Retell, custom LLM)

Retell runs the phone line, speech recognition and the voice; every reply comes from our agent over a websocket (`receptionist/live.py`), so all the guards in `agent.py` apply on real calls. Texting is off for now (`texting_enabled: false` in the business JSON), so the agent never promises a text; the owner sees calls, summaries, bookings and messages on the dashboard instead.

Once, in Terminal (the secrets go in `~/.zshrc`, never in the project):

```bash
echo 'export RETELL_API_KEY=paste-your-retell-key' >> ~/.zshrc
echo "export RETELL_WS_SECRET=$(openssl rand -hex 16)" >> ~/.zshrc
echo "export DASHBOARD_TOKEN=$(openssl rand -hex 12)" >> ~/.zshrc
source ~/.zshrc
```

Then, with ngrok installed and your free static domain claimed:

```bash
ngrok http --url=your-domain.ngrok-free.app 8000                     # terminal 1
python -m receptionist serve --port 8000                             # terminal 2
python -m receptionist retell-voices                                 # pick a voice_id
python -m receptionist retell-setup --base-url https://your-domain.ngrok-free.app --number +1XXXXXXXXXX --voice <voice_id>
```

`retell-setup` creates the Retell agent pointed at your server, assigns your number to it, and adds the number to `businesses/acme.json`. Call the number to talk to Sam. The owner's page is at `https://your-domain.ngrok-free.app/dashboard?token=<DASHBOARD_TOKEN>`.

The line works only while the Mac is awake and both terminals are running; move `serve` to a host such as Railway for an always-on line. The platform-hosted Retell and Vapi paths (`export retell|vapi`, `/retell/tool/*`, `/vapi/webhook`) still work, but they skip our agent loop and its guards.

## How the design is enforced

| Rule (design doc) | Where |
|---|---|
| Recording notice on every call; honest about being AI whenever asked | `greeting` in `businesses/acme.json`; platform rules in `prompts.py`; `ai_question_faq` scenario |
| Answer only from the business's facts | Facts compiled into the cached prompt (`prompts.business_facts`); graded on every test call |
| Offer only real availability | `check_availability` → `schedule.open_windows` (hours, 2-hour lead time, 2 technicians per window) |
| Book only after a read-back | Prompt rule; `book_appointment` rejects stale windows, towns outside the area and partial phone numbers |
| Emergencies get the safety line, then a person | `playbooks/home_services.md`; `transfer_call` allows the office only in business hours and on-call only after hours |
| Transfers fail over to an urgent message | `transfer_call` returns `no_answer`; `take_message` promises the callback time |
| Never take card numbers | Prompt rule; `card_payment` scenario |
| Only safe advice | `approved_tips` plus the prompt's limits; `advice_tips` scenario; graded on every call |
| Only the customer can change an appointment | `tools._callers_appointment`; `cancel_wrong_number` scenario |
| Callers can't change the agent | Tools scoped to the dialed number's business; `prompt_injection` scenario |
| Fast replies | Claude Sonnet 5.5 at low effort with thinking off, streamed a sentence at a time, the whole conversation cached, safety lines spoken by code; about 1.5 s to first words |
| Summary and quality flags after every call | `postcall.summarize` on Claude Sonnet 5.5, run by `chat` and by the server after each call |

Two changes from the design doc's tool list: business knowledge sits in the cached prompt instead of a `search_knowledge` tool, which saves a round trip per question while a business's facts stay small; and confirmation texts are sent by `book_appointment` itself rather than a separate `send_sms` tool.

## Cost per client (estimate, 2026-10-23)

Assumes 3-minute calls on Retell: $0.055 platform + $0.015 telephony + the voice + about $0.01 of Claude Sonnet 5.5 per minute ([Retell pricing](https://www.retellai.com/pricing)). The voice is $0.08 a minute for ElevenLabs v4 Turbo (the current one, chosen 2026-10-26 to sound more human), $0.04 for ElevenLabs Flash, or $0.015 for Cartesia, OpenAI and Retell's own voices. Each call also gets a post-call summary (about $0.008 on Claude Sonnet 5.5, cached) and, once texting is on, about 1.4 texts at about $0.013 each ([Twilio SMS](https://www.twilio.com/en-us/sms/pricing/us)). Fixed costs per client: $2 number, about $2 a month for the texting campaign ([Twilio 10DLC fees](https://support.twilio.com/hc/en-us/articles/1260803965530)), and about $1 of shared hosting.

| Plan | Price | Minutes | Cost with ElevenLabs v4 Turbo | Margin | Cost with a $0.015 voice | Margin |
|---|---|---|---|---|---|---|
| Starter | $99 | 200 | $39 | 61% | $26 | 74% |
| Pro | $249 | 800 | $140 | 44% | $88 | 65% |
| Team | $499 | 2,000 | $342 | 31% | $212 | 57% |

The voice platform and the voice are about 90% of the cost; Claude is under 10%. Options: keep ElevenLabs for the most human sound and price plans higher, offer it as a premium tier, or move to a self-hosted voice pipeline (LiveKit or Pipecat, about $0.04 a minute plus the voice). One-time per client: texting registration, $4.50 (sole proprietor or low volume) or $46 (standard brand), plus $15 per campaign.

## Hosting on Render

`render.yaml` describes the service: an always-on web service in Oregon (close to Retell), a 1 GB disk at `/var/data` for the database, and the start command `python -m receptionist serve --host 0.0.0.0 --port $PORT`. The free plan sleeps when idle, so use a paid plan.

1. Put the project in a private GitHub repository (`.gitignore` already leaves out `data/`, `.venv/` and caches; no secrets are in the project):
   `git init && git add . && git commit -m "Receptionist" && git branch -M main`, then create an empty private repo on GitHub and `git remote add origin <url> && git push -u origin main`.
2. In Render: New > Blueprint, connect the repo, and enter the four secrets it asks for (`ANTHROPIC_API_KEY`, `RETELL_API_KEY`, `RETELL_WS_SECRET`, `DASHBOARD_TOKEN`; use the same `RETELL_WS_SECRET` as in `~/.zshrc`).
3. When it is live, `https://<name>.onrender.com/health` should answer. Then point Retell at it from your Mac: `python -m receptionist retell-tune --base-url https://<name>.onrender.com`.
4. Call the number, then stop ngrok and the local server. The dashboard is at `https://<name>.onrender.com/dashboard?token=<DASHBOARD_TOKEN>`.

The first deploy seeds the demo data. Calls, bookings and messages live on the disk, so they survive deploys. Set `PYTHON_VERSION` in `render.yaml` if a build complains about the Python version.

## Going live: what's left

| Step | Status |
|---|---|
| Agent, tools, scenarios, budget guard | Built and tested in text (`simulate`) |
| Voice platform (Retell) and a phone number | Live-call websocket, setup command and dashboard are built and tested against a simulated Retell; **needs your Retell account** for the first real call |
| Public hosting for `serve`, always on, with https | **Needs a host** (Render, Railway, Fly.io or a small VPS); ngrok works for a demo |
| Sending texts | **Pinned** (`texting_enabled: false`): needs a Twilio sender and A2P 10DLC approval (days to weeks) |
| Owner sees bookings | Dashboard at `/dashboard?token=...`; calendar sync later |
| Run our agent on real calls | **Recommended:** use the platform's custom-LLM option (Retell Custom LLM websocket, Vapi custom-llm) so `agent.py` runs every turn, with thinking on, language-aware fixed lines, the short-number note and the silent-hang-up guard. With the platform-hosted model, none of those apply; re-run `simulate --thinking disabled` to see how that model does |
| A real client's config | Hours, services, prices, approved tips and transfer numbers in a `businesses/<id>.json` |
| Legal review | Recording notice, AI disclosure by state, consent for texts, terms of service |
| Monitoring and backups | Alerts on errors and call-volume drops; database backups |
| Billing | Stripe, or invoice the first pilots by hand |

Accounts and keys needed: Anthropic API key (have), a Retell or Vapi account and API key, a Twilio account (API key, a number, 10DLC registration), a hosting account, a Google Cloud project for the Calendar API (free), and Stripe when billing starts.

## Tests

`python -m unittest discover tests` covers phone and time formatting, scheduling, tool guardrails, the prompt layers, the agent loop against a scripted fake client, both platform adapters (including Retell signature checks) and the webhook server. Set `RECEPTIONIST_CONFIG=/path/to/other.json` to run against a separate config and data folder.
