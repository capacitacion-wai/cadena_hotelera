"""v7.143.0 — the contract of the TWO memories and the re-priming of live sessions.

These tests come from a real and hard-to-see failure: the Vigía, with its own memory
deployed and `remember_learning` in its catalogue, told its owner that «it has no
learning memory of its own». Two causes, and each has its guard here:

1. **Impossible instruction.** The chat was injected with the hint that asks to store
   the learning inside a ``STRUCTURED_RESPONSE``… and the chat contract forbids
   emitting it. The only procedure it received was the one it could not follow.
   It is tested that in chat the procedure is TOOLS and that the boundary between
   the two memories is the SAME on every path (if each path tells it differently,
   the model picks the version that suits it).
2. **Yesterday's session.** Instructions injected only once live in the session
   history, and sessions survive a redeploy: a new capability only reached sessions
   created AFTERWARDS. It is tested that a session stamped with an old contract (or
   unstamped) asks for re-priming and that after priming it stops asking — without
   minting a new session, that is, without losing the conversation.

And a third one, about observability: with memory at zero, «it learned nothing» and
«it was not asked to learn» look the same. It is tested that the turn's verdict is
recorded, including the case where the model OMITS the block.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── 1. The procedure given on each path ───────────────────────────────────
def test_en_chat_el_procedimiento_son_herramientas_no_un_bloque_prohibido() -> None:
    from app.memory_contract import MODE_CHAT, two_memories_contract

    texto = two_memories_contract(MODE_CHAT, memory_user="ana@wai.com")
    assert "remember_learning" in texto, (
        "en chat la única forma de guardar aprendizaje es la herramienta: si no "
        "se nombra, el empleado responde que no puede")
    assert "search_learnings" in texto, "debe saber que puede CONSULTAR lo aprendido"
    assert "you do NOT emit" in texto, "debe recordar que aquí no hay respuesta estructurada"
    assert '"self_memory"' not in texto, (
        "pedirle un bloque `self_memory` en chat es pedirle lo que el contrato de "
        "chat le prohíbe emitir — el bug que se está arreglando")
    assert "remember_fact" in texto, "la memoria de la persona sigue teniendo su vía"
    assert "NEVER answer that you have no memory of your own" in texto, (
        "la respuesta literal que dio el empleado tiene que estar prohibida")


def test_en_autonomo_el_procedimiento_es_el_bloque_estructurado() -> None:
    from app.memory_contract import (
        MODE_AUTONOMOUS, MODE_CHANNEL, two_memories_contract,
    )

    for modo in (MODE_AUTONOMOUS, MODE_CHANNEL):
        texto = two_memories_contract(modo)
        assert "STRUCTURED_RESPONSE" in texto
        assert "nothing_new" in texto, (
            "hay que exigir una DECISIÓN explícita: omitir el bloque no se "
            "distingue de olvidarlo")


def test_la_frontera_es_identica_en_todos_los_caminos() -> None:
    from app import memory_contract as mc

    modos = (mc.MODE_CHAT, mc.MODE_AUTONOMOUS, mc.MODE_CHANNEL,
             mc.MODE_READONLY, mc.MODE_MAINTENANCE)
    for modo in modos:
        texto = mc.two_memories_contract(modo)
        assert mc.HEADER in texto
        assert mc._BOUNDARY in texto, (
            f"«{modo}» cuenta la frontera a su manera; una regla dicha de dos "
            "formas es una regla que el modelo puede elegir")
        # Learning does not authorise: the line that stops a learning from
        # becoming a permission through the back door.
        assert "propose_silence" in texto


def test_el_run_de_solo_lectura_dice_que_no_guarda_nada() -> None:
    from app.memory_contract import MODE_READONLY, two_memories_contract

    texto = two_memories_contract(MODE_READONLY)
    assert "YOU SAVE NOTHING" in texto
    assert '"self_memory"' not in texto


def test_sin_memoria_propia_no_se_explican_dos_memorias(monkeypatch) -> None:
    """With the capability off, talking about two memories is worse than staying silent."""
    import app.config as cfg
    from app.memory_contract import MODE_CHAT, two_memories_contract

    monkeypatch.setattr(cfg, "AGENT_SELF_MEMORY_ENABLED", False, raising=False)
    assert two_memories_contract(MODE_CHAT) == ""


# ── 2. The stamp and the re-priming of a live session ─────────────────────
def test_una_sesion_sin_sellar_o_con_sello_viejo_esta_desfasada() -> None:
    from app.agent_sessions import is_contract_stale
    from app.memory_contract import prompt_contract_stamp

    assert is_contract_stale(None) is True, (
        "las sesiones anteriores al sello son EXACTAMENTE las que fallaron")
    assert is_contract_stale("7.0.0:deadbeef") is True
    assert is_contract_stale(prompt_contract_stamp()) is False


def test_el_sello_cambia_cuando_cambia_el_texto_inyectado(monkeypatch) -> None:
    from app import memory_contract as mc

    antes = mc.prompt_contract_stamp()
    monkeypatch.setattr(mc, "_BOUNDARY", mc._BOUNDARY + "\nregla nueva.")
    assert mc.prompt_contract_stamp() != antes, (
        "si el sello no cambia con el texto, un EVOLVE no llega a las sesiones "
        "abiertas — que es el fallo original")


def test_una_sesion_viva_con_contrato_viejo_pide_reprimado_sin_perder_el_hilo() -> None:
    from app.agent_sessions import (
        get_session_manager, interactive_scope, stamp_prompt_contract,
    )
    from app.app_database import app_write
    from app.app_models.runtime import AgentSession

    scope = interactive_scope("ana@wai.com")

    async def sembrar_sesion_de_ayer():
        # A FRESH session (it is reused) but primed before the contract existed:
        # the case of the operator still in yesterday's conversation.
        async with app_write() as s:
            s.add(AgentSession(
                scope_key=scope.scope_key,
                session_id="ses_de_ayer",
                role=scope.role,
                user_email=scope.user_email,
                workspace_subdir=scope.workspace_subdir,
                memory_user=scope.memory_user,
                origin="chat",
                message_count=3,
                status="active",
                created_at=datetime.now(timezone.utc),
                last_used_at=datetime.now(timezone.utc),
                prompt_contract=None,
            ))
            await s.commit()

    _run(sembrar_sesion_de_ayer())
    mgr = get_session_manager()

    handle = _run(mgr.resolve(scope))
    assert handle.session_id == "ses_de_ayer", (
        "re-primar NO es minar una sesión nueva: el operador conserva su "
        "conversación")
    assert handle.is_new is False
    assert handle.needs_reprime is True
    assert handle.needs_prime is True

    _run(stamp_prompt_contract(scope.scope_key))
    handle2 = _run(mgr.resolve(scope))
    assert handle2.session_id == "ses_de_ayer"
    assert handle2.needs_prime is False, (
        "una vez primada, no puede re-primarse en cada mensaje (sería un turno "
        "silencioso de coste por mensaje)")


# ── 3. The turn's learning verdict ───────────────────────────────────────
def test_declarar_nada_nuevo_queda_registrado() -> None:
    from app import agent_memory_utils as amu

    async def go():
        out = await amu.materialize_self_memory_block(
            7, {"self_memory": {"nothing_new": "todo dentro de lo normal"}})
        return out, await amu.last_verdict()

    out, verdict = _run(go())
    assert out["learnings"] == 0
    assert verdict is not None
    assert verdict["nothing_new"] == "todo dentro de lo normal"
    assert verdict["run_id"] == 7
    assert verdict["absent"] is False


def test_omitir_el_bloque_se_registra_como_ausencia() -> None:
    """It is the difference between «there was nothing» and «the instruction was
    ignored»: without this, the panel shows empty memory in both cases."""
    from app import agent_memory_utils as amu

    async def go():
        out = await amu.materialize_self_memory_block(9, {"summary": "todo ok"})
        return out, await amu.last_verdict()

    out, verdict = _run(go())
    assert out.get("absent") is True
    assert verdict is not None and verdict["absent"] is True
    assert verdict["run_id"] == 9


async def _sembrar_run(run_id: int) -> None:
    """A learning hangs from the run that produced it (`created_by_run_id` has an
    FK): without a run there is no provenance, and without provenance you cannot
    audit where what the employee believes came from."""
    from app.app_database import app_write
    from app.app_models.runtime import AgentRun

    async with app_write() as s:
        s.add(AgentRun(
            id=run_id, idempotency_key=f"test-{run_id}", status="COMPLETED",
            mode="autonomous", attempts=1, prompt="prueba",
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        await s.commit()


def test_un_aprendizaje_real_se_registra_en_el_veredicto() -> None:
    from app import agent_memory_utils as amu

    async def go():
        await _sembrar_run(11)
        out = await amu.materialize_self_memory_block(11, {"self_memory": {
            "learnings": [{"subject": "tenant:rdwai",
                           "content": "Metabase registra la conexión 30.",
                           "kind": "entity"}],
        }})
        return out, await amu.last_verdict()

    out, verdict = _run(go())
    assert out["learnings"] == 1
    assert verdict["learnings"] == 1 and verdict["absent"] is False


# ── 4. What survives the prune: the TURN ──────────────────────────────────
# A contract primed once is not enough. With 7.143.0 deployed, the Vigía had
# `remember_learning` announced, `allow` permission and the session re-primed… and
# still answered «that tool is not enabled in this session» without attempting
# the call: the sidecar compacts with `prune`, and the priming message was no
# longer in its window. The only guaranteed thing is what travels in every turn.
def _turno(msg: str) -> str:
    from app.chat_prompt import build_chat_turn_prompt

    return build_chat_turn_prompt(
        msg, memory_user="ana@wai.com", run_id=42, run_token="tok-42",
        workspace_subdir="ana",
    )


def test_cada_turno_nombra_las_dos_memorias() -> None:
    texto = _turno("¿cómo está el clúster?")
    assert "remember_learning" in texto, (
        "si su memoria propia solo se nombra en el priming, unos turnos después "
        "el empleado responde que no la tiene")
    assert "search_learnings" in texto
    assert "remember_fact" in texto, "la memoria de la persona no se pierde"
    # v12.0.0 — the turn is named with `run_token` (random), not with the integer
    # `run_id`: it was guessable and could be used to act as another person.
    assert "run_token=tok-42" in texto


def test_una_orden_de_memoria_se_lleva_el_procedimiento_pegado() -> None:
    from app.memory_contract import HEADER

    texto = _turno("aprende y guarda en tu memoria de aprendizaje la situación "
                   "del clúster")
    assert HEADER in texto, (
        "la orden literal que falló tiene que viajar CON su procedimiento")
    assert "NEVER answer that you have no memory of your own" in texto


@pytest.mark.parametrize("orden", [
    "guárdalo en tu memoria",
    "guardalo en tu memoria",          # without the accent
    "apréndete el número de nodos",
    "memoriza que este barrido tarda 3 minutos",
    "¿qué has aprendido del entorno?",
    "olvida lo que aprendiste de indexing",
    "a tu parte de aprendizaje",       # how the operator actually asked for it
    # The person writes in THEIR language (docs/idioma-de-los-agentes.md).
    "save it in your memory",
    "what have you learned about the environment?",
    "remember that this sweep takes 3 minutes",
    "forget what you learned about indexing",
])
def test_las_formas_reales_de_pedirlo_se_reconocen(orden: str) -> None:
    from app.chat_prompt import looks_like_memory_order

    assert looks_like_memory_order(orden) is True


def test_un_turno_normal_no_paga_el_procedimiento_entero() -> None:
    """The mention goes in every turn because it is cheap; the procedure (≈1.5 kB)
    only when needed."""
    from app.memory_contract import HEADER

    assert HEADER not in _turno("dame el estado de los tenants")


def test_el_procedimiento_del_turno_no_se_le_escapa_al_operador() -> None:
    from app.chat_prompt import strip_internal_chat_leak

    texto = _turno("guarda en tu memoria que plexamx no indexa")
    visible = strip_internal_chat_leak("Guardado.\n\n" + texto.split("\n\n", 1)[1])
    assert "remember_learning" not in visible
    assert "INTERNAL INSTRUCTION" not in visible


# ── 5. Static guards: the shape of the code, not just its result ──────────
_APP = Path(__file__).resolve().parents[1] / "app"


def test_todo_camino_que_inyecta_memoria_inyecta_su_frontera() -> None:
    """A memory without a boundary is how «the operator's things» get mixed with
    «the environment's things»: the SHAPE is forbidden, not just the symptom."""
    exentos = {"agent_memory_prompt.py", "agent_memory_routes.py"}
    incumplen = []
    for path in _APP.rglob("*.py"):
        if path.name in exentos:
            continue
        src = path.read_text(encoding="utf-8")
        if "build_agent_memory_block(" not in src:
            continue
        if "two_memories_contract" not in src:
            incumplen.append(str(path.relative_to(_APP)))
    assert not incumplen, (
        "inyectan la memoria propia sin la frontera de las dos memorias: "
        f"{incumplen}")


