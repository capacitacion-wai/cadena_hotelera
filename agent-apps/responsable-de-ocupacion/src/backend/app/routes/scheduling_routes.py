"""Calendar + Encargos (delegated tasks) API.

ONE model — ``ScheduledItem`` — backs three product surfaces:

* **Calendar** (``/api/calendar``): every time-anchored item (the
  employee's own scheduled work, follow-ups, and tasks/reminders it put
  on a person's calendar) plus operator-created events.
* **Encargos** (``/api/agent/tasks``): tasks a human delegates to the AI
  employee. Creating one with no future ``due_at`` fires it NOW (a
  ``DELEGATED_TASK`` event → autonomous run); with a future ``due_at`` it
  is just scheduled and the watcher fires it when due.

Time policy: all datetimes are stored UTC-naive and (de)serialised at the
edge with :mod:`app.timeutil` so the SPA always sends/receives explicit
ISO-8601 with offset and renders in the business timezone.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# How many weekly occurrences to materialise for a recurring entry.
# Each occurrence is a real ScheduledItem (so the watcher can fire agent
# items and the month grid can render/strike them independently).
_WEEKLY_HORIZON = 26

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.app_database import app_session, app_write
from app.app_models.business import ScheduledItem
from app.app_models.runtime import Event
# 14.18.0 — WHAT A MEMBER SEES. A member USES the employee (chat, manual, its own encargos);
# it does not operate it. These screens expose everyone's work (1,125 conversations of the
# whole team on the Panel of a member, 04/10/2026), so the door is HERE and the menu only
# reflects it.
from app.auth import TokenPayload, require_admin, require_project_access
from app.timeutil import iso_utc, now_utc, parse_to_utc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["scheduling"])

DELEGATED_TASK_EVENT_TYPE = "DELEGATED_TASK"

_KINDS = ("task", "followup", "event", "reminder")
_ASSIGNEES = ("agent", "user")
_PRIORITIES = ("low", "medium", "high", "urgent")
_STATUSES = ("pending", "scheduled", "in_progress", "done", "cancelled", "overdue", "failed")


def _json_obj(raw) -> dict:
    """Parse a metadata JSON string/dict into a dict. Never raises."""
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


async def settle_source_on_complete(session, item: ScheduledItem) -> None:
    """Close the loop when an encargo (ScheduledItem) reaches a terminal state.

    v7.40.0 — an encargo spawned via ``to_task`` from an ALERT, a RECOMMENDATION
    or a DECISION carries ``metadata_json.from = {table, pk, action}``. The three
    sources now share ONE lifecycle (open → acknowledged → in_progress → done ;
    dismissed ✗), so we settle them uniformly:

      * encargo DONE      → source ``status = done`` (with the outcome on its
        metadata, stamping whichever resolved_/decided_ columns it carries);
      * encargo CANCELLED → source back to ``acknowledged`` — the work did not
        happen, so it returns to the operator's plate.

    We only move a source still in ``in_progress`` (the state ``to_task`` left it
    in) — never override a status the operator changed by hand in the meantime.

    Best-effort: never raises — a bookkeeping miss must not fail closing the task.
    """
    try:
        src = _json_obj(getattr(item, "metadata_json", None)).get("from") or {}
        table = str(src.get("table") or "").lower()
        pk = src.get("pk")
        status = (item.status or "").lower()
        if not table or pk is None or status not in ("done", "cancelled"):
            return
        from app.app_models.business import Alert, Decision, Recommendation
        model = {"alerts": Alert, "recommendations": Recommendation,
                 "decisions": Decision}.get(table)
        if model is None:
            return
        src_row = (await session.execute(
            select(model).where(model.id == int(pk))
        )).scalar_one_or_none()
        if src_row is None:
            return
        m = _json_obj(src_row.metadata_json)
        m["outcome"] = {
            "encargo_id": item.id,
            "status": status,
            "result_md": (getattr(item, "result_md", None) or "")[:4000],
            "completed_at": iso_utc(getattr(item, "completed_at", None)),
            "last_run_id": getattr(item, "last_run_id", None),
        }
        src_row.metadata_json = json.dumps(m, default=str)
        # Only settle a source that is still «en curso» (what to_task left it in);
        # respect any status the operator set by hand since then.
        if (getattr(src_row, "status", None) or "").lower() == "in_progress":
            if status == "done":
                src_row.status = "done"
                stamp = now_utc().replace(tzinfo=None)
                for at_col, by_col in (("resolved_at", "resolved_by"),
                                       ("decided_at", "decided_by")):
                    if hasattr(src_row, at_col):
                        setattr(src_row, at_col, stamp)
                    if hasattr(src_row, by_col) and not getattr(src_row, by_col, None):
                        setattr(src_row, by_col, "system")
            else:  # cancelled → back to the operator's plate
                src_row.status = "acknowledged"
        await session.commit()
    except Exception:
        logger.debug("settle_source_on_complete failed for item %s", getattr(item, "id", "?"), exc_info=True)


_SIGNAL_MODELS = {"alerts": "Alert", "recommendations": "Recommendation", "decisions": "Decision"}


async def materialize_signal_statuses(session, tables: Optional[set[str]] = None) -> int:
    """Materialize the completion of alerts/recommendations/decisions from the
    REAL current state — NO events, NO propagation to trust. For every source
    still «en curso» that carries an ``metadata.encargo_id``, we read the actual
    photo and settle it deterministically:

      * the **encargo** (``ScheduledItem``) is DONE      → source ``done``;
      * the encargo was CANCELLED                        → source ``acknowledged``;
      * the encargo hasn't caught up but ITS **execution** (``AgentRun`` via
        ``last_run_id``) already COMPLETED               → materialize the encargo
        from its run, then the source ``done``.

    Idempotent and self-correcting: it can never drift because it derives from the
    live encargo+run rows every time it runs. Cheap (only scans ``in_progress``
    rows) so it is safe to call on every read of a signal table. ``tables`` limits
    the scan to the tables being served. Best-effort; never raises.
    """
    from app.app_models.business import Alert, Decision, Recommendation
    from app.app_models.runtime import AgentRun
    models = {"alerts": Alert, "recommendations": Recommendation, "decisions": Decision}
    if tables is not None:
        models = {k: v for k, v in models.items() if k in tables}
    settled = 0
    try:
        for model in models.values():
            srcs = (await session.execute(
                select(model).where(model.status == "in_progress")
            )).scalars().all()
            for src in srcs:
                eid = _json_obj(getattr(src, "metadata_json", None)).get("encargo_id")
                if not eid:
                    continue
                item = (await session.execute(
                    select(ScheduledItem).where(ScheduledItem.id == int(eid))
                )).scalar_one_or_none()
                if item is None:
                    continue
                st = (item.status or "").lower()
                if st not in ("done", "cancelled") and item.last_run_id:
                    # Encargo status hasn't caught up — consult the real execution.
                    run_status = (await session.execute(
                        select(AgentRun.status).where(AgentRun.id == item.last_run_id)
                    )).scalar_one_or_none()
                    if str(run_status or "").upper() == "COMPLETED":
                        item.status = "done"
                        if getattr(item, "completed_at", None) is None:
                            item.completed_at = now_utc().replace(tzinfo=None)
                        st = "done"
                if st not in ("done", "cancelled"):
                    continue
                await settle_source_on_complete(session, item)
                settled += 1
        if settled:
            logger.info("materialize_signal_statuses settled %d source(s)", settled)
    except Exception:
        logger.debug("materialize_signal_statuses failed", exc_info=True)
    return settled


async def materialize_encargo_statuses(session, item_ids: Optional[list[int]] = None) -> int:
    """Materialize an encargo's completion from its EXECUTION — the same real-photo
    approach as ``materialize_signal_statuses``, one level down. Normally the
    run→encargo mirror (``executor._sync_item_for_run``) closes an encargo when its
    run finishes; this derives the SAME truth from the live ``AgentRun`` on read,
    so a missed mirror can't leave an encargo «en curso» forever. An agent encargo
    still non-terminal whose ``last_run_id`` run has COMPLETED is set to «done» and
    its source alert/recommendation/decision is settled too. Idempotent; cheap
    (only touches non-terminal items that already have a run). Never raises.
    """
    from app.app_models.runtime import AgentRun
    changed = 0
    try:
        stmt = select(ScheduledItem).where(
            ScheduledItem.assignee == "agent",
            ScheduledItem.last_run_id.isnot(None),
            ScheduledItem.status.notin_(("done", "cancelled", "failed")),
        )
        if item_ids:
            stmt = stmt.where(ScheduledItem.id.in_(item_ids))
        for item in (await session.execute(stmt)).scalars().all():
            run_status = (await session.execute(
                select(AgentRun.status).where(AgentRun.id == item.last_run_id)
            )).scalar_one_or_none()
            if str(run_status or "").upper() != "COMPLETED":
                continue
            item.status = "done"
            if getattr(item, "completed_at", None) is None:
                item.completed_at = now_utc().replace(tzinfo=None)
            await session.commit()
            # Cascade to the source signal (encargo → alert/recommendation/decision).
            await settle_source_on_complete(session, item)
            changed += 1
        if changed:
            logger.info("materialize_encargo_statuses closed %d encargo(s)", changed)
    except Exception:
        logger.debug("materialize_encargo_statuses failed", exc_info=True)
    return changed


async def _resolve_assigned_to(raw: Any) -> Optional[str]:
    """``assigned_to`` must reference a DIRECTORY person (v7.24.0). Accepts
    an email/name/alias/phone, stores the canonical directory email. 422 on
    no-match or ambiguity — free-typed addresses are no longer persisted;
    add the person in Usuarios first."""
    term = str(raw).strip().lower() if raw else None
    if not term:
        return None
    from app.users_directory import resolve_directory_email
    resolved = await resolve_directory_email(term)
    if not resolved:
        raise HTTPException(
            status_code=422,
            detail=f"assigned_to {term!r} does not match exactly one user in "
                   "the directory. Add the person in Usuarios (or disambiguate) first.",
        )
    return resolved


def _series_id(item: ScheduledItem) -> Optional[str]:
    try:
        return (json.loads(item.metadata_json or "{}") or {}).get("series_id")
    except Exception:
        return None


# Statuses that mark a not-yet-executed FUTURE occurrence of a recurring
# series. The Encargos board collapses these to the single earliest one
# (the "next" task); later copies stay hidden until the current one runs.
_PENDING_FUTURE = ("scheduled", "pending")


def _due_sort_key(item: ScheduledItem):
    """Ascending sort key by the item's time anchor; undated items sort
    last so a dated occurrence always wins as the "next" one."""
    anchor = item.due_at or item.start_at
    return (anchor is None, anchor or datetime.max)


def _collapse_recurring(rows: list[ScheduledItem]) -> list[ScheduledItem]:
    """Encargos view: show only the NEXT occurrence of each recurring
    series instead of the whole materialised chain.

    A weekly calendar task assigned to the employee is stored as N real
    ``ScheduledItem`` rows (so the calendar month grid can render/strike
    every week independently). Without this collapse the Encargos board
    would list all N future copies at once — the reported bug. We keep:

    * every non-recurring item (no ``series_id``),
    * every occurrence that is already running / finished (in_progress,
      overdue, failed, done, cancelled) — i.e. the active one + history,
    * and, per series, ONLY the earliest still-pending occurrence.

    As each occurrence executes it leaves the pending set, so the next
    one automatically surfaces — exactly the desired
    one-task-at-a-time behaviour.
    """
    earliest: dict[str, ScheduledItem] = {}
    for it in rows:
        sid = _series_id(it)
        if not sid or it.status not in _PENDING_FUTURE:
            continue
        cur = earliest.get(sid)
        if cur is None or _due_sort_key(it) < _due_sort_key(cur):
            earliest[sid] = it
    out: list[ScheduledItem] = []
    for it in rows:
        sid = _series_id(it)
        if sid and it.status in _PENDING_FUTURE and earliest.get(sid) is not it:
            continue
        out.append(it)
    return out


def _artifacts(item: ScheduledItem) -> list[dict]:
    raw = getattr(item, "artifacts_json", None)
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except Exception:
        return []


def _serialize(item: ScheduledItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "description": item.description or "",
        "kind": item.kind,
        "assignee": item.assignee,
        "origin": item.origin,
        "status": item.status,
        "priority": item.priority,
        "due_at": iso_utc(item.due_at),
        "start_at": iso_utc(item.start_at),
        "end_at": iso_utc(item.end_at),
        "all_day": bool(item.all_day),
        "recurrence": item.recurrence,
        "series_id": _series_id(item),
        "created_by": item.created_by,
        "assigned_to": item.assigned_to,
        "fired_at": iso_utc(item.fired_at),
        "completed_at": iso_utc(item.completed_at),
        "result_md": item.result_md,
        "last_run_id": item.last_run_id,
        "artifacts": _artifacts(item),
        "created_at": iso_utc(item.created_at),
        "updated_at": iso_utc(item.updated_at),
    }


def _parse_dt(body: dict, key: str) -> Optional[datetime]:
    raw = body.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        parsed = parse_to_utc(str(raw))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{key} must be ISO-8601")
    return parsed.replace(tzinfo=None) if parsed else None


async def _emit_delegated_task(s, item: ScheduledItem, actor: str) -> int:
    now = now_utc()
    seed = f"delegated::{item.id}::{actor}::{now.isoformat()}"
    payload = {
        "item_id": item.id,
        "title": item.title,
        "description": item.description or "",
        "priority": item.priority,
        "created_by": actor,
        "trigger": "delegated",
    }
    fp = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    stmt = sqlite_insert(Event).values(
        fingerprint=fp,
        type=DELEGATED_TASK_EVENT_TYPE,
        source=f"encargo:{item.id}",
        payload_json=json.dumps(payload, default=str),
        status="PENDING",
        attempts=0,
        detected_at=now.replace(tzinfo=None),
    ).on_conflict_do_nothing(index_elements=[Event.fingerprint])
    await s.execute(stmt)
    row = (await s.execute(
        select(Event.id).where(Event.fingerprint == fp)
    )).scalar_one_or_none()
    return int(row) if row else 0


# ── Calendar CRUD ────────────────────────────────────────────────────


@router.get("/calendar")
async def list_calendar(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    assignee: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    _: TokenPayload = Depends(require_admin),
):
    """List scheduled items, optionally bounded to a [from, to] window
    (matched against due_at OR start_at) and filtered by assignee/status.
    """
    filters = []
    if assignee in _ASSIGNEES:
        filters.append(ScheduledItem.assignee == assignee)
    if status in _STATUSES:
        filters.append(ScheduledItem.status == status)
    f_dt = None
    t_dt = None
    try:
        if from_:
            f_dt = parse_to_utc(from_)
            f_dt = f_dt.replace(tzinfo=None) if f_dt else None
        if to:
            t_dt = parse_to_utc(to)
            t_dt = t_dt.replace(tzinfo=None) if t_dt else None
    except ValueError:
        raise HTTPException(status_code=422, detail="from/to must be ISO-8601")

    async with app_session() as s:
        await materialize_encargo_statuses(s)
        stmt = select(ScheduledItem)
        for cond in filters:
            stmt = stmt.where(cond)
        from sqlalchemy import or_, and_
        # Range filter on the effective time anchor (start_at if set else due_at).
        if f_dt is not None:
            stmt = stmt.where(or_(
                and_(ScheduledItem.start_at.isnot(None), ScheduledItem.start_at >= f_dt),
                and_(ScheduledItem.start_at.is_(None), ScheduledItem.due_at.isnot(None),
                     ScheduledItem.due_at >= f_dt),
            ))
        if t_dt is not None:
            stmt = stmt.where(or_(
                and_(ScheduledItem.start_at.isnot(None), ScheduledItem.start_at <= t_dt),
                and_(ScheduledItem.start_at.is_(None), ScheduledItem.due_at.isnot(None),
                     ScheduledItem.due_at <= t_dt),
            ))
        stmt = stmt.order_by(ScheduledItem.due_at.asc().nullslast()).limit(1000)
        rows = (await s.execute(stmt)).scalars().all()
    return {"items": [_serialize(r) for r in rows]}


@router.post("/calendar")
async def create_calendar_item(
    body: dict = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """Create a calendar item (event / task / reminder). The operator can
    create items for themselves, for the employee, or for another person.
    An item assigned to the agent with a future ``due_at`` is picked up by
    the scheduled-items watcher when it falls due."""
    title = str(body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="title is required")
    kind = (body.get("kind") or "event").strip().lower()
    if kind not in _KINDS:
        kind = "event"
    assignee = (body.get("assignee") or "user").strip().lower()
    if assignee not in _ASSIGNEES:
        assignee = "user"
    priority = (body.get("priority") or "medium").strip().lower()
    if priority not in _PRIORITIES:
        priority = "medium"

    due_at = _parse_dt(body, "due_at")
    start_at = _parse_dt(body, "start_at")
    end_at = _parse_dt(body, "end_at")
    # Calendar blocks anchor on start_at; agent work anchors on due_at.
    if assignee == "agent" and due_at is None and start_at is not None:
        due_at = start_at

    # Weekly recurrence: ``repeat == "weekly"`` (or recurrence == "weekly")
    # materialises a series of identical entries, one per week, sharing a
    # ``series_id`` so the whole chain can be deleted at once.
    repeat = str(body.get("repeat") or body.get("recurrence") or "").strip().lower()
    weekly = repeat in ("weekly", "week", "1") or repeat.startswith("week")
    series_id = uuid.uuid4().hex if weekly else None
    assigned_to = await _resolve_assigned_to(body.get("assigned_to"))
    all_day = 1 if body.get("all_day") else 0

    def _make(offset_weeks: int) -> ScheduledItem:
        delta = timedelta(weeks=offset_weeks)
        d_due = (due_at + delta) if due_at else None
        d_start = (start_at + delta) if start_at else None
        d_end = (end_at + delta) if end_at else None
        return ScheduledItem(
            idempotency_key=f"cal-{uuid.uuid4().hex}",
            title=title,
            description=str(body.get("description") or ""),
            kind=kind,
            assignee=assignee,
            origin="user",
            status="scheduled" if (d_due or d_start) else "pending",
            priority=priority,
            due_at=d_due,
            start_at=d_start,
            end_at=d_end,
            all_day=all_day,
            recurrence="weekly" if weekly else None,
            created_by=user.email,
            assigned_to=assigned_to,
            metadata_json=json.dumps({"series_id": series_id}) if series_id else "{}",
        )

    count = _WEEKLY_HORIZON if weekly else 1
    async with app_write() as s:
        first = _make(0)
        s.add(first)
        for w in range(1, count):
            s.add(_make(w))
        await s.commit()
        await s.refresh(first)
        result = _serialize(first)
    logger.info(
        "calendar item %s created by %s (weekly=%s, count=%s)",
        result["id"], user.email, weekly, count,
    )
    return result


async def _puede_tocar(item: ScheduledItem, user: TokenPayload) -> None:
    """SECURITY: who may change or delete a calendar item.

    The calendar is shared by design (the team SEES it), but changing or deleting belongs to
    whoever created it, whoever it was assigned to, or an admin. Without this a member could
    move, close or delete —including a whole series with `scope=series`— any colleague's item."""
    from app.users_directory import is_admin
    e = (user.email or "").strip().lower()
    if await is_admin(e):
        return
    if (item.created_by or "").strip().lower() == e:
        return
    if (item.assigned_to or "").strip().lower() == e:
        return
    raise HTTPException(status_code=403,
                        detail="Solo puede cambiarlo quien lo creó, a quien se le encargó o un "
                               "administrador.")


