"""v7.62.0 — CRUD for SignalNotifyRule (operator notification rules on the
alerts / recommendations / decisions screens).

A rule = "when a NEW <signal> meets these intensity thresholds, message this
directory user via this channel". Writes are admin-only (only a real human sets
recipients — same governance as channels/automations); reads are project-scoped.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select, delete as sql_delete

from app.app_database import app_session, app_write
from app.app_models.notify_rules import SignalNotifyRule
from app.app_models.channels import Channel
from app.app_models.users import AppUser
from app.auth import TokenPayload, require_admin, require_project_access
from app.timeutil import iso_utc, now_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent/notify-rules", tags=["notify-rules"])

_TABLES = {"alerts", "recommendations", "decisions"}
_AXES = ("impact_min", "urgency_min", "confidence_min")


def _clean_conditions(raw) -> dict:
    """Keep only the known threshold axes (+ match mode). Values are lower-cased
    band names; anything else is dropped so a rule can never carry junk."""
    src = raw if isinstance(raw, dict) else {}
    out: dict = {}
    for axis in _AXES:
        v = str(src.get(axis) or "").strip().lower()
        if v:
            out[axis] = v
    match = str(src.get("match") or "all").strip().lower()
    out["match"] = "any" if match == "any" else "all"
    return out


async def _to_dict(rule: SignalNotifyRule, chan_names: dict, user_names: dict) -> dict:
    try:
        cond = json.loads(rule.conditions_json or "{}")
    except Exception:
        cond = {}
    return {
        "id": rule.id,
        "signal_table": rule.signal_table,
        "name": rule.name,
        "conditions": cond if isinstance(cond, dict) else {},
        "channel_id": rule.channel_id,
        "channel_name": chan_names.get(rule.channel_id),
        "recipient_user": rule.recipient_user,
        "recipient_name": user_names.get((rule.recipient_user or "").lower()),
        "enabled": bool(rule.enabled),
        "last_fired_at": iso_utc(rule.last_fired_at),
        "updated_at": iso_utc(rule.updated_at),
    }


@router.get("/options")
async def options(user: TokenPayload = Depends(require_project_access)):
    """Channels + directory users to populate the rule form (name pickers, never
    raw ids)."""
    async with app_session() as s:
        channels = (await s.execute(select(Channel))).scalars().all()
        users = (await s.execute(select(AppUser))).scalars().all()
    return {
        "channels": [
            {"id": c.id, "label": c.display_name or c.mailbox_address, "kind": c.kind,
             "status": c.status}
            for c in channels
        ],
        "users": [
            {"email": u.email, "display_name": u.display_name or u.email}
            for u in users if u.email
        ],
    }


@router.get("")
async def list_rules(
    table: str | None = Query(default=None),
    user: TokenPayload = Depends(require_project_access),
):
    async with app_session() as s:
        stmt = select(SignalNotifyRule).order_by(
            SignalNotifyRule.signal_table.asc(), SignalNotifyRule.id.asc())
        if table in _TABLES:
            stmt = stmt.where(SignalNotifyRule.signal_table == table)
        rows = (await s.execute(stmt)).scalars().all()
        chan_names = {c.id: (c.display_name or c.mailbox_address)
                      for c in (await s.execute(select(Channel))).scalars().all()}
        user_names = {(u.email or "").lower(): (u.display_name or u.email)
                      for u in (await s.execute(select(AppUser))).scalars().all()}
    return {"items": [await _to_dict(r, chan_names, user_names) for r in rows]}


@router.post("")
async def create_rule(
    body: dict = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    table = str(body.get("signal_table") or "").strip().lower()
    if table not in _TABLES:
        raise HTTPException(422, "signal_table must be alerts|recommendations|decisions")
    channel_id = body.get("channel_id")
    recipient = str(body.get("recipient_user") or "").strip().lower()
    if not channel_id:
        raise HTTPException(422, "channel_id is required")
    if not recipient:
        raise HTTPException(422, "recipient_user is required")
    cond = _clean_conditions(body.get("conditions"))
    if not any(cond.get(a) for a in _AXES):
        raise HTTPException(422, "at least one threshold (impact/urgency/confidence) is required")
    now = now_utc().replace(tzinfo=None)
    row = SignalNotifyRule(
        signal_table=table,
        name=(str(body.get("name") or "").strip()[:200] or None),
        conditions_json=json.dumps(cond),
        channel_id=int(channel_id),
        recipient_user=recipient,
        enabled=bool(body.get("enabled", True)),
        created_by=getattr(user, "email", None),
        created_at=now, updated_at=now,
    )
    async with app_write() as s:
        s.add(row)
        await s.commit()
        await s.refresh(row)
        chan_names = {c.id: (c.display_name or c.mailbox_address)
                      for c in (await s.execute(select(Channel))).scalars().all()}
        user_names = {(u.email or "").lower(): (u.display_name or u.email)
                      for u in (await s.execute(select(AppUser))).scalars().all()}
        return await _to_dict(row, chan_names, user_names)


@router.patch("/{rule_id}")
async def update_rule(
    rule_id: int,
    body: dict = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    async with app_write() as s:
        row = (await s.execute(
            select(SignalNotifyRule).where(SignalNotifyRule.id == rule_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, f"rule {rule_id} not found")
        if "name" in body:
            row.name = (str(body["name"]).strip()[:200] or None)
        if "conditions" in body:
            row.conditions_json = json.dumps(_clean_conditions(body["conditions"]))
        if "channel_id" in body and body["channel_id"]:
            row.channel_id = int(body["channel_id"])
        if "recipient_user" in body and str(body["recipient_user"]).strip():
            row.recipient_user = str(body["recipient_user"]).strip().lower()
        if "enabled" in body:
            row.enabled = bool(body["enabled"])
        row.updated_at = now_utc().replace(tzinfo=None)
        await s.commit()
        await s.refresh(row)
        chan_names = {c.id: (c.display_name or c.mailbox_address)
                      for c in (await s.execute(select(Channel))).scalars().all()}
        user_names = {(u.email or "").lower(): (u.display_name or u.email)
                      for u in (await s.execute(select(AppUser))).scalars().all()}
        return await _to_dict(row, chan_names, user_names)


@router.delete("/{rule_id}")
async def delete_rule(
    rule_id: int,
    user: TokenPayload = Depends(require_admin),
):
    async with app_write() as s:
        res = await s.execute(sql_delete(SignalNotifyRule).where(SignalNotifyRule.id == rule_id))
        await s.commit()
    return {"deleted": bool(res.rowcount)}
