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

# Med spa: the real risks are an allergic reaction, filler blocking a blood vessel (vision changes, skin turning white
# or blue), heavy bleeding, and an infection or burn. None of these is judged by the receptionist: it says the line
# and gets a provider.
MED_SPA = [
    ("allergic_reaction", [r"\b(?:can'?t|cannot|couldn'?t) breathe\b", r"\b(?:trouble|difficulty|hard to|hard time) breathing\b",
                           r"\b(?:throat|tongue|lips?|face)(?: \S+){0,3} (?:swelling|swollen|closing|tight|puffing up)\b",
                           r"\b(?:swelling|swollen)(?: \S+){0,2} (?:in|of|on) (?:my |the )?(?:throat|tongue|face|lips?)\b",
                           r"\banaphyla", r"\bhives all over\b", r"\bpassing out\b|\bpassed out\b|\bfainted\b"],
     "If you're having trouble breathing, or your face or throat is swelling, call 911 right now."),
    ("vision_or_blockage", [r"\b(?:vision|eyesight)\b(?: \S+){0,4} (?:blurry|blurred|changed|changing|gone|dark|double|dim|doubled)\b",
                            r"\b(?:blurry|double|blurred) vision\b", r"\b(?:went|going|gone) blind\b|\bcan'?t see\b",
                            r"\b(?:skin|lip|lips|nose|area|forehead|cheek)(?: \S+){0,3} (?:turning|turned|went|looks|looking|gone|is) "
                            r"(?:white|blue|gr[ae]y|purple|dark|mottled|dusky|blanched)\b",
                            r"\b(?:severe|terrible|horrible|intense|excruciating) pain\b(?: \S+){0,6} (?:filler|injection|injected|botox)"],
     "That needs a provider right away. If your vision has changed or you can't breathe, call 911 now."),
    ("heavy_bleeding", [r"\b(?:won'?t|wont|doesn'?t|not) stop bleeding\b", r"\bbleeding (?:a lot|heavily|badly|everywhere|through)\b",
                        r"\bsoaking through\b"],
     "Press firmly on the area with a clean cloth, and call 911 if the bleeding doesn't stop in a few minutes."),
    ("urgent_reaction", [r"\b(?:pus|oozing)\b", r"\bspreading redness\b|\bredness (?:is )?spreading\b",
                         r"\bblisters?\b|\bblistering\b", r"\bburn(?:ed|t|s)? (?:my|the) (?:skin|face)\b",
                         r"\bfever\b(?: \S+){0,6} (?:treatment|filler|botox|laser|peel)|(?:treatment|filler|botox|laser|peel)(?: \S+){0,10} fever\b"],
     "That should be looked at by a provider soon, so I'm getting our nurse team for you."),
]
MED_SPA_ES = [
    ("allergic_reaction", [r"\bno puedo respirar\b", r"\bdificultad (?:para|al) respirar\b",
                           r"\b(?:garganta|lengua|labios|cara)(?: \S+){0,3} (?:hinchad[ao]s?|cerr[ao]ndose)\b", r"\banafilax"],
     "Si le cuesta respirar, o se le hincha la cara o la garganta, llame al 911 ahora mismo."),
    ("vision_or_blockage", [r"\bvisi[oó]n(?: \S+){0,4} (?:borrosa|doble|cambi[oó]|oscura)\b", r"\bvisi[oó]n borrosa\b",
                            r"\bno puedo ver\b"],
     "Eso necesita a un proveedor de inmediato. Si cambió su visión o no puede respirar, llame al 911 ahora."),
    ("heavy_bleeding", [r"\bno (?:para|deja) de sangrar\b", r"\bsangr(?:a|ando) mucho\b"],
     "Presione firmemente la zona con un paño limpio, y llame al 911 si el sangrado no para en unos minutos."),
]

# HVAC and plumbing, winter first. Every home-services line comes first and is untouched (first match wins, so gas,
# carbon monoxide, flooding and the rest still win over the new tiers); the cold-weather tiers are added after them.
_NUMBER_WORDS = (r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
                 r"sixteen|seventeen|eighteen|nineteen|twenty|thirty)(?:[ -](?:one|two|three|four|five|six|seven|eight|nine))?")
