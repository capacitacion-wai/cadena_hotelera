"""Conversation threads: create, list, title and evict (v9.1.0).

The whole module rests on one separation: **the thread is durable, the session is a cache**.

  · The thread (`ChatThread`) is a name and an identity. Only a person deletes it.
  · The session (`AgentSession`) is the agent's live context. It rotates, expires and is evicted.

Without that separation, the garbage collector —which has always existed and deletes idle
sessions— would be deleting named conversations, and people would rightly call it data
loss.

**Existing is cheap; being active is expensive.** A thread that exists is a row. An active
one is a live OpenCode session in the sister agent pod, the same 1 GiB one that already
bounds `MAX_CONCURRENT_RUNS`. That is why there are two limits and not one.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from app.app_database import app_session, app_write
from app.app_models.runtime import AgentSession, ChatThread
from app.agent_sessions import HILO_POR_DEFECTO, es_hilo_historico, hilo_semilla_de
from app.config import MAX_CHAT_THREADS, MAX_CONCURRENT_CHATS

logger = logging.getLogger(__name__)

#: Cap on the title. A long one does not fit in a phone's list and gets cut in half.
MAX_TITULO = 60


def _ahora() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _con_zona(x: datetime | None) -> str | None:
    """The timestamp, said in UTC and with the zone SET.

    It is stored without a zone (naive column, and it stays so: changing it would need a
    migration), but emitting it bare —`2026-08-31T19:28:00`— makes JavaScript read it as
    LOCAL time, so in GMT+2 the user saw 19:28 for something that happened at 21:28. Two
    hours of lies in the conversation list, in both clients at once, because both do
    `new Date(...)`. Here what it always was is said: UTC.
    """
    return x.replace(tzinfo=timezone.utc).isoformat() if x else None


def nuevo_id() -> str:
    """Short and meaningless: it appears in session keys and in URLs."""
    return secrets.token_hex(6)


#: Marks of a BROKEN title: an echo of the instruction that asked for it, or a piece of the
#: internal block. Nobody calls a conversation that, so recognising them cannot be confused
#: with getting it right. The Spanish ones cover titles from before the prompt moved to
#: English (they are still in the database).
_TITULOS_ROTOS = (
    "título de esta conversación", "titulo de esta conversacion",
    "tres a cinco palabras", "3-5 palabras", "3 a 5 palabras",
    "[titulo", "[título", "run_id=", "exports=", "interno",
    "responde solo", "en el idioma del usuario",
    "title of this conversation", "3 to 5 words", "reply only", "in the user's language",
)


def esta_roto(titulo: str) -> bool:
    """Did this title come from a failed attempt and not from the conversation?"""
    bajo = (titulo or "").strip().lower()
    return bool(bajo) and any(x in bajo for x in _TITULOS_ROTOS)


def titulo_valido_del_modelo(propuesto: str, pregunta: str) -> str:
    """The model's title, ONLY if it looks like a title. Empty string if not.

    The gate exists because the model has twice returned the instruction instead of obeying
    it: the "separate" session is still the employee's agent, the same channel that already
    showed it repeats what it is told. With this, when it fails, it fails towards the
    deterministic title —which is good— instead of towards nonsense in the conversation list.
    """
    t = re.sub(r"\s+", " ", (propuesto or "").strip()).strip("\"“”«»' .:")
    if not t or len(t) > MAX_TITULO or esta_roto(t):
        return ""
    # A title is a few words, not a sentence. Eight is generous and cuts any explanation.
    if len(t.split(" ")) > 8:
        return ""
    # And it cannot be an echo of the question itself in another format: that is what the
    # deterministic one is for, which is exactly that and without spending a call.
    return "" if t.lower() == (pregunta or "").strip().lower() else t


def titulo_de(mensaje: str) -> str:
    """Emergency title with the first words. Cost 0.

    It is set on CREATION so there is never a thread without a name —a list with gaps while
    a model thinks reads like a broken application—. Then the model improves it, unless the
    person has renamed it.
    """
    limpio = re.sub(r"\s+", " ", (mensaje or "").strip())
    if not limpio:
        return "Conversación"
    # Six words and no final punctuation: it is what fits in a list row without being cut.
    # With an initial capital because it is going to be read as a name, not as a sentence.
    corto = " ".join(limpio.split(" ")[:6])
    corto = corto[:MAX_TITULO].rstrip(" ,.;:—-¿?¡!")
    if not corto:
        return "Conversación"
    return corto[0].upper() + corto[1:]


async def listar(email: str, incluir_archivados: bool = False) -> list[dict]:
    """This person's threads, the most recent first.

    Cross-checked with `agent_sessions` to say which ones are ALIVE: in the list it matters
    to tell the one that will answer instantly from the one that will have to remember what
    it was about.
    """
    e = (email or "").strip().lower()
    # v12.0.0 — the default conversation is ALWAYS in the list: it is ensured when listing,
    # not only on the first message. It is what makes whoever comes in for the first time
    # see ONE conversation, and not "No conversations yet" until they press "+".
    await asegurar_por_defecto(e)
    async with app_session() as s:
        q = select(ChatThread).where(
            ChatThread.user_email == e, ChatThread.deleted_at.is_(None),
        )
        if not incluir_archivados:
            q = q.where(ChatThread.archived_at.is_(None))
        hilos = (await s.execute(q.order_by(ChatThread.last_used_at.desc()))).scalars().all()
        vivas = {
            r.scope_key: r for r in
            (await s.execute(select(AgentSession).where(
                AgentSession.user_email == e, AgentSession.role == "interactive",
            ))).scalars().all()
        }
    # REPAIR ON LISTING. A broken title —an echo of the instruction that asked for it— was
    # only fixed on the next turn OF THAT conversation, so if you did not go back to it, it
    # stayed like that forever. Here it is corrected when opening the list: it is the moment
    # it is seen, and it costs nothing because the rows are already loaded. Only the ones
    # matching the known damage marks are touched, and never one with a name set by hand.
    rotos = [h for h in hilos if not h.title_locked and esta_roto(h.title or "")]
    if rotos:
        await _reparar_titulos([h.id for h in rotos], e)
        arreglados = await _titulos_de([h.id for h in rotos], e)
        for h in hilos:
            if h.id in arreglados:
                h.title = arreglados[h.id]

    fuera = []
    for h in hilos:
        # The thread's REAL key (the default one has no suffix): before, `interactive:{e}:{id}`
        # was always looked for and the default one came out as "no live context" even if it had it.
        ses = vivas.get(clave_de(e, h.id))
        fuera.append({
            "id": h.id,
            "titulo": h.title,
            # v12.0.0 — the client opens it on entering and goes back to it when archiving/deleting another.
            "por_defecto": bool(h.is_default),
            "creado": _con_zona(h.created_at),
            "ultimo_uso": _con_zona(h.last_used_at),
            "archivado": h.archived_at is not None,
            # v12.4.0 — since when the employee works on a summary of this thread. The client
            # folds what came before; NULL = never compacted.
            "compactado_en": _con_zona(h.compacted_at),
            "activo": ses is not None,
            "tokens": int(getattr(ses, "context_tokens", 0) or 0) if ses else 0,
            "mensajes": int(getattr(ses, "message_count", 0) or 0) if ses else 0,
        })
    return fuera


async def _reparar_titulos(ids: list[str], email: str) -> None:
    """Puts broken titles back in place: it takes them from the thread's FIRST question.

    It is the same source the normal titling uses, so the result is the one it would have
    had if nothing had failed — not a different patch that shows later.

    SECURITY (v12.0.0): the first question is looked for among THIS PERSON's turns
    (`memory_user`), not anyone's with that `thread_id`: with the shared 'default' id, A's
    title came out of B's question."""
    from app.app_models.runtime import AgentRun
    from sqlalchemy import func

    e = (email or "").strip().lower()
    for hilo_id in ids:
        async with app_session() as s:
            fila = (await s.execute(
                select(AgentRun).where(
                    AgentRun.thread_id == hilo_id, AgentRun.mode == "chat",
                    func.lower(func.json_extract(AgentRun.request_meta_json, "$.memory_user")) == e,
                ).order_by(AgentRun.started_at.asc()).limit(1)
            )).scalars().first()
            pregunta = (getattr(fila, "prompt", "") or "") if fila is not None else ""
        # Without any turn there is nothing to take it from: the generic one stays, which at least does not lie.
        await poner_titulo_sugerido(
            hilo_id, titulo_de(pregunta) if pregunta else "Conversación", e)


