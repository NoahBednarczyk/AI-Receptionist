"""Sign up a new shop: write <data dir>/clients/<id>.json from a short intake and print the owner's one-page checklist.

    python -m receptionist new-client                 # asks the questions, with defaults in [brackets]
    python -m receptionist new-client --from intake.yaml

The shop starts from a demo shop of its trade: Peak Heating & Plumbing (businesses/hvac.json, the default and the pilot
vertical) or Cedar Ridge Lawn & Snow (businesses/lawn_care.json); choose with `--template hvac|lawn_care` or a
`template:` line in the intake. Same agent, playbook, safety lines and overflow mode; only the shop's own facts change. Nothing here calls the API, so it costs nothing and a new client's
file can be written, checked and corrected in a few minutes.

Real shops are written to the clients folder (`cfg["clients_dir"]`, under the data directory and so never in git),
not to `businesses/`, which holds the demo shops in the public repo. A shop's owner cell and prices stay off GitHub.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import prompts, safety, schedule, tools
from .speech import fictional_phone, normalize_phone

DEFAULT_TEMPLATE = "hvac"   # the pilot vertical is emergency home trades (decided 2026-11-11)
TEMPLATE_ID = DEFAULT_TEMPLATE

# What differs between the trades: wording that must fit the shop's own work, and what is kept from the demo shop.
PROFILES = {
    "hvac": dict(
        agent_name="Sam",
        no_quote="We don't quote repair or replacement prices over the phone. The technician gives a firm price on site "
                 "before any work starts.",
        tip_example="a furnace that won't start: check the thermostat is set to heat",
        safety_probe="my furnace is out and it is freezing",
        keep_faqs=("stove", "no heat", "text confirmation", "is this call recorded"),
        brand_words=2,
        test_lines=("Ask {agent} for a furnace repair; give any name. Then open your dashboard and find the call.",
                    'Call again, and this time say "my furnace is out and it\'s 5 degrees". {agent} should treat it as urgent.'),
        rules="{agent} never gives advice about gas, electrical work, stoves or fireplaces, and never quotes a repair price.",
        ),
    "lawn_care": dict(
        agent_name="Riley",
        no_quote="We don't quote a price over the phone for a specific property. The crew lead gives a firm price on site "
                 "before any work starts.",
        tip_example="a sprinkler system that won't shut off: turn off the water at the main shutoff valve",
        safety_probe="a tree fell on the power line",
        keep_faqs=("text confirmation", "is this call recorded"),
        brand_words=2,
        test_lines=("Ask {agent} to book a lawn estimate; give any name. Then open your dashboard and find the call.",
                    'Call again, and this time say "a tree just fell across my driveway". {agent} should treat it as urgent.'),
        rules="{agent} never gives advice about chemicals, electrical work or equipment, and never quotes a price for a specific property.",
        ),
}


def profile_for(biz: dict) -> dict:
    return PROFILES.get(biz.get("vertical"), PROFILES[DEFAULT_TEMPLATE])
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
    ("about", "Tell us about the shop, one fact per line: its story, what you're known for, how a job goes (blank line to finish; the more the agent knows, the more natural it sounds)", "", None),
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
    prof = profile_for(template)
    spoken = intake.get("spoken_name") or name
    biz.update(id=slug(name), name=name, spoken_name=spoken, agent_name=intake.get("agent_name") or prof["agent_name"],
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
        if not any(s.get("emergency") for s in biz["services"]):   # storm and urgent calls always need an urgent visit
            biz["services"].append(next(s for s in template["services"] if s.get("emergency")))
    # Only the shop's own facts: the demo shop's prices, warranty and so on must never reach a real shop's callers.
    biz["pricing"] = _lines(intake.get("prices")) + [
        prof["no_quote"]]
    tips = []
    for line in _lines(intake.get("approved_tips")):
        problem, sep, tip = line.partition(":")
        if not sep or not tip.strip():
            raise IntakeError(f"Tip '{line}' should look like '{prof['tip_example']}'.")
        tips.append({"when": problem.strip(), "tip": tip.strip()})
    biz["approved_tips"] = tips
    biz["about"] = _lines(intake.get("about"))   # the demo shop's own story must never reach a real shop
    brand = " ".join(template["spoken_name"].split()[:prof["brand_words"]]).lower()   # the demo shop's own name never reaches a real shop
    biz["speech_keywords"] = [w for w in template["speech_keywords"] if w.lower() != brand] + [spoken]
    biz["transfer_targets"]["owner"]["number"] = owner
    biz["greetings"] = [g.replace(template["spoken_name"], spoken) for g in biz["greetings"]]
    biz["greeting"] = biz["greetings"][0]
    # The recording notice and AI disclosure are on for every new shop (the demo shops' own switches are not inherited).
    # (and they stay on: a real shop cannot ask for either to be off. Washington callers are all-party consent.)
    if "recording_notice" in intake and not str(intake["recording_notice"] or "").strip():
        raise IntakeError("A real shop's greeting always carries the recording notice; leave recording_notice out to use the standard one.")
    if intake.get("ai_disclosure_in_greeting") is False:
        raise IntakeError("A real shop's greeting always says it is the shop's virtual assistant; it cannot be turned off.")
    biz["recording_notice"] = str(intake.get("recording_notice") or prompts.DEFAULT_NOTICE).strip()
    biz["ai_disclosure_in_greeting"] = True
    biz["retention_days"] = int(intake.get("retention_days") or 90)
    biz["token_version"] = 1
    biz["calendars"] = []
    if intake.get("calendar_url"):
        url = str(intake["calendar_url"]).strip()
        if not re.match(r"^(https|webcal)://[^\s]+$", url, re.I):
            raise IntakeError("The calendar link must start with https:// (or webcal://).")
        biz["calendars"] = [{"type": "ical", "url": url, "label": intake.get("calendar_label") or "shop calendar"}]
    if intake.get("google_calendar_id"):   # the shop shares this calendar with our service account
        gid = str(intake["google_calendar_id"]).strip()
        if not re.match(r"^[^\s@]+@[^\s@]+$", gid):
            raise IntakeError("The Google calendar id looks like an email address (or xyz@group.calendar.google.com).")
        biz["calendars"].append({"type": "google", "calendar_id": gid,
                                 "label": intake.get("calendar_label") or "shop calendar", "write": True})
    for k in ("notice_waived", "demo"):   # a real shop is never a demo and never inherits the demo's waived notice
        biz.pop(k, None)
    # Keep only the answers that describe how the agent itself works; everything else comes from the shop.
    keep = prof["keep_faqs"]
    biz["faqs"] = [f for f in template["faqs"] if any(k in f["q"].lower() for k in keep)]
    for line in _lines(intake.get("faqs")):
        q, sep, a = line.partition(":")
        if not sep or not a.strip():
            raise IntakeError(f"FAQ '{line}' should look like 'Do you take cards: Yes, all major cards'.")
        biz["faqs"].append({"q": q.strip().rstrip("?") + "?", "a": a.strip()})
    return biz


def check(biz: dict) -> None:
    """Everything the live server does with a business, run once so a bad file is caught now, not on a call."""
    if not biz.get("demo") and not (biz.get("recording_notice") and biz.get("ai_disclosure_in_greeting")):
        # Demo shops may waive them (the live demo line); a real shop never does, whatever else its file says.
        raise IntakeError("A real shop's greeting must carry both the recording notice and the AI disclosure "
                          "(recording_notice set, ai_disclosure_in_greeting true).")
    prompts.stable_prompt(biz)
    tools.schemas(biz)
    safety.match(biz["vertical"], profile_for(biz)["safety_probe"])
    schedule.spoken_hours(biz)
    assert schedule.open_windows(biz, schedule.local_now(biz), {}), "no booking windows fit inside the shop's hours"


def check_unique(cfg: dict, biz: dict, replacing: bool = False) -> None:
    """The id and the phone numbers must not already belong to another shop, demo or real."""
    from .config import all_businesses
    for other in all_businesses(cfg):
        if other["id"] == biz["id"]:
            if replacing and other["id"] not in {Path(p).stem for p in Path(cfg["businesses_dir"]).glob("*.json")}:
                continue
            raise IntakeError(f"A shop with the id {biz['id']!r} already exists. Choose a different name or pass --force.")
        shared = {normalize_phone(n) for n in biz.get("phone_numbers", [])} & {normalize_phone(n) for n in other.get("phone_numbers", [])}
        if shared:
            raise IntakeError(f"That number already belongs to {other['name']}.")


def ask_interactively(input_fn=input, out=print) -> dict:
    out("A few questions about the shop. Press Enter to accept a [default].\n")
    intake: dict = {}
    for key, question, default, _ in QUESTIONS:
        if key in ("prices", "approved_tips", "faqs", "about"):
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
    prof = profile_for(biz)
    ours = spoken_phone(our_number) if our_number else "<the number we assign you>"
    raw = re.sub(r"\D", "", our_number or "")[-10:] or "NUMBER"
    return f"""\
{biz['name']}: call-forwarding checklist
==============================================================
What this does: when nobody at the shop picks up (or the line is busy), the call goes to {biz['agent_name']}, our virtual assistant.
{biz['agent_name']} books the job, takes a message, or tries your cell for an emergency. You see every call on your dashboard.
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
  [ ] From a different phone, call the shop. Don't answer. After about 20 seconds you should hear {biz['agent_name']}.
  [ ] {prof['test_lines'][0].replace('{agent}', biz['agent_name'])}
  [ ] {prof['test_lines'][1].replace('{agent}', biz['agent_name'])}

