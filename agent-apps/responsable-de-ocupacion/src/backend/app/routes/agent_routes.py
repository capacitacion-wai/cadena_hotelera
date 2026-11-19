"""Chat proxy: turns the simple-API SSE into a stable UI vocabulary.

COPIED VERBATIM by ``wai-agent-app-builder`` into every generated
agent-app at ``src/backend/app/routes/agent_routes.py``. Do not rewrite.

The browser must NEVER see raw simple-API events. Every event written
to the stream is one of:

* ``session``    — the proxy session id (so the SPA can reconnect)
* ``text_delta`` — token-level text (``content``, ``isReasoning``)
* ``tool_call``  — the agent invoked a tool (``name``, ``args``)
* ``tool_result``— the tool returned (``name``, ``result``)
* ``subagent``   — activity INSIDE a ``task`` subagent (``call_id`` of the
                   parent task row, ``kind``: tool|text, ``name``,
                   ``status``, ``args``, ``text``)
* ``complete``   — the agent finished (``runId``, ``cost``, ``tokens``,
                                       ``model``, ``durationMs``)
* ``error``      — anything went wrong (``message``)

Identity contract (v7+):
========================
Each chat session is bound to the **logged-in operator's email** at
creation time via ``proxy.create_session(user_email=..., role='interactive')``.
The OpenCode wai-impersonation plugin then injects that email as
``acting_user`` into every wai-* MCP tool call, so the agent only
sees data the operator already has access to. There is NO pod-level
identity mutex; concurrent chats from different users run in parallel
without serialisation.

Server-side sessions (multi-user):
==================================
This pod is multi-user, so the OpenCode session is owned SERVER-SIDE by
``app.agent_sessions.SessionManager``, keyed by the operator email
(``interactive:<email>``). Benefits: a page reload keeps context (the
registry remembers the operator's session), each operator writes into
their OWN ``/workspace`` folder (no cross-user artefact collisions), and
"Limpiar contexto" maps to ``reset=true`` which rotates the session.
The ``session_id`` the SPA posts is advisory only.
"""

from __future__ import annotations

import asyncio

import httpx
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.timeutil import iso_utc
from app.agent_proxy import (
    AGENT_DEFAULT_MODEL,
    AGENT_NAME,
    FatalAgentError,
    RecoverableAgentError,
    get_agent_proxy,
    is_turn_time_limit,
)
from app.agent_sessions import (
    build_memory_block,
    get_session_manager,
    interactive_scope,
    is_contract_stale,
    workspace_block,
)
from app.scoped_knowledge import build_scoped_knowledge_block
from app import chat_threads
from app.auth import require_admin, require_project_access
from app.chat_runtime import ChatTurn, get_turn, start_turn
from app.tool_usage import anotar as anotar_uso

logger = logging.getLogger(__name__)

async def esperar_al_agente(px, durante_s: float = 120.0, cada_s: float = 3.0) -> bool:
    """Poll the agent's health until it is back (True) or time runs out (False). Used when the
    chat finds the agent's pod restarting (it restarts to apply a connector just connected)."""
    esperado = 0.0
    while esperado < durante_s:
        await asyncio.sleep(cada_s)
        esperado += cada_s
        if await px.check_health():
            logger.info("chat: agent back after a restart (%ss); sending the prompt again", int(esperado))
            return True
    return False

router = APIRouter(prefix="/api/agent", tags=["agent"])

# SSE response headers shared by the POST /chat and the GET /chat/stream
# reconnect endpoint. ``X-Accel-Buffering: no`` defeats nginx/ingress
# buffering so token deltas reach the browser as they happen.
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class ChatAttachment(BaseModel):
    """A file the operator attached to a chat turn. The SPA already uploaded
    the bytes into the shared workspace via ``POST /workspace/upload`` (so the
    agent pod can read them off the same PVC) and sends only the resulting
    workspace-relative path + light metadata here — never the bytes."""
    workspace_path: str
    filename: str | None = None
    size: int | None = None
    mime: str | None = None
    # v10.5.0 — WHERE the photo was taken. Without this, "tell me where it was taken" has no
    # possible answer: the phone recompresses the image when attaching it and the EXIF is lost
    # on the way, so the agent opens the file and finds no trace of the position. It travels
    # as separate data, not inside the file, and only if the client sends it: optional on
    # purpose so as not to break those that don't know about it.
    lat: float | None = None
    lon: float | None = None
    tomada_en: str | None = None


class ChatLocation(BaseModel):
    """A position shared on purpose in THIS turn. It is never stored or tracked."""
    lat: float
    lon: float
    # Uncertainty radius in metres: without it, "you are here" with 2 km of error misleads.
    precision_m: float | None = None


class TurnoHablado(BaseModel):
    """One turn of the voice conversation: who spoke and what they said.

    `de` is `tu` (the person) or `el` (the reception that answers by voice, NOT the employee).
    The distinction matters: what the reception answered was not said by the employee, and if
    it attributes it to itself it ends up taking for granted something it does not know."""

    de: str = ""
    texto: str = ""


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    # Files the operator attached to THIS turn (already staged in the
    # workspace by the SPA). Surfaced to the agent as an "ATTACHED FILES"
    # block appended to the prompt it sees — never to the text shown in chat.
    attachments: list[ChatAttachment] = []
    # When true the operator asked to "Limpiar contexto" — rotate the
    # server-side session so the conversation starts fresh. The
    # ``session_id`` the SPA sends is advisory only: the source of truth
    # is the per-operator session the SessionManager keeps server-side
    # (keyed by the logged-in email), so a page reload keeps context.
    reset: bool = False
    # Set when ``message`` is the user's answer to a clarifying question the
    # agent asked (a ``question`` event carried this ``request_id``). The turn
    # is then routed as a question REPLY (resuming the blocked run) instead of
    # a new prompt — so the agent continues where it paused.
    request_id: str | None = None
    # v9.1.0 — which THREAD this turn happens in. Absent = the default thread, which is the
    # usual conversation: an old client (or a freshly updated employee) carries on exactly
    # where it left off, without migrating a single row.
    thread_id: str | None = None
    # v10.2.0 — which CARDS this client can render (see `app/tarjetas.py`). The interface is
    # generic and global, but not all clients are equal: the phone has a native map and the
    # web not necessarily. Absent = a client that does not declare it, and that one gets ALL
    # of them stripped: that is what keeps a card inside the message from polluting anyone.
    # Empty list = the same, said on purpose.
    tarjetas: list[str] | None = None
    # v12.22.0 — how much THINKING this request asks for (see `app/razonamiento.py`).
    # The model is already chosen and does not change; this picks, on top of that model, the
    # reasoning level of this particular message. Absent = the one the person pinned; and if
    # they have none either, the employee's. Ids: `rapido` | `normal` | `profundo`.
    razonamiento: str | None = None
    # v12.25.0 — THROUGH WHAT the conversation happens. Decides how much it writes (by voice,
    # always brief) and whether the wording has to be done here or by the talker. Absent =
    # screen.
    canal: str | None = None
    # What the TALKER recognised when reading the request, when there is something: the
    # capability declared with a shortcut and the data it extracted. It names; the app runs.
    capacidad: str | None = None
    argumentos: dict | None = None
    # And how much reasoning it thinks it needs. It is a PROPOSAL: it only applies if nobody
    # has chosen anything (see `razonamiento.resolver`).
    razonamiento_sugerido: str | None = None
    # Where the writer is, when they shared it on purpose from the phone.
    #
    # It travels as DATA and not inside the text for a concrete reason: pasting "my location
    # is 41.0, 28.9" into the message forces the agent to extract two numbers again from a
    # sentence it has just read, and sometimes it doesn't. With the field, the position
    # arrives exact and with its accuracy.
    #
    # Added on 31/08/2026 because the mobile app was already sending it and here it was
    # silently DISCARDED —Pydantic ignores what it does not declare—: the employee answered
    # "I have a previous location in memory, but it could be outdated" with the good one
    # right in front of it.
    location: ChatLocation | None = None
    # v12.1.0 — what this client can DO, besides rendering (`tarjetas`). Today: `ubicacion`,
    # meaning "if the employee asks for it, this client can resolve it". It travels per turn
    # and not in the employee's prompt: the prompt belongs to the employee and the client
    # changes (web, phone, a channel, an autonomous run with nobody in front). Absent = it can
    # do nothing, which is right for an old client: the employee will ask in words, as before.
    capacidades: list[str] | None = None
    # v12.48.0 — WHAT WAS SAID BEFORE by voice, when the reception resolved a few turns by
    # itself and now delegates. Without this the employee got ONLY the last sentence ("in the
    # centre and for two nights") and answered with whatever it remembered from another
    # conversation: the person had said Madrid, tomorrow, two adults and three children, and
    # it asked for the check-in date "in Budapest" (22/09/2026, measured on a real call). The
    # thread did not fix it: the turns the reception resolves do not enter the employee's
    # memory, precisely because they do not cost it a turn.
    hablado: list[TurnoHablado] | None = None
    # v12.55.0 — the push-notification token of the PHONE sending the message. When the turn
    # ends it is notified ("your answer is ready"): meanwhile the person is in another app,
    # iOS has frozen WAI's and nobody else can tell them. See `app/aviso_push.py`.
    aviso_push: str | None = None


def _chat_location_block(loc: "ChatLocation | None") -> str:
    """«UBICACIÓN DEL USUARIO» (user location) block for the turn, like the attachments one.

    It goes to the AGENT, not to the chat: the conversation still shows what the person
    wrote. The label `UBICACIÓN DEL USUARIO` stays byte-identical (it is the documented
    marker); the rest is English. It says explicitly that it is CURRENT so the agent does not
    confuse it with what it remembers from earlier turns — which is exactly what it did when
    it did not get it."""
    if loc is None:
        return ""
    precision = (f" (accuracy ±{int(loc.precision_m)} m)"
                 if loc.precision_m is not None else "")
    return (
        "\n\n---\nUBICACIÓN DEL USUARIO (user location, shared by them in THIS message, "
        "CURRENT):\n"
        f"- Coordinates: {loc.lat:.6f}, {loc.lon:.6f}{precision}\n"
        "- It is their current position: use it as is and do NOT replace it with any you "
        "remember from before, nor ask for it again.\n"
    )


def _chat_voz_block(hablado: "list[TurnoHablado] | None", mensaje: str) -> str:
    """"SPOKEN EARLIER BY VOICE" block, like the location one: it goes to the AGENT.

    Why it is needed. By voice, the reception resolves by itself the turns it can (asking for
    a missing piece of data, answering something it already knows) and only calls the
    employee when there is work. That is what makes the conversation fast, and it has a
    price: those turns do NOT go through the employee, so they are not in its memory. Sending
    it only the last sentence was sending it the end of a film.

    It is also told, explicitly, that this overrides what it remembers: without that line the
    employee preferred a city from a previous conversation over the one the person had just
    said."""
    turnos = [x for x in (hablado or []) if (x.texto or "").strip()]
    # The last sentence is the one that already goes as the message: repeating it would make
    # the employee read it twice and take it for two different requests.
    while turnos and turnos[-1].texto.strip() == (mensaje or "").strip():
        turnos.pop()
    if not turnos:
        return ""
    lineas = []
    for x in turnos[-12:]:
        quien = "The person" if (x.de or "").lower() in ("tu", "tú", "persona", "user") else "The reception"
        lineas.append(f"- {quien}: «{x.texto.strip()}»")
    return (
        "\n\n---\nSPOKEN EARLIER BY VOICE, in this same conversation and a moment ago:\n"
        + "\n".join(lineas)
        + "\n\nThe message above is the LAST sentence of that conversation: read it together "
          "with everything before it. Do NOT ask again for what you have already been told "
          "there, and what was settled overrides any data you remember from previous "
          "conversations. What \"the reception\" answered was said by it, not by you.\n"
    )


def _chat_attachments_block(attachments: list[ChatAttachment]) -> str:
    """Render an 'ATTACHED FILES' block for files the operator attached to a
    chat turn (already staged in the workspace by ``/workspace/upload``).
    Lists each file by its ``/workspace``-relative path and tells the agent how
    to read binary office/PDF files so it never stalls on them — the same
    contract channel inbound attachments use (channels/executor)."""
    lines: list[str] = []
    has_doc = False
    has_image = False
    for a in attachments or []:
        path = (a.workspace_path or "").strip().lstrip("/")
        if not path or ".." in path.split("/"):
            continue
        name = a.filename or path.rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in ("docx", "pptx", "xlsx", "pdf"):
            has_doc = True
        if ext in ("png", "jpg", "jpeg", "webp", "gif"):
            has_image = True
        extra = ", ".join(
            x for x in (a.mime, (f"{a.size} bytes" if a.size else None)) if x
        )
        suffix = f" ({extra})" if extra else ""
        # The position goes ATTACHED to the file, not in a loose note: with several photos in
        # the same turn you have to be able to say which one was taken where.
        donde = ""
        if a.lat is not None and a.lon is not None:
            donde = f" — taken at {a.lat},{a.lon}"
            if a.tomada_en:
                donde += f" on {a.tomada_en}"
        lines.append(f"- /workspace/{path}{suffix}{donde}  [{name}]")
    if not lines:
        return ""
    how = ["Read them from your workspace if they are relevant to answer."]
    if has_doc:
        how.append(
            "For .docx/.pptx/.xlsx use `unzip -p <path> …` and for .pdf "
            "`pdftotext <path> -` (do NOT read them as raw text); if you cannot "
            "extract it in one attempt, carry on with what you have."
        )
    if any(a.lat is not None and a.lon is not None for a in attachments):
        how.append(
            "Some image carries WHERE it was taken, in coordinates: use them as they are —do "
            "not deduce them from what is seen— and, if you have a maps connector, turn them "
            "into a specific place before answering."
        )
    if has_image:
        # v7.94.0 — screen-capture help: the chat can attach UI screenshots.
        # The employee must LOOK at them (multimodal read), not shell-dump them.
        how.append(
            "The images are usually SCREENSHOTS of the user's application: open "
            "them with your read tool to SEE them (you are multimodal; do NOT use "
            "`cat`/`strings`) and reason about what they show — screen, fields, "
            "menus, visible error messages — to answer concretely and actionably."
        )
    return (
        "\n\n--- ATTACHED FILES ---\n"
        "I am attaching these files (they are already in your workspace):\n"
        + "\n".join(lines)
        + "\n"
        + " ".join(how)
        + "\n--- end of attachments ---"
    )


def _adjuntos_de_esta_persona(
    attachments: list[ChatAttachment] | None, subdir: str,
) -> list[ChatAttachment]:
    """SECURITY: only the attachments that live in the folder of WHOEVER writes.

    `workspace_path` is set by the CLIENT, and until here only `..` was rejected. The
    attachments block tells the agent "read them", and the agent's pod sees the whole disk:
    it was enough to pass `sessions/users/<token(B)>/exports/x.pdf` —the token is computed
    from an email `/api/agent/users` gives to any member— for the employee to READ another
    person's files and moreover have them indexed in the memory of whoever asked. The
    per-folder isolation is organizational, not a sandbox: it has to be enforced here. What
    does not hang from this person's folder is discarded without executing anything; it is
    logged for diagnosis. It is the same rule as `/chat-upload-to` (`workspace_routes`)."""
    base = (subdir or "").strip().strip("/")
    if not base:
        return []
    propios: list[ChatAttachment] = []
    for a in attachments or []:
        crudo = (a.workspace_path or "").strip().lstrip("/")
        partes = [x for x in crudo.split("/") if x not in ("", ".")]
        if not partes or ".." in partes:
            continue
        norm = "/".join(partes)
        if norm == base or norm.startswith(base + "/"):
            propios.append(a)
        else:
            logger.warning("chat: adjunto FUERA de la carpeta de la persona, descartado: %s "
                           "(carpeta permitida: %s)", norm, base)
    return propios


