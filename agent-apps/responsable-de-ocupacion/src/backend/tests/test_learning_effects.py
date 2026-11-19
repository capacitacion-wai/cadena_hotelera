"""v7.140.0 — BIDIRECTIONAL traceability of what the employee learned.

The point of the registry is that no manager ever fills up with rules nobody
knows the origin of. So:

* a proposal changes NOTHING until an admin accepts it (and accepting is what
  puts it in force, in one call, so it can't be left half-applied);
* from the rule you can reach the learning (``origin_for``) and from the learning
  the rules it produced (``list_effects``) — the same link stored ONCE;
* reverting undoes the rule in its own home, restores a parameter's PREVIOUS
  value (not "empty"), keeps the learning alive and blocks re-proposal, so
  undoing is a decision and not a weekly chore;
* the queue is an aggregated VIEW: there is no second truth about "is this
  approved?".
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _learning(content="El barrido nocturno tarda 40 min: es normal.",
                    subject="job:indexing"):
    from app import agent_memory_utils as amu
    out = await amu.remember(content=content, subject=subject, kind="baseline")
    return int(out["id"])


async def _suppression(review_state="proposed"):
    from app.app_database import app_session
    from app.app_models.signal_suppressions import SignalSuppression
    async with app_session() as s:
        row = SignalSuppression(
            signal_table="alerts", match_key="indexing-sweep",
            match_label="Barrido de indexing lento", enabled=True,
            review_state=review_state, suppress_count=0,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return int(row.id)


def test_proposal_is_inert_until_accepted() -> None:
    from app import learning_effects_service as les
    from app.app_database import app_session
    from app.app_models.signal_suppressions import SignalSuppression
    from sqlalchemy import select

    async def go():
        lid = await _learning()
        sid = await _suppression()
        effect = await les.materialize(
            learning_id=lid, target="suppression", target_id=sid,
            label="No avisar del barrido nocturno", origin_run_id=7)
        async with app_session() as s:
            before = (await s.execute(
                select(SignalSuppression.review_state)
                .where(SignalSuppression.id == sid))).scalar()
        accepted = await les.accept(int(effect["id"]), approved_by="ana@x.com",
                                    approved_where="chat")
        async with app_session() as s:
            after = (await s.execute(
                select(SignalSuppression.review_state)
                .where(SignalSuppression.id == sid))).scalar()
        return effect, before, accepted, after

    effect, before, accepted, after = _run(go())
    assert effect["state"] == "proposed"
    assert before == "proposed", "proponer no puede activar nada"
    assert accepted["state"] == "active"
    assert accepted["approved_where"] == "chat"
    assert after == "active", "aceptar es lo que la pone en vigor, en un solo paso"


def test_accepting_validates_the_learning_behind_it() -> None:
    """Agreeing with a proposal is agreeing with the observation behind it — and
    the learning must stop being re-proposed."""
    from app import learning_effects_service as les
    from app.app_database import app_session
    from app.app_models.agent_memory import AgentMemory
    from sqlalchemy import select

    async def go():
        lid = await _learning()
        sid = await _suppression()
        effect = await les.materialize(
            learning_id=lid, target="suppression", target_id=sid, label="x")
        await les.accept(int(effect["id"]), approved_by="ana@x.com")
        async with app_session() as s:
            return (await s.execute(
                select(AgentMemory.review_state, AgentMemory.behavior_effect)
                .where(AgentMemory.id == lid))).first()

    state, behavior = _run(go())
    assert state == "validated"
    assert behavior and behavior.startswith("suppression:")


def test_traceability_is_bidirectional_with_one_link() -> None:
    from app import learning_effects_service as les

    async def go():
        lid = await _learning()
        sid = await _suppression()
        effect = await les.materialize(
            learning_id=lid, target="suppression", target_id=sid,
            label="No avisar del barrido", origin_run_id=11)
        await les.accept(int(effect["id"]), approved_by="ana@x.com")
        forward = await les.list_effects(learning_id=lid)      # learning → rules
        backward = await les.origin_for("suppression", [sid])  # rule → learning
        return forward, backward, sid, lid

    forward, backward, sid, lid = _run(go())
    assert forward["total"] == 1
    assert forward["items"][0]["target_id"] == str(sid)
    origin = backward[str(sid)]
    assert origin["learning_id"] == lid
    assert origin["origin_run_id"] == 11
    assert origin["learning"], "el texto del aprendizaje se congela en el efecto"


def test_orphan_rule_has_no_origin() -> None:
    from app import learning_effects_service as les
    assert _run(les.origin_for("suppression", [424242])) == {}


def test_revert_undoes_in_destination_and_blocks_reproposal() -> None:
    from app import learning_effects_service as les
    from app.app_database import app_session
    from app.app_models.agent_memory import AgentMemory
    from app.app_models.signal_suppressions import SignalSuppression
    from sqlalchemy import func, select

    async def go():
        lid = await _learning()
        sid = await _suppression()
        effect = await les.materialize(
            learning_id=lid, target="suppression", target_id=sid, label="x")
        await les.accept(int(effect["id"]), approved_by="ana@x.com")
        out = await les.revert(int(effect["id"]), reverted_by="ana@x.com",
                              reason="volvió a tardar de más y no avisó")
        async with app_session() as s:
            rules = (await s.execute(
                select(func.count(SignalSuppression.id))
                .where(SignalSuppression.id == sid))).scalar()
            learning = (await s.execute(
                select(AgentMemory.repropose_blocked, AgentMemory.behavior_effect,
                       AgentMemory.content).where(AgentMemory.id == lid))).first()
        return out, rules, learning

    out, rules, learning = _run(go())
    assert out["state"] == "reverted"
    assert out["revert_reason"]
    assert rules == 0, "revertir un silencio lo quita de su gestor"
    blocked, behavior, content = learning
    assert bool(blocked) is True, "no puede volver a pedir lo mismo mañana"
    assert behavior is None
    assert content, "el aprendizaje sigue vivo: sabe lo que observó, ya no actúa"


def test_revert_restores_the_previous_parameter_value() -> None:
    """A threshold's undo is "put back what was there", not "blank it" — the
    difference between an undo and a second outage."""
    from app import learning_effects_service as les
    from app.app_database import app_session
    from app.app_models.runtime import KnowledgeRequest
    from sqlalchemy import select

    async def go():
        lid = await _learning(content="El umbral de 30 min genera falsos avisos.",
                              subject="param:umbral_barrido")
        async with app_session() as s:
            row = KnowledgeRequest(
                key="umbral_barrido", label="Umbral del barrido",
                question="¿Cuántos minutos son normales?",
                value="30", status="provided", proposed_value="45",
                proposed_by="agent", proposed_at=datetime.utcnow(),
            )
            s.add(row)
            await s.commit()
        effect = await les.materialize(
            learning_id=lid, target="parameter", target_id="umbral_barrido",
            label="Subir el umbral del barrido a 45 min")
        await les.accept(int(effect["id"]), approved_by="ana@x.com")
        async with app_session() as s:
            applied = (await s.execute(
                select(KnowledgeRequest.value)
                .where(KnowledgeRequest.key == "umbral_barrido"))).scalar()
        await les.revert(int(effect["id"]), reverted_by="ana@x.com")
        async with app_session() as s:
            restored = (await s.execute(
                select(KnowledgeRequest.value)
                .where(KnowledgeRequest.key == "umbral_barrido"))).scalar()
        return applied, restored

    applied, restored = _run(go())
    assert applied == "45", "aceptar promociona el valor propuesto"
    assert restored == "30", "revertir devuelve el valor ANTERIOR, no lo vacía"


def test_materialize_is_idempotent() -> None:
    """Proposing the same thing twice must not pile up effects: the queue is read
    by a human and duplicates are what make people stop reading it."""
    from app import learning_effects_service as les

    async def go():
        lid = await _learning()
        sid = await _suppression()
        first = await les.materialize(learning_id=lid, target="suppression",
                                      target_id=sid, label="x")
        second = await les.materialize(learning_id=lid, target="suppression",
                                       target_id=sid, label="x mejorada")
        return first, second, await les.list_effects(learning_id=lid)

    first, second, listing = _run(go())
    assert first["id"] == second["id"]
    assert listing["total"] == 1
    assert listing["items"][0]["label"] == "x mejorada"


def test_queue_surfaces_proposals_without_a_learning() -> None:
    """The employee can propose a silence with no learning behind it; if the queue
    only listed effects, that proposal would be invisible AND unapprovable."""
    from app import learning_effects_service as les

    async def go():
        sid = await _suppression()
        return sid, await les.pending_proposals()

    sid, items = _run(go())
    assert any(str(i.get("target_id")) == str(sid) for i in items)


def test_revert_is_idempotent() -> None:
    from app import learning_effects_service as les

    async def go():
        lid = await _learning()
        sid = await _suppression()
        effect = await les.materialize(learning_id=lid, target="suppression",
                                       target_id=sid, label="x")
        await les.accept(int(effect["id"]), approved_by="ana@x.com")
        first = await les.revert(int(effect["id"]), reverted_by="ana@x.com")
        second = await les.revert(int(effect["id"]), reverted_by="ana@x.com")
        return first, second

    first, second = _run(go())
    assert first["state"] == second["state"] == "reverted"


@pytest.mark.parametrize("target", ["suppression", "parameter", "instruction",
                                    "automation"])
def test_every_target_has_a_home_in_the_ui(target) -> None:
    """Each of the four targets must resolve to an existing manager, or the
    "Learned from" chip would link nowhere."""
    from app.learning_effects_service import HOME_HASH
    assert HOME_HASH.get(target)