async def _titulos_de(ids: list[str], email: str) -> dict[str, str]:
    e = (email or "").strip().lower()
    async with app_session() as s:
        filas = (await s.execute(
            select(ChatThread.id, ChatThread.title).where(
                ChatThread.id.in_(ids), ChatThread.user_email == e)
        )).all()
    return {i: t for i, t in filas}


def clave_de(email: str, hilo: str | None) -> str:
    """A thread's session key. The usual one keeps the historical one (also with its seed id,
    v12.0.0): same rule as `agent_sessions.interactive_scope`."""
    e = (email or "").strip().lower()
    h = (hilo or "").strip()
    return f"interactive:{e}" if es_hilo_historico(e, h) else f"interactive:{e}:{h}"


async def resolver_hilo(email: str, hilo_id: str | None) -> str:
    """The REAL id of the thread the client names (v12.0.0).

    The ``"default"`` alias (or no thread) is THE CALLER's default conversation: it is
    resolved here to their per-person row, ensuring it exists. It is what makes an old client
    —a phone not yet updated that keeps sending ``thread_id='default'``— work exactly the
    same, and no endpoint have to treat "default" as a special case. Any other id is returned
    as is: ownership is checked by `obtener`.

    v12.3.0 — the SEED id resolves to the CURRENT default one only if its row no longer
    exists: that way an old tab with that id in its cache lands in the current conversation
    instead of getting a 404 without having done anything wrong. While the row EXISTS it is
    returned as is, even if it is no longer the default one —it was rotated and someone
    recovered it from the archive—: it is one more conversation, and mapping it would make
    deleting THAT one delete THIS one.
    """
    e = (email or "").strip().lower()
    h = (hilo_id or "").strip()
    if not h or h == HILO_POR_DEFECTO:
        return await asegurar_por_defecto(e)
    if h == hilo_semilla_de(e) and await obtener(e, h) is None:
        return await asegurar_por_defecto(e)
    return h


