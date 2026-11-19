"""WHERE the conversation is happening, and what can be asked for there.

THE PROBLEM. Until now the conversation received the same text on mobile and in the
browser —it is written that way on purpose in `voz.py`: "so that the web and the mobile app
receive EXACTLY the same"— and that made impossible what is good about mobile. On a phone
the person carries their location and a camera; on a laptop, usually not. With a single
text only two behaviours fit and both are bad: always offering it —and looking ridiculous
on the desktop— or never offering it, which is what happened.

WHAT IS DECLARED, IS NOT DEDUCED. The surface says what it has when opening the
conversation. It is not guessed from the `User-Agent` or the window size: a mobile browser
is not the app —it has no native camera or notifications—, and a denied permission cannot
be seen from here. Whoever knows what there is, is whoever is going to resolve it.

AND KNOWING IT IS NOT ENOUGH. A capability the agent knows about but cannot invoke is
exactly the "promises something it cannot do" we chase in the definition: it would end up
asking the person to type in their coordinates. That is why each entry says who resolves
it, and only one has a TOOL: the location, which the EMPLOYEE asks for
(`pedir_ubicacion`, in `mcp_server.py`) and the device answers. The others are resolved by
the PERSON with the paperclip, and saying so in the catalog is what stops the
conversational layer from waiting for a mechanism that does not exist.

THE RULE, which `test_superficie.py` pins: every `herramienta` declared here has to exist
on the other side. A capability with a function nobody can fire is the server version of
the same failure we chase in the machine —an event without a producer—: it does not fail,
it does not warn, and it promises whoever speaks something that is never going to happen.

The texts are instructions for the conversational model and are in English; the «…»
phrases are examples of what is said to the person.
"""
from __future__ import annotations

from typing import Any, Dict, List

#: The channels that exist. `web` and `movil` are the two there are today; they are named
#: here so a new channel has to go through this file and does not slip in as free text.
CANALES = {
    "movil": "You are on this person's phone, talking.",
    "web": "You are in this person's browser, talking.",
}

#: The catalog. Each entry says what can be ASKED FOR and with which function it is asked.
#:
#: `puedo` is read in the conversation's voice ("you can ask them…"), so it goes in plain
#: person language: no "geolocation API". And `cuando` matters as much as `puedo` — without
#: it, a model with a tool at hand uses it just in case, and asking for the location for a
#: question that does not need it is exactly how trust is lost.
CAPACIDADES: Dict[str, Dict[str, Any]] = {
    "ubicacion": {
        "puedo": "ask them where they are right now, ASKING THEM out loud first",
        "cuando": "only when knowing their starting point changes the answer —a route, "
                  "a duration, something nearby—, and ONCE per conversation: you ask them, "
                  "you wait for them to answer, and only if they say yes you request it",
        "herramienta": {
            "name": "pedir_ubicacion",
            "description": ("Reads the device's location. FIRST ask them out loud and "
                            "wait for their answer: if you have not done so yet, this "
                            "will tell you instead of reading it. Once per conversation; "
                            "the second attempt does nothing. Returns latitude, longitude "
                            "and accuracy in metres, or the reason it could not be done — "
                            "and that reason tells apart whether they said no or the "
                            "browser has it blocked, which is not fixed by insisting."),
            "parameters": {"type": "object", "properties": {
                "para_que": {"type": "string",
                             "description": "What you are going to do with it, in one "
                                            "sentence, in the person's language. It is "
                                            "shown to the person when asking for permission."}},
                "required": ["para_que"]},
        },
    },
    # THE THREE BELOW HAVE NO TOOL, and it is a fact, not an oversight: the employee has
    # only ONE device function (`pedir_ubicacion`, in `mcp_server.py`). There is nobody who
    # can fire the camera, the gallery or the file picker, so declaring them with a tool
    # promised the conversational layer that "you will be asked in one sentence" for
    # something nobody was ever going to ask it. They stay in the catalog —and are needed—
    # because the person CAN send a photo or a document with the paperclip: what changes is
    # who does it. You ask for it by talking; they attach it.
    "camara": {
        "puedo": "ask them to send you a photo taken on the spot",
        "cuando": "when what has to be seen is in front of them —a paper receipt, "
                  "a number plate, the state of something—; you tell them and they attach "
                  "it themselves, there is nothing for you to fire",
        "herramienta": None,
    },
    "galeria": {
        "puedo": "ask them for a photo they already have on the phone",
        "cuando": "when what is needed was already photographed before; they choose it "
                  "and attach it themselves",
        "herramienta": None,
    },
    "ficheros": {
        "puedo": "ask them for a document or a file from their device",
        "cuando": "when a PDF, an invoice or a specific file is needed; they look for it "
                  "and attach it themselves",
        "herramienta": None,
    },
    # No tool on purpose: it is not something ASKED FOR in the moment, it is something
    # known. Telling it changes what it offers ("I'll let you know when it's ready") and
    # needs no function.
    "notificaciones": {
        "puedo": "notify them later even if they do not have this open",
        "cuando": "when something is going to take a while and it is worth offering to "
                  "notify them later",
        "herramienta": None,
    },
    "pantalla": {
        "puedo": "show them something on the screen while you talk",
        "cuando": "when there is a list, a table or a link that is understood better seen "
                  "than said — you say it briefly and leave it written",
        "herramienta": None,
    },
}