COLD_CUE = (r"\b(?:freezing|frozen|below zero|sub ?zero|snow|snowing|snowed|blizzard|frigid|ice storm|"
            r"(?:[0-9]|[1-3][0-9]|" + _NUMBER_WORDS + r") degrees|(?:[0-9]+|" + _NUMBER_WORDS + r") below|"
            r"baby|infant|newborn|elderly|oxygen|sick|cold)\b")
COLD_CUE_ES = r"\b(?:congelando|helando|bajo cero|nieve|nevando|fr[ií]o|heladas?|beb[eé]|anciano|enferm[oa])\b"
HVAC_WINTER = [
    ("no_heat_freezing",
     [r"\bno heat\b", r"\b(?:furnace|boiler|heater|heat pump|heat|mini ?split|thermostat)\b(?: \S+){0,3} "
      r"(?:out|off|dead|died|quit|stopped|down|broke|broken|not working|won't (?:start|turn on|come on|kick on)|"
      r"doesn't (?:work|start|come on)|not (?:coming|kicking) on|isn't working|stopped working)\b"],
     "No heat in this cold is urgent. If anyone there is in danger from the cold, call 911.",
     COLD_CUE),
    ("frozen_pipe",
     [r"\bfrozen (?:pipe|pipes|line|lines)\b", r"\b(?:pipe|pipes)(?: \S+){0,3} (?:froze|frozen|freezing)\b"],
     "A frozen pipe can burst, so we're treating this as urgent. If it does burst, shut off the main water valve, "
     "usually near the water meter."),
]
HVAC_WINTER_ES = [
    ("no_heat_freezing",
     [r"\bsin calefacci[oó]n\b", r"\bno hay calefacci[oó]n\b", r"\b(?:calefacci[oó]n|calentador|caldera|horno)\b"
      r"(?: \S+){0,3} (?:no funciona|no enciende|se apag[oó]|est[aá] descompuest[oa]|dej[oó] de funcionar)\b"],
     "Sin calefacción con este frío es urgente. Si alguien corre peligro por el frío, llame al 911.",
     COLD_CUE_ES),
    ("frozen_pipe",
     [r"\btuber[ií]as? (?:congelad[ao]s?|heladas?)\b", r"\b(?:se )?congel[oó] (?:la|una) tuber[ií]a\b"],
     "Una tubería congelada puede reventar, así que lo tratamos como urgente. Si revienta, cierre la llave de paso "
     "principal, normalmente cerca del medidor."),
]
VERTICALS = {"home_services": {"en": HOME_SERVICES, "es": HOME_SERVICES_ES},
             "hvac": {"en": HOME_SERVICES + HVAC_WINTER, "es": HOME_SERVICES_ES + HVAC_WINTER_ES},
             "med_spa": {"en": MED_SPA, "es": MED_SPA_ES}}

# Gas or CO with the caller still inside: after the line, this, then hang up (the playbook's rule).
LEAVE_NOW = {"gas", "carbon_monoxide", "allergic_reaction"}
CALL_BACK = {"en": "Once you're safe outside, you can call us back.",
             "es": "Cuando esté a salvo afuera, puede volver a llamarnos."}
# A possible allergic reaction needs 911, not a callback: after the line, the call ends so they can dial.
HANG_UP_NOW = {"en": "Please hang up now and make that call.", "es": "Por favor cuelgue ahora y haga esa llamada."}


def closing(name: str, language: str) -> str:
    """What the fixed hang-up turn says after the safety line."""
    return (HANG_UP_NOW if name == "allergic_reaction" else CALL_BACK)[language]
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
    for name, patterns, line, *rest in VERTICALS.get(vertical, {}).get(language, []):
        for pattern in patterns:
            for s in sentences:
                if any(not _negated(s, m.start()) for m in re.finditer(pattern, s)):
                    # Some tiers also need a cue elsewhere in what the caller said (no heat AND cold weather).
                    if rest and not any(re.search(rest[0], t) for t in sentences):
                        continue
                    return name, line
    return None


def must_leave(name: str, text: str) -> bool:
    """The caller has to get out now, so the call ends after the safety line: gas or CO, and nothing they said
    puts them outside already ("I'm outside", "we left the house"; "I haven't gone outside" doesn't count)."""
    if name not in LEAVE_NOW:
        return False
    if name == "allergic_reaction":
        return True   # someone who can't breathe needs 911 now, wherever they are
    return not any(not _negated(s, m.start()) for s in _sentences(text) for m in OUTSIDE.finditer(s))
