"""Memory is context about the person, never business data (15.3.3, agents end2end 30/10/2026).

With a past question about trip «d03267» among the recent interactions, a new question about «qxtpcber» was
answered with that trip. The block now says, every time it carries something, that records are looked up now with
the tools and checked literally against what is asked.
"""
from __future__ import annotations

import asyncio


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_el_bloque_dice_que_la_memoria_no_es_dato():
    from app.memory_prompt import build_memory_block
    from app.memory_utils import auto_record_chat_episode
    from app.app_database import app_write
    from app.app_models.runtime import AgentRun

    async def run():
        async with app_write() as s:
            r = AgentRun(idempotency_key="chat-no-es-dato", prompt="x", mode="chat")
            s.add(r)
            await s.commit()
            return r.id

    rid = _run(run())
    _run(auto_record_chat_episode(rid, "ana@acme.com", "¿Tengo algún viaje cuyo título contenga «d03267»?",
                                  response_text="Sí, en Oporto."))
    texto = _run(build_memory_block("ana@acme.com"))
    assert "d03267" in texto                                   # the episode is there…
    assert "NOT data" in texto                                 # …marked as what it is
    assert "never a source of business data" in texto
    assert "literally" in texto
