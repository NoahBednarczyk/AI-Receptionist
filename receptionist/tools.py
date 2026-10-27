"""The agent's tools: schemas the model sees, and the code that runs them.

Every tool is scoped to one business through ToolContext, and every call is logged to tool_events.
The voice platforms call the same run_tool() through the webhook server.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import schedule
from .speech import fictional_phone, normalize_phone, spoken_date, spoken_phone, spoken_time
from .store import Store


@dataclass
class ToolContext:
    biz: dict
    store: Store
    now: datetime
    caller: str | None
    call_id: str
    transfer_outcome: str = "connected"   # simulations set "no_answer" to test the fallback
    ended: bool = False
    end_reason: str | None = None
    transferred_to: str | None = None
    language: str = "en"                 # "es" once the caller speaks Spanish: fillers, spoken dates and texts follow
    safety_given: bool = False           # an emergency safety line was already spoken by code this call
    real_transfers: bool = False         # a live phone call: a transfer really dials the target's number
    log: list = field(default_factory=list)


# Lines the phone system plays the moment a tool call starts, so the caller doesn't wait in silence. They play
# only when the agent hasn't spoken yet that turn. create_lead (an internal note) and end_call have none.
FILLERS = {
    "en": {
        "check_availability": "Let me take a look at the schedule.",
        "book_appointment": "Okay, let me get that booked.",
        "lookup_customer": "Let me pull that up.",
        "take_message": "Let me write that down.",
        "reschedule_appointment": "Okay, let me move that for you.",
        "cancel_appointment": "Okay, let me cancel that for you.",
        "transfer_call": "Let me connect you now.",
    },
    "es": {
        "check_availability": "Un momento, reviso la agenda.",
        "book_appointment": "Un momento, lo reservo.",
        "lookup_customer": "Un momento, lo busco.",
        "take_message": "Déjeme anotar su mensaje.",
        "reschedule_appointment": "Un momento, lo cambio.",
        "cancel_appointment": "Un momento, lo cancelo.",
        "transfer_call": "Le comunico ahora.",
    },
}
# Spoken if the agent hangs up without having said anything this turn, so no call ever ends in silence,
# and a safety hang-up always carries the safety instruction.
END_LINES = {
    "en": {"safety": "Please leave the house now, don't touch any switches, and call 911 or your gas company from "
                     "outside. Once you're safe, you can call us back.",
           "spam": "Thanks for calling. Goodbye.",
           "completed": "Thanks for calling, take care. Bye."},
    "es": {"safety": "Por favor salga de la casa ahora, no toque ningún interruptor y llame al 911 o a su compañía "
                     "de gas desde afuera. Cuando esté a salvo, puede volver a llamarnos.",
           "spam": "Gracias por llamar. Adiós.",
           "completed": "Gracias por llamar. Adiós."},
}
STOP_EN = " Reply STOP to opt out."
STOP_ES = " Responda STOP para no recibir más mensajes."


def _text(ctx: ToolContext, to: str | None, body: str) -> None:
    """Queues a text, only while the business has texting on: anything queued meanwhile would all go out the day a
    sender is connected."""
    if ctx.biz.get("texting_enabled") and to:
        ctx.store.queue_sms(ctx.biz["id"], ctx.call_id, to, body, ctx.now)


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


# The model tends to write a spoken "note" before a tool call ("the number matches, so I'll take the message"). The phone
# system already speaks a short line while these tools run, so the strongest place to forbid the note is the tool itself.
NO_WORDS_FIRST = (" Call this without writing any words first, and never describe your checks: the phone system "
                  "speaks a short line while it runs.")
QUIET_TOOLS = {"check_availability", "book_appointment", "lookup_customer", "reschedule_appointment",
               "cancel_appointment", "take_message"}


def schemas(biz: dict) -> list[dict]:
    return [{**t, "description": t["description"] + NO_WORDS_FIRST} if t["name"] in QUIET_TOOLS else t
            for t in _schemas(biz)]


def _schemas(biz: dict) -> list[dict]:
    service_ids = [s["id"] for s in biz["services"]]
    clinic = schedule.in_clinic(biz)
    targets = list(biz["transfer_targets"])
    text = {"type": "string"}
    return [
        {
            "name": "check_availability",
            "description": ("Get open appointment times" if clinic else "Get open arrival windows") + " for a service. "
                           "Call before offering any time; never guess. "
                           "Returns up to 6 starting from earliest_date, or as soon as possible.",
            "input_schema": _obj({
                "service_id": {"type": "string", "enum": service_ids},
                "earliest_date": {"type": "string", "description": "YYYY-MM-DD; omit for the soonest windows."},
            }, ["service_id"]),
        },
        {
            "name": "book_appointment",
            "description": ("Book an appointment time from check_availability, only after the caller confirmed the "
                            "read-back of day, time and callback number. " if clinic else
                            "Book an arrival window from check_availability, only after the caller confirmed the "
                            "read-back of day and window, street address, town and callback number. ")
                           + ("Texts the caller a confirmation. " if biz.get("texting_enabled") else "")
                           + "Calling it again for the same service on the same call updates that booking (new notes or "
                           "window) instead of making a second one; a different service makes a second booking.",
            "input_schema": _obj({
                "window_id": {"type": "string", "description": "Exactly as returned by check_availability."},
                "service_id": {"type": "string", "enum": service_ids},
                "customer_name": text,
                "callback_number": {"type": "string", "description": "Full 10-digit number."},
                **({} if clinic else {"street_address": text, "town": text}),
                "problem_summary": {"type": "string", "description": "One sentence for the " + (
                    "provider: what the client wants to come in for." if clinic else "technician.")},
                "access_notes": {"type": "string", "description": (
                    "Anything the client wants the team to know. Omit if none." if clinic
                    else "Gate codes, pets, parking. Omit if none.")},
            }, ["window_id", "service_id", "customer_name", "callback_number",
                *([] if clinic else ["street_address", "town"]), "problem_summary"]),
        },
        {
            "name": "lookup_customer",
            "description": "Look up a customer and their upcoming appointments by phone number. "
                           "The record for the caller ID, if any, is already in the call details. For any other number, "
                           "pass the name the caller gives for the account; details come back only if it matches.",
            "input_schema": _obj({"phone": text, "name": {"type": "string", "description":
                                  "The name the caller gave for the account. Needed unless it's the caller ID."}},
                                 ["phone"]),
        },
        {
            "name": "reschedule_appointment",
            "description": "Move an existing appointment to a new window from check_availability, after the caller confirms "
                           "the new window. Only the customer on the appointment can change it: the caller ID or the "
                           "callback number they give must match the number on file.",
            "input_schema": _obj({
                "confirmation": {"type": "string", "description": "From lookup_customer or the caller, like A0901."},
                "new_window_id": {"type": "string", "description": "Exactly as returned by check_availability."},
                "callback_number": {"type": "string", "description": "Only if the caller gave a number other than the caller ID."},
            }, ["confirmation", "new_window_id"]),
        },
        {
            "name": "cancel_appointment",
            "description": "Cancel an existing appointment after the caller confirms which one. Same number check as "
                           "reschedule_appointment.",
            "input_schema": _obj({
                "confirmation": text,
                "reason": text,
                "callback_number": {"type": "string", "description": "Only if the caller gave a number other than the caller ID."},
            }, ["confirmation", "reason"]),
        },
        {
            "name": "create_lead",
            "description": "Save a caller with a service need who didn't book, so the office follows up.",
            "input_schema": _obj({
                "customer_name": text,
                "callback_number": text,
                "street_address": text,
                "town": text,
                "problem_summary": text,
                "urgency": {"type": "string", "enum": ["emergency", "this_week", "flexible"]},
                "reason_not_booked": text,
            }, ["customer_name", "callback_number", "problem_summary", "urgency", "reason_not_booked"]),
        },
        {
            "name": "take_message",
            "description": "Record a message for the office, for example when a transfer doesn't connect, "
                           "for billing questions, or for anything you can't handle. Needs the caller's full 10-digit "
                           "callback number: use the one the caller already gave or confirmed this call without asking again; only if you have none, ask for it before calling this.",
            "input_schema": _obj({
                "caller_name": text,
                "callback_number": text,
                "message": text,
                "urgent": {"type": "boolean"},
            }, ["caller_name", "callback_number", "message", "urgent"]),
        },
        {
            "name": "transfer_call",
            "description": "Warm-transfer the caller to a person. Only to a listed target, and only when its rules allow.",
            "input_schema": _obj({
                "target": {"type": "string", "enum": targets},
                "reason": {"type": "string", "description": "One sentence for the person picking up."},
            }, ["target", "reason"]),
        },
        {
            "name": "end_call",
            "description": "Hang up after your goodbye. Use reason spam for robocalls and sales pitches, "
                           "and safety when the caller must leave the house now.",
            "input_schema": _obj({"reason": {"type": "string", "enum": ["completed", "spam", "safety"]}}, ["reason"]),
        },
    ]


class ToolError(Exception):
    pass


def _service(biz: dict, service_id: str) -> dict:
    for s in biz["services"]:
        if s["id"] == service_id:
            return s
    raise ToolError(f"Unknown service_id {service_id!r}.")


def _phone(raw: str) -> str:
    n = normalize_phone(raw)
    if not n:
        raise ToolError("That callback number isn't a full 10-digit number. Ask the caller for it again.")
    return n


def _town(biz: dict, raw: str) -> str:
    # Speech recognition writes "Sand Point" or "Sandpoint, Idaho": compare letters only, without a trailing state.
    def key(name: str) -> str:
        return re.sub(r"(idaho|id|washington|wa|montana|mt)$", "", re.sub(r"[^a-z]", "", name.lower())) or name.lower()
    for t in biz["service_area"]["towns"]:
        if key(raw) == key(t):
            return t
    raise ToolError(f"{raw} is outside the service area. Don't book; tell the caller politely that we don't cover it.")


def check_availability(ctx: ToolContext, a: dict) -> dict:
    service = _service(ctx.biz, a["service_id"])
    earliest = None
    if a.get("earliest_date"):
        try:
            earliest = date.fromisoformat(a["earliest_date"])
        except ValueError:
            raise ToolError(f"earliest_date must look like {ctx.now.date().isoformat()}.")
        last = ctx.now.date() + timedelta(days=ctx.biz["booking"]["days_ahead"])
        if earliest < ctx.now.date():
            raise ToolError(f"earliest_date {earliest} is in the past; today is {ctx.now.date().isoformat()}. "
                            "Check the year and try again.")
        if earliest > last:
            raise ToolError(f"Bookings open only through {last.isoformat()}. Offer to take a message for later dates.")
    wins = schedule.open_windows(ctx.biz, ctx.now, ctx.store.booked_counts(ctx.biz["id"]), earliest,
                                 **schedule.emergency_rules(ctx.biz, service))
    if not wins:
        return {"windows": [], "note": "Nothing open in the booking range. Take a message so the office can fit them in."}
    return {
        "windows": [{"window_id": w["window_id"],
                     "spoken": schedule.spoken_window(ctx.biz, w["window_id"], ctx.now, ctx.language)} for w in wins],
        "note": "Soonest first. Offer the caller at most two of these.",
    }


def _sent_note(ctx: ToolContext, what: str) -> str:
    """What to tell the caller about a text, so the agent never promises one that isn't sent."""
    if ctx.biz.get("texting_enabled"):
        return f"A {what} text is on its way."
    return ("No confirmation text goes out, so give the caller the confirmation number to write down. Don't bring "
            "up texts unless the caller asks"
            + ("." if schedule.in_clinic(ctx.biz) else "; the technician still calls or texts before arriving."))