async def hilo_actual(email: str) -> dict:
    """The conversation that must open on entering: the most recently used one that is not
    archived, or the default one (which always exists). It is the SERVER's "current thread",
    so two devices open the same conversation instead of each remembering its own."""
    e = (email or "").strip().lower()
    por_defecto = await asegurar_por_defecto(e)
    async with app_session() as s:
        fila = (await s.execute(
            select(ChatThread).where(
                ChatThread.user_email == e,
                ChatThread.deleted_at.is_(None),
                ChatThread.archived_at.is_(None),
            ).order_by(ChatThread.last_used_at.desc()).limit(1)
        )).scalars().first()
    if fila is None:
        fila_id, titulo, es_def = por_defecto, "Conversación", True
    else:
        fila_id, titulo, es_def = fila.id, fila.title, bool(fila.is_default)
    return {"id": fila_id, "titulo": titulo, "por_defecto": es_def,
            "id_por_defecto": por_defecto}


async def asegurar_por_defecto(email: str) -> str:
    """Registers THIS person's default conversation if it is missing, and returns its id.

    v12.0.0 — it is a REAL row PER PERSON. Before it was a single row with the literal id
    "default" for the whole pod: since `id` is the PK, the INSERT only succeeded for the
    FIRST user and for the rest it failed with UNIQUE and was silently swallowed —that is why
    "I come in for the first time and no conversation is created"—.

    v12.3.0 — which one is the default is said by the row's MARK (`is_default`, with a
    partial unique index per person), NOT by a computed id. That was the real reason the
    default one could not be deleted: if its id was deduced from the email, when it was
    needed again there was no way to create another row with that id, because the deleted
    one was still occupying it. With the mark, deleting it is ROTATING it
    (`_rotar_por_defecto`) and the id stops meaning anything.

    The seed (`hilo_semilla_de`) is still used to be born: it keeps the historical session
    key (see `interactive_scope`), and that is what makes updating an employee not cut any
    ongoing conversation. If that row already exists —it was deleted at some point and is
    still there, keeping its history— the new one is born with any id.

    Archived BUT marked cannot happen since 12.3.0 (archiving also rotates). If it appears
    —data from an earlier version— it is unarchived: a default one that is not seen but keeps
    receiving messages was exactly the trap.
    """
    e = (email or "").strip().lower()
    if not e:
        return ""
    fila = await _la_por_defecto(e)
    if fila is not None:
        if fila.archived_at is not None:
            async with app_write() as s:
                f2 = (await s.execute(select(ChatThread).where(
                    ChatThread.id == fila.id))).scalars().first()
                if f2 is not None:
                    f2.archived_at = None
                    await s.commit()
        return fila.id
    semilla = hilo_semilla_de(e)
    async with app_write() as s:
        ocupada = (await s.execute(select(ChatThread.id).where(
            ChatThread.id == semilla))).scalars().first()
        s.add(ChatThread(
            id=semilla if ocupada is None else nuevo_id(), user_email=e,
            title="Conversación", is_default=1,
            title_locked=0, created_at=_ahora(), last_used_at=_ahora(),
        ))
        try:
            await s.commit()
        except Exception:  # noqa: BLE001 — two turns at once from the SAME person
            await s.rollback()
    # It is ALWAYS re-read, also after inserting. If two turns of the same person come in at
    # once, the partial unique index rejects the second and the good id is the winner's, not
    # the one this one tried to set. Before it was enough to return the computed id because
    # both computed the same; with the mark, not any more.
    fila = await _la_por_defecto(e)
    # Without a row here the database is broken; the seed is returned, which is what it always
    # returned, and the caller will get an honest 404 in `obtener` instead of an invented id.
    return fila.id if fila is not None else semilla