WHAT TO TELL US (3 minutes)
  [ ] Your cell number on file for emergencies: {biz['transfer_targets']['owner']['number'][2:]}  (correct?)
  [ ] Anything {biz['agent_name']} says that is wrong or you wouldn't say. Send it the same day; fixes take minutes.

TURN IT OFF (10 seconds), any time
  AT&T and T-Mobile: dial  ##004#   and press call.   (cancels all forwarding when you don't answer)
  Verizon:           dial  *73      and press call.
  Phone system:      switch "forward when no answer" back off in the admin page.
  Then test by calling the shop and letting it ring: you should hear your own voicemail again.

THE RULES OF THE PILOT
  - {biz['agent_name']} tells callers it is the shop's virtual assistant whenever they ask. It never pretends to be a person.
  - {prof['rules'].replace('{agent}', biz['agent_name'])}
  - You can stop at any time by turning forwarding off with the code above.
"""


def write(biz: dict, clients_dir: Path, force: bool = False) -> Path:
    Path(clients_dir).mkdir(parents=True, exist_ok=True)
    path = Path(clients_dir) / f"{biz['id']}.json"
    if path.exists() and not force:
        raise IntakeError(f"{path.name} already exists. Choose a different shop name or pass --force.")
    path.write_text(json.dumps(biz, indent=2, ensure_ascii=False) + "\n")
    return path


def run(cfg: dict, intake: dict | None = None, force: bool = False, out=print,
        template_id: str | None = None) -> Path:
    intake = intake or ask_interactively()
    template_id = template_id or intake.get("template") or DEFAULT_TEMPLATE
    if template_id not in PROFILES:
        raise IntakeError(f"Unknown template {template_id!r}. Choose from: {', '.join(PROFILES)}.")
    template = json.loads((Path(cfg["businesses_dir"]) / f"{template_id}.json").read_text())
    biz = build(intake, template)
    check(biz)
    check_unique(cfg, biz, force)
    path = write(biz, cfg["clients_dir"], force)
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
        f"phone_numbers in {path.name}, then `client-upload --business {biz['id']} --base-url <server>` to put the file "
        f"on the server (it is kept out of git on purpose), and run `retell-tune --business {biz['id']}`. "
        "Then call it yourself once before the owner turns forwarding on.")
    return path


ID_OK = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def accept_upload(cfg: dict, biz: dict) -> Path:
    """A client file sent to the server: it must load like any other shop and clash with none. Raises IntakeError."""
    if not isinstance(biz, dict) or not ID_OK.match(str(biz.get("id", ""))):
        raise IntakeError("the file needs an id of lowercase letters, digits and dashes")
    try:
        check(biz)
    except Exception as e:   # a bad file is the sender's mistake, whatever it tripped over
        raise IntakeError(f"the file does not load: {e}") from e
    check_unique(cfg, biz, replacing=True)
    return write(biz, cfg["clients_dir"], force=True)