def normalizar(declaradas: Any) -> List[str]:
    """Only what is in the catalog, and without repeats.

    Whatever it does not recognise is silently discarded ON PURPOSE: the client is newer than
    the server half of the time —an app already published cannot be rewound— and breaking a
    conversation because the phone declared something this server does not know yet would
    be punishing the person for our deployment."""
    fuera: List[str] = []
    for x in (declaradas or []):
        k = str(x).strip().lower()
        if k in CAPACIDADES and k not in fuera:
            fuera.append(k)
    return fuera


def instrucciones(canal: str, capacidades: List[str]) -> str:
    """The piece of instructions that says where it is and what it can ask for there.

    What is NOT there is said too, and it is not filler: without that sentence the model
    assumes it has the usual things and offers to take a photo on a laptop. Saying "there
    is no camera here" saves the person a whole round trip."""
    canal = str(canal or "").strip().lower()
    if canal not in CANALES:
        return ""
    l = ["WHERE YOU ARE TALKING", CANALES[canal]]

    hay = [k for k in capacidades if k in CAPACIDADES]
    con_tool = [k for k in hay if (CAPACIDADES[k].get("herramienta"))]
    if con_tool:
        # YOU DO NOT ASK FOR IT: IT DOES (12.43.0). Before, these were declared as tools of
        # the conversational layer, and it used them just in case —before knowing whether
        # they would be needed— and got stuck when the permission was blocked. Now the
        # EMPLOYEE asks for it, which is who knows whether it needs it and what for; the
        # conversational layer is only told it may happen, so it is not surprised and does
        # not offer it itself.
        l.append("")
        l.append("WHAT THIS DEVICE CAN GIVE, and that you do NOT ask for:")
        for k in con_tool:
            c = CAPACIDADES[k]
            l.append(f"- {c['puedo']} · {c['cuando']}")
        l.append("")
        l.append("If any of that is needed, you will be asked in one sentence so you "
                 "ask the person; you say it, wait for their answer, and that is "
                 "it. Do not offer it on your own, do not ask for it twice and, if they say "
                 "no, carry on without it without insisting.")

    sin_tool = [k for k in hay if not CAPACIDADES[k].get("herramienta")]
    if sin_tool:
        l.append("")
        # WITH ITS OWN TITLE, and it is not cosmetic: without it these lines fell right under
        # the "you will be asked in one sentence" above and read as part of the same thing —
        # which is precisely the confusion to remove. Here nobody asks for anything: either
        # the person does it, or it is something that simply exists.
        l.append("THE REST THAT IS AVAILABLE HERE:")
        for k in sin_tool:
            c = CAPACIDADES[k]
            l.append(f"- Here you can {c['puedo']}: {c['cuando']}.")

    faltan = [k for k in CAPACIDADES if k not in hay]
    if faltan:
        l.append("")
        l.append("Here you do NOT have: "
                 + ", ".join(CAPACIDADES[k]["puedo"] for k in faltan)
                 + ". Do not offer it — it does not exist on this surface, and offering it "
                   "costs them a whole round trip.")
        # "DO NOT OFFER IT" IS NOT ENOUGH: you have to say what is done INSTEAD. Measured on
        # 20/09/2026 with the location blocked in the browser: it asked for the position, it
        # was told there was no way, and it kept repeating "the browser does not share your
        # position" for the whole conversation — even AFTER the person dictated their address
        # out loud, which was exactly what was needed.
        if "ubicacion" in faltan:
            l.append("")
            l.append("And the location in particular: here there is NO way of knowing where "
                     "they are, and it cannot be retried. If you need their starting point "
                     "—a route, something nearby, a distance—, ASK THEM BY TALKING (\"where "
                     "are you leaving from?\", \"which area are you in?\") and work with what "
                     "they tell you, which is worth exactly the same. Do not mention the "
                     "permission, the browser or the device: that is your kitchen. And if they "
                     "have already told you an address, USE IT: repeating that you do not have "
                     "their location when they have just given it to you is the worst mistake "
                     "in this conversation.")
    return "\n".join(l)