def book_appointment(ctx: ToolContext, a: dict) -> dict:
    biz, bid = ctx.biz, ctx.biz["id"]
    wid = a["window_id"]
    service = _service(biz, a["service_id"])
    clinic = schedule.in_clinic(biz)
    street = None if clinic else a["street_address"]
    town = None if clinic else _town(biz, a["town"])
    phone = _phone(a["callback_number"])
    name = a["customer_name"].strip()
    if not name:
        raise ToolError("Ask for the caller's name before booking.")
    # Booking the same service again on this call updates that booking; it never double-books the caller.
    earlier = next((x for x in ctx.store.appointments_for_call(ctx.call_id)
                    if x["phone"] == phone and x["status"] == "booked" and x["service_id"] == service["id"]), None)
    keeps_own_window = earlier is not None and wid == earlier["window_id"]   # full now only because of this booking
    open_ids = {w["window_id"] for w in schedule.open_windows(biz, ctx.now, ctx.store.booked_counts(bid), limit=10_000,
                                                              **schedule.emergency_rules(biz, service))}
    if wid not in open_ids and not keeps_own_window:
        raise ToolError("That window isn't available. Call check_availability again and offer a current window.")
    if earlier:
        ctx.store.update_appointment(earlier["id"], wid, service["id"], street, town,
                                     a["problem_summary"], a.get("access_notes"))
        spoken = schedule.spoken_window(biz, wid, ctx.now, ctx.language)
        moved = wid != earlier["window_id"]
        if moved:
            _text(ctx, phone, _moved_text(ctx, earlier["confirmation"], spoken))
        return {"status": "moved" if moved else "updated", "confirmation": earlier["confirmation"], "window": spoken,
                "note": "Same booking and confirmation number, details updated."}

    confirmation = ctx.store.add_appointment(bid, ctx.call_id, wid, service["id"], name, phone, street,
                                             town, a["problem_summary"], a.get("access_notes"), ctx.now)
    ctx.store.upsert_customer(bid, phone, name, street, town)
    spoken = schedule.spoken_window(biz, wid, ctx.now, ctx.language)
    if ctx.language == "es":
        body = (f"{biz['name']}: su cita está reservada para {spoken}. Confirmación {confirmation}. "
                + (biz["booking"].get("arrival_note_es") or "El técnico le enviará un mensaje unos 30 minutos antes de "
                                                            "llegar.") + STOP_ES)
    else:
        body = (f"{biz['name']}: you're booked for {service['name'].lower()}, {spoken}. Confirmation {confirmation}. "
                + (biz["booking"].get("arrival_note") or "Your technician will text about 30 minutes before arriving.")
                + STOP_EN)
    _text(ctx, phone, body)
    _text(ctx, biz["owner_sms"], f"New booking {confirmation}: {name}, {spoken_phone(phone)}, "
                                 + (f"{street}, {town}. " if street else "") + f"{service['name']}: {a['problem_summary']} "
                                 f"Window: {schedule.spoken_window(biz, wid, ctx.now)}.")
    return {"status": "booked", "confirmation": confirmation, "window": spoken, "note": _sent_note(ctx, "confirmation")}


