# Texting registration (A2P 10DLC): prepared answers

Status: **not started**. Texting stays off (`texting_enabled: false`) until Noah decides to spend on it. The sender code
is built and tested against a fake (`receptionist/notify.py`: `TwilioSender`, STOP handling, owner-only alerts).
Nothing here has been submitted. Check every fee and field name against Twilio's current pages when you register:
<https://www.twilio.com/docs/messaging/compliance/a2p-10dlc> and the fees page linked from README "Cost model".

## What gets registered
US carriers require every business that texts US numbers from a normal 10-digit number to register a *brand* and a
*campaign*. Two paths:
1. **Sole proprietor** (no EIN): lowest volume, lowest fee, one number. Fine for Noah's own first pilots only if the
   texts come from Noah's business, not from each shop.
2. **Standard / low-volume standard brand** (EIN needed): the right one once each shop is a client. As an ISV (we send
   on behalf of shops) Twilio expects either one brand per shop or a registered ISV setup; read Twilio's ISV page
   before choosing.
Registration review takes days to weeks. Start it when the first pilot agrees, not before.

## What the product texts (the campaign description)
Use case: **Account notifications / customer care** (low volume, one number per shop).

Two kinds of text, both *transactional*, never marketing:
- To the **shop owner's own phone**: "New booking A1007: Pat, (208) 555-0142, Fri 8-10, ...", new message / lead alerts,
  cancellations. The owner supplies their own number during onboarding and agrees in the pilot agreement.
- To a **caller who just phoned the shop and gave their number to the assistant**: one confirmation or change notice for
  their own booking (date, window, confirmation code). No promotions, no follow-up campaigns.

Campaign description (paste): "A virtual receptionist answers a home-services business's phone calls. When a caller books,
changes or cancels a visit, the system texts that caller one confirmation with the date, time window and confirmation
code, and texts the business owner an alert about the booking. Messages are sent only to people who called the business
and gave their number during the call, or to the business owner. No marketing."

Sample messages (2-5 are required; these match the code):
The first shop is a heating, cooling and plumbing company (the pilot vertical), so the samples use one. A lawn care or
snow shop's texts have the same shape with its own name and service.
1. `Peak Heating & Plumbing: you're booked for furnace repair, Fri Nov 13, 8-10 AM. Confirmation A1007. Your technician will text about 30 minutes before arriving. Reply STOP to opt out.`
2. `Peak Heating & Plumbing: your visit A1007 moved to Mon Nov 16, 10-12. Reply STOP to opt out.`
3. `Peak Heating & Plumbing: your visit A1007 is cancelled. Reply STOP to opt out.`
4. `New booking A1007: Pat, 208-555-0142, Fri 8-10, sprinkler leak.`   (owner alert)

## How the callers opted in (the consent answer)
"The person phones the business and, on the call, asks to book or change a visit and gives their phone number so the
business can confirm. The assistant confirms the number aloud before saving it. The confirmation text is the direct
result of that request." Opt-in is therefore verbal, on a recorded call; keep the call record (transcript) as proof.
Twilio may want a *written* description of the opt-in flow plus a screenshot or script: use the greeting and the
`take_message` / `book_appointment` read-back from the playbook.

## Required elements the code already supports
- Every caller-facing text ends with "Reply STOP to opt out." (`notify.STOP_SUFFIX`; Spanish line in `tools.STOP_ES`).
- STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT stop all caller texts; START, YES, UNSTOP resume them
  (`Notifier.handle_inbound_sms`, table `sms_optout`). Twilio answers STOP/HELP itself on the Messaging Service; our
  table keeps the queue from texting a number that opted out.
- Owner alerts go only to the shop's own `owner_sms`.
- HELP reply to set on the Messaging Service: "Peak Heating & Plumbing booking texts. Questions: call (208) 555-0100. Reply STOP to opt out." (use the shop's own name and number)

## Not built yet (needed before texting goes live)
- A route for Twilio's inbound-message webhook that calls `Notifier.handle_inbound_sms` (the method is ready and tested;
  the HTTP route is not, to keep `server.py` untouched by this work).
- Privacy policy and terms pages on a real domain (the registration form asks for URLs).
- Per shop: confirm `owner_sms` and set `texting_enabled: true` only after the campaign is approved.

## What Noah does (in order)
1. Decide sole proprietor vs standard brand (above). 2. Create the Twilio account and a Messaging Service.
3. Submit the brand, then the campaign with the answers on this page. 4. Wait for approval. 5. Set
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_MESSAGING_SERVICE_SID` on Render. 6. Set `texting_enabled: true` on one shop and
send a test booking to his own phone.
