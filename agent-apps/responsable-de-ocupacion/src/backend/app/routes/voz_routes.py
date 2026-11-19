"""Talking to the employee (v12.12.0).

``GET  /api/voz/ajustes``     how it sounds — anyone who can talk to it may read it.
``PATCH /api/voz/ajustes``    only the ADMIN changes it: it is the employee's identity.
``POST /api/voz/credencial``  the ephemeral secret to open the connection from the browser.
``POST /api/voz/turno``       does the voice answer this turn itself or must it ask?
``POST /api/voz/uso``         what was spoken, into the cost ledger.

THE AUDIO DOES NOT GO THROUGH HERE, and that is deliberate: it goes from the browser to the
model over a single WebRTC connection. That single pipe is what makes the browser's echo
cancellation work —it subtracts from the microphone what the browser itself plays through
the speaker— and what leaves interruption in the hands of the voice server. An earlier
attempt took the audio out of there to save money: it cost 335 lines of compensation,
twelve hand-tuned parameters and an unfixable bug (the employee heard itself and cut
itself off mid-sentence).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException

from app import voz as vozmod
from app.auth import TokenPayload, require_admin, require_project_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voz", tags=["voz"])


@router.get("/ajustes")
async def get_ajustes(user: TokenPayload = Depends(require_project_access)):
    """How the employee sounds, plus the catalogs to render the form."""
    return {
        "ajustes": await vozmod.leer_ajustes(),
        # Name and hint of each voice. The hint narrows it down; the fact is hearing it.
        "voces": [{"id": k, "como_es": v} for k, v in vozmod.VOCES.items()],
        "acompanamientos": list(vozmod.ACOMPANAMIENTOS),
        "arranques": list(vozmod.ARRANQUES),
        "modelo": vozmod.MODELO_VOZ_POR_DEFECTO,
    }


# PATCH and not PUT on purpose: it merges what arrives and leaves the rest as it was.
# With PUT, sending a single field would have to wipe the other three, and a form that
# saves «the voice» would leave the employee without style or accompaniment.
@router.patch("/ajustes")
async def patch_ajustes(
    cuerpo: dict = Body(default_factory=dict),
    user: TokenPayload = Depends(require_admin),
):
    """Admin only: the voice is the employee's identity, not a preference of whoever
    listens to it. Values outside the catalog are ignored and the previous one stays."""
    return {"ajustes": await vozmod.guardar_ajustes(cuerpo)}


@router.post("/credencial")
async def post_credencial(
    cuerpo: dict = Body(default_factory=dict),
    user: TokenPayload = Depends(require_project_access),
):
    """The short-lived secret the browser or the phone opens the connection with.

    It travels together with the settings so the screen carries NO decision written
    inside it: which voice, how it speaks and with which model all come from here.

    AND NOW WITH THE SURFACE. Whoever opens the conversation declares where it comes in
    from and what it has at hand —`{"canal": "movil", "capacidades": ["ubicacion", "camara"]}`—,
    because that is the only thing the employee cannot know about itself: on a phone the
    person carries their location and a camera, on a laptop usually not. It is DECLARED
    and not inferred from the `User-Agent`: a mobile browser is not the app, and a denied
    permission cannot be seen from the server.

    Besides the instructions, the response brings the TOOLS that surface can resolve.
    Without them the agent would know location exists and could not ask for it, which is
    the «promises something it cannot do» one layer further down."""
    try:
        cred = await vozmod.credencial_efimera()
    except Exception as ex:  # noqa: BLE001
        # The reason, not a mute 500: without an OpenAI key in the deployment this cannot
        # work, and whoever opens it deserves to read why instead of seeing a stalled orb.
        raise HTTPException(status_code=503, detail=str(ex)) from ex
    cred["ajustes"] = await vozmod.leer_ajustes()
    # The instructions travel WITH the credential: that way the screen carries not a single
    # decision written inside it, and changing how the employee speaks does not require
    # publishing a version of the app. For a few hours this was not sent and the session
    # opened with NO context at all: the employee answered like a generic assistant.
    canal = str((cuerpo or {}).get("canal") or "").strip().lower()
    declaradas = (cuerpo or {}).get("capacidades") or []
    # AND THE WRITTEN THREAD. Whoever opens sends it because they are the one who has it:
    # the chat conversation lives in the client (the browser and the phone keep it, the
    # employee does not expose a history). Without this the talker was born blank and
    # continued nothing of what you had just typed — from outside that looks like «it lost
    # the session».
    hablado = (cuerpo or {}).get("hablado") or (cuerpo or {}).get("charla") or []
    cred["ajustes"]["instrucciones"] = await vozmod.instrucciones_de_voz(
        canal, declaradas, hablado if isinstance(hablado, list) else [])
    # THE TALKER HAS NO TOOLS, and this is what makes that true (12.43.0).
    #
    # It had two: asking the employee for something and asking the device for something.
    # Both are gone, for different reasons:
    #
    # · it no longer calls the employee — the reception decides and the client executes.
    #   That way answering from memory stops being forbidden and becomes IMPOSSIBLE;
    # · the device is asked by the EMPLOYEE, which is the one that knows whether it needs
    #   it and what for. The client tells it on every turn what it can provide, so it does
    #   not ask for what is not there.
    #
    # Without tools it cannot call where it should not, nor waste a shot on the device,
    # nor make up a request. A whole family of bugs that no longer has anywhere to happen.
    cred["herramientas"] = []
    # THE CONTRACT FOR HOW THE EMPLOYEE'S OUTPUT IS RELATED, separately and raw.
    #
    # It already goes inside `instrucciones`, but the client needs it ON ITS OWN because in
    # the voice API per-response instructions REPLACE the session ones (they do not add up).
    # When opening the response that says what the employee wrote it must be put in front
    # again, or the voice speaks without it. And it is sent from here —not written in the
    # client— so that there is ONE single wording: the same rule in two places diverges by
    # the third change.
    from app.voz_texto import COMO_SE_CUENTA_LO_DEL_EMPLEADO, NUCLEO_AL_HABLAR
    cred["ajustes"]["como_se_cuenta"] = COMO_SE_CUENTA_LO_DEL_EMPLEADO
    # THE CORE, separate from the previous one and for the same reason squared: it goes in
    # front of ALL responses —also the literal ones and the asides—, which is where the
    # session contract stops existing. Without this, the voice twice in one conversation
    # made up a limitation of its own that does not exist («I cannot search in real time»)
    # with the freshly fetched data right in front of it.
    cred["ajustes"]["nucleo"] = NUCLEO_AL_HABLAR
    return cred


@router.get("/muestra")
async def get_muestra(voz: str, user: TokenPayload = Depends(require_project_access)):
    """Three seconds of a voice, to choose it by ear.

    It goes over GET so the browser can hand it to an `<audio>` as is and it plays as it
    arrives, without waiting for the whole file."""
    from fastapi.responses import Response
    try:
        audio = await vozmod.muestra_de_voz(voz)
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(ex)) from ex
    return Response(content=audio, media_type="audio/mpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.post("/turno")
async def post_turno(
    cuerpo: dict = Body(default_factory=dict),
    user: TokenPayload = Depends(require_project_access),
):
    """What the person just said, through the SAME reception as what is typed.

    The screen calls this BEFORE opening the response, and with what it returns it decides
    whether to open it requiring a tool. That is the mechanism: not one more instruction
    the model can ignore, but taking away its option to answer from memory on the turns
    where the work belongs to the employee.

    It costs a short call to a small model —the same second the written chat already
    pays— and it is silence after you stop talking. That is the price, and it is paid
    knowingly."""
    texto = str((cuerpo or {}).get("texto") or "").strip()
    if not texto:
        raise HTTPException(status_code=400, detail="falta lo que se ha dicho (texto)")
    charla = (cuerpo or {}).get("charla")
    # The WHOLE decision: what is done and with which text. The client executes it as is
    # —it does not interpret it— and that is why the same sentence always produces the same.
    return await vozmod.decidir_turno(texto, charla if isinstance(charla, list) else [])


@router.post("/uso")
async def post_uso(
    cuerpo: dict = Body(default_factory=dict),
    user: TokenPayload = Depends(require_project_access),
):
    """What a conversation consumed, into the ledger.

    The screen sends TOKENS —a fact the model itself returns— and the price is set here.
    The other way round, anyone could bill themselves whatever they wanted and the frontend
    would have to be redeployed every time a rate changed.

    Consumption is ALWAYS attributed to whoever carries the token, never to a user that
    comes in the body: otherwise a person could charge their spending to someone else.
    """
    uso = cuerpo.get("usage") or cuerpo.get("uso") or {}
    if not isinstance(uso, dict) or not uso:
        raise HTTPException(status_code=400, detail="falta el consumo (usage)")
    quien = (getattr(user, "email", "") or getattr(user, "sub", "") or "").strip()
    def _tri(clave):
        v = cuerpo.get(clave)
        return bool(v) if isinstance(v, bool) else None

    return await vozmod.registrar_uso(
        solicitante=quien,
        uso=uso,
        modelo=str(cuerpo.get("model") or "") or None,
        sesion=str(cuerpo.get("session_id") or "") or None,
        # What turns a turn answered from memory into something COUNTABLE. An older client
        # sends none of this and the row comes out anyway, with the fields as `null`:
        # «unknown» and «it answered on its own» are not the same and are not mixed.
        dicho=str(cuerpo.get("dicho") or ""),
        con_herramienta=_tri("con_herramienta"),
        exigida=_tri("exigida"),
    )