def _same_person(given: str | None, on_file: str | None) -> bool:
    names = set(re.findall(r"[a-z]{2,}", (on_file or "").lower()))
    return bool(names & set(re.findall(r"[a-z]{2,}", (given or "").lower())))


def lookup_customer(ctx: ToolContext, a: dict) -> dict:
    phone = _phone(a["phone"])
    customer = ctx.store.get_customer(ctx.biz["id"], phone)
    if not customer:
        return {"found": False}
    if phone != normalize_phone(ctx.caller) and not _same_person(a.get("name"), customer["name"]):
        # Anyone can say a phone number; a stranger mustn't learn a customer's name, address or appointments from it.
        return {"found": True, "verified": False,
                "note": "There's an account for that number, but the caller isn't calling from it. Ask for the name on "
                        "the account and look it up again with it. Until it matches, don't say whose account it is or "
                        "share anything from it."}
    upcoming = ctx.store.upcoming_appointments(ctx.biz["id"], phone, ctx.now)
    return {
        "found": True, "name": customer["name"], "address": customer["address"], "town": customer["town"],
        "notes": customer["notes"],
        "upcoming": [{"confirmation": x["confirmation"], "service_id": x["service_id"],
                      "window": schedule.spoken_window(ctx.biz, x["window_id"], ctx.now, ctx.language)} for x in upcoming],
    }