async def _la_por_defecto(email: str) -> Optional[ChatThread]:
    """The row MARKED as this person's default, if it exists and is not deleted."""
    async with app_session() as s:
        return (await s.execute(select(ChatThread).where(
            ChatThread.user_email == (email or "").strip().lower(),
            ChatThread.is_default == 1,
            ChatThread.deleted_at.is_(None),
        ))).scalars().first()


async def _rotar_por_defecto(s, fila: ChatThread) -> str:
    """Removes the mark from `fila` and opens a NEW default conversation. Returns its id.

    It is what happens when deleting or archiving the default one (v12.3.0): it leaves the
    list like any other and one carries on in another empty one. Until 12.2.1 this was a
    409 —"the default one cannot be deleted; use Clear context"—, and it was OUR rule, not
    the user's: that there is always a default one is the program's problem, and it is solved
    by opening another, not by forbidding. Whoever wants to start from scratch deletes the
    conversation, which is what that means.

    Two things that depend on the order and on the id:

      · The mark is removed BEFORE inserting, in the SAME transaction: the partial unique
        index (`uq_chat_threads_default`) only admits one marked row per person.
      · The new one CANNOT reuse the seed id if that row exists (it is the PK, and the
        deleted one is still there with its history). Changing the id changes the session
        key (`clave_de`), and that is why it is born WITHOUT the previous one's context:
        really starting from scratch.
    """
    e = fila.user_email
    fila.is_default = 0
    await s.flush()
    semilla = hilo_semilla_de(e)
    ocupada = (await s.execute(select(ChatThread.id).where(
        ChatThread.id == semilla))).scalars().first()
    nueva = ChatThread(
        id=semilla if ocupada is None else nuevo_id(), user_email=e,
        title="Conversación", is_default=1,
        title_locked=0, created_at=_ahora(), last_used_at=_ahora(),
    )
    s.add(nueva)
    await s.flush()
    return nueva.id


async def obtener(email: str, hilo_id: str) -> Optional[ChatThread]:
    """The thread, if it is this person's and not deleted. Another owner's → None.

    The ownership check goes HERE and not in the route: a thread belongs to a person, and
    leaving that decision to each endpoint is how the one that forgot to check it slips in.
    """
    e = (email or "").strip().lower()
    async with app_session() as s:
        return (await s.execute(select(ChatThread).where(
            ChatThread.id == (hilo_id or "").strip(),
            ChatThread.user_email == e,
            ChatThread.deleted_at.is_(None),
        ))).scalars().first()


async def necesita_nombre(hilo_id: str, email: str) -> bool:
    """Does this conversation still have the factory name?

    The title is looked at, not the turns: a thread can carry several exchanges and still be
    called «Conversación» if the agent never proposed anything. A name set by hand
    (`title_locked`) is never touched. Always bounded to THIS PERSON's row (v12.0.0)."""
    h = (hilo_id or "").strip()
    e = (email or "").strip().lower()
    if not h or not e:
        return False
    async with app_session() as s:
        fila = (await s.execute(
            select(ChatThread).where(ChatThread.id == h, ChatThread.user_email == e)
        )).scalars().first()
    if fila is None or fila.title_locked:
        return False
    t = (fila.title or "").strip()
    if t in ("", "Conversación"):
        return True
    # REPAIR. The titles that came from the in-band attempt are an echo of the instruction
    # itself ("three to five words saying what it is about") or pieces of the internal block.
    # They are treated as "no name" so they get re-titled on their own, without touching the
    # database by hand. The mark is unmistakable: nobody calls a conversation that.
    return esta_roto(t)


async def _sin_estrenar(email: str) -> ChatThread | None:
    """The open conversation that does not have a single turn yet, if there is one.

    "Unused" is measured by the TURNS, not by the title: someone may have renamed it before
    writing, and that is still the same empty conversation to go back to."""
    from app.app_models.runtime import AgentRun

    async with app_session() as s:
        filas = (await s.execute(
            select(ChatThread).where(
                ChatThread.user_email == email,
                ChatThread.deleted_at.is_(None),
                ChatThread.archived_at.is_(None),
            ).order_by(ChatThread.created_at.desc()).limit(10)
        )).scalars().all()
        from sqlalchemy import func
        for fila in filas:
            # The default one never counts as "unused": it is not reused when pressing "+".
            if fila.is_default:
                continue
            usados = (await s.execute(
                select(AgentRun.id).where(
                    AgentRun.thread_id == fila.id,
                    func.lower(func.json_extract(AgentRun.request_meta_json, "$.memory_user"))
                    == (email or "").strip().lower(),
                ).limit(1)
            )).scalars().first()
            if usados is None:
                return fila
    return None


