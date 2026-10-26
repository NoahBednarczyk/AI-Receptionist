"""Phone numbers, times and dates in the form a text-to-speech voice reads naturally."""
from __future__ import annotations

import re
from datetime import date


# Words the caller should never hear: names of tools and prompt sections, markup.
SPOKEN_INTERNALS = re.compile(r"\b(safety (line|step)s?|playbook|business facts|read-?back|check_availability|book_appointment|"
                              r"lookup_customer|create_lead|take_message|transfer_call|end_call)\b|\bstart with:|<[a-z/]", re.I)
# A sentence that starts like the model thinking out loud ("Wait, I should just ask...", "The caller wants...").
SELF_TALK = re.compile(r"^(?:wait\b|hmm+\b|let me (?:think|rethink|reconsider)\b|i should\b|i need to (?:ask|check|find out|"
                       r"figure|first|follow)\b|(?:actually|okay|ok|so),? (?:i should|i need to|let me (?:think|rethink))\b|"
                       r"the caller\b|this caller\b|(?:they|he|she) (?:asked|are asking|is asking|want|wants|wanted|said|are declining|"
                       r"is declining|declined|don't want|doesn't want|seem|seems) |this is an emergency\b|per the\b|according to (?:the|my)\b|"
                       r"based on (?:the|my) (?:rules|instructions|playbook)\b|(?:it's|it is) business hours, so\b|"
                       r"the office is open(?: right)? now, so I (?:can|should|will) (?:connect|transfer)\b)", re.I)


# Reasoning that can sit anywhere in a sentence ("...so that's the closest match", "...from them").
NARRATION = re.compile(r"\b(?:closest match|better (?:fit|match)|available services|from them|i shouldn't have said|"
                       r"intake|since i don't have (?:one|it|a number|your number)|(?:save|create|log|record) (?:a|the) lead|(?:the|your) (?:cost|price|pricing) question|"
                       r"(?:since|because) (?:they|he|she) (?:asked|want|wanted|said|are|is|declined)|"
                       r"i'll (?:book|put|file|log) (?:it|this|that) (?:as|under) (?:a |an )?(?:leak|drain|fixture|"
                       r"water heater|furnace|repair|install\w*|tune-up|estimate|service))\b|"
                       # a reason, then the step it leads to: "Intake comes first, so I'll get your address"
                       # (a short "Got it, so I'll..." is ordinary speech)
                       r"^\S+(?: \S+){2,}, so i'll (?:check|look|get|use|ask|switch|try|start|need)\b|"
                       r"^(?:i (?:used|picked|chose|went with)|i(?:'ll| will) ask (?:for|about)|no caller id)\b", re.I)


def unspeakable(sentence: str) -> bool:
    """Thinking out loud or naming internals: dropped before the caller hears it."""
    s = sentence.strip()
    return bool(SELF_TALK.match(s) or NARRATION.search(s) or SPOKEN_INTERNALS.search(s))

