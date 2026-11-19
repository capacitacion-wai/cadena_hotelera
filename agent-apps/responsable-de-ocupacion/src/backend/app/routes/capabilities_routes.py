"""v7.65.0 — Capabilities API for Administración → "Capacidades".

``GET /api/agent/capabilities`` returns the rigorous, real-config snapshot of what
the employee can do (reused runtime resolvers — see ``app.capabilities``).
``POST /api/agent/capabilities/ask`` lets the agent NARRATE that deterministic
snapshot and translate the operator's intent into concrete settings (it never
decides the facts, and never applies changes). Admin-only: it exposes the full
governance posture.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends

from app import capabilities as caps
from app.auth import TokenPayload, require_admin
from app.timeutil import iso_utc, now_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent/capabilities", tags=["capabilities"])


@router.get("")
async def get_capabilities(user: TokenPayload = Depends(require_admin)):
    snap = await caps.build_snapshot()
    snap["generated_at"] = iso_utc(now_utc())
    return snap


@router.post("/ask")
async def ask_capabilities(
    body: dict = Body(default_factory=dict),
    user: TokenPayload = Depends(require_admin),
):
    """Plain-language explanation / intent translation over the deterministic
    snapshot. The employee narrates the facts (from the snapshot) and suggests the
    concrete setting to change — it does NOT apply anything."""
    question = str(body.get("question") or "").strip()
    return await caps.explain(question)