async def crear(email: str, titulo: str = "") -> ChatThread:
    """Creates a thread. Raises `LimiteDeHilos` when over the cap."""
    e = (email or "").strip().lower()
    async with app_session() as s:
        cuantos = len((await s.execute(select(ChatThread.id).where(
            ChatThread.user_email == e,
            ChatThread.deleted_at.is_(None),
            ChatThread.archived_at.is_(None),
        ))).scalars().all())
    # If there is already an UNUSED conversation, that one is returned instead of creating
    # another. Without this, each "+" that is never used leaves a thread called
    # «Conversación», and since the titler only runs AFTER the first exchange, they stay there
    # forever: the list ends up with three or four identical ones that cannot be told apart.
    if not titulo:
        vacia = await _sin_estrenar(e)
        if vacia is not None:
            return vacia
    if cuantos >= MAX_CHAT_THREADS:
        raise LimiteDeHilos(
            f"Tienes {cuantos} conversaciones abiertas, que es el máximo. Archiva alguna "
            "para empezar otra: archivar no borra nada, solo la saca de la lista."
        )
    fila = ChatThread(
        id=nuevo_id(), user_email=e,
        title=(titulo or "Conversación")[:MAX_TITULO],
        title_locked=1 if titulo else 0,
        created_at=_ahora(), last_used_at=_ahora(),
    )
    async with app_write() as s:
        s.add(fila)
        await s.commit()
    return fila


async def hilo_de_invocacion(email: str, capacidad: str, *, prueba: bool = False) -> str:
    """The thread where THIS person's invocations of `capacidad` run (14.23.0, `app/invocacion.py`).

    One per person and capability, with a STABLE id (derived from both) and born ARCHIVED:

      · its own thread, so the API never writes into the conversation the person is having —an
        invocation is a long English instruction and a JSON, nothing to read in a chat—;
      · archived, so it does not crowd the list nor count towards `MAX_CHAT_THREADS` (an
        application cannot be refused because the person has many conversations open), yet
        the person can still find it among the archived ones: what was done in their name
        stays visible;
      · stable, so repeated invocations do not leave one row per call. Each one rotates the
        session (`reset`), so no invocation sees the previous one's context.

    `prueba` (15.7.0): a READ-ONLY test of the capability (`prueba_solo_lectura`) runs in its OWN
    thread, so its read-only session never shares a key with the real invocations.
    """
    import hashlib
    e = (email or "").strip().lower()
    cid = (capacidad or "").strip()
    hid = ("prueba-" if prueba else "api-") + hashlib.sha256(
        f"{e}|{cid}{'|prueba' if prueba else ''}".encode("utf-8")).hexdigest()[:20]
    if await obtener(e, hid) is not None:
        return hid
    async with app_write() as s:
        fila = (await s.execute(select(ChatThread).where(
            ChatThread.id == hid, ChatThread.user_email == e))).scalars().first()
        if fila is not None:
            # The person deleted it: the next invocation needs it back (archived, as born).
            fila.deleted_at, fila.archived_at = None, _ahora()
        else:
            s.add(ChatThread(
                id=hid, user_email=e, title=f"{'Prueba' if prueba else 'API'} · {cid}"[:MAX_TITULO], title_locked=1,
                is_default=0, created_at=_ahora(), last_used_at=_ahora(), archived_at=_ahora(),
            ))
        try:
            await s.commit()
        except Exception:  # noqa: BLE001 — two first invocations at once: the row exists
            await s.rollback()
    return hid


class LimiteDeHilos(Exception):
    """The cap of open threads was reached."""


async def tocar(hilo_id: str, email: str) -> None:
    """Marks the thread as used now: it is what decides whom to evict (and which one opens
    on entering, see `hilo_actual`). Bounded to THIS PERSON's row (v12.0.0): with the shared
    'default' id, each turn of B pushed A's conversation up in A's list."""
    async with app_write() as s:
        fila = (await s.execute(select(ChatThread).where(
            ChatThread.id == (hilo_id or "").strip(),
            ChatThread.user_email == (email or "").strip().lower(),
        ))).scalars().first()
        if fila is not None:
            fila.last_used_at = _ahora()
            await s.commit()


async def renombrar(email: str, hilo_id: str, titulo: str) -> bool:
    """Renames and LOCKS the title: from here on the model does not touch it again."""
    async with app_write() as s:
        fila = (await s.execute(select(ChatThread).where(
            ChatThread.id == (hilo_id or "").strip(),
            ChatThread.user_email == (email or "").strip().lower(),
            ChatThread.deleted_at.is_(None),
        ))).scalars().first()
        if fila is None:
            return False
        fila.title = (titulo or "").strip()[:MAX_TITULO] or fila.title
        fila.title_locked = 1
        await s.commit()
        return True


