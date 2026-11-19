"""Operator-managed KNOWLEDGE / INSTRUCTIONS with a scope.

CRUD over ``ScopedKnowledge`` (Operations → "Conocimiento"). Entries are
injected into the agent's prompt per run, filtered by scope — see
``app.scoped_knowledge.build_scoped_knowledge_block``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import delete as sql_delete, select, update

from app.timeutil import iso_utc
from app.app_database import app_session, app_write
from app.app_models.runtime import KnowledgeRequest, ScopedKnowledge
from app.auth import require_project_access, require_admin, TokenPayload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

_VALID_SCOPES = ("all", "autonomous", "attending", "user")


def _row_to_dict(r: ScopedKnowledge) -> dict[str, Any]:
    return {
        "id": r.id,
        "title": r.title,
        "content": r.content,
        "scope": r.scope,
        "user_email": r.user_email,
        "enabled": bool(r.enabled),
        "position": r.position,
        "updated_by": r.updated_by,
        "updated_at": iso_utc(r.updated_at),
    }


def _validate(body: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "title" in body or not partial:
        title = str(body.get("title") or "").strip()
        if not title:
            raise HTTPException(422, "title is required")
        out["title"] = title[:200]
    if "content" in body or not partial:
        content = str(body.get("content") or "").strip()
        if not content:
            raise HTTPException(422, "content is required")
        out["content"] = content
    if "scope" in body or not partial:
        scope = str(body.get("scope") or "all").strip().lower()
        if scope not in _VALID_SCOPES:
            raise HTTPException(400, f"scope must be one of {_VALID_SCOPES}")
        out["scope"] = scope
    if "user_email" in body:
        out["user_email"] = (str(body.get("user_email") or "").strip().lower() or None)
    if "enabled" in body:
        out["enabled"] = 1 if body.get("enabled") else 0
    if "position" in body:
        try:
            out["position"] = int(body.get("position") or 0)
        except (TypeError, ValueError):
            out["position"] = 0
    # A user-scoped entry needs a target email.
    eff_scope = out.get("scope")
    if eff_scope == "user" and "user_email" in out and not out["user_email"]:
        raise HTTPException(422, "user_email is required when scope == 'user'")
    return out


async def _with_origin(target: str, items: list[dict], key: str = "id") -> list[dict]:
    """Attach the optional ``learned_from`` stamp (v7.140.0).

    ONE batch call to the shared resolver; the link lives only in
    ``learning_effects``, never as a column here. So an entry a human wrote comes
    back with no badge — which is exactly right: the point of the stamp is that
    nothing the EMPLOYEE introduced looks like it appeared out of nowhere.
    """
    if not items:
        return items
    try:
        from app import learning_effects_service as lfx
        origins = await lfx.origin_for(target, [i.get(key) for i in items])
    except Exception:  # pragma: no cover — a missing badge must never 500 a list
        return items
    for it in items:
        found = origins.get(str(it.get(key)))
        if found:
            it["learned_from"] = found
    return items


@router.get("/knowledge")
async def list_knowledge(user: TokenPayload = Depends(require_admin)):
    """All knowledge entries (operator-facing, ordered).

    SECURITY: admin only. Returns ALL entries, including those with `scope='user'`
    —instructions about specific people, with their `user_email`—; with
    `require_project_access` a member could read what the employee knows about their
    colleagues. The document library already gates this same axis with admin. The only
    screen that consumes it (Operaciones → Conocimiento) is admin-only."""
    async with app_session() as s:
        rows = list((await s.execute(
            select(ScopedKnowledge)
            .order_by(ScopedKnowledge.position.asc(), ScopedKnowledge.id.asc())
        )).scalars().all())
    from app.learning_effects_service import TARGET_INSTRUCTION
    items = await _with_origin(TARGET_INSTRUCTION, [_row_to_dict(r) for r in rows])
    return {"items": items, "scopes": list(_VALID_SCOPES)}


@router.post("/knowledge")
async def create_knowledge(
    body: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    values = _validate(body, partial=False)
    if values.get("scope") == "user" and not values.get("user_email"):
        raise HTTPException(422, "user_email is required when scope == 'user'")
    now = datetime.now(timezone.utc)
    row = ScopedKnowledge(
        title=values["title"],
        content=values["content"],
        scope=values.get("scope", "all"),
        user_email=values.get("user_email"),
        enabled=values.get("enabled", 1),
        position=values.get("position", 0),
        created_by=getattr(user, "email", None),
        updated_by=getattr(user, "email", None),
        created_at=now,
        updated_at=now,
    )
    async with app_write() as s:
        s.add(row)
        await s.commit()
        await s.refresh(row)
    return _row_to_dict(row)


@router.patch("/knowledge/{item_id}")
async def update_knowledge(
    item_id: int,
    body: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    values = _validate(body, partial=True)
    if not values:
        raise HTTPException(422, "no updatable fields supplied")
    values["updated_by"] = getattr(user, "email", None)
    values["updated_at"] = datetime.now(timezone.utc)
    async with app_write() as s:
        res = await s.execute(
            update(ScopedKnowledge)
            .where(ScopedKnowledge.id == item_id)
            .values(**values)
        )
        if res.rowcount == 0:
            raise HTTPException(404, f"knowledge {item_id} not found")
        await s.commit()
        row = (await s.execute(
            select(ScopedKnowledge).where(ScopedKnowledge.id == item_id)
        )).scalar_one()
    return _row_to_dict(row)


@router.delete("/knowledge/{item_id}")
async def delete_knowledge(
    item_id: int,
    user: TokenPayload = Depends(require_admin),
):
    async with app_write() as s:
        res = await s.execute(
            sql_delete(ScopedKnowledge).where(ScopedKnowledge.id == item_id)
        )
        await s.commit()
    return {"deleted": bool(res.rowcount)}


# ── Knowledge REQUESTS (structured business parameters the agent asks for) ──
# v7.43.0. The inverse of ScopedKnowledge: the agent registers a TYPED parameter
# it needs (via the ``request_knowledge`` MCP tool); the operator fills a typed
# value here; the confirmed value is injected into the prompt. Members' chat
# changes arrive as ``proposed_value`` an admin confirms. Reads are open to any
# authenticated user; every mutation is admin-only (it governs behaviour).

_KR_VALUE_TYPES = ("text", "number", "boolean", "list", "date")


def _kr_slug(raw: Any) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")
    return s[:80] or "param"


def _kr_to_dict(r: KnowledgeRequest) -> dict[str, Any]:
    try:
        options = json.loads(r.options_json) if r.options_json else []
    except Exception:
        options = []
    return {
        "id": r.id,
        "key": r.key,
        "label": r.label,
        "question": r.question or "",
        # English pair (falls back to the Spanish text when the agent didn't
        # translate it). The UI picks label/question or *_en by i18n.language.
        "label_en": r.label_en or None,
        "question_en": r.question_en or None,
        "value_type": r.value_type,
        "unit": r.unit,
        "options": options,
        "suggested": r.suggested,
        "value": r.value,
        "status": r.status,
        "origin": r.origin,
        "proposed_value": r.proposed_value,
        "proposed_by": r.proposed_by,
        "proposed_at": iso_utc(r.proposed_at),
        "has_proposal": bool((r.proposed_value or "") != ""),
        "provided_by": r.provided_by,
        "provided_at": iso_utc(r.provided_at),
        "requested_at": iso_utc(r.requested_at),
        "updated_at": iso_utc(r.updated_at),
    }


def _kr_needs_attention(r: KnowledgeRequest) -> bool:
    """A request the operator should look at: no confirmed value yet, OR a
    pending proposal awaiting confirmation."""
    return r.status != "provided" or bool((r.proposed_value or "") != "")


@router.get("/knowledge-requests")
async def list_knowledge_requests(
    _: TokenPayload = Depends(require_project_access),
):
    """All business parameters (pending first). ``pending_count`` drives the
    badge/banner that tells the operator the employee is waiting on data."""
    async with app_session() as s:
        rows = list((await s.execute(
            select(KnowledgeRequest)
            .order_by(KnowledgeRequest.status.asc(), KnowledgeRequest.label.asc(),
                      KnowledgeRequest.id.asc())
        )).scalars().all())
    from app.learning_effects_service import TARGET_PARAMETER
    # A business parameter is keyed by its slug, not its row id — that is what
    # the agent reads and what the effect stores as ``target_id``.
    items = await _with_origin(
        TARGET_PARAMETER, [_kr_to_dict(r) for r in rows], key="key")
    return {
        "items": items,
        "value_types": list(_KR_VALUE_TYPES),
        "pending_count": sum(1 for r in rows if _kr_needs_attention(r)),
    }


@router.post("/knowledge-requests")
async def create_knowledge_request(
    body: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """Operator PRE-DEFINES a business parameter (seed it before the agent asks).
    Optionally supply ``value`` to define + answer in one go."""
    label = str(body.get("label") or "").strip()
    if not label:
        raise HTTPException(422, "label is required")
    key = _kr_slug(body.get("key") or label)
    value_type = str(body.get("value_type") or "text").strip().lower()
    if value_type not in _KR_VALUE_TYPES:
        value_type = "text"
    opts = body.get("options")
    options_json = json.dumps([str(o) for o in opts]) if isinstance(opts, list) and opts else None
    raw_value = body.get("value")
    value = str(raw_value) if raw_value not in (None, "") else None
    now = datetime.now(timezone.utc)
    async with app_write() as s:
        if (await s.execute(
            select(KnowledgeRequest).where(KnowledgeRequest.key == key)
        )).scalar_one_or_none() is not None:
            raise HTTPException(409, f"knowledge key '{key}' already exists")
        row = KnowledgeRequest(
            key=key,
            label=label[:200],
            question=str(body.get("question") or ""),
            label_en=(str(body.get("label_en") or "").strip()[:200] or None),
            question_en=(str(body.get("question_en") or "").strip() or None),
            value_type=value_type,
            unit=(str(body.get("unit") or "").strip() or None),
            options_json=options_json,
            suggested=(str(body["suggested"]) if body.get("suggested") not in (None, "") else None),
            value=value,
            status="provided" if value else "pending",
            origin="operator",
            provided_by=getattr(user, "email", None) if value else None,
            provided_at=now if value else None,
            created_at=now,
            updated_at=now,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
    return _kr_to_dict(row)


@router.patch("/knowledge-requests/{item_id}")
async def update_knowledge_request(
    item_id: int,
    body: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """Provide / edit a value, or confirm/reject a member's proposal. Setting a
    value (or confirming a proposal) marks it ``provided`` and injects it into
    the agent's context; clearing it reverts to ``pending``."""
    now = datetime.now(timezone.utc)
    async with app_write() as s:
        row = (await s.execute(
            select(KnowledgeRequest).where(KnowledgeRequest.id == item_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, f"knowledge request {item_id} not found")

        if body.get("reject_proposal"):
            row.proposed_value = None
            row.proposed_by = None
            row.proposed_at = None
        elif body.get("confirm_proposal"):
            if (row.proposed_value or "") != "":
                row.value = row.proposed_value
                row.status = "provided"
                row.provided_by = getattr(user, "email", None)
                row.provided_at = now
                row.proposed_value = None
                row.proposed_by = None
                row.proposed_at = None
        else:
            if "label" in body:
                row.label = (str(body["label"]).strip()[:200] or row.label)
            if "question" in body:
                row.question = str(body.get("question") or "")
            if "label_en" in body:
                row.label_en = (str(body.get("label_en") or "").strip()[:200] or None)
            if "question_en" in body:
                row.question_en = (str(body.get("question_en") or "").strip() or None)
            if "unit" in body:
                row.unit = (str(body.get("unit") or "").strip() or None)
            if "value_type" in body:
                vt = str(body.get("value_type") or "").strip().lower()
                if vt in _KR_VALUE_TYPES:
                    row.value_type = vt
            if "options" in body:
                opts = body.get("options")
                row.options_json = (
                    json.dumps([str(o) for o in opts]) if isinstance(opts, list) and opts else None
                )
            if "suggested" in body:
                row.suggested = (str(body["suggested"]) if body.get("suggested") not in (None, "") else None)
            if "value" in body:
                v = body.get("value")
                if v in (None, ""):
                    row.value = None
                    row.status = "pending"
                    row.provided_by = None
                    row.provided_at = None
                else:
                    row.value = str(v)
                    row.status = "provided"
                    row.provided_by = getattr(user, "email", None)
                    row.provided_at = now
                # A manual set supersedes any pending proposal.
                row.proposed_value = None
                row.proposed_by = None
                row.proposed_at = None
        row.updated_at = now
        await s.commit()
        await s.refresh(row)
    return _kr_to_dict(row)


@router.delete("/knowledge-requests/{item_id}")
async def delete_knowledge_request(
    item_id: int,
    user: TokenPayload = Depends(require_admin),
):
    async with app_write() as s:
        res = await s.execute(
            sql_delete(KnowledgeRequest).where(KnowledgeRequest.id == item_id)
        )
        await s.commit()
    return {"deleted": bool(res.rowcount)}
