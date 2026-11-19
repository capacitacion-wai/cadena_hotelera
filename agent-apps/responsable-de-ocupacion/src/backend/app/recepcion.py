"""The conversation, also when writing.

THE PROBLEM. By voice there is someone in front who understands: asks for the missing
piece of data, answers the general things and only delegates the real work. Writing came in
through the back door — EVERYTHING started an agent turn of 25-40k tokens. A "hello", a
"thanks", a "what is intra-community VAT?" and, the most expensive of all, a "put me down
for Lisbon" without dates: twenty seconds and $0.014 to answer "which dates?".

WHAT THIS DOES. A short call BEFORE the agent that does not answer for it: it **decides**.

    contesto  — courtesy or elementary general knowledge (the test is in
                `conversacion.QUE_PUEDES_CONTESTAR_TU`, the same one voice uses)
    pregunto  — a piece of data the capability declares it needs is missing
    atajo     — it is a capability declared with `resuelve_con` and all its data is there
    delego    — everything else: the normal turn, as today

FOUR DECISIONS that make it safe:

1. **In writing it does NOT word the agent's output.** By voice it does, because it has to
   speak. Here the agent's answer passes VERBATIM with its markdown, its tables and its
   cards: passing it through a second model pays the tokens twice and spoils exactly what
   the agent did well.
2. **It runs with the employee's model, in `directo`.** The same one that gives it a voice
   —one single identity, one single cost— but without reasoning and with the definition
   and the last turns as context, not the whole conversation. If this reasoned, it would
   add seconds to EVERY message, including the ones that do need the agent.
3. **When in doubt, delegate.** It is the usual path and being wrong towards it costs
   nothing. Being wrong towards `contesto` leaves a false piece of data someone believes.
4. **Whatever it handles is told to the agent when it is finally called.** The opencode
   session IS the memory of the conversation: if three messages do not go through it, the
   agent will have a gap in its history and may contradict something already discussed.
   See `pendiente_de_contar`.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx
from sqlalchemy import select

from app.app_database import app_session
from app.app_models.runtime import AppSetting

logger = logging.getLogger(__name__)

CLAVE_ACTIVA = "recepcion_activa"

#: How many recent EXCHANGES it is shown (one = what the person said + what they were
#: answered) to be able to judge "and the next one?". It is not the employee's memory, it is
#: the thread of the chat.
#:
#: CAREFUL with the double: the list has one entry PER MESSAGE, so cutting it with this
#: plain number showed HALF. Measured on 22/09/2026 asking for a hotel: six entries are three
#: messages, so "two adults and two children" —said in the first— was no longer in front of
#: it when it was time to decide, and it asked how many adults there were. The comment said
#: "exchanges" and the code cut messages: the kind of mismatch nobody sees by reading.
_INTERCAMBIOS_DE_CONTEXTO = 8

#: Hard cap on what it can write. If it needs more than this, it was not its to answer —
#: and "not its" means DELEGATE, not trim: until 12.36.0 this cut at 900 and showed the
#: piece, which is exactly the opposite of what the sentence says. An invented route of
#: 1,200 characters was not rejected: it was served half-way.
#:
#: The number does not judge the content, it judges the SHAPE. A courtesy, a sum or a unit
#: conversion fit easily in 400; an itinerary does not (the two measured on 20/09/2026 went
#: past 550). Going over costs twenty seconds —the agent answers it anyway—; falling short
#: leaves a false piece of data someone believes.
_TOPE_RESPUESTA = 400


async def activa() -> bool:
    """Whether this piece runs on THIS message. Two conditions, and in this order:

    1. that the employee HAS it (deployment decision, `AGENT_RECEPTION_ENABLED`);
    2. that it is switched on (Operations switch, on by default).

    The first rules over the second and not the other way round: a switch that governs
    something that is not deployed is a control that does nothing, and that is worse than
    not having it."""
    from app.config import RECEPTION_ENABLED
    if not RECEPTION_ENABLED:
        return False
    try:
        async with app_session() as s:
            v = ((await s.execute(
                select(AppSetting.value).where(AppSetting.key == CLAVE_ACTIVA)
            )).scalar_one_or_none() or "").strip().lower()
    except Exception:  # noqa: BLE001
        return True
    return v not in ("0", "false", "no", "off")


def _modelo() -> str:
    """The EMPLOYEE's model. The same one that gives it a voice: one single identity and
    one single cost.

    `agent_model` is set by the operator in Operations; if there is none, the deployment's."""
    try:
        from app.config import DEFAULT_MODEL
        m = DEFAULT_MODEL or ""
    except Exception:  # pragma: no cover
        m = ""
    return (os.environ.get("AGENT_RECEPCION_MODEL") or m or "").split("/", 1)[-1]


