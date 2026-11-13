# Receptionist

An AI phone receptionist for small trades shops, sold as a monthly subscription. When nobody at a shop picks up (or the
line is busy) the shop's carrier forwards the call to us: the agent answers in the shop's name, books the job inside
the shop's hours, takes a message, and for an emergency speaks the safety line and tries the owner's cell. The owner
gets every call by email and on a phone dashboard, a daily digest, and a weekly report.

- **Pilot vertical:** emergency home trades (heating, cooling, plumbing), North Idaho. Demo shop `businesses/hvac.json`
  ("Peak Heating & Plumbing (demo)", agent Riley) runs the live number (`live_businesses`). Lawn care and snow removal
  (`businesses/lawn_care.json`, Cedar Ridge Lawn & Snow) is the second vertical, kept as a demo shop with a placeholder
  number. `new-client --template hvac|lawn_care` starts either one; the agent's name comes from the template. `acme` (business hours) is a test fixture. The med spa is archived at git tag `med-spa-archive`.
- **Design doc** (product, pricing, go-to-market): https://claude.ai/artifact/GtSqWJL89vevGzkp3oeMUN
- `CLAUDE.md` is the working notes (rules, architecture, current state); `docs/HISTORY.md` is the dated changelog.

## How a call runs
Retell (voice, phone line) opens a websocket to `receptionist/live.py`; every turn is answered by our own agent loop
(`agent.py`, Claude Sonnet 5.5) with its guards: `safety.py` speaks emergency lines from code, `agent.Sentences` and the
`<say>` channel decide what is spoken, `degraded.py` takes over if the model fails. Tools (`tools.py`) book, look up,
reschedule, take messages, create leads and transfer. After the call `postcall.py` summarizes it and `notify.py` emails
the owner. Platform-hosted agents (Vapi, Retell-hosted LLM) were removed: they bypass the guards.

Prompt layers: `prompts.py` (rules) + `playbooks/<vertical>.md` + the shop's facts (`businesses/<id>.json`). Real clients'
files live in `data/clients/` on the server, never in git.

## Setup
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY=...        # needed for chat and simulate only; the unit tests need no key
.venv/bin/python -m unittest discover tests          # about 13 s
.venv/bin/python -m receptionist chat --business hvac   # talk to it as a caller
```

## Testing budget: $1 a day
`python -m receptionist budget` shows today's spend. `simulate --business hvac --quick` (6 scenarios, about $0.40) while
iterating; the full suite (`simulate --business hvac`, 16 scenarios, about $1.05) only to confirm. The gate for a change
to the agent is 9/10 across two full runs with no failure on safety lines, invented prices or AI disclosure. Run reports
with full transcripts land in `data/runs/`. Raise the day's limit only on the owner's word (`budget --raise-today N`).

## Commands (all in `python -m receptionist ...`)
| Command | What it does |
|---|---|
| `serve` | the live server (Retell websocket and webhooks, dashboard, landing page at `/`, admin routes) |
| `new-client [--from intake.yaml] [--template hvac\|lawn_care]` | writes a shop's file and a printable owner checklist (`.checklist.html`) |
| `client-upload`, `owner-links` | put a shop's file on the server; print its private dashboard and calendar links |
| `retell-tune` | push voice settings to the Retell agent (needs the owner's yes: it changes the live line) |
| `simulate`, `budget`, `chat`, `record` | testing |
| `backup`, `restore --dry-run`, `purge` | data safety and deletion |
| `latency-report`, `finance-sync`, `spend-report` | where the time and money go (read-only) |
| `calendar-sync --business ID` | check a shop's Google Calendar access (`--push` re-sends bookings) |
| `voice-test lines|blind` | blind voice comparison kit (`docs/voice-comparison.md`) |

Outbound discovery caller (`outbound*.py`, `playbooks/sales.md`, cron `outbound-caller-trigger`): its pitch follows the
lead's trade (heating, cooling and plumbing first, lawn and snow second) and the demo line it points to is whichever shop
holds the number. The leads on the server today are still lawn and snow companies. Its mode, gates, call hours and cron
are the owner's to change; leave them alone.

## Server settings (environment on Render)
Required: `ANTHROPIC_API_KEY`, `RETELL_API_KEY`, `RETELL_WS_SECRET`, `DASHBOARD_TOKEN` (admin key), `TOKEN_SECRET`.
Email: `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` (Gmail with an app password works with no domain), `ADMIN_EMAIL`; later
`EMAIL_API_URL`, `EMAIL_API_KEY`, `EMAIL_FROM` for an HTTP provider. Backups: `BACKUP_S3_*`. Calendar: `GOOGLE_SERVICE_ACCOUNT_JSON`.
A shop gets email with `"notify": {"email": [...]}` in its file (`new-client` asks). Texting is off (`texting_enabled`)
until Twilio registration is done; see `docs/10dlc-registration.md`.

Admin routes (admin key): `/admin/notifications` (emails sent and failed), `/admin/costs` (cost per minute per shop),
`/admin/latency`. Open to anyone: `/health`, `/health/deep` (point an uptime monitor here), `/`.

## When something fails
- **Model fails or stalls:** the call becomes a code-driven message taker (name, 10-digit callback, one sentence, saved as urgent).
- **Server unreachable:** Retell (docs checked 2026-11-12) gives the caller nothing: it retries the websocket three times,
  then ends the call. `fallback_number` only covers Retell's own outage or full concurrency (`inbound_fallback_number`
  in the shop file). The shop's carrier forwarding on "no answer / busy" is the safety net; watch `/health/deep`.
- **Backups:** Render snapshots the disk every 24 h for 7 days; `backup.py` also uploads a tar.gz twice a day to an
  S3-compatible bucket and keeps 30 days (off until `BACKUP_S3_*` is set).
- **Prompt cache:** `keepwarm.py` refreshes each live shop's cache every 50 minutes by day. Measured 2026-11-12 on the
  hvac/lawn prefix (14.9k tokens): a second identical request read all of it and wrote nothing, so a refresh costs a read
  (about $0.003), not a write.

## Rules that do not change
- The agent says plainly that it is the shop's virtual assistant when asked and never claims to be human.
- Real clients' greetings always carry the recording notice and the AI disclosure (`new-client` refuses otherwise).
- Advice is limited to approved tips and simple, safe steps; never gas, electrical work beyond tips, opening equipment or
  chemicals; no diagnosis; emergencies stay on the safety lines in `safety.py`.
- Card numbers are removed before the model sees them; a record is shown only to the caller ID's owner.

## Go-live checklist for a new shop
1. `new-client --from intake.yaml`; print `<id>.checklist.html` for the owner. It needs the owner's cell and email.
2. Assign the shop its own Retell number and add it to `phone_numbers` in the file; `client-upload --business ID --base-url <server>`; `retell-tune --business ID`.
3. Call the number yourself, including an emergency line; check the email arrives and the call shows on the dashboard.
4. Send the owner their dashboard link (`owner-links`, never in chat) and have them do the forwarding test.
5. Day one: read every transcript; turn forwarding off the same day if a safety line was missed, a price invented or a caller misled.
6. Have a lawyer read `docs/pilot-agreement-template.md` before anyone signs it (draft; price and terms are placeholders).
7. Optional: Google Calendar sharing (`calendar-sync --business ID` to verify; not yet run against a real calendar).