async def _titulo_del_modelo(pregunta: str, respuesta: str) -> str:
    """Asks for the title in a SEPARATE session. It is how ChatGPT does it, and for a reason.

    Asking for it IN BAND —the agent ending its answer with a mark— failed in the two worst
    possible ways on 31/08/2026: the mark leaked into the operator's conversation, and when
    the model repeated the instruction instead of obeying it, the EXAMPLE TEXT became the
    title ("three to five words saying what it is about"). A mark inside the answer cannot be
    told apart from an echo of the instruction that asks for it.

    Out of band that problem does not exist: it is another session, without tools and without
    history, and the only thing it can return is the title. It costs a few hundred tokens ONCE
    per conversation, and it is launched in the background so as not to delay the turn.
    """
    from app.agent_proxy import get_agent_proxy

    proxy = get_agent_proxy()
    sid = await proxy.create_session(role="autonomous")
    prompt = (
        "Title of this conversation in 3 to 5 words, in the user's language. "
        "Reply ONLY with the title, without quotes, without a final full stop and without "
        "explaining anything.\n\n"
        f"User: {(pregunta or '')[:600]}\n"
        f"Assistant: {(respuesta or '')[:900]}"
    )
    trozos: list[str] = []
    async for ev in proxy.stream_interactive(sid, prompt, timeout=45.0):
        if ev.get("type") == "delta":
            trozos.append(ev.get("delta") or "")
        elif ev.get("type") in ("done", "error"):
            break
    return "".join(trozos).strip()


async def titular_tras_el_primer_intercambio(
    hilo_id: str, pregunta: str, respuesta: str, email: str,
) -> None:
    """Names the conversation once it is known what it was about.

    Order: what the MODEL says in a separate session, and if that is not possible, the first
    words of the question. A «Conversación» never remains, which is what made the list have
    several identical, indistinguishable ones.

    It only acts while the conversation still has no name of its own: a title set by hand
    always rules, and one already generated is not rewritten on every turn.
    """
    if not await necesita_nombre(hilo_id, email):
        return
    # The fallback is ALWAYS computed first: if the call to the model fails or takes long,
    # the conversation already has a reasonable name instead of staying as «Conversación».
    await poner_titulo_sugerido(hilo_id, titulo_de(pregunta), email)
    try:
        propuesto = await _titulo_del_modelo(pregunta, respuesta)
    except Exception:  # noqa: BLE001 — a title is a convenience, it never breaks a turn
        logger.debug("hilos: no se pudo pedir el título al modelo", exc_info=True)
        return
    bueno = titulo_valido_del_modelo(propuesto, pregunta)
    if bueno:
        await poner_titulo_sugerido(hilo_id, bueno, email)


async def poner_titulo_sugerido(hilo_id: str, titulo: str, email: str) -> None:
    """Applies the title the model proposed. It does NOT overwrite one set by hand. And ONLY
    on this person's row (v12.0.0): with the shared 'default' id, A's conversation got titled
    with the summary of B's."""
    limpio = re.sub(r"\s+", " ", (titulo or "").strip()).strip('"“”«» ')[:MAX_TITULO]
    if not limpio:
        return
    async with app_write() as s:
        fila = (await s.execute(select(ChatThread).where(
            ChatThread.id == (hilo_id or "").strip(),
            ChatThread.user_email == (email or "").strip().lower(),
        ))).scalars().first()
        if fila is not None and not fila.title_locked:
            fila.title = limpio
            await s.commit()


async def archivar(email: str, hilo_id: str, archivar_si: bool = True) -> Optional[dict]:
    """Archiving REMOVES from the list without deleting anything. Returns which one is the
    default conversation AFTER the operation (`{"id_por_defecto": ...}`), or None if that
    conversation does not exist.

    Archiving the default one ROTATES it (v12.3.0): it leaves the list like any other and
    another empty one opens. Until 12.2.1 it was a 409 with the argument that "it is the one
    you go back to": that justifies that there is ALWAYS one, not that it has to be THIS one.
    The archived one keeps its session —archiving is reversible—, so when recovering it, it
    carries on where it was.
    """
    e = (email or "").strip().lower()
    real = await resolver_hilo(e, hilo_id)
    rotada: Optional[str] = None
    async with app_write() as s:
        fila = (await s.execute(select(ChatThread).where(
            ChatThread.id == real,
            ChatThread.user_email == e,
            ChatThread.deleted_at.is_(None),
        ))).scalars().first()
        if fila is None:
            return None
        if archivar_si and fila.is_default:
            rotada = await _rotar_por_defecto(s, fila)
        fila.archived_at = _ahora() if archivar_si else None
        await s.commit()
    return {"id_por_defecto": rotada or await asegurar_por_defecto(e)}