@router.patch("/calendar/{item_id}")
async def update_calendar_item(
    item_id: int,
    body: dict = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """Update / reschedule / change the status of a scheduled item."""
    async with app_write() as s:
        item = (await s.execute(
            select(ScheduledItem).where(ScheduledItem.id == item_id)
        )).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail="not found")
        await _puede_tocar(item, user)
        if "title" in body and str(body["title"]).strip():
            item.title = str(body["title"]).strip()
        if "description" in body:
            item.description = str(body.get("description") or "")
        if "priority" in body and str(body.get("priority")).lower() in _PRIORITIES:
            item.priority = str(body["priority"]).lower()
        became_terminal = False
        if "status" in body and str(body.get("status")).lower() in _STATUSES:
            item.status = str(body["status"]).lower()
            if item.status in ("done", "cancelled"):
                item.completed_at = now_utc().replace(tzinfo=None)
                became_terminal = True
        if "kind" in body and str(body.get("kind")).lower() in _KINDS:
            item.kind = str(body["kind"]).lower()
        if "assigned_to" in body:
            item.assigned_to = await _resolve_assigned_to(body.get("assigned_to"))
        for key in ("due_at", "start_at", "end_at"):
            if key in body:
                setattr(item, key, _parse_dt(body, key))
        if "all_day" in body:
            item.all_day = 1 if body.get("all_day") else 0
        if "recurrence" in body:
            item.recurrence = (str(body["recurrence"]).strip() or None
                               if body.get("recurrence") else None)
        await s.commit()
        await s.refresh(item)
        # Settle a linked recommendation/decision when this encargo just closed.
        if became_terminal:
            await settle_source_on_complete(s, item)
        result = _serialize(item)
    logger.info("calendar item %s updated by %s", item_id, user.email)
    return result


