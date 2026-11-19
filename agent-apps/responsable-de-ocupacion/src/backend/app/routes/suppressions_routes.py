"""v7.64.0 — CRUD for SignalSuppression ("silencios / no repetir") on the alerts /
recommendations / decisions screens.

A suppression = "do NOT regenerate this recurring signal, forever or until a date".
It is created inline when you close a signal (pick it from the recently-closed list)
or from this manager, and enforced deterministically at signal-creation time (see
``signal_suppress.gate_signal_creation``). Everything is recorded, visible and
reversible: deleting a rule re-enables generation immediately.

Reads + writes are project-scoped (any operator can silence their own noise — it's
low-risk, reversible and attributed). Only a HUMAN may create a PERMANENT mute; the
agent (via the MCP tool) is capped to bounded snoozes.

v7.140.0 — three additions, all in this same manager because a silence is ONE
concept in the product:

* a silence may carry a CONDITION ("normal on Sundays during the backup"), and
  the ``/preview`` dry-run shows its effect over real history BEFORE you accept
  anything — the only honest way to approve conditional behaviour;
* a silence the EMPLOYEE proposed shows up here as ``proposed`` and silences
  nothing until an admin accepts it (``/accept`` — admin-only);
* ``/silences`` is the "why didn't it warn me?" feed, and every row carries an
  optional ``learned_from`` so no entry is orphaned.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import delete as sql_delete, select

from app.app_database import app_session, app_write
from app.app_models.business import Alert, Decision, Recommendation
from app.app_models.signal_silence_log import SignalSilenceLog
from app.app_models.signal_suppressions import SignalSuppression
from app.auth import TokenPayload, require_admin, require_project_access
from app.signal_suppress import (
    create_suppression, is_active, signal_label, suppression_key,
)
from app.timeutil import iso_utc, now_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent/suppressions", tags=["suppressions"])

_TABLES = {"alerts", "recommendations", "decisions"}
_MODEL = {"alerts": Alert, "recommendations": Recommendation, "decisions": Decision}


def _to_dict(r: SignalSuppression) -> dict:
    cond = None
    if getattr(r, "condition_json", None):
        try:
            cond = json.loads(r.condition_json)
        except ValueError:
            cond = None
    return {
        "id": r.id,
        "signal_table": r.signal_table,
        "muted": r.match_label or r.match_key or r.subject_pattern,
        "match_key": r.match_key,
        "muted_until": iso_utc(r.muted_until),   # None ⇒ forever
        "forever": r.muted_until is None and not getattr(r, "expires_at", None),
        "active": is_active(r),
        "reason": r.reason,
        "created_by": r.created_by,
        "by_agent": (r.created_by or "") == "agent",
        "source_signal_id": r.source_signal_id,
        "suppress_count": r.suppress_count or 0,
        "last_suppressed_at": iso_utc(r.last_suppressed_at),
        "enabled": bool(r.enabled),
        "created_at": iso_utc(r.created_at),
        # v7.140.0
        "review_state": getattr(r, "review_state", "active"),
        "proposed": (getattr(r, "review_state", "active") == "proposed"),
        "subject_pattern": getattr(r, "subject_pattern", None),
        "condition": cond,
        "conditional": cond is not None,
        "severity_max": getattr(r, "severity_max", None),
        "expires_at": iso_utc(getattr(r, "expires_at", None)),
    }


async def _with_origin(items: list[dict]) -> list[dict]:
    """Attach the optional ``learned_from`` stamp. ONE batch call to the shared
    resolver — the link lives only in ``learning_effects``, so a row a human
    created simply comes back without a badge instead of a faked one."""
    if not items:
        return items
    try:
        from app import learning_effects_service as lfx
        origins = await lfx.origin_for(
            lfx.TARGET_SUPPRESSION, [i["id"] for i in items])
    except Exception:  # pragma: no cover — a missing badge must never 500 a list
        return items
    for it in items:
        found = origins.get(str(it["id"]))
        if found:
            it["learned_from"] = found
    return items


@router.get("")
async def list_rules(
    table: str | None = Query(default=None),
    user: TokenPayload = Depends(require_project_access),
):
    async with app_session() as s:
        stmt = select(SignalSuppression).order_by(
            SignalSuppression.signal_table.asc(), SignalSuppression.id.desc())
        if table in _TABLES:
            stmt = stmt.where(SignalSuppression.signal_table == table)
        rows = (await s.execute(stmt)).scalars().all()
    return {"items": await _with_origin([_to_dict(r) for r in rows])}


@router.get("/options")
async def options(
    table: str = Query(...),
    user: TokenPayload = Depends(require_project_access),
):
    """Recently CLOSED signals (done/dismissed) of this table, so the operator can
    pick 'stop repeating THIS one' by name — never a raw id."""
    if table not in _TABLES:
        raise HTTPException(422, "table must be alerts|recommendations|decisions")
    model = _MODEL[table]
    async with app_session() as s:
        rows = (await s.execute(
            select(model).where(model.status.in_(("done", "dismissed")))
            .order_by(model.created_at.desc()).limit(40)
        )).scalars().all()
    return {"signals": [
        {"id": r.id, "label": signal_label(table, r), "status": r.status}
        for r in rows
    ]}


@router.post("")
async def create_rule(
    body: dict = Body(...),
    user: TokenPayload = Depends(require_project_access),
):
    table = str(body.get("signal_table") or "").strip().lower()
    if table not in _TABLES:
        raise HTTPException(422, "signal_table must be alerts|recommendations|decisions")
    reason = str(body.get("reason") or "").strip() or None
    condition = body.get("condition")
    if isinstance(condition, str):
        try:
            condition = json.loads(condition)
        except ValueError:
            condition = None
    if not isinstance(condition, dict):
        condition = None
    pattern = str(body.get("subject_pattern") or "").strip() or None
    severity_max = str(body.get("severity_max") or "").strip() or None
    # The fourth duration option ("while a condition holds") has no date of its
    # own: the condition IS the window. ``expires_days`` bounds the rule itself,
    # because a conditional silence should not outlive the environment it was
    # written for.
    forever = bool(body.get("forever")) or condition is not None
    muted_until = None
    if not forever:
        try:
            days = int(body.get("until_days") or 30)
        except (TypeError, ValueError):
            days = 30
        muted_until = now_utc() + timedelta(days=max(1, days))
    expires_at = None
    if body.get("expires_days"):
        try:
            expires_at = now_utc() + timedelta(days=max(1, int(body["expires_days"])))
        except (TypeError, ValueError):
            expires_at = None

    key = label = None
    source_id = None
    sid = body.get("from_signal_id")
    if sid is not None:
        model = _MODEL[table]
        async with app_session() as s:
            try:
                obj = (await s.execute(select(model).where(model.id == int(sid)))).scalar_one_or_none()
            except (TypeError, ValueError):
                obj = None
            if obj is None:
                raise HTTPException(404, f"{table} {sid} not found")
            key, label, source_id = suppression_key(table, obj), signal_label(table, obj), int(sid)
    elif body.get("dedupe_key"):
        dk = str(body["dedupe_key"]).strip()
        # v7.68.0 — normalize identically to the gate (`_norm`), else a raw
        # dedupe_key with whitespace runs / >160 chars would never match.
        from app.signal_suppress import _TABLE_TO_PREFIX, _norm
        key, label = f"{_TABLE_TO_PREFIX[table]}:{_norm(dk)}", dk
    elif pattern:
        # INVARIANT: exactly one of match_key / subject_pattern carries the
        # scope. A pattern rule keeps ``match_key`` empty so today's exact
        # identity semantics are untouched.
        key, label = "", f"señales que coincidan con «{pattern}»"
    else:
        raise HTTPException(
            422, "need from_signal_id, dedupe_key or subject_pattern")

    async with app_write() as s:
        rule = await create_suppression(
            s, table=table, key=key, label=label, muted_until=muted_until,
            reason=reason or "Silenciada por el operador (no repetir).",
            created_by=getattr(user, "email", None) or "operator",
            source_signal_id=source_id,
            subject_pattern=pattern, condition=condition,
            severity_max=severity_max, expires_at=expires_at,
        )
        await s.commit()
        await s.refresh(rule)
        return _to_dict(rule)


@router.post("/preview")
async def preview_rule(
    body: dict = Body(...),
    user: TokenPayload = Depends(require_project_access),
):
    """«Probar» — replay a candidate silence over the last N REAL signals and
    report which ones it would have affected. Reads only, cost 0, writes nothing.

    Same role ``test_automation`` plays for a standing order: you don't approve a
    condition by reading it, you approve it by seeing its effect.
    """
    table = str(body.get("signal_table") or "").strip().lower()
    if table not in _TABLES:
        raise HTTPException(422, "signal_table must be alerts|recommendations|decisions")
    condition = body.get("condition")
    if isinstance(condition, str):
        try:
            condition = json.loads(condition)
        except ValueError:
            condition = None
    from app.signal_conditions import preview
    return await preview({
        "signal_table": table,
        "match_key": str(body.get("match_key") or ""),
        "match_label": str(body.get("match_label") or ""),
        "subject_pattern": body.get("subject_pattern"),
        "condition": condition if isinstance(condition, dict) else None,
        "severity_max": body.get("severity_max"),
    }, body.get("last_n"))


@router.post("/{rule_id}/accept")
async def accept_rule(
    rule_id: int,
    body: dict = Body(default={}),
    user: TokenPayload = Depends(require_admin),
):
    """Accept a silence the EMPLOYEE proposed — ADMIN ONLY.

    Until this call the rule silences nothing; from here the deterministic gate
    honours it. Optionally bound it (``until_days``) instead of accepting it
    indefinitely. If a learning justified it, the effect is stamped so the row
    keeps pointing at where it came from.
    """
    async with app_write() as s:
        r = (await s.execute(
            select(SignalSuppression).where(SignalSuppression.id == rule_id)
        )).scalar_one_or_none()
        if r is None:
            raise HTTPException(404, f"suppression {rule_id} not found")
        r.review_state = "active"
        r.enabled = True
        if body.get("until_days"):
            try:
                r.muted_until = (now_utc() + timedelta(
                    days=max(1, int(body["until_days"])))).replace(tzinfo=None)
            except (TypeError, ValueError):
                pass
        r.updated_at = now_utc().replace(tzinfo=None)
        await s.commit()
        await s.refresh(r)
        payload = _to_dict(r)
    try:
        from app import learning_effects_service as lfx
        origins = await lfx.origin_for(lfx.TARGET_SUPPRESSION, [rule_id])
        found = origins.get(str(rule_id))
        if found and found.get("effect_id"):
            await lfx.approve(
                int(found["effect_id"]),
                approved_by=getattr(user, "email", None) or "operator",
                approved_where=str(body.get("where") or "queue"),
            )
    except Exception:  # pragma: no cover — the trail is best-effort
        logger.debug("effect approve on accept failed", exc_info=True)
    return payload


@router.post("/{rule_id}/reject")
async def reject_rule(
    rule_id: int,
    body: dict = Body(default={}),
    user: TokenPayload = Depends(require_admin),
):
    """Reject a proposed silence — ADMIN ONLY. Deletes the row and, when a
    learning justified it, marks it so the employee does not ask again next
    week."""
    try:
        from app import learning_effects_service as lfx
        origins = await lfx.origin_for(lfx.TARGET_SUPPRESSION, [rule_id])
        found = origins.get(str(rule_id))
        if found and found.get("effect_id"):
            # revert() deletes the destination row too — one path for "undo".
            await lfx.revert(
                int(found["effect_id"]),
                reverted_by=getattr(user, "email", None) or "operator",
                reason=(str(body.get("reason") or "").strip() or "Propuesta rechazada"),
            )
            return {"deleted": True, "reverted_effect": found["effect_id"]}
    except Exception:  # pragma: no cover
        logger.debug("effect revert on reject failed", exc_info=True)
    async with app_write() as s:
        res = await s.execute(
            sql_delete(SignalSuppression).where(SignalSuppression.id == rule_id))
        await s.commit()
    return {"deleted": bool(res.rowcount)}


@router.get("/silences")
async def silence_log(
    suppression_id: int | None = Query(default=None),
    since: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: TokenPayload = Depends(require_project_access),
):
    """The "why didn't it warn me?" feed — one row per TIME a silence acted.

    Distinct from the suppression's counter, and both are needed: the counter
    gives the permanent number, this gives the recent narrative (what exactly was
    not created, when, and which condition matched). It is how you audit by
    EFFECTS instead of by reading rules.
    """
    from datetime import datetime as _dt
    stmt = select(SignalSilenceLog, SignalSuppression).join(
        SignalSuppression, SignalSuppression.id == SignalSilenceLog.suppression_id,
        isouter=True,
    ).order_by(SignalSilenceLog.occurred_at.desc()).limit(limit)
    if suppression_id:
        stmt = stmt.where(SignalSilenceLog.suppression_id == int(suppression_id))
    if (since or "").strip():
        try:
            stmt = stmt.where(SignalSilenceLog.occurred_at >= _dt.fromisoformat(
                since.strip().replace("Z", "")))
        except ValueError:
            pass
    async with app_session() as s:
        rows = (await s.execute(stmt)).all()
    items = [{
        "id": log.id,
        "suppression_id": log.suppression_id,
        "silence": (getattr(rule, "match_label", None)
                    or getattr(rule, "match_key", None)
                    or getattr(rule, "subject_pattern", None)),
        "signal_table": log.signal_table,
        "label": log.label,
        "severity": log.severity,
        "effect_applied": log.effect_applied,
        "reason": log.reason,
        "occurred_at": iso_utc(log.occurred_at),
        "run_id": log.run_id,
        "silence_still_active": (is_active(rule) if rule is not None else False),
    } for log, rule in rows]
    return {"items": await _with_origin_key(items)}


async def _with_origin_key(items: list[dict]) -> list[dict]:
    """``learned_from`` for the silence-log feed, keyed by ``suppression_id``."""
    if not items:
        return items
    try:
        from app import learning_effects_service as lfx
        origins = await lfx.origin_for(
            lfx.TARGET_SUPPRESSION,
            [i.get("suppression_id") for i in items],
        )
    except Exception:  # pragma: no cover
        return items
    for it in items:
        found = origins.get(str(it.get("suppression_id")))
        if found:
            it["learned_from"] = found
    return items


@router.patch("/{rule_id}")
async def update_rule(
    rule_id: int,
    body: dict = Body(...),
    user: TokenPayload = Depends(require_project_access),
):
    async with app_write() as s:
        r = (await s.execute(
            select(SignalSuppression).where(SignalSuppression.id == rule_id)
        )).scalar_one_or_none()
        if r is None:
            raise HTTPException(404, f"suppression {rule_id} not found")
        # SECURITY: a member only touches the rules THEY created. Without this they could
        # edit (or un-silence for everyone) any colleague's rules, while its MCP twin
        # `unsuppress_signal` requires admin. An admin can touch any of them.
        from app.users_directory import is_admin
        if not await is_admin(user.email) and \
                (r.created_by or "").strip().lower() != (user.email or "").strip().lower():
            raise HTTPException(403, "Solo puedes modificar los silencios que creaste tú.")
        if "enabled" in body:
            r.enabled = bool(body["enabled"])
        if "reason" in body:
            r.reason = (str(body["reason"]).strip() or None)
        if body.get("forever"):
            r.muted_until = None
        elif "until_days" in body and body["until_days"] is not None:
            try:
                r.muted_until = (now_utc() + timedelta(days=max(1, int(body["until_days"])))).replace(tzinfo=None)
            except (TypeError, ValueError):
                pass
        r.updated_at = now_utc().replace(tzinfo=None)
        await s.commit()
        await s.refresh(r)
        return _to_dict(r)


@router.delete("/{rule_id}")
async def delete_rule(
    rule_id: int,
    user: TokenPayload = Depends(require_project_access),
):
    async with app_write() as s:
        r = (await s.execute(
            select(SignalSuppression).where(SignalSuppression.id == rule_id)
        )).scalar_one_or_none()
        if r is None:
            return {"deleted": False}
        # SECURITY: deleting a rule un-silences it for EVERYONE. A member only deletes their
        # own; an admin, any of them (same line as `unsuppress_signal`, which is admin).
        from app.users_directory import is_admin
        if not await is_admin(user.email) and \
                (r.created_by or "").strip().lower() != (user.email or "").strip().lower():
            raise HTTPException(403, "Solo puedes borrar los silencios que creaste tú.")
        res = await s.execute(sql_delete(SignalSuppression).where(SignalSuppression.id == rule_id))
        await s.commit()
    return {"deleted": bool(res.rowcount)}