async def _modelo_del_operador() -> str:
    try:
        async with app_session() as s:
            m = ((await s.execute(
                select(AppSetting.value).where(AppSetting.key == "agent_model")
            )).scalar_one_or_none() or "").strip()
        from app.config import modelo_vigente
        m = modelo_vigente(m)
        return m.split("/", 1)[-1] if m else _modelo()
    except Exception:  # noqa: BLE001
        return _modelo()


CONTRATO = """\
You are this employee's CONVERSATION, in writing. You are not another assistant: to the
outside you ARE HIM, with its name and its way of speaking.

Your job is not to answer: it is to DECIDE what is done with what they have just written. You
ALWAYS return a JSON and nothing else:

  {"accion": "contesto",  "texto": "..."}          ← courtesy or general knowledge
  {"accion": "pregunto",  "texto": "..."}          ← a piece of data is missing to be able to do it
  {"accion": "atajo",     "capacidad": "<name>", "argumentos": {...}}
  {"accion": "delego",    "razonamiento": "rapido|normal|profundo"}

`texto` is read by the person as is: write it in the language they write in.

WHEN EACH ONE

`contesto` — only what you can answer yourself:
{QUE_PUEDES_CONTESTAR}

`pregunto` — when what they ask for is something it DOES do but an ESSENTIAL piece of data is
missing that ONLY the person can give («apúntame Lisboa» without dates). The "I need to be
given" of each capability says what IT uses, not what you have to ask for: before asking
anything, go through the list of WHAT IS NEVER ASKED OF THE PERSON (below). And with three
more brakes:

  · **Everything that is missing, at once and in one line.** No asking in instalments.
  · **What is OPTIONAL is not asked.** Budget, preferences, "anything else I should know":
    if they have not said it, it is not needed. It is what turns a question into an
    interrogation.
  · **What can be deduced is NOT asked**: the date comes from above, and what was said in
    ANY earlier message of this conversation is already said — asking for it again is what
    tires whoever talks to you most.

`atajo` — only if what they ask for is EXACTLY a capability marked [DIRECT] and you have all
its data: the ones in `data:` in its mark. The session, who is asking and their permissions
do not count as missing data — they always go. If one is really missing, it is not a
shortcut: either `pregunto`, or `delego`.

`delego` — EVERYTHING else, and in `razonamiento` say how much it has to be thought through:
`rapido` to look, list or confirm; `normal` to do the work; `profundo` to compare, decide,
recommend or plan. It is a suggestion: if that person has already chosen their level, theirs
rules.

And what slips through the other door the most, said here with its name: planning,
recommending, proposing a route, putting together a plan or comparing options IS `delego`
whenever it is on the list of what it does. It is not general knowledge however well you
know the subject: yours would look similar from outside and be made from memory, and its
comes from its data.

THE RULE THAT GOVERNS ALL THIS: when in any doubt, `delego`. Delegating too much only costs
time —it gets resolved anyway—; answering too much leaves a false piece of data someone
believes, and asking too much when the data was already written tires whoever talks to you.

And never say you are going to check it, that you are looking at it or that you are asking
anyone: if you delegate, the answer is given by it directly and you write nothing."""


def _hoy(zona: str) -> str:
    """What day and time it is, in the business's time zone.

    It goes in the contract because without this the conversation CANNOT resolve
    "tomorrow" nor know which year it lives in: measured on 22/09/2026, it asked "which year
    is 23 September?". That is not clumsiness of the model, it is data it did not have —
    and none has it: a model does not know what day it is unless it is told.
    """
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        ahora = datetime.now(ZoneInfo(zona))
    except Exception:  # noqa: BLE001 — a misspelled zone cannot leave it without a date
        ahora = datetime.now()
        zona = "server time"
    dias = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    meses = ("January", "February", "March", "April", "May", "June", "July", "August",
             "September", "October", "November", "December")
    return (f"TODAY is {dias[ahora.weekday()]} {ahora.day} {meses[ahora.month - 1]} "
            f"{ahora.year}, {ahora:%H:%M} ({zona}). \"Tomorrow\", \"on Monday\" or \"this "
            f"week\" (in any language) you resolve yourself with this: do not ask for a date "
            f"that can already be deduced.")


