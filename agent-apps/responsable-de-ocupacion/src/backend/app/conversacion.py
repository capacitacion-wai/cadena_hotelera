"""The conversation contract, written ONCE for both doors.

There are two ways of talking to an employee: voice and writing. Until 12.26.0 only voice
had someone in front who understands, asks for what is missing and answers the general
things; writing came in through the back door and EVERYTHING —a «hola», a «gracias», an
«apúntame Lisboa» without dates— started an agent turn of 25-40k tokens.

Taking that to writing brought up the obvious risk: two texts that say the same thing in
different words and diverge by the third change. So what belongs to the EMPLOYEE —who it
is, what it can do, what it does not do, what it can answer on its own, when something is
resolved in one go— lives here, and each door puts its frame on it:

* the **voice** speaks, waits in silence, does not read URLs and words what reaches it;
* **writing** words nothing of the agent's —it passes verbatim, with its markdown and its
  tables— and only decides: contesto, pregunto, atajo or delego.

What is NOT here is anything that depends on the surface (microphone, camera, screen):
that is declared by `superficie.py`, which already existed for the same thing.

The texts are instructions for a model and are in English (docs/idioma-de-los-agentes.md);
the quoted «…» phrases are examples of what a person says.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


#: THE TEST of what the conversation can answer on its own. It goes here and not in the
#: text of each door because it is THE rule that decides whether an employee makes things
#: up: with an ambiguous "answer what you know", a chat model fills the gap with what sounds
#: familiar. It is literally the same paragraph at both doors — if it is ever refined, it is
#: refined once.
QUE_PUEDES_CONTESTAR_TU = """\
What is general and elementary, that anyone knows and that it would not resolve better: a
sum, a unit conversion, how to say something in another language, what you yourself just
said a moment ago, or a courtesy. That is part of conversing and sending it off to be asked
would be absurd: you do not take two minutes to say that 2 and 2 are 4, nor does it know
it better.

THE FIRST TEST, and it decides on its own: **is it one of the things it does?** You have
them listed up there. If what they ask for falls into one of them it is NOT yours, however
much you think you know about the subject. Recommending a place, putting together a plan,
proposing a route, comparing options: when that is on its list, the good answer is ITS
—made with its data, its rules and leaving a trace—, not a similar one made from memory.
Knowing how to write something with the same SHAPE does not make you the one who has to
write it.

THE SECOND, for what is not on the list: **if the answer could be different for another
person, at another time or in another company, it is not yours either.** Their data, a
timetable, a price, a real distance, the state of anything, what it can do: all of that
changes depending on who asks and when, so it is asked for. The answer sounding familiar
does not make it yours — you know it from somewhere else, not from its place.

AND A MISSING PIECE OF DATA DOES NOT AUTHORISE YOU TO INVENT THE REST. If you are not given
the location, the date or the permission you were going to use, it is asked for anyway with
whatever there is, or what is missing is asked. What is NEVER done is covering the gap with
something of yours "in the meantime", "in general" or "broadly speaking": that is a false answer with a good look, and whoever listens
cannot tell it from the good one.

And when in doubt, ASK FOR IT. Being wrong by asking costs two minutes; being wrong by
answering from memory leaves them a false piece of data they believe."""


#: The rule that produced the "I can't register trips" while having the tool to register
#: them: a model does not classify a REFUSAL as a statement, and refusing looks like the safe
#: option to it.
#: Identifiers belong to the machine, and they show up in answers because the tools return
#: them. Saying them —«ID 4 · Budapest»— is showing the kitchen: nobody asks for "trip
#: number 4", and with two that have the same name what tells them apart is the dates or the city.
LO_QUE_NO_SE_PIDE = """\
WHAT IS NEVER ASKED OF THE PERSON, because it is already known or it has it:

- **Who is talking to you.** Their session, their user, their role, their permissions and what \
they can see go ALWAYS with every message: the session is active by definition, or they would \
not be talking to you. If a capability says it needs "the session", "the user" or "their \
permissions", it ALREADY HAS IT — it is not a missing piece of data nor something that can be asked.
- **What is in THEIR DATA.** What it keeps or can look up about this person — their records, \
what they have contracted or booked, their addresses, what they left noted — is not asked: it \
is asked of it and it looks. «Cerca de mi hotel» ("near my hotel"), «mi próximo viaje» ("my \
next trip"), «el pedido de ayer» ("yesterday's order") are NOT missing data: they are things it \
knows how to look up. Asking "which is your hotel?" makes the person repeat something they \
already gave, and it is what tires them most.
- **What was already said or can be deduced**: any earlier message of this conversation, the \
date and time, and where they are if they have shared their location.

