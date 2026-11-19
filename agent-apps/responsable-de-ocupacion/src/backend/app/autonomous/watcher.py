import asyncio
import json
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.app_database import app_session
from app.app_models.runtime import Event, WatcherCursor, WatcherSchedule
from app.app_models.business import Approval
from app.config import WATCHERS  # generated from wai-app.json
from app.database import async_session as project_db_ro  # read-only project Postgres
from app.timeutil import business_tz as _business_tz

logger = logging.getLogger(__name__)

LOOP_SLEEP = 5  # seconds

# Event type emitted when a ScheduledItem assigned to the agent reaches
# its due time. The executor turns it into an autonomous run that does
# the work and closes the item. Kept here (not in the manifest) because
# the scheduled-items drain is a SYSTEM capability every app gets.
SCHEDULED_TASK_DUE_EVENT_TYPE = "SCHEDULED_TASK_DUE"


def _fingerprint(*parts: Any) -> str:
    raw = "::".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _load_cursor(session, name: str) -> dict:
    q = await session.execute(select(WatcherCursor).where(WatcherCursor.watcher_name == name))
    row = q.scalar_one_or_none()
    if row is None:
        return {"last_seen_value_json": None, "last_checked_at": None}
    return {
        "last_seen_value_json": json.loads(row.last_seen_value_json or "null"),
        "last_checked_at": row.last_checked_at,
    }


async def _save_cursor(session, name: str, value: Any):
    now = datetime.now(timezone.utc)
    stmt = sqlite_insert(WatcherCursor).values(
        watcher_name=name,
        last_checked_at=now,
        last_seen_value_json=json.dumps(value, default=str),
    ).on_conflict_do_update(
        index_elements=[WatcherCursor.watcher_name],
        set_={
            "last_checked_at": now,
            "last_seen_value_json": json.dumps(value, default=str),
        },
    )
    await session.execute(stmt)


async def _emit_event(session, *, type_: str, source: str, payload: dict, dedup_key: str | None = None):
    # dedup_key (when given) is the STABLE identity for the on-conflict
    # fingerprint — used when the payload carries volatile fields that must NOT
    # break dedup (e.g. an automation_fired payload folds in a data/sql
    # condition's live ``value``; without this, re-claiming the same trigger
    # event after a retry would change the payload hash and double-fire).
    fp = _fingerprint(
        type_, source,
        dedup_key if dedup_key is not None else json.dumps(payload, sort_keys=True, default=str),
    )
    stmt = sqlite_insert(Event).values(
        fingerprint=fp,
        type=type_,
        source=source,
        payload_json=json.dumps(payload, default=str),
        status="PENDING",
        attempts=0,
        detected_at=datetime.now(timezone.utc),
    ).on_conflict_do_nothing(index_elements=[Event.fingerprint])
    result = await session.execute(stmt)
    inserted = bool(result.rowcount)
    if inserted:
        logger.info("Event emitted: %s (%s) fp=%s", type_, source, fp[:12])
    # True ⇒ a NEW event row was inserted; False ⇒ deduped (on-conflict no-op),
    # so callers can avoid bumping fire state for a non-fire.
    return inserted


# ── Watcher implementations ──────────────────────────────────────────

def _safe_ident(s: str | None) -> bool:
    return bool(s) and s.replace("_", "").replace(".", "").isalnum()


def _due(cursor: dict, cfg: dict, default_interval: int = 60) -> bool:
    """Honour ``interval_sec`` for polling watchers. The loop ticks every
    ``LOOP_SLEEP`` (5s); without this gate every polling watcher would
    hit the project Postgres on every tick regardless of its declared
    cadence. Cron watchers do their own bucketing and skip this."""
    every = int(cfg.get("interval_sec") or default_interval)
    last = cursor.get("last_checked_at")
    if not last:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() >= every


async def _query_scalar(session, cfg: dict, sql: str):
    """Run a scalar SELECT against the right store: the read-only
    project Postgres (default) or the app's local store when the
    watcher declares ``source: "local"`` (e.g. watch the agent's own
    output tables)."""
    if (cfg.get("source") or "project") == "local":
        return (await session.execute(text(sql))).scalar()
    async with project_db_ro() as ro:
        return (await ro.execute(text(sql))).scalar()


_AGG_SQL = {"max": "MAX", "min": "MIN", "sum": "SUM", "count": "COUNT", "avg": "AVG"}


def _agg_select(cfg: dict) -> str:
    """``<AGG>("column") FROM <table>`` with identifier validation."""
    table, column = cfg.get("table"), cfg.get("column")
    if not _safe_ident(table):
        raise ValueError(f"unsafe table {table!r}")
    if not _safe_ident(column):
        raise ValueError(f"unsafe column {column!r}")
    agg = _AGG_SQL.get((cfg.get("aggregate") or "max").lower())
    if agg is None:
        raise ValueError(f"unsupported aggregate {cfg.get('aggregate')!r}")
    return f'SELECT {agg}("{column}") FROM {table}'


