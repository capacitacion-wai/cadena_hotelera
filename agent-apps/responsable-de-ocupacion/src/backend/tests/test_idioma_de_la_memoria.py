"""The memory is written —and searched— in the employee's working language (14.17.0).

  · every surface's preamble carries the MEMORY LANGUAGE note (store AND search in it);
  · the texts the platform itself stores (conversation episode, a rejection's lesson) follow
    the employee's language; what the person said is quoted as is.
"""
from __future__ import annotations

import asyncio

from app.agent_locale import memory_language_note, rotulo


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _idioma(valor: str):
    from app.app_database import app_write
    from app.app_models.runtime import AppSetting

    async def go():
        async with app_write() as s:
            await s.merge(AppSetting(key="agent_language", value=valor))
            await s.commit()

    _run(go())


def test_la_nota_dice_guardar_y_buscar_en_el_idioma():
    n = memory_language_note("en")
    assert "ENGLISH" in n and "SEARCH" in n and "translate" in n
    assert "SPANISH" in memory_language_note("es")


def test_los_rotulos_de_la_plataforma():
    assert rotulo("en", "peticion", x="send the quote") == "Request: send the quote"
    assert rotulo("es", "peticion", x="manda el presupuesto") == "Petición: manda el presupuesto"
    assert "Operator's reason: too expensive" in rotulo("en", "rechazo", titulo="T", tipo="tool_call",
                                                        motivo="too expensive")


def test_el_preambulo_de_todas_las_superficies_lleva_la_nota():
    from app.persona import employee_identity_preamble
    assert "MEMORY LANGUAGE" in _run(employee_identity_preamble("en"))


def test_el_episodio_automatico_sigue_al_empleado():
    from sqlalchemy import select
    from app.app_database import app_session
    from app.app_models.memory import UserEpisode
    from app.memory_utils import auto_record_chat_episode
    from app.app_database import app_write
    from app.app_models.runtime import AgentRun
    _idioma("en")

    async def run():
        async with app_write() as s:
            r = AgentRun(idempotency_key="chat-idioma", prompt="x", mode="chat")
            s.add(r)
            await s.commit()
            return r.id

    rid = _run(run())
    _run(auto_record_chat_episode(rid, "ana@acme.com", "prepara el informe semanal de ventas",
                                  response_text="Hecho."))

    async def resumenes():
        async with app_session() as s:
            return [e.summary for e in (await s.execute(select(UserEpisode))).scalars()]

    assert any(r.startswith("Request: prepara el informe") for r in _run(resumenes()))
