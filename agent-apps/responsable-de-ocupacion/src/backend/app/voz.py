"""Talking to the employee: settings, credential and what it costs.

THREE THINGS AND NOTHING ELSE. The whole conversation happens between the browser and the
voice model over a single WebRTC connection —the audio does not go through here and must
not—; this module only (1) stores how the employee sounds, (2) mints the ephemeral
credential so the browser can open that connection without ever seeing the tenant's key,
and (3) turns the consumption the model itself reports into a row of the cost ledger.

WHY THE PRICE IS COMPUTED HERE. The browser reports TOKENS, which is a fact the model
returns; the price is a decision of ours and lives on the server. If the price were on the
screen, anyone could bill themselves whatever they wanted, and on top of that the frontend
would have to be deployed every time OpenAI changes a rate.

THE PRICE TABLE IS THE *MINI* MODEL'S, and better not repeat the mistake: for an afternoon
it was loaded with the big `gpt-realtime` one ($32/$64 audio) while the mini was the one in
use ($10/$20). It inflated the bill 3.3× and led to recommending exactly the opposite of
what the numbers said. Checked against OpenAI's real invoice on 17/09/2026. If you change
`MODELO_VOZ`, change these six numbers too.

The instructions for the voice model are in English (docs/idioma-de-los-agentes.md); the
voice list descriptions and the sample sentence are the PERSON's UI and stay in Spanish.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select

from app.app_database import app_session, app_write
from app.config import TRANSCRIPTION_API_KEY, TRANSCRIPTION_BASE_URL

logger = logging.getLogger(__name__)

MODELO_VOZ_POR_DEFECTO = "gpt-realtime-2.1-mini"

#: $ per million tokens · developers.openai.com/api/docs/pricing (17/09/2026).
PRECIOS: dict[str, float] = {
    "texto_entrada": 0.60,
    "texto_cacheado": 0.06,
    "texto_salida": 2.40,
    "audio_entrada": 10.0,
    "audio_cacheado": 0.30,
    "audio_salida": 20.0,
}

#: The voices the model offers. The choice is the employee's, not the listener's.
#:
#: The description is a HINT to narrow the search, not a fact: the fact is hearing it, and
#: that is why `/api/voz/muestra` exists. A name like «coral» tells nobody anything, but
#: neither does an adjective I choose: what decides is the ear.
VOCES: dict[str, str] = {
    "alloy":   "neutra y equilibrada",
    "ash":     "grave y seria",
    "ballad":  "cálida, algo melancólica",
    "coral":   "clara y cercana",
    "echo":    "firme y sobria",
    "sage":    "tranquila y pausada",
    "shimmer": "luminosa y con energía",
    "verse":   "expresiva, con matices",
}

#: What the sample says. A sentence of the trade, not "hi, how are you": a voice is chosen
#: by hearing it say what it is really going to say.
FRASE_DE_MUESTRA = "Hola, soy tu empleado. Déjame que lo mire y te cuento en un momento."

#: The model of the SAMPLES. It is not the conversation one —that is realtime— but the
#: text-to-speech one, which for three seconds of audio costs a thousandth of a dollar.
MODELO_MUESTRA = os.environ.get("MODELO_MUESTRA", "gpt-4o-mini-tts")

#: How much it keeps you company while it works. It is not a style whim: each sentence said
#: is a model TURN —it is paid and, measured, 5 out of 24 times it said something different
#: from what it was given—, so there are employees for whom it is better to keep quiet.
ACOMPANAMIENTOS = ("nada", "hechos", "todo")

#: How it starts listening. `pulsar` is the only mode that guarantees only whoever speaks is
#: heard, and there are places —an open-plan office— where that is not optional.
ARRANQUES = ("abierto", "pulsar")

# WHERE IT SOUNDS IS NOT STORED HERE, and it is deliberate.
#
# It was in these settings for a while and it was doubly wrong: only the admin touched it
# and it applied the same to everyone. But speaker or earpiece says nothing about the
# employee —it is not part of its identity, like the voice or the style—: it depends on
# where you are and that moment, and it changes between a conversation in the car and one
# in a meeting. So it lives on each person's DEVICE, next to the control that changes it,
# and it is remembered there. A setting each person needs differently cannot be the employee's.

POR_DEFECTO: dict[str, Any] = {
    "voz": "coral",
    "estilo": "",
    "acompana": "hechos",
    "arranque": "abierto",
}

_CLAVES = {f"voz_{k}": k for k in POR_DEFECTO}


async def leer_ajustes() -> dict[str, Any]:
    """How this employee sounds. It lives in `app_settings`, next to `agent_language` and
    `agent_model`: it is an operator's decision, not the deployment's."""
    fuera = dict(POR_DEFECTO)
    try:
        from app.app_models.runtime import AppSetting
        async with app_session() as s:
            filas = (await s.execute(
                select(AppSetting.key, AppSetting.value).where(AppSetting.key.in_(_CLAVES))
            )).all()
        for clave, valor in filas:
            if valor is not None:
                fuera[_CLAVES[clave]] = valor
    except Exception:  # pragma: no cover — without settings it speaks anyway, with the defaults
        logger.debug("no he podido leer los ajustes de voz", exc_info=True)
    return fuera


async def guardar_ajustes(nuevos: dict[str, Any]) -> dict[str, Any]:
    """Stores only what is known and VALID; the rest is silently ignored.

    A value outside the catalog is not rejected with an error: it is discarded and the
    previous one stays. A style setting cannot leave the employee without a voice."""
    from app.app_models.runtime import AppSetting
    limpio: dict[str, Any] = {}
    if (v := str(nuevos.get("voz") or "").strip()) in VOCES:
        limpio["voz"] = v
    if "estilo" in nuevos:
        limpio["estilo"] = str(nuevos.get("estilo") or "").strip()[:300]
    if (v := str(nuevos.get("acompana") or "").strip()) in ACOMPANAMIENTOS:
        limpio["acompana"] = v
    if (v := str(nuevos.get("arranque") or "").strip()) in ARRANQUES:
        limpio["arranque"] = v

    # Through the QUEUE, which is what any write requires: SQLite in WAL allows a single
    # writer, and a block that goes straight to the pool ends in "database is locked" as
    # soon as it coincides with the executor. What is here is tiny —four keys— but the
    # rule admits no exceptions by size: the one the partial fix of 7.139.4 skipped also
    # looked harmless.
    async with app_write() as s:
        for corta, valor in limpio.items():
            clave = f"voz_{corta}"
            fila = (await s.execute(
                select(AppSetting).where(AppSetting.key == clave)
            )).scalar_one_or_none()
            if fila is None:
                s.add(AppSetting(key=clave, value=str(valor)))
            else:
                fila.value = str(valor)
        await s.commit()
    return await leer_ajustes()


async def muestra_de_voz(voz: str) -> bytes:
    """Three seconds of that voice, so it can be chosen by ear and not by an adjective.

    It uses text-to-speech and not the conversation model: for a loose sentence it is the
    cheap and appropriate thing —realtime exists to converse, not to recite—. It is not
    billed: a thousandth of a dollar per sample does not deserve a row in the ledger, and
    charging for trying a voice discourages exactly what we want people to do.
    """
    if voz not in VOCES:
        raise ValueError(f"esa voz no existe: {voz!r}")
    from app.config import VOICE_ENABLED
    if not VOICE_ENABLED:
        # Defence in depth: the screens no longer paint the button, but the door is
        # closed HERE. An employee deployed without the conversation module cannot end up
        # talking because someone knows the route.
        raise RuntimeError("este empleado no tiene módulo de conversación")
    if not TRANSCRIPTION_API_KEY:
        raise RuntimeError("este despliegue no tiene clave de OpenAI para la voz")
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(
            f"{TRANSCRIPTION_BASE_URL.rstrip('/')}/audio/speech",
            headers={"Authorization": f"Bearer {TRANSCRIPTION_API_KEY}"},
            json={"model": MODELO_MUESTRA, "voice": voz,
                  "input": FRASE_DE_MUESTRA, "response_format": "mp3"},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"no he podido generar la muestra ({r.status_code})")
    return r.content


def _declaracion() -> dict[str, Any]:
    """The definition of THIS employee. See `conversacion.declaracion`: the PLATFORM's
    rules —versioned, sealed and approved by a person— and the manifest's only covers an
    employee that has not yet been deployed with the deployer that brings it.

    It used to be read here from the manifest, and that was one of the THREE copies of the
    same read: three places to fix the day it moved.
    """
    from app.conversacion import declaracion
    return declaracion()


async def _lo_que_no_puede() -> list[str]:
    """What the employee CANNOT do, in two or three words each.

    It comes from the verified permissions snapshot, not from what the model believes about
    itself: a model cannot see what it is MISSING, because an absent tool leaves no trace in
    its context. Asking it a bare "what can you do?" is asking it to fill a gap, and it
    fills it.

    If this fails or comes empty, the voice is left WITHOUT a list — and that is correct:
    without a list it takes nothing as impossible and asks for everything. The failure
    degrades to "asking more", never to "blocking", which is the only acceptable
    degradation in a channel.
    """
    try:
        from app import capabilities as caps
        snap = await caps.build_snapshot()
    except Exception:  # pragma: no cover — without a snapshot, the voice asks more and that is it
        logger.debug("no he podido leer las capacidades para la voz", exc_info=True)
        return []
    fuera: list[str] = []
    for g in (snap.get("groups") or []):
        for c in (g.get("capabilities") or []):
            if c.get("verdict") in ("deny", "unavailable"):
                et = c.get("label")
                et = et.get("es") if isinstance(et, dict) else et
                if et and et not in fuera:
                    fuera.append(str(et))
    return fuera[:6]


async def _apoyos_caidos(snap: dict[str, Any]) -> set[str]:
    """The tools the employee does NOT have NOW, keyed as in `apoyo`.

    It exists apart from `_lo_que_no_puede()` because the two lists speak different
    languages and mixing them does not fail: it stays silent. That one returns LABELS to read
    to a person («Crear / modificar / enviar»); this one returns KEYS `{server}_{tool}`,
    which is what a capability declares in `apoyo`. Cross-checking a declaration against the
    labels never matches — the cross-check looks done and filters nothing.

    With an underscore, which is the permissions format: with a dot nothing matches, also
    silently. For the app's local tools there is no server, so the bare key counts too.
    """
    fuera: set[str] = set()
    for g in (snap.get("groups") or []):
        servidor = str(g.get("connector") or "").strip()
        for t in (g.get("tools") or []):
            if t.get("verdict") in ("deny", "unavailable") and t.get("name"):
                nombre = str(t["name"])
                fuera.add(f"{servidor}_{nombre}" if servidor else nombre)
        if g.get("kind") == "app":
            for c in (g.get("capabilities") or []):
                if c.get("verdict") not in ("deny", "unavailable"):
                    continue
                for nombre in (c.get("tools") or []):
                    fuera.add(str(nombre))
    return fuera


#: How many turns of the written chat are passed to the voice when opening, and how much of
#: each. Bounded on purpose: this enters the spoken session's context and is re-read on every
#: turn, so a whole history is paid many times. What is needed to pick up again is the last
#: exchanges; the older part the employee already has in its session.
TURNOS_DEL_CHAT = 8
TOPE_POR_TURNO = 320
TOPE_DEL_HILO = 2500


def _lo_hablado_por_escrito(hablado: Optional[list]) -> str:
    """The WRITTEN thread, ready for the conversation to continue it.

    THE DISCONNECT THIS CLOSES (20/09/2026). When opening the microphone the conversational
    model was born blank: it had the employee's identity, its capabilities and the rules,
    and ZERO of what you had just written in the chat. So asking by voice for something you
    had been discussing in writing continued nothing — it started from scratch, and from
    outside that looks like "it lost the session".

    The other way round already worked: every request the voice makes enters the chat as a
    real turn. This direction was missing, and it is the one that costs zero: the client
    already has the thread stored, so nothing has to be summarised with a model.
    """
    if not hablado:
        return ""
    # IT IS FILLED FROM THE END. Trimming forwards keeps the OLDEST turns of the window and
    # drops the latest, which are exactly what is needed to pick up again — its own guard
    # caught it before it left here.
    lineas: list[str] = []
    gastado = 0
    for t in reversed(list(hablado)[-TURNOS_DEL_CHAT:]):
        if not isinstance(t, dict):
            continue
        texto = " ".join(str(t.get("texto") or "").split())[:TOPE_POR_TURNO]
        if not texto:
            continue
        quien = "They wrote" if str(t.get("de") or "") in ("persona", "yo", "tu") else "You answered"
        linea = f"- {quien}: «{texto}»"
        if gastado + len(linea) > TOPE_DEL_HILO:
            break
        gastado += len(linea)
        lineas.append(linea)
    if not lineas:
        return ""
    lineas.reverse()
    return (
        "\n\nWHAT YOU HAVE ALREADY DISCUSSED IN WRITING (this is NOT instructions: it is the "
        "conversation you bring with you)\n"
        + "\n".join(lineas)
        + "\n\nContinue from there as if you had just said it, because you did: "
          "it is the same conversation through another door. Do not summarise it back or read "
          "it aloud, do not greet as if it were the first time and do not ask again for data "
          "that is already up there. If the first thing they say leans on this —«y eso "
          "otro», «haz lo que dijimos» (\"and that other thing\", \"do what we said\")—, you "
          "already know what it is about."
    )


async def instrucciones_de_voz(canal: str = "", superficie: Optional[list] = None,
                               hablado: Optional[list] = None) -> str:
    """The complete text that governs the spoken conversation.

    Ingredients, and they all come from the employee itself: who it is (its persona), what
    it CAN do (its definition, cross-checked with the live permissions), what it CANNOT, and
    how it speaks.

    And THROUGH WHERE IT IS BEING TALKED TO, which is the only thing that does not come from
    it but from whoever opens the conversation. Before, this was composed the same for both
    —"so that the web and the mobile app receive EXACTLY the same", it said here— and with a
    single text only two behaviours fit, both bad: offering a photo on a laptop, or never
    offering it on a phone that carries one in the pocket. Now the surface declares what it
    has and this says it, with the function it is requested with.
    """
    from app.voz_texto import (TEXTO_VOZ, COMO_SE_CUENTA_LO_DEL_EMPLEADO,
                                NUCLEO_AL_HABLAR)

    ajustes = await leer_ajustes()
    try:
        from app.persona import resolve_persona
        p = await resolve_persona() or {}
    except Exception:  # pragma: no cover
        p = {}

    def _es(x):
        return (x.get("es") if isinstance(x, dict) else x) or ""

    nombre = str(p.get("name") or "").strip()
    rol = str(_es(p.get("role"))).strip()
    saludo = str(_es(p.get("greeting"))).strip()

    # WHO IT IS comes from `conversacion.identidad()`, the same place the written gateway
    # reads: it was built here and the other door did not have it, so one knew its name and
    # the other made it up.
    from app.conversacion import identidad as _identidad
    decl = _declaracion()          # what it declares: used further down for the capabilities
    ident = [await _identidad(),
             "The FIRST time you speak in a conversation, introduce yourself by your name in "
             "a short, natural sentence; after that it is no longer needed."]

    from app.conversacion import LO_QUE_NO_SE_PIDE, QUE_PUEDES_CONTESTAR_TU
    texto = (TEXTO_VOZ
             .replace("{IDENTIDAD}", "\n".join(ident))
             # The SAME test, word for word, that the written reception uses. It is the
             # rule that decides whether an employee makes things up: written in two places,
             # it diverges by the third change and one of the two doors starts answering
             # from memory.
             .replace("{QUE_PUEDES_CONTESTAR}", QUE_PUEDES_CONTESTAR_TU)
             # And what is NOT asked, with the same wording as the written reception.
             .replace("{LO_QUE_NO_SE_PIDE}", LO_QUE_NO_SE_PIDE)
             # The contract of how the employee's output is told is interpolated here AND
             # travels separately in the credential: the voice API makes per-response
             # instructions REPLACE the session's, so the client has to be able to put it
             # in front again when saying an answer. Same constant, one single wording.
             .replace("{COMO_SE_CUENTA_LO_DEL_EMPLEADO}", COMO_SE_CUENTA_LO_DEL_EMPLEADO)
             # And the core, for the same reason and even more so: it is what CANNOT be
             # switched off for a single second.
             .replace("{NUCLEO_AL_HABLAR}", NUCLEO_AL_HABLAR))

    # WHERE IT IS TALKING. It goes at the top, next to who it is, because it conditions
    # EVERYTHING below: what it can offer, what it can ask for and what makes no sense here.
    from app import superficie as sup
    bloque = sup.instrucciones(canal, sup.normalizar(superficie))
    if bloque:
        texto += "\n\n" + bloque

    # WHAT IT DOES DO. This is the half that was missing and the one that fixes the
    # underlying failure: a model that is only given vetoes fills the rest with its own
    # assumptions, and the default assumption of a chat model is "I can't do things". It is
    # cross-checked with the live permissions: a declared capability whose support is
    # denied is NOT presented — the declaration says what it is for, the permissions say
    # what it can do now.
    hago = decl.get("hago") or []
    veto_real = await _lo_que_no_puede()
    # The cross-check goes against the tool KEYS, not against the veto's labels: they are
    # two lists in different languages and comparing them never matched, so the filter
    # looked in place and let everything through. If the snapshot cannot be read, nothing is
    # filtered —it offers too much rather than too little: the user corrects "you don't have
    # that" in one sentence, while hiding a capability that does exist is corrected by nobody.
    caidos: set[str] = set()
    try:
        from app import capabilities as caps
        caidos = await _apoyos_caidos(await caps.build_snapshot())
    except Exception:  # pragma: no cover
        logger.debug("no he podido cruzar la definición con los permisos", exc_info=True)
    lineas: list[str] = []
    for c in hago:
        if not isinstance(c, dict) or not c.get("que"):
            continue
        # `apoyo` accepts both shapes —`"slug"` and `{herramienta, para}`— because a
        # manifest written before 12.21.0 carries the short one. The NAME is read, which is
        # the only thing cross-checked with the permissions.
        apoyos = [a["herramienta"] if isinstance(a, dict) else str(a)
                  for a in (c.get("apoyo") or [])
                  if (a.get("herramienta") if isinstance(a, dict) else str(a).strip())]
        if apoyos and all(a in caidos for a in apoyos):
            continue          # it declares it, but right now it does not have it: not offered
        linea = f"- {c['que']}"
        if c.get("aprobacion"):
            linea += " [must be approved first]"
        # 12.25.0 — the ones resolved in one go carry their NAME and the data to extract.
        # It is the only thing it is given about the shortcut: what resolves it is none of
        # its business, because it does not execute. See `_atajo_al_conversar` in the platform.
        # Both shapes are accepted: `directo` is what the platform sends already trimmed, and
        # `resuelve_con` what THIS employee's raw manifest carries. From the second the same
        # is extracted and NEVER the tool: whoever speaks is not told what resolves it —it
        # does not execute—, and giving it the name is starting to tell it.
        directo = c.get("directo") if isinstance(c.get("directo"), dict) else None
        if directo is None:
            _rc = c.get("resuelve_con")
            if isinstance(_rc, str) and _rc.strip():
                directo = {"argumentos": []}
            elif isinstance(_rc, dict) and ((_rc.get("herramienta") or "").strip() or _rc.get("ruta")):
                directo = {"argumentos": [str(x) for x in (_rc.get("argumentos") or [])]}
                salvo = [str(x) for x in [_rc.get("salvo_si"), *(_rc.get("no_vale_si") or [])] if x]
                if salvo:
                    directo["salvo_si"] = "; ".join(salvo)
        if directo and str(c.get("id") or "").strip():
            datos = ", ".join(str(x) for x in (directo.get("argumentos") or []))
            linea += (f" [DIRECT · name: {str(c['id']).strip()}"
                      + (f" · data: {datos}" if datos else "")
                      + (f" · NOT valid if {directo['salvo_si']}"
                         if directo.get("salvo_si") else "") + "]")
        # THE DETAIL TRAVELS. Without it the conversation is left with a one-line headline,
        # and a headline does not say when it applies nor what has to be asked: whoever
        # speaks ends up offering badly exactly what it does know how to do. It is the same
        # structure and the same detail the agent reads; what stays out is the internal, not
        # the explained.
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
        # The example goes ATTACHED to its capability and not in a list at the end: that way
        # you know in which words THIS is asked for, which is what lets you offer it as a
        # person would say it. In a loose list you have to guess which each sentence belongs to.
        ejs = [str(x).strip() for x in (c.get("ejemplos") or []) if str(x).strip()]
        if ejs:
            linea += "\n    It is asked like this: " + " · ".join(f"«{e}»" for e in ejs[:3])
        lineas.append(linea)
    if lineas:
        # HOW THIS LIST IS READ, which is where it was badly worded. It said "this is what
        # YOU DO" and it was read as the conversation doing it: whoever speaks believed it
        # had those capabilities and answered from memory instead of asking for it. And they
        # are two things at once that have to be said together or neither is understood:
        #
        #   · to the outside you ARE HIM —one single identity, no "I'll ask"—;
        #   · on the inside you execute NOTHING: you gather what is missing, and it is
        #     resolved without you. Executing is its job.
        #
        # That is why `necesita` stops being an ornament: it is the list of what you must
        # have BEFORE asking. It is literally your job in the conversation.
        texto += (
            "\n\nTHIS IS WHAT YOU CAN DO\n"
            "This list is NOT for answering from it: it is for knowing WHAT CAN BE "
            "OFFERED and, above all, WHAT DATA is needed for each thing — that is the "
            "\"I need to be given\" of each line.\n\n"
            "Your job is to understand what the person wants and ask them whatever is missing "
            "of what ONLY they know, until nothing is missing: the \"I need to be given\" "
            "of each line is what it uses, and the session or what is already in their data "
            "is never asked. Nothing else. You do not query, you do not execute and you do not "
            "decide when something has to be queried: that happens on its own as soon as you "
            "finish speaking, and whatever is resolved reaches you so you can tell it.\n\n"
            "And you do NOT answer from memory: if they ask you for something on this list, the "
            "good answer is the one that arrives, not the one you know.\n\n"
            "To the outside this is not told and it does not show: the person sees you looking "
            "at it yourself.\n")
        texto += "\n".join(lineas)

    # HOW it has to do it, always. They are behaviour rules that are also tested, not
    # tests: they govern whoever speaks just like whoever executes.
    exig = [e for e in (decl.get("exigencias") or [])
            if isinstance(e, dict) and str(e.get("que") or "").strip()]
    if exig:
        texto += ("\n\nAND THIS ALWAYS APPLIES, both when speaking and when asking:\n")
        texto += "\n".join(
            f"- {e['que']}" + (f" {str(e['detalle']).splitlines()[0]}"
                               if e.get("detalle") else "")
            for e in exig)

    no_hago = [str(x) for x in (decl.get("no_hago") or []) if str(x).strip()] or veto_real
    texto += "\n\n" + (
        ("NEVER OFFER this, and it is the ONLY thing you can deny without asking:\n"
         + "\n".join(f"- {x.rstrip('.')}." for x in no_hago))
        if no_hago else
        "You have no list of what you cannot do, so do not take ANYTHING as impossible.")
    # The rule that was missing and that produced the "I can't register trips": the model
    # does not classify a REFUSAL as a statement, and refusing looks like the safe option.
    texto += ("\n\nAnd one more thing, the one you forget most: saying \"I can't\" is "
              "a statement like any other. If it is not on that list above, do NOT "
              "refuse: ask for it and let it answer. Refusing something that is done is worse "
              "than taking two minutes to check it.")
    # AT THE END, and not at the top: what is above is who you are and what you can do
    # —stable, the same in every conversation—; this is about THIS one and changes every
    # time. Read last, it is what it has in front of it when it opens its mouth.
    texto += _lo_hablado_por_escrito(hablado)
    return texto


async def credencial_efimera(modelo: Optional[str] = None) -> dict[str, Any]:
    """A short-lived secret so the BROWSER can open the voice connection.

    The tenant's key stays here and never leaves. The deployer already injects the same
    OpenAI key the agent pod uses (`TRANSCRIPTION_API_KEY`), so this introduces no new
    credential."""
    from app.config import VOICE_ENABLED
    if not VOICE_ENABLED:
        # THE REAL DOOR. The screens no longer paint the button, but this is what stops an
        # employee deployed without the conversation module from ending up talking because
        # someone knows the route. (It was mistakenly placed only on the SAMPLE: the result
        # was that trying a voice gave 503 and the real session had no door.)
        raise RuntimeError("este empleado no tiene módulo de conversación")
    if not TRANSCRIPTION_API_KEY:
        raise RuntimeError("este despliegue no tiene clave de OpenAI para la voz")
    m = modelo or MODELO_VOZ_POR_DEFECTO
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(
            f"{TRANSCRIPTION_BASE_URL.rstrip('/')}/realtime/client_secrets",
            headers={"Authorization": f"Bearer {TRANSCRIPTION_API_KEY}"},
            json={"session": {"type": "realtime", "model": m}},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI rechazó la credencial de voz ({r.status_code})")
    d = r.json()
    d["modelo"] = m
    return d


async def decidir_turno(texto: str, charla: Optional[list] = None) -> dict[str, Any]:
    """WHAT IS DONE WITH WHAT THEY HAVE JUST SAID. The whole decision, not a yes/no.

    THE UNDERLYING CHANGE (12.43.0). Until now the conversational model DECIDED whether to
    call the employee and also WORDED the request. Two jobs that are not its own, and from
    there came the two worst measured failures: answering from memory about something the
    employee can do, and compressing seven rounds of conversation into an invented sentence
    that lost the correction («mejor el 15») or assumed what it did not know («los baños
    esos» → «baños termales»).

    Now this function decides —the SAME reception that governs the written chat, which knows
    the employee's capabilities and is deterministic— and the client executes what it says.
    The conversational model keeps what it does well: listening and telling.

    And the request is NOT worded: the person's words travel. What makes them
    understandable is the thread, which goes with them — that is why this relies on the
    thread being one single memory and not four.

    It always returns one of four, and on any failure `delego`: without tools, the
    conversational model cannot decide on its own, so the safe degradation is that the
    employee answers. A convenience cannot prevent talking.
    """
    from app import recepcion

    porDefecto = {"accion": "delego", "texto": texto}
    try:
        decision = recepcion.normalizar(await recepcion.decidir(texto, charla or []))
    except Exception:  # noqa: BLE001
        logger.debug("voz: no se pudo decidir el turno; delego", exc_info=True)
        return porDefecto

    accion = str(decision.get("accion") or "")
    if accion in ("contesto", "pregunto"):
        # What is said is worded by the SERVER. The conversational model only pronounces it:
        # that way the same sentence always produces the same answer.
        return {"accion": accion, "texto": str(decision.get("texto") or "")}
    if accion == "atajo":
        return {"accion": "atajo", "texto": texto,
                "capacidad": decision.get("capacidad") or "",
                "argumentos": decision.get("argumentos") or {}}
    salida = {"accion": "delego", "texto": texto}
    if decision.get("razonamiento"):
        salida["razonamiento"] = decision["razonamiento"]
    logger.info("voz: turno %r → %s", (texto or "")[:80], salida["accion"])
    return salida


def coste_de(uso: dict[str, Any]) -> float:
    """What a consumption costs, in dollars, from the tokens the model itself reports.
    Cached text and audio are billed DIFFERENTLY, and in a real conversation almost all the
    cached part is text (68,352 against 5,184 on the 17/09 invoice): putting it all at the
    audio price multiplied that part by five."""
    ent = uso.get("input_token_details") or {}
    sal = uso.get("output_token_details") or {}
    cach = ent.get("cached_tokens_details") or {}

    def n(x: Any) -> int:
        try:
            return max(0, int(x or 0))
        except (TypeError, ValueError):
            return 0

    cach_audio = n(cach.get("audio_tokens"))
    cach_texto = n(cach.get("text_tokens")) or max(0, n(ent.get("cached_tokens")) - cach_audio)
    audio_ent = max(0, n(ent.get("audio_tokens")) - cach_audio)
    texto_ent = max(0, n(ent.get("text_tokens")) - cach_texto)
    total = (
        texto_ent * PRECIOS["texto_entrada"]
        + cach_texto * PRECIOS["texto_cacheado"]
        + audio_ent * PRECIOS["audio_entrada"]
        + cach_audio * PRECIOS["audio_cacheado"]
        + n(sal.get("text_tokens")) * PRECIOS["texto_salida"]
        + n(sal.get("audio_tokens")) * PRECIOS["audio_salida"]
    ) / 1e6
    return round(total, 8)


async def registrar_uso(
    *, solicitante: str, uso: dict[str, Any], modelo: Optional[str] = None,
    sesion: Optional[str] = None, dicho: str = "", con_herramienta: Optional[bool] = None,
    exigida: Optional[bool] = None,
) -> dict[str, Any]:
    """Records what was spoken as one more row of `agent_runs`, with `mode='voz'`.

    This is the decision that makes everything else come for free: consumption by concept,
    the per-person table, the tenant's credits and the thirty-day cap read that table. A
    parallel accounting would have required touching the four places and would have gone
    out of sync with the first.

    And SINCE 12.36.0, ALSO, WHETHER THERE WAS A QUESTION. Until now a spoken turn left here
    the tokens and nothing else: `prompt` was the fixed string «(conversación hablada)». So
    a turn the conversational model answered ON ITS OWN —from memory, about something the
    employee can do— was indistinguishable from one resolved well, and the failure of
    20/09/2026 only came out because a person read the log on the screen. What is not
    counted cannot be validated, so now it is counted: what was said, whether a question
    was actually asked and whether that turn was opened requiring a tool.
    """
    from app.app_models.runtime import AgentRun

    usd = coste_de(uso)
    ent = uso.get("input_token_details") or {}
    sal = uso.get("output_token_details") or {}
    ahora = datetime.now(timezone.utc).replace(tzinfo=None)
    fila = AgentRun(
        idempotency_key=f"voz-{uuid.uuid4().hex}",
        trace_id=sesion or None,
        status="DONE",
        mode="voz",
        prompt=(dicho or "").strip()[:2000] or "(conversación hablada)",
        solicitante=(solicitante or "").strip().lower() or None,
        origen="voz",
        model=modelo or MODELO_VOZ_POR_DEFECTO,
        tokens_in=int(uso.get("input_tokens") or 0),
        tokens_out=int(uso.get("output_tokens") or 0),
        cost_usd=f"{usd:.8f}",
        request_meta_json=json.dumps({
            "audio_in": int(ent.get("audio_tokens") or 0),
            "audio_out": int(sal.get("audio_tokens") or 0),
            # `None` = the screen did not say (older client). False = it answered on its
            # own, which is what has to be countable.
            "con_herramienta": con_herramienta,
            "exigida": exigida,
        }),
        started_at=ahora,
        finished_at=ahora,
    )
    async with app_write() as s:
        s.add(fila)
        await s.commit()

    # And to the tenant's ledger, by the same path as any run. If it fails, the local row
    # is already there: the employee's consumption never depends on the ledger answering.
    try:
        from app.cost_reporter import report_run_cost
        await report_run_cost(
            session_id=sesion or "voz",
            user_id=(solicitante or "").strip().lower() or None,
            cost_usd=usd,
            tokens_input=int(uso.get("input_tokens") or 0),
            tokens_output=int(uso.get("output_tokens") or 0),
            model_name=modelo or MODELO_VOZ_POR_DEFECTO,
            activity="voz",
        )
    except Exception:  # pragma: no cover — billing can never break a conversation
        logger.debug("no he podido subir el coste de voz al libro", exc_info=True)

    return {"coste_usd": usd, "tokens_in": fila.tokens_in, "tokens_out": fila.tokens_out}