async def borrar(email: str, hilo_id: str) -> Optional[dict]:
    """Deletes the thread. Returns which one is the default AFTER (`{"id_por_defecto": ...}`),
    or None if it does not exist.

    The `agent_runs` history is NOT touched: it is the audit of the spend and of what the
    employee did, and that cannot depend on someone cleaning their list.

    Deleting the default one ROTATES it (v12.3.0): it disappears from the list and one
    carries on in another empty one, with new context. Until 12.2.1 it answered 409 —"use
    Clear context to start from scratch"—, which is exactly what the person was asking for
    in other words, and on top of that it left the conversation in front of them.

    And the thread's SESSION is released: it was live context in the agent pod —the 1 GiB
    one— of a conversation that no longer exists. That it would expire on its own from
    inactivity did not make it right.
    """
    e = (email or "").strip().lower()
    real = await resolver_hilo(e, hilo_id)
    rotada: Optional[str] = None
    async with app_write() as s:
        fila = (await s.execute(select(ChatThread).where(
            ChatThread.id == real,
            ChatThread.user_email == e,
            ChatThread.deleted_at.is_(None),
        ))).scalars().first()
        if fila is None:
            return None
        if fila.is_default:
            rotada = await _rotar_por_defecto(s, fila)
        fila.deleted_at = _ahora()
        await s.commit()
    # OUTSIDE the write block: it talks to the sister pod over the network, and `app_write`
    # serializes EVERYONE who writes behind however long it takes.
    await _olvidar_la_sesion(e, real)
    return {"id_por_defecto": rotada or await asegurar_por_defecto(e)}


async def _olvidar_la_sesion(email: str, hilo_id: str) -> None:
    """Releases the live context of a thread that has just been deleted. It never raises:
    deleting the conversation has already happened, and failing to clean the cache cannot undo it."""
    from app.agent_sessions import get_session_manager
    try:
        await get_session_manager().delete(clave_de(email, hilo_id))
    except Exception:  # noqa: BLE001
        logger.warning("hilos: no se pudo soltar la sesión de %s", hilo_id, exc_info=True)


async def marcar_compactado(email: str, hilo_id: str) -> Optional[str]:
    """Records that this thread has just been compacted. Returns the mark in UTC.

    Compacting summarises the thread INSIDE the pod: from here on the model no longer
    remembers in detail what came before, even though everything is still on screen. Without
    this mark that was not visible anywhere —neither on the web nor on mobile—, so the
    conversation stayed just as long right after lightening it inside, and nobody knew where
    the employee had lost the detail. It is stored in the THREAD and not in the client
    because it belongs to the conversation: whoever compacts from the phone has to see it
    folded on the laptop too.
    """
    e = (email or "").strip().lower()
    real = await resolver_hilo(e, hilo_id)
    ahora = _ahora()
    async with app_write() as s:
        fila = (await s.execute(select(ChatThread).where(
            ChatThread.id == real,
            ChatThread.user_email == e,
            ChatThread.deleted_at.is_(None),
        ))).scalars().first()
        if fila is None:
            return None
        fila.compacted_at = ahora
        await s.commit()
    return _con_zona(ahora)


async def compactado_en(email: str, hilo_id: str) -> Optional[str]:
    """When this thread was last compacted, or None."""
    fila = await obtener((email or "").strip().lower(), hilo_id)
    return _con_zona(fila.compacted_at) if fila is not None else None


