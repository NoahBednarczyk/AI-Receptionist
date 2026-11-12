"""Degraded mode: when the model can't answer (an API error, a timeout, an outage), the call is not dropped. Code takes
over as a plain message taker: name, a callback number read back and confirmed, one sentence about what they need,
saved as an urgent message, then a goodbye. No model is involved, so nothing here can fail the way the model did.

Emergency safety lines still run first (`CallSession.respond` checks them before anything else).
"""
from __future__ import annotations

import re

from .speech import _digit_runs, normalize_phone

OPENING = {
    "en": "Sorry, I'm having trouble on my end, but I can still take a message and get it to the office right away. "
          "What's your name?",
    "es": "Perdone, tengo un problema de mi lado, pero todavía puedo tomar un mensaje y hacérselo llegar a la oficina "
          "enseguida. ¿Cómo se llama?",
}
ASK_NUMBER = {"en": "Thanks, {name}. What's the best number to call you back on?",
              "es": "Gracias, {name}. ¿A qué número le podemos devolver la llamada?"}
SHORT_NUMBER = {"en": "I only caught {n} digits. Could you give me the whole number, area code first?",
                "es": "Solo alcancé {n} dígitos. ¿Me da el número completo, con el código de área?"}
READ_BACK = {"en": "I have {number}. Is that right?", "es": "Tengo el {number}. ¿Es correcto?"}
ASK_AGAIN = {"en": "Okay, let's try that number again.", "es": "Bien, repitamos el número."}
ASK_NEED = {"en": "Got it. In a sentence, what do you need from them?",
            "es": "Listo. En una frase, ¿qué necesita que le digan?"}
GOODBYE = {"en": "Thanks, I've passed that along as urgent, and someone will call you back as soon as they can. Bye now.",
           "es": "Gracias, ya lo pasé como urgente y alguien le devolverá la llamada lo antes posible. Hasta luego."}
SAVED_ALREADY = {"en": "I've got that saved, and the office will follow up. Thanks for calling.",
                 "es": "Ya quedó anotado y la oficina le dará seguimiento. Gracias por llamar."}
NO_NUMBER = {"en": "I still can't get a full number, so I've left the message without one. Thanks for calling.",
             "es": "No logro obtener el número completo, así que dejé el mensaje sin él. Gracias por llamar."}

YES = re.compile(r"\b(yes|yeah|yep|yup|correct|right|that's right|that is right|exactly|sure|sí|si|correcto|claro)\b", re.I)
NO = re.compile(r"\b(no|nope|wrong|not right|incorrect|nah|negative|incorrecto)\b", re.I)
LEAD_IN = re.compile(r"^(?:\W*(?:yes|yeah|yep|hi|hello|hey|uh|um|okay|ok|well|so|sure|sí|hola)\b[\s,.]*)+", re.I)
NAME_INTRO = re.compile(r"^(?:my name is|my name's|the name is|this is|it's|it is|i'm|i am|me llamo|soy|mi nombre es)\s+", re.I)
MAX_NUMBER_TRIES = 3


def phone_in(text: str) -> tuple[str | None, int]:
    """(E.164 number or None, longest run of digits heard). A full number is 10 digits, or 11 with a leading 1."""
    runs = [d for _, _, d in _digit_runs(text)]
    for d in runs:
        n = normalize_phone(d)
        if n:
            return n, len(d)
    return None, max((len(d) for d in runs), default=0)


def name_in(text: str) -> str:
    text = re.sub(r"[\d]+", " ", text)
    text = LEAD_IN.sub("", text.strip())
    text = NAME_INTRO.sub("", text)
    words = [w for w in re.split(r"[\s,]+", text) if w][:4]
    return " ".join(w.strip(".!?") for w in words).title()


def _digits_spoken(e164: str) -> str:
    d = e164[2:] if e164.startswith("+1") else e164.lstrip("+")
    return f"{d[:3]}, {d[3:6]}, {d[6:]}"   # commas give the voice a pause between the groups


class MessageTaker:
    """One caller's trip through the message-taking steps. `step` takes what the caller said and returns
    (what to say, whether to save the message and hang up now)."""

    def __init__(self, language: str = "en", caller_id: str | None = None):
        self.lang = language
        self.state = "start"
        self.name = ""
        self.number: str | None = None
        self.tries = 0
        self.need = ""
        self.last = ""   # the last thing asked, repeated after a safety line

    def _l(self, pool: dict) -> str:
        return pool.get(self.lang) or pool["en"]

    def step(self, caller_text: str) -> tuple[str, bool]:
        line, done = self._step(caller_text)
        self.last = line
        return line, done

    def _step(self, caller_text: str) -> tuple[str, bool]:
        text = (caller_text or "").strip()
        if self.state == "start":
            self.state = "name"
            return self._l(OPENING), False
        if self.state == "name":
            self.name = name_in(text) or "Unknown caller"
            number, _ = phone_in(text)   # some callers give the name and number together
            if number:
                self.number = number
                self.state = "confirm"
                return self._l(READ_BACK).format(number=_digits_spoken(number)), False
            self.state = "number"
            return self._l(ASK_NUMBER).format(name=self.name if self.name != "Unknown caller" else "").replace(" ,", ","), False
        if self.state == "number":
            number, heard = phone_in(text)
            if number:
                self.number, self.state = number, "confirm"
                return self._l(READ_BACK).format(number=_digits_spoken(number)), False
            self.tries += 1
            if self.tries >= MAX_NUMBER_TRIES:   # can't get one: keep the message anyway, it still tells the office who
                self.state = "need_nonumber"
                return self._l(ASK_NEED), False
            return self._l(SHORT_NUMBER).format(n=heard), False
        if self.state == "confirm":
            if NO.search(text) and not YES.search(text):
                self.number, self.state = None, "number"
                return self._l(ASK_AGAIN) + " " + self._l(ASK_NUMBER).format(name="").replace(" ,", ","), False
            number, _ = phone_in(text)
            if number and number != self.number and not YES.search(text):   # they simply said it again, corrected
                self.number = number
                return self._l(READ_BACK).format(number=_digits_spoken(number)), False
            self.state = "need"
            return self._l(ASK_NEED), False
        # need / need_nonumber: whatever they say is the message
        self.need = text[:300] or "(no details given)"
        return self._l(GOODBYE if self.number else NO_NUMBER), True

    def message(self) -> str:
        return (f"{self.need} [Taken by the automatic message line because the assistant had a technical problem; "
                "the call was not handled by the usual assistant.]")
