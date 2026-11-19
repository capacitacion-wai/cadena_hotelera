"""«Usada» now means «helped», not «was selected» (7.164.0).

The hole: `access_count` only goes up when the entry makes it into the block, and the block
takes a quota per kind ordered by repetition. Measured on the Vigía: 37 live observations
and **6** slots — the other 31 had `access_count = 0` by construction, not for being useless.
Pruning decided on that number: it was measuring RANK and calling it usefulness.

This pins the other half: what happened AFTERWARDS. When a run finishes, the outcome is
distributed among the learnings it carried. It is not causality —being present does not
prove having helped— and that is why there are two counters: what informs is the ratio
across many runs.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("AGENT_SELF_MEMORY_ENABLED", "1")
_DB = os.path.join(tempfile.mkdtemp(), "feedback.sqlite")
os.environ["APP_DB_PATH"] = _DB
os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"


def _run(coro):
    # A NEW loop per call, like the rest of the suite: `get_event_loop()` blows up
    # on 3.12+ when there is no loop in the thread, and it only showed when running
    # the whole suite (on its own there was a loop left from another module).
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


async def _guardar(content, subject="bucle:uno"):
    from app.agent_memory_utils import remember
    return await remember(subject=subject, content=content, kind="observation")


def test_una_ejecucion_que_sale_bien_puntua_lo_que_llevaba_dentro():
    from app.agent_memory_utils import attribute_outcome, record_injection
    from sqlalchemy import select
    from app.app_database import app_session
    from app.app_models.agent_memory import AgentMemory

    e = _run(_guardar("El barrido nocturno tarda unos 40 minutos"))
    _run(record_injection(4242, [e["id"]]))
    assert _run(attribute_outcome(4242, True)) == 1

    async def _leer():
        async with app_session() as s:
            return (await s.execute(
                select(AgentMemory.used_ok, AgentMemory.used_ko)
                .where(AgentMemory.id == e["id"]))).first()
    ok, ko = _run(_leer())
    assert (ok, ko) == (1, 0)


def test_una_que_sale_mal_puntua_en_la_otra_columna():
    from app.agent_memory_utils import attribute_outcome, record_injection
    from sqlalchemy import select
    from app.app_database import app_session
    from app.app_models.agent_memory import AgentMemory

    e = _run(_guardar("Esta métrica no significa lo que parece", subject="bucle:dos"))
    _run(record_injection(4343, [e["id"]]))
    _run(attribute_outcome(4343, False))

    async def _leer():
        async with app_session() as s:
            return (await s.execute(
                select(AgentMemory.used_ok, AgentMemory.used_ko)
                .where(AgentMemory.id == e["id"]))).first()
    assert _run(_leer()) == (0, 1)


def test_el_enlace_se_consume_para_no_puntuar_dos_veces():
    """If the link survived, a retry of the same run would add again and the ratio
    would stop meaning anything."""
    from app.agent_memory_utils import attribute_outcome, record_injection

    e = _run(_guardar("Un hecho cualquiera", subject="bucle:tres"))
    _run(record_injection(4444, [e["id"]]))
    assert _run(attribute_outcome(4444, True)) == 1
    assert _run(attribute_outcome(4444, True)) == 0, "el enlace debería estar consumido"


def test_sin_run_id_no_hace_nada_y_no_revienta():
    """The chat path has no run: it must degrade silently."""
    from app.agent_memory_utils import attribute_outcome, record_injection
    _run(record_injection(None, [1, 2, 3]))
    assert _run(attribute_outcome(None, True)) == 0


def test_haber_ayudado_cuenta_para_la_confianza():
    from app.agent_memory_utils import trust_level
    base = {"observations": 2, "access_count": 0, "used_ok": 0, "source": "agent",
            "review_state": "new"}
    assert trust_level(base) == "media"
    assert trust_level({**base, "used_ok": 2}) == "alta", (
        "dos ejecuciones buenas llevándolo dentro es evidencia de utilidad")


def test_lo_que_a_uno_le_CONTARON_no_sube_solo_a_confianza_alta():
    """The poisoning gate: repeating something three times through a channel cannot
    crown it. What a person says counts, but it needs its own corroboration — or an
    operator's signature, which is the other branch."""
    from app.agent_memory_utils import trust_level
    dicho = {"observations": 9, "access_count": 5, "used_ok": 4, "source": "chat",
             "review_state": "new"}
    assert trust_level(dicho) == "media"
    assert trust_level({**dicho, "review_state": "validated"}) == "alta"
    observado = {**dicho, "source": "agent"}
    assert trust_level(observado) == "alta", "lo que observa él sí puede subir solo"


def test_los_caminos_que_TRABAJAN_cierran_el_bucle():
    """Loop coverage, declared and checked (7.164.0).

    Learning is injected in five places and attributing only makes sense in those where the
    employee WORKS and there is an outcome. This test pins which ones, so that partial
    coverage is a visible decision and not an oversight:

      · autonomous and channels → YES. Real work, real outcome, same `_finish_run`.
      · maintenance             → NO. It is the consolidation itself: attributing to itself
                                  would be a self-feeding loop.
      · evaluate condition      → NO. A condition evaluating correctly says nothing about
                                  whether the learning helped; it would add noise to the ratio.
      · chat                    → CANNOT. There is no run to attribute to.
    """
    import pathlib
    app = pathlib.Path(__file__).resolve().parent.parent / "app"
    con_bucle = ("autonomous/executor.py", "channels/executor.py")
    for rel in con_bucle:
        assert "record_injection" in (app / rel).read_text(encoding="utf-8"), (
            f"{rel} inyecta aprendizaje y NO apunta la inyección: su desenlace se "
            f"pierde y `used_ok` queda sesgado hacia los caminos que sí lo hacen")
