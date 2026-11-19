"""Liveness / readiness endpoints.

G-3 (P3): K8s probes need two distinct semantics. Without the
split, a transient SQLite WAL checkpoint that briefly locks the DB
fails the only health endpoint and K8s kills the pod.

* ``/health/live`` — am I alive? Cheap, never touches the DB. K8s
  livenessProbe target. Failure → restart.
* ``/health/ready`` — am I ready to serve? Pings the local SQLite
  AND optionally the agent pod. K8s readinessProbe target.
  Failure → temporarily remove from Service endpoints, do NOT
  restart.

The legacy ``/health`` endpoint stays as an alias of ``/ready`` for
back-compat with deployers / operators that scripted against it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app.app_database import app_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health/live")
async def liveness() -> dict:
    """Cheap liveness probe — only proves the process can answer
    HTTP. Never touches the DB; never blocks on external services."""
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness() -> dict:
    """Readiness probe — proves the local store is reachable. The
    agent pod is intentionally NOT pinged: the app must stay
    serving the operator UI even when the agent is rebooting (the
    Operations page surfaces the agent state separately)."""
    try:
        async with app_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness probe failed: %s", exc)
        raise HTTPException(status_code=503, detail="local sqlite unreachable")
    return {"status": "ready"}


@router.get("/health")
async def health() -> dict:
    """Back-compat alias of ``/health/ready``. Operators who
    scripted against the legacy single endpoint don't have to
    change anything."""
    return await readiness()
