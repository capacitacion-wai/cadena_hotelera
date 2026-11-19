"""Recover events stranded in CLAIMED/IN_PROGRESS after a hard restart.

Run once at FastAPI startup (see ``app.main.lifespan``). Without this
the watcher would skip rows that look "in flight" forever and the
agent would silently stop reacting to its triggers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.app_database import app_engine

log = logging.getLogger(__name__)

# Anything CLAIMED/IN_PROGRESS for more than this is considered stranded.
STALE_THRESHOLD = timedelta(minutes=5)

RECOVER_SQL = text(
    """
    UPDATE events
       SET status = 'PENDING',
           heartbeat_at = NULL,
           next_attempt_at = NULL
     WHERE status IN ('CLAIMED', 'IN_PROGRESS')
       AND (heartbeat_at IS NULL OR heartbeat_at < :cutoff)
    """
)

RECOVER_RUNS_SQL = text(
    """
    UPDATE agent_runs
       SET status = 'FAILED',
           error = COALESCE(error, 'Lost during restart'),
           finished_at = :now
     WHERE status IN ('PENDING', 'RUNNING', 'CLAIMED')
       AND started_at < :cutoff
    """
)


async def recover_stale_runs() -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - STALE_THRESHOLD
    async with app_engine.begin() as conn:
        ev = await conn.execute(RECOVER_SQL, {"cutoff": cutoff})
        rn = await conn.execute(RECOVER_RUNS_SQL, {"cutoff": cutoff, "now": now})
    log.info(
        "stale-recovery: events=%s agent_runs=%s",
        ev.rowcount if ev is not None else 0,
        rn.rowcount if rn is not None else 0,
    )


async def recover_stale_events() -> int:
    """Periodic variant, called from the executor loop. Only requeues
    EVENTS whose heartbeat went stale — the executor renews the
    heartbeat of the event it is processing, so a live long run is
    never touched. ``RECOVER_RUNS_SQL`` is deliberately NOT executed
    here: it keys off ``started_at`` and would fail genuine multi-minute
    runs; orphan run rows are cleaned at boot only."""
    cutoff = datetime.now(timezone.utc) - STALE_THRESHOLD
    async with app_engine.begin() as conn:
        ev = await conn.execute(RECOVER_SQL, {"cutoff": cutoff})
    n = ev.rowcount or 0
    if n:
        log.info("stale-recovery (periodic): requeued %d events", n)
    return n
