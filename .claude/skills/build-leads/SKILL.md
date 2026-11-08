---
name: build-leads
description: Find lawn care, landscaping and snow removal businesses around Sandpoint, Idaho and add them to the outbound discovery caller's lead list. Use when Noah asks to build, refresh or extend the lead list.
---

# Build leads

Goal: real lawn care, landscaping and snow removal businesses in our service area, in the lead list safely. Nothing else
(no heating, plumbing, HVAC, tree-only services).

## Rules that never bend
- **Never invent or guess** a phone number, a town or a category. If the page does not show it, skip the business.
- Every candidate carries `source`: the URL of the page you read it on (the business's own site or listing page).
- **Never write to the live list directly.** The only route in is `POST /admin/outbound/leads/merge`; the server checks
  every candidate (name, real 10-digit number, service-area town, source URL, trade; skips known and do-not-call numbers;
  never edits existing leads). Do not use `PUT /admin/outbound/leads` (it replaces the whole list).
- **Show Noah a sample first** and post to the live server only after he says yes. Build and check candidates in a local
  file in the scratchpad until then.
- `line` (landline/voip/mobile) is optional: set it only if the page or a lookup shows it, never guess. It does not gate
  calling (Noah handles the legal side).
- The cron calls the next eligible lead every weekday morning, so tell Noah how many new leads are queued before posting.

## Steps
1. Towns: `service_area.towns` in `businesses/lawn_care.json` (24 towns: Sandpoint, Ponderay, Kootenai, Dover, Sagle,
   Hope, East Hope, Clark Fork, Priest River, Oldtown, Laclede, Cocolalla, Careywood, Colburn, Samuels, Naples,
   Bonners Ferry, Priest Lake, Coolin, Nordman, Blanchard, Spirit Lake, Athol, Bayview).
2. For each town, WebSearch the three trades ("lawn care <town> Idaho", "landscaping <town> Idaho", "snow removal <town>
   Idaho"). Open the business's own page (WebFetch) and read the name, phone, town and what they do from it.
3. Write candidates as JSON: `{"name", "phone", "town", "type", "source"}`, optionally `contact` (an owner's name shown
   on the page), `notes`. `type` is the category as the page words it ("Lawn care", "Landscaping and snow removal").
4. Check them locally without the network:
   `.venv/bin/python -c "import json,sys; from receptionist import leads; from receptionist.config import load_config; m,r=leads.merge([],json.load(open(sys.argv[1])),leads.service_towns(load_config())); print(json.dumps(r,indent=1))" FILE`
5. Report to Noah: how many found, how many would be added, how many skipped and why, and a sample of 5 leads (name,
   town, type, phone, source). Wait for his yes.
6. On yes, with his admin key (ask via `request_credential` for `receptionist-575z.onrender.com`; never put keys in the
   repo or chat): `POST https://receptionist-575z.onrender.com/admin/outbound/leads/merge` with
   `{"candidates": [...]}` and `Authorization: Bearer <DASHBOARD_TOKEN>`. Report the server's counts back.

## Refill mode (scheduled run, no one in the chat)
Used by the scheduled refill task. Do it in this order:
1. `GET /admin/outbound/leads`. Count leads with `stage == "leads"` (not yet called). If 5 or more are left, stop and say so.
2. Otherwise run the Steps above, skipping the "wait for Noah's yes" step: post the batch to `/leads/merge`. Rules above still apply (nothing guessed, a source URL on every lead, merge endpoint only).
3. Report to Noah: how many were uncalled before, found, added, skipped and why, and the names added. If fewer than 5 new leads were added, say the sources are running dry and ask for a new source.
