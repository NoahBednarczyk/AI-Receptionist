# Build spec: structural speech, first-seconds latency, outbound queue, personality (2026-11-09, evening)

For: the Claude Code session that edits `NoahBednarczyk/AI-Receptionist` (local copy `~/Documents/receptionist`).
Written against `main` at `d5d9455`. `git pull` first. If HEAD differs, re-check every line reference before building.
Approved by Noah in chat on 2026-11-09 ("everything you just said I want done"). Supersedes the parts of
`2026-11-09-outbound-latency-and-leak-fix-spec.md` it contradicts (the no-handling ladder and the blocklist guard).

## 0. Rules for this build

- Four phases, in order. One commit per work item, with the item id in the message (`A1: ...`). Push once per phase, only when `python -m unittest discover tests` shows no new failure (the old `test_privacy.py` dashboard-escaping failure may remain).
- **The weekday cron (`outbound-caller-trigger`, 08:00 PT) dials a real shop with whatever is deployed.** Before the first push, ask Noah whether it is suspended. If it is not, do not push a phase until its paid check below has passed.
- Never call `POST /admin/outbound/call-next` yourself. No calls to shops. The only live call in this spec is the one test call to Noah's own phone (`test: true` lead).
- Spend caps: simulations stay inside the existing $1 a day (`receptionist/budget.py`); one outbound test call, about $0.30; the cache refresh in C2, about $0.05 to $0.10 a day. Anything beyond these needs Noah's yes with the amount stated.
- Do not run `retell-tune` or change any Retell agent setting. Voice and Retell settings are Noah's decisions; report numbers, change nothing.
- Never weaken a test that pins: the safety lines, "never claim to be human", the AI-disclosure sentence, `LawnCareOnlyTests`, stop-request handling.
- If anything here contradicts the code at HEAD, stop and report before building on either.

## 1. Evidence (measured on `d5d9455`, 2026-11-09)

- `speech.unspeakable` drops sentences the agent is supposed to say: "The owner isn't picking up right now.", "You'll hear from them within thirty minutes.", "Wait, did you say Sagle or Sandpoint?", "They don't plow walkways unless it's in the contract.", "That's a better fit for spring." all return True. Since W5, one caught sentence silences the rest of its block, so the callback time after a failed owner transfer is lost with it.
- It passes real reasoning: "So they want mowing, which means I need the address first." returns False.
- `finish()` is the only place an attempt is recorded (`outbound_live.py:518`), and it runs only when the socket opened. An unanswered call leaves no attempt and no row, so `call_next` picks the same lead every weekday.
- `outbound_leads.json` is written only by `PUT /admin/outbound/leads`. A lead's stage never changes after a call, so "Not interested" and "Demo booked" leads stay callable (up to 2 a week, forever).
- `outbound.facts` says the product "texts or calls the owner". Texting is off (`texting_enabled: false`) and SPEAKING says so two paragraphs earlier.
- The lawn care stable prompt is 7,999 words with 94 "never" and 56 "don't / do not". Character is defined in three places (`PLATFORM_RULES` personality bullet, `lawn_care.md` "How you sound", `call_state` flavors/quirks/acks) and a random style is drawn per call.
- `MAX_NOS = 4` and `sales.md` "Playful pushback" ("A no is where the conversation starts", "And the one you missed Tuesday?", which states a fact nobody gave).

---

## Phase A. Outbound: first seconds, queue, honesty

Files: `receptionist/outbound_live.py`, `receptionist/outbound.py`, `receptionist/playbooks/sales.md`, `receptionist/server.py`, `CLAUDE.md`, `docs/outbound-plan.md`, tests.

