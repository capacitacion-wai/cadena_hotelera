import json
from datetime import datetime, timezone
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import asc, desc, select, update, func, or_
from sqlalchemy.orm import selectinload

from app.api_errors import api_error
from app.app_database import app_session, app_write
from app.app_models.runtime import Event, AgentRun, EventAudit
from app.auth import require_admin, require_project_access, TokenPayload
from app.pagination import paginated

# The event log is an Administration (Audit) surface (every detected signal
# across all users). Gate the whole router with require_admin; only the admin
# Events/EventDetail pages consume it.
router = APIRouter(prefix="/api/events", tags=["events"],
                   dependencies=[Depends(require_admin)])


# R-TZ-2: the ORM column defaults call ``datetime.utcnow`` which
# produces naive datetimes — by the time we read them back here
# their tzinfo is gone. Serialising them with bare ``.isoformat()``
# yields ``"2026-05-13T17:11:50"`` and the SPA's ``new Date()``
# helpfully interprets that as the BROWSER's local timezone,
# which is exactly the bug the user reported (Detectado/Heartbeat
# shown in UTC while Intento #1 — which we stamp ourselves with
# ``datetime.now(timezone.utc)`` — was correct). The fix is to
# tag every outbound datetime as UTC-aware before isoformat() so
# the wire payload always carries an explicit offset.
def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