def _moved_text(ctx: ToolContext, confirmation: str, spoken: str) -> str:
    if ctx.language == "es":
        return f"{ctx.biz['name']}: su cita {confirmation} se cambió a {spoken}." + STOP_ES
    return f"{ctx.biz['name']}: your visit {confirmation} moved to {spoken}." + STOP_EN


def _callers_appointment(ctx: ToolContext, a: dict) -> dict:
    """The booked appointment, but only for the customer on it: caller ID or a number they give must match."""
    appt = ctx.store.get_appointment(ctx.biz["id"], a["confirmation"])
    if not appt or appt["status"] != "booked":
        raise ToolError("No booked appointment has that confirmation number. Look the caller up by phone to find it.")
    numbers = {normalize_phone(ctx.caller), normalize_phone(a.get("callback_number"))} - {None}
    if appt["phone"] not in numbers:
        raise ToolError("This caller's number doesn't match the one on that appointment, so you can't change it. "
                        "Don't share its details; offer to take a message for the office.")
    return appt


def reschedule_appointment(ctx: ToolContext, a: dict) -> dict:
    appt = _callers_appointment(ctx, a)
    wid = a["new_window_id"]
    open_ids = {w["window_id"] for w in schedule.open_windows(ctx.biz, ctx.now, ctx.store.booked_counts(ctx.biz["id"]),
                                                              limit=10_000)}
    if wid == appt["window_id"]:
        raise ToolError("That's the window the appointment already has.")
    if wid not in open_ids:
        raise ToolError("That window isn't available. Call check_availability again and offer a current window.")
    ctx.store.set_appointment(appt["id"], window_id=wid)
    spoken = schedule.spoken_window(ctx.biz, wid, ctx.now, ctx.language)
    _text(ctx, appt["phone"], _moved_text(ctx, appt["confirmation"], spoken))
    _text(ctx, ctx.biz["owner_sms"],
          f"Rescheduled {appt['confirmation']} ({appt['name']}): now {schedule.spoken_window(ctx.biz, wid, ctx.now)}.")
    return {"status": "moved", "confirmation": appt["confirmation"], "window": spoken, "note": _sent_note(ctx, "rescheduling")}