async def _index_chat_attachments(
    user_email: str, attachments: list[ChatAttachment], run_id: int | None,
    context_message: str | None = None,
    origin: str | None = None,
) -> int:
    """Register chat-attached files in the user's ARTIFACTS memory
    (``UserFile``, ``source='chat'``) so they persist beyond this turn and
    surface in the memory block + the Operations "Memoria" tab — the SAME
    contract inbound channel attachments use
    (``channels/poller._index_inbound_files``). Idempotent on
    ``(user_email, workspace_path)``; best-effort, never blocks the turn.

    v7.96.0 — rows carry SEMANTICS: the description composes the origin, the
    chat message the file arrived with (the \"why/when\" anchor) and a content
    sniff, and is embedded for dense recall (``build_file_semantics``).
    v7.97.0 — reused by the workspace-panel upload (``origin`` overridable);
    returns how many rows were written."""
    e = (user_email or "").strip().lower()
    if not e or not attachments:
        return 0
    import hashlib as _hashlib
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from app.app_database import app_write
    from app.app_models.memory import UserFile
    from app.memory_utils import build_file_semantics
    from app import memory_embeddings as _e
    from app.agent_locale import resolve_agent_language, rotulo
    lang = await resolve_agent_language()
    origin = origin or rotulo(lang, "adjuntado_chat")
    try:
        root = ""
        try:
            from app.workspace_fs import workspace_root
            root = workspace_root() or ""
        except Exception:
            root = ""
        written = 0
        # PREPARE outside the lock. `build_file_semantics` reads from disk and the embedder
        # is a local model (CPU): doing it inside `app_write` —the process's single write
        # queue— blocked EVERY other writer while a turn's attachments were being indexed.
        # It is the same cause KOM-44 measured as 14 s waits (INC-VIG-25). The lock protects
        # the write, not the computation.
        preparados = []
        for a in attachments:
            path = (a.workspace_path or "").strip().lstrip("/")
            if not path or ".." in path.split("/"):
                continue
            key = _hashlib.sha256(f"{e}|{path}".encode()).hexdigest()
            desc = build_file_semantics(
                path, root=root,
                origin=origin,
                user_message=context_message, lang=lang,
            ) or origin
            try:
                blob = _e.pack(_e.embed(desc))
                emodel = _e.model_id()
            except Exception:  # pragma: no cover
                blob, emodel = None, None
            preparados.append((a, path, key, desc, blob, emodel))
        if not preparados:
            return 0
        async with app_write() as session:
            for a, path, key, desc, blob, emodel in preparados:
                res = await session.execute(
                    sqlite_insert(UserFile).values(
                        idempotency_key=key,
                        user_email=e,
                        created_by_run_id=run_id,
                        filename=a.filename or path.rsplit("/", 1)[-1],
                        workspace_path=path,
                        mime=a.mime,
                        size=a.size,
                        source="chat",
                        description=desc,
                        embedding=blob,
                        embed_model=emodel,
                    ).on_conflict_do_nothing(
                        index_elements=[UserFile.user_email, UserFile.workspace_path],
                    )
                )
                written += int(res.rowcount or 0)
            await session.commit()
        return written
    except Exception:  # pragma: no cover — never block the chat turn
        logger.debug("chat attachment index failed (ignored)", exc_info=True)
        return 0


def _resolve_chat_user_email(user: Any) -> str:
    """Extract the logged-in user's email from the auth dependency.

    The simple-API rejects sessions whose ``user.email`` doesn't exist
    on the tenant; we surface a 401 here so the SPA shows a meaningful
    "please log in again" instead of a 500.
    """
    candidates = (
        getattr(user, "email", None),
        getattr(user, "mail", None),
        getattr(user, "username", None),
    )
    if isinstance(user, dict):
        candidates = (*candidates, user.get("email"), user.get("mail"))
    for c in candidates:
        if isinstance(c, str) and "@" in c:
            return c.strip().lower()
    raise HTTPException(
        status_code=401,
        detail="Cannot resolve a chat user identity from the request",
    )


