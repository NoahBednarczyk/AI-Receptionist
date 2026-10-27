"""Sign up a new shop: write businesses/<id>.json from a short intake and print the owner's one-page checklist.

    python -m receptionist new-client                 # asks the questions, with defaults in [brackets]
    python -m receptionist new-client --from intake.yaml

The shop starts from the Peak Heating & Plumbing demo (businesses/hvac.json): same agent, playbook, safety lines and
overflow mode; only the shop's own facts change. Nothing here calls the API, so it costs nothing and a new client's
file can be written, checked and corrected in a few minutes.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import prompts, safety, schedule, tools
from .speech import fictional_phone, normalize_phone

TEMPLATE_ID = "hvac"
DAYS = schedule.DAYS
DAY_NAMES = dict(zip(DAYS, ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]))
DEFAULT_HOURS = {"mon": "07:00-17:00", "tue": "07:00-17:00", "wed": "07:00-17:00", "thu": "07:00-17:00",
                 "fri": "07:00-17:00", "sat": "08:00-12:00", "sun": None}

# The questions, in the order they're asked. (key, question, default, help)
QUESTIONS = [
    ("shop_name", "Shop name (as it appears on the truck)", None, None),
    ("owner_cell", "Owner's cell number (the agent can try it for emergencies)", None, None),
    ("our_number", "The number the shop will forward to (the line we assign), or blank if not assigned yet", "", None),
    ("towns", "Towns you serve, comma separated", None, None),
    ("hours", "Hours, like 'weekdays 7-5, sat 8-12', or press Enter for Mon-Fri 7-5 and Sat 8-12", "", None),
    ("services", "Services you offer, comma separated, or Enter for everything in the demo list", "", None),
    ("prices", "Prices the agent may quote, one per line like 'Service visit: $95' (blank line to finish; none means it quotes nothing)", "", None),
    ("approved_tips", "Tips the agent may give, 'problem: tip' one per line (blank line to finish; none means no tips)", "", None),
    ("faqs", "Answers it may give, 'question: answer' one per line, like payment or warranty (blank line to finish; none is fine)", "", None),
]


class IntakeError(Exception):
    pass


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "shop"


def parse_hours(spec) -> dict:
    """Hours as a dict {mon: ["07:00", "17:00"] | None}. Accepts the YAML mapping (mon: "07:00-17:00"), or the text
    forms 'weekdays 7-5, sat 8-12'."""
    if not spec:
        spec = DEFAULT_HOURS
    if isinstance(spec, str):
        parsed: dict = {d: None for d in DAYS}
        for part in re.split(r"[;,]", spec.lower()):
            part = part.strip()
            if not part:
                continue
            m = re.match(r"(weekdays?|weekends?|daily|every ?day|[a-z]{3})[a-z]*\s+(\d{1,2})(?::(\d\d))?\s*(am|pm)?\s*-\s*(\d{1,2})(?::(\d\d))?\s*(am|pm)?$", part)
            if not m:
                raise IntakeError(f"Can't read the hours '{part}'. Use something like 'weekdays 7-5' or 'sat 8-12'.")
            word, h1, m1, ap1, h2, m2, ap2 = m.groups()
            days = (DAYS[:5] if word.startswith("weekday") else DAYS[5:] if word.startswith("weekend")
                    else DAYS if word in ("daily", "everyday", "every day") else [word[:3]])
            if days[0] not in DAYS:
                raise IntakeError(f"'{word}' isn't a day. Use mon, tue, wed, thu, fri, sat, sun or weekdays.")
            start, end = _clock(h1, m1, ap1, True), _clock(h2, m2, ap2 or ap1, False)
            for d in days:
                parsed[d] = [start, end]
        spec = parsed
    out = {}
    for d in DAYS:
        v = spec.get(d)
        if not v:
            out[d] = None
        elif isinstance(v, str):
            a, _, b = v.partition("-")
            out[d] = [_hhmm(a), _hhmm(b)]
        else:
            out[d] = [_hhmm(v[0]), _hhmm(v[1])]
        if out[d] and out[d][0] >= out[d][1]:
            raise IntakeError(f"{DAY_NAMES[d]}: closing time must be after opening time.")
    if not any(out.values()):
        raise IntakeError("The shop has to be open at least one day.")
    return out


def _hhmm(text: str) -> str:
    m = re.match(r"\s*(\d{1,2}):?(\d\d)?\s*$", str(text))
    if not m or int(m.group(1)) > 24:
        raise IntakeError(f"Can't read the time '{text}'; use 24-hour like 07:00-17:00.")
    return f"{int(m.group(1)):02d}:{m.group(2) or '00'}"


def _clock(h: str, m: str | None, ap: str | None, opening: bool) -> str:
    hour = int(h)
    if ap == "pm" and hour < 12:
        hour += 12
    elif ap == "am" and hour == 12:
        hour = 0
    elif not ap and not opening and hour < 12:   # '7-5' means 7 AM to 5 PM
        hour += 12
    return f"{hour:02d}:{m or '00'}"


def _lines(value) -> list[str]:
    if isinstance(value, str):
        return [x.strip() for x in re.split(r"[\n;]", value) if x.strip()]
    return [str(x).strip() for x in (value or []) if str(x).strip()]


def build(intake: dict, template: dict) -> dict:
    """The business JSON for this shop, from the intake answers and the demo template."""
    name = (intake.get("shop_name") or "").strip()
    if not name:
        raise IntakeError("The shop needs a name.")
    owner = normalize_phone(str(intake.get("owner_cell") or ""))
    if not owner:
        raise IntakeError("The owner's cell must be a full 10-digit number.")
    towns = [t.strip() for t in (intake["towns"].split(",") if isinstance(intake.get("towns"), str)
                                 else intake.get("towns") or []) if str(t).strip()]
    if not towns:
        raise IntakeError("List at least one town you serve.")
    ours = normalize_phone(str(intake.get("our_number") or "")) if intake.get("our_number") else None
    if intake.get("our_number") and not ours:
        raise IntakeError("The forwarding number must be a full 10-digit number, or left blank.")

    biz = json.loads(json.dumps(template))   # a private copy
    spoken = intake.get("spoken_name") or name
    biz.update(id=slug(name), name=name, spoken_name=spoken, agent_name=intake.get("agent_name") or "Sam",
               timezone=intake.get("timezone") or template["timezone"], phone_numbers=[ours] if ours else [],
               hours=parse_hours(intake.get("hours")), owner_sms=owner, texting_enabled=False)
    for k in ("demo_seed", "simulate_quick"):
        biz.pop(k, None)
    biz["service_area"] = {"towns": towns, "note": intake.get("service_area_note")
                           or f"Homes and small businesses in {towns[0]} and the towns around it."}
    wanted = _lines(intake.get("services"))
    if wanted:
        known = {s["id"]: s for s in template["services"]}
        unknown = [w for w in wanted if w not in known]
        if unknown:
            raise IntakeError(f"Unknown service(s) {', '.join(unknown)}. Choose from: {', '.join(known)}.")
        biz["services"] = [known[w] for w in wanted]
        if not any(s.get("emergency") for s in biz["services"]):   # winter emergencies always need an emergency visit
            biz["services"].append(known["emergency_visit"])
    # Only the shop's own facts: the demo shop's prices, warranty and so on must never reach a real shop's callers.
    biz["pricing"] = _lines(intake.get("prices")) + [
        "We don't quote repair or replacement prices over the phone. The technician gives a firm price on site "
        "before any work starts."]
    tips = []
    for line in _lines(intake.get("approved_tips")):
        problem, sep, tip = line.partition(":")
        if not sep or not tip.strip():
            raise IntakeError(f"Tip '{line}' should look like 'a furnace that won't start: check the thermostat is set to heat'.")
        tips.append({"when": problem.strip(), "tip": tip.strip()})
    biz["approved_tips"] = tips
    biz["speech_keywords"] = [w for w in template["speech_keywords"] if "peak" not in w.lower()] + [spoken]
    biz["transfer_targets"]["owner"]["number"] = owner
    biz["greeting"] = biz["greetings"][0].replace("Peak Heating and Plumbing", spoken)
    biz["greetings"] = [g.replace("Peak Heating and Plumbing", spoken) for g in biz["greetings"]]
    # Keep only the answers that describe how the agent itself works; everything else comes from the shop.
    keep = ("stove", "no heat")
    biz["faqs"] = [f for f in template["faqs"] if any(k in f["q"].lower() for k in keep)]
    for line in _lines(intake.get("faqs")):
        q, sep, a = line.partition(":")
        if not sep or not a.strip():
            raise IntakeError(f"FAQ '{line}' should look like 'Do you take cards: Yes, all major cards'.")
        biz["faqs"].append({"q": q.strip().rstrip("?") + "?", "a": a.strip()})
    return biz


def check(biz: dict) -> None:
    """Everything the live server does with a business, run once so a bad file is caught now, not on a call."""
    prompts.stable_prompt(biz)
    tools.schemas(biz)
    safety.match(biz["vertical"], "my furnace is out and it is freezing")
    schedule.spoken_hours(biz)
    assert schedule.open_windows(biz, schedule.local_now(biz), {}), "no booking windows fit inside the shop's hours"


def ask_interactively(input_fn=input, out=print) -> dict:
    out("A few questions about the shop. Press Enter to accept a [default].\n")
    intake: dict = {}
    for key, question, default, _ in QUESTIONS:
        if key in ("prices", "approved_tips", "faqs"):
            out(question)
            lines = []
            while True:
                line = input_fn("  > ").strip()
                if not line:
                    break
                lines.append(line)
            intake[key] = lines
            continue
        shown = f" [{default}]" if default else ""
        answer = input_fn(f"{question}{shown}: ").strip()
        intake[key] = answer or default
    return intake


def checklist(biz: dict, our_number: str | None) -> str:
    from .speech import spoken_phone
    ours = spoken_phone(our_number) if our_number else "<the number we assign you>"
    raw = re.sub(r"\D", "", our_number or "")[-10:] or "NUMBER"
    return f"""\
{biz['name']}: call-forwarding checklist
==============================================================
What this does: when nobody at the shop picks up (or the line is busy), the call goes to Sam, our virtual assistant.
Sam books the job, takes a message, or tries your cell for an emergency. You see every call on your dashboard.
You can turn this off in ten seconds, any time, with the code at the bottom.

