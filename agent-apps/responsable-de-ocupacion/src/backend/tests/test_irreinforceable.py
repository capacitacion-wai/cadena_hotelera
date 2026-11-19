"""Irreinforceability is measured instead of guessing the cause (7.165.0).

The 7.157.0 rule looked for the date in the text. It worked for what was written
while looking at it, but fell short: after trimming the tag line («On 29 July 2026, …»)
the fact still carried the numbers of the moment —«four workers went over 85%, with a
peak of 92.34% on usewai-wk-2»— and tomorrow those numbers are different. Different fingerprint, new
row, one observation, never consolidates. **Measured in the Vigía: 50 of 107 were still
unrepeatable after cleaning the date.**

Banning numbers by regex would have been worse: «the sweep takes 30-45 min» is a
legitimate fact and one of the most useful it can learn. So we measure the RESULT — an
observation that has been sitting for days with ONE single sighting while its subject has not
stopped receiving writes is not new: it simply never matches itself.

The active-subject condition is what avoids the obvious false positive: in a quiet
subject, a single sighting means «it has not happened again», not «it cannot be reinforced».
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta

import pytest

_DB = os.path.join(tempfile.mkdtemp(), "irref.sqlite")
os.environ["APP_DB_PATH"] = _DB
os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"


# Working age: GREATER than the irreinforceable threshold (3 days) and LESS than the grace of
# the disuse pruning (7). Without that window the tests would not test what they claim to —
# with 10 days everything fell to the disuse rule and this rule was never exercised.
_EDAD = 5


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(scope="module", autouse=True)
def _schema():
    from app.app_database import AppBase, app_engine
    from app import app_models  # noqa: F401

    async def _crear():
        async with app_engine.begin() as conn:
            await conn.run_sync(AppBase.metadata.create_all)
    _run(_crear())
    yield


async def _guardar(content, subject):
    from app.agent_memory_utils import remember
    return await remember(subject=subject, content=content, kind="observation")


def _envejecer(mid, dias):
    from sqlalchemy import update as sql_update
    from app.app_database import app_write
    from app.app_models.agent_memory import AgentMemory

    async def _go():
        async with app_write() as s:
            await s.execute(sql_update(AgentMemory).where(AgentMemory.id == mid)
                            .values(created_at=datetime.utcnow() - timedelta(days=dias)))
            await s.commit()
    _run(_go())


def _estado(mid):
    from sqlalchemy import select
    from app.app_database import app_session
    from app.app_models.agent_memory import AgentMemory

    async def _go():
        async with app_session() as s:
            return (await s.execute(
                select(AgentMemory.review_state)
                .where(AgentMemory.id == mid))).scalar()
    return _run(_go())


def test_una_vista_en_un_asunto_QUE_SIGUE_VIVO_se_retira():
    """The real case: numbers of the day, so it never matches itself again."""
    from app.agent_memory_utils import compact

    vieja = _run(_guardar("Cuatro workers superaron el ochenta y cinco por ciento hoy",
                          subject="irref:activo"))
    _envejecer(vieja["id"], _EDAD)
    # The subject keeps receiving writes: there is something newer than it.
    _run(_guardar("Tres workers rondaron el setenta por ciento esta tarde",
                  subject="irref:activo"))

    _run(compact())
    assert _estado(vieja["id"]) == "rejected"


def test_en_un_asunto_TRANQUILO_una_sola_vista_sobrevive():
    """Here one sighting means «it has not happened again», not «it cannot be reinforced».
    Without this distinction the rule would eat the knowledge of everything stable."""
    from app.agent_memory_utils import compact

    sola = _run(_guardar("El certificado del dominio se renueva sin intervencion",
                         subject="irref:tranquilo"))
    _envejecer(sola["id"], _EDAD)

    _run(compact())
    assert _estado(sola["id"]) != "rejected"


def test_lo_reforzado_no_se_toca():
    """Two sightings already prove the fact CAN be repeated."""
    from app.agent_memory_utils import compact

    texto = "La replica de la base de datos va con retraso por la noche"
    e = _run(_guardar(texto, subject="irref:reforzado"))
    _run(_guardar(texto, subject="irref:reforzado"))   # reinforces: observations = 2
    _envejecer(e["id"], _EDAD)
    _run(_guardar("Otro hecho distinto del mismo asunto", subject="irref:reforzado"))

    _run(compact())
    assert _estado(e["id"]) != "rejected"


def test_lo_importante_sigue_a_salvo():
    """Same consistency as the rest of the cycle: if the queue asks you about it, it is not deleted."""
    from sqlalchemy import update as sql_update
    from app.agent_memory_utils import compact
    from app.app_database import app_write
    from app.app_models.agent_memory import AgentMemory
    from app.config import AGENT_SELF_MEMORY_IMPORTANT_FLOOR

    grave = _run(_guardar("El nodo cuatro se queda sin espacio de disco",
                          subject="irref:grave"))

    async def _marcar():
        async with app_write() as s:
            await s.execute(sql_update(AgentMemory).where(AgentMemory.id == grave["id"])
                            .values(importance=float(AGENT_SELF_MEMORY_IMPORTANT_FLOOR),
                                    created_at=datetime.utcnow() - timedelta(days=_EDAD)))
            await s.commit()
    _run(_marcar())
    _run(_guardar("Otra cosa cualquiera del mismo asunto", subject="irref:grave"))

    _run(compact())
    assert _estado(grave["id"]) != "rejected"


# ── The self-reference guard ───────────────────────────────────────────
def test_un_aprendizaje_no_puede_citar_su_propia_memoria():
    """7 entries in the Vigía said «observations 27, 35, 36… show that…»: the
    maintenance run summarised its memory into a NEW row instead of merging the
    originals, so the cleanup fattened what it came to clean."""
    from app.agent_memory_utils import normalize_content

    _, motivo = normalize_content(
        "Las observaciones 27, 35 y 36 muestran cuatro o mas workers saturados")
    assert motivo and "supersedes" in motivo, (
        "hay que decirle QUÉ hacer en su lugar, no solo que está mal")


@pytest.mark.parametrize("texto", [
    "El barrido cubre 18 de 18 tenants",
    "La cola de indexado tarda 30-45 min",
    "Hay 3 nodos de control y 9 workers",
    "El pod se reinicia 5 veces al dia",
])
def test_los_numeros_LEGITIMOS_no_se_tocan(texto):
    """Banning figures would have been the easy fix and the costly mistake: half of the
    employee's useful memory is magnitudes."""
    from app.agent_memory_utils import normalize_content
    _, motivo = normalize_content(texto)
    assert not (motivo or "").startswith("the content cites"), texto
