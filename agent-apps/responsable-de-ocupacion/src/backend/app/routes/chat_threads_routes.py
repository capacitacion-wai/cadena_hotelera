"""Conversation threads: list, create, rename, archive, delete (v9.1.0).

A thread belongs to ONE person. The owner check lives in `app.chat_threads`, not here:
spreading it across every endpoint is exactly how the one that forgot to do it slips through.

These routes require `require_project_access`, same as the chat: whoever can talk to the
employee can organize THEIR conversations. No need to be admin — their threads are theirs.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import chat_threads as hilos
from app import prueba_controlada as _pc
from app.auth import require_project_access, TokenPayload
from app.routes.agent_routes import _resolve_chat_user_email

router = APIRouter(prefix="/api/agent/threads", tags=["threads"])


class CrearHilo(BaseModel):
    titulo: str | None = None


class RenombrarHilo(BaseModel):
    titulo: str


@router.get("")
async def listar_hilos(
    incluir_archivados: bool = False,
    user: TokenPayload = Depends(require_project_access),
):
    """My conversations, most recent first."""
    email = _resolve_chat_user_email(user)
    return {"hilos": await hilos.listar(email, incluir_archivados=incluir_archivados)}


@router.post("")
async def crear_hilo(
    body: CrearHilo,
    request: Request,
    user: TokenPayload = Depends(require_project_access),
):
    email = _resolve_chat_user_email(user)
    try:
        fila = await hilos.crear(email, titulo=(body.titulo or "").strip())
    except hilos.LimiteDeHilos as exc:
        # 409 and not 400: the request is not malformed, the current state does not
        # allow it. And the message says what to do, which is what avoids the ticket.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if getattr(request.state, "prueba_controlada", False):
        # 15.8.0 — a test conversation of the code agent: the ONLY one a controlled test may talk in.
        _pc.abrir_hilo(email, fila.id)
    return {"id": fila.id, "titulo": fila.title}


@router.get("/{hilo_id}/history")
async def historial_del_hilo(
    hilo_id: str,
    limite: int = 100,
    # v10.2.0 — which cards the caller can render, comma-separated. Pruned HERE and not
    # when saving: what is stored is the raw text, so the same conversation shows with a
    # map on the phone and as clean prose on a client that lacks them. Absent = none.
    tarjetas: str = "",
    user: TokenPayload = Depends(require_project_access),
):
    """Repaints a thread's conversation. It is what lets it continue from phone to laptop.

    Brings the text, not the process: tools and reasoning only exist on the live channel.
    The interface says so; hiding it would make people think the employee did less.
    """
    email = _resolve_chat_user_email(user)
    # v12.0.0 — no special case: the "default" alias resolves to the caller's PER-PERSON
    # row (which always exists), then the same ownership check as any other. Before, precisely
    # the default thread skipped `obtener`, and its shared id did the rest.
    real = await hilos.resolver_hilo(email, hilo_id)
    if await hilos.obtener(email, real) is None:
        raise HTTPException(status_code=404, detail="Esa conversación no existe.")
    from app.tarjetas import podar
    soportadas = [t for t in (x.strip() for x in tarjetas.split(",")) if t]
    turnos = await hilos.historial(email, real, limite=limite)
    for t in turnos:
        if isinstance(t.get("respuesta"), str):
            t["respuesta"] = podar(t["respuesta"], soportadas)
    # v12.4.0 — up to where the employee only remembers a summary. It travels WITH the
    # history because that is what makes the fold survive a reload and also show on the other
    # device: without it, each client would have to note it on its own and the one that did
    # not compact would never find out. NULL = never compacted.
    return {"turnos": turnos, "parcial": True,
            "compactado_en": await hilos.compactado_en(email, real)}


@router.get("/current")
async def hilo_actual(user: TokenPayload = Depends(require_project_access)):
    """The conversation to OPEN on entry, decided on the server (v12.0.0).

    It is the one most recently used by this person that is not archived, or their default
    conversation (which always exists). Having it here, and not in each client, is what makes
    the phone and the web open the SAME conversation: before, each remembered its own in its
    local cache and the web always went back to 'default' showing another thread's timeline.

    Also returns `id_por_defecto`, so a client can go back to it when archiving or deleting
    the current one without having to guess an id."""
    email = _resolve_chat_user_email(user)
    return await hilos.hilo_actual(email)


@router.patch("/{hilo_id}")
async def renombrar_hilo(
    hilo_id: str,
    body: RenombrarHilo,
    user: TokenPayload = Depends(require_project_access),
):
    email = _resolve_chat_user_email(user)
    if not await hilos.renombrar(email, hilo_id, body.titulo):
        raise HTTPException(status_code=404, detail="Esa conversación no existe.")
    return {"ok": True}


@router.post("/{hilo_id}/archive")
async def archivar_hilo(
    hilo_id: str,
    request: Request,
    archivar: bool = True,
    user: TokenPayload = Depends(require_project_access),
):
    """Archiving removes it from the list WITHOUT deleting anything. It is what is offered
    when the cap is reached.

    Returns `id_por_defecto`: where to go if the archived one was the open one. It is said
    HERE and not deduced on the client because archiving the default one rotates it
    (v12.3.0), so the one the client had in its list is no longer the right one.
    """
    email = _resolve_chat_user_email(user)
    prueba = getattr(request.state, "prueba_controlada", False)
    if prueba and not _pc.es_hilo_de_prueba(email, hilo_id):
        raise HTTPException(status_code=403, detail="A controlled test only archives its own test conversation.")
    res = await hilos.archivar(email, hilo_id, archivar_si=archivar)
    if res is None:
        raise HTTPException(status_code=404, detail="Esa conversación no existe.")
    if prueba:
        _pc.cerrar_hilo(email, hilo_id)       # the test is over: its marker is worth nothing from now on
    return {"ok": True, **res}


@router.delete("/{hilo_id}")
async def borrar_hilo(
    hilo_id: str,
    user: TokenPayload = Depends(require_project_access),
):
    """Deletes the conversation —including the default one, which is ROTATED (v12.3.0): it
    disappears from the list and continues in an empty one, whose id goes in `id_por_defecto`.

    Up to 12.2.1 this was a 409 "the default conversation cannot be deleted; use Clear
    context", and that was a rule of the program, not of the user: deleting the conversation
    and starting from scratch are the same thing said two ways.
    """
    email = _resolve_chat_user_email(user)
    res = await hilos.borrar(email, hilo_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Esa conversación no existe.")
    return {"ok": True, **res}
