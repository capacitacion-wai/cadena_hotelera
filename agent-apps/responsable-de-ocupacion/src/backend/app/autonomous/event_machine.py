# src/backend/app/autonomous/event_machine.py
from datetime import datetime, timezone
from sqlalchemy import text

# ``{extra_where}`` lets the reserved-inbound worker (v7.25.0 concurrency
# pool) claim ONLY external_message events — a long watcher/cron run can
# then never occupy every slot while a person waits for a channel reply.
_CLAIM_SQL_TEMPLATE = """
    UPDATE events
       SET status = 'CLAIMED',
           attempts = attempts + 1,
           last_attempt_at = :now,
           heartbeat_at = :now
     WHERE id = (
         SELECT id FROM events
          WHERE status IN ('PENDING', 'RETRY_SCHEDULED')
            AND (next_attempt_at IS NULL OR next_attempt_at <= :now)
            {extra_where}
          -- PRIORITY (v7.29.0): inbound channel message (a person waiting) = 0,
          -- normal work (watcher/cron/scheduled/automation/approval) = 1,
          -- memory maintenance = 2 (LOWEST — hygiene must yield to real work).
          -- Strict FIFO (detected_at) breaks ties within each class.
          ORDER BY (CASE
                      WHEN type = 'external_message' THEN 0
                      WHEN type = 'memory_maintenance' THEN 2
                      ELSE 1 END) ASC,
                   detected_at ASC
          LIMIT 1
     )
    RETURNING id, fingerprint, trace_id, type, source, payload_json, attempts, max_attempts;
    """

CLAIM_SQL = text(_CLAIM_SQL_TEMPLATE.format(extra_where=""))
CLAIM_SQL_INBOUND_ONLY = text(
    _CLAIM_SQL_TEMPLATE.format(extra_where="AND type = 'external_message'")
)


async def claim_next_event(session=None, *, inbound_only: bool = False):
    """Atomically claim the next pending event.

    IS2 (revised B7 + IS2.b): SQLAlchemy 2.x's SQLite dialect rejects
    ``execution_options(isolation_level="IMMEDIATE")`` with::

        ArgumentError: Invalid value 'IMMEDIATE' for isolation_level.
        Valid isolation levels for 'sqlite' are READ UNCOMMITTED,
        SERIALIZABLE, AUTOCOMMIT.

    That used to crash the executor task on its very first iteration
    — observable as "events stay PENDING forever, attempts=0,
    agent_runs only has chat rows" (see the demo Retail-Ecommerce
    Pulse Sentinel post-mortem). The previous implementation
    therefore left the executor silently dead in any tenant that
    pinned SQLAlchemy ≥ 2.x — which is everyone, since the canonical
    Dockerfile pins ``SQLAlchemy[asyncio]==2.0.35``.

    The correct way to upgrade the transaction to ``BEGIN IMMEDIATE``
    on SQLite is to drop down to the DBAPI level and emit the
    literal ``BEGIN IMMEDIATE`` before SQLAlchemy's auto-begin
    triggers a regular ``BEGIN``. We try that path defensively; if
    SQLAlchemy has already auto-begun (it does, on some versions,
    even before any execute), the raw BEGIN raises
    ``OperationalError: cannot start a transaction within a
    transaction`` which we swallow. The CLAIM_SQL itself is a single
    ``UPDATE WHERE id = (SELECT ... LIMIT 1) RETURNING`` statement
    that is atomic in SQLite even without IMMEDIATE — two replicas
    racing on the same row both submit the UPDATE; SQLite's
    write-lock serializes them and the second one finds no row to
    update, so RETURNING is empty.

    The legacy ``session`` argument is accepted for source
    compatibility with v6.x callers but ignored — we always use a
    fresh engine connection.

    INC-VIG-59: this claim is the LOUDEST writer in the app (8 workers, every
    2 s, forever) and it used to write outside the process write queue. Its churn
    was what starved the writers that were outside too — the channel poller's
    telemetry UPDATE and, worst of all, the operator's ``triggers_active`` toggle,
    which failed with 'database is locked' precisely while a round was running.
    ``app_write_conn`` puts it in the SAME fair queue as every other writer, which
    costs a claim nothing (it is one indexed UPDATE) and is what makes the kill
    switch reachable mid-round.
    """
    now = datetime.now(timezone.utc)
    from app.app_database import app_write_conn
    async with app_write_conn() as conn:
        sql = CLAIM_SQL_INBOUND_ONLY if inbound_only else CLAIM_SQL
        result = await conn.execute(sql, {"now": now})
        row = result.mappings().first()
        await conn.commit()
    return dict(row) if row else None


from sqlalchemy import update
from app.app_models.runtime import Event
from datetime import datetime, timedelta, timezone

async def mark_in_progress(session, event_id: int):
    await session.execute(
        update(Event).where(Event.id == event_id).values(
            status="IN_PROGRESS", heartbeat_at=datetime.now(timezone.utc),
        )
    )

# Only an actively-processing event may transition to a terminal/retry
# state from the executor. The ``status`` guard makes an operator CANCEL
# (which sets FAILED while the run is still IN_PROGRESS) sticky: a late
# completion or a post-timeout retry can no longer resurrect a cancelled
# event (v6.36.6).
_ACTIVE = ("IN_PROGRESS", "CLAIMED")

async def mark_completed(session, event_id: int):
    now = datetime.now(timezone.utc)
    await session.execute(
        update(Event).where(Event.id == event_id)
        .where(Event.status.in_(_ACTIVE)).values(
            status="COMPLETED", processed_at=now, heartbeat_at=now,
        )
    )

async def defer_event(session, event_id: int, claimed_attempts: int, delay_sec: float = 3.0):
    """Hand a JUST-CLAIMED event back WITHOUT burning an attempt (v7.25.0).

    Used by the executor's per-conversation guard: when the event's session
    scope (same person, same channel) already has a run in flight, the event
    is re-scheduled a few seconds out so messages of ONE conversation stay
    strictly ordered while other conversations keep running in parallel.
    The claim incremented ``attempts``; this rolls it back so a deferral is
    never confused with a failed try."""
    await session.execute(
        update(Event).where(Event.id == event_id)
        .where(Event.status.in_(_ACTIVE)).values(
            status="RETRY_SCHEDULED",
            attempts=max(0, int(claimed_attempts) - 1),
            next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=delay_sec),
            heartbeat_at=None,
        )
    )


async def schedule_retry(session, event_id: int, attempts: int, backoff_seconds: list[int]):
    delay = backoff_seconds[min(attempts - 1, len(backoff_seconds) - 1)]
    next_ts = datetime.now(timezone.utc) + timedelta(seconds=delay)
    await session.execute(
        update(Event).where(Event.id == event_id)
        .where(Event.status.in_(_ACTIVE)).values(
            status="RETRY_SCHEDULED", next_attempt_at=next_ts,
        )
    )

async def mark_failed(session, event_id: int, error: str):
    await session.execute(
        update(Event).where(Event.id == event_id)
        .where(Event.status.in_(_ACTIVE)).values(
            status="FAILED", last_error=error[:4000],
            processed_at=datetime.now(timezone.utc),
        )
    )


async def requeue(session, event_id: int):
    await session.execute(
        update(Event)
        .where(Event.id == event_id)
        .where(Event.status.in_(["FAILED", "COMPLETED"]))
        .values(status="PENDING", last_error=None, next_attempt_at=None)
    )
