"""Braking the writing is cheaper than cleaning up afterwards (7.164.0).

Measured on the Vigía: 89 new learnings in one day, 31 on a single subject and 21 pairs
saying the same thing. Nothing warned the agent BEFORE writing. And the asymmetry is huge:
merging afterwards costs a model call, not writing costs nothing.

The warning enters the prompt only when there is something to warn about — otherwise the
prompt is not cluttered, which is what keeps these warnings being read.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import pytest

_DB = os.path.join(tempfile.mkdtemp(), "presion.sqlite")
os.environ["APP_DB_PATH"] = _DB
os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["AGENT_SELF_MEMORY_SATURATED_AT"] = "4"

# And if `app.config` is ALREADY imported, the environment variable arrives TOO LATE: that
# module reads its value on import and keeps the default 8. Here that depends on which other
# test files exist and in what order pytest collects them — and a generated app has more files
# than this scaffold, so the same test passes alone and fails in the whole suite. A result
# that depends on who ran before is not a result.
#
# The ATTRIBUTE is forced too, since that is what is really consulted: `agent_memory_utils`
# does the `from app.config import …` INSIDE the function, so it reads the live value.
if "app.config" in sys.modules:
    sys.modules["app.config"].AGENT_SELF_MEMORY_SATURATED_AT = 4


def _run(coro):
    # A NEW loop per call, like the rest of the suite: `get_event_loop()` blows up in
    # 3.12+ when the thread has no loop, and it only showed when running the whole
    # suite (alone there was a loop from another module).
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


# Truly distinct facts, reused by the cases that need to saturate.
_DISTINTOS = (
    "La planificacion reserva mas CPU de la que consume",
    "Los pods del tenant arrancan en menos de veinte segundos",
    "El disco del nodo cuatro va mas lleno que el resto",
    "Las replicas de la base de datos van con retraso por la noche",
    "El certificado del dominio principal caduca cada noventa dias",
    "La cola de indexado se vacia sola tras el pico de la manana",
)


def _saturar(subject: str) -> None:
    for frase in _DISTINTOS:
        _run(_guardar(frase, subject=subject))


def test_sin_saturacion_no_dice_nada():
    """A warning that always shows stops being read."""
    from app.agent_memory_prompt import saturation_warning
    _run(_guardar("Un hecho suelto", subject="tranquilo:uno"))
    assert _run(saturation_warning()) == ""


def test_con_un_asunto_saturado_avisa_ANTES_de_escribir():
    from app.agent_memory_prompt import saturation_warning
    # TRULY distinct facts: with six variations of the same sentence the deduplicator
    # itself merges them (that is its job) and there would be no saturation to measure.
    _saturar("lleno:uno")
    aviso = _run(saturation_warning())
    assert "lleno:uno" in aviso
    assert "do NOT open a new learning" in aviso
    assert "write nothing" in aviso, (
        "no basta con avisar: hay que decirle que callarse es una respuesta válida")


def test_el_informe_cuenta_hechos_vivos_y_repeticion_medida():
    """Self-sufficient on purpose: it creates ITS own subject instead of inheriting the
    previous case's. A test that depends on order lies as soon as someone reorders."""
    from app.agent_memory_utils import saturation_report
    _saturar("informe:uno")
    informe = {x["subject"]: x for x in _run(saturation_report(top=5))}
    assert "informe:uno" in informe
    assert informe["informe:uno"]["entries"] >= 6
    assert "redundant_pairs" in informe["informe:uno"]


def test_lo_retirado_no_cuenta_para_saturar():
    """If it counted, the subject would stay "saturated" after being cleaned and the
    agent would never write about it again."""
    from sqlalchemy import update as sql_update
    from app.agent_memory_utils import saturation_report
    from app.app_database import app_write
    from app.app_models.agent_memory import AgentMemory

    _saturar("retirado:uno")
    assert "retirado:uno" in {x["subject"] for x in _run(saturation_report(top=9))}

    async def _retirar_todo():
        async with app_write() as s:
            await s.execute(sql_update(AgentMemory)
                            .where(AgentMemory.subject == "retirado:uno")
                            .values(review_state="rejected"))
            await s.commit()
    _run(_retirar_todo())
    assert "retirado:uno" not in {x["subject"] for x in _run(saturation_report(top=9))}
