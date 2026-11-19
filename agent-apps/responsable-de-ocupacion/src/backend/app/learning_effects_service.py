"""The bridge between "what the employee learned" and "what the employee does"
(v7.140.0) — materialize, approve, revert, and the reverse link.

Four functions and none duplicated:

* ``materialize`` — write the row in the DESTINATION home (proposed) + the
  effect that records why.
* ``approve``     — put the rule in force in its home and stamp the effect with
  who approved it and where (in the chat, or later from the queue).
* ``revert``      — undo it in the destination, mark the effect reverted, leave
  the learning ALIVE but flagged so it is not proposed again. Without that last
  part, reverting starts a weekly loop.
* ``origin_for``  — the BATCH reverse lookup ("this silence, where did it come
  from?") that all four listings call. One implementation, not four.

The invariant that keeps this from creating a second truth: this module NEVER
decides whether a rule is in force. The destination table does. Here we keep the
link, the audit trail and the undo handle.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime

from sqlalchemy import delete as sql_delete, func, select, update as sql_update

from app.app_database import app_session, app_write
from app.app_models.learning_effects import (
    LearningEffect, STATE_ACTIVE, STATE_PROPOSED, STATE_REVERTED,
    TARGET_AUTOMATION, TARGET_INSTRUCTION, TARGET_PARAMETER, TARGET_SUPPRESSION,
    TARGETS,
)

logger = logging.getLogger(__name__)

# Where each target lives in the UI, so the effect row can link to the real row.
HOME_HASH = {
    TARGET_SUPPRESSION: "silencios",
    TARGET_PARAMETER: "conocimiento",
    TARGET_INSTRUCTION: "conocimiento",
    TARGET_AUTOMATION: "trabajo-autonomo",
}


def _key(learning_id: int, target: str, target_id: str) -> str:
    return "lfx-" + hashlib.sha1(
        f"{learning_id}|{target}|{target_id}".encode("utf-8")
    ).hexdigest()[:24]


async def materialize(
    *,
    learning_id: int,
    target: str,
    target_id: str | int,
    label: str,
    origin_run_id: int | None = None,
    state: str = STATE_PROPOSED,
    approved_by: str | None = None,
    approved_where: str | None = None,
    undo: dict | None = None,
) -> dict | None:
    """Record that ``learning_id`` produced the rule ``(target, target_id)``.

    Idempotent on the triple: proposing the same thing twice updates the row
    instead of piling up effects. The caller has already written (or is about to
    write) the destination row — this only records the link.
    """
    if target not in TARGETS:
        return None
    tid = str(target_id)
    try:
        from app.app_models.agent_memory import AgentMemory
        now = datetime.utcnow()
        async with app_write() as s:
            learning = (await s.execute(
                select(AgentMemory).where(AgentMemory.id == int(learning_id))
            )).scalar_one_or_none()
            if learning is None:
                return None
            key = _key(int(learning_id), target, tid)
            row = (await s.execute(
                select(LearningEffect).where(LearningEffect.idempotency_key == key)
            )).scalar_one_or_none()
            if row is None:
                row = LearningEffect(
                    idempotency_key=key,
                    learning_id=int(learning_id),
                    # Frozen text: the learning may be corrected later, the audit
                    # trail must keep saying what was actually agreed.
                    learning_text=(learning.content or "")[:4000],
                    target=target, target_id=tid, label=(label or "")[:400],
                    proposed_at=now, origin_run_id=origin_run_id, state=state,
                    approved_by=approved_by, approved_where=approved_where,
                    approved_at=(now if state == STATE_ACTIVE else None),
                    undo_json=(json.dumps(undo, ensure_ascii=False) if undo else None),
                )
                s.add(row)
            else:
                row.label = (label or row.label)[:400]
                row.state = state
                if state == STATE_ACTIVE:
                    row.approved_by = approved_by or row.approved_by
                    row.approved_where = approved_where or row.approved_where
                    row.approved_at = row.approved_at or now
                if undo:
                    row.undo_json = json.dumps(undo, ensure_ascii=False)
            if state == STATE_ACTIVE:
                await s.execute(
                    sql_update(AgentMemory)
                    .where(AgentMemory.id == int(learning_id))
                    .values(behavior_effect=f"{target}:{tid}", updated_at=now)
                )
            await s.commit()
            await s.refresh(row)
            return serialize(row)
    except Exception:  # pragma: no cover — never block the caller
        logger.warning("learning effect materialize failed", exc_info=True)
        return None


async def capture_undo(target: str, target_id: str | int) -> dict:
    """Snapshot whatever "undo" will need, BEFORE the rule goes in force.

    Only the business parameter needs it (its previous value is about to be
    overwritten); for the other three, undoing is deleting or disabling and
    needs no snapshot. Captured here so the effect row is self-sufficient later,
    when the old value is long gone.
    """
    if target != TARGET_PARAMETER:
        return {}
    try:
        from app.app_models.runtime import KnowledgeRequest
        async with app_session() as s:
            prev = (await s.execute(
                select(KnowledgeRequest.value)
                .where(KnowledgeRequest.key == str(target_id))
            )).scalar()
        return {"previous_value": prev}
    except Exception:  # pragma: no cover
        return {}


async def accept(
    effect_id: int, *, approved_by: str, approved_where: str = "queue",
) -> dict | None:
    """The whole approval in one call: snapshot the undo, put the rule in force
    in its home, and stamp the audit trail. The route does not orchestrate this,
    so the three steps can never be done half-way from a different caller."""
    async with app_session() as s:
        row = (await s.execute(
            select(LearningEffect).where(LearningEffect.id == int(effect_id))
        )).scalar_one_or_none()
        if row is None:
            return None
        target, target_id = row.target, row.target_id
    undo = await capture_undo(target, target_id)
    if undo:
        async with app_write() as s:
            await s.execute(
                sql_update(LearningEffect)
                .where(LearningEffect.id == int(effect_id))
                .values(undo_json=json.dumps(undo, ensure_ascii=False))
            )
            await s.commit()
    note = await put_in_force(target, target_id, approved_by=approved_by)
    payload = await approve(int(effect_id), approved_by=approved_by,
                            approved_where=approved_where)
    if payload is not None:
        payload["apply_note"] = note
    return payload


async def put_in_force(target: str, target_id: str | int, *,
                       approved_by: str) -> str:
    """Put the rule IN FORCE in its own home. Returns a short human note.

    Symmetric with ``_undo_in_destination``, and deliberately in this module so
    "approve" and "revert" can never drift apart. What "in force" means is
    different in each home, and each home keeps owning that truth:
    a silence flips ``review_state``, a parameter promotes its proposed value, an
    instruction and an automation are simply enabled.
    """
    tid = str(target_id)
    now = datetime.utcnow()
    async with app_write() as s:
        if target == TARGET_SUPPRESSION:
            from app.app_models.signal_suppressions import SignalSuppression
            await s.execute(
                sql_update(SignalSuppression)
                .where(SignalSuppression.id == int(tid))
                .values(review_state="active", enabled=True, updated_at=now)
            )
            note = "silencio activado"
        elif target == TARGET_INSTRUCTION:
            from app.app_models.runtime import ScopedKnowledge
            await s.execute(
                sql_update(ScopedKnowledge)
                .where(ScopedKnowledge.id == int(tid))
                .values(enabled=1, updated_by=approved_by, updated_at=now)
            )
            note = "instrucción activada"
        elif target == TARGET_AUTOMATION:
            from app.app_models.automations import Automation
            await s.execute(
                sql_update(Automation).where(Automation.id == int(tid))
                .values(enabled=1, updated_by=approved_by, updated_at=now)
            )
            note = "automatización activada"
        elif target == TARGET_PARAMETER:
            from app.app_models.runtime import KnowledgeRequest
            row = (await s.execute(
                select(KnowledgeRequest).where(KnowledgeRequest.key == tid)
            )).scalar_one_or_none()
            if row is None:
                return "parámetro no encontrado"
            row.value = row.proposed_value if row.proposed_value is not None else row.value
            row.status = "provided" if row.value not in (None, "") else "pending"
            row.provided_by = approved_by
            row.provided_at = now
            row.proposed_value = None
            row.proposed_by = None
            row.proposed_at = None
            note = "umbral confirmado"
        else:
            return "destino desconocido"
        await s.commit()
    return note


async def approve(
    effect_id: int, *, approved_by: str, approved_where: str = "queue",
) -> dict | None:
    """Mark an effect as in force. The destination row is put in force by its own
    route (it owns that truth); this stamps the audit trail and marks the
    learning as already applied so the agent stops re-proposing it."""
    try:
        from app.app_models.agent_memory import AgentMemory
        now = datetime.utcnow()
        async with app_write() as s:
            row = (await s.execute(
                select(LearningEffect).where(LearningEffect.id == int(effect_id))
            )).scalar_one_or_none()
            if row is None:
                return None
            row.state = STATE_ACTIVE
            row.approved_by = approved_by
            row.approved_where = approved_where
            row.approved_at = now
            await s.execute(
                sql_update(AgentMemory)
                .where(AgentMemory.id == row.learning_id)
                .values(
                    behavior_effect=f"{row.target}:{row.target_id}",
                    # Approving a proposal is also an implicit validation of the
                    # learning behind it: the operator just agreed with it.
                    review_state="validated",
                    reviewed_by=approved_by, reviewed_at=now, updated_at=now,
                )
            )
            await s.commit()
            await s.refresh(row)
            payload = serialize(row)
        from app.agent_memory_utils import bump_memory_version
        bump_memory_version()
        return payload
    except Exception:  # pragma: no cover
        logger.warning("learning effect approve failed", exc_info=True)
        return None


async def _undo_in_destination(session, row: LearningEffect) -> str:
    """Undo the rule in its own home. Returns a short human note of what was
    done, so the audit trail says it rather than implying it.

    Delete vs disable is decided per home by what preserves the operator's
    history: a silence and an instruction carry none (deleting re-enables
    behaviour immediately and cleanly), an automation carries its whole
    evaluation history (so it is disabled, never deleted).
    """
    tid = row.target_id
    if row.target == TARGET_SUPPRESSION:
        from app.app_models.signal_suppressions import SignalSuppression
        await session.execute(
            sql_delete(SignalSuppression).where(SignalSuppression.id == int(tid))
        )
        return "silencio eliminado"
    if row.target == TARGET_INSTRUCTION:
        from app.app_models.runtime import ScopedKnowledge
        await session.execute(
            sql_delete(ScopedKnowledge).where(ScopedKnowledge.id == int(tid))
        )
        return "instrucción eliminada"
    if row.target == TARGET_AUTOMATION:
        from app.app_models.automations import Automation
        await session.execute(
            sql_update(Automation).where(Automation.id == int(tid)).values(enabled=0)
        )
        return "automatización desactivada"
    if row.target == TARGET_PARAMETER:
        from app.app_models.runtime import KnowledgeRequest
        undo = {}
        if row.undo_json:
            try:
                undo = json.loads(row.undo_json) or {}
            except ValueError:
                undo = {}
        previous = undo.get("previous_value")
        await session.execute(
            sql_update(KnowledgeRequest)
            .where(KnowledgeRequest.key == tid)
            .values(
                value=previous,
                status=("provided" if previous not in (None, "") else "pending"),
                proposed_value=None, proposed_by=None, proposed_at=None,
            )
        )
        return (
            "umbral devuelto a su valor anterior" if previous not in (None, "")
            else "umbral vaciado (no tenía valor previo)"
        )
    return "sin acción"


async def revert(
    effect_id: int, *, reverted_by: str, reason: str | None = None,
) -> dict | None:
    """Undo an effect: remove it from its home, mark it ``reverted``, and leave
    the learning alive but BLOCKED for re-proposal.

    The learning is deliberately NOT deleted: the employee still knows what it
    observed, it simply no longer acts on it. And ``repropose_blocked`` is what
    stops it from asking for the same rule again tomorrow — reverting must be a
    decision, not a chore you repeat every week.
    """
    try:
        from app.app_models.agent_memory import AgentMemory
        now = datetime.utcnow()
        async with app_write() as s:
            row = (await s.execute(
                select(LearningEffect).where(LearningEffect.id == int(effect_id))
            )).scalar_one_or_none()
            if row is None:
                return None
            if row.state == STATE_REVERTED:
                return serialize(row)
            note = await _undo_in_destination(s, row)
            row.state = STATE_REVERTED
            row.reverted_by = reverted_by
            row.reverted_at = now
            row.revert_reason = ((reason or "").strip() or None)
            await s.execute(
                sql_update(AgentMemory)
                .where(AgentMemory.id == row.learning_id)
                .values(
                    behavior_effect=None,
                    repropose_blocked=True,
                    updated_at=now,
                )
            )
            await s.commit()
            await s.refresh(row)
            payload = serialize(row)
            payload["undo_note"] = note
        from app.agent_memory_utils import bump_memory_version
        bump_memory_version()
        return payload
    except Exception:  # pragma: no cover
        logger.warning("learning effect revert failed", exc_info=True)
        return None


async def origin_for(target: str, ids) -> dict[str, dict]:
    """BATCH reverse lookup: for each destination row id, where it came from.

    This is the single shared resolution the four listings call to add their
    optional ``learned_from``. Returns ``{target_id_str: {...}}``, and an id with
    no entry simply was not born from a learning (a human wrote it) — which is
    exactly what the UI must show: no badge, not a fake one.
    """
    out: dict[str, dict] = {}
    if target not in TARGETS:
        return out
    keys = [str(i) for i in (ids or []) if i is not None]
    if not keys:
        return out
    try:
        from app.app_models.agent_memory import AgentMemory
        async with app_session() as s:
            rows = (await s.execute(
                select(LearningEffect, AgentMemory)
                .join(AgentMemory, AgentMemory.id == LearningEffect.learning_id,
                      isouter=True)
                .where(
                    LearningEffect.target == target,
                    LearningEffect.target_id.in_(keys),
                    LearningEffect.state != STATE_REVERTED,
                )
                .order_by(LearningEffect.created_at.desc())
            )).all()
        for effect, learning in rows:
            # Newest wins if a row was somehow materialized twice.
            out.setdefault(effect.target_id, {
                "effect_id": effect.id,
                "learning_id": effect.learning_id,
                "learning": effect.learning_text,
                "learning_subject": getattr(learning, "subject", None),
                "learning_observations": getattr(learning, "observations", None),
                "approved_by": effect.approved_by,
                "approved_at": (effect.approved_at.isoformat()
                                if effect.approved_at else None),
                "approved_where": effect.approved_where,
                "origin_run_id": effect.origin_run_id,
                "state": effect.state,
            })
    except Exception:  # pragma: no cover — a missing badge must never 500 a list
        logger.debug("origin_for failed for %s", target, exc_info=True)
        return {}
    return out


def serialize(row: LearningEffect) -> dict:
    return {
        "id": row.id,
        "learning_id": row.learning_id,
        "learning": row.learning_text,
        "target": row.target,
        "target_id": row.target_id,
        "label": row.label,
        "home": HOME_HASH.get(row.target, ""),
        "proposed_at": row.proposed_at.isoformat() if row.proposed_at else None,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "approved_where": row.approved_where,
        "origin_run_id": row.origin_run_id,
        "state": row.state,
        "reverted_by": row.reverted_by,
        "reverted_at": row.reverted_at.isoformat() if row.reverted_at else None,
        "revert_reason": row.revert_reason,
    }


async def list_effects(
    *, target: str | None = None, state: str | None = None,
    learning_id: int | None = None, limit: int = 100, offset: int = 0,
) -> dict:
    """The "what has changed in its behaviour" register."""
    async with app_session() as s:
        q = select(LearningEffect)
        if target:
            q = q.where(LearningEffect.target == target)
        if state:
            q = q.where(LearningEffect.state == state)
        if learning_id:
            q = q.where(LearningEffect.learning_id == int(learning_id))
        total = (await s.execute(
            select(func.count()).select_from(q.subquery())
        )).scalar() or 0
        rows = list((await s.execute(
            q.order_by(LearningEffect.created_at.desc())
            .limit(max(1, min(int(limit), 200))).offset(max(0, int(offset)))
        )).scalars().all())
    return {"total": int(total), "items": [serialize(r) for r in rows]}


async def pending_proposals(run_id: int | None = None) -> list[dict]:
    """The proposal queue — an AGGREGATED VIEW, not a table of its own.

    Each item joins the real state of the destination row (is it in force?) with
    the ``why`` from here. There is deliberately no ``proposals`` table: two
    truths about whether a rule is approved is exactly the bug this design is
    avoiding.

    Also surfaces a ``proposed`` silence with no effect row (the employee can
    propose one without a learning behind it) so nothing can hide from the queue.
    """
    from app.app_models.agent_memory import AgentMemory
    from app.app_models.signal_suppressions import SignalSuppression
    items: list[dict] = []
    async with app_session() as s:
        q = (
            select(LearningEffect, AgentMemory)
            .join(AgentMemory, AgentMemory.id == LearningEffect.learning_id,
                  isouter=True)
            .where(LearningEffect.state == STATE_PROPOSED)
        )
        if run_id:
            q = q.where(LearningEffect.origin_run_id == int(run_id))
        rows = (await s.execute(q.order_by(LearningEffect.created_at.desc()))).all()
        seen_suppressions: set[str] = set()
        for effect, learning in rows:
            if effect.target == TARGET_SUPPRESSION:
                seen_suppressions.add(effect.target_id)
            items.append({
                **serialize(effect),
                "learning_subject": getattr(learning, "subject", None),
                "learning_observations": getattr(learning, "observations", None),
                "learning_kind": getattr(learning, "kind", None),
            })
        if not run_id:
            orphans = list((await s.execute(
                select(SignalSuppression).where(
                    SignalSuppression.review_state == "proposed",
                )
            )).scalars().all())
            for sup in orphans:
                if str(sup.id) in seen_suppressions:
                    continue
                items.append({
                    "id": None, "learning_id": None, "learning": "",
                    "target": TARGET_SUPPRESSION, "target_id": str(sup.id),
                    "label": sup.match_label or sup.match_key,
                    "home": HOME_HASH[TARGET_SUPPRESSION],
                    "proposed_at": (sup.created_at.isoformat()
                                    if sup.created_at else None),
                    "approved_by": None, "approved_at": None,
                    "approved_where": None,
                    "origin_run_id": sup.created_by_run_id,
                    "state": STATE_PROPOSED,
                    "reverted_by": None, "reverted_at": None,
                    "revert_reason": None,
                    "learning_subject": None, "learning_observations": None,
                    "learning_kind": None,
                })
    return items