@router.delete("/calendar/{item_id}")
async def delete_calendar_item(
    item_id: int,
    scope: str = Query("single"),
    user: TokenPayload = Depends(require_admin),
):
    """Delete a scheduled item.

    ``scope=single`` (default) removes just this occurrence; ``scope=series``
    removes every entry that shares its recurring ``series_id`` (the whole
    weekly chain).
    """
    async with app_write() as s:
        item = (await s.execute(
            select(ScheduledItem).where(ScheduledItem.id == item_id)
        )).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail="not found")
        await _puede_tocar(item, user)
        deleted = 1
        sid = _series_id(item)
        if scope == "series" and sid:
            members = (await s.execute(
                select(ScheduledItem).where(
                    ScheduledItem.metadata_json.like(f'%"series_id": "{sid}"%')
                )
            )).scalars().all()
            deleted = 0
            for m in members:
                await s.delete(m)
                deleted += 1
        else:
            await s.delete(item)
        await s.commit()
    logger.info("calendar item %s deleted by %s (scope=%s, n=%s)",
                item_id, user.email, scope, deleted)
    return {"ok": True, "id": item_id, "deleted": deleted}


# ── Encargos (delegated tasks) ───────────────────────────────────────


@router.get("/agent/tasks/producers")
async def task_producers(_: TokenPayload = Depends(require_admin)):
    """Automations that have created encargos — populates the provenance filter."""
    from app import automation_provenance as prov

    return {"items": await prov.producers_of("scheduled_items")}