def cancel_appointment(ctx: ToolContext, a: dict) -> dict:
    appt = _callers_appointment(ctx, a)
    ctx.store.set_appointment(appt["id"], status="cancelled")
    body = (f"{ctx.biz['name']}: su cita {appt['confirmation']} fue cancelada." + STOP_ES if ctx.language == "es"
            else f"{ctx.biz['name']}: your visit {appt['confirmation']} is cancelled." + STOP_EN)
    _text(ctx, appt["phone"], body)
    _text(ctx, ctx.biz["owner_sms"], f"Cancelled {appt['confirmation']} ({appt['name']}): {a['reason']}")
    return {"status": "cancelled", "confirmation": appt["confirmation"], "note": _sent_note(ctx, "cancellation")}


def create_lead(ctx: ToolContext, a: dict) -> dict:
    bid = ctx.biz["id"]
    address = ", ".join(x for x in (a.get("street_address"), a.get("town")) if x)
    ctx.store.add_lead(bid, ctx.call_id, a["customer_name"], a["callback_number"], address, a["problem_summary"],
                       a["urgency"], a["reason_not_booked"], ctx.now)
    _text(ctx, ctx.biz["owner_sms"], f"New lead ({a['urgency']}): {a['customer_name']}, {a['callback_number']}. "
                                     f"{a['problem_summary']} Not booked: {a['reason_not_booked']}")
    return {"status": "saved", "note": "The office will follow up during business hours."}


