"""In-memory registry of live chat turns (v6.66.0).

COPIED VERBATIM by ``wai-agent-app-builder`` into every generated
agent-app at ``src/backend/app/chat_runtime.py``.

WHY THIS EXISTS
===============
Until v6.65.x the chat answer was streamed **inside** the
``POST /api/agent/chat`` request: the browser owned the run. Switching
tabs (component unmount → ``AbortController.abort()``) or refreshing the
page closed that SSE socket, FastAPI raised ``CancelledError``, the proxy
closed its ``httpx`` stream to the sidecar, and the agent turn was
*actually cancelled* — the in-flight answer was lost.

This module mirrors the proven WAI **code agent** model
(``opencode_api`` → ``send_message_stream`` + ``reconnect_stream``):

* the turn runs in a **detached** ``asyncio.Task`` that is independent of
  any HTTP request,
* every UI event the agent produces is appended to a per-operator ring
  buffer (``ChatTurn.events``) keyed by the session ``scope_key``,
* the browser's ``POST /chat`` and any later ``GET /chat/stream`` reconnect
  are mere **subscribers** that replay the buffer from an index and then
  follow the live tail until the turn reaches a terminal state.

So a tab switch / refresh no longer cancels the run: it keeps executing
server-side and the SPA re-attaches on return, picking the answer up
exactly where it left off.

SINGLE-WORKER ONLY
==================
The buffer lives in the process, exactly like ``SessionManager`` and its
locks (``app.agent_sessions``). The generated agent-app runs ONE uvicorn
worker; do not scale ``--workers`` past 1 without moving this registry
(and the session registry) to a shared store (Redis / DB), or a reconnect
could land on a worker that never saw the turn.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)

# How long a finished turn lingers in the registry so a late reconnect
# (e.g. the operator returns to the tab a minute after it completed) can
# still replay the full, finalised transcript before we free the memory.
_FINISHED_TTL_S = 300.0

# Safety cap so a pathological turn can't grow the buffer without bound.
_MAX_EVENTS = 20_000

# 15.12.0 — the HEARTBEAT. A step that takes minutes (a 12.590-key ERP batch, reading a big
# result) sent nothing to the screen: the person saw a spinner frozen on «step 25» and thought
# the employee had hung (COBSA, 10/11/2026), and the client's own silence watchdog could drop the
# live stream. After `_LATIDO_TRAS_S` of silence the turn emits a `latido` event —which tool is
# running and for how long— and repeats it every `_LATIDO_CADA_S`. It is not activity: it never
# resets the silence it reports.
_LATIDO_TRAS_S = 50.0
_LATIDO_CADA_S = 30.0
_LATIDO_MIRA_S = 10.0

# Terminal states.
_RUNNING = "running"
_COMPLETE = "complete"
_ERROR = "error"


@dataclass
class ChatTurn:
    """One chat turn executing server-side, decoupled from the browser.

    ``events`` is an append-only list of UI-vocabulary dicts (``session``,
    ``text_delta``, ``tool_call``, ``tool_result``, ``step_done``,
    ``complete``, ``error``) — exactly what the SPA expects. Subscribers
    index into it via ``subscribe(from_index)``.
    """

    scope_key: str
    session_id: str
    run_id: int | None = None
    # v12.1.0 — what the CLIENT of this turn can do (e.g. ``ubicacion``). It travels per
    # turn and not in the employee's prompt because the prompt belongs to the employee and
    # the client changes: web, mobile, a channel, or an autonomous run with nobody in front.
    # It is stored here so a tool can look at it IN THE CALL — which is where filtering has
    # to happen, because opencode fixes a session's tools when creating it and they cannot
    # be removed per turn.
    capacidades: list[str] = field(default_factory=list)
    # v12.55.0 — which phone to notify when it finishes (Expo token) and which thread it
    # belongs to. It goes with the turn because the notification is for THIS turn: see
    # `app/aviso_push.py`.
    aviso_push: str | None = None
    hilo: str | None = None
    # 14.23.0 — the RAW text the agent wrote in this turn, set when it ends. What the events carry
    # is the visible text, normalised for Markdown (a dash glued to a word gets a line break): a
    # JSON does not survive that, and `invocar` (`app/invocacion.py`) parses the answer.
    texto: str = ""
    events: list[dict] = field(default_factory=list)
    # 15.12.0 — for the heartbeat: when the last REAL event arrived, and the tools still running
    # (call_id → (name, since)), so the beat can say what it is waiting for.
    ultimo_evento_at: float = field(default_factory=time.time)
    en_marcha: dict = field(default_factory=dict)
    status: str = _RUNNING
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    task: asyncio.Task | None = None
    _cond: asyncio.Condition = field(default_factory=asyncio.Condition)

    @property
    def is_running(self) -> bool:
        return self.status == _RUNNING

    async def append(self, event: dict) -> None:
        """Append one UI event and wake every subscriber."""
        tipo = event.get("type") if isinstance(event, dict) else None
        if tipo != "latido":
            ahora = time.time()
            self.ultimo_evento_at = ahora
            clave = str(event.get("call_id") or event.get("name") or "")
            if tipo == "tool_call":
                self.en_marcha[clave] = (str(event.get("name") or ""), ahora)
            elif tipo == "tool_result":
                self.en_marcha.pop(clave, None)
        async with self._cond:
            if len(self.events) < _MAX_EVENTS:
                self.events.append(event)
            self._cond.notify_all()

    def latido(self, ahora: float | None = None) -> dict | None:
        """The heartbeat event due now, or None. Pure over the turn's state (tested alone): the
        oldest tool still running and how long it has been running, else how long the silence is."""
        ahora = time.time() if ahora is None else ahora
        silencio = ahora - self.ultimo_evento_at
        if not self.is_running or silencio < _LATIDO_TRAS_S:
            return None
        if self.en_marcha:
            nombre, desde = min(self.en_marcha.values(), key=lambda v: v[1])
            return {"type": "latido", "herramienta": nombre, "segundos": int(ahora - desde)}
        return {"type": "latido", "herramienta": "", "segundos": int(silencio)}

    async def finish(self, status: str) -> None:
        """Mark the turn terminal (``complete`` / ``error``) and wake all
        subscribers so their ``subscribe`` loops can drain + return."""
        async with self._cond:
            if self.status == _RUNNING:
                self.status = status if status in (_COMPLETE, _ERROR) else _ERROR
                self.finished_at = time.time()
            self._cond.notify_all()

    async def subscribe(self, from_index: int = 0) -> AsyncIterator[tuple[int, dict]]:
        """Yield ``(index, event)`` from ``from_index`` onward.

        Replays everything already buffered, then blocks on the condition
        for new events, returning once the turn is terminal and the buffer
        is fully drained. The browser passes ``from_index`` so a reconnect
        skips events it already rendered; a fresh ``POST`` passes ``0``.

        Events are yielded OUTSIDE the lock so a slow client can never
        stall the producer (the detached runner task).
        """
        idx = max(0, int(from_index))
        while True:
            async with self._cond:
                while idx >= len(self.events) and self.status == _RUNNING:
                    await self._cond.wait()
                pending = self.events[idx:]
                terminal = self.status != _RUNNING
            for event in pending:
                yield idx, event
                idx += 1
            if terminal and idx >= len(self.events):
                return


# ── Process-wide registry (one live turn per operator scope) ───────────
_TURNS: dict[str, ChatTurn] = {}
_REGISTRY_LOCK = asyncio.Lock()

# Runner contract: an async callable that drives the turn, appending events
# via ``turn.append(...)`` and returning ``"complete"`` (success) or
# ``"error"``. Raising is also treated as an error (an ``error`` event is
# synthesised). It must NOT call ``turn.finish`` itself — the wrapper does.
TurnRunner = Callable[[ChatTurn], Awaitable[str]]


async def get_turn(scope_key: str) -> ChatTurn | None:
    """Return the live (or recently-finished, within TTL) turn for a scope."""
    async with _REGISTRY_LOCK:
        return _TURNS.get(scope_key)


async def start_turn(
    scope_key: str,
    session_id: str,
    run_id: int | None,
    runner: TurnRunner,
    capacidades: list[str] | None = None,
    aviso_push: str | None = None,
    hilo: str | None = None,
) -> ChatTurn:
    """Register a new turn for ``scope_key`` and launch its detached task.

    If a turn is already running for this scope it is superseded: its task is
    cancelled so the sidecar stream is released. Since 15.11.0 the chat route
    refuses a new message while a turn runs (`TURN_IN_PROGRESS`), so this only
    happens in a race; superseding does NOT stop the agent's run.
    """
    turn = ChatTurn(scope_key=scope_key, session_id=session_id, run_id=run_id,
                    capacidades=list(capacidades or []), aviso_push=aviso_push, hilo=hilo)
    async with _REGISTRY_LOCK:
        old = _TURNS.get(scope_key)
        if old is not None and old.is_running and old.task and not old.task.done():
            logger.info("chat_runtime: superseding running turn for %s", scope_key)
            old.task.cancel()
        _TURNS[scope_key] = turn

    turn.task = asyncio.create_task(_run_and_cleanup(turn, runner))
    asyncio.create_task(_latidos(turn))
    return turn


async def _latidos(turn: ChatTurn) -> None:
    """Emit the heartbeat while the turn runs (see `_LATIDO_TRAS_S`). Ends with the turn."""
    ultimo = 0.0
    try:
        while turn.is_running:
            await asyncio.sleep(_LATIDO_MIRA_S)
            ahora = time.time()
            ev = turn.latido(ahora)
            if ev is not None and ahora - ultimo >= _LATIDO_CADA_S:
                ultimo = ahora
                await turn.append(ev)
    except Exception:  # noqa: BLE001 — a heartbeat can never bring a turn down
        logger.debug("chat_runtime: heartbeat stopped for %s", turn.scope_key, exc_info=True)


async def _run_and_cleanup(turn: ChatTurn, runner: TurnRunner) -> None:
    status = _ERROR
    try:
        status = await runner(turn) or _COMPLETE
    except asyncio.CancelledError:
        # Superseded by a newer turn or app shutdown: finalise as error so
        # any subscriber's loop terminates, then re-raise so the task is
        # marked cancelled.
        try:
            await turn.finish(_ERROR)
        finally:
            raise
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("chat_runtime: turn runner crashed for %s", turn.scope_key)
        try:
            await turn.append({"type": "error", "message": str(exc)})
        except Exception:
            pass
        status = _ERROR
    finally:
        # Reached on normal completion and on the synthesised-error path
        # (not on CancelledError, which re-raises above after finishing).
        if turn.status == _RUNNING:
            await turn.finish(status)

    # The mobile notification, AFTER closing the turn and without waiting for it: whoever
    # is watching already has their ending, and a slow third-party service cannot delay
    # anyone. A cancellation (turn replaced or pod stopping) does not reach here: it
    # re-raises above.
    if turn.aviso_push:
        from app.aviso_push import avisar
        asyncio.create_task(avisar(turn.aviso_push, list(turn.events), turn.status, turn.hilo))

    _schedule_expiry(turn)


def _schedule_expiry(turn: ChatTurn) -> None:
    """Drop the turn from the registry ``_FINISHED_TTL_S`` after it ends,
    so late reconnects can still replay it but memory is eventually freed."""

    async def _expire() -> None:
        await asyncio.sleep(_FINISHED_TTL_S)
        async with _REGISTRY_LOCK:
            if _TURNS.get(turn.scope_key) is turn:
                del _TURNS[turn.scope_key]

    try:
        asyncio.create_task(_expire())
    except RuntimeError:  # pragma: no cover — no running loop
        pass


__all__ = ["ChatTurn", "get_turn", "start_turn", "TurnRunner"]
