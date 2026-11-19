"""Operations → Aprendizaje tab API (v7.140.0).

The operator surface over the employee's SELF-memory (what it learned about the
ENVIRONMENT) and over the bridge from a learning to a real rule. Sibling of
``memory_routes.py``, and the split is deliberate: that one is keyed by PERSON,
this one by ``subject``.

Three groups of endpoints, answering three different operator questions:

* ``/entries`` `/episodes` `/subjects` `/stats` `/block` — "what does it believe
  about my environment, and what of that is actually in its head right now?"
  (``/block`` renders the LITERAL injected text: for a cluster watcher, seeing
  what it thinks "normal" is beats any reconstruction.)
* ``/proposals`` — "what is it asking me to let it do?" An AGGREGATED VIEW over
  the four destination tables, never a proposals table of its own.
* ``/effects`` — "what have I let it change, and how do I undo it?"

Governance: browsing is ``require_project_access`` like the rest of
``/api/agent``; anything that VALIDATES a learning or puts a rule in force is
``require_admin``. Same anti-self-escalation line the platform already draws
around ``set_knowledge`` / ``KnowledgeRequest``.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import delete as sql_delete, func, or_, select, update as sql_update

from app.app_database import app_session, app_write
from app.app_models.agent_memory import AgentEpisode, AgentMemory
from app.auth import TokenPayload, require_admin, require_project_access
from app.timeutil import iso_utc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent/self-memory", tags=["self-memory"])

KINDS = ("observation", "baseline", "lesson", "procedure", "entity")
REVIEW_STATES = ("new", "validated", "rejected")


def _entry_dict(r: AgentMemory, injected: set[int] | None = None) -> dict:
    from app.agent_memory_utils import trust_level
    now = datetime.utcnow()
    expired = bool(r.valid_until is not None and r.valid_until < now)
    base = {
        "id": r.id,
        "kind": r.kind,
        "subject": r.subject,
        "content": r.content,
        "tags": r.tags,
        "observations": int(r.observations or 1),
        "first_seen_at": iso_utc(r.first_seen_at),
        "last_confirmed_at": iso_utc(r.last_confirmed_at),
        "valid_until": iso_utc(r.valid_until),
        "expired": expired,
        "confidence": r.confidence,
        "importance": r.importance,
        "pinned": bool(r.pinned),
        "review_state": r.review_state,
        "reviewed_by": r.reviewed_by,
        "reviewed_at": iso_utc(r.reviewed_at),
        # What answers "does this affect it RIGHT NOW?" in the UI: applied as a
        # rule (hard), in its context (soft), or merely stored.
        "behavior_effect": r.behavior_effect,
        "repropose_blocked": bool(r.repropose_blocked),
        "in_context": (injected is not None and r.id in injected),
        "superseded_by": r.superseded_by,
        "source": r.source,
        "created_by_run_id": r.created_by_run_id,
        "created_at": iso_utc(r.created_at),
        "updated_at": iso_utc(r.updated_at),
    }
    # 7.154.0 — TRUST derived from the evidence (times seen, used, confirmed),
    # which is what the screen must show instead of "not validated" on everything.
    #
    # 7.164.0 — the ORDER mattered and was wrong: `trust_level` was called BEFORE
    # setting `access_count`, so the "ever used" dimension was always 0 and that
    # branch of trust never ran from this route. The signals are filled in first
    # and the trust is derived afterwards.
    base["access_count"] = int(r.access_count or 0)
    base["used_ok"] = int(getattr(r, "used_ok", 0) or 0)
    base["used_ko"] = int(getattr(r, "used_ko", 0) or 0)
    base["trust"] = trust_level(base)
    return base


def _episode_dict(e: AgentEpisode) -> dict:
    return {
        "id": e.id, "subject": e.subject, "summary": e.summary,
        "outcome": e.outcome, "source": e.source,
        "created_by_run_id": e.created_by_run_id,
        "created_at": iso_utc(e.created_at),
    }


@router.get("/entries")
async def list_entries(
    kind: str | None = Query(default=None),
    subject: str | None = Query(default=None),
    review_state: str | None = Query(default=None),
    q: str | None = Query(default=None),
    include_superseded: bool = Query(default=False),
    sort: str = Query(default="evidence"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: TokenPayload = Depends(require_project_access),
):
    stmt = select(AgentMemory)
    if not include_superseded:
        stmt = stmt.where(AgentMemory.superseded_by.is_(None))
    if kind in KINDS:
        stmt = stmt.where(AgentMemory.kind == kind)
    if subject:
        from app.agent_memory_utils import normalize_subject
        stmt = stmt.where(AgentMemory.subject == normalize_subject(subject))
    if review_state in REVIEW_STATES:
        stmt = stmt.where(AgentMemory.review_state == review_state)
    if (q or "").strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(AgentMemory.content.ilike(like),
                              AgentMemory.subject.ilike(like)))
    order = {
        "evidence": (AgentMemory.observations.desc(), AgentMemory.updated_at.desc()),
        "recent": (AgentMemory.updated_at.desc(),),
        "subject": (AgentMemory.subject.asc(), AgentMemory.kind.asc()),
    }.get(sort, (AgentMemory.observations.desc(), AgentMemory.updated_at.desc()))
    async with app_session() as s:
        total = (await s.execute(
            select(func.count()).select_from(stmt.subquery())
        )).scalar() or 0
        rows = list((await s.execute(
            stmt.order_by(*order).limit(limit).offset(offset)
        )).scalars().all())
    # Which of these are ACTUALLY in the injected block right now. Read-only
    # preview: never enqueues consolidation, or inspecting the tab would
    # schedule work.
    injected: set[int] = set()
    try:
        from app.agent_memory_prompt import build_agent_memory_block_detail
        detail = await build_agent_memory_block_detail(enqueue_consolidation=False)
        injected = {int(i) for i in detail.get("entry_ids") or []}
    except Exception:  # pragma: no cover — the badge is best-effort
        injected = set()
    return {"total": int(total), "items": [_entry_dict(r, injected) for r in rows]}


@router.get("/episodes")
async def list_episodes(
    subject: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: TokenPayload = Depends(require_project_access),
):
    stmt = select(AgentEpisode)
    if subject:
        from app.agent_memory_utils import normalize_subject
        stmt = stmt.where(AgentEpisode.subject == normalize_subject(subject))
    async with app_session() as s:
        total = (await s.execute(
            select(func.count()).select_from(stmt.subquery())
        )).scalar() or 0
        rows = list((await s.execute(
            stmt.order_by(AgentEpisode.created_at.desc()).limit(limit).offset(offset)
        )).scalars().all())
    return {"total": int(total), "items": [_episode_dict(e) for e in rows]}


@router.get("/subjects")
async def list_subjects(user: TokenPayload = Depends(require_project_access)):
    """The environment as the employee has it carved up — what the viewer groups
    by, and the fastest read of "what has it been looking at"."""
    async with app_session() as s:
        rows = (await s.execute(
            select(
                AgentMemory.subject,
                func.count(AgentMemory.id),
                func.sum(AgentMemory.observations),
                func.max(AgentMemory.updated_at),
            )
            .where(AgentMemory.superseded_by.is_(None))
            .group_by(AgentMemory.subject)
            .order_by(func.count(AgentMemory.id).desc())
        )).all()
    return {"items": [{
        "subject": r[0], "entries": int(r[1] or 0),
        "observations": int(r[2] or 0), "last_update": iso_utc(r[3]),
    } for r in rows]}


@router.get("/stats")
async def stats(user: TokenPayload = Depends(require_project_access)):
    from app.agent_memory_utils import stats as _stats
    from app.config import AGENT_SELF_MEMORY_ENABLED
    payload = await _stats()
    payload["enabled"] = bool(AGENT_SELF_MEMORY_ENABLED)
    try:
        from app.agent_memory_prompt import build_agent_memory_block_detail
        detail = await build_agent_memory_block_detail(enqueue_consolidation=False)
        payload["block_chars"] = detail.get("chars") or 0
        payload["block_tokens"] = detail.get("tokens") or 0
        payload["injected"] = len(detail.get("entry_ids") or [])
    except Exception:  # pragma: no cover
        pass
    # v7.143.0 — what the LAST round decided about learning. With memory at
    # zero, "there is nothing" and "it does not learn" look the same from the
    # panel; this verdict tells them apart (and says so if the block was absent).
    try:
        from app.agent_memory_utils import last_verdict
        payload["last_verdict"] = await last_verdict()
    except Exception:  # pragma: no cover
        payload["last_verdict"] = None
    return payload


@router.get("/attention")
async def attention_queue(user: TokenPayload = Depends(require_project_access)):
    """THE SHORT QUEUE: the only thing that needs a person's judgement.

    Replaces "review all 107": contradictions the machine cannot resolve and
    important things backed by a single observation. If this comes back empty,
    there is nothing to decide — and that is information too."""
    from app.agent_memory_utils import needs_attention
    return await needs_attention()


@router.get("/block")
async def block(
    subject: str | None = Query(default=None),
    user: TokenPayload = Depends(require_project_access),
):
    """The LITERAL text injected into the employee's prompt. The key piece for a
    watcher: you read what it believes about your cluster, not a reconstruction
    of it."""
    from app.agent_memory_prompt import build_agent_memory_block_detail
    detail = await build_agent_memory_block_detail(
        subject=subject, enqueue_consolidation=False)
    return {
        "text": detail.get("text") or "", "empty": bool(detail.get("empty")),
        "chars": detail.get("chars") or 0, "tokens": detail.get("tokens") or 0,
        "entry_ids": detail.get("entry_ids") or [],
    }


@router.get("/export")
async def export_all(user: TokenPayload = Depends(require_project_access)):
    async with app_session() as s:
        entries = list((await s.execute(
            select(AgentMemory).order_by(AgentMemory.subject.asc(),
                                        AgentMemory.kind.asc())
        )).scalars().all())
        episodes = list((await s.execute(
            select(AgentEpisode).order_by(AgentEpisode.created_at.desc()).limit(500)
        )).scalars().all())
    return {
        "exported_at": iso_utc(datetime.utcnow()),
        "entries": [_entry_dict(r) for r in entries],
        "episodes": [_episode_dict(e) for e in episodes],
    }


@router.post("/entries")
async def create_entry(
    body: dict = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """An operator TEACHING the employee something about the environment.

    Admin-only and stored ``validated``: unlike an agent-written learning, this
    one comes from the person who owns the environment, so it is ground truth
    from the start.
    """
    from app.agent_memory_utils import bump_memory_version, remember
    content = str(body.get("content") or "").strip()
    subject = str(body.get("subject") or "").strip()
    if not content or not subject:
        raise HTTPException(422, "subject and content are required")
    res = await remember(
        content=content, subject=subject,
        kind=str(body.get("kind") or "baseline"),
        source="operator",
        importance=body.get("importance"), confidence=body.get("confidence"),
        valid_until=body.get("valid_until"),
        tags=(str(body["tags"]) if body.get("tags") else None),
    )
    if not res.get("id"):
        raise HTTPException(400, res.get("error") or "could not store the learning")
    now = datetime.utcnow()
    async with app_write() as s:
        await s.execute(
            sql_update(AgentMemory).where(AgentMemory.id == res["id"]).values(
                review_state="validated",
                reviewed_by=getattr(user, "email", None) or "operator",
                reviewed_at=now, pinned=bool(body.get("pinned", True)),
                updated_at=now,
            )
        )
        await s.commit()
        row = (await s.execute(
            select(AgentMemory).where(AgentMemory.id == res["id"])
        )).scalar_one()
        payload = _entry_dict(row)
    bump_memory_version()
    return payload


@router.patch("/entries/{entry_id}")
async def update_entry(
    entry_id: int,
    body: dict = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """Correct, pin or re-date a learning.

    SECURITY: admin only, like POST/DELETE/review/bulk. These entries are injected into
    the prompt of ALL sessions: with `require_project_access` a member could rewrite
    the `content` (or pin it) and slip an instruction into their colleagues' prompt —a
    cross-user prompt injection—. The screen that edits it (Operaciones → Aprendizaje)
    is already admin-only."""
    from app.agent_memory_utils import (
        bump_memory_version, content_fingerprint, parse_valid_until,
    )
    async with app_write() as s:
        row = (await s.execute(
            select(AgentMemory).where(AgentMemory.id == entry_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, f"learning {entry_id} not found")
        if "content" in body:
            new = str(body["content"]).strip()
            if not new:
                raise HTTPException(422, "content cannot be empty")
            row.content = new
            # Keep the fingerprint in step, or reinforcement would stop folding
            # future re-assertions into this row.
            row.content_hash = content_fingerprint(row.subject, new)
        if "kind" in body and str(body["kind"]) in KINDS:
            row.kind = str(body["kind"])
        if "tags" in body:
            row.tags = (str(body["tags"]).strip() or None)
        if "pinned" in body:
            row.pinned = bool(body["pinned"])
        if "importance" in body:
            try:
                row.importance = float(body["importance"])
            except (TypeError, ValueError):
                pass
        if "valid_until" in body:
            row.valid_until = parse_valid_until(body["valid_until"])
        row.updated_at = datetime.utcnow()
        await s.commit()
        await s.refresh(row)
        payload = _entry_dict(row)
    bump_memory_version()
    return payload


@router.post("/entries/{entry_id}/review")
async def review_entry(
    entry_id: int,
    body: dict = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """Confirm or discard a learning — ADMIN ONLY.

    7.154.0 — The human decision now HAS A CONSEQUENCE, which is what it lacked:
    before, it only removed the "⚠ SIN VALIDAR" label from the block, so confirming
    was useless and that is why nobody confirmed (103 not validated / 4 validated,
    measured).

      · CONFIRM → HIGH trust, and it becomes PROTECTED from the lifecycle: neither
        the disuse pruning retires it nor a new unconfirmed entry supersedes it. What
        a person has accepted is not thrown away by a heuristic.
      · DISCARD → it is retired AND re-proposal is blocked (`repropose_blocked`), so
        the employee does not propose the same thing again next week.

    It still does NOT authorize a behavior change: that is what proposal and effect
    are for.
    """
    state = str(body.get("review_state") or "").strip().lower()
    if state not in ("validated", "rejected"):
        raise HTTPException(422, "review_state must be validated|rejected")
    now = datetime.utcnow()
    async with app_write() as s:
        row = (await s.execute(
            select(AgentMemory).where(AgentMemory.id == entry_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, f"learning {entry_id} not found")
        row.review_state = state
        row.reviewed_by = getattr(user, "email", None) or "operator"
        row.reviewed_at = now
        row.updated_at = now
        if state == "validated":
            # Protected: disuse pruning and expiry respect what is pinned.
            row.pinned = True
        else:
            # Discarded: out of the block and never proposed again.
            row.pinned = False
            row.repropose_blocked = True
        await s.commit()
        await s.refresh(row)
        payload = _entry_dict(row)
    from app.agent_memory_utils import bump_memory_version
    bump_memory_version()
    return payload


@router.delete("/entries/{entry_id}")
async def delete_entry(
    entry_id: int,
    user: TokenPayload = Depends(require_admin),
):
    async with app_write() as s:
        row = (await s.execute(
            select(AgentMemory).where(AgentMemory.id == entry_id)
        )).scalar_one_or_none()
        if row is None:
            return {"deleted": False}
        if row.behavior_effect:
            raise HTTPException(
                409,
                ("Este aprendizaje sostiene una regla activa "
                 f"({row.behavior_effect}). Revierte el efecto primero, en "
                 "«Qué ha cambiado en su comportamiento»."),
            )
        await s.execute(sql_delete(AgentMemory).where(AgentMemory.id == entry_id))
        await s.commit()
    from app.agent_memory_utils import bump_memory_version
    bump_memory_version()
    return {"deleted": True}


@router.post("/entries/bulk")
async def bulk_entries(
    body: dict = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """Validate / reject / delete several learnings at once — the only way the
    tab is usable after a few months of nightly observations."""
    ids = [int(i) for i in (body.get("ids") or []) if str(i).isdigit()]
    action = str(body.get("action") or "").strip().lower()
    if not ids:
        raise HTTPException(422, "ids is required")
    now = datetime.utcnow()
    async with app_write() as s:
        if action == "delete":
            protected = list((await s.execute(
                select(AgentMemory.id).where(
                    AgentMemory.id.in_(ids),
                    AgentMemory.behavior_effect.isnot(None),
                )
            )).scalars().all())
            deletable = [i for i in ids if i not in set(protected)]
            if deletable:
                await s.execute(
                    sql_delete(AgentMemory).where(AgentMemory.id.in_(deletable))
                )
            await s.commit()
            out = {"deleted": len(deletable), "skipped_with_rule": len(protected)}
        elif action in ("validated", "rejected"):
            await s.execute(
                sql_update(AgentMemory).where(AgentMemory.id.in_(ids)).values(
                    review_state=action,
                    reviewed_by=getattr(user, "email", None) or "operator",
                    reviewed_at=now, updated_at=now,
                )
            )
            await s.commit()
            out = {"reviewed": len(ids), "review_state": action}
        else:
            raise HTTPException(422, "action must be validated|rejected|delete")
    from app.agent_memory_utils import bump_memory_version
    bump_memory_version()
    return out


@router.post("/compact")
async def compact_now(user: TokenPayload = Depends(require_admin)):
    from app.agent_memory_utils import compact
    return {"ok": True, **(await compact())}


@router.post("/consolidate")
async def consolidate_now(user: TokenPayload = Depends(require_admin)):
    """Request semantic consolidation NOW — ADMIN ONLY (7.164.0).

    Merging what is repeated has no clock on purpose: it costs a model call and is
    enqueued on its own when the employee works. The practical problem is the IDLE
    employee: it accumulates repetition and is never cleaned inside because it never
    builds its block. Until now the only way out was to wait for it to work.

    Enqueues the maintenance run bypassing the redundancy trigger, but NOT the
    one-per-hour brake: the cost cap still stands, so clicking twice in a row does
    not charge twice. Returns `queued: false` when one was already queued.
    """
    from app.agent_memory_utils import force_consolidation
    encolada = await force_consolidation()
    return {"ok": True, "queued": bool(encolada)}


# ── Proposals: what it is asking to be allowed to do ──────────────────
@router.get("/proposals")
async def proposals(
    run_id: int | None = Query(default=None),
    user: TokenPayload = Depends(require_project_access),
):
    """Pending proposals. With ``run_id`` it returns only those born in that
    conversation — which is what feeds the in-thread card, with no change to the
    SSE contract."""
    from app import learning_effects_service as lfx
    items = await lfx.pending_proposals(run_id)
    return {"items": items, "total": len(items)}


@router.post("/proposals/{effect_id}/accept")
async def accept_proposal(
    effect_id: int,
    body: dict = Body(default={}),
    user: TokenPayload = Depends(require_admin),
):
    """Accept a proposal — ADMIN ONLY. Puts the rule in force in its own home and
    stamps who approved it and WHERE (``chat`` when accepted in the thread,
    ``queue`` from this tab), so the audit trail can point at the conversation
    where it was agreed."""
    from app import learning_effects_service as lfx
    where = str(body.get("where") or "queue").strip().lower()
    payload = await lfx.accept(
        effect_id, approved_by=getattr(user, "email", None) or "operator",
        approved_where=("chat" if where == "chat" else "queue"),
    )
    if payload is None:
        raise HTTPException(404, f"effect {effect_id} not found")
    return payload


@router.post("/proposals/{effect_id}/reject")
async def reject_proposal(
    effect_id: int,
    body: dict = Body(default={}),
    user: TokenPayload = Depends(require_admin),
):
    """Reject a proposal — ADMIN ONLY.

    Deliberately the SAME path as revert: it undoes the (not-yet-in-force) row in
    its home, closes the effect and marks the learning so it is not proposed
    again. Otherwise saying "no" today means saying it again next week.
    """
    from app import learning_effects_service as lfx
    payload = await lfx.revert(
        effect_id, reverted_by=getattr(user, "email", None) or "operator",
        reason=(str(body.get("reason") or "").strip() or "Propuesta rechazada"),
    )
    if payload is None:
        raise HTTPException(404, f"effect {effect_id} not found")
    return payload


# ── Effects: what I have let it change, and how to undo it ────────────
@router.get("/effects")
async def effects(
    target: str | None = Query(default=None),
    state: str | None = Query(default=None),
    learning_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: TokenPayload = Depends(require_project_access),
):
    from app import learning_effects_service as lfx
    return await lfx.list_effects(
        target=target, state=state, learning_id=learning_id,
        limit=limit, offset=offset,
    )


@router.post("/effects/{effect_id}/revert")
async def revert_effect(
    effect_id: int,
    body: dict = Body(default={}),
    user: TokenPayload = Depends(require_admin),
):
    """Undo a learned rule in one click — ADMIN ONLY.

    The learning SURVIVES (the employee still knows what it observed) but stops
    acting, and is flagged so it does not re-propose the same thing.
    """
    from app import learning_effects_service as lfx
    payload = await lfx.revert(
        effect_id, reverted_by=getattr(user, "email", None) or "operator",
        reason=(str(body.get("reason") or "").strip() or None),
    )
    if payload is None:
        raise HTTPException(404, f"effect {effect_id} not found")
    return payload