async def historial(email: str, hilo_id: str, limite: int = 100) -> list[dict]:
    """A thread's turns, from oldest to newest, to repaint the conversation.

    It comes from `agent_runs`, which stores `prompt` and `response_md` of each turn since
    long before threads existed. That is why this phase does not need to start storing
    anything.

    Two limits that have to be said on screen and not hidden:

      · **It brings neither tools nor reasoning.** That only exists in the live channel. A
        reopened thread looks thinner than when it happened.
      · **Turns prior to the update have no thread** (`thread_id` NULL) and cannot be
        attributed without guessing whose they were. They stay out: showing someone else's
        conversation by rebuilding it with a heuristic would be much worse than starting the
        history where it started being recorded.

    Isolation is guaranteed by THIS function, filtering by the run's person: it CANNOT be
    trusted that the caller has checked it —the default thread does not even go through
    `obtener`, and its id is common to all—.
    """
    from app.app_models.runtime import AgentRun
    from sqlalchemy import func

    e = (email or "").strip().lower()
    if not e:
        return []
    # v12.0.0 — the "default" alias (and the per-person id) resolves to the caller's row;
    # that way an old client that keeps sending 'default' sees its history, and the
    # per-person filter below is still the guarantee even if the id were someone else's.
    h = await resolver_hilo(e, hilo_id)
    if not h:
        return []
    async with app_session() as s:
        filas = (await s.execute(
            select(AgentRun)
            .where(
                AgentRun.thread_id == h,
                AgentRun.mode == "chat",
                # ISOLATION PER PERSON (security). The default thread ("default") shares an
                # id among ALL people —each has their own 'default' ChatThread—, so filtering
                # ONLY by thread_id returned the turns of other people of the same pod: a leak
                # between users (one's answer appeared on another's phone). The identity is
                # DERIVED from the run (`request_meta_json.memory_user`, stamped by the chat
                # endpoint when starting the turn), NEVER from what the client sends. A run
                # without `memory_user` (old ones) stays out: better to lose an old line than
                # to show someone else's.
                func.lower(func.json_extract(
                    AgentRun.request_meta_json, "$.memory_user")) == e,
            )
            # THE LAST `limite`, NOT THE FIRST. With `asc()` + `limit`, SQL returns the
            # BEGINNING of the thread: whoever asked for "the last 8" got the 8 oldest.
            # Measured on 22/09/2026 in the travel assistant — thread of 11 turns, the
            # reception asked for 8 and got 1 to 8, so the person said "three adults three
            # children" in 9 and two messages later was asked again how many they were. It
            # was not the model forgetting: it was the window looking at the past.
            # And for repainting a long conversation, just as bad: it opened at the
            # beginning and today's did not show.
            .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
            .limit(max(1, min(limite, 500)))
        )).scalars().all()
    # They are requested from newest to oldest and returned reversed: to the outside this
    # function promises chronological order, and that does not change.
    filas = list(reversed(filas))
    fuera: list[dict] = []
    for f in filas:
        fuera.append({
            "cuando": f.started_at.isoformat() if f.started_at else None,
            "pregunta": f.prompt or "",
            "respuesta": f.response_md or "",
            "error": f.error or None,
            "estado": f.status,
            # WHO handled the turn: the agent, the written reception or a shortcut. Since
            # 12.26.0 not all turns go through the agent, and from outside the three answers
            # are identical —all are `response_md`—. Without this it cannot be known what the
            # agent has seen of the conversation, and that is exactly the count needed so as
            # not to leave gaps in its memory.
            "atendido_por": _quien_atendio(f.request_meta_json),
            # And WHAT the reception did: `contesto` or `pregunto`. Without telling them apart
            # it cannot be known whether the previous turn left a question hanging, which is
            # what decides whether to ask again or to get to work (12.52.0).
            "recepcion": _que_hizo_la_recepcion(f.request_meta_json),
        })
    return fuera


def _que_hizo_la_recepcion(meta_json: str | None) -> str:
    """`contesto` | `pregunto` | "" (it was not the reception)."""
    try:
        import json as _json
        m = (_json.loads(meta_json or "{}") or {}).get("medicion") or {}
    except Exception:  # noqa: BLE001
        return ""
    v = str(m.get("recepcion") or "")
    return v if v in ("contesto", "pregunto") else ""


def _quien_atendio(meta_json: str | None) -> str:
    """`agente` | `recepcion` | `atajo`, read from the turn's measurement."""
    try:
        import json as _json
        m = (_json.loads(meta_json or "{}") or {}).get("medicion") or {}
    except Exception:  # noqa: BLE001
        return "agente"
    if m.get("atajo"):
        return "atajo"
    if (m.get("recepcion") or "") in ("contesto", "pregunto"):
        return "recepcion"
    return "agente"


async def sesiones_a_desalojar(email: str, hilo_vivo: str) -> list[str]:
    """Which sessions are left over so that `hilo_vivo` fits, from the least used upwards.

    It returns session keys, NOT threads: what is evicted is the context: the thread stays
    in the list and is re-primed when writing in it again. It is exactly what the code
    agent's pods already do on inactivity, and for the same reason — the pod's memory is
    finite and whoever uses it least misses it least.
    """
    e = (email or "").strip().lower()
    prefijo = f"interactive:{e}"
    async with app_session() as s:
        filas = (await s.execute(select(AgentSession).where(
            AgentSession.user_email == e, AgentSession.role == "interactive",
        ))).scalars().all()
    mias = [f for f in filas if f.scope_key == prefijo or f.scope_key.startswith(prefijo + ":")]
    clave_viva = clave_de(e, hilo_vivo)
    otras = [f for f in mias if f.scope_key != clave_viva]
    # `hilo_vivo` plus (MAX-1) of the others fit; the least used are left over.
    sobran = len(otras) - (MAX_CONCURRENT_CHATS - 1)
    if sobran <= 0:
        return []
    otras.sort(key=lambda f: f.last_used_at or datetime.min)
    return [f.scope_key for f in otras[:sobran]]