async def _solo_suyos(user: TokenPayload):
    """None for an admin OF THIS EMPLOYEE (sees every encargo); else the clause «the ones THIS person created or
    that are for them» (14.18.0: a member saw the whole team's encargos). The role is the employee's directory's,
    never the platform's `is_superuser` (the same rule as /users/me, INC-AGO-39): a WAI admin who is a member here
    saw everyone's encargos (15.0.2, AGO-185)."""
    from app.users_directory import is_admin
    if await is_admin(user.email):
        return None
    e = (user.email or "").strip().lower()
    return or_(ScheduledItem.created_by == e, ScheduledItem.assigned_to == e)


async def _es_suyo(item: ScheduledItem, user: TokenPayload) -> bool:
    from app.users_directory import is_admin
    if await is_admin(user.email):
        return True
    e = (user.email or "").strip().lower()
    return e in ((item.created_by or "").strip().lower(), (item.assigned_to or "").strip().lower())


@router.get("/agent/tasks")
async def list_tasks(
    request: Request,
    status: Optional[str] = Query(None),
    user: TokenPayload = Depends(require_project_access),
):
    """List tasks delegated to the employee (Encargos board): every item
    assigned to the agent, newest first. A member only gets their own.

    A recurring series (a weekly calendar task) is collapsed to its NEXT
    occurrence so the board shows one upcoming task, not the whole
    materialised chain (see :func:`_collapse_recurring`)."""
    from app import automation_provenance as prov

    aid, only_open = prov.parse_provenance_query(request)
    async with app_session() as s:
        await materialize_encargo_statuses(s)
        stmt = select(ScheduledItem).where(ScheduledItem.assignee == "agent")
        suyos = await _solo_suyos(user)
        if suyos is not None:
            stmt = stmt.where(suyos)
        if status in _STATUSES:
            stmt = stmt.where(ScheduledItem.status == status)
        if aid is not None:
            clause = prov.sqlalchemy_provenance_filter(
                ScheduledItem, aid, only_open=only_open)
            if clause is not None:
                stmt = stmt.where(clause)
        stmt = stmt.order_by(ScheduledItem.created_at.desc()).limit(500)
        rows = (await s.execute(stmt)).scalars().all()
    items = [_serialize(r) for r in _collapse_recurring(rows)]
    items = await prov.attach_origin("scheduled_items", items)
    ctx = await prov.provenance_context(aid, only_open=only_open) if aid else None
    return {"items": items, "filtered_by_automation": ctx}


