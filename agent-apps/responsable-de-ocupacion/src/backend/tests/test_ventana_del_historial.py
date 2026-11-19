"""The history window looks at the PRESENT, not at the start of the thread (12.49.0).

`historial(email, hilo, limite=N)` returned the N OLDEST turns: `ORDER BY started_at
ASC` + `LIMIT` is the start of the conversation, not the end. With a high limit (the
screen asks for 100) it was almost never noticed; with a low one, always.

WHAT WAS SEEN (22/09/2026, travel assistant, 11-turn thread). The reception —the piece
that decides, BEFORE calling the employee, whether to answer, ask or delegate— asks for the last 8
so as not to ask again what was already said. It received 1 to 8. The person said "three adults
three children" in turn 9, and two messages later was asked again how many they were:

    Find a hotel in Madrid now          → dates, travellers, budget?
    In the morning one day three adults… → dates and ages?
    For tomorrow                        → HOW MANY TRAVELLERS ARE YOU…?   ← already said

It was not the model forgetting: it was the window showing it the past. That is why this is tested
with MORE turns than the limit — with fewer, both versions give the same result and the test proves nothing.
"""
from __future__ import annotations

import os
import tempfile

import pytest


CORREO = "ana@ejemplo.com"


@pytest.mark.asyncio
async def test_se_devuelven_los_ultimos_y_en_orden() -> None:
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    from datetime import datetime, timedelta

    from app.app_database import AppBase, app_engine, app_session
    from app.app_models import runtime  # noqa: F401
    from app.app_models.runtime import AgentRun, ChatThread
    from app import chat_threads as T

    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)

    base = datetime(2026, 9, 22, 19, 0, 0)
    async with app_session() as s:
        s.add(ChatThread(id="hilo1", user_email=CORREO, title="Conversación",
                         is_default=1, created_at=base, last_used_at=base))
        # ELEVEN turns, like the real case. The eighth is the boundary: with the bug, the window
        # of 8 stopped right there and the last three did not exist for whoever decides.
        for i in range(1, 12):
            s.add(AgentRun(
                idempotency_key=f"t{i}", status="COMPLETED", mode="chat",
                thread_id="hilo1", prompt=f"mensaje {i}", response_md=f"respuesta {i}",
                started_at=base + timedelta(minutes=i),
                request_meta_json=f'{{"memory_user": "{CORREO}"}}',
            ))
        await s.commit()

    turnos = await T.historial(CORREO, "hilo1", limite=8)

    preguntas = [t["pregunta"] for t in turnos]
    assert preguntas == [f"mensaje {i}" for i in range(4, 12)], (
        "la ventana tiene que traer los ÚLTIMOS 8 (del 4 al 11), en orden cronológico; "
        f"trajo: {preguntas}"
    )
    # And what really mattered: the last thing said is ALWAYS there.
    assert preguntas[-1] == "mensaje 11"

    os.remove(path)


@pytest.mark.asyncio
async def test_un_hilo_mas_corto_que_la_ventana_se_devuelve_entero() -> None:
    """The other side of the same rule: asking for 8 from a 3-turn thread returns the 3, in order.
    Without this, a misplaced `reversed()` would pass the test above and break the common case."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    from datetime import datetime, timedelta

    from app.app_database import AppBase, app_engine, app_session
    from app.app_models import runtime  # noqa: F401
    from app.app_models.runtime import AgentRun, ChatThread
    from app import chat_threads as T

    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)

    base = datetime(2026, 9, 22, 19, 0, 0)
    async with app_session() as s:
        s.add(ChatThread(id="hilo2", user_email=CORREO, title="Conversación",
                         is_default=1, created_at=base, last_used_at=base))
        for i in range(1, 4):
            s.add(AgentRun(
                idempotency_key=f"c{i}", status="COMPLETED", mode="chat",
                thread_id="hilo2", prompt=f"mensaje {i}", response_md=f"respuesta {i}",
                started_at=base + timedelta(minutes=i),
                request_meta_json=f'{{"memory_user": "{CORREO}"}}',
            ))
        await s.commit()

    turnos = await T.historial(CORREO, "hilo2", limite=8)
    assert [t["pregunta"] for t in turnos] == ["mensaje 1", "mensaje 2", "mensaje 3"]

    os.remove(path)
