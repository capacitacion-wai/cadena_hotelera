import json
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import asc, desc, select, func, or_, update
from sqlalchemy.orm import selectinload

from app.api_errors import api_error
from app.app_database import app_session, app_write
from app.app_models.runtime import AgentRun, ToolCallLog, Event, EventAudit
from app.auth import require_admin, require_project_access, TokenPayload
from app.config import AGENT_COST_PER_CREDIT
from app.cost_reporter import usd_to_credits
from app.pagination import paginated

# Run history is an Administración (Auditoría) surface — it exposes every user's
# activity, spend and transcripts. Gate the whole router: a member is redirected
# from the page AND blocked at the API (defence in depth). Only Runs/RunDetail
# (admin pages) consume it; Dashboard/Actividad use their own endpoints.
router = APIRouter(prefix="/api/runs", tags=["runs"],
                   dependencies=[Depends(require_admin)])


def _fecha(value: str | None, campo: str) -> Optional[datetime]:
    """Reads a date from the query. Absent → None. Unreadable → 422, never None.

    Returning None on something unreadable looks kind and is the worst a filter can do:
    `if since:` stays false, the query goes out WITHOUT filtering and the server answers
    200 OK with everything. Whoever asked for "the last 7 days" gets "everything" and has
    no way of noticing.

    It really happened on 30/08/2026 and cost a misdirected incident: the `+00:00` of an
    ISO travels in a query as a SPACE if it is not encoded (`+` is the good old space in a
    form), `fromisoformat` blew up, this returned None and the listing brought 891 rows
    where it should have brought 56. The summary —which does not use this parameter— said
    42, and the incident ended up accusing the counter of lying. The filter was lying.

    With 422, a client that does not encode finds out on the first call.
    """
    if value is None or value == "":
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=(f"«{campo}» no es una fecha ISO 8601 válida: {value!r}. "
                    "Si la mandas con zona horaria, recuerda codificar el «+» de «+00:00» "
                    "(%2B) — en una query sin codificar se convierte en un espacio."),
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _credits(cost_usd) -> float:
    """USD → agent credits, rounded for display (see cost_reporter)."""
    return round(usd_to_credits(cost_usd), 4)


# R-TZ-2: align with events_routes._iso_utc. The ORM stores naive
# UTC datetimes; serialise with an explicit offset so the SPA's
# ``new Date(...)`` always converts to the operator's local TZ
# instead of (mis)interpreting the bare string as local.
def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def _modos(mode: str | None) -> list[str]:
    """The ORIGIN filter accepts several modes separated by commas.

    It exists because the audit view wants neither "everything" nor "only the autonomous":
    it wants the employee's WORK —what it did on its own and what a colleague asked of it—
    without drowning in the conversation. With a single value you had to choose between
    hiding a colleague's tasks or flooding the list with chat, and both options are bad."""
    return [m.strip() for m in (mode or "").split(",") if m.strip()]