def _prompt(catalogo: list[str], exigencias: list[str], no_hace: list[str],
            historial: list[dict], mensaje: str, quien: str = "",
            zona: str = "UTC", puede_dar_ubicacion: bool = False) -> list[dict]:
    from app.conversacion import (
        LO_QUE_NO_SE_PIDE, QUE_PUEDES_CONTESTAR_TU, SIN_IDENTIFICADORES,
    )
    partes = [_hoy(zona)]
    # WHO IT IS, after the date. Without this the contract told it "you ARE HIM, with its
    # name" without saying which: asked «¿cómo te llamas?» it answered with its connector's name.
    if quien:
        partes.append(quien)
    partes.append(CONTRATO.replace("{QUE_PUEDES_CONTESTAR}", QUE_PUEDES_CONTESTAR_TU))
    partes.append(SIN_IDENTIFICADORES)
    # What is NOT asked, by name (12.56.0): the session was asked for because a capability
    # declared it, and «cerca de mi hotel» asked for the hotel it has in its data.
    partes.append(LO_QUE_NO_SE_PIDE)
    if puede_dar_ubicacion:
        partes.append(PUEDE_DAR_UBICACION)
    if catalogo:
        partes.append("THIS IS WHAT IT CAN DO (do not answer from here: it is to "
                      "know what can be asked for and with which data)\n" + "\n".join(catalogo))
    if exigencias:
        partes.append("AND THIS ALWAYS APPLIES:\n" + "\n".join(f"- {x}" for x in exigencias))
    if no_hace:
        partes.append("WHAT IT DOES NOT DO (the only thing you can take as impossible):\n"
                      + "\n".join(f"- {x.rstrip('.')}." for x in no_hace))
    charla = "\n".join(
        f"{'Person' if t.get('de') == 'persona' else 'You'}: {str(t.get('texto') or '')[:600]}"
        for t in historial[-(_INTERCAMBIOS_DE_CONTEXTO * 2):])
    usuario = (f"WHAT HAS BEEN SAID SO FAR:\n{charla}\n\n" if charla else "") + \
              f"WHAT THEY HAVE JUST WRITTEN:\n{mensaje[:2000]}"
    return [{"role": "system", "content": "\n\n".join(partes)},
            {"role": "user", "content": usuario}]


async def _zona() -> str:
    """The BUSINESS's time zone, which is the one that matters: "tomorrow" is tomorrow where
    the customer is, not where the server is."""
    try:
        from app.business_timezone import effective_timezone
        return await effective_timezone() or "UTC"
    except Exception:  # noqa: BLE001
        return "UTC"


#: The line the reception reads when the person's device CAN send its location (`capacidades` has "ubicacion").
#: Without it, «find me a pharmacy near where I am» got «which city?» from here and never reached the
#: agent, which is who asks the device (AVJ-33, 19/10/2026).
PUEDE_DAR_UBICACION = ("THE PERSON'S DEVICE CAN SEND ITS LOCATION: never ask them where they are or which city; "
                       "when what they ask depends on where they are (near me, around here, where I am), it is "
                       "`delego` — the employee asks the device for it.")
#: What the person writes when it depends on WHERE THEY ARE (es/en; lower case, no accents needed to match).
_DONDE_ESTOY = ("donde estoy", "dónde estoy", "cerca de mi", "cerca de mí", "por aqui", "por aquí", "aqui cerca",
                "aquí cerca", "mi ubicacion", "mi ubicación", "near me", "where i am", "around here", "my location")


def depende_de_donde_esta(mensaje: str) -> bool:
    t = (mensaje or "").lower()
    return any(x in t for x in _DONDE_ESTOY)