def test_el_priming_del_chat_no_pide_un_structured_response() -> None:
    from app.routes import agent_routes

    src = inspect.getsource(agent_routes._prime_workspace)
    assert "agent_memory_capture_hint()" not in src, (
        "ese hint pide un `self_memory` dentro de un STRUCTURED_RESPONSE que el "
        "chat prohíbe emitir — fue la causa de que el empleado dijera que no "
        "podía guardar aprendizaje")
    assert "two_memories_contract" in src


def test_el_chat_prima_tambien_cuando_el_contrato_esta_desfasado() -> None:
    """The easiest regression: going back to `handle.is_new` and leaving out every
    open session."""
    src = (_APP / "routes" / "agent_routes.py").read_text(encoding="utf-8")
    assert "needs_prime = handle.needs_prime" in src
    assert "stamp_prompt_contract" in src, (
        "sin sellar tras primar, la sesión se re-primaría en cada mensaje")


def test_la_linea_de_cada_turno_no_puede_volver_a_olvidar_su_memoria() -> None:
    """The cheap regression: someone shortens the `[INTERNO]` line to save tokens and
    takes out the only mention that survives compaction."""
    src = (_APP / "chat_prompt.py").read_text(encoding="utf-8")
    assert "remember_learning/search_learnings" in src
    assert "looks_like_memory_order" in src


def test_el_priming_termina_pidiendo_solo_ok() -> None:
    """14.17.3 — asked nothing, the priming got a self-introduction in Spanish that stayed in
    the session, and the person's English question was answered in Spanish. It ends asking
    for a language-less «OK» and naming the person's first message as the one that sets it."""
    import asyncio
    from app.routes import agent_routes

    enviado: list[str] = []

    class _Proxy:
        async def stream_interactive(self, session_id, prompt, timeout=0):
            enviado.append(prompt)
            yield {"type": "done"}

    asyncio.run(agent_routes._prime_workspace(_Proxy(), "s1", "", "", memory_user="ana@acme.com"))
    assert enviado and enviado[0].endswith(agent_routes.PRIME_CLOSING)
    assert "exactly `OK`" in agent_routes.PRIME_CLOSING and "ITS language" in agent_routes.PRIME_CLOSING