def normalize_phone(raw: str | None) -> str | None:
    """US numbers to E.164 (+13125550142). Anything with fewer than 10 digits is not a callback number."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if raw.strip().startswith("+") and len(digits) > 10:
        return "+" + digits
    return None


def fictional_phone(raw: str | None) -> bool:
    """555-0100 to 555-0199: reserved for fiction, so a placeholder that can never ring anyone."""
    n = normalize_phone(raw)
    return bool(n) and n.startswith("+1") and n[5:8] == "555" and n[8:10] == "01"


def spoken_phone(e164: str | None) -> str:
    n = normalize_phone(e164)
    if not n or not n.startswith("+1"):
        return e164 or "unknown"
    d = n[2:]
    return f"{d[:3]}-{d[3:6]}-{d[6:]}"


def spoken_time(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    if h == 12 and m == 0:
        return "noon"
    if h == 0 and m == 0:
        return "midnight"
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12} {suffix}" if m == 0 else f"{h12}:{m:02d} {suffix}"


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]


def spoken_time_es(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    if h == 12 and m == 0:
        return "el mediodía"
    period = "de la mañana" if h < 12 else ("de la tarde" if h < 19 else "de la noche")
    h12 = h % 12 or 12
    return f"{'la' if h12 == 1 else 'las'} {h12 if m == 0 else f'{h12}:{m:02d}'} {period}"


def spoken_date_es(d: date, today: date | None = None) -> str:
    full = f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}"
    if today is not None and d == today:
        return f"hoy, {full}"
    if today is not None and (d - today).days == 1:
        return f"mañana, {full}"
    return full


SPANISH_WORDS = {"hola", "gracias", "por", "favor", "necesito", "quiero", "tengo", "agua", "casa", "cita", "buenos",
                 "buenas", "días", "tardes", "usted", "sí", "está", "puede", "ayuda", "español", "fuga", "baño",
                 "cocina", "calefacción", "qué", "cómo", "dónde", "cuándo", "mi", "una", "tubería", "fregadero",
                 "lavabo", "llamo", "señor", "señora", "habla"}


def looks_spanish(text: str) -> bool:
    if any(ch in text for ch in "¿¡ñ"):
        return True
    words = re.findall(r"[a-záéíóúü]+", text.lower())
    return sum(w in SPANISH_WORDS for w in words) >= 2


DIGIT_WORDS = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
               "six": "6", "seven": "7", "eight": "8", "nine": "9", "cero": "0", "uno": "1", "dos": "2", "tres": "3",
               "cuatro": "4", "cinco": "5", "seis": "6", "siete": "7", "ocho": "8", "nueve": "9"}


CARD_WORDS = re.compile(r"\b(?:card|credit|debit|visa|mastercard|amex|tarjeta|cr[eé]dito|d[eé]bito)\b", re.I)


def _digit_runs(text: str) -> list[tuple[int, int, str]]:
    """(start, end, digits) for each run of digits in the text, spoken ("three one two") or written, allowing
    commas, dashes and pauses in between."""
    runs, start, end, digits = [], None, None, ""
    for m in re.finditer(r"\d+|[a-záéíóú]+|[^\s\w]", text.lower()):
        tok = m.group()
        if tok.isdigit() or tok in DIGIT_WORDS:
            start = m.start() if start is None else start
            end, digits = m.end(), digits + (tok if tok.isdigit() else DIGIT_WORDS[tok])
        elif tok in {",", "-", ".", "dash"} and start is not None:
            continue
        elif start is not None:
            runs.append((start, end, digits))
            start, digits = None, ""
    if start is not None:
        runs.append((start, end, digits))
    return runs


def redact_card(text: str) -> str:
    """Card numbers never reach the model, the transcript or the database: when the caller talks about a card,
    any run of 8 or more digits that isn't a 10-digit phone number is replaced."""
    if not CARD_WORDS.search(text):
        return text
    out, last = [], 0
    for start, end, digits in _digit_runs(text):
        if len(digits) >= 8 and len(digits) != 10:
            out += [text[last:start], "[card number removed]"]
            last = end
    return "".join(out) + text[last:]


def short_phone_numbers(text: str) -> list[str]:
    """Digit runs of 7 to 9 in what the caller said, counting spoken digits ("three one two, five five five...").
    A run of that length is almost always a phone number with a digit missing, unless they're reading a card."""
    if CARD_WORDS.search(text):
        return []
    runs, current = [], ""
    for token in re.findall(r"\d+|[a-záéíóú]+|[^\s\w]", text.lower()):
        if token.isdigit():
            current += token
        elif token in DIGIT_WORDS:
            current += DIGIT_WORDS[token]
        elif token in {",", "-", ".", "dash"}:
            continue
        else:
            if current:
                runs.append(current)
            current = ""
    if current:
        runs.append(current)
    if any(len(r) in (10, 11) for r in runs):   # a correction in the same breath: "oh one four... uh, four two? no,
        return []                              # wait, three one two, five five five, oh one four two"
    return [r for r in runs if 7 <= len(r) <= 9]


def spoken_date(d: date, today: date | None = None) -> str:
    full = f"{d.strftime('%A')}, {d.strftime('%B')} {ordinal(d.day)}"
    if today is not None:
        if d == today:
            return f"today, {full}"
        if (d - today).days == 1:
            return f"tomorrow, {full}"
    return full