async def decidir(mensaje: str, historial: Optional[list[dict]] = None,
                  capacidades: Optional[list[str]] = None) -> dict:
    """What is done with this message. Returns `{}` when it could not be decided.

    `{}` means DELEGATE: any failure —no key, no network, broken JSON, the model saying
    something odd— falls on the usual path. It is the only acceptable degradation here:
    the reception is a convenience, and a convenience cannot prevent talking."""
    from app.config import TRANSCRIPTION_API_KEY, TRANSCRIPTION_BASE_URL
    if not TRANSCRIPTION_API_KEY or not (mensaje or "").strip():
        return {}
    modelo = await _modelo_del_operador()
    if not modelo:
        return {}

    from app import conversacion as conv
    try:
        catalogo = await conv.capacidades_vivas()
        exigencias = conv.exigencias()
        no_hace = await conv.lo_que_no_hace()
        quien = await conv.identidad()
    except Exception:  # noqa: BLE001 — without a catalog it cannot decide well: it delegates
        logger.debug("recepcion: no se pudo componer el contrato", exc_info=True)
        return {}

    cuerpo: dict[str, Any] = {
        "model": modelo,
        "messages": _prompt(catalogo, exigencias, no_hace, historial or [], mensaje, quien,
                            await _zona(), puede_dar_ubicacion="ubicacion" in (capacidades or [])),
        "max_completion_tokens": 1200,
        "response_format": {"type": "json_object"},
    }
    # A LITTLE deduction, not a deliberation (22/09/2026). It was on `none` —a reflex—
    # because this goes IN FRONT of every message, including the ones that do need the
    # agent, and every second here is eaten by the whole conversation. But what is asked
    # of it is not to classify: it is to gather what has been said over four messages,
    # resolve "tomorrow" and decide what is REALLY missing. With `none` it answered in 1.6 s
    # and asked the same thing three times. `low` is the step that buys that; if more were
    # needed, the next control is the model (`AGENT_RECEPCION_MODEL`), not raising this again.
    if "gpt-5" in modelo:
        cuerpo["reasoning_effort"] = "low"
    try:
        async with httpx.AsyncClient(timeout=25) as cli:
            r = await cli.post(
                f"{TRANSCRIPTION_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {TRANSCRIPTION_API_KEY}"},
                json=cuerpo,
            )
        if r.status_code >= 400:
            logger.info("recepcion: el modelo contestó %s; se delega", r.status_code)
            return {}
        crudo = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        d = json.loads(crudo)
    except Exception:  # noqa: BLE001
        logger.debug("recepcion: no se pudo decidir; se delega", exc_info=True)
        return {}
    if not isinstance(d, dict):
        return {}
    return _solo_atajos_declarados(d)


def _solo_atajos_declarados(d: dict) -> dict:
    """A shortcut to a capability that is NOT declared as direct becomes delegating.

    The model sometimes names a shortcut that does not exist —«¿cómo llego andando…?» came
    out as shortcut `calcular_distancia_duracion`, which the travel assistant does not have
    (24/09/2026)—. The normal path ends up resolving it, but the decision was already lying
    and the measurement counted a shortcut that never happened. It is decided with the real
    catalog, not with what the model believes."""
    if str(d.get("accion") or "").strip().lower() != "atajo":
        return d
    from app import atajos
    if atajos.buscar(str(d.get("capacidad") or "")):
        return d
    logger.info("recepcion: atajo a %r, que no está declarado; se delega", d.get("capacidad"))
    return {"accion": "delego", "razonamiento": d.get("razonamiento") or ""}


def normalizar(decision: dict) -> dict:
    """Leaves the decision in a shape that can be trusted.

    Whatever does not fit becomes `delego`, which is what makes this piece safe: a model
    that returns something odd cannot end up answering on its own."""
    accion = str((decision or {}).get("accion") or "").strip().lower()
    if accion in ("contesto", "pregunto"):
        texto = str(decision.get("texto") or "").strip()
        if not texto:
            return {"accion": "delego"}
        if len(texto) > _TOPE_RESPUESTA:
            # Long where short was expected: the shape of the failure, without looking at what it is about.
            logger.info("recepcion: %s de %d caracteres; eso no era suyo, delego",
                        accion, len(texto))
            return {"accion": "delego"}
        return {"accion": accion, "texto": texto}
    if accion == "atajo":
        cap = str(decision.get("capacidad") or "").strip()
        if not cap:
            return {"accion": "delego"}
        args = decision.get("argumentos")
        return {"accion": "atajo", "capacidad": cap,
                "argumentos": args if isinstance(args, dict) else {}}
    nivel = str((decision or {}).get("razonamiento") or "").strip().lower()
    return {"accion": "delego", "razonamiento": nivel if nivel in
            ("rapido", "normal", "profundo") else ""}


def pendiente_de_contar(historial: list[dict]) -> str:
    """What the reception handled since the agent's last turn, to attach it when it is
    finally called.

    THIS IS NOT AN ORNAMENT. The opencode session is the memory of the conversation: if
    three messages are resolved here and do not go through it, the agent will have a gap in
    its history and may contradict something that had already been discussed. The failure
    would show up days later and would look like the model's."""
    tramo: list[str] = []
    for t in reversed(historial or []):
        # It stops at the last turn the AGENT HANDLED, not at the last answer: from outside
        # the three answers are identical —all are the employee's text—, and stopping at any
        # of them would leave out exactly what it did not see. That is why the history
        # carries `atendido_por` (see `chat_threads._quien_atendio`).
        if t.get("de") == "agente" and t.get("atendido_por", "agente") == "agente":
            break
        tramo.append(f"{'Person' if t.get('de') == 'persona' else 'You'}: "
                     f"{str(t.get('texto') or '')[:400]}")
    if not tramo:
        return ""
    tramo.reverse()
    return ("[BEFORE THIS, in this same conversation, the following was said — you handled it "
            "without getting to work on it:]\n" + "\n".join(tramo))
