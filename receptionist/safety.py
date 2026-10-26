"""Emergency safety lines, spoken by code the moment a caller describes the emergency.

The model never has to produce them, so they come out instantly, word for word, and it can't narrate them
("Burst pipe, so I'll give the safety line first"), which it does without thinking. The lines match the
playbook's; the model is told they were said and carries on with the rest of the emergency flow. For gas or
carbon monoxide with the caller still inside, code says the whole turn and hangs up: no model call at all.

Phrasings these patterns miss still reach the model, which has the same lines in its playbook.
"""
from __future__ import annotations

import re

NEGATIONS = {"no", "not", "don't", "dont", "doesn't", "doesnt", "never", "isn't", "isnt", "without", "haven't",
             "havent", "didn't", "didnt", "can't", "cant", "won't", "wont", "nunca", "ni", "sin", "tampoco"}
# A negation only reaches across to the next clause: "I don't know, I smell gas" is a gas smell.
CLAUSE_BREAKS = {",", "but", "though", "although", "pero", "aunque", "sino"}

# (name, trigger patterns, safety line) for the home services vertical; first match wins, so gas and CO come first.
# Patterns run on one sentence at a time, lowercased, with punctuation other than apostrophes removed.
GAP = r"(?: \S+){0,3}"   # a few words in between: "pipe just burst", "pipe under the sink broke"
HOME_SERVICES = [
    ("gas", [rf"smell{GAP} gas\b", r"smells? like gas\b", r"\bgas (?:smell|leak|odor)", r"rotten eggs?"],
     "Please leave the house now, don't touch any switches, and once you're outside, call 911 or the gas company."),
    ("carbon_monoxide", [r"^(?!.*\b(?:battery|batteries|chirp\w*)\b).*\b(?:carbon monoxide|monoxide|co) (?:alarm|detector)s?"
                         rf"{GAP} (?:going off|went off|sounding|alarming|beeping)",
                         r"carbon monoxide (?:leak|poisoning)"],
     "Please get everyone out of the house now and call 911 from outside."),
    ("flooding", [rf"\bpipes?{GAP} (?:burst|broke|exploded|split|busted)",
                  r"\b(?:burst|broken|busted|cracked) (?:water )?pipes?\b",
                  r"^(?!.*\b(?:stains?|spots?|damage|bill|pressure)\b).*\bwater\S*(?: \S+){0,6} "
                  r"(?:spraying|all over|everywhere|spreading|pouring|gushing)",
                  r"\b(?:pouring|gushing|spraying) (?:out|water|everywhere|from)",
                  r"(?:'s|\bis|\bare|\bkeeps|\bstarted|\bstarting|\bgetting) flooding",
                  r"(?:'s|\bis|\bare|\bgot|\bgetting) flooded",
                  r"can't stop the water",
                  r"^(?=.*\b(?:water(?! heater| softener| bill| pressure)|leak\w*)\b)"
                  r"(?!.*\b(?:toilet|faucet|tap|drip\w*|running)\b)"
                  r".*\b(?:can't|cannot|won't) (?:get (?:it|the water) to |make (?:it|the water) )?stop(?: it)?$"],
     "If it's safe, shut off the main water valve, usually near the water meter, and stay away from anything "
     "electrical near the water."),
    ("sewage", [r"\bsewage(?: \S+){0,4} (?:back\w* up|coming|everywhere|all over|overflow\w*)",
                r"\b(?:back\w* up|overflow\w*)(?: \S+){0,4} sewage", r"\bsewer(?: \S+){0,2} back\w* up"],
     "Please stop using the sinks, toilets and showers until a technician has looked at it."),
    ("sparks", [r"\bsparking\b", r"\bsparks\b", r"\bsmoke (?:is )?coming", r"\bburning smell",
                r"smells? like (?:something )?burning", r"\bon fire\b"],
     "If it's safe, turn off the power or gas to the unit and keep everyone away from it."),
]
# The same emergencies for Spanish-speaking callers, with the playbook's lines translated.
HOME_SERVICES_ES = [
    ("gas", [r"(?:huele|olor|oler)(?: \S+){0,2} a gas\b", r"\bfuga de gas", r"\bhuevos? podridos?"],
     "Por favor salga de la casa ahora, no toque ningún interruptor y, cuando esté afuera, llame al 911 o a la "
     "compañía de gas."),
    ("carbon_monoxide", [r"mon[oó]xido de carbono", r"(?:alarma|detector) de mon[oó]xido"],
     "Por favor saque a todos de la casa ahora y llame al 911 desde afuera."),
    ("flooding", [r"(?:revent[oó]|rompi[oó]|estall[oó]|explot[oó])(?: \S+){0,2} (?:tubo|tuber[ií]a|ca[ñn]o|ca[ñn]er[ií]a)",
                  r"(?:tubo|tuber[ií]a|ca[ñn]o|ca[ñn]er[ií]a)(?: \S+){0,3} (?:revent[oó]|rompi[oó]|reventad[oa]|rot[oa])",
                  r"(?:se est[aá]|est[aá]) inundando", r"\best[aá] inundad[oa]", r"agua por todas partes",
                  r"agua por todos lados", r"(?:sale|saliendo|brota) (?:mucha )?agua (?:a chorros|sin parar)",
                  r"no (?:puedo|podemos) (?:parar|detener) el agua"],
     "Si es seguro, cierre la llave de paso principal del agua, normalmente cerca del medidor, y aléjese de "
     "cualquier cosa eléctrica cerca del agua."),
    ("sewage", [r"\baguas negras", r"(?:drenaje|desag[uü]e|alcantarilla)(?: \S+){0,3} (?:regresando|desbordando|se desborda)"],
     "Por favor deje de usar los lavabos, los inodoros y las regaderas hasta que un técnico lo revise."),
    ("sparks", [r"\bchispas\b", r"\bchispea", r"(?:sale|saliendo|echando) humo", r"(?:huele|olor)(?: \S+){0,2} a quemado",
                r"\ben llamas\b", r"\bhay fuego\b"],
     "Si es seguro, apague la electricidad o el gas del aparato y mantenga a todos alejados."),
]
VERTICALS = {"home_services": {"en": HOME_SERVICES, "es": HOME_SERVICES_ES}}