async def _watch_row_count_delta(session, cfg: dict):
    """Emit an event every time ``SELECT COUNT(*) FROM {table}`` grows by
    at least ``threshold`` rows."""
    name = cfg["name"]
    threshold = int(cfg.get("threshold") or 1)
    table = cfg["table"]
    if not _safe_ident(table):
        raise ValueError(f"watcher {name!r}: refused to count rows on unsafe table {table!r}")
    cursor = await _load_cursor(session, name)
    if not _due(cursor, cfg):
        return
    total = int(await _query_scalar(session, cfg, f"SELECT COUNT(*) FROM {table}") or 0)
    # FIRST OBSERVATION: seed the baseline and return WITHOUT emitting. Without
    # this guard ``last`` defaulted to 0 on the first tick after deploy, so the
    # full pre-existing row count was treated as a delta → a spurious DELTA
    # event (and autonomous run/alert) for historical data. Mirrors
    # _watch_value_change / _watch_local_event.
    if cursor["last_seen_value_json"] is None:
        await _save_cursor(session, name, {"total": total})
        return
    last = int((cursor["last_seen_value_json"] or {}).get("total") or 0)
    if total - last >= threshold:
        await _emit_event(
            session,
            type_=cfg.get("event_type") or f"DELTA::{cfg['table']}",
            source=name,
            payload={"table": cfg["table"], "previous": last, "current": total},
        )
        await _save_cursor(session, name, {"total": total})
    else:
        # Refresh last_checked_at WITHOUT moving the baseline — the
        # delta must keep accumulating across checks.
        await _save_cursor(session, name, cursor["last_seen_value_json"])


async def _watch_value_change(session, cfg: dict):
    """Emit when the observed aggregate of ``column`` on ``table``
    CHANGES between checks ("if the value of X changes"). The first
    observation only sets the baseline — no event."""
    name = cfg["name"]
    cursor = await _load_cursor(session, name)
    if not _due(cursor, cfg):
        return
    current = await _query_scalar(session, cfg, _agg_select(cfg))
    current_s = None if current is None else str(current)
    prev = (cursor["last_seen_value_json"] or {}).get("value") if cursor["last_seen_value_json"] else None
    first_observation = cursor["last_seen_value_json"] is None
    if not first_observation and current_s != prev:
        await _emit_event(
            session,
            type_=cfg.get("event_type") or f"CHANGE::{cfg['table']}.{cfg['column']}",
            source=name,
            payload={
                "table": cfg["table"], "column": cfg["column"],
                "previous": prev, "current": current_s,
            },
        )
    await _save_cursor(session, name, {"value": current_s})


async def _watch_threshold(session, cfg: dict):
    """Emit when the observed aggregate CROSSES ``threshold``
    ("when it exceeds the threshold" / "if it drops below N"). Crossing semantics:
    one event when the value enters the breached side, then the
    watcher re-arms when it returns to the safe side — no event spam
    while the value stays breached. ``direction``: ``above`` (default,
    breach when value >= threshold) or ``below`` (value <= threshold)."""
    name = cfg["name"]
    threshold = float(cfg.get("threshold") or 0)
    direction = (cfg.get("direction") or "above").lower()
    cursor = await _load_cursor(session, name)
    if not _due(cursor, cfg):
        return
    current = await _query_scalar(session, cfg, _agg_select(cfg))
    if current is None:
        await _save_cursor(session, name, cursor["last_seen_value_json"] or {"breached": False})
        return
    value = float(current)
    breached = value <= threshold if direction == "below" else value >= threshold
    was_breached = bool((cursor["last_seen_value_json"] or {}).get("breached"))
    if breached and not was_breached:
        await _emit_event(
            session,
            type_=cfg.get("event_type") or f"THRESHOLD::{cfg['table']}.{cfg['column']}",
            source=name,
            payload={
                "table": cfg["table"], "column": cfg["column"],
                "threshold": threshold, "direction": direction, "current": value,
            },
        )
    await _save_cursor(session, name, {"breached": breached, "value": value})


async def _watch_local_event(session, cfg: dict):
    """Watch a LOCAL app-store table for new rows ("when a local event
    of type Z is inserted") — e.g. react when the operator accepts
    a recommendation or when another action persisted a row. Cursor is
    the max id seen; the first observation only sets the baseline."""
    name = cfg["name"]
    table = cfg.get("table")
    id_col = cfg.get("id_column") or "id"
    if not _safe_ident(table) or not _safe_ident(id_col):
        raise ValueError(f"watcher {name!r}: unsafe table/id_column")
    cursor = await _load_cursor(session, name)
    if not _due(cursor, cfg, default_interval=30):  # local SQLite is cheap
        return
    current_max = int(
        (await session.execute(text(f'SELECT MAX("{id_col}") FROM "{table}"'))).scalar() or 0
    )
    if cursor["last_seen_value_json"] is None:
        await _save_cursor(session, name, {"max_id": current_max})
        return
    last = int((cursor["last_seen_value_json"] or {}).get("max_id") or 0)
    if current_max > last:
        await _emit_event(
            session,
            type_=cfg.get("event_type") or f"LOCAL::{table}",
            source=name,
            payload={
                "table": table, "previous_max_id": last,
                "current_max_id": current_max, "new_rows": current_max - last,
            },
        )
    await _save_cursor(session, name, {"max_id": current_max})