@router.get("/agent/tasks/{item_id}")
async def get_task(
    item_id: int,
    user: TokenPayload = Depends(require_project_access),
):
    """Single encargo (delegated task / calendar item) for the DETAIL view.

    Returns the full serialised item plus ``source`` — the ``metadata_json.from``
    trace ({entity, table, pk, action}) so the detail page can link BACK to the
    alert / recommendation / decision this encargo was created from (the «→
    Encargo #X» chip is now a real, navigable round-trip)."""
    async with app_session() as s:
        await materialize_encargo_statuses(s, [item_id])
        item = (await s.execute(
            select(ScheduledItem).where(ScheduledItem.id == item_id)
        )).scalar_one_or_none()
        # 404 and not 403 for someone else's: «it exists but it is not yours» already tells something.
        if item is None or not await _es_suyo(item, user):
            raise HTTPException(status_code=404, detail="not found")
        result = _serialize(item)
        result["source"] = _json_obj(item.metadata_json).get("from") or None
    return result


@router.post("/agent/tasks")
async def create_task(
    body: dict = Body(...),
    user: TokenPayload = Depends(require_project_access),
):
    """Delegate a task to the AI employee.

    No ``due_at`` (or a due time already in the past) ⇒ run it NOW. A
    future ``due_at`` ⇒ schedule it; the watcher fires it when due.
    Returns the created item plus the ``event_id`` when fired immediately.
    """
    title = str(body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="title is required")
    priority = (body.get("priority") or "medium").strip().lower()
    if priority not in _PRIORITIES:
        priority = "medium"
    due_at = _parse_dt(body, "due_at")
    now_naive = now_utc().replace(tzinfo=None)
    run_now = due_at is None or due_at <= now_naive

    item = ScheduledItem(
        idempotency_key=f"task-{uuid.uuid4().hex}",
        title=title,
        description=str(body.get("description") or ""),
        kind="task",
        assignee="agent",
        origin="user",
        status="in_progress" if run_now else "scheduled",
        priority=priority,
        due_at=None if run_now else due_at,
        created_by=user.email,
    )
    event_id = 0
    async with app_write() as s:
        s.add(item)
        await s.commit()
        await s.refresh(item)
        if run_now:
            item.fired_at = now_naive
            event_id = await _emit_delegated_task(s, item, user.email)
            await s.commit()
            await s.refresh(item)
        result = _serialize(item)
    logger.info(
        "encargo %s created by %s (run_now=%s, event=%s)",
        result["id"], user.email, run_now, event_id,
    )
    return {**result, "event_id": event_id or None}