# Gas or CO with the caller still inside: after the line, this, then hang up (the playbook's rule).
LEAVE_NOW = {"gas", "carbon_monoxide"}
CALL_BACK = {"en": "Once you're safe outside, you can call us back.",
             "es": "Cuando esté a salvo afuera, puede volver a llamarnos."}
OUTSIDE = re.compile(r"\b(?:outside|out of the (?:house|home|building)|left the (?:house|home)|"
                     r"in (?:the|my) (?:car|yard|driveway)|at (?:a|my|the) neighbor\S*|afuera|fuera de la casa|"
                     r"(?:ya )?(?:sal[ií]|salimos)\b|en el (?:carro|coche|patio)|en la calle)")


def _sentences(text: str) -> list[str]:
    """Lowercased sentences, punctuation removed except apostrophes and commas (kept as " , " clause marks)."""
    out = []
    for part in re.split(r"[.!?;¡¿\n]+", text.lower()):
        words = re.sub(r"[^\w\s',]", " ", part.replace(",", " , ")).split()
        while words and words[0] == ",":
            words.pop(0)
        if words:
            out.append(" ".join(words))
    return out


def _negated(sentence: str, at: int) -> bool:
    before = sentence[:at].split()
    breaks = [i for i, w in enumerate(before) if w in CLAUSE_BREAKS]
    clause = before[breaks[-1] + 1:] if breaks else before
    return any(w in NEGATIONS for w in clause[-3:])


def match(vertical: str, text: str, language: str = "en") -> tuple[str, str] | None:
    """(emergency name, safety line) if the caller describes one, else None. Ignores negated phrases
    ("I don't smell gas") so nobody gets an alarming instruction by mistake."""
    sentences = _sentences(text)
    for name, patterns, line in VERTICALS.get(vertical, {}).get(language, []):
        for pattern in patterns:
            for s in sentences:
                if any(not _negated(s, m.start()) for m in re.finditer(pattern, s)):
                    return name, line
    return None


def must_leave(name: str, text: str) -> bool:
    """The caller has to get out now, so the call ends after the safety line: gas or CO, and nothing they said
    puts them outside already ("I'm outside", "we left the house"; "I haven't gone outside" doesn't count)."""
    if name not in LEAVE_NOW:
        return False
    return not any(not _negated(s, m.start()) for s in _sentences(text) for m in OUTSIDE.finditer(s))