def _budget_notice_stream(message: str) -> StreamingResponse:
    """R-BUDGET-1/2: with the credit budget EXHAUSTED, instead of cutting the chat with an
    HTTP 402 —which the SPA shows as "Conversation error HTTP 402", opaque to the operator—
    a normal chat MESSAGE is delivered explaining the limit (the person's text, Spanish). The
    operator sees an actionable explanation in the thread (not an error code) and the turn
    closes cleanly."""
    from app.config import AGENT_COST_PER_CREDIT

    async def _gen():
        yield _sse({"type": "text_delta", "content": message,
                    "isReasoning": False, "stepIndex": 0})
        yield _sse({"type": "complete", "runId": None, "cost": 0, "credits": 0,
                    "costPerCredit": AGENT_COST_PER_CREDIT,
                    "tokens": {"input": 0, "output": 0}, "model": None, "durationMs": 0})

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/chat")
async def chat_with_agent(
    payload: ChatRequest,
    request: Request,
    user=Depends(require_project_access),
):
    """Stream the agent answer as Server-Sent Events.

    Every chat turn is also written to ``agent_runs`` with
    ``mode='chat'`` so ``/runs`` is a unified audit trail.
    """
    # R-BUDGET-1: when the monthly credit budget is exhausted the employee does
    # NOTHING — including boss chat. The Operations "Consumo" section (which
    # reads /budget, never gated) still shows the spend + how to raise it.
    # 7.122.0: the barrier is TWO-layered — the employee's own monthly cap and
    # the PROJECT balance (account balance, backend V68) — with a
    # DISTINGUISHABLE wording so the operator knows which limit to raise.
    from app.budget import budget_block_reason
    _reason = await budget_block_reason()
    if _reason == "project_budget":
        return _budget_notice_stream(
            "⚠️ **Saldo de la cuenta agotado.** El proyecto ha consumido todo el saldo "
            "disponible de su cuenta, así que ninguno de sus empleados puede procesar "
            "consultas (no es el límite mensual de este empleado). Amplía el saldo de la "
            "cuenta para reanudar; el consumo se ve en **Operaciones → Consumo**."
        )
    if _reason is not None:
        return _budget_notice_stream(
            "⚠️ **Límite de presupuesto alcanzado.** Este empleado ha consumido todo su "
            "presupuesto de créditos del periodo actual (últimos 30 días), así que no puedo "
            "procesar la consulta. Un administrador puede ampliar el presupuesto (se aplica al "
            "redesplegar el empleado); el consumo se ve en **Operaciones → Consumo**."
        )
    proxy = get_agent_proxy()
    mgr = get_session_manager()
    chat_email = _resolve_chat_user_email(user)
    # 14.23.0 — an INVOCATION by API (`POST /invocar`, `app/invocacion.py`) runs through this same
    # turn. Only the route sets it (request.state, never the client's body): the reception and
    # the shortcuts would answer in prose, and the invocation needs the agent's JSON.
    _invocacion = getattr(request.state, "invocacion", None)

    # R-BUDGET-2 (v7.23.0): per-user cap. An operator can set a personal credit
    # limit per person in Usuarios (directory metadata) so one boss/member can't
    # drain the whole employee's shared budget. Independent of, and additional
    # to, the global budget gate above. No cap set → no-op.
    from app.budget import user_cap_status
    _cap = await user_cap_status(chat_email)
    if _cap and _cap["over"]:
        return _budget_notice_stream(
            f"⚠️ **Límite personal de créditos alcanzado.** Has llegado a tu límite personal "
            f"de créditos ({_cap['cap']:g}) para el periodo actual (últimos 30 días). Pide a un "
            "administrador que lo amplíe en **Usuarios**."
        )

    # Server-side session, per operator AND PER THREAD (v9.1.0). Without `thread_id` the key
    # is the usual one —the default thread—, so an old client keeps its context.
    # `reset=true` rotates. The file folder and the memory still belong to the PERSON: two
    # threads of theirs share documents and what was learned; only the context is separated.
    # Without an explicit thread it is the USUAL one, which also has a row: that way every
    # turn is attributed to a thread and its history can be rebuilt without guessing whose it was.
    # v12.0.0 — the "default" alias (or no thread) resolves to THIS PERSON's default
    # conversation (own row, created if missing), and then the SAME ownership check as any
    # other thread. There is no special case any more: it was exactly where the single row
    # for the whole pod and the `thread_id='default'` shared between people slipped in.
    hilo_id = await chat_threads.resolver_hilo(chat_email, payload.thread_id)
    if await chat_threads.obtener(chat_email, hilo_id) is None:
        # 404 and not 403: saying "it exists but it is not yours" already tells something about another.
        raise HTTPException(status_code=404, detail={"code": "conversation_not_found",
                                                     "message": "Esa conversación no existe."})
    if getattr(request.state, "prueba_controlada", False):
        # 15.8.0 — a CONTROLLED test chat (`prueba_controlada`): only in ITS test conversation, on a session
        # that asks every write of its person.
        from app import prueba_controlada as _pc
        if not _pc.es_hilo_de_prueba(chat_email, hilo_id):
            raise HTTPException(status_code=403, detail={
                "code": "NOT_A_TEST_CONVERSATION",
                "message": "A controlled test only talks in the test conversation it created."})
        from app.agent_sessions import resolve_tool_permissions as _tabla
        request.state.prueba_controlada_sesion = await _pc.config_de(chat_email, hilo_id, await _tabla())

    # 15.11.0 — a message while THIS conversation's turn is still working does not replace it.
    # Until 15.10 `start_turn` superseded it: it cancelled only the app's task, the agent kept
    # working with nobody showing it, and the new message queued behind it in the same session —
    # the person saw neither the old work nor an answer (COBSA, 09/11/2026: «¿dónde lo has dejado?»
    # threw away 20 minutes of a 12.590-row lookup). Now the client is told, re-attaches to the
    # live turn and keeps the text; stopping is the Stop button (`/chat/abort`), which does abort
    # the agent. A reply to a question (`request_id`) resumes the paused run, so it passes.
    if not payload.request_id:
        _vivo = await get_turn(interactive_scope(chat_email, hilo_id or None).scope_key)
        if _vivo is not None and _vivo.is_running:
            raise HTTPException(status_code=409, detail={
                "code": TURN_IN_PROGRESS,
                "message": "The employee is still working on the previous message in this "
                           "conversation. Wait for it to finish or stop it."})
    # Cap of ACTIVE threads. Existing is cheap; being alive takes an OpenCode session in the
    # 1 GiB agent pod — the same one the autonomous work is already bounded by. The SESSION
    # of the least used thread is evicted, never the thread: it stays in the list and is
    # re-primed when writing in it again. The turn is neither rejected nor made to wait.
    for sobra in await chat_threads.sesiones_a_desalojar(chat_email, hilo_id):
        try:
            await mgr.delete(sobra)
            logger.info("hilos: desalojada la sesión %s para dejar sitio a %s",
                        sobra, hilo_id)
        except Exception:  # noqa: BLE001 — evicting can never bring a turn down
            logger.warning("hilos: no se pudo desalojar %s", sobra, exc_info=True)
    await chat_threads.tocar(hilo_id, chat_email)
    scope = interactive_scope(chat_email, hilo_id or None)
    # SECURITY: the attachments are paths the client sets; only those in this person's folder
    # count (see `_adjuntos_de_esta_persona`). It is decided ONCE and used in both places
    # —the memory index and the block the agent reads— so they do not disagree.
    adjuntos = _adjuntos_de_esta_persona(payload.attachments, scope.workspace_subdir)
    try:
        # 15.7.0 — a READ-ONLY test (`invocar` with the delegation's claim) gets its own session
        # with the read-only permission map and marker (`prueba_solo_lectura`). Only the route sets it.
        try:
            handle = await mgr.resolve(scope, force_new=bool(payload.reset), **_solo_lectura_de(request))
        except Exception as exc:
            from app.prueba_controlada import NoDisponible
            if isinstance(exc, NoDisponible):
                raise HTTPException(status_code=409, detail={
                    "code": "PRUEBA_CONTROLADA_NO_DISPONIBLE",
                    "message": "This employee's agent pod cannot pause its writes for a test yet (its agent "
                               "image is older): the test chat does not run."}) from exc
            raise
        session_id = handle.session_id
        # Whether THIS request has to run the silent priming turn: either it
        # minted a brand-new session, or it reused one primed with an OLDER
        # prompt contract (v7.143.0 — see ``SessionHandle.needs_reprime``).
        needs_prime = handle.needs_prime
    except RecoverableAgentError as exc:
        # This 503 is almost always the same: the agent's pod is not serving yet because the
        # employee has just been deployed or restarted. The raw text —"cannot reach simple
        # API: ..."— tells nothing to whoever is in front, and it is a VERY visible moment:
        # the employee just created, someone opens its chat and writes. What is happening and
        # what to do is told; the technical detail goes to the log, not to the user's face.
        logger.warning("chat: el agente aún no atiende (%s)", exc)
        raise HTTPException(
            status_code=503,
            # A CODE the screen says in the person's language (`errors.api.agent_starting`);
            # the Spanish text is only the fallback for a client that does not know the code.
            detail={"code": "agent_starting",
                    "message": ("Tu empleado está arrancando y su agente todavía no atiende. "
                                "Espera unos segundos y vuelve a enviar el mensaje.")},
        )
    except FatalAgentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # R-CHAT-3 (v7.2): session self-heal.
    #
    # The simple-API sidecar GCs idle sessions and forgets every
    # session ID when its pod restarts. A SPA that cached the old
    # session_id then posts to ``/api/agent/chat`` with that ID,
    # we forward it to ``stream_interactive`` and the sidecar
    # responds 404 ``"session 'ses_*' unknown to simple API"``.
    # The operator used to see a confusing error and had to press
    # "Limpiar contexto" by hand. Now we detect that exact 404
    # *inside the SSE generator* (so we still get to emit a fresh
    # ``session`` event to the SPA) and transparently mint a new
    # session, retry the original message once, and let the
    # conversation continue. The user's prompt is preserved end
    # to end — no history is lost.
    user_message = payload.message
    # CHAT MUST NOT LEAK TECHNICAL WORKSPACE INFO (v6.28.0).
    #
    # Earlier builds appended the workspace pin to EVERY user turn
    # (``user_message + workspace_block(...)``). Chat-tuned models, faced
    # with a trivial turn like "hola" followed by a long technical
    # instruction, tended to "answer" by restating that instruction —
    # so the operator saw an internal "CARPETA DE TRABAJO: /workspace/…"
    # block as the agent's reply.
    #
    # The SPA still displays ``user_message`` verbatim. Chat-mode rules
    # are injected ONCE silently in ``_prime_workspace``; each turn
    # only adds a compact ``[INTERNO run_id=…]`` line for MCP tools.
    run_id, run_token = await _create_chat_run(session_id, user_message, thread_id=hilo_id or None,
                                               solicitante=chat_email,
                                               origen=getattr(request.state, "origen", None) or "chat")
    if run_id:
        from app.memory_utils import persist_run_memory_user
        await persist_run_memory_user(run_id, scope.memory_user)
    # Register attached files in the user's ARTIFACTS memory (source='chat') so
    # they persist past this turn — same as inbound channel attachments.
    if adjuntos:
        await _index_chat_attachments(
            scope.memory_user, adjuntos, run_id,
            context_message=user_message,
        )
    from app.chat_prompt import (
        build_chat_turn_prompt,
        normalize_chat_markdown,
        strip_internal_chat_leak,
    )
    if payload.request_id:
        # This turn ANSWERS a clarifying question: send the chosen option
        # VERBATIM so OpenCode matches it to the question's option. No
        # ``[INTERNO …]`` trailer (it would corrupt the option label).
        agent_prompt = user_message
    else:
        # Attachments go to the AGENT only (appended to the message body before
        # the silent internal trailer) — the SPA keeps showing ``message``
        # verbatim and renders its own attachment chips on the user bubble.
        agent_message = (user_message
                         + _chat_attachments_block(adjuntos)
                         + _chat_location_block(payload.location)
                         + _chat_voz_block(payload.hablado, user_message))
        # The ROLE of whoever writes travels in the internal line of EVERY turn: without it
        # the employee offers a member screens and actions only an administrator opens, and
        # the person hits a permission after having been promised them. Cheap (one word) and
        # it is the only thing that is not pruned.
        try:
            from app.users_directory import resolve_role
            _role = await resolve_role(scope.memory_user)
        except Exception:  # pragma: no cover — never break the turn over this
            _role = None
        agent_prompt = build_chat_turn_prompt(
            agent_message,
            memory_user=scope.memory_user,
            run_id=run_id,
            run_token=run_token,
            workspace_subdir=scope.workspace_subdir,
            role=_role,
            tarjetas=payload.tarjetas,
            capacidades=payload.capacidades,
        )
    started_at = time.monotonic()
    started_wall = time.time()

    # How much THIS message thinks: what the client asks for wins; if it asks for nothing,
    # what the person left set; and if not that either, empty = whatever the employee has.
    from app import razonamiento as _razonamiento
    nivel, lo_puso = await _razonamiento.resolver(
        chat_email, payload.razonamiento, payload.razonamiento_sugerido)
    canal = (payload.canal or "").strip().lower() or "pantalla"
    # How much it WRITES. The employee's if it has set it; if not, the channel's.
    largo = await _razonamiento.longitud_para(canal)
    variante = _razonamiento.variante(nivel, largo)

    # The turn's account (see `app/medicion.py`). It is opened HERE, with the clock, so the
    # total measures the same as what whoever asks sees —including the first wait—, and not
    # only what the stream takes.
    from app.medicion import MedicionDelTurno
    medicion = MedicionDelTurno(
        run_id=run_id, hilo=hilo_id, origen="chat",
        # The model is not decided here (it is set by the session, Operations → Model):
        # the `step_done`s say it as they run, and it is filled in there.
        modelo="", nivel=nivel,
        esfuerzo=_razonamiento.esfuerzo_de(nivel),
        caracteres_pedidos=len(agent_prompt or ""),
        lo_puso=lo_puso, longitud=largo, canal=canal,
    )

    # R-CHAT-4 (v6.66.0): the turn runs in a DETACHED background task
    # (``app.chat_runtime``) so the browser can switch tabs / refresh
    # without cancelling it. ``runner`` appends every UI event to the
    # turn's buffer; the HTTP request below merely *subscribes* to that
    # buffer. It returns ``"complete"`` / ``"error"`` so the runtime can
    # mark the turn terminal and wake any reconnecting subscriber.
    async def runner(turn: ChatTurn) -> str:
        # R-CHAT-3: the self-heal branch below rebinds ``session_id``
        # to a freshly-minted id after a stale-session 404. Because
        # we *write* to ``session_id`` inside this nested function,
        # Python would otherwise treat it as a local variable for
        # the WHOLE function — including the very first read on the
        # line that emits the initial ``session`` SSE event,
        # producing ``UnboundLocalError`` before any byte ever
        # reaches the SPA. Hoisting the ``nonlocal`` declaration
        # makes the outer scope the source of truth and lets the
        # heal branch update it in place.
        nonlocal session_id, needs_prime
        # The reception can propose a level when nobody had chosen one, and that happens
        # INSIDE the turn (it is the one reading the message).
        nonlocal nivel, variante, lo_puso
        # And the prompt, so what the reception handled without it seeing can be attached.
        nonlocal agent_prompt
        total_cost = 0.0
        total_input = 0
        total_output = 0
        model_id = ""
        streamed_text = ""

        # CARDS filter (v10.2.0, see `app/tarjetas.py`). It holds back the ```wai-card block
        # from when it opens until it closes: the text arrives in chunks and without this the
        # client would see loose JSON and half a card arrive. It decides with what THIS client
        # declared it can paint; what is STORED is still the raw text, so the history keeps
        # the card even if this turn's client does not receive it.
        from app.tarjetas import Filtro as _FiltroTarjetas
        from app.chat_prompt import _LEAK_MARKERS as _FUGAS
        # It is given THIS TURN's INTERNAL BLOCK, not a list of known markers. That way the
        # cut is a rule —"what we put in does not go out"— and not a collection of cases to be
        # extended every time the model repeats a different piece.
        _inyectado = agent_prompt[agent_prompt.find("[INTERNO"):] \
            if "[INTERNO" in agent_prompt else ""
        _tarjetas = _FiltroTarjetas(
            payload.tarjetas if payload.tarjetas is not None else [],
            cortar_en=_FUGAS, inyectado=_inyectado)

        # R-CHAT-2 (v7.1): per-turn step tracking. The chat UI now
        # renders the agent's response as a sequence of "steps"
        # (reasoning + tool calls) + a final answer bubble, mirroring
        # the OpenCodeChatPanel timeline but trimmed for business
        # users. Each ``step_done`` event from the simple-API proxy
        # closes the current step and starts a new one; the SPA
        # buckets text_delta / tool_call / tool_result by the
        # ``step_index`` we tag here so it doesn't have to count
        # SSE events itself.
        #
        # The step counter starts at 0 and increments on every
        # ``step_done`` so the FIRST step (index 0) holds the
        # reasoning + tools the model emits before its first
        # step-finish boundary. The last step often contains only
        # the final assistant answer (no tools), which the SPA
        # treats specially.
        step_index = 0
        # The text part being painted, and the step in which it STARTED. It is needed for the
        # tail the cards filter releases on closing: it arrives when the last `step_done` has
        # already moved the counter, so without this it would be emitted as if it were from
        # another step, the client would open a new block and its `join('\n\n')` would put a
        # break in the middle of the text —inside a URL, the link stops being one—. The tail
        # is of the SAME text: let it say so.
        parte_texto = ""
        paso_del_texto = 0

        # Text passthrough (v8): mirror the proven "code agent" pattern.
        # NO echo suppression — earlier prompt-echo stripping chopped
        # characters out of the middle of real answers ("Ventas"→"s",
        # characters out of the middle of real answers ("Ventas"→"s",
        # at RENDER time in the SPA (a text block is reasoning if flagged
        # so OR emitted in a step at/under the last tool step). Here we only
        # remove internal markers and apply path-safe markdown fixes.
        def _visible_chat_delta(raw: str) -> str:
            return normalize_chat_markdown(strip_internal_chat_leak(raw))

        def _visible_reasoning_delta(raw: str) -> str:
            if not raw:
                return ""
            return normalize_chat_markdown(strip_internal_chat_leak(raw))

        def _is_session_404(exc: Exception) -> bool:
            """Identify the simple-API's specific "I forgot this
            session" error so we can self-heal without masking real
            transport / agent failures."""
            msg = str(exc)
            return "unknown to simple API" in msg or "stream 404" in msg

        try:
            await turn.append({"type": "session", "session_id": session_id})
            # Tell the SPA we're working: the first delta can lag on long
            # reasoning / slow providers, so without this the chat looks frozen.
            await turn.append({"type": "processing"})

            # ── The conversation, also when writing ────────────────────────
            #
            # Before starting the agent, someone READS what has been written and decides: if
            # it is a courtesy or general knowledge it answers it; if a piece of data is
            # missing to be able to do it, it asks for it; if it is a capability declared as
            # direct, it names it; and if not, it delegates — which is the usual path. See
            # `app/recepcion.py`.
            _decision = {"accion": "delego"} if _invocacion else await _recepcion_decide(
                payload=payload, mensaje=user_message, chat_email=chat_email,
                hilo_id=hilo_id, medicion=medicion)
            _accion = _decision.get("accion") or "delego"
            if _accion in ("contesto", "pregunto"):
                await _cerrar_turno_corto(
                    turn, texto=_decision["texto"], origen=f"recepcion:{_accion}",
                    medicion=medicion, run_id=run_id, user_message=user_message,
                    hilo_id=hilo_id, scope=scope, chat_email=chat_email,
                    started_at=started_at, started_wall=started_wall,
                    session_id=session_id)
                return "complete"
            if _accion == "delego" and _decision.get("razonamiento") and lo_puso == "modelo":
                # Only here: if someone had chosen a level, `resolver` does not even look at this.
                nivel, lo_puso = await _razonamiento.resolver(
                    chat_email, None, _decision["razonamiento"])
                variante = _razonamiento.variante(nivel, largo)
                medicion.nivel, medicion.lo_puso = nivel, lo_puso
                medicion.esfuerzo = _razonamiento.esfuerzo_de(nivel)
            if _accion == "delego" and _decision.get("pendiente"):
                # THE MEMORY SEAM. The opencode session is the memory of the conversation: what
                # the reception handled has not gone through it, so the agent would have a gap in
                # its history and could contradict something already discussed. It is told
                # BEFORE the request, only once and only what concerns it.
                agent_prompt = _decision["pendiente"] + "\n\n" + agent_prompt

            # ── What is resolved with one call, is resolved with one call ──
            #
            # If the definition DECLARES that this capability is resolved in one go, it is
            # answered here: one call to its tool and a write-up. Without resending the
            # conversation, without passing the catalog to the model and without deliberating.
            # See `app/atajos.py` — and above all its rule 3: when in doubt, the normal path.
            if not _invocacion and await _atajo_resuelve(turn, payload=payload, canal=canal,
                                     reconocido=_decision if _accion == "atajo" else None,
                                     nivel_pedido=nivel, lo_puso=lo_puso,
                                     medicion=medicion, run_id=run_id,
                                     user_message=user_message, hilo_id=hilo_id,
                                     scope=scope, chat_email=chat_email,
                                     started_at=started_at, started_wall=started_wall,
                                     session_id=session_id):
                return "complete"

            # ── What almost every turn reads, read before the model starts ──
            #
            # The definition's `contexto_de_turno` (read-only, nothing from the request): made
            # here in parallel, as the person of the turn, and handed to the agent already done.
            # Each one saves a round trip to the model in almost every turn. Not for an answer
            # to a clarifying question: that prompt travels verbatim. See `app/contexto_de_turno.py`.
            if not payload.request_id:
                from app import contexto_de_turno as _contexto
                _bloque, _medida = await _contexto.bloque(run_id)
                if _bloque:
                    agent_prompt = agent_prompt + "\n" + _bloque
                    medicion.contexto_previo(**_medida)

            # v6.28.0: pin the workspace folder ONCE, silently, on a
            # freshly-minted session — never in the user-visible turn. The
            # workspace path + memory + operator knowledge are stable per
            # session, so we do NOT re-pin on every turn (that burned an extra
            # silent round-trip and tokens per turn for no benefit).
            if needs_prime:
                memory = await build_memory_block(scope.memory_user)
                memory += await build_scoped_knowledge_block(
                    attending=True, memory_user=scope.memory_user,
                )
                # Write-policy rules (v7.26.0) — same behaviour in chat as in
                # autonomous runs: a vetoed write is parked via request_approval
                # (Aprobaciones inbox), never retried inline.
                try:
                    from app.mcp_policy import write_policy_block
                    memory += await write_policy_block()
                except Exception:  # pragma: no cover
                    pass
                await _prime_workspace(
                    proxy, session_id, scope.workspace_subdir, memory,
                    memory_user=scope.memory_user,
                )
                # v7.143.0 — seals the contract it was primed with. What turns "deployed" into
                # "the employee knows it": an EVOLVE that changes these instructions re-primes
                # the live session on its next message, without the operator losing the
                # conversation.
                from app.agent_sessions import stamp_prompt_contract
                await stamp_prompt_contract(scope.scope_key)
                needs_prime = False

            # R-CHAT-3 (v7.2): two-attempt loop. The first attempt
            # tries whatever ``session_id`` we resolved above. If
            # it 404s with the simple-API's specific "unknown to
            # simple API" body (e.g. because the sidecar pod
            # restarted and forgot every session), we mint a fresh
            # session, tell the SPA via a second ``session`` SSE
            # event so future messages use the new id, reset our
            # accumulators, and retry once. Any other error
            # propagates as before.
            # 15.6.0 — the turn's ceiling, read LIVE (Behaviour screen → manifest → 30 min default,
            # `app/execution_limits.py`): a change there is felt on the next message, no
            # redeploy. It was 600 s written here, and in cobsa (06/11/2026) it cut three
            # legitimate 80-step turns in one afternoon.
            from app.execution_limits import effective_time_limits as _techos
            tope_min = (await _techos())[0]
            attempt = 0
            # 15.8.0 — a controlled test chat (`prueba_controlada`): the arguments of each call, to show its
            # person what a pending write would send.
            _controlada = getattr(request.state, "prueba_controlada_sesion", None) is not None
            _args_de_llamada: dict[str, Any] = {}
            # Waiting once for an agent pod that is restarting (see below), and renewing the
            # session once, are independent: after a restart the session is usually gone too.
            esperado_al_agente = False
            sesion_renovada = False
            while True:
                attempt += 1
                stream_ended_cleanly = False
                reintentar = False
                try:
                    async for event in proxy.stream_interactive(
                        session_id, agent_prompt, timeout=tope_min * 60.0,
                        request_id=payload.request_id,
                        # How much it thinks AND how much it writes, combined in the variant
                        # id. Empty = nothing is sent and it runs with whatever the employee has.
                        nivel=variante,
                        # 15.8.0 — a controlled test chat: its asks wait for its person.
                        **({"auto_grant": False} if _controlada else {}),
                    ):
                        etype = event.get("type", "")
                        if etype == "delta":
                            delta = event.get("delta", "")
                            if not delta:
                                continue
                            streamed_text += delta
                            medicion.texto(len(delta))
                            visible = _visible_chat_delta(delta)
                            if not visible:
                                continue
                            visible = _tarjetas.consumir(visible)
                            if not visible:
                                # The whole chunk was (part of) a card: there is nothing to show
                                # yet. It is not a failure, it is the block opening.
                                continue
                            _parte = event.get("partID", "")
                            if _parte and _parte != parte_texto:
                                parte_texto = _parte
                                paso_del_texto = step_index
                            await turn.append({
                                "type": "text_delta",
                                "content": visible,
                                "isReasoning": False,
                                "stepIndex": step_index,
                                # WHICH part of the message it comes from. `stepIndex` is not
                                # enough to group: a part's tail —what the markers holder held
                                # back— can arrive after a `step_done`, and the client would
                                # paint it as a loose block ("…viajes\n\n."). With the part, a
                                # part's text is ALWAYS one block.
                                "partId": event.get("partID", ""),
                            })
                        elif etype == "reasoning":
                            # R-REASON-1: reasoning fragments arrive on
                            # their own channel from the simple-API so
                            # the chat client can render them inside the
                            # step card rather than the final-answer
                            # bubble. Echo suppression is skipped here
                            # because the prompt is never echoed inside
                            # reasoning parts.
                            r_delta = event.get("delta", "")
                            if not r_delta:
                                continue
                            medicion.senal()
                            visible_r = _visible_reasoning_delta(r_delta)
                            if not visible_r:
                                continue
                            await turn.append({
                                "type": "text_delta",
                                "content": visible_r,
                                "isReasoning": True,
                                "stepIndex": step_index,
                                "partId": event.get("partID", ""),
                            })
                        elif etype == "tool":
                            tool_name = event.get("tool") or "tool"
                            state = event.get("state") or {}
                            status = state.get("status") or "running"
                            # The OpenCode part for ONE invocation is re-emitted
                            # as its state advances (pending→running→completed),
                            # each time on the SAME ``callID``. Forward that id so
                            # the SPA dedupes the repeats into a single row (it
                            # used to append one ghost row with empty args + the
                            # real one). See Agent.tsx ``tool_call`` handler.
                            call_id = (
                                event.get("callID")
                                or event.get("call_id")
                                or event.get("id")
                            )
                            # And it is RECORDED, not only forwarded to the screen. What goes
                            # through here is the only thing that knows EVERYTHING the employee
                            # touched; the writes ledger (`tool_call_log`) only sees its own,
                            # and the run's card showed one line out of six. See
                            # `app/tool_usage.py`.
                            # A FAILED call is a closed call too. Only `completed` used to close
                            # it: an `error` fell to the opening branch (a no-op, it was already
                            # open) and the call stayed «running» until the end of the turn —
                            # measured on 07/10/2026, `consultar_viaje` charged 33-116 s per turn
                            # it never took, and its failure never showed on the run's card.
                            if status in ("completed", "error"):
                                salida = (state.get("output") or state.get("result")
                                          or state.get("error"))
                                medicion.herramienta_termina(
                                    call_id or "", tool_name, ok=(status != "error"),
                                )
                                await anotar_uso(
                                    run_id, call_id=call_id or "", tool_name=tool_name,
                                    step_idx=step_index, result=salida, terminada=True,
                                    estado=("error" if status == "error" else "ok"))
                                await turn.append({
                                    "type": "tool_result",
                                    "name": tool_name,
                                    "call_id": call_id,
                                    "result": salida,
                                    "stepIndex": step_index,
                                })
                            else:
                                entrada = state.get("input") or state.get("args")
                                if call_id:
                                    _args_de_llamada[str(call_id)] = entrada
                                medicion.herramienta_empieza(call_id or "", tool_name)
                                await anotar_uso(
                                    run_id, call_id=call_id or "", tool_name=tool_name,
                                    step_idx=step_index, args=entrada)
                                await turn.append({
                                    "type": "tool_call",
                                    "name": tool_name,
                                    "call_id": call_id,
                                    "args": entrada,
                                    "stepIndex": step_index,
                                })
                        elif etype == "subagent":
                            # v7.167.0 — the ``task`` tool runs a subagent in
                            # its OWN OpenCode session. The sidecar used to
                            # drop those events (different sessionID), so the
                            # chat showed a dead "task" row for minutes and
                            # the 30s liveness watchdog killed the stream
                            # while OpenCode kept working detached. Now they
                            # arrive tagged with the parent ``call_id`` and
                            # the SPA nests them under that row.
                            await turn.append({
                                "type": "subagent",
                                "call_id": event.get("call_id") or "",
                                "session": event.get("session") or "",
                                "kind": event.get("kind") or "tool",
                                "name": event.get("name") or "",
                                "status": event.get("status") or "",
                                "args": event.get("args") or {},
                                "text": event.get("text") or "",
                                "stepIndex": step_index,
                            })
                        elif etype == "heartbeat":
                            # Emitted while a subagent is thinking with
                            # nothing to show. Never reaches the UI as
                            # content; it exists so intermediaries don't
                            # cut an idle connection.
                            continue
                        elif etype == "step_done":
                            medicion.paso_cerrado(
                                tokens=event.get("tokens") or {},
                                coste=event.get("cost"),
                                modelo=event.get("model") or "",
                            )
                            if event.get("cost"):
                                try:
                                    total_cost += float(event["cost"])
                                except (TypeError, ValueError):
                                    pass
                            tokens = event.get("tokens") or {}
                            if tokens:
                                total_input += int(
                                    tokens.get("input", 0)
                                    or tokens.get("promptTokens", 0)
                                    or 0
                                )
                                total_output += int(
                                    tokens.get("output", 0)
                                    or tokens.get("completionTokens", 0)
                                    or 0
                                )
                            if event.get("model"):
                                model_id = event["model"]
                            # R-CHAT-2: forward to the SPA so it
                            # can close the current step bucket.
                            await turn.append({
                                "type": "step_done",
                                "stepIndex": step_index,
                                "model": event.get("model") or model_id or None,
                            })
                            step_index += 1
                        elif etype == "permission" and _controlada:
                            # 15.8.0 — a write of a controlled test chat waits for its person: what it
                            # would change goes to the platform (the code agent asks the person), and
                            # `POST /chat/permisos/{id}` answers it. The turn keeps waiting meanwhile.
                            from app import prueba_controlada as _pc
                            _pend = _pc.legible(event, _args_de_llamada.get(str(event.get("callID") or "")))
                            _pc.anotar_pendiente(_pend["id"], sesion=session_id, email=chat_email,
                                                 hilo=hilo_id, clave=_pend["clave"])
                            await turn.append({"type": "permission", **_pend})
                        elif etype == "question":
                            # The agent asked the user a clarifying question.
                            # Surface it to the SPA so it renders an interactive
                            # widget; the answer returns as a follow-up turn
                            # carrying ``request_id`` (routed as a reply). The
                            # sidecar already ended the turn after the question.
                            await turn.append({
                                "type": "question",
                                "request_id": event.get("request_id", ""),
                                "questions": event.get("questions", []),
                            })
                        elif etype == "message_done":
                            continue
                        elif etype == "error":
                            err = str(event.get("error") or "agent error")
                            if is_turn_time_limit(err):  # the ceiling: stop it, say so, no retry
                                return await _cerrar_por_tope(turn, proxy, session_id, run_id,
                                                              tope_min, started_at, medicion)
                            # v7.169.0 — a turn that runs out the clock WITHOUT having emitted
                            # anything is retried ONCE.
                            #
                            # Measured in demo: a call to the model took 7 minutes for its
                            # first token —with the ones before and after at 8 seconds—, so
                            # the latency tail exists and sometimes crosses the ceiling.
                            # Losing the whole turn for that is avoidable.
                            #
                            # The condition is strict on purpose: if text or a tool ALREADY
                            # went out, retrying would duplicate work (and writes) and the
                            # operator would see the answer twice. Only what never got to
                            # start is recovered.
                            # (15.12.0: a heartbeat is not output — it only says it is waiting.)
                            if (attempt == 1 and "timeout" in err.lower()
                                    and not any(e.get("type") != "latido" for e in turn.events)):
                                logger.info(
                                    "chat: turno agotó el reloj sin emitir nada; "
                                    "reintentando una vez (%s)", err,
                                )
                                try:
                                    await proxy.abort_session(session_id)
                                except Exception:
                                    logger.debug("abort previo al reintento falló",
                                                 exc_info=True)
                                reintentar = True
                                medicion.reintento()
                                break
                            await turn.append({"type": "error", "message": err})
                            await _finish_chat_run(
                                run_id, status="failed", error=err,
                                duration_ms=_elapsed(started_at),
                                medicion=medicion.registrar("error"),
                            )
                            return "error"
                        elif etype == "done":
                            break
                    if reintentar:
                        continue
                    stream_ended_cleanly = True
                except RecoverableAgentError as exc:
                    # THE AGENT'S POD IS RESTARTING (to apply a connector just connected, or
                    # after a crash): the connection is refused before any event, so nothing
                    # was shown yet and the prompt can be sent again. Wait for it once instead
                    # of answering «stream transport: All connection attempts failed»
                    # (04/10/2026: two «look again» landed in the 20 s of a restart).
                    if not esperado_al_agente and isinstance(exc.__cause__, httpx.ConnectError):
                        esperado_al_agente = True
                        await turn.append({"type": "processing"})
                        if await esperar_al_agente(proxy):
                            continue
                    if not sesion_renovada and _is_session_404(exc):
                        sesion_renovada = True
                        # R-CHAT-3: the registry pointed at a session
                        # the sidecar has forgotten (idle GC or pod
                        # restart). Rotate the operator's server-side
                        # session, tell the SPA, and retry the prompt
                        # once. No state was emitted yet on this attempt
                        # (stream_interactive raises BEFORE the first
                        # event when the GET /stream 404s), so we don't
                        # need to roll anything back.
                        logger.info(
                            "chat session %s unknown to simple API, "
                            "self-healing with a fresh session",
                            session_id,
                        )
                        try:
                            handle = await mgr.resolve(scope, force_new=True,
                                                       **_solo_lectura_de(request))
                            session_id = handle.session_id
                            if run_id:
                                from sqlalchemy import update as _update
                                # BOTH: this block writes with `app_write` (see below).
                                # Importing only `app_session` left a latent NameError that
                                # does not show until this branch runs.
                                from app.app_database import app_session, app_write
                                from app.app_models.runtime import AgentRun
                                from app.memory_utils import persist_run_memory_user
                                # `app_write`: THIS WRITES. With `app_session` the commit takes SQLite's
                                # lock without queueing, and then the `BEGIN IMMEDIATE` of a writer that
                                # DID queue waits up to busy_timeout (15 s) holding the slot — everyone
                                # behind measures those 15 s. Measured on three pods: 14,980 / 15,061 / 14,883 ms.
                                async with app_write() as _s:
                                    await _s.execute(
                                        _update(AgentRun)
                                        .where(AgentRun.id == run_id)
                                        .values(opencode_session_id=session_id)
                                    )
                                    await _s.commit()
                                await persist_run_memory_user(
                                    run_id, scope.memory_user,
                                )
                        except (RecoverableAgentError, FatalAgentError) as e2:
                            await turn.append({"type": "error", "message": str(e2)})
                            await _finish_chat_run(
                                run_id, status="failed", error=str(e2),
                                duration_ms=_elapsed(started_at),
                            )
                            return "error"
                        await turn.append({"type": "session", "session_id": session_id})
                        # Freshly-minted session: re-pin its workspace
                        # folder AND re-inject the per-user memory block
                        # silently before retrying the prompt (a fresh
                        # session lost the memory primed on the old one).
                        heal_memory = await build_memory_block(scope.memory_user)
                        heal_memory += await build_scoped_knowledge_block(
                            attending=True, memory_user=scope.memory_user,
                        )
                        await _prime_workspace(
                            proxy, session_id, scope.workspace_subdir,
                            heal_memory, memory_user=scope.memory_user,
                        )
                        from app.agent_sessions import stamp_prompt_contract
                        await stamp_prompt_contract(scope.scope_key)
                        continue
                    await turn.append({"type": "error", "message": str(exc)})
                    await _finish_chat_run(
                        run_id, status="failed", error=str(exc),
                        duration_ms=_elapsed(started_at),
                        medicion=medicion.registrar("error"),
                    )
                    return "error"
                if stream_ended_cleanly:
                    break

            # Whatever the filter held back: the normal text tail, or nothing if the employee
            # left a block half-way (half a JSON is not a card).
            _cola = _tarjetas.cerrar()
            if _cola:
                await turn.append({
                    "type": "text_delta", "content": _cola,
                    "isReasoning": False,
                    # From the SAME text and the SAME step as what was held back, not from the
                    # step in which it is released.
                    "stepIndex": paso_del_texto if parte_texto else step_index,
                    "partId": parte_texto,
                })

            from app.config import AGENT_COST_PER_CREDIT
            _cuenta = medicion.registrar("ok")
            # BEFORE `complete`: whoever waits for it (`invocar`) reads the raw text right after.
            turn.texto = streamed_text
            await turn.append({
                "type": "complete",
                "runId": run_id,
                "cost": total_cost,
                # Cost expressed as agent credits — what the chat shows
                # instead of money (tenant agent_cost_per_credit rate).
                "credits": (total_cost / AGENT_COST_PER_CREDIT)
                if (total_cost and AGENT_COST_PER_CREDIT > 0) else 0,
                "costPerCredit": AGENT_COST_PER_CREDIT,
                "tokens": {"input": total_input, "output": total_output},
                "model": model_id,
                "durationMs": _elapsed(started_at),
                # The turn's account: where the time went, at which level it ran and which
                # tools cost. It also goes through the log (a JSON line `event: "turno"`),
                # which is where it is diagnosed.
                "medicion": _cuenta,
            })
            await _finish_chat_run(
                run_id,
                status="success",
                text=streamed_text,
                model=model_id,
                tokens_input=total_input,
                tokens_output=total_output,
                cost=total_cost,
                duration_ms=_elapsed(started_at),
                memory_user=scope.memory_user,
                user_message=user_message,
                workspace_subdir=scope.workspace_subdir,
                started_wall=started_wall,
                user_email=chat_email,
                opencode_session_id=session_id,
                medicion=_cuenta,
            )
            # Title the thread once it is known what it was about: with the question AND the
            # answer a much better name comes out than with the question alone. Best-effort
            # and at the end of the turn: a title is a convenience, it cannot spoil a
            # conversation that already went well.
            if hilo_id:
                # IN THE BACKGROUND: the title comes from a separate call to the model and cannot
                # make anyone wait. The answer is already delivered; the name appears in the list
                # a few seconds later, which is exactly how ChatGPT behaves.
                async def _titular(h=hilo_id, p=user_message, r=streamed_text or ""):
                    try:
                        await chat_threads.titular_tras_el_primer_intercambio(h, p, r, chat_email)
                    except Exception:  # noqa: BLE001 — never break anything over a title
                        logger.debug("hilos: no se pudo titular %s", h, exc_info=True)
                asyncio.create_task(_titular())

            # The LAST turn's input tokens already include the whole
            # replayed history, so input+output ≈ the context window in
            # use after this turn — a good "is this session filling up?"
            # proxy for the chat header and the Operations page.
            await mgr.touch(
                scope.scope_key,
                context_tokens=(total_input + total_output) or None,
            )
            return "complete"
        except asyncio.CancelledError:
            # Replaced by a newer message of the same person (or the pod stopping): the run
            # is CLOSED here, or it stays «running» in the history forever — the restart
            # recovery is the only other thing that closes it. Shielded so the cancellation
            # that brought us here cannot also cut the write.
            try:
                await asyncio.shield(_finish_chat_run(
                    run_id, status="cancelled",
                    error="Replaced by a newer message before it finished",
                    duration_ms=_elapsed(started_at),
                ))
            except Exception:  # noqa: BLE001 — closing the row never blocks the cancellation
                logger.warning("chat: no se pudo cerrar el run %s cancelado", run_id,
                               exc_info=True)
            raise

    # Launch the turn detached from this request, then subscribe to its
    # buffer from the start. If the browser drops (tab switch / refresh)
    # the subscription is cancelled but the runner keeps going; the SPA
    # re-attaches via GET /chat/stream on return.
    from app.aviso_push import token_valido
    turn = await start_turn(scope.scope_key, session_id, run_id, runner,
                            capacidades=list(payload.capacidades or []),
                            aviso_push=token_valido(payload.aviso_push),
                            hilo=(payload.thread_id or "").strip() or None)
    return StreamingResponse(
        _subscribe_turn_sse(turn, 0),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def _solo_lectura_de(request: Request) -> dict:
    """`{"solo_lectura": {grant, permisos}}` for the session manager in a READ-ONLY test (15.7.0), or
    `{"prueba_controlada": {grant, permisos, reglas}}` in a CONTROLLED test chat (15.8.0), else nothing —
    so a normal turn calls `resolve` exactly as before (and an employee's older test doubles still fit)."""
    ro = getattr(request.state, "solo_lectura_sesion", None)
    if ro is not None:
        return {"solo_lectura": ro}
    pc = getattr(request.state, "prueba_controlada_sesion", None)
    return {"prueba_controlada": pc} if pc is not None else {}


def _solo_su_hilo_de_prueba(request: Request, email: str, hilo: Any) -> None:
    """A controlled test (15.8.0) only follows, stops or asks about ITS test conversation — never a real one."""
    if getattr(request.state, "prueba_controlada", False):
        from app import prueba_controlada as _pc
        if not _pc.es_hilo_de_prueba(email, str(hilo or "")):
            raise HTTPException(status_code=403, detail={
                "code": "NOT_A_TEST_CONVERSATION",
                "message": "A controlled test only reaches the test conversation it created."})


class RespuestaAlPermiso(BaseModel):
    permitir: bool
    motivo: str | None = None
    thread_id: str


@router.post("/chat/permisos/{permiso_id}")
async def responder_permiso(permiso_id: str, body: RespuestaAlPermiso, request: Request,
                            user=Depends(require_project_access)):
    """THE PERSON's answer to a pending write of a CONTROLLED test chat (15.8.0, `prueba_controlada`): run it
    once, or reject it with its reason (the call fails with it and the agent goes on). Only through the
    platform's signed test delegation and only for a write pending in THAT person's test conversation (403
    otherwise). Allowing a write the employee would normally park for approval needs the same right as
    approving it there (`users_directory.can_approve`): the test does not widen who decides."""
    if not getattr(request.state, "prueba_controlada", False):
        raise HTTPException(status_code=403, detail={"code": "NOT_A_CONTROLLED_TEST",
                                                     "message": "Only a controlled test answers its pending writes."})
    from app import prueba_controlada as _pc
    chat_email = _resolve_chat_user_email(user)
    _solo_su_hilo_de_prueba(request, chat_email, body.thread_id)
    pend = _pc.tomar_pendiente(permiso_id, chat_email, body.thread_id)
    if pend is None:
        raise HTTPException(status_code=403, detail={"code": "NOT_YOUR_PENDING_WRITE",
                                                     "message": "That write is not pending in this test conversation."})
    if body.permitir:
        from app import mcp_policy
        from app.users_directory import can_approve
        conector, _, herramienta = str(pend["clave"]).partition("_")
        if conector != "app-tools" and herramienta:
            veredicto = (await mcp_policy.decide_call(conector, herramienta, {}))[0]
            if veredicto == "approve" and not await can_approve(chat_email):
                raise HTTPException(status_code=403, detail={
                    "code": "ONLY_AN_APPROVER",
                    "message": "This employee parks this change for approval, and this person may not approve "
                               "it here: only an admin or a designated approver can allow it."})
    try:
        await get_agent_proxy().answer_permission(pend["sesion"], permiso_id, body.permitir, body.motivo or "")
    except FatalAgentError as exc:
        raise HTTPException(status_code=409, detail={"code": "PERMISSION_GONE", "message": str(exc)}) from exc
    _pc.PENDIENTES.pop(permiso_id, None)
    logger.info("prueba controlada: %s %s %s (%s)", chat_email, "permite" if body.permitir else "rechaza",
                pend["clave"], permiso_id)
    return {"ok": True, "permitido": body.permitir}


# ── Invoking a capability by API (14.23.0, `app/invocacion.py`) ────────────────────────────────

class InvocacionRequest(BaseModel):
    capacidad: str
    argumentos: dict | None = None
    # The DECLARED USE this call is (docs/red-de-integraciones.md): its id and its approved instructions, already
    # resolved by the platform. Both empty for a person invoking from this employee's own screens.
    uso: str | None = None
    instrucciones: str | None = None
    # The caller's rejection of a previous output, to fix only those elements (`invocacion.validar_rechazo_previo`).
    rechazo_previo: dict | None = None
    # 15.7.0 — a READ-ONLY test (`app/prueba_solo_lectura.py`). From the body it can only NARROW: the platform's
    # preview asks for it in the SIGNED delegation (`auth`), which the caller cannot drop.
    solo_lectura: bool = False


#: One invocation at a time per person AND capability: they share a thread (and so a session
#: key), and a second turn on the same key would SUPERSEDE the first (`chat_runtime.start_turn`
#: cancels it). Serialised, both finish. In-process like the turn registry (single worker).
_CERROJOS_DE_INVOCACION: dict[str, asyncio.Lock] = {}


def _rechazo(estado: int, codigo: str, mensaje: str, **mas: Any) -> HTTPException:
    """Same shape as every coded error of the employee: `detail = {code, message, ...}`."""
    return HTTPException(status_code=estado, detail={"code": codigo, "message": mensaje, **mas})


async def _turno_de_invocacion(payload: ChatRequest, request: Request, user: Any,
                               scope_key: str) -> dict:
    """Runs ONE turn through the chat's own path and waits for its end.

    The same path on purpose —session as the person, memory, permissions gate, the run in the
    audit with its origin, the budget—: an invocation is a turn of the employee like any other;
    only its message and what is done with the answer differ. Returns the RAW text
    (`ChatTurn.texto`), the run, and whether it asked or failed."""
    desde = time.time()
    await chat_with_agent(payload, request, user)
    turn = await get_turn(scope_key)
    if turn is None or turn.created_at < desde:
        # No turn of ours: chat_with_agent answered without starting one (a budget notice that
        # appeared between the check and the turn). Never read someone else's older turn.
        raise _rechazo(503, "INVOCACION_FALLIDA",
                       "The employee did not start the turn for this invocation; try again.")
    fuera = {"texto": "", "run_id": turn.run_id, "pregunta": "", "error": "", "bloqueadas": []}
    # 15.7.0 — in a read-only test, the calls the session's ACL or the proxy refused, from the turn's own events.
    solo_lectura = getattr(request.state, "solo_lectura_sesion", None) is not None
    llamadas: dict[str, tuple[str, Any]] = {}
    async for _i, ev in turn.subscribe(0):
        tipo = ev.get("type")
        if solo_lectura and tipo == "tool_call":
            llamadas[str(ev.get("call_id") or "")] = (str(ev.get("name") or ""), ev.get("args"))
        elif solo_lectura and tipo == "tool_result":
            from app import prueba_solo_lectura as _ro
            if _ro.es_bloqueo(ev.get("result")):
                nombre, args = llamadas.get(str(ev.get("call_id") or ""), (str(ev.get("name") or ""), None))
                fuera["bloqueadas"].append(_ro.bloqueada(nombre or str(ev.get("name") or ""), args))
        if tipo == "question":
            fuera["pregunta"] = json.dumps(ev.get("questions") or [], ensure_ascii=False)[:400]
        elif tipo == "error":
            fuera["error"] = str(ev.get("message") or "agent error")
        elif tipo == "complete" and ev.get("runId"):
            fuera["run_id"] = ev["runId"]
    fuera["texto"] = turn.texto or ""
    return fuera


def _juzgar(turno: dict, salida: dict) -> tuple[bool, Any, list[str]]:
    """`(True, value, [])` if the turn's answer is the JSON the capability promises."""
    from app import invocacion as inv
    if turno["pregunta"]:
        return False, None, ["you asked a question instead of answering with the JSON"]
    ok, valor = inv.extraer_json(turno["texto"])
    if not ok:
        return False, None, [f"$: {valor}"]
    errores = inv.validar(valor, salida)
    return (not errores), valor, errores


@router.post("/invocar")
async def invocar_capacidad(
    payload: InvocacionRequest,
    request: Request,
    user=Depends(require_project_access),
):
    """Invoke a declared capability with ARGUMENTS and get its STRUCTURED output, validated.

    Contract (docs/aplicaciones-que-llaman-a-empleados.md, «Invocar una capacidad»):
    `{capacidad, argumentos}` → `200 {resultado, run_id, intentos}` ·
    `404 CAPACIDAD_NO_EXISTE` · `400 CAPACIDAD_SIN_SALIDA` · `400 ENTRADA_NO_VALIDA {errores}` ·
    `402 PRESUPUESTO_AGOTADO` · `422 SALIDA_NO_CUMPLE {errores, run_id}` ·
    `502 INVOCACION_FALLIDA`. Reached by a person with a session or, through the platform, by an
    application acting as a person (`auth._RUTAS_DE_APLICACION`); either way the turn runs AS
    that person, never with more."""
    from app import invocacion as inv
    cid = (payload.capacidad or "").strip()
    cap = inv.capacidad(cid)
    if cap is None:
        raise _rechazo(404, inv.CAPACIDAD_NO_EXISTE,
                       f"This employee's Definition has no capability «{cid}».")
    salida = cap.get("salida")
    if not isinstance(salida, dict) or not salida:
        raise _rechazo(400, inv.CAPACIDAD_SIN_SALIDA,
                       f"The capability «{cid}» declares no output schema (`salida`): it can be "
                       "asked in a conversation, not invoked.")
    argumentos = dict(payload.argumentos or {})
    entrada = cap.get("entrada")
    if isinstance(entrada, dict) and entrada:
        errores = inv.validar(argumentos, entrada)
    else:
        errores = ["$: this capability declares no arguments (`entrada`)"] if argumentos else []
    if errores:
        # BEFORE spending a turn: a wrong argument is the caller's error and costs nothing.
        raise _rechazo(400, inv.ENTRADA_NO_VALIDA,
                       f"The arguments do not conform to the input schema of «{cid}».",
                       errores=errores[:inv.MAX_ERRORES])
    try:
        rechazo_previo = inv.validar_rechazo_previo(payload.rechazo_previo)
    except ValueError as exc:
        raise _rechazo(400, inv.ENTRADA_NO_VALIDA, str(exc), errores=[f"$.rechazo_previo: {exc}"])

    # The budget, BEFORE: in the chat an exhausted budget is a message in the thread; here
    # there is no thread to read, so it is a code the application can act on.
    from app.budget import budget_block_reason, user_cap_status
    chat_email = _resolve_chat_user_email(user)
    _cap = await user_cap_status(chat_email)
    if await budget_block_reason() is not None or (_cap and _cap["over"]):
        raise _rechazo(402, inv.PRESUPUESTO_AGOTADO,
                       "The employee's (or this person's) credit budget is exhausted.")

    # 15.7.0 — READ-ONLY TEST: the delegation's signed claim (`auth`) or the body (which can only narrow).
    solo_lectura = bool(getattr(request.state, "solo_lectura", False) or payload.solo_lectura)
    # `prueba` only when it is one: an employee's older scaffold test doubles this with `(email, cid)`.
    hilo = await chat_threads.hilo_de_invocacion(chat_email, cid, **({"prueba": True} if solo_lectura else {}))
    scope_key = interactive_scope(chat_email, hilo).scope_key
    request.state.invocacion = cid
    grant = ""
    if solo_lectura:
        from app import prueba_solo_lectura as _ro
        from app.agent_sessions import resolve_tool_permissions
        grant = _ro.abrir()
        request.state.solo_lectura_sesion = {"grant": grant,
                                             "permisos": await _ro.permisos(await resolve_tool_permissions())}
        if not str(getattr(request.state, "origen", "") or "").startswith("prueba:"):
            request.state.origen = "prueba:pantalla"

    def _peticion(mensaje: str, nueva: bool) -> ChatRequest:
        # `tarjetas=[]`: a program renders no cards. `reset`: each invocation starts from a clean
        # context (the previous one's file is not this one's); the correction keeps it.
        return ChatRequest(message=mensaje, thread_id=hilo, reset=nueva, canal="api",
                           capacidad=cid, argumentos=argumentos, tarjetas=[])

    # In a read-only test, what the caller always gets back: that it was one, and what was refused.
    bloqueadas: list[dict] = []
    def _de_prueba() -> dict:
        return {"solo_lectura": True, "escrituras_bloqueadas": bloqueadas} if solo_lectura else {}

    cerrojo = _CERROJOS_DE_INVOCACION.setdefault(scope_key, asyncio.Lock())
    try:
        async with cerrojo:
            instrucciones = (payload.instrucciones or "").strip()
            turno = await _turno_de_invocacion(_peticion(inv.encargo(cap, argumentos, instrucciones=instrucciones,
                                                                     rechazo_previo=rechazo_previo,
                                                                     solo_lectura=solo_lectura), True),
                                               request, user, scope_key)
            bloqueadas += turno.get("bloqueadas") or []
            if turno["error"] and not turno["texto"].strip():
                raise _rechazo(502, inv.INVOCACION_FALLIDA, turno["error"], run_id=turno["run_id"], **_de_prueba())
            ok, valor, errores = _juzgar(turno, salida)
            intentos = 1
            if not ok:
                # ONE retry. After a question the session is waiting for an answer nobody will give:
                # a fresh one with the whole request again. Otherwise the same session (the work is
                # done) and only the format to fix.
                mensaje, nueva = ((inv.encargo(cap, argumentos, pregunto=turno["pregunta"],
                                               instrucciones=instrucciones, rechazo_previo=rechazo_previo,
                                               solo_lectura=solo_lectura), True)
                                  if turno["pregunta"] else (inv.correccion(cap, errores), False))
                logger.info("invocar %s: la salida no cumple (%s); un reintento", cid, errores[:3])
                turno = await _turno_de_invocacion(_peticion(mensaje, nueva), request, user, scope_key)
                bloqueadas += turno.get("bloqueadas") or []
                if turno["error"] and not turno["texto"].strip():
                    raise _rechazo(502, inv.INVOCACION_FALLIDA, turno["error"], run_id=turno["run_id"],
                                   **_de_prueba())
                ok, valor, errores = _juzgar(turno, salida)
                intentos = 2
    finally:
        if grant:
            # The test is over: whatever its session still tries is refused by the proxy (fail-closed).
            from app import prueba_solo_lectura as _ro
            _ro.cerrar(grant)
    # THE AUDIT of this use lives HERE, in this employee's run (docs/red-de-integraciones.md): which capability,
    # which declared use and its arguments as data (not only inside the prompt). The platform keeps only an index.
    coste = await _anotar_invocacion(turno["run_id"], cid, (payload.uso or "").strip(), argumentos)
    if not ok:
        raise _rechazo(422, inv.SALIDA_NO_CUMPLE,
                       f"The employee's answer to «{cid}» does not conform to its output schema "
                       "after one retry.", errores=errores[:inv.MAX_ERRORES],
                       run_id=turno["run_id"], **_de_prueba())
    return {"resultado": valor, "run_id": turno["run_id"], "intentos": intentos, "coste_usd": coste,
            **_de_prueba()}


async def _anotar_invocacion(run_id, capacidad: str, uso: str, argumentos: dict) -> float | None:
    """Writes the capability, the use and the arguments into the invocation's run and returns its cost (for the
    platform's index). Never breaks the answer: the result is already there."""
    if not run_id:
        return None
    import json as _json
    from sqlalchemy import select, update
    from app.app_database import app_write
    from app.app_models.runtime import AgentRun
    try:
        async with app_write() as s:
            await s.execute(update(AgentRun).where(AgentRun.id == int(run_id)).values(
                capacidad=capacidad or None, uso=uso or None,
                argumentos_json=_json.dumps(argumentos or {}, ensure_ascii=False)[:20000]))
            coste = (await s.execute(select(AgentRun.cost_usd).where(AgentRun.id == int(run_id)))).scalar()
            await s.commit()
        return float(coste) if coste not in (None, "") else None
    except Exception:  # noqa: BLE001
        logger.warning("invocar: could not record the use in run %s", run_id, exc_info=True)
        return None


# ── Sessions registry (operator visibility / control) ──────────────
@router.get("/razonamiento")
async def catalogo_de_razonamiento(user=Depends(require_project_access)):
    """The reasoning levels that can be requested, and the one the caller has set.

    An empty `mio` means "I have not chosen": then each message runs with whatever the
    employee has configured, and that is what the screens show as an option («El del
    empleado»), not a fourth level."""
    from app import razonamiento as _r
    email = _resolve_chat_user_email(user)
    return {
        "niveles": _r.catalogo(),
        "mio": await _r.preferencia_de(email),
        # What someone who does not choose runs with. Without this «El del empleado» was a
        # black box: it was chosen blindly and an operator's change was not seen anywhere.
        "del_empleado": await _r.del_empleado(),
    }


class PreferenciaDeRazonamiento(BaseModel):
    # Empty or unknown = clear the preference (back to the employee's level).
    nivel: str | None = None


@router.put("/razonamiento")
async def fijar_razonamiento(payload: PreferenciaDeRazonamiento,
                             user=Depends(require_project_access)):
    """Sets the caller's default level. It is THEIRS: it does not change the employee's nor
    anyone else's, and any message can ask for another one."""
    from app import razonamiento as _r
    email = _resolve_chat_user_email(user)
    return {"mio": await _r.fijar_preferencia(email, payload.nivel)}


@router.get("/sessions")
async def list_agent_sessions(user=Depends(require_admin)):
    # SECURITY: admin only. The list carries the `scope_key` of ALL people (emails, and
    # phones/handles of the channel senders), their `memory_user` and their `workspace_subdir`
    # —the exact folder of each one—. With `require_project_access` any member saw everyone
    # else. The only screen that uses it (Operations → Sessions) is already admin-only.
    """List the server-side sessions the pod keeps (one chat session per
    operator + one per person-and-channel). Since v7.25.0 every row is
    ENRICHED with the association the operator actually thinks in:
    the PERSON (directory display name + memory key), the CHANNEL
    (label + kind) and the shared workspace folder — so the SPA can group
    a person's chat + channel sessions together and show that they all
    share one workspace and one memory."""
    from app.app_database import app_session
    mgr = get_session_manager()
    rows = await mgr.list_sessions()

    # Directory: memory_user email → display name (best-effort).
    names: dict[str, str] = {}
    try:
        from sqlalchemy import select as _select
        from app.app_models.users import AppUser
        async with app_session() as s:
            for email, dn, alias in (await s.execute(
                _select(AppUser.email, AppUser.display_name, AppUser.alias)
            )).all():
                if email:
                    names[email.strip().lower()] = (dn or alias or "").strip()
    except Exception:  # pragma: no cover — enrichment is best-effort
        logger.debug("sessions list: directory lookup failed", exc_info=True)

    # Channels: id → (label, kind).
    channels: dict[int, tuple[str, str]] = {}
    try:
        from sqlalchemy import select as _select
        from app.app_models.channels import Channel
        async with app_session() as s:
            for cid, label, kind in (await s.execute(
                _select(Channel.id, Channel.display_name, Channel.kind)
            )).all():
                channels[int(cid)] = (label or f"#{cid}", kind or "email")
    except Exception:  # pragma: no cover
        logger.debug("sessions list: channels lookup failed", exc_info=True)

    def _row(r) -> dict:
        origin = getattr(r, "origin", None) or (
            "chat" if r.scope_key.startswith("interactive:") else "channel"
        )
        # Legacy rows (pre-v7.25.0) have NULL association columns — derive
        # what we can from the scope_key so the view stays complete.
        memory_user = getattr(r, "memory_user", None)
        channel_id = getattr(r, "channel_id", None)
        if origin == "chat" and not memory_user:
            memory_user = r.scope_key.split(":", 1)[1] if ":" in r.scope_key else None
        if origin == "channel" and channel_id is None:
            parts = r.scope_key.split(":", 2)
            if len(parts) == 3 and parts[1].isdigit():
                channel_id = int(parts[1])
                if not memory_user and "@" in parts[2]:
                    memory_user = parts[2]
        ch = channels.get(int(channel_id)) if channel_id is not None else None
        return {
            "scope_key": r.scope_key,
            "role": r.role,
            "user_email": r.user_email,
            "origin": origin,
            "memory_user": memory_user,
            "display_name": names.get((memory_user or "").strip().lower(), ""),
            "channel_id": channel_id,
            "channel_label": ch[0] if ch else None,
            "channel_kind": ch[1] if ch else None,
            "workspace_subdir": r.workspace_subdir,
            "message_count": int(r.message_count or 0),
            "context_tokens": int(getattr(r, "context_tokens", 0) or 0),
            "status": r.status,
            "session_id": (r.session_id or "")[:12],
            "created_at": iso_utc(r.created_at),
            "last_used_at": iso_utc(r.last_used_at),
            # v7.143.0 — which version of the instructions this operator is talking to. It is
            # the difference between "deployed" and "the employee knows it": if it is
            # outdated, the next message re-primes it.
            "prompt_contract": getattr(r, "prompt_contract", None) or "",
            "prompt_contract_stale": (
                origin == "chat"
                and is_contract_stale(getattr(r, "prompt_contract", None))
            ),
        }

    return {"sessions": [_row(r) for r in rows]}


class RespuestaDeUbicacion(BaseModel):
    """What the client returns when the employee asks it for the location."""
    lat: float | None = None
    lon: float | None = None
    #: Error radius in metres, as the device gives it. It ALWAYS travels: it is what lets
    #: the employee say "you are near X" instead of asserting a street with 800 m of error.
    precision: float | None = None
    #: Seconds since the position was taken. The client takes it ON receiving the request, so
    #: it is normally ~0; it is sent anyway so a cached position cannot pass as fresh without
    #: the employee knowing.
    antiguedad_s: float | None = None
    #: Why it could not: `sin_permiso`, `en_segundo_plano`, `sin_senal`, `rechazado`.
    #: When this comes, there are no coordinates and the employee carries on without them, knowing why.
    no_disponible: str | None = None


@router.post("/location/{peticion_id}")
async def responder_ubicacion(
    peticion_id: str,
    payload: RespuestaDeUbicacion,
    user=Depends(require_project_access),
):
    """The client answers a location request the employee left waiting.

    The authorization has TWO layers and both are needed: the caller is identified
    (`require_project_access`) and moreover `ubicacion.resolver` checks the request is
    THEIRS. The identifier travels to the client, so on its own it cannot be a permission to
    inject a position into another person's turn in the same pod.

    Answering "not available" is a fully valid answer: it unblocks the turn just like a
    coordinate, and the employee still knows where it stands. What is not acceptable is
    staying silent.
    """
    from app import ubicacion as ubic
    email = _resolve_chat_user_email(user)
    datos = None if payload.no_disponible else {
        "lat": payload.lat, "lon": payload.lon,
        "precision": payload.precision, "antiguedad_s": payload.antiguedad_s,
    }
    if datos is not None and (datos["lat"] is None or datos["lon"] is None):
        datos = None
    entregado = ubic.resolver(peticion_id, email,
                              datos or {"no_disponible": payload.no_disponible or "sin_datos"})
    if not entregado:
        # 404 and not 403: an expired request and another person's are answered the same, so
        # as not to tell anyone that something exists that is not theirs.
        raise HTTPException(status_code=404,
                            detail={"code": "location_request_gone",
                                        "message": "Esa petición de ubicación ya no está esperando."})
    return {"ok": True}


class SessionDeleteRequest(BaseModel):
    scope_key: str


@router.post("/sessions/delete")
async def delete_agent_session(
    payload: SessionDeleteRequest,
    user=Depends(require_project_access),
):
    """Reset ONE session (sidecar + registry). The next message for that
    scope mints a fresh session. POST (not DELETE-with-path) because a
    scope_key carries ``:`` and an email and shouldn't ride in the URL."""
    # SECURITY: a member can only reset THEIR conversations. The key is sent by the client,
    # and without this anyone could delete the live context of a colleague or a channel
    # (also in the sidecar). An admin can reset any (Operations → Sessions).
    key = (payload.scope_key or "").strip()
    email = _resolve_chat_user_email(user)
    from app.users_directory import is_admin
    if not await is_admin(email):
        propia = key == f"interactive:{email}" or key.startswith(f"interactive:{email}:")
        if not propia:
            raise HTTPException(status_code=403,
                                detail={"code": "only_own_conversations",
                                        "message": "Solo puedes reiniciar tus propias conversaciones."})
    removed = await get_session_manager().delete(key)
    return {"removed": bool(removed)}


@router.get("/session/current")
async def current_session(thread_id: str | None = None,
                          user=Depends(require_project_access)):
    """Context-usage summary for the LOGGED-IN operator's chat session.

    The chat header uses this to show how full the session's context
    window is (and to know whether a session even exists yet). Returns
    zeros when the operator hasn't chatted yet (no row)."""
    from app.agent_sessions import ROTATE_MAX_MESSAGES
    chat_email = _resolve_chat_user_email(user)
    scope = interactive_scope(chat_email, (thread_id or '').strip() or None)
    rows = await get_session_manager().list_sessions()
    row = next((r for r in rows if r.scope_key == scope.scope_key), None)
    return {
        "scope_key": scope.scope_key,
        "exists": row is not None,
        "context_tokens": int(getattr(row, "context_tokens", 0) or 0) if row else 0,
        "message_count": int(row.message_count or 0) if row else 0,
        "rotate_max_messages": ROTATE_MAX_MESSAGES,
        # The frontend cannot derive the hashed workspace token; expose the
        # caller's own workspace folder + memory key so the chat can render
        # a "Mi workspace" / "Mi memoria" panel scoped to them.
        "workspace_subdir": scope.workspace_subdir,
        "memory_user": scope.memory_user,
    }


@router.post("/chat/compact")
async def compact_chat_context(thread_id: str | None = None,
                               user=Depends(require_project_access)):
    """Compact the LOGGED-IN operator's chat session IN PLACE (v7.95.0).

    Mirror of the code agent's «Compactar»: OpenCode summarizes the current
    thread and continues on top of the summary, so the context tokens (and
    the cost of every following message) drop WITHOUT losing the visible
    conversation — the cheap alternative to «Limpiar contexto» when the
    operator wants to keep the topic. Runs against the operator's OWN
    session only; blocking until the summarize turn finishes so the SPA can
    report success/failure truthfully.
    """
    from app.agent_proxy import _resolve_model
    from app.agent_sessions import resolve_operator_model
    from app.app_database import app_write
    from app.app_models.runtime import AgentSession

    chat_email = _resolve_chat_user_email(user)
    scope = interactive_scope(chat_email, (thread_id or '').strip() or None)
    rows = await get_session_manager().list_sessions()
    row = next((r for r in rows if r.scope_key == scope.scope_key), None)
    if row is None or not (row.session_id or "").strip():
        raise HTTPException(404, "No hay una sesión de chat activa que compactar.")
    # Same model resolution the chat itself uses: operator override
    # (app_settings.agent_model) else the env-configured interactive default.
    model = (await resolve_operator_model()) or _resolve_model("interactive")
    status = await get_agent_proxy().compact_session(row.session_id, model)
    if status == "error":
        raise HTTPException(502, "No se pudo compactar la sesión.")
    # "ok": the stored context size is now stale-high. "gone": the native
    # session died with an agent-pod restart (its store is container-local),
    # so there is nothing left to compact and the stored size never matched
    # reality — the next message self-heals into a fresh session. Either way
    # zero the counter so the header stops warning; the next turn re-reports
    # the real usage.
    try:
        async with app_write() as s:
            db_row = (await s.execute(
                select(AgentSession).where(AgentSession.scope_key == scope.scope_key)
            )).scalar_one_or_none()
            if db_row is not None:
                db_row.context_tokens = 0
                await s.commit()
    except Exception:  # pragma: no cover — cosmetic reset, never fail the compact
        logger.debug("compact: context_tokens reset failed", exc_info=True)
    # v12.4.0 — it is recorded IN THE THREAD. It is what lets both clients fold what came
    # before ("N earlier messages, summarised when compacting", like the code agent) and keeps
    # the fold there when reloading and when opening the conversation from the other device.
    # Only when it really was compacted: with "gone" nothing was summarised —the session no
    # longer existed— and marking the thread would be saying a detail was lost that nobody lost.
    compactado = None
    if status == "ok":
        compactado = await chat_threads.marcar_compactado(chat_email, thread_id or "")
    return {"compacted": status == "ok", "stale": status == "gone",
            "compactado_en": compactado}


# ── Live-turn reconnect (R-CHAT-4, v6.66.0) ────────────────────────
@router.get("/chat/active")
async def chat_active(request: Request, thread_id: str | None = None,
                      user=Depends(require_project_access)):
    """Does the logged-in operator have a chat turn in flight right now?

    The SPA calls this on mount to decide whether to re-attach to a run
    that kept executing while the tab was hidden / the page was reloaded.
    Returns ``active=false`` when there is no live (or recently-finished,
    still-buffered) turn for the operator."""
    chat_email = _resolve_chat_user_email(user)
    _solo_su_hilo_de_prueba(request, chat_email, thread_id)
    scope = interactive_scope(chat_email, (thread_id or '').strip() or None)
    turn = await get_turn(scope.scope_key)
    if turn is None:
        return {"active": False}
    return {
        "active": turn.is_running,
        "status": turn.status,
        "run_id": turn.run_id,
        "event_count": len(turn.events),
    }


@router.get("/chat/stream")
async def reconnect_chat_stream(
    request: Request,
    from_index: int = 0,
    thread_id: str | None = None,
    user=Depends(require_project_access),
):
    """Re-attach to the operator's in-flight chat turn.

    Mirrors the WAI code agent's stream-replay endpoint
    (``GET stream/{id}?from_index=N``):
    replays the buffered events the browser missed while it was away, then
    follows the live tail until the turn completes. When there is no live
    turn (it finished and aged out, or never existed) we emit a single
    ``no_active_turn`` event so the SPA can finalise its optimistic bubble
    instead of spinning forever."""
    chat_email = _resolve_chat_user_email(user)
    _solo_su_hilo_de_prueba(request, chat_email, thread_id)
    scope = interactive_scope(chat_email, (thread_id or '').strip() or None)
    turn = await get_turn(scope.scope_key)
    if turn is None:
        async def _empty() -> AsyncIterator[bytes]:
            yield _sse({"type": "no_active_turn"})
        return StreamingResponse(
            _empty(), media_type="text/event-stream", headers=_SSE_HEADERS,
        )
    return StreamingResponse(
        _subscribe_turn_sse(turn, max(0, int(from_index))),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# ── Stop the in-flight chat turn (v7.86.0) ─────────────────────────
@router.post("/chat/abort")
async def abort_chat(request: Request, thread_id: str | None = None,
                     user=Depends(require_project_access)):
    """Stop the operator's in-flight chat turn — server-side (the Stop button).

    The browser's ``AbortController`` only closes the SSE reader in THIS tab; the
    turn keeps running detached in the app pod (``chat_runtime``) and the OpenCode
    sidecar keeps generating (and billing). This finds the operator's live turn,
    marks it terminal — so every subscriber (this tab, a reconnecting tab, the
    buffered replay) settles instead of spinning — and tells the sidecar to abort
    the generation so compute is freed immediately. Mirrors the autonomous-run kill
    switch (``runs_routes`` ``/{run_id}/cancel``). Best-effort + idempotent."""
    chat_email = _resolve_chat_user_email(user)
    _solo_su_hilo_de_prueba(request, chat_email, thread_id)
    scope = interactive_scope(chat_email, (thread_id or '').strip() or None)
    turn = await get_turn(scope.scope_key)
    if turn is None or not turn.is_running:
        return {"ok": False, "reason": "no_active_turn"}
    # Emit a marker then mark the turn terminal so buffered-event subscribers stop
    # waiting (a reconnecting tab sees the abort even if this one already left).
    try:
        await turn.append({"type": "aborted", "message": "Detenido por el usuario."})
    except Exception:
        logger.debug("abort_chat: append marker failed", exc_info=True)
    await turn.finish("error")
    # Free the sidecar generation (never raises).
    if turn.session_id:
        try:
            await get_agent_proxy().abort_session(turn.session_id)
        except Exception:
            logger.debug("abort_chat: sidecar abort failed", exc_info=True)
    return {"ok": True}


# ── Helpers ────────────────────────────────────────────────────────
#: Coded error of a chat turn that reached its ceiling (`chat_turn_max_minutes`). The screen
#: keys its message on it (`agent.chat.turnTimeLimit`), never on the text.
TURN_TIME_LIMIT = "TURN_TIME_LIMIT"
#: Coded refusal of a message sent while the conversation's turn is still running (15.11.0).
TURN_IN_PROGRESS = "TURN_IN_PROGRESS"


async def _cerrar_por_tope(turn: ChatTurn, proxy, session_id: str, run_id: int | None,
                           tope_min: int, started_at: float, medicion) -> str:
    """Close a chat turn whose ceiling (`chat_turn_max_minutes`) ran out.

    The sidecar only stops LISTENING: the opencode run keeps working, unseen, in this same
    session, and the person's next message queues behind it and runs out the clock too
    (cobsa, 06/11/2026: «why did you error after 80 steps?» also died at 600.0 s). So the run
    is ABORTED first —the same sidecar abort as the Stop button (`/chat/abort`)— and only then
    is the person told, with a stable code and the minutes so the screen says it in their
    language. Never retried: the ceiling is the operator's decision and a retry would be a
    second full ceiling. The abort never raises: the person must get their message even if it
    fails (logged: the run then goes on until opencode ends it)."""
    try:
        if not await proxy.abort_session(session_id):
            logger.warning("chat: turn ceiling reached, the abort of %s was not confirmed",
                           session_id)
    except Exception:  # noqa: BLE001
        logger.warning("chat: turn ceiling reached, could not abort %s", session_id,
                       exc_info=True)
    await turn.append({
        "type": "error",
        "code": TURN_TIME_LIMIT,
        "minutes": tope_min,
        # For whoever reads raw events (an invocation, a log); the screen keys on `code`.
        "message": f"Stopped: the task exceeded the maximum response time ({tope_min} min).",
    })
    await _finish_chat_run(
        run_id, status="failed",
        error=f"{TURN_TIME_LIMIT}: stopped after {tope_min} min (chat_turn_max_minutes)",
        duration_ms=_elapsed(started_at),
        medicion=medicion.registrar("timeout"),
    )
    return "error"


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


# How often the heartbeat comment is sent. 20 s leaves plenty of margin under the two
# clocks to beat: the browser (60 s) and nginx (180 s).
_SSE_KEEPALIVE_S = 20.0

async def _subscribe_turn_sse(
    turn: ChatTurn, from_index: int = 0,
) -> AsyncIterator[bytes]:
    """Stream a turn's buffered + live events as SSE.

    Every event carries an ``idx`` (its position in the turn buffer) so
    the SPA can persist its progress and reconnect with the right
    ``from_index`` instead of replaying the whole turn. Used by both the
    POST /chat (``from_index=0``) and the GET /chat/stream reconnect.
    """
    # HEARTBEAT every 20 s. Without this, a turn that takes a while to produce the next
    # event —the agent thinking, or a slow tool: Maps, the browser, a search— leaves the
    # channel SILENT, and everyone kills a silent channel: the ingress nginx cuts at 180 s
    # (`proxy-read-timeout`) and the browser itself gives up at 60 (`SILENCE_MS` of
    # `pumpReader`). The user sees "connection error" in the middle of an answer that was
    # going perfectly.
    #
    # It goes as an SSE COMMENT (`: keepalive`), not as an event: it keeps the connection
    # alive and resets both clocks without the client having to learn a new type —its parser
    # already ignores any line not starting with `data: `—. That way it works the same with
    # the browsers already deployed.
    it = turn.subscribe(from_index).__aiter__()
    pendiente: Optional[asyncio.Future] = None
    try:
        while True:
            if pendiente is None:
                # The future is kept BETWEEN heartbeats on purpose. With `asyncio.wait_for`
                # the `__anext__` would be cancelled on every wait, and cancelling half-way can
                # swallow the event that was arriving: a piece of the answer would be lost
                # without anything saying so.
                pendiente = asyncio.ensure_future(it.__anext__())
            hechos, _ = await asyncio.wait({pendiente}, timeout=_SSE_KEEPALIVE_S)
            if not hechos:
                yield b": keepalive\n\n"
                continue
            try:
                idx, event = pendiente.result()
            except StopAsyncIteration:
                return
            finally:
                pendiente = None
            out = dict(event)
            out["idx"] = idx
            yield _sse(out)
    finally:
        if pendiente is not None:
            pendiente.cancel()


#: How the priming turn ENDS (14.17.3). Its answer is discarded, but it stays in the session:
#: asked nothing, the model answered with a self-introduction in whatever language the
#: instructions leaned to (Spanish headers), and the person's first message in English got a
#: Spanish answer — the conversation already «was» in Spanish. «OK» has no language.
PRIME_CLOSING = (
    "\n\nThis is a setup message: nobody reads your answer to it. Reply with exactly `OK` and "
    "nothing else. The person's first message comes next, and ITS language is the one you answer in."
)


async def _prime_workspace(
    proxy, session_id: str, subdir: str, memory: str = "",
    *, memory_user: str = "",
) -> None:
    """Silently pin the session's workspace folder, inject per-user
    memory, and establish the chat-mode contract — all as an out-of-band
    priming turn whose response is discarded. Must NOT include autonomous
    ``traceability_block`` (that triggers stock/alert flows on casual chat)."""
    try:
        from app.chat_prompt import build_chat_mode_prime
        # Establish WHO the employee is FIRST — otherwise the model answers as a
        # generic coding assistant. Resolved from the persona (manifest+override).
        try:
            from app.persona import employee_identity_preamble
            from app.agent_locale import resolve_agent_language
            _identity = await employee_identity_preamble(await resolve_agent_language())
        except Exception:
            _identity = ""
        # v7.140.0 — what the employee LEARNED about this environment, primed
        # ONCE per session like the rest. It lives here (and not in the caller) so
        # BOTH prime paths get it — the normal one and the "session was lost, heal
        # it" retry; a healed session that forgot its learnings would answer the
        # same question differently, which is the worst possible failure mode for
        # something the operator is meant to trust.
        try:
            from app.agent_memory_prompt import build_agent_memory_block
            from app.memory_contract import MODE_CHAT, two_memories_contract
            # NOT ``agent_memory_capture_hint`` here (it was, until 7.143.0): that
            # hint asks for a ``self_memory`` block inside a STRUCTURED_RESPONSE,
            # and three lines below chat-mode forbids emitting one — so the ONLY
            # capture procedure chat received was the one it could not follow.
            # The chat contract names the tools instead.
            learned = (await build_agent_memory_block()
                       + two_memories_contract(MODE_CHAT, memory_user=memory_user))
            # (The colleagues —the ones it has and the ones it lacks— are NO longer hooked in
            # here: they live in `employee_identity_preamble`, which is the only thing the
            # three surfaces read. Repeating them here duplicated them in the chat and left
            # them out of the channels, which is exactly the kind of asymmetry that makes an
            # employee behave differently depending on where you talk to it.)
        except Exception:  # pragma: no cover — priming must stay best-effort
            learned = ""
        # v7.146.0 — the MAP of its own application. It is odd to give out an employee with
        # chat that cannot explain the screen the person is on: here goes the (compact)
        # index, and the detail is requested with ``describe_app`` — which is also what
        # survives when the sidecar prunes this priming.
        try:
            from app.self_knowledge import index_block
            own_app = "\n\n" + index_block()
        except Exception:  # pragma: no cover — never break the prime
            own_app = ""
        prompt = (
            (_identity + "\n\n" if _identity else "")
            + workspace_block(subdir)
            + (memory or "")
            + learned
            + own_app
            + build_chat_mode_prime(memory_user, workspace_subdir=subdir)
        ).strip() + PRIME_CLOSING
        async for _event in proxy.stream_interactive(
            session_id, prompt, timeout=120.0,
        ):
            # Drain to completion; never yield to the SPA.
            if _event.get("type") == "done":
                break
    except Exception:  # pragma: no cover — priming is best-effort
        logger.debug("workspace prime failed (ignored)", exc_info=True)


def _elapsed(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


async def _historial_reciente(email: str, hilo_id: str | None) -> list[dict]:
    """The last exchanges of this conversation, in the shape the reception reads.

    FEW are given on purpose: this is not the employee's memory —which lives apart and
    belongs to the person—, it is the thread of the chat, and it is the only thing that
    allows judging an "and the next one?" without guessing."""
    if not hilo_id:
        return []
    try:
        # 10 and not 8: the reception shows the last 8 exchanges, so asking for exactly 8 left
        # the window stuck to the limit and any turn without an answer shortened it.
        turnos = await chat_threads.historial(email, hilo_id, limite=10)
    except Exception:  # noqa: BLE001 — without history it judges worse, nothing breaks
        logger.debug("recepcion: no se pudo leer el historial", exc_info=True)
        return []
    fuera: list[dict] = []
    for t in turnos:
        if t.get("pregunta"):
            fuera.append({"de": "persona", "texto": t["pregunta"]})
        if t.get("respuesta"):
            fuera.append({"de": "agente", "texto": t["respuesta"],
                          "atendido_por": t.get("atendido_por") or "agente",
                          "recepcion": t.get("recepcion") or ""})
    return fuera


def _ya_pregunto(historial: list[dict]) -> bool:
    """Was the previous turn a QUESTION from the reception?

    It is the brake of "a single round": if the last thing it did was ask, this message is
    the answer, and the answer is not answered with another question. It delegates, and the
    employee —which asks better, with the work already started— takes care of whatever is
    missing.

    Measured on 22/09/2026 asking for a hotel: seven messages, six questions, two of them
    identical word for word. None of this is fixed by asking the model: a model that already
    repeated itself twice will not remember on the third.
    """
    for t in reversed(historial or []):
        if t.get("de") != "agente":
            continue
        return (t.get("recepcion") or "") == "pregunto"
    return False


async def _recepcion_decide(*, payload, mensaje: str, chat_email: str,
                            hilo_id: str | None, medicion) -> dict:
    """What is done with this message. `{"accion": "delego"}` on any doubt.

    It does not run with an attachment (there is something to look at) nor when the turn
    answers an open question of the agent (that turn RESUMES where it stopped): in both cases
    the decision is already taken and getting in the way can only spoil it."""
    from app import recepcion as _recepcion
    # And with a LOCATION attached, the same as with an attachment (24/09/2026): the
    # reception does not receive it, so it answered "which city are you in?" to someone who
    # had just sent it. There is something to look at: it goes to the employee, which does see it.
    if payload.attachments or payload.request_id or payload.location:
        return {"accion": "delego"}
    # BY VOICE, what the conversational model recognizes WINS; if it recognized nothing, it
    # is recognized here.
    #
    # Before, by voice it just delegated: it was taken for granted that the conversational
    # model had already decided. But it only fills in `capacidad` if it remembers —it is a
    # model's adherence, not a contract—, and measured on 20/09/2026 it did NOT fill it in
    # having the capability marked [DIRECT] in front of it and having written "I need to look
    # up the traveller's context" in its own request. Result: twenty seconds for a query that
    # resolves in two. Recognizing here costs a short call and does not depend on anyone
    # remembering.
    es_voz = (payload.canal or "").strip().lower() == "voz"
    if es_voz and payload.capacidad:
        return {"accion": "atajo", "capacidad": payload.capacidad,
                "argumentos": dict(payload.argumentos or {})}
    if not await _recepcion.activa():
        return {"accion": "delego"}
    if es_voz:
        # By voice it does NOT answer nor ask from here —that is done by the conversational
        # model, which is who speaks—: the only thing sought is the shortcut it did not name.
        from app import atajos as _atj
        if not _atj.catalogo():
            return {"accion": "delego"}
        visto = await _atj.reconocer(mensaje)
        if visto.get("capacidad"):
            medicion.recepcion = "atajo"
            return {"accion": "atajo", "capacidad": visto["capacidad"],
                    "argumentos": dict(visto.get("argumentos") or {})}
        return {"accion": "delego"}
    historial = await _historial_reciente(chat_email, hilo_id)
    capacidades = list(getattr(payload, "capacidades", None) or [])
    decision = _recepcion.normalizar(await _recepcion.decidir(mensaje, historial, capacidades))
    # Its device can send the location and what they ask depends on where they are: asking «¿en qué ciudad?» from
    # here is never right — the agent asks the device. Checked here too, because it cannot depend on the model.
    if decision.get("accion") == "pregunto" and "ubicacion" in capacidades \
            and _recepcion.depende_de_donde_esta(mensaje):
        decision = {"accion": "delego", "razonamiento": "normal"}
    # A SINGLE ROUND. Asking once saves an agent turn of 25-40k tokens; asking twice in a row
    # is an interrogation, and the person has already answered. It is checked here and not in
    # the contract because this cannot depend on the model remembering.
    if decision.get("accion") == "pregunto" and _ya_pregunto(historial):
        decision = {"accion": "delego", "razonamiento": decision.get("razonamiento") or ""}
    medicion.recepcion = decision.get("accion") or ""
    # What the reception handled and the agent has not seen. It is told when delegating: the
    # opencode session IS the memory of the conversation, and three messages resolved here
    # would leave it a gap that would show up days later looking like the model's doing.
    if decision.get("accion") == "delego":
        decision["pendiente"] = _recepcion.pendiente_de_contar(historial)
    return decision


async def _cerrar_turno_corto(turn: ChatTurn, *, texto: str, origen: str, medicion,
                              run_id: int | None, user_message: str,
                              hilo_id: str | None, scope, chat_email: str,
                              started_at: float, started_wall: float,
                              session_id: str, tarjetas: list[str] | None = None) -> None:
    """Answers the turn with an in-house text —without agent— and closes it like any other:
    same event, same row in the audit, same measurement line. What is STORED is the raw text
    (cards included: the history repaints from it); what is EMITTED is pruned for this client
    (`tarjetas.podar`), the same rule the agent's answers follow."""
    from app.config import AGENT_COST_PER_CREDIT
    from app.tarjetas import podar as _podar
    await turn.append({"type": "text_delta", "content": _podar(texto, tarjetas or []), "isReasoning": False,
                       "stepIndex": 0, "partId": origen})
    cuenta = medicion.registrar("ok")
    await turn.append({
        "type": "complete", "runId": run_id, "cost": 0.0, "credits": 0,
        "costPerCredit": AGENT_COST_PER_CREDIT,
        "tokens": {"input": 0, "output": 0}, "model": "",
        "durationMs": _elapsed(started_at), "medicion": cuenta,
    })
    await _finish_chat_run(
        run_id, status="success", text=texto, model="", tokens_input=0, tokens_output=0,
        cost=0.0, duration_ms=_elapsed(started_at), memory_user=scope.memory_user,
        user_message=user_message, workspace_subdir=scope.workspace_subdir,
        started_wall=started_wall, user_email=chat_email,
        opencode_session_id=session_id, medicion=cuenta,
    )
    logger.info("%s resuelto en %d ms sin arrancar al agente", origen, _elapsed(started_at))
    if hilo_id:
        async def _titular(h=hilo_id, p=user_message, r=texto):
            try:
                await chat_threads.titular_tras_el_primer_intercambio(h, p, r, chat_email)
            except Exception:  # noqa: BLE001
                logger.debug("hilos: no se pudo titular %s", h, exc_info=True)
        asyncio.create_task(_titular())


async def _atajo_resuelve(turn: ChatTurn, *, payload, canal: str, reconocido: dict | None,
                          nivel_pedido: str,
                          lo_puso: str, medicion, run_id: int | None,
                          user_message: str, hilo_id: str | None, scope,
                          chat_email: str, started_at: float, started_wall: float,
                          session_id: str) -> bool:
    """Answers the turn WITHOUT starting the agent, if the definition declares so.

    Returns True when it has answered. False = it does not apply, and the turn goes the
    usual way — which is what happens on any doubt, by design (`app/atajos.py`).

    The order of the checks is not by chance: first what is known without spending anything
    (switch, attachments, level requested by hand), and only at the end what costs a call
    to the small model. An employee without declared shortcuts pays absolutely nothing."""
    from app import atajos as _atajos

    # Asking for a level is asking it to think it through: there is no shortcut there. The
    # same with an attachment (there is something to look at), with the answer to an open
    # question (the turn resumes) and with a location request in flight.
    if lo_puso == "persona" or payload.attachments or payload.request_id:
        return False
    if not await _atajos.activos():
        return False

    # Who recognized it depends on the door: by voice, the conversational model (it comes in
    # the payload); in writing, the reception, which has already read the message. Nothing is
    # recognized again here — a second opinion on the same thing can only contradict the
    # first.
    fuente = reconocido or {}
    capacidad = (fuente.get("capacidad") or payload.capacidad or "").strip()
    argumentos = dict(fuente.get("argumentos") or payload.argumentos or {})
    if not capacidad:
        return False

    atajo = _atajos.buscar(capacidad)
    if atajo is None:
        # It named something that is not declared as a shortcut (or no longer is). Nothing is
        # forced: normal path, which is where that capability is resolved just the same.
        logger.info("atajo: capacidad %r no declarada; va por el camino normal", capacidad)
        return False
    # Only what is DECLARED travels: whoever recognizes is a model and models fill gaps.
    argumentos = _atajos.solo_los_declarados(atajo, argumentos)
    faltan = _atajos.faltan_argumentos(atajo, argumentos)
    if faltan:
        logger.info("atajo %s: faltan datos %s; camino normal", capacidad, faltan)
        return False

    t0 = time.monotonic()
    salida = await _atajos.ejecutar(atajo, argumentos, run_id)
    # The permissions gate said no: it is a legitimate answer, the same the agent would give.
    # It is not retried another way — that would be asking twice for what was already denied.
    if not salida.get("ok") and salida.get("denegado"):
        texto = str((salida["denegado"] or {}).get("error")
                    or "No tienes permiso para eso.")
    elif not salida.get("ok"):
        logger.warning("atajo %s falló (%s); camino normal", capacidad, salida.get("error"))
        return False
    else:
        # ONE SINGLE WAY OF ANSWERING, wherever it comes from. By voice this had an
        # exception —"the conversational model already words it, it is who speaks"— and the
        # exception was looking at the wrong place: the conversational model does read the
        # raw data and tells it well out loud, but that SAME text is the one stored in the
        # thread and seen on the screen. Measured on 20/09/2026: the spoken answer was
        # correct and the chat showed the whole `{"content":[{"type":"text",...}]}`.
        #
        # And the normal path —the one that starts the agent— never returns raw data by voice
        # either: it returns the agent's prose. It was the shortcut that stepped out of the
        # contract, not the contract that lacked a case.
        crudo = salida.get("resultado")
        texto = await _atajos.redactar(user_message, crudo)
        if not texto:
            # Without a write-up there is no answer to show. It is NOT retried by the normal
            # path —the tool has already run and a shortcut may be declared on something that
            # writes—, but the JSON is not released either: in the chat it is unreadable and
            # by voice it is read out loud, which is worse.
            logger.warning("atajo %s: sin redacción; no enseño el crudo", capacidad)
            texto = ("He consultado el dato pero no he podido ponerlo en palabras "
                     "ahora mismo. Pídemelo otra vez, por favor.")
        # A PLACE resolved by the shortcut goes with its card: without it «take me there» is
        # an address with nothing to press (06/10/2026, travel employee). Built from the data.
        from app import tarjetas as _tarjetas_mod
        lugar = _tarjetas_mod.lugar_de_geocodificacion(crudo, nombre=str(
            next(iter(argumentos.values()), "") if len(argumentos) == 1 else ""))
        if lugar and _tarjetas_mod.APERTURA not in texto:
            texto = f"{texto.rstrip()}\n{_tarjetas_mod.bloque(lugar)}"

    medicion.por_atajo(capacidad)
    # The door goes in the line: the shortcut no longer behaves differently depending on
    # where it comes in, but knowing through which one it came is half of any diagnosis.
    logger.info("atajo %s por %s: herramienta en %d ms", capacidad, canal or "chat",
                int((time.monotonic() - t0) * 1000))
    await _cerrar_turno_corto(
        turn, texto=texto, origen="atajo", medicion=medicion, run_id=run_id,
        user_message=user_message, hilo_id=hilo_id, scope=scope,
        chat_email=chat_email, started_at=started_at, started_wall=started_wall,
        session_id=session_id, tarjetas=getattr(payload, "tarjetas", None))
    return True


async def _create_chat_run(session_id: str, message: str,
                           thread_id: str | None = None,
                           solicitante: str | None = None,
                           origen: str = "chat") -> tuple[int | None, str | None]:
    """Persist a row in ``agent_runs`` with ``mode='chat'``.

    Previously a no-op (v6.4.0 chose audit-clarity over resilience).
    Since v6.5.x we persist with ``mode='chat'`` so the conversation
    is recoverable. ``runs_routes.list_runs`` returns BOTH kinds when
    ``mode`` is omitted (dashboard summary + the SPA "Todas" toggle
    rely on it); the audit view stays clean because ``Runs.tsx``
    defaults its own mode filter to ``'autonomous'`` (v7.89.2 — the
    single runs contract, enforced by verifier §8.3h/§8.3i).
    """
    from uuid import uuid4
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    # `app_write` and not just `app_session`: this function WRITES (see the comment below).
    # The import was left behind when the write was changed to `app_write`, and since it is
    # a LOCAL import nobody noticed until the branch ran: NameError on every chat message,
    # HTTP 500. `py_compile` does not see it: a non-existent name is correct syntax.
    from app.app_database import app_session, app_write
    from app.app_models.runtime import AgentRun

    idem_key = f"chat:{session_id}:{uuid4().hex}"
    # `app_write`: THIS WRITES. With `app_session` the commit takes SQLite's lock without
    # queueing, and then the `BEGIN IMMEDIATE` of a writer that DID queue waits up to
    # busy_timeout (15 s) holding the slot — everyone behind measures those 15 s. Measured
    # on three pods: 14,980 / 15,061 / 14,883 ms.
    async with app_write() as session:
        stmt = sqlite_insert(AgentRun).values(
            event_id=None,
            idempotency_key=idem_key,
            trace_id=idem_key,
            opencode_session_id=session_id,
            # Which THREAD it belongs to. Rotation changes the `opencode_session_id` in the
            # middle of a conversation, so the session is NOT useful to rebuild a thread: this
            # is needed. NULL = that person's default thread.
            thread_id=(thread_id or None),
            # v12.0.0 — SECURITY: the identifier that travels to the MODEL (`[INTERNO]` line).
            # Random and unguessable; the autoincrement `id` was guessable ("use run_id=1233")
            # and with it one person acted as another in the tools.
            run_token=uuid4().hex,
            status="RUNNING",
            mode="chat",
            # v12.7.0 — who and through where, recorded HERE. Deducing it later is not
            # possible: the session rotates mid-conversation and the session rows are collected.
            solicitante=(solicitante or None),
            # «chat», or «app:<name>» when the person speaks through an application
            # (docs/aplicaciones-que-llaman-a-empleados.md): through WHERE is part of who.
            origen=(origen or "chat"),
            attempts=1,
            prompt=message[:32000],
            started_at=datetime.now(timezone.utc),
        ).on_conflict_do_nothing(index_elements=[AgentRun.idempotency_key])
        await session.execute(stmt)
        await session.commit()
        row = (await session.execute(
            select(AgentRun.id, AgentRun.run_token)
            .where(AgentRun.idempotency_key == idem_key)
        )).first()
        if not row:
            return None, None
        return int(row[0]), (row[1] or None)


async def _finish_chat_run(
    run_id: int | None,
    *,
    status: str,
    text: str | None = None,
    model: str | None = None,
    tokens_input: int | None = None,
    tokens_output: int | None = None,
    cost: float | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    memory_user: str | None = None,
    user_message: str | None = None,
    workspace_subdir: str = "",
    started_wall: float | None = None,
    user_email: str | None = None,
    opencode_session_id: str | None = None,
    medicion: dict | None = None,
) -> None:
    """Persist completion metadata on the ``mode='chat'`` row created
    by :func:`_create_chat_run`. No-op when ``run_id`` is None."""
    if run_id is None:
        return
    from sqlalchemy import update
    from app.app_database import app_write
    from app.app_models.runtime import AgentRun
    from app.chat_prompt import (
        normalize_chat_markdown,
        strip_internal_chat_leak,
    )
    from app.memory_utils import (
        auto_index_generated_files,
        auto_record_chat_episode,
    )

    # What is STORED is what the assistant wrote, without trimming by resemblance. Until
    # 12.9.0 "the echo of the question" was pruned here by comparing text: it was needed
    # because the sidecar's proxy returned the USER's message as if it were an answer. That
    # was fixed where the fault was —now the proxy looks at whose each part is— and this
    # trimming is superfluous. Trimming by resemblance was never free: it left the closing
    # mark loose at the start of the answer and could eat real characters.
    cleaned = (
        normalize_chat_markdown(strip_internal_chat_leak(text or ""))
        if text else None
    )

    # v12.1.0 — LOCATION TRAIL. If it was asked for in this turn (and shared or not), the
    # line is attached to the TEXT that is stored. It has to go here, in what is persisted,
    # and not only as a live event: the history does NOT bring tools nor reasoning, so when
    # repainting the conversation in another client a tool call is not seen — and an "I know
    # where you are" would remain without any explanation. Being automatic there is no
    # dialog that gives it away, so this line is the only thing that makes it auditable.
    if cleaned is not None and run_id is not None:
        try:
            from app import ubicacion as ubic
            from sqlalchemy import select as _select
            from app.app_database import app_session as _sess
            from app.app_models.runtime import AgentRun as _AR
            async with _sess() as _s:
                _meta_raw = (await _s.execute(
                    _select(_AR.request_meta_json).where(_AR.id == run_id))).scalar_one_or_none()
            _meta = json.loads(_meta_raw or "{}") if _meta_raw else {}
            _ubic = _meta.get("ubicacion")
            if _ubic is not None:
                cleaned = (cleaned.rstrip() + "\n\n"
                           + ubic.describir(None if _ubic.get("no_disponible") else _ubic))
        except Exception:  # noqa: BLE001 — the trail can never break storing the turn
            logger.debug("ubicacion: no se pudo anotar el rastro en el run %s", run_id,
                         exc_info=True)

    values: dict[str, Any] = {
        "status": ("COMPLETED" if status in ("done", "ok", "completed", "success")
                   else "CANCELLED" if status == "cancelled" else "FAILED"),
        "finished_at": datetime.now(timezone.utc),
        "error": error[:4000] if error else None,
    }
    if text is not None:
        values["response_md"] = cleaned if cleaned is not None else text
    if model is not None:
        values["model"] = model
    if tokens_input is not None:
        values["tokens_in"] = int(tokens_input)
    if tokens_output is not None:
        values["tokens_out"] = int(tokens_output)
    if cost is not None:
        values["cost_usd"] = str(cost)
    if duration_ms is not None:
        values["duration_sec"] = int(duration_ms / 1000)
    if medicion:
        # The turn's account is stored MERGED into `request_meta_json`: that bag is already
        # written by whoever receives the message (location, client…) and overwriting it
        # whole would delete their part. `duration_sec` rounds to seconds and there exactly
        # what has to be looked at is lost.
        try:
            from sqlalchemy import select as _sel_meta
            from app.app_models.runtime import AgentRun as _AR_meta
            async with app_write() as _s_meta:
                _prev = (await _s_meta.execute(
                    _sel_meta(_AR_meta.request_meta_json).where(_AR_meta.id == run_id)
                )).scalar_one_or_none()
                _bolsa = json.loads(_prev or "{}") if _prev else {}
                if not isinstance(_bolsa, dict):
                    _bolsa = {}
                _bolsa["medicion"] = medicion
                values["request_meta_json"] = json.dumps(_bolsa, ensure_ascii=False,
                                                         default=str)
        except Exception:  # noqa: BLE001 — measuring never breaks closing a turn
            logger.debug("medicion: no se pudo fusionar en el run %s", run_id,
                         exc_info=True)

    async with app_write() as session:
        await session.execute(
            update(AgentRun).where(AgentRun.id == run_id).values(**values)
        )
        await session.commit()

    # Register this chat turn's spend in the TENANT billing ledger so it
    # surfaces (as credits) on the platform /usage page and counts
    # against the monthly limit — same path the WAI code agent uses.
    # Fire-and-forget: a billing failure must never break the turn.
    if cost is not None:
        try:
            from app.cost_reporter import report_run_cost
            await report_run_cost(
                session_id=opencode_session_id or (f"chat-{run_id}" if run_id else None),
                user_id=user_email or memory_user or None,
                cost_usd=cost,
                tokens_input=int(tokens_input or 0),
                tokens_output=int(tokens_output or 0),
                model_name=model or None,
            )
        except Exception:
            logger.debug("tenant cost report failed for chat run %s", run_id, exc_info=True)

    if status in ("done", "ok", "completed", "success"):
        # Auto-index any deliverable the agent produced (or merely named)
        # this turn so it surfaces in "Mis ficheros", then link those
        # artifacts onto the episode. Detection uses the RAW response so
        # markdown normalisation can't mangle the path tokens.
        artifacts = await auto_index_generated_files(
            run_id, memory_user, text or "", workspace_subdir, started_wall,
            user_message=user_message,
        )
        await auto_record_chat_episode(
            run_id,
            memory_user,
            user_message or "",
            cleaned,
            artifacts=artifacts,
        )