async def _rerun_item(item_id: int, actor: str) -> dict:
    """Re-fire an agent-owned scheduled item NOW.

    Used by both Encargos ("reejecutar") and Calendar ("ejecutar ahora").
    Resets the item to ``in_progress``, clears the previous outcome and
    emits a fresh ``DELEGATED_TASK`` event (the seed includes the current
    timestamp, so each retry gets a distinct fingerprint → a new run).
    """
    async with app_write() as s:
        item = (await s.execute(
            select(ScheduledItem).where(ScheduledItem.id == item_id)
        )).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail="not found")
        if (item.assignee or "agent") != "agent":
            raise HTTPException(
                status_code=409,
                detail="only items assigned to the employee can be run",
            )
        if item.status == "in_progress":
            raise HTTPException(status_code=409, detail="already running")
        now = now_utc().replace(tzinfo=None)
        item.status = "in_progress"
        item.fired_at = now
        item.completed_at = None
        item.result_md = None
        item.artifacts_json = None
        event_id = await _emit_delegated_task(s, item, actor)
        await s.commit()
        await s.refresh(item)
        result = _serialize(item)
    logger.info("scheduled item %s re-run by %s (event=%s)", item_id, actor, event_id)
    return {**result, "event_id": event_id or None}


@router.post("/agent/tasks/{item_id}/retry")
async def retry_task(
    item_id: int,
    user: TokenPayload = Depends(require_project_access),
):
    """Re-run a delegated task (Encargo) now — typically after a failure. Only your own (an
    admin, any)."""
    async with app_session() as s:
        item = (await s.execute(select(ScheduledItem).where(ScheduledItem.id == item_id))).scalar_one_or_none()
    if item is None or not await _es_suyo(item, user):
        raise HTTPException(status_code=404, detail="not found")
    return await _rerun_item(item_id, user.email)


@router.post("/calendar/{item_id}/run")
async def run_calendar_item(
    item_id: int,
    user: TokenPayload = Depends(require_admin),
):
    """Run a calendar item assigned to the employee now (or re-run it)."""
    return await _rerun_item(item_id, user.email)