BEFORE YOU START (2 minutes)
  [ ] Our number for your shop: {ours}. This is the number your calls forward to.
  [ ] Your carrier: _______________ (AT&T, T-Mobile, Verizon, or a business phone system)

TURN IT ON (5 minutes). Do this from the shop phone that customers call.
  AT&T and T-Mobile (standard codes; type them on the keypad and press call):
    [ ] No answer:   **61*{raw}**20#     (forwards after about 20 seconds of ringing)
    [ ] Busy:        **67*{raw}#
    [ ] No signal:   **62*{raw}#
  Verizon:
    [ ] No answer or busy: dial *71{raw}   (carrier codes change; if it doesn't work, use the My Verizon app)
  A business phone system, VoIP or landline (RingCentral, Ooma, a desk phone and so on):
    [ ] In the phone's admin page, set "forward when no answer / busy" to {ours}.
  If a code doesn't work, tell us your carrier and we'll find the right one. Don't guess.

TEST IT (5 minutes)
  [ ] From a different phone, call the shop. Don't answer. After about 20 seconds you should hear Sam.
  [ ] Ask Sam for a furnace repair; give any name. Then open your dashboard and find the call.
  [ ] Call again, and this time say "my furnace is out and it's 5 degrees". Sam should treat it as urgent.

WHAT TO TELL US (3 minutes)
  [ ] Your cell number on file for emergencies: {biz['transfer_targets']['owner']['number'][2:]}  (correct?)
  [ ] Anything Sam says that is wrong or you wouldn't say. Send it the same day; fixes take minutes.

TURN IT OFF (10 seconds), any time
  AT&T and T-Mobile: dial  ##004#   and press call.   (cancels all forwarding when you don't answer)
  Verizon:           dial  *73      and press call.
  Phone system:      switch "forward when no answer" back off in the admin page.
  Then test by calling the shop and letting it ring: you should hear your own voicemail again.

THE RULES OF THE PILOT
  - Sam tells callers it is the shop's virtual assistant whenever they ask. It never pretends to be a person.
  - Sam never gives advice about gas, electrical work, stoves or fireplaces, and never quotes a repair price.
  - You can stop at any time by turning forwarding off with the code above.
"""


def write(biz: dict, businesses_dir: Path, force: bool = False) -> Path:
    path = Path(businesses_dir) / f"{biz['id']}.json"
    if path.exists() and not force:
        raise IntakeError(f"{path.name} already exists. Choose a different shop name or pass --force.")
    path.write_text(json.dumps(biz, indent=2, ensure_ascii=False) + "\n")
    return path


def run(cfg: dict, intake: dict | None = None, force: bool = False, out=print) -> Path:
    template = json.loads((Path(cfg["businesses_dir"]) / f"{TEMPLATE_ID}.json").read_text())
    intake = intake or ask_interactively()
    biz = build(intake, template)
    check(biz)
    path = write(biz, cfg["businesses_dir"], force)
    sheet = checklist(biz, biz["phone_numbers"][0] if biz["phone_numbers"] else None)
    sheet_path = path.with_suffix(".checklist.txt")
    sheet_path.write_text(sheet)
    out(f"\nWrote {path}\nChecked it: the prompt, tools, safety lines and booking windows all load.")
    if fictional_phone(biz["transfer_targets"]["owner"]["number"]):
        out("Heads up: that owner cell is a 555-01xx placeholder, so live calls will never ring it.")
    if not biz["phone_numbers"]:
        out("No forwarding number yet. Assign one, add it to phone_numbers in the file, and regenerate the checklist.")
    out(f"Owner checklist (also saved to {sheet_path.name}):\n\n{sheet}")
    out("OUR SIDE: assign the shop its own number in Retell, point it at the existing agent, add the number to "
        f"phone_numbers in {path.name}, run the tests, push, and run `retell-tune --business {biz['id']}`. "
        "Then call it yourself once before the owner turns forwarding on.")
    return path