@router.get("")
async def list_events(
    user: TokenPayload = Depends(require_project_access),
    # NB: use ``Annotated[..., Query()] = <default>`` (not ``= Query(default=…)``)
    # so the real default is a plain value — the route is ALSO called DIRECTLY from
    # the contract tests, where a bare ``Query(...)`` default would leak the Query
    # sentinel object into the query (``status.in_(Query(None))`` → ArgumentError).
    status: Annotated[list[str] | None, Query()] = None,
    type_: Annotated[str | None, Query(alias="type")] = None,
    source: str | None = None,
    q: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    # Server-side sort: the grid sends ``sort``/``order``; whitelist the
    # sortable columns (anything else falls back to newest-first) so a column
    # header click actually reorders instead of being a dead affordance.
    _SORTABLE = {
        "detected_at": Event.detected_at,
        "status": Event.status,
        "type": Event.type,
        "source": Event.source,
    }
    sort_col = _SORTABLE.get(sort or "", Event.detected_at)
    sort_dir = asc if (order or "desc").lower() == "asc" else desc
    async with app_session() as s:
        base = select(Event)
        if status: base = base.where(Event.status.in_(status))
        if type_:  base = base.where(Event.type == type_)
        if source: base = base.where(Event.source == source)
        if q:
            base = base.where(or_(
                Event.fingerprint.contains(q),
                Event.last_error.contains(q),
            ))
        total = (await s.execute(
            select(func.count()).select_from(base.subquery())
        )).scalar()
        rows = (await s.execute(
            base.order_by(sort_dir(sort_col)).limit(limit).offset(offset)
        )).scalars().all()
        return paginated(total or 0, limit, offset, [_summary(r) for r in rows])


@router.get("/stats")
async def stats(user: TokenPayload = Depends(require_project_access)):
    async with app_session() as s:
        rows = (await s.execute(
            select(Event.status, func.count()).group_by(Event.status)
        )).all()
    return {row[0]: int(row[1]) for row in rows}


@router.get("/catalog")
async def catalog(user: TokenPayload = Depends(require_project_access)):
    """Distinct ``type`` and ``source`` values so the events filter can offer
    real SELECTORS (not free-text boxes the operator has to guess). Declared
    BEFORE ``/{event_id}`` so ``…/catalog`` never resolves as an id."""
    async with app_session() as s:
        types = [r[0] for r in (await s.execute(
            select(Event.type).where(Event.type.isnot(None)).distinct().order_by(Event.type)
        )).all() if r[0]]
        sources = [r[0] for r in (await s.execute(
            select(Event.source).where(Event.source.isnot(None)).distinct().order_by(Event.source)
        )).all() if r[0]]
    return {"types": types, "sources": sources}


@router.get("/{event_id}")
async def event_detail(
    event_id: int,
    user: TokenPayload = Depends(require_project_access),
):
    async with app_session() as s:
        event = (await s.execute(
            select(Event)
            .options(selectinload(Event.runs))
            .where(Event.id == event_id)
        )).scalar_one_or_none()
        if event is None:
            raise api_error(404, "not_found", "Event not found.")

        audit = (await s.execute(
            select(EventAudit)
            .where(EventAudit.event_id == event_id)
            .order_by(desc(EventAudit.at))
        )).scalars().all()

    return {
        **_summary(event),
        "payload": _safe_json(event.payload_json),
        "key_fields": _extract_key_fields(event.payload_json),
        "heartbeat_at": _iso_utc(event.heartbeat_at),
        "last_attempt_at": _iso_utc(event.last_attempt_at),
        "timeline": _build_timeline(event),
        "runs": [_run_row(r) for r in sorted(event.runs, key=lambda r: r.id)],
        "audit": [
            {
                "at":     _iso_utc(a.at),
                "actor":  a.actor_email,
                "action": a.action,
                "from_status": a.from_status,
                "to_status":   a.to_status,
                "note":   a.note,
            }
            for a in audit
        ],
    }


@router.post("/{event_id}/reprocess")
async def reprocess(
    event_id: int,
    user: TokenPayload = Depends(require_project_access),
):
    async with app_write() as s:
        event = (await s.execute(
            select(Event).where(Event.id == event_id)
        )).scalar_one_or_none()
        if event is None:
            raise api_error(404, "not_found", "Event not found.")
        before = event.status

        # Idempotency guard for external channels: an external_message
        # whose channel_message already reached a TERMINAL state
        # (replied / skipped) must never be reprocessed — it would call
        # the agent again and could RE-SEND the reply to the user.
        if event.type == "external_message":
            from app.app_models.channels import ChannelMessage

            def _blocks_reprocess(status: str | None, provider_id: str | None) -> bool:
                pid = (provider_id or "").strip()
                st = (status or "").strip().lower()
                if st in ("replied", "skipped") or bool(pid):
                    return True
                if st == "processing":
                    return True
                return False

            blocked = (await s.execute(
                select(ChannelMessage.status, ChannelMessage.reply_provider_message_id)
                .where(ChannelMessage.event_id == event_id)
                .limit(1)
            )).first()
            if blocked is not None and _blocks_reprocess(blocked[0], blocked[1]):
                status, provider_id = blocked[0], blocked[1]
                reason = (
                    f"channel message already {status}"
                    if status in ("replied", "skipped")
                    else "channel message already has an outbound reply id"
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"{reason}; reprocessing would re-send the reply",
                )
            payload = json.loads(event.payload_json or "{}")
            cid = payload.get("channel_id")
            pid = (payload.get("provider_message_id") or "").strip()
            if cid and pid:
                by_key = (await s.execute(
                    select(ChannelMessage.status, ChannelMessage.reply_provider_message_id)
                    .where(
                        ChannelMessage.channel_id == int(cid),
                        ChannelMessage.provider_message_id == pid,
                    )
                    .limit(1)
                )).first()
                if by_key is not None and _blocks_reprocess(by_key[0], by_key[1]):
                    status, provider_id = by_key[0], by_key[1]
                    reason = (
                        f"channel message already {status}"
                        if status in ("replied", "skipped")
                        else "channel message already has an outbound reply id"
                    )
                    raise HTTPException(
                        status_code=409,
                        detail=f"{reason}; reprocessing would re-send the reply",
                    )

        result = await s.execute(
            update(Event)
            .where(Event.id == event_id)
            .where(Event.status.in_(["FAILED", "COMPLETED"]))
            .values(
                status="PENDING", last_error=None,
                next_attempt_at=None, heartbeat_at=None,
            )
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=409, detail="event not in terminal state")
        s.add(EventAudit(
            event_id=event_id, actor_email=user.email, action="reprocess",
            from_status=before, to_status="PENDING",
            at=datetime.now(timezone.utc),
        ))
        await s.commit()
    return {"ok": True}


@router.post("/{event_id}/cancel")
async def cancel(
    event_id: int,
    user: TokenPayload = Depends(require_project_access),
):
    # `app_write`: THIS WRITES (cancelling an event / a run is the STOP
    # SWITCH). With `app_session` the commit takes the SQLite lock without
    # queueing, and the `BEGIN IMMEDIATE` of a writer that DID queue waits
    # until busy_timeout (15 s) holding the slot: everyone behind measures those 15 s.
    # The operator's brake being one of the ones that skipped the queue is
    # exactly what INC-VIG-59 came to prevent.
    async with app_write() as s:
        before = (await s.execute(
            select(Event.status).where(Event.id == event_id)
        )).scalar_one_or_none()
        if before is None:
            raise api_error(404, "not_found", "Event not found.")
        # Also allow cancelling an IN_PROGRESS / CLAIMED run — the operator
        # kill switch for a stuck run (e.g. a job looping past the gateway
        # timeout). The event_machine ``_ACTIVE`` guards make this FAILED
        # state sticky so a late completion/retry cannot resurrect it. We
        # clear heartbeat/next_attempt so neither the stale-recovery nor the
        # backoff scheduler picks it up again (v6.36.6).
        result = await s.execute(
            update(Event)
            .where(Event.id == event_id)
            .where(Event.status.in_(
                ["PENDING", "RETRY_SCHEDULED", "IN_PROGRESS", "CLAIMED"]
            ))
            .values(
                status="FAILED", last_error="cancelled by user",
                heartbeat_at=None, next_attempt_at=None,
                processed_at=datetime.now(timezone.utc),
            )
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=409, detail="event already done")
        s.add(EventAudit(
            event_id=event_id, actor_email=user.email, action="cancel",
            from_status=before, to_status="FAILED",
            at=datetime.now(timezone.utc), note="cancelled by user",
        ))
        # For a stuck IN_PROGRESS/CLAIMED run, also abort the live OpenCode
        # session so the kill switch frees compute immediately instead of
        # letting the runaway generation churn until the gateway timeout.
        oc_session_id = None
        if before in ("IN_PROGRESS", "CLAIMED"):
            oc_session_id = (await s.execute(
                select(AgentRun.opencode_session_id)
                .where(AgentRun.event_id == event_id)
                .where(AgentRun.opencode_session_id.is_not(None))
                .order_by(desc(AgentRun.id))
                .limit(1)
            )).scalar_one_or_none()
        await s.commit()
    if oc_session_id:
        try:
            from app.agent_proxy import get_agent_proxy
            await get_agent_proxy().abort_session(oc_session_id)
        except Exception:  # pragma: no cover — best-effort kill switch
            pass
    return {"ok": True}


def _safe_json(blob: str | None):
    if not blob:
        return None
    try:
        return json.loads(blob)
    except (TypeError, ValueError):
        return blob


def _extract_key_fields(blob: str | None) -> dict:
    """Surface a small subset of well-known fields so the UI can render
    a "campos clave" sub-block without scrolling the full payload."""
    payload = _safe_json(blob)
    if not isinstance(payload, dict):
        return {}
    keys = ("cursor_value", "delta", "table", "column", "value",
            "row_id", "watcher", "since")
    return {k: payload[k] for k in keys if k in payload}


def _build_timeline(e: Event) -> list[dict]:
    """Synthesise the state transitions in chronological order.

    The model does not store a full transition log per row to keep the
    write path cheap; the timeline is reconstructed from the few
    timestamps we DO store + the runs."""
    items = [{"kind": "detected", "at": _iso_utc(e.detected_at),
              "label": "Detectado", "by": e.source}]
    if e.last_attempt_at:
        items.append({
            "kind": "claimed",
            "at": _iso_utc(e.last_attempt_at),
            "label": f"Intento #{e.attempts}",
        })
    if e.heartbeat_at:
        items.append({
            "kind": "heartbeat",
            "at": _iso_utc(e.heartbeat_at),
            "label": "Heartbeat",
        })
    if e.next_attempt_at and e.status == "RETRY_SCHEDULED":
        items.append({
            "kind": "retry_scheduled",
            "at": _iso_utc(e.next_attempt_at),
            "label": "Próximo intento programado",
        })
    if e.processed_at:
        kind = "completed" if e.status == "COMPLETED" else "failed"
        items.append({
            "kind": kind,
            "at": _iso_utc(e.processed_at),
            "label": "Completado" if kind == "completed" else "Fallido",
            "error": e.last_error if kind == "failed" else None,
        })
    return items


def _summary(e: Event) -> dict:
    return {
        "id": e.id,
        "fingerprint": e.fingerprint,
        "type": e.type,
        "source": e.source,
        "status": e.status,
        "attempts": e.attempts,
        "max_attempts": e.max_attempts,
        "next_attempt_at": _iso_utc(e.next_attempt_at),
        "detected_at": _iso_utc(e.detected_at),
        "processed_at": _iso_utc(e.processed_at),
        "last_error": e.last_error,
    }


def _run_row(r: AgentRun) -> dict:
    return {
        "id":              r.id,
        "idempotency_key": r.idempotency_key,
        "status":          r.status,
        "mode":            r.mode,
        "started_at":      _iso_utc(r.started_at),
        "finished_at":     _iso_utc(r.finished_at),
        "duration_sec":    r.duration_sec,
        "tokens_in":       r.tokens_in,
        "tokens_out":      r.tokens_out,
        "model":           r.model,
        "error":           (r.error or "")[:280] if r.error else None,
    }
