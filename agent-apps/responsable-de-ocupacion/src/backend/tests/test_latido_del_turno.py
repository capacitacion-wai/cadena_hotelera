"""The heartbeat of a long turn (15.12.0).

COBSA, 10/11/2026: a step reading a 12.591-row ERP result sent nothing for minutes; the person saw a
spinner frozen on «step 25» and thought the employee had hung. After 50 s of silence the turn emits a
`latido` with the running tool and how long it has been running; it is not activity, so it never
resets the silence it reports, and it does not count as output for the timeout retry.
"""
from __future__ import annotations

import asyncio
import pathlib

from app.chat_runtime import ChatTurn

_ROUTES = (pathlib.Path(__file__).resolve().parent.parent / "app" / "routes" / "agent_routes.py").read_text()


def _turn() -> ChatTurn:
    return ChatTurn(scope_key="s", session_id="x")


def test_no_beat_before_fifty_seconds_of_silence():
    t = _turn()
    assert t.latido(t.ultimo_evento_at + 40) is None
    assert t.latido(t.ultimo_evento_at + 55)["herramienta"] == ""


def test_the_beat_names_the_oldest_running_tool_and_its_time():
    t = _turn()
    asyncio.run(t.append({"type": "tool_call", "name": "codigo10-cobsa_consultar_en_lote", "call_id": "a"}))
    inicio = t.ultimo_evento_at
    ev = t.latido(inicio + 180)
    assert ev == {"type": "latido", "herramienta": "codigo10-cobsa_consultar_en_lote", "segundos": 180}
    asyncio.run(t.append({"type": "tool_result", "name": "codigo10-cobsa_consultar_en_lote", "call_id": "a"}))
    assert t.en_marcha == {}


def test_a_beat_is_not_activity():
    t = _turn()
    antes = t.ultimo_evento_at
    asyncio.run(t.append({"type": "latido", "herramienta": "", "segundos": 60}))
    assert t.ultimo_evento_at == antes


def test_no_beat_once_the_turn_ended():
    t = _turn()
    asyncio.run(t.finish("complete"))
    assert t.latido(t.ultimo_evento_at + 600) is None


def test_a_beat_does_not_block_the_timeout_retry():
    assert 'not any(e.get("type") != "latido" for e in turn.events)' in _ROUTES