# ── Cron parsing (no external deps) ─────────────────────────────────
#
# We support the 5-field cron grammar emitted by the SPA's
# CronBuilder plus a handful of widely-recognised extensions
# (``*``, ``N``, ``A,B,C``, ``*/N``, ``A-B``, ``A-B/N``). That
# covers everything the operator can produce through the visual
# builder; pathological hand-typed expressions fall through the
# "advanced" mode and are validated by attempting to parse them.

def _parse_cron_field(spec: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field (e.g. ``*/5``) into the set of values
    it matches. Raises ``ValueError`` for any token that doesn't fit
    the supported grammar so the watcher loop can skip the cron and
    log a warning instead of crashing."""
    out: set[int] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"step must be > 0: {part!r}")
        else:
            base = part
        if base in ("*", ""):
            a, b = lo, hi
        elif "-" in base:
            a_s, b_s = base.split("-", 1)
            a, b = int(a_s), int(b_s)
        else:
            a = b = int(base)
        if a < lo or b > hi or a > b:
            raise ValueError(f"out of range or inverted: {part!r}")
        out.update(range(a, b + 1, step))
    if not out:
        raise ValueError(f"empty cron field: {spec!r}")
    return out


# expr → parsed field sets (or None when malformed). The watcher loop
# re-evaluates every cron each LOOP_SLEEP tick; re-tokenising the same
# 5 fields tens of thousands of times during the back-walk below was
# pure waste. Manifest + operator overrides yield a handful of distinct
# expressions, so an unbounded dict is safe.
_CRON_CACHE: dict[str, tuple[set[int], set[int], set[int], set[int], set[int]] | None] = {}


def _parse_cron_expr(
    expr: str,
) -> tuple[set[int], set[int], set[int], set[int], set[int]] | None:
    if expr in _CRON_CACHE:
        return _CRON_CACHE[expr]
    parsed = None
    parts = (expr or "").split()
    if len(parts) == 5:
        try:
            minutes = _parse_cron_field(parts[0], 0, 59)
            hours   = _parse_cron_field(parts[1], 0, 23)
            doms    = _parse_cron_field(parts[2], 1, 31)
            months  = _parse_cron_field(parts[3], 1, 12)
            dows    = _parse_cron_field(parts[4], 0, 7)
            # Cron treats Sunday as both 0 and 7; normalise.
            if 7 in dows:
                dows.add(0)
            parsed = (minutes, hours, doms, months, dows)
        except ValueError:
            parsed = None
    _CRON_CACHE[expr] = parsed
    return parsed


def _cron_matches(expr: str, dt: datetime) -> bool:
    parsed = _parse_cron_expr(expr)
    if parsed is None:
        return False
    minutes, hours, doms, months, dows = parsed
    # Python ``weekday()`` is Mon=0..Sun=6 — cron is Sun=0..Sat=6.
    cron_dow = (dt.weekday() + 1) % 7
    return (
        dt.minute in minutes
        and dt.hour in hours
        and dt.day in doms
        and dt.month in months
        and cron_dow in dows
    )


def _cron_last_tick(expr: str, now: datetime) -> datetime | None:
    """Return the most recent UTC minute matching ``expr`` on or
    before ``now``. Walks back minute-by-minute over the pre-parsed
    field sets. Returns ``None`` when the expression is malformed."""
    parsed = _parse_cron_expr(expr)
    if parsed is None:
        return None
    minutes, hours, doms, months, dows = parsed
    cur = now.replace(second=0, microsecond=0)
    # Cap at ~40 days so a monthly cron (``30 14 1 * *``) can still
    # resolve on the 28th of the following month without spinning
    # forever on a never-matching expression.
    for _ in range(60 * 24 * 40):
        cron_dow = (cur.weekday() + 1) % 7
        if (
            cur.minute in minutes
            and cur.hour in hours
            and cur.day in doms
            and cur.month in months
            and cron_dow in dows
        ):
            return cur
        cur -= timedelta(minutes=1)
    return None


async def _watch_cron(session, cfg: dict):
    """Emit ``CRON`` events deterministically using EITHER:

    * ``cron_expr`` — the 5-field cron expression edited by the
      operator (preferred when present), or
    * ``interval_sec`` — the aligned-bucket cadence baked into the
      manifest (legacy / fallback).

    In both cases the event fingerprint embeds the resolved tick
    so two ticks within the same bucket are deduplicated by the
    ``ON CONFLICT DO NOTHING`` on ``Event.fingerprint``.
    """
    name = cfg["name"]
    # Cron fields ("every day at 8:00") are evaluated against the
    # BUSINESS wall clock, not the pod's UTC, so "8am" means 8am where
    # the operator lives. The resolved tick is stored back in UTC for a
    # stable, tz-independent fingerprint.
    now_local = datetime.now(_business_tz())
    expr = (cfg.get("cron_expr") or "").strip()
    if expr:
        tick_local = _cron_last_tick(expr, now_local)
        if tick_local is None:
            logger.warning(
                "Watcher %s: cron_expr %r could not be parsed; skipping tick",
                name, expr,
            )
            return
        tick = tick_local.astimezone(timezone.utc)
    else:
        every = int(cfg.get("interval_sec") or 60)
        now = datetime.now(timezone.utc)
        bucket = int(now.timestamp() // every) * every
        tick = datetime.fromtimestamp(bucket, tz=timezone.utc)
    await _emit_event(
        session,
        type_=cfg.get("event_type") or "CRON",
        source=name,
        payload={"tick": tick.isoformat()},
    )


DISPATCH = {
    "row_count_delta": _watch_row_count_delta,
    "value_change":    _watch_value_change,
    "threshold":       _watch_threshold,
    "cron":            _watch_cron,
    "local_event":     _watch_local_event,
}


async def _load_schedules(session) -> dict[str, WatcherSchedule]:
    """Read the operator overrides table once per tick. The schedules
    table is small (≤ 10 rows) so a full scan is cheap and avoids races
    where the operator toggles a watcher between two queries."""
    rows = (await session.execute(select(WatcherSchedule))).scalars().all()
    return {row.watcher_name: row for row in rows}


def _is_enabled(name: str, schedules: dict[str, WatcherSchedule]) -> bool:
    sched = schedules.get(name)
    if sched is None:
        return True
    return bool(sched.enabled)


def _override_cfg(cfg: dict, sched: WatcherSchedule | None) -> dict:
    """Apply operator-defined cadence overrides to the manifest config.
    Only ``interval_sec`` and ``cron_expr`` are user-tunable; thresholds
    and table/column references stay frozen because changing them means
    the watcher is fundamentally a different watcher."""
    if sched is None:
        return cfg
    out = dict(cfg)
    if sched.interval_sec is not None and sched.interval_sec > 0:
        out["interval_sec"] = sched.interval_sec
    if sched.cron_expr:
        out["cron_expr"] = sched.cron_expr
    return out


async def _drain_scheduled_items(session):
    """SYSTEM watcher (not manifest-driven): scan the unified
    ``scheduled_items`` table for items whose due time has passed.

    * Items assigned to the AGENT → emit one ``SCHEDULED_TASK_DUE``
      event (the executor runs the agent to do the work and close the
      item) and flip the item to ``in_progress`` so it is not fired
      twice. The event payload carries the item id; closing happens via
      the ``complete_scheduled_item`` MCP tool from inside the run.
    * Items assigned to the USER (a reminder/task on a person's calendar) →
      MESSAGE that person on their preferred channel (WhatsApp/email) with what
      to do and mark the item ``done`` (v7.39.0). If they can't be reached (no
      channel), the item flips to ``overdue`` so the SPA highlights it. No agent
      run either way.

    Every emission is fingerprinted on (type, item id, due tick) so a
    pod restart never double-fires. Safe to call on every loop tick:
    the query is a cheap indexed range scan on local SQLite.
    """
    now = datetime.now(timezone.utc)
    # Lazy import to avoid a circular import at module load (business
    # imports timeutil → config; keep the watcher import graph light).
    from app.app_models.business import ScheduledItem

    due = (await session.execute(
        select(ScheduledItem).where(
            ScheduledItem.status.in_(("pending", "scheduled")),
            ScheduledItem.due_at.isnot(None),
            ScheduledItem.due_at <= now,
        ).limit(50)
    )).scalars().all()

    for item in due:
        if (item.assignee or "agent") == "agent":
            await _emit_event(
                session,
                type_=SCHEDULED_TASK_DUE_EVENT_TYPE,
                source=f"scheduled_item:{item.id}",
                payload={
                    "item_id": item.id,
                    "title": item.title,
                    "description": item.description or "",
                    "kind": item.kind,
                    "origin": item.origin,
                    "due_at": (item.due_at.replace(tzinfo=timezone.utc).isoformat()
                               if item.due_at and item.due_at.tzinfo is None
                               else (item.due_at.isoformat() if item.due_at else None)),
                    "created_by": item.created_by or "",
                },
            )
            item.status = "in_progress"
            item.fired_at = now
        else:
            # A person's reminder came due → message them on their preferred
            # channel with what to do, and close it. If we can't reach them
            # (no channel), leave it OVERDUE so the SPA highlights it.
            delivered = False
            try:
                from app.channels.notifier import dispatch_user_reminder
                delivered = await dispatch_user_reminder(item)
            except Exception:  # pragma: no cover — never break the drain
                logger.debug("user reminder failed for item %s", item.id, exc_info=True)
            if delivered:
                item.status = "done"
                item.fired_at = now
                item.completed_at = now.replace(tzinfo=None)
                item.result_md = "Recordatorio enviado al destinatario por su canal preferido."
            else:
                item.status = "overdue"
    if due:
        logger.info("scheduled drain: %d due item(s) processed", len(due))


def _aware(dt):
    """Treat a naive (stored) datetime as UTC for comparisons."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def _fire_automation(session, a, context: dict, now: datetime, *, bucket: str | None = None) -> bool:
    """Emit the ``automation_fired`` event the executor turns into a run.

    ``bucket`` is folded into the event fingerprint to dedup retries. Polled
    automations default to the minute (one fire per minute). Arrival-triggered
    automations (channel_message / business_event) pass a per-source-event
    bucket (``ev{event_id}``) so re-claiming the SAME triggering event never
    double-fires, regardless of when the retry lands.
    """
    bucket_v = bucket or now.strftime("%Y%m%d%H%M")
    # HARD APPROVAL GATE (deterministic, server-side): when the operator marked
    # this standing order "Requiere tu aprobación", NOTHING of the reaction runs
    # until they approve — regardless of HOW it fired (schedule / data / channel
    # / business event / manual Play). Instead of emitting the reaction event we
    # park a PENDING Approval; approving it (approvals_routes.approve) emits the
    # ``automation_fired`` event that actually runs the reaction. This is the one
    # choke point every fire path goes through, so the guarantee is uniform.
    if bool(getattr(a, "requires_approval", False)):
        # v7.63.0 CHECK-BEFORE-APPROVE — for a NATURAL-LANGUAGE (agent) condition
        # the watcher couldn't verify the prose before now, so parking the
        # approval here would ask the operator to approve BLIND. Instead run a
        # READ-ONLY verification first (automation_check event); its settle step
        # parks the approval ONLY if the condition holds, enriched with evidence.
        # data/sql/always conditions were already gated deterministically above,
        # so they still park the approval directly.
        from app import automations_service as _asvc
        if _asvc.wants_precheck(_asvc.row_to_dict(a)):
            return await _emit_condition_check(session, a, context, bucket_v)
        return await _park_automation_approval(session, a, context, bucket_v)
    # DETERMINISTIC reaction (0 tokens): a templated outcome (e.g. create_task) is
    # performed DIRECTLY here instead of emitting the run event. Only when it does
    # NOT require approval (approval already returned above). If not eligible
    # (done=False), fall through to the normal LLM run.
    from app import automations_service as _asvc_det
    _a_dict = _asvc_det.row_to_dict(a)
    if _a_dict.get("execution_mode") == "deterministic":
        try:
            done, detail = await _asvc_det.execute_deterministic_reaction(
                _a_dict, {"bucket": bucket_v, **(context or {})})
        except Exception:  # pragma: no cover — never break the loop; fall back to a run
            logger.warning("deterministic reaction failed for automation %s", a.id, exc_info=True)
            done = False
        if done:
            logger.info("Automation %s fired DETERMINISTICALLY (0 tokens): %s", a.id, detail)
            return True   # a fire happened → caller bumps fire state; no event/run
    payload = {
        "automation_id": a.id,
        "title": a.title,
        "context": context,
        "bucket": bucket_v,
    }
    # Dedup ONLY on (automation, bucket) — NOT the full payload, which folds in
    # a data/sql condition's volatile ``value`` (that would change between a
    # failed trigger event's retries and double-fire). Returns False when the
    # fire was deduped (no new event), so callers don't bump fire state.
    return await _emit_event(
        session, type_="automation_fired", source=f"automation:{a.id}",
        payload=payload, dedup_key=bucket_v,
    )


async def _park_automation_approval(
    session, a, context: dict, bucket_v: str, *,
    check_evidence: str | None = None, check_run_id: int | None = None,
    unconfirmed: bool = False,
) -> bool:
    """Create a PENDING Approval for a ``requires_approval`` automation instead
    of running its reaction. Idempotent on (automation, bucket) so retries and
    repeated polls never stack duplicate approvals. Returns True when a NEW
    approval was parked (so callers bump fire state), False when deduped.

    v7.63.0 — when the firing went through a read-only PRE-CHECK first
    (check-before-approve), ``check_evidence`` / ``check_run_id`` are threaded in
    so the operator sees WHY it fired (the concrete evidence the employee found)
    and approves an INFORMED decision. ``unconfirmed`` flags the rare case where
    the check ran but couldn't return a clear verdict (parked fail-safe)."""
    idem = f"autoappr:{a.id}:{bucket_v}"
    sig = a.signal_summary or a.signal_type or "señal"
    instr = (a.reaction_instructions or "").strip()
    lines = [
        f"La automatización «{a.title}» se ha disparado y **requiere tu aprobación** "
        "antes de que el empleado ejecute nada.",
        f"- Señal: {sig}",
        f"- Acción prevista: {a.reaction_type}",
    ]
    # Pre-check evidence: the employee ALREADY verified the condition (read-only)
    # before this approval — surface what it found so the operator approves an
    # informed, evidenced firing instead of a blind one.
    if check_evidence:
        cond_txt = ""
        try:
            from app import automations_service as asvc
            cond_txt = ((asvc._json(a.condition_spec) if a.condition_spec else {}).get("instructions") or "").strip()
        except Exception:
            cond_txt = ""
        header = ("\n⚠️ La comprobación previa se ejecutó pero **no pudo confirmar con "
                  "certeza** la condición; revísala antes de aprobar."
                  if unconfirmed else
                  "\n✅ El empleado ya **comprobó la condición** antes de pedirte "
                  "aprobación y se cumple:")
        lines.append(header)
        if cond_txt:
            lines.append(f"- Condición comprobada: {cond_txt if len(cond_txt) <= 300 else cond_txt[:300] + '…'}")
        lines.append("- Evidencia encontrada: " + (check_evidence if len(check_evidence) <= 700 else check_evidence[:700] + "…"))
    if instr:
        lines.append("\nInstrucciones de la automatización:\n" + (instr if len(instr) <= 500 else instr[:500] + "…"))
    # v7.35.0 disclosure — reactions that may touch external connectors: tell the
    # operator that ONE approval here also covers those writes (coalescence), so
    # they give an INFORMED single OK instead of being asked again per write.
    if (a.reaction_type or "") in ("act", "draft_and_approve", "generate_deliverable"):
        lines.append(
            "\n⚠️ Al aprobarla, el empleado la ejecutará **incluidas las escrituras "
            "en conectores externos** que implique (crear / enviar / actualizar) — "
            "no se te pedirá una segunda aprobación por cada una."
        )
    lines.append("\nApruébala para que el empleado la ejecute; recházala para que no haga nada.")
    payload = {
        "automation_id": a.id,
        "title": a.title,
        "context": context or {},
        "bucket": bucket_v,
    }
    stmt = sqlite_insert(Approval).values(
        idempotency_key=idem,
        created_by_run_id=None,
        title=str(a.title or "Automatización"),
        summary_md="\n".join(lines),
        action_type="automation_reaction",
        action_payload_json=json.dumps(payload, default=str),
        risk="medium",
        requested_for=None,
        status="pending",
    ).on_conflict_do_nothing(index_elements=[Approval.idempotency_key])
    result = await session.execute(stmt)
    inserted = bool(result.rowcount)
    if inserted:
        logger.info(
            "Automation %s fired but REQUIRES APPROVAL — parked approval (bucket=%s)",
            a.id, bucket_v,
        )
    return inserted


async def _emit_condition_check(session, a, context: dict, bucket_v: str) -> bool:
    """v7.63.0 — emit an ``automation_check`` event: a READ-ONLY verification the
    executor runs BEFORE parking the approval (check-before-approve). Its settle
    step (``automations_service.settle_condition_check``) reads the agent's
    verdict and either parks the approval (condition holds, enriched with the
    evidence) or logs a ``no_action`` decision (condition not met). Idempotent on
    (automation, bucket) like every other fire path. Returns True on a new event."""
    payload = {
        "automation_id": a.id,
        "title": a.title,
        "context": context or {},
        "bucket": bucket_v,
        "precheck": True,
    }
    return await _emit_event(
        session, type_="automation_check", source=f"automation:{a.id}",
        payload=payload, dedup_key=f"check:{bucket_v}",
    )


# Event types that must NEVER trigger an automation (they ARE the automation
# machinery or internal maintenance — matching them would loop).
#
# Self-amplification note: a ``business_event`` automation whose reaction writes
# a row that a local_event watcher turns back into the SAME event type can loop.
# We do NOT impose an implicit time floor (that silently drops legitimate
# high-frequency business events) — the throttle is the operator-set
# ``cooldown_sec``; see patterns/automations.md (set a cooldown on a
# self-emitting business_event rule).
_NON_TRIGGER_EVENT_TYPES = {"automation_fired", "automation_check", "memory_maintenance",
                            "self_memory_maintenance"}


async def match_arrival_automations(session, event: dict) -> None:
    """Fire enabled ``channel_message`` / ``business_event`` automations that
    match a just-arrived event.

    These two signals are NOT polled by ``_evaluate_automations`` (which only
    handles ``data_condition`` / ``schedule``); they are matched HERE, once,
    when the triggering event is claimed by the executor:

    * ``external_message`` → ``channel_message`` automations whose
      ``signal_spec.channel_id`` matches the event's channel (empty = any).
    * any other business event → ``business_event`` automations whose
      ``signal_spec.event_type`` equals the event ``type``.

    The agent CONDITION (the 2nd gate) and per-automation cooldown still apply,
    exactly like the polled path. Best-effort: one bad automation never blocks
    event processing.
    """
    from app.app_models.automations import Automation, AutomationState
    from app import automations_service as asvc

    etype = (event.get("type") or "").strip()
    if not etype or etype in _NON_TRIGGER_EVENT_TYPES:
        return
    want = "channel_message" if etype == "external_message" else "business_event"
    now = datetime.now(timezone.utc)

    autos = list((await session.execute(
        select(Automation).where(
            Automation.enabled == 1, Automation.signal_type == want
        )
    )).scalars().all())
    if not autos:
        return

    try:
        payload = json.loads(event.get("payload_json") or "{}")
    except Exception:
        payload = {}
    ev_id = event.get("id")
    ev_channel = payload.get("channel_id")

    for a in autos:
        try:
            # SAVEPOINT per automation: a failure here rolls back ONLY this one,
            # so earlier automations that already fired in this batch survive
            # (the batch is committed once by the caller). A plain
            # ``session.rollback()`` would discard the whole pending batch, and
            # since arrival matching runs BEFORE the triggering event's own run
            # — which then completes and is never re-claimed — those fires would
            # be lost forever (unlike the polled path, which self-heals).
            async with session.begin_nested():
                spec = asvc._json(a.signal_spec)
                if want == "channel_message":
                    want_ch = spec.get("channel_id")
                    # Empty channel_id = listen on every channel; otherwise scope.
                    if want_ch not in (None, "", 0) and str(want_ch) != str(ev_channel):
                        continue
                else:  # business_event: an empty event_type matches nothing (a
                       # standing order with no event configured must NOT fire on
                       # every internal event).
                    want_type = (spec.get("event_type") or "").strip()
                    if not want_type or want_type != etype:
                        continue

                st = (await session.execute(
                    select(AutomationState).where(AutomationState.automation_id == a.id)
                )).scalar_one_or_none()
                if st is None:
                    st = AutomationState(automation_id=a.id, state="armed")
                    session.add(st)
                    await session.flush()
                in_cooldown = bool(
                    a.cooldown_sec and st.last_fired_at
                    and (now - _aware(st.last_fired_at)).total_seconds() < a.cooldown_sec
                )
                if in_cooldown:
                    continue

                # Second gate: the agent's CONDITION (always/data/agent/sql).
                # Surface a bad condition on the state instead of silently never
                # firing.
                met, ctx = await asvc.evaluate_condition(asvc.row_to_dict(a))
                st.last_checked_at = now.replace(tzinfo=None)
                st.last_error = ctx.get("error")
                if ctx:
                    st.last_value_json = json.dumps(ctx, default=str)
                if not met:
                    continue

                fire_ctx = {
                    "trigger_event_id": ev_id,
                    "trigger_event_type": etype,
                    "channel_id": ev_channel,
                    **(ctx or {}),
                }
                # Per-source-event bucket so a retried claim of the same event is
                # a fingerprint no-op (no duplicate automation_fired). Only bump
                # fire state when an event was ACTUALLY emitted — a deduped retry
                # must not inflate fire_count / drift last_fired_at.
                fired = await _fire_automation(
                    session, a, fire_ctx, now, bucket=f"ev{ev_id}",
                )
                if fired:
                    st.fire_count = (st.fire_count or 0) + 1
                    st.last_fired_at = now.replace(tzinfo=None)
        except Exception:  # pragma: no cover — one bad automation never stalls
            # The SAVEPOINT already rolled back this automation's changes; the
            # outer batch (earlier automations) stays intact.
            logger.exception("arrival automation %s match failed", getattr(a, "id", "?"))


async def _evaluate_automations(session):
    """Evaluate enabled POLLED automations (data_condition + schedule) and fire
    them, with re-arm + cooldown. channel_message / business_event signals are
    matched when those events arrive (not polled); manual never auto-fires."""
    from app.app_models.automations import Automation, AutomationState
    from app import automations_service as asvc
    now = datetime.now(timezone.utc)
    autos = list((await session.execute(
        select(Automation).where(Automation.enabled == 1)
    )).scalars().all())
    for a in autos:
        if a.signal_type not in ("data_condition", "schedule"):
            continue
        try:
            st = (await session.execute(
                select(AutomationState).where(AutomationState.automation_id == a.id)
            )).scalar_one_or_none()
            if st is None:
                st = AutomationState(automation_id=a.id, state="armed")
                session.add(st)
                await session.flush()
            spec = asvc._json(a.signal_spec)
            in_cooldown = bool(
                a.cooldown_sec and st.last_fired_at
                and (now - _aware(st.last_fired_at)).total_seconds() < a.cooldown_sec
            )

            a_dict = asvc.row_to_dict(a)

            if a.signal_type == "data_condition":
                interval = max(int(a.check_interval_sec or 300), 30)
                if st.last_checked_at and (now - _aware(st.last_checked_at)).total_seconds() < interval:
                    continue
                # The agent's condition (legacy data_condition → signal_spec).
                met, ctx = await asvc.evaluate_condition(a_dict)
                st.last_checked_at = now.replace(tzinfo=None)
                st.last_value = ctx.get("value")
                st.last_value_json = json.dumps(ctx, default=str)
                # Surface a condition error (e.g. a SQL/data rule referencing a
                # column that doesn't exist) so it shows on the automation state
                # instead of silently never firing. Cleared (→ None) on success.
                st.last_error = ctx.get("error")
                fired_dc = bool(met and st.state == "armed" and not in_cooldown)
                if fired_dc:
                    await _fire_automation(session, a, ctx, now)
                    st.state = "triggered"
                    st.fire_count = (st.fire_count or 0) + 1
                    st.last_fired_at = now.replace(tzinfo=None)
                elif not met and st.state == "triggered" and a.rearm_on_recovery:
                    st.state = "armed"
                await asvc.record_eval(session, a.id, met=met, fired=fired_dc, ctx=ctx,
                                       mode=(a_dict.get("condition_spec") or {}).get("mode"))

            elif a.signal_type == "schedule":
                expr = (spec.get("cron") or "").strip()
                if not expr:
                    continue
                # Cron fields are a WALL CLOCK: "0 8 * * 1-5" means 08:00 in the
                # rule's timezone, not UTC. The timezone is the per-automation
                # ``signal_spec.tz`` (IANA name) when set, else the employee's
                # business tz. Resolve the tick there, then store it back in UTC
                # so the fire/dedup comparison stays tz-stable.
                from app.timeutil import tz_for
                rule_tz = tz_for(spec.get("tz"))
                tick_local = _cron_last_tick(expr, now.astimezone(rule_tz))
                if tick_local is None:
                    continue
                tick = tick_local.astimezone(timezone.utc)
                # First evaluation after (re)arming: SEED last_fired_at to now so
                # we don't retro-fire the most recent PAST tick (enabling a "daily
                # 08:00" rule at 14:00 must wait for tomorrow's 08:00, not fire
                # immediately). Subsequent ticks fire normally.
                if st.last_fired_at is None:
                    st.last_fired_at = now.replace(tzinfo=None)
                    st.last_checked_at = now.replace(tzinfo=None)
                    continue
                already = _aware(st.last_fired_at) >= tick
                st.last_checked_at = now.replace(tzinfo=None)
                if not already and not in_cooldown:
                    # On the scheduled tick the agent ALSO checks its condition
                    # (the second gate); fire only if it holds. Either way the
                    # tick is consumed (last_fired_at advances) so we don't
                    # re-evaluate this same past tick every loop.
                    met, ctx = await asvc.evaluate_condition(a_dict)
                    # PER-ITEM dedup if the automation has `watch` (NO-OP if it doesn't →
                    # identical behaviour): it fires only on NEW/escalated items and
                    # auto-resolves the ones that disappear. That is what stops a monitor every
                    # 5 min from re-alerting about the same thing. Generic (cluster, leads, tickets…).
                    should_fire, ctx = (await asvc.resolve_watch(session, a, ctx)) if met else (False, ctx)
                    st.last_value = ctx.get("value")
                    if ctx:
                        st.last_value_json = json.dumps(ctx, default=str)
                    st.last_error = ctx.get("error") or ctx.get("watch_error")  # diagnose
                    if should_fire:
                        await _fire_automation(
                            session, a, {"scheduled_for": tick.isoformat(), **(ctx or {})}, now,
                        )
                        st.fire_count = (st.fire_count or 0) + 1
                    st.last_fired_at = now.replace(tzinfo=None)
                    # Trace of the tick (incl. when it does NOT fire): history/traceability of the "only if".
                    await asvc.record_eval(session, a.id, met=met, fired=should_fire, ctx=ctx,
                                           mode=(a_dict.get("condition_spec") or {}).get("mode"))
        except Exception:  # pragma: no cover — one bad automation never stalls the loop
            # Rollback so a DBAPI error (e.g. a bad SQL condition) doesn't poison
            # the session for the remaining automations + the tick's final commit.
            await session.rollback()
            logger.exception("automation %s evaluation failed", getattr(a, "id", "?"))


async def _tick_once():
    # Master switch: when the agent's autonomous triggers are OFF (the default
    # for a freshly created agent), do nothing — no watcher fires and no
    # scheduled item is drained. Chat / channel replies / manual actions are
    # unaffected. The operator turns this on from Operations → Disparadores.
    from app.triggers_state import triggers_active
    if not await triggers_active():
        return
    # DELIBERATELY NOT ``app_write`` (INC-VIG-59). Everything else that writes was
    # moved into the serialized queue, but this session spans the WHOLE tick —
    # evaluating watcher units and automation conditions, which query the project DB
    # and can take seconds. Holding the write slot for that long would starve every
    # other writer (including the kill switch): the exact disease, with a new
    # carrier. Its writes are short commit-per-unit bursts at a low frequency (one
    # tick loop, not a worker pool), so with the loud writers now queued they land
    # in milliseconds. If this ever needs serializing, wrap each UNIT's commit —
    # never the tick.
    async with app_session() as session:
        # ONE session, but COMMIT PER UNIT (+ rollback on failure). A watcher
        # that raises a DBAPI error (e.g. a renamed/missing local table) poisons
        # the session; without rolling back, EVERY later watcher, the automation
        # pass and the final commit would fail on the poisoned transaction —
        # discarding the whole tick (incl. due agent-task firing) on every tick
        # until the bad watcher is fixed. Per-unit commit isolates the blast
        # radius to the failing unit only.

        # SYSTEM watcher first so a due agent-task fires promptly even if
        # a manifest watcher below raises.
        try:
            await _drain_scheduled_items(session)
            await session.commit()
        except Exception:  # pragma: no cover — never crash the loop
            await session.rollback()
            logger.exception("scheduled-items drain failed")
        schedules = await _load_schedules(session)
        for cfg in WATCHERS:
            name = cfg.get("name")
            if not _is_enabled(name, schedules):
                logger.debug("Watcher %s skipped (disabled by operator)", name)
                continue
            fn = DISPATCH.get(cfg["type"])
            if fn is None:
                logger.warning("Unknown watcher type %s", cfg["type"])
                continue
            try:
                await fn(session, _override_cfg(cfg, schedules.get(name)))
                await session.commit()
            except Exception:  # pragma: no cover — never crash the loop
                await session.rollback()
                logger.exception("Watcher %s failed", name)
        # Boss-authored automatizaciones (data_condition + schedule). A bad
        # automation rolls back internally (so it can't poison the rest); this
        # commit persists the survivors.
        try:
            await _evaluate_automations(session)
            await session.commit()
        except Exception:  # pragma: no cover — never crash the loop
            await session.rollback()
            logger.exception("automations evaluation failed")


async def run_forever():
    logger.info("Watcher loop starting (watchers=%d)", len(WATCHERS))
    while True:
        try:
            await _tick_once()
        except Exception:
            logger.exception("Watcher iteration failed")
        await asyncio.sleep(LOOP_SLEEP)