Only what ONLY the person can know is asked: what they want, a choice between options, or a \
new piece of data that does not exist anywhere yet (the dates of a trip they have not told \
yet, for how many people). If you doubt whether the data is in their data, do NOT ask: ask \
it for it — if it really is missing, it will ask with its data in front of it."""


SIN_IDENTIFICADORES = """\
AND DO NOT SAY IDENTIFIERS. An «ID 4», a record number or an internal reference mean \
nothing to whoever reads you: they belong to the machine. Name things by their NAME —«tu \
viaje a Budapest» ("your trip to Budapest"), not «el ID 4»—. If they were needed to tell apart \
two with the same name, use what really distinguishes them: the dates, the city."""


NO_TE_NIEGUES = """\
And one more thing, the one you forget most: saying "I can't" is a statement like any \
other. If it is not on that list above, do NOT refuse: ask for it and let it answer. \
Refusing something that is done is worse than taking two minutes to check it."""


#: Where the deployer leaves the published agent's definition. It is a mounted ConfigMap,
#: so kubelet refreshes it in ~1 min: correcting what the employee promises does not
#: require redeploying it.
RUTA_DEFINICION = "/etc/wai/definicion/presentation.json"


def declaracion() -> dict:
    """This employee's definition. One single source for both doors and the shortcuts.

    **THE PLATFORM'S RULES.** It is the one that is versioned, sealed, approved by a person
    and with its integrity checked against what the agent really has. The manifest's
    (`agent_app.presentation`) is written by the skill when generating the app and nobody
    syncs it with the former: they were two definitions of the same employee that aged
    separately. Measured on 20/09/2026 in the travel assistant: 3 capabilities and no
    shortcut in the pod, against 8 capabilities and 3 shortcuts in the published version.
    The employee ran one and the person reviewed and approved the other.

    The manifest is still read ONLY while the platform's has not arrived —an employee
    deployed with an older deployer— and it disappears on the first deployment with the
    new one. It is not an alternative route that is chosen: it is the old state, which ends.
    """
    try:
        import json as _json
        import os as _os
        if _os.path.exists(RUTA_DEFINICION):
            with open(RUTA_DEFINICION, encoding="utf-8") as fh:
                d = _json.load(fh)
            if isinstance(d, dict) and d:
                return d
    except Exception:  # noqa: BLE001 — an unreadable definition cannot bring the pod down
        logger.warning("no pude leer la definición de %s; uso la del manifiesto",
                       RUTA_DEFINICION, exc_info=True)
    try:
        from app.config import APP_META
        meta = APP_META if isinstance(APP_META, dict) else {}
        p = (meta.get("agent_app") or {}).get("presentation")
        if isinstance(p, dict):
            return p
        if isinstance(p, str) and p.strip():
            return {"soy": p.strip()}
    except Exception:  # pragma: no cover
        pass
    return {}


async def identidad() -> str:
    """WHO this employee is: its name, its job and how it speaks.

    It lives here and not in each door because both speak AS HIM, and one that does not
    know its name makes it up: measured on 20/09/2026, asked «¿cómo te llamas?» in writing,
    it answered «me llamo WAI Viajes» —the name of its CONNECTOR, the only thing resembling
    a name it had in front of it— while its identity said «Asistente de Viajes».

    An employee that does not know its own name is not a wording detail: it is the first
    question anyone asks it."""
    partes: list[str] = []
    try:
        from app.persona import resolve_persona
        p = await resolve_persona() or {}
    except Exception:  # pragma: no cover — without a persona it goes on, with less
        p = {}

    def _es(x):
        return (x.get("es") if isinstance(x, dict) else x) or ""

    nombre = str(p.get("name") or "").strip()
    rol = str(_es(p.get("role"))).strip()
    saludo = str(_es(p.get("greeting"))).strip()
    partes.append(f"You are {nombre}." if nombre else "You are this person's employee.")
    if rol:
        partes.append(f"Your job: {rol}.")
    decl = declaracion()
    if decl.get("soy"):
        partes.append(str(decl["soy"]))
    if saludo:
        partes.append(f"Your way of greeting, to inspire you and not to recite: «{saludo}»")
    try:
        from app.voz import leer_ajustes
        estilo = (await leer_ajustes()).get("estilo")
        if estilo:
            partes.append(f"How you speak: {estilo}")
    except Exception:  # pragma: no cover
        pass
    return "\n".join(partes)


def atajo_de(cap: dict[str, Any]) -> dict[str, Any]:
    """What the conversation may know about a shortcut: that there is one and which data
    to extract.

    NEVER the tool: neither door executes anything, and giving them the name is starting to
    tell them — the following month someone would ask them to call it."""
    rc = cap.get("directo") if isinstance(cap.get("directo"), dict) else cap.get("resuelve_con")
    if isinstance(rc, str) and rc.strip():
        return {"argumentos": []}
    if isinstance(rc, dict):
        if rc.get("herramienta") or rc.get("ruta") or "argumentos" in rc or "salvo_si" in rc:
            d: dict[str, Any] = {"argumentos": [str(x) for x in (rc.get("argumentos") or [])]}
            # A route shortcut also does not apply when one of its skipped steps would be needed.
            salvo = [str(x) for x in [rc.get("salvo_si"), *(rc.get("no_vale_si") or [])] if x]
            if salvo:
                d["salvo_si"] = "; ".join(salvo)
            return d
    return {}


async def capacidades_vivas() -> list[str]:
    """What the employee CAN do, one line per capability, cross-checked with the permissions.

    It is the half a model cannot deduce: one that is only given vetoes fills the rest with
    its default assumption, which in a chat model is "I can't do things".

    Each line carries its detail, what has to be asked of whoever requests it and in which
    words it is asked; and, if it is resolved in one go, its name and the data to extract.
    The format is the same at both doors: what changes is the frame each one puts on top,
    not the catalog."""
    from app.voz import _apoyos_caidos  # same permissions snapshot, one single place

    decl = declaracion()
    caidos: set[str] = set()
    try:
        from app import capabilities as caps
        caidos = await _apoyos_caidos(await caps.build_snapshot())
    except Exception:  # pragma: no cover
        logger.debug("no he podido cruzar la definición con los permisos", exc_info=True)

    lineas: list[str] = []
    for c in (decl.get("hago") or []):
        if not isinstance(c, dict) or not c.get("que"):
            continue
        apoyos = [a["herramienta"] if isinstance(a, dict) else str(a)
                  for a in (c.get("apoyo") or [])
                  if (a.get("herramienta") if isinstance(a, dict) else str(a).strip())]
        if apoyos and all(a in caidos for a in apoyos):
            continue          # it declares it, but right now it does not have it: not offered
        linea = f"- {c['que']}"
        if c.get("aprobacion"):
            linea += " [must be approved first]"
        directo = atajo_de(c)
        if directo and str(c.get("id") or "").strip():
            datos = ", ".join(str(x) for x in (directo.get("argumentos") or []))
            linea += (f" [DIRECT · name: {str(c['id']).strip()}"
                      + (f" · data: {datos}" if datos else "")
                      + (f" · NOT valid if {directo['salvo_si']}"
                         if directo.get("salvo_si") else "") + "]")
        if c.get("detalle"):
            for parrafo in str(c["detalle"]).split("\n"):
                if parrafo.strip():
                    linea += f"\n    {parrafo.strip()}"
        if c.get("necesita"):
            linea += f"\n    I need to be given: {c['necesita']}"
        # WHERE THIS CAPABILITY ENDS. Without it the one who speaks stretches the nearest
        # capability over a request that belongs to another (or to none) and offers it.
        fuera = [str(x).strip() for x in (c.get("no_hace") or []) if str(x).strip()]
        if fuera:
            linea += "\n    Not part of this (route it elsewhere or say so): " + " · ".join(fuera)
        ejs = [str(x).strip() for x in (c.get("ejemplos") or []) if str(x).strip()]
        if ejs:
            linea += "\n    It is asked like this: " + " · ".join(f"«{e}»" for e in ejs[:3])
        lineas.append(linea)
    return lineas


def exigencias() -> list[str]:
    """How it ALWAYS has to do it. They govern the one who speaks just like the one who executes."""
    out = []
    for e in (declaracion().get("exigencias") or []):
        if isinstance(e, dict) and str(e.get("que") or "").strip():
            linea = str(e["que"])
            if e.get("detalle"):
                linea += " " + str(e["detalle"]).splitlines()[0]
            out.append(linea)
    return out


async def lo_que_no_hace() -> list[str]:
    """What is declared in `no_hago` and, if nothing is declared, the real permissions veto."""
    decl = declaracion()
    declarado = [str(x) for x in (decl.get("no_hago") or []) if str(x).strip()]
    if declarado:
        return declarado
    from app.voz import _lo_que_no_puede
    return await _lo_que_no_puede()