def take_message(ctx: ToolContext, a: dict) -> dict:
    bid = ctx.biz["id"]
    phone = _phone(a["callback_number"])   # a message nobody can return is worthless, urgent ones most of all
    ctx.store.add_message(bid, ctx.call_id, a["caller_name"], phone, a["message"], "office", a["urgent"], ctx.now)
    _text(ctx, ctx.biz["owner_sms"], f"{'URGENT ' if a['urgent'] else ''}Message from {a['caller_name']}, "
                                     f"{spoken_phone(phone)}: {a['message']}")
    if a["urgent"]:
        promise = f"within {ctx.biz['emergency_callback_minutes']} minutes"
    elif schedule.is_open(ctx.biz, ctx.now):
        promise = "later today, during business hours"
    else:
        opens = schedule.next_opening(ctx.biz, ctx.now)
        promise = (f"the office is closed right now; someone will call back after it opens "
                   f"{spoken_date(opens.date(), ctx.now.date())} at {spoken_time(opens.strftime('%H:%M'))}"
                   if opens else "the office is closed right now; someone will call back when it reopens")
    return {"status": "saved", "callback": promise}


def transfer_call(ctx: ToolContext, a: dict) -> dict:
    target = ctx.biz["transfer_targets"].get(a["target"])
    if not target:
        raise ToolError(f"Unknown transfer target {a['target']!r}.")
    open_now = schedule.is_open(ctx.biz, ctx.now)
    # "available" is "open" (business hours only), "closed" (after hours only) or "always".
    available = target.get("available") or {"office": "open", "on_call": "closed"}.get(a["target"], "always")
    if available == "open" and not open_now:
        raise ToolError("The office is closed. For an emergency transfer to on_call; otherwise take a message."
                        if a["target"] == "office" else
                        f"{target['label']} is reachable only during business hours. Take a message instead.")
    if available == "closed" and open_now:
        raise ToolError("During business hours, transfer emergencies to office."
                        if a["target"] == "on_call" else
                        f"{target['label']} is reachable only after hours. Use another target or take a message.")
    # A live call can't ring a placeholder number (555-01xx); treat it as nobody picking up.
    if ctx.transfer_outcome != "connected" or (ctx.real_transfers and fictional_phone(target["number"])):
        return {"status": "no_answer", "note": "Nobody picked up. Tell the caller exactly that, in plain words and without "
                                               "apologizing (\"The office isn't picking up.\"). Then take a message "
                                               "(urgent if it's an emergency) and tell the caller when to expect a callback."}
    ctx.ended, ctx.end_reason, ctx.transferred_to = True, "transferred", a["target"]
    return {"status": "connected", "note": f"The caller is now connected to {target['label']}."}


def end_call(ctx: ToolContext, a: dict) -> dict:
    ctx.ended, ctx.end_reason = True, a["reason"]
    return {"status": "ended"}


HANDLERS = {f.__name__: f for f in (check_availability, book_appointment, lookup_customer, reschedule_appointment,
                                    cancel_appointment, create_lead, take_message, transfer_call, end_call)}


def run_tool(ctx: ToolContext, name: str, args: dict) -> tuple[dict, bool]:
    """Returns (result, is_error). Errors are written for the model: they say what to do next."""
    handler = HANDLERS.get(name)
    try:
        if not handler:
            raise ToolError(f"No tool named {name}.")
        result, is_error = handler(ctx, args), False
    except ToolError as e:
        result, is_error = {"error": str(e)}, True
    except (KeyError, TypeError, AttributeError) as e:
        result, is_error = {"error": f"Missing or malformed argument ({e}). Check the tool's fields and try again."}, True
    ctx.store.log_tool(ctx.call_id, name, args, result, ctx.now)
    ctx.log.append({"name": name, "args": args, "result": result, "error": is_error})
    return result, is_error
