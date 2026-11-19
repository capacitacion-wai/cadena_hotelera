"""A message while a turn is running does not throw it away, and a request's work goes in its folder (15.11.0).

Measured in `cobsa` on 09/11/2026: Cora had spent 20 minutes on a 12.590-item lookup and the person asked
«¿dónde lo has dejado?». The new turn SUPERSEDED the old one: the app cancelled its task but the agent kept
working with nobody showing it, and the new message queued behind it in the same session. On screen, nothing.
Now the chat refuses the message (409 TURN_IN_PROGRESS), the screen re-attaches to the live turn and gives the
text back to the box.

The same afternoon the file panel had dozens of loose scripts and CSVs and the deliverable was lost among them:
working files go in one folder per request and the reply names the deliverable with its folder.
"""
from __future__ import annotations

import pathlib

import pytest

from app.chat_prompt import build_chat_mode_prime, build_chat_turn_prompt

_APP = pathlib.Path(__file__).resolve().parent.parent / "app"
_ROUTES = (_APP / "routes" / "agent_routes.py").read_text(encoding="utf-8")
_SCREEN = _APP.parents[1] / "frontend" / "src" / "pages" / "Agent.tsx"


def _chat() -> str:
    i = _ROUTES.index("async def chat_with_agent(")
    return _ROUTES[i:_ROUTES.index("\ndef _solo_lectura_de(", i)]


def test_chat_refuses_a_message_while_the_turn_runs():
    chat = _chat()
    assert "_vivo.is_running" in chat and "status_code=409" in chat and '"code": TURN_IN_PROGRESS' in chat
    # Before opening a session or launching anything: a refused message can neither evict nor start a turn.
    assert chat.index("TURN_IN_PROGRESS") < chat.index("sesiones_a_desalojar") < chat.index("await start_turn(")


def test_a_reply_to_a_question_passes():
    chat = _chat()
    assert "if not payload.request_id:" in chat[:chat.index("TURN_IN_PROGRESS")]


@pytest.mark.skipif(not _SCREEN.exists(), reason="the frontend is not next to this backend")
def test_the_screen_reattaches_and_gives_the_text_back():
    p = _SCREEN.read_text(encoding="utf-8")
    i = p.index("motivo?.code === 'TURN_IN_PROGRESS'")
    branch = p[i:i + 1200]
    assert "setInput(userMsg)" in branch and "reengancharRef.current?.(turnId)" in branch
    assert "it.id !== userId" in branch


def test_the_work_folder_is_in_the_contract_and_every_turn():
    prime = build_chat_mode_prime("ana@cobsa.es", "sessions/users/ana")
    assert "/workspace/sessions/users/ana/work/" in prime and "exports/" in prime
    turn = build_chat_turn_prompt("hola", memory_user="ana@cobsa.es",
                                   workspace_subdir="sessions/users/ana", run_id=1)
    assert "work/<request>/" in turn and "exports/<file>" in turn


def test_without_subfolder_the_rule_stays():
    assert "/workspace/work/" in build_chat_mode_prime("ana@cobsa.es")