### A1. Code speaks the intro and the first question (no model in the first two turns)
Today the turn after "is this the owner?" is the first model request of the call. Its content is always the same: who is calling, then question one.
- Add `AFFIRM`: the caller's answer is 8 words or fewer, matches one of `yes|yeah|yep|yup|speaking|this is (he|she|him|her|\w+)|that'?s me|you got (him|her|me)|it is|uh[- ]?huh|mm[- ]?hm+|hello|hi|hey|go ahead|what can i do for you|how can i help( you)?|what'?s up|who'?s (this|calling)|who is this`, and matches none of `stop_request`, `NO_SIGNAL`, `AI_QUESTION`, `AI_REFUSAL`, `TIMING` (A4), `machine_kind`, or the words `no|not|wrong|busy`.
- In `OutboundSession.respond`, at the `not self._intro_checked` turn: on `AFFIRM`, set `_intro_checked` and `_person`, speak `self.intro_text` then one line from a new pool `FIRST_QUESTIONS`, save both into `messages` and `transcript` the way the opener branch does, return a `Turn` with no model call. Anything else falls through to the model with `intro_note`, as now.
- `FIRST_QUESTIONS` by `trade_of(lead)`, three variants each, each one sincere question about the last missed call, for example lawn: "When's the last time the phone rang while you were on a mower and you couldn't get to it?"; snow: "...while you were in the plow truck..."; default: "...while you were out on a job...". No claims, no numbers.
- Emit the intro and the question as two `on_text` chunks. `protected(chunk)` returns True for the intro chunk only, so line noise cannot cut off who is calling.
- Tests: "Yes, this is Dave." and "What can I do for you?" get intro + question with zero client requests; "Who is this?" gets the same; "No, wrong number", "I'm busy", "Is this a robot?" go to the model with the intro note; with `disclose_ai_in_intro` true the spoken intro contains "AI assistant".

### A2. Every dial leaves a record; the queue reads results
- `place_call`: after `call_fn` returns, `state.add_attempt(phone, now)` and `state.add_dial(call_id, {...lead id, name, phone, at})`. `finish()` adds an attempt only when no dial is recorded for its call id (calls started outside `place_call`). Every row gets a `callId`.
- `/retell/webhook`, on `call_ended` for a call id found in `state.dials` that has no result row: write a row with `outbound.call_row` (outcome "No answer", or "Voicemail" when Retell's `disconnection_reason` says a machine answered; duration from Retell; `estCost`). The reason strings and duration field are **unverified: check Retell's docs first** and map conservatively (unknown reason = "No answer").
- `outbound.can_call` takes the phone's past result rows. New refusals: last outcome in `{"Not interested", "Demo booked", "Pilot signed"}` ("already has an outcome: X"); "Callback requested" before its `followDate`; total attempts at or over new setting `max_attempts_total` (default 4). `plan_queue`, `call_next`, `place_call` and the block runner pass the rows through. A `test: true` lead skips these as it skips the others.
- `State`: every mutator reloads the file inside a module-level lock keyed by path, applies its change, then saves (read-modify-write). Today each socket holds its own copy from call start and writes it whole at the end, which can erase a do-not-call entry or slots written meanwhile.
- Tests: an unanswered dial blocks a same-day redial and counts toward the week; the webhook row appears once (a second `call_ended` adds nothing); a "Not interested" lead is refused next week; two `State` objects on one file both keep their writes; `spent_today` counts an unanswered call at its row estimate, not the full reserve.

### A3. Two refusals end the call; pushback is one sincere question
- `MAX_NOS = 2`. Delete `NOTE_LAST_TRY`, `NOTE_NO_AGAIN_CALM`, `_last_try`. `_no_directive`: an irritated first no, or any second no, returns `NOTE_NO_AGAIN`. Meeting asks: at most 2.
- `NOTE_NO_ONCE`: "they said no. Accept it in a few words, then ask one sincere question about the reason (is it the timing, the AI, or are missed calls simply not a problem for them). Take the answer at face value. No joke about the no, no teasing, no pitch."
- `NOTE_AI_REFUSAL` (first one counts as the first no): "they don't want to talk to an AI. Say plainly that yes, you are one, and offer the alternative: Noah can call them himself. One short question. Nothing else."
- `NOTE_NO_AGAIN`: "they said no again. Thank them, wish them a good season, call log_call, then end_call. No further ask and no parting quip."
- Tests: second calm no ends the call; irritated first no ends the call; the AI refusal note offers Noah; no reply after `_nos == 2` contains a meeting ask.

### A4. "Not right now" is timing, not a refusal
- Remove `not right now` from `NO_SIGNAL`. Add `TIMING`: `not (right )?now|bad time|busy|in the middle of|on a job|driving|call (me )?(back|later)|later|another time`. When it matches and `NO_SIGNAL`/`stop_request` do not: no increment, note: "bad time for them. Offer one specific callback time from the open list, get a yes or a better time, call log_call with Callback requested and the date, then end_call. No questions about their calls now."
- Tests: "Not right now, I'm on a job" leaves `_nos` at 0 and passes the note; "don't call me today, try Thursday" still is not a stop request.

### A5. The pitch only claims what is live
- `outbound.facts`: replace "takes messages, and texts or calls the owner" with "takes messages, and puts every call on the owner's dashboard". Keep "Texting and calendar sync are not live" in SPEAKING.
- `sales.md` step 6: "say Noah will call from this number" (no "text").
- Test in the style of `LawnCareOnlyTests`: the stable outbound prompt contains no `\btext(s|ing|ed)?\b` outside the one "not live" sentence and the "Send me some info" objection.

### A6. Rewrite `sales.md` character and objections (text in section 6)
- Replace "Who you are on the phone", delete "Playful pushback", rewrite "When they hold back" and the objections that depend on it, align "Hard stops" ("a second no", not "a third"), and fix step 1 so it matches A1 (code says the intro and first question; the model starts at the owner's first real answer).
- Delete every example that asserts something about the owner's business ("the one you missed Tuesday", "that $800 spring cleanup went to whoever picked up" unless the owner said the figure).
- Extend the grep test from W3: `sales.md` must not contain "ladder", "playful pushback", "where the conversation starts", "stubborn", "naive", "Tuesday?".

### A7. Remove the "off until the legal check" wording (Noah's instruction)
- `CLAUDE.md`: in "Outbound discovery caller", delete the clause saying cold mode stays off until the legal check. Leave the other rules of that sentence (AI said in the first sentence, never human, a clear stop ends the call).
- `outbound.py`: docstring line 10 drop "and the legal check in docs/outbound-plan.md"; line 44 comment becomes "the switch for cold mode"; line 86 reason becomes "cold calling is switched off".
- `docs/outbound-plan.md` lines 5, 32, 49: drop the legal-check wording.
- Do not touch the README's inbound "Going live" table or `docs/outbound-legal-notes.md`.

### Phase A check
Unit tests, push, then one test call to Noah's phone (`test: true` lead; Noah places and answers it). He reads: "Yeah, this is Noah." / (answers the question in one sentence) / "Not interested." / "No, I'm good." Pass: turn 2 is the code intro plus question with `first words` under 0.3 s and no model request in the log; every later turn `first words` at or under 1.8 s with `cache_read > 0` on the first model request; the first no gets one question; the second no ends the call with `log_call` then `end_call`; no stall line; the transcript has no sentence about the owner in the third person. If it fails, stop and report the numbers; no second paid call without Noah's yes.

---

## Phase B. Structural speech: the say channel

Files: `receptionist/agent.py`, `receptionist/speech.py`, `receptionist/prompts.py`, `receptionist/outbound.py`, `receptionist/outbound_live.py`, `receptionist/simulate.py`, `config.json`, tests.

Principle: today everything the model writes is speech unless a regex recognises it as reasoning. Invert it: nothing is speech unless the model put it inside `<say>...</say>`.

### B1. `SayChannel` (in `agent.py`)
- A stream parser with two states. Outside a tag, text goes to `private`. Inside, text goes on to `Sentences.add`. Tags are exactly `<say>` and `</say>`. Tags split across deltas must work: hold back a buffer tail that is a prefix of the tag being looked for. An unclosed `<say>` is closed by the end of the text block. Text after `</say>` is private again; several `<say>` in one block are allowed.
- In `respond`, `text_delta` goes through the channel; `content_block_stop` closes the channel, then `words.close()`.
- A text block with no `<say>`: nothing is spoken. If the private text matches `SILENT` ("(listening)"), set `words.silent`. Otherwise the existing nudge path runs (once); change `NUDGE` to end with "Reply now inside <say>...</say>."
- History: every assistant text block is stored as `<say>{what was spoken}</say>`, and omitted if nothing was spoken. Private text never goes into `messages`. It goes into the transcript entry as `private` and to the log at INFO.
- Lines the code speaks and saves as assistant turns are wrapped the same way through one helper: the must-leave safety turn, the refusal and max_tokens lines, the end-of-turn fallback, and in `OutboundSession` the opener, the A1 intro and `_stop`. Otherwise the model sees untagged examples of itself and drifts.
- `interject()` parses its reply with the same channel and uses only the say text.
- Flag `agent.speech_channel` in `config.json`, default `true`. With `false`, behaviour is byte-for-byte today's (rollback).

### B2. The prompt block
- `prompts.SAY_FORMAT`, appended as the last section of the stable prompt through one helper used by `CallSession.__init__`, `agent.prewarm`, `outbound_live._stable_system` (so `warm()` and the session still build the identical prefix; keep the JSON-equality test). `stable_prompt(biz, channel=False)` stays the default so `platforms.py` (Vapi and Retell-hosted) and `onboarding.py` never get it: a hosted model would read the tags aloud.

```
# Output format (this overrides anything else about format)
Only text inside <say>...</say> is spoken. Everything outside the tags is private: nobody hears it and it is thrown away.
- Almost every reply is just <say>your words</say>, with <say> as your very first characters. The caller is waiting, and this is the fastest way to answer.
- If you need to work something out first (which service fits, whether a town is in the area, what they meant), write it before the tag in eight words or fewer, like a note to yourself, then open <say>.
- Inside <say> you talk to the caller and only to the caller: never about them, never about what you are doing or why.
- Before a tool call, write no <say> unless the rules tell you to say a line first.
- To stay silent, write (listening) with no tags.
```
- In both prompts, change "reply with exactly (listening)" wording to agree with the last bullet.

### B3. The guard that remains
- In channel mode, `Sentences._release` drops a sentence only for `speech.channel_guard(s)`: `SPOKEN_INTERNALS` minus its `<[a-z/]` alternative, plus one pattern, a sentence opening with "the caller" or "this caller". The taint (rest of block dropped) applies to these only.
- `SELF_TALK` and `NARRATION` stay in the file and run in shadow: when the old `unspeakable` would have dropped a sentence that channel mode speaks, log a WARNING (`shadow: blocklist would drop %r`) and append it to the entry's `shadow` list. It is still spoken. After about 20 calls the list says whether anything real is leaking inside the tags.
- `FIRST_CLAUSE` early release stays, inside the tag.
- The existing `unspeakable` tests stay as they are (legacy path).

### B4. Tests (scripted fake client, stream deltas)
- `<say>` split as `<s` + `ay>Hi.` + `</sa` + `y>` speaks "Hi." once.
- "They said no once. <say>Fair enough, is it the timing?</say>" speaks only the question; `private` holds the note; `messages` holds `<say>Fair enough, is it the timing?</say>`.
- "The owner isn't picking up right now." and "You'll hear from them within thirty minutes." inside `<say>` are spoken, with a shadow entry.
- A block with no tag speaks nothing, triggers one nudge, then the fallback line.
- "(listening)" with no tags is silent, no nudge.
- Text after `</say>` is not spoken. A tool call after a say block works as before.
- `speech_channel: false` reproduces today's outputs for three existing `Sentences` cases.
- `platforms.py` prompts contain no "<say>".

### B5. Simulation (inside the $1 a day)
`simulate --business lawn_care --quick` once. Add to the report per turn: private words and whether the reply began with `<say>`. Pass: no reasoning spoken; at least 80% of replies begin with `<say>`; no private note over 12 words; pass count no worse than the last quick run (5/7 on 2026-11-01) by more than one. If replies arrive with missing tags more than once per call, stop and report: the format instruction needs work before this ships.

---

## Phase C. Latency: measure, keep warm, then decide

### C1. `latency-report` (read-only)
`python -m receptionist latency-report --last 10`: for the last N Retell calls, print per call the e2e, llm and tts latency (p50, p90) from Retell's call object, our own `first words` median where stored, the voice model, and the remainder `e2e - llm - tts` (end-of-speech detection plus network). Field names are unverified: read Retell's get-call and list-calls docs first. If `RETELL_API_KEY` is not on the Mac, expose the same thing as `GET /admin/latency?last=N` (admin key) and have the CLI call it. Paste the table to Noah.
Report, do not change: if tts p50 is over 400 ms, that is a voice-model decision (`eleven_v3_conversational` was never measured); if the remainder p50 is over 700 ms on the receptionist, name `enable_dynamic_responsiveness` (on since 2026-11-02) as the likely cause.

### C2. Keep the inbound prompt cache warm
`server.keep_warm`: every 50 minutes, between 07:00 and 21:00 in the shop's time zone, run `prewarm` for `prewarm_targets(cfg)` and log its usage. Expected: `cache_read > 0`, `cache_write` near 0 (a read should renew the one-hour life). If the log shows a full write every time, turn it off (`live.cache_refresh: false`) and report; that would cost about $0.45 a day, which is not approved. Setting `live.cache_refresh`, default true. Add the refresh to the finance ledger so `finance-sync` prices it. Tests with a fake clock: refresh fires at 50 minutes, not outside hours, and never for a shop without a live number.

### C3. Speculative start (build only if C1 says so; flag off by default)
Gate: build this only if C1 shows the receptionist's remainder p50 at or above 500 ms. Otherwise skip it and say so.
- Inbound `CallSession` only (`speculative_ok = True` on `CallSession`, `False` on `OutboundSession`, whose `respond` changes counters before the model runs).
- On an `update_only` whose last entry is caller text of two or more words ending in `.`, `?` or `!`, with the worker idle: start `session.respond` on that text with a `TurnControl` in hold mode. Chunks are buffered, not sent. `proceed(irreversible=True)` blocks until the turn is confirmed (5 s limit, then it drops itself), so nothing books, transfers or hangs up on words the caller may not have finished.
- On `response_required`: if the text to answer equals the speculated text after lowercasing and stripping punctuation, and there is no `heard_only` or echo note, adopt the turn: flush the buffer under the new response id, confirm, continue the normal loop. Otherwise drop it and answer normally. A dropped turn changes nothing (it works on copies), which the existing design already guarantees.
- Setting `live.speculative.enabled`, default `false`. Log adopted vs dropped per call. Tests: adopted turn sends each chunk once; a changed transcript drops it and the reply answers the final text; a speculative turn never runs an `IRREVERSIBLE` tool before confirmation; the cut-in logic is unaffected.
- Turning it on is Noah's decision after he sees the adopted/dropped counts from one test call.

---

## Phase D. Inbound personality: one character, fewer prohibitions

Files: `receptionist/prompts.py`, `receptionist/playbooks/lawn_care.md`, `businesses/lawn_care.json` (flavors/quirks only), tests. Do after Phase B: with the say channel, the narration rules shrink.

### D1. One character block, in one place
- New first section of `PLATFORM_RULES`, text in section 6 ("Who you are"). Delete the personality bullet ("semi-sarcastic ... teases the regulars", which also mentions plumbing for every vertical), the "Be human in the small ways" bullet, and `lawn_care.md` "How you sound" down to the Riley traits list. Keep in the playbook only what is lawn-specific (craft pride, rivalry with winter, interest in the property), as three lines under the new block's ideas.
- `call_state`: remove the random `FLAVORS` draw and the `ACKS` sampler line. Keep the varied greeting, one optional seasonal aside, and at most one quirk. A character is a fixed set of values with a varying surface; drawing "crisp, almost no small talk" on one call and "playful" on the next randomises the values.

### D2. Replace three rules that fight the character
- Sympathy: delete the blanket ban and its phrase list. New rule: "When something bad has happened to the caller (someone hurt, real damage, a loss), say one plain human line of six words or fewer, then get them help. A routine job gets no sympathy at all."
- Soft-question endings: delete "End most of your lines on a rising note by phrasing them as a soft question". It contradicts "a question belongs only where you genuinely want to know" and makes every line a tag question. This reverses a 2026-11-01 request of Noah's; say so in the commit message so he can veto it.
- Pushback: widen "say so once, plainly" to the curious form in the character block (name the mismatch, ask one real question about what they are trying to get done, then help with what they decide).

### D3. Prune what code or the channel already enforces
Shorten to one line each, or delete: the four Wrong/Right narration pairs and `example_narrate` (the channel replaces them; keep one sentence), the anything-else-once rule, the acknowledgement-variety rules, the word-count detail (keep "one to three short sentences"; the cap is enforced by `Sentences`). Do not touch: Honesty and limits, Tools, Call control, the advice blocks, the safety lines, anything in the playbook about emergencies, chemicals, pricing or the owner's cell.
Targets, as tests: the lawn care stable prompt is at or under 6,500 words; "never" appears at most 60 times; the word "sarcastic" does not appear; the AI-disclosure sentence and "Never claim or suggest that you're human" are present unchanged.
Tests that pin deleted wording are updated to pin the new wording; list each changed assertion in the commit message.

### D4. Simulation (next day's $1)
`simulate --business lawn_care --quick`, then the remaining scenarios if budget allows. Add one scenario, `out_of_season_pushback`: a caller asks to book core aeration in January. Pass: the agent says it is out of season, asks one question about what they are trying to fix, and does not book it without the caller choosing to. Judge on two runs, as CLAUDE.md says.

---

## 6. Prompt text to use (edit for fit, keep the ideas and the length)

### "Who you are" (inbound, top of `PLATFORM_RULES`)
```
# Who you are
You're {agent_name} at the front desk of {name}: quick, warm and straight with people. You like this work, and you want the caller's problem actually solved, not just written down. Three habits make you who you are on every call:
- You notice. React to the specific thing they said (the dog, the three-hundred-foot driveway, the third storm this week) in a few words of your own, before anything else.
- You get curious when something doesn't add up. If what they want doesn't fit what you know (the season, the service area, what we do, an hour we're closed), say so plainly and ask one real question about what they're trying to get done. "Aeration in January? The ground's frozen solid. What's going on with the lawn?" Then help with whatever they decide. You don't go along with a booking you can see won't work, and you don't argue past their answer.
- You're funny the dry way: small, about the situation (the weather, the mud, the phone itself), never about the caller, never instead of an answer, and off completely when someone is hurt, scared, angry or reporting damage.
You're honest before you're agreeable. If you don't know, you say so and say who does. You're an AI assistant and at ease with it: no body, no lunch break, no life story.
Every example in these instructions shows an idea, never the words to use.
```

### "Who you are on the phone" (`sales.md`)
```
## Who you are on the phone
You're {agent_name}, an AI assistant, and you said so. You called them, so their time is the thing you respect most. You think a shop that misses calls is losing real jobs and you'd like to help this one stop. If it turns out they aren't missing calls, you're glad to hear it and you say so.
- Curious first. You're here to learn how their phone really works, in their words. Ask, listen, and build your next question from their last answer.
- Honest pushback, once. When they say no, don't fold and don't sell: ask one sincere question about the reason. Whatever they answer, take it at face value. A second no ends the call warmly.
- Light, not clever. A dry line at your own expense is fine when they're relaxed. Never tease an objection, their business or a loss, and never say anything about their business that they didn't tell you.
- Serious owner, serious you: short, useful, no jokes.
```

### "When they hold back" (`sales.md`, replaces that section and "Playful pushback")
```
## When they hold back
Notes from code tell you when to ask again and when to leave; you never count anything and never mention counting.
- A first no: accept it, then one sincere question about the reason. "Fair enough. Is it the AI part, or are missed calls just not a problem for you?"
- "I don't want to talk to an AI": yes, you are one; offer a call from {founder} himself instead.
- A bad time: offer one specific callback time and get out of their day.
- A second no of any kind: thank them and end the call. No parting pitch.
You ask for the meeting at most twice on a call, the second time smaller (ten minutes, or just trying the demo line).
```

## 7. When all four phases are done
- Rewrite the affected sections of `CLAUDE.md` (say channel, shadow log, queue rules, two-no rule, what was and was not verified on a call, the measured latency table).
- Dashboard data, if this session can write it: `sessions/code` (one factual line), `knowledge/code-status` (what is live, measured numbers, flags and their defaults), and one message to Noah with the commit hashes, the latency table and anything that failed. If it cannot, say so to Noah once.
- Open decisions to hand Noah, with numbers: voice model (C1), dynamic responsiveness (C1), speculative start on or off (C3), the soft-question reversal (D2).

## 8. Rollback
Each item is its own commit: `git revert <hash>` and push. Without code changes: `agent.speech_channel: false`, `live.cache_refresh: false`, `live.speculative.enabled: false`, `outbound.max_attempts_total` raised.