@router.get("")
async def list_runs(
    user: TokenPayload = Depends(require_project_access),
    status: list[str] | None = Query(default=None),
    mode: str | None = Query(default=None),
    # v12.7.0 — where it came in through: chat | canal | automatizacion | vigia | programado |
    # companero. A list, because "show me everything that came from outside" is two at once.
    origen: list[str] | None = Query(default=None),
    model: list[str] | None = Query(default=None),
    event_type: list[str] | None = Query(default=None),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    q: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Lists executions, newest first. ``mode`` filters by execution
    kind: ``autonomous`` (watcher/event triggered) or ``chat`` (a
    persisted chat turn). When ``mode`` is omitted the list includes
    BOTH kinds — this matches ``/api/runs/summary`` so the dashboard
    KPIs and the table below never disagree (the SPA's "Todas" toggle
    relies on this).

    Accepts the same filter family as ``/api/runs/summary``
    (``model``, ``event_type``, ``from``, ``to``) so the
    dashboard panel and the list below stay in sync without the
    caller having to re-implement filtering twice."""
    since = _fecha(date_from, "from")
    until = _fecha(date_to, "to")

    async with app_session() as s:
        base = select(AgentRun).options(selectinload(AgentRun.event))
        if status: base = base.where(AgentRun.status.in_(status))
        modos = _modos(mode)
        if modos:  base = base.where(AgentRun.mode.in_(modos))
        if origen: base = base.where(AgentRun.origen.in_(origen))
        if model:  base = base.where(AgentRun.model.in_(model))
        if since:  base = base.where(AgentRun.started_at >= since)
        if until:  base = base.where(AgentRun.started_at < until)
        if q:
            base = base.where(or_(
                AgentRun.prompt.contains(q),
                AgentRun.response_md.contains(q),
            ))
        # event_type filters in SQL through the events join — loading
        # the WHOLE history into Python just to filter (the previous
        # behaviour) degraded linearly with table size and defeated
        # LIMIT/OFFSET pagination.
        if event_type:
            base = base.join(Event, AgentRun.event_id == Event.id).where(
                Event.type.in_(event_type)
            )
        # Server-side sort (whitelisted direct columns; anything else →
        # newest-first) so a sortable column header actually reorders.
        _SORTABLE = {
            "started_at": AgentRun.started_at,
            "status": AgentRun.status,
            "attempts": AgentRun.attempts,
            "model": AgentRun.model,
            "duration_sec": AgentRun.duration_sec,
            "tokens_in": AgentRun.tokens_in,
            "tokens_out": AgentRun.tokens_out,
        }
        sort_col = _SORTABLE.get(sort or "", AgentRun.started_at)
        sort_dir = asc if (order or "desc").lower() == "asc" else desc
        total = (await s.execute(
            select(func.count()).select_from(base.subquery())
        )).scalar()
        rows = (await s.execute(
            base.order_by(sort_dir(sort_col)).limit(limit).offset(offset)
        )).scalars().all()
        return paginated(total or 0, limit, offset, [_summary(r) for r in rows])


# R-RUNS-1 (v7.x): ``/summary`` MUST be declared before the
# ``/{run_id}`` catch-all. FastAPI matches routes top-to-bottom, so
# putting ``/{run_id}`` first makes ``GET /api/runs/summary`` match
# ``run_id="summary"`` and crash with 422 when the int validator
# rejects the path param. Swapping the order is the canonical fix
# (see verify_generated_app.sh §8.4).
@router.get("/summary")
async def runs_summary(
    user: TokenPayload = Depends(require_project_access),
    days: int = Query(7, ge=1, le=365),
    mode: str | None = Query(default=None),
    status: list[str] | None = Query(default=None),
    model: list[str] | None = Query(default=None),
    event_type: list[str] | None = Query(default=None),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
):
    """Aggregated KPI panel used by the Ejecuciones dashboard.

    Returns totals (runs, cost, tokens), success rate, duration
    quantiles (p50/p95), counts by model / day / hour / event_type
    / status, plus the catalog of models and event types observed
    inside the selected window so the UI can populate its
    dropdowns without a second round-trip.

    Filters accepted (all optional, AND-combined):

      * ``days`` (default 7) — ignored when ``from`` is given.
      * ``from`` / ``to`` (ISO-8601) — custom window. ``to`` is
        exclusive; when omitted defaults to "now".
      * ``mode`` — ``autonomous`` or ``chat``; ``None`` includes
        both.
      * ``status`` — one or more of RUNNING/COMPLETED/FAILED.
      * ``model`` — one or more model ids.
      * ``event_type`` — one or more event type strings (joins
        against the ``events`` table).
    """
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    explicit_from = _fecha(date_from, "from")
    explicit_to = _fecha(date_to, "to") or now
    since = explicit_from if explicit_from else (now - timedelta(days=days))
    # Guard against inverted ranges that would otherwise blow up
    # downstream date math and produce empty panels.
    if explicit_to <= since:
        explicit_to = since + timedelta(seconds=1)

    async with app_session() as s:
        q = (
            select(AgentRun)
            .options(selectinload(AgentRun.event))
            .where(AgentRun.started_at >= since)
            .where(AgentRun.started_at < explicit_to)
        )
        modos = _modos(mode)
        if modos:
            q = q.where(AgentRun.mode.in_(modos))
        if status:
            q = q.where(AgentRun.status.in_(status))
        if model:
            q = q.where(AgentRun.model.in_(model))
        if event_type:
            q = q.join(Event, AgentRun.event_id == Event.id).where(
                Event.type.in_(event_type)
            )
        rows = (await s.execute(q)).scalars().all()

    def _to_float(v) -> float:
        if v is None or v == "":
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    total = len(rows)
    completed = sum(1 for r in rows if r.status == "COMPLETED")
    failed    = sum(1 for r in rows if r.status == "FAILED")
    running   = sum(1 for r in rows if r.status == "RUNNING")
    cost      = sum(_to_float(r.cost_usd) for r in rows)
    tokens_in = sum((r.tokens_in or 0) for r in rows)
    tokens_out = sum((r.tokens_out or 0) for r in rows)
    durs = sorted([r.duration_sec for r in rows if r.duration_sec is not None])

    def _quantile(xs: list[float], q: float) -> float | None:
        if not xs: return None
        k = max(0, min(len(xs) - 1, int(round((len(xs) - 1) * q))))
        return float(xs[k])

    by_model: dict[str, dict] = {}
    for r in rows:
        m = r.model or "unknown"
        b = by_model.setdefault(m, {"runs": 0, "cost_usd": 0.0, "credits": 0.0, "tokens_in": 0, "tokens_out": 0})
        b["runs"] += 1
        b["cost_usd"]   += _to_float(r.cost_usd)
        b["credits"]    += usd_to_credits(r.cost_usd)
        b["tokens_in"]  += (r.tokens_in or 0)
        b["tokens_out"] += (r.tokens_out or 0)

    by_day: dict[str, dict] = {}
    for r in rows:
        if not r.started_at:
            continue
        day = r.started_at.date().isoformat()
        b = by_day.setdefault(day, {"runs": 0, "cost_usd": 0.0, "credits": 0.0, "completed": 0, "failed": 0})
        b["runs"] += 1
        b["cost_usd"]  += _to_float(r.cost_usd)
        b["credits"]   += usd_to_credits(r.cost_usd)
        if r.status == "COMPLETED": b["completed"] += 1
        if r.status == "FAILED":    b["failed"]    += 1

    # Hourly granularity is only meaningful for short windows. We
    # emit it when the selected range is ≤ 48 hours so the UI can
    # switch from a daily bar chart to a denser timeline.
    window_hours = (explicit_to - since).total_seconds() / 3600.0
    by_hour: list[dict] | None = None
    if window_hours <= 48.0:
        bucket: dict[str, dict] = {}
        for r in rows:
            if not r.started_at:
                continue
            slot = r.started_at.replace(minute=0, second=0, microsecond=0)
            # Tag as UTC so the SPA's ``new Date(...)`` shifts the
            # hourly buckets into the browser's local timezone
            # instead of treating them as naive local times.
            key = _iso_utc(slot)
            b = bucket.setdefault(key, {"runs": 0, "cost_usd": 0.0, "credits": 0.0})
            b["runs"] += 1
            b["cost_usd"] += _to_float(r.cost_usd)
            b["credits"] += usd_to_credits(r.cost_usd)
        by_hour = [{"hour": h, **stats} for h, stats in sorted(bucket.items())]

    by_event_type: dict[str, dict] = {}
    for r in rows:
        et = (r.event.type if r.event else None) or ("(chat)" if r.mode == "chat" else "(manual)")
        b = by_event_type.setdefault(et, {"runs": 0, "cost_usd": 0.0, "credits": 0.0, "failed": 0})
        b["runs"] += 1
        b["cost_usd"] += _to_float(r.cost_usd)
        b["credits"] += usd_to_credits(r.cost_usd)
        if r.status == "FAILED":
            b["failed"] += 1

    by_status = {
        "RUNNING": running,
        "COMPLETED": completed,
        "FAILED": failed,
    }
    by_mode: dict[str, dict] = {}
    for r in rows:
        m = r.mode or "autonomous"
        b = by_mode.setdefault(m, {"runs": 0, "cost_usd": 0.0, "credits": 0.0})
        b["runs"] += 1
        b["cost_usd"] += _to_float(r.cost_usd)
        b["credits"] += usd_to_credits(r.cost_usd)

    # Catalog the models / event types we saw in this window so
    # the UI doesn't have to issue another round-trip just to
    # populate its filter dropdowns. We intentionally do NOT
    # restrict by the current filters here — operators expect the
    # dropdowns to keep showing every option even after applying
    # one of them. We answer "what other models / event types
    # exist in the database?" with a small auxiliary query.
    async with app_session() as s2:
        models_seen = (await s2.execute(
            select(AgentRun.model)
            .where(AgentRun.started_at >= since)
            .where(AgentRun.started_at < explicit_to)
            .distinct()
        )).scalars().all()
        # Event types via a direct DISTINCT join — re-loading every run
        # row with its event just to collect the distinct types was an
        # O(rows) memory hit on busy tenants.
        event_types_rows = (await s2.execute(
            select(Event.type)
            .join(AgentRun, AgentRun.event_id == Event.id)
            .where(AgentRun.started_at >= since)
            .where(AgentRun.started_at < explicit_to)
            .distinct()
        )).scalars().all()
    event_types_seen = sorted({t for t in event_types_rows if t})
    models_list = sorted([m for m in models_seen if m])

    return {
        "since": since.isoformat(),
        "until": explicit_to.isoformat(),
        "window_hours": round(window_hours, 2),
        "total": total,
        "completed": completed,
        "failed": failed,
        "running": running,
        "success_rate": (completed / total) if total else None,
        "cost_usd": round(cost, 6),
        "avg_cost_per_run": round(cost / total, 6) if total else None,
        # Credits = headline figure the UI shows instead of money.
        "credits": round(usd_to_credits(cost), 4),
        "avg_credits_per_run": round(usd_to_credits(cost) / total, 4) if total else None,
        "cost_per_credit": AGENT_COST_PER_CREDIT,
        "tokens_in":  tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_in + tokens_out,
        "duration_p50": _quantile(durs, 0.50),
        "duration_p95": _quantile(durs, 0.95),
        "duration_avg": (sum(durs) / len(durs)) if durs else None,
        "by_model": [
            {"model": m, **stats} for m, stats in sorted(by_model.items(), key=lambda kv: -kv[1]["runs"])
        ],
        "by_day": [
            {"day": d, **stats} for d, stats in sorted(by_day.items())
        ],
        "by_hour": by_hour,
        "by_event_type": [
            {"event_type": et, **stats} for et, stats in sorted(by_event_type.items(), key=lambda kv: -kv[1]["runs"])
        ],
        "by_status": [
            {"status": k, "runs": v} for k, v in by_status.items()
        ],
        "by_mode": [
            {"mode": k, **v} for k, v in sorted(by_mode.items())
        ],
        "filters": {
            "mode": mode,
            "status": status or [],
            "model": model or [],
            "event_type": event_type or [],
            "from": since.isoformat(),
            "to": explicit_to.isoformat(),
        },
        "catalogs": {
            "models": models_list,
            "event_types": event_types_seen,
        },
    }


@router.get("/{run_id}")
async def run_detail(
    run_id: int,
    user: TokenPayload = Depends(require_project_access),
):
    """Detailed view of a single autonomous run. The shape mirrors the
    tabs the frontend expects: ``request`` (what was sent to the
    agent — prompt + meta + system_prompt preview), ``response``
    (markdown the agent returned), ``structured`` (the parsed
    STRUCTURED_RESPONSE JSON tail when present), ``tool_calls``
    (list of tool invocations) and the headline metrics.

    R-RUNS-1: this route MUST stay declared AFTER ``/summary`` — see
    the comment on ``runs_summary`` above. Moving it back to the top
    re-introduces the 422 on ``/api/runs/summary`` that the v7.x
    fix removed.
    """
    async with app_session() as s:
        run = (await s.execute(
            select(AgentRun)
            .options(selectinload(AgentRun.event), selectinload(AgentRun.tool_calls))
            .where(AgentRun.id == run_id)
        )).scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")

    meta = _safe_json(run.request_meta_json) or {}
    if not isinstance(meta, dict):
        meta = {"raw": meta}

    arts = _safe_json(run.artifacts_json)
    if not isinstance(arts, list):
        arts = []

    from app.tool_usage import usos_de
    usos = await usos_de(run_id, run_terminado=(run.status != "RUNNING"))
    for u in usos:
        # The result was stored trimmed and as TEXT: if it was JSON it is returned as such
        # so the screen paints it as always, and if not, as is.
        u["result_json"] = _safe_json(u["result_json"]) if u.get("result_json") else None

    return {
        **_summary(run),
        "artifacts": arts,
        "request": {
            "prompt": run.prompt,
            "meta":   meta,
        },
        "response": {
            "markdown":   run.response_md,
            "structured": _safe_json(run.structured_json),
        },
        # EVERYTHING it touched, not just what it wrote. It used to come from `tool_call_log`,
        # which is the idempotency ledger of the app's writes: a turn with six calls —two to
        # the PMS, two to a colleague, two to the app— showed ONE line, and the manager
        # believed they were seeing what their employee did. Now what is recorded in
        # `tool_use_log` rules and it only falls back to the old ledger for runs PRIOR to
        # this, which have no annotation and never will.
        "tool_calls": usos or [
            {
                "step_idx":        tc.step_idx,
                "tool_name":       tc.tool_name,
                "arguments_hash":  tc.arguments_hash,
                "idempotency_key": tc.idempotency_key,
                "result_hash":     tc.result_hash,
                "result_json":     _safe_json(tc.result_json),
                "executed_at":     _iso_utc(tc.executed_at),
            }
            for tc in sorted(run.tool_calls, key=lambda t: t.step_idx)
        ],
        "error": run.error,
    }


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: int,
    user: TokenPayload = Depends(require_project_access),
):
    """Operator kill switch for an in-flight run. Works for BOTH chat
    and autonomous runs:

      * marks the ``AgentRun`` FAILED (sticky — a late ``_finish_run``
        sees a non-RUNNING row and won't overwrite it),
      * for autonomous runs it also flips the owning ``Event`` to
        FAILED and clears heartbeat/next_attempt so neither stale
        recovery nor the backoff scheduler re-picks it, and
      * best-effort aborts the live OpenCode session so compute is
        freed immediately instead of churning until the gateway
        timeout.
    """
    now = datetime.now(timezone.utc)
    # `app_write`: THIS WRITES (cancelling a run is the STOP SWITCH). With
    # `app_session` the commit takes SQLite's lock without queueing, and the
    # `BEGIN IMMEDIATE` of a writer that DID queue waits up to busy_timeout
    # (15 s) holding the slot: everyone behind measures those 15 s. The operator's brake
    # being one of those that skipped the queue is exactly what INC-VIG-59
    # came to prevent.
    async with app_write() as s:
        run = (await s.execute(
            select(AgentRun).where(AgentRun.id == run_id)
        )).scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if run.status != "RUNNING":
            raise HTTPException(status_code=409, detail="run already finished")

        oc_session_id = run.opencode_session_id
        event_id = run.event_id

        await s.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .where(AgentRun.status == "RUNNING")
            .values(status="FAILED", error="cancelled by user", finished_at=now)
        )

        if event_id is not None:
            before = (await s.execute(
                select(Event.status).where(Event.id == event_id)
            )).scalar_one_or_none()
            if before in ("PENDING", "RETRY_SCHEDULED", "IN_PROGRESS", "CLAIMED"):
                await s.execute(
                    update(Event)
                    .where(Event.id == event_id)
                    .values(
                        status="FAILED", last_error="cancelled by user",
                        heartbeat_at=None, next_attempt_at=None,
                        processed_at=now,
                    )
                )
                s.add(EventAudit(
                    event_id=event_id, actor_email=user.email, action="cancel",
                    from_status=before, to_status="FAILED",
                    at=now, note="cancelled by user (from run)",
                ))
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


def _summary(run: AgentRun) -> dict:
    return {
        "id": run.id,
        "event_id": run.event_id,
        "event_type": run.event.type if run.event else None,
        "event_fingerprint": run.event.fingerprint if run.event else None,
        "idempotency_key": run.idempotency_key,
        "trace_id": run.trace_id,                  # B9: surface for triage
        "attempts": run.attempts,                  # B2: how many tries
        "status": run.status,
        "mode": run.mode,
        # v12.7.0 — who asked for it and through where. The loose FIELDS go, not the already
        # composed sentence: the screen builds «Correo · fulano@…», but filtering needs the data.
        # NULL in runs prior to this version and it cannot be backfilled: the session it
        # would have to be deduced from rotates and is collected.
        "solicitante": run.solicitante,
        "origen": run.origen,
        "canal_kind": run.canal_kind,
        "canal_id": run.canal_id,
        "model": run.model,
        "duration_sec": run.duration_sec,
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "cost_usd": run.cost_usd,
        # Cost expressed as agent credits — what the UI shows instead of
        # money (raw USD kept for back-compat / debugging).
        "credits": _credits(run.cost_usd),
        "cost_per_credit": AGENT_COST_PER_CREDIT,
        "started_at": _iso_utc(run.started_at),
        "finished_at": _iso_utc(run.finished_at),
    }
