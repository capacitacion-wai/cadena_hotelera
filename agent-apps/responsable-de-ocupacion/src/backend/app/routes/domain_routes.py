"""Generic domain-entity router driven by ``agent_app.domain_entities``.

Two endpoints per entity (list + detail) plus a single ``/api/domain/_meta``
that returns the manifest entries (used by the frontend to drive the
sidebar and the page configuration).

Entities have a ``source`` discriminator:

* ``project`` (default) — read-only against the project Postgres.
  The agent only **reads** from these tables.
* ``local`` — read against the agent-app local store. v6.4.0 limits
  this to **business-shaped** outputs the agent writes (alerts,
  recommendations, communications, scoring, …). The platform's
  operational tables (``events``, ``agent_runs``, ``tool_call_log``,
  ``event_audit``, ``watcher_cursor``, ``watcher_schedules``) are NEVER
  exposed here; they live under ``/events`` and ``/runs`` (audit
  pages). A guard at ``/api/domain/{slug}`` returns 404 if a runtime
  slug ever sneaks in via a stale ``wai-app.json``."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import bindparam, select, text

from app.app_database import app_session, app_write
# 14.18.0 — WHAT A MEMBER SEES. A member USES the employee (chat, manual, its own encargos);
# it does not operate it. These screens expose everyone's work (1,125 conversations of the
# whole team on the Panel of a member, 04/10/2026), so the door is HERE and the menu only
# reflects it.
from app.auth import TokenPayload, require_admin, require_project_access
from app.config import APP_MANIFEST, DB_SCHEMA
from app.database import async_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/domain", tags=["domain"])


# ── Timestamp normalisation at the API boundary ─────────────────────
#
# The SPA's date helpers (``utils/datetime.ts``) render every timestamp
# in the BUSINESS timezone, but ONLY if the backend ships it as ISO-8601
# WITH an explicit offset (``…+00:00`` / ``Z``) — same contract the runs
# and events endpoints honour via ``manifest_routes._iso_utc``. The
# generic domain rows are read with raw ``text()`` SQL and returned as
# ``dict(row._mapping)`` verbatim, so:
#
#   * Local (SQLite) rows: ``DateTime`` columns come back as a NAIVE
#     string ``"2026-06-22 11:00:00.123456"`` (no ``T``, no offset). The
#     browser's ``new Date()`` then reads those digits in the OPERATOR's
#     LOCAL timezone — off by their UTC offset. This is the "las fechas
#     salen confusas" bug for the agent-output entities.
#   * Project (Postgres) rows: ``timestamp`` columns come back as Python
#     ``datetime`` (often naive). Same problem once JSON-serialised.
#
# ``_to_iso_utc`` fixes both at the boundary: naive datetimes/strings are
# stamped as UTC (the store writes UTC), aware ones are converted to UTC,
# and the ISO output always carries ``+00:00`` so the SPA can localise it
# deterministically. Pure dates and non-date values pass through
# untouched.
_NAIVE_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?(\.\d+)?$")


def _to_iso_utc(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        # Pure calendar date — no time/zone component to localise.
        return value.isoformat()
    if isinstance(value, str) and _NAIVE_DT_RE.match(value):
        # Naive-UTC datetime string from the local store → make the UTC
        # offset explicit so the SPA renders it in the business tz.
        return value.replace(" ", "T") + "+00:00"
    return value


def _normalize_row(row: dict) -> dict:
    """Return ``row`` with every datetime value made offset-aware UTC so
    the SPA can localise it to the business timezone."""
    return {k: _to_iso_utc(v) for k, v in row.items()}

# Mirrors ``manifest_routes.RUNTIME_BLACKLIST`` — kept duplicated here
# so this module is independent of the manifest router.
_RUNTIME_SLUGS: set[str] = {
    "events",
    "agent_runs",
    "tool_call_log",
    "event_audit",
    "watcher_cursor",
    "watcher_schedules",
    "channels",
    "channel_messages",
    # v7.30.0 — work-coordination substrate with a dedicated home in
    # "Trabajo del empleado" (calendar, encargos, approvals). Not served
    # under /api/domain; their own routes (scheduling/approvals) own them.
    "scheduled_items",
    "approvals",
}


def _entities() -> list[dict]:
    raw = APP_MANIFEST.get("agent_app", {}).get("domain_entities", []) or []
    # Same filter as the manifest endpoint: runtime tables are not
    # exposed under /api/domain even if the static manifest still lists
    # them. ``/events`` and ``/runs`` own that surface.
    return [e for e in raw if not _is_runtime(e)]


def _is_runtime(entity: dict) -> bool:
    slug = (entity.get("slug") or "").strip().lower()
    table = (entity.get("table") or "").strip().lower()
    return slug in _RUNTIME_SLUGS or table in _RUNTIME_SLUGS


def _entity(slug: str) -> dict:
    if (slug or "").strip().lower() in _RUNTIME_SLUGS:
        # Defence-in-depth: even if the manifest still mentions it,
        # never serve runtime data through the domain endpoint. The
        # SPA's audit pages call ``/api/events`` / ``/api/runs`` for
        # this data.
        raise HTTPException(
            status_code=404,
            detail=(
                f"entity {slug!r} is a runtime table — query "
                f"/api/events or /api/runs instead"
            ),
        )
    for e in _entities():
        if e.get("slug") == slug:
            return e
    raise HTTPException(status_code=404, detail=f"unknown entity {slug!r}")


def _is_local(entity: dict) -> bool:
    return (entity.get("source") or "project") == "local"


def _qualify(table: str, *, local: bool = False) -> str:
    """Quote ``[schema.]table`` defensively. Project tables live under
    the tenant's data area schema; local tables live in the
    single-database app store and have no schema prefix."""
    if not table.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail=f"invalid table {table!r}")
    if local:
        return f'"{table}"'
    return f'"{DB_SCHEMA}"."{table}"'


def _session_for(entity: dict):
    """Return the right async sessionmaker for the entity. Local
    entities point at the SPA's own writable store, project entities
    at the read-only project Postgres."""
    return app_session if _is_local(entity) else async_session


# ─────────────────────────────────────────────────────────────────────
# Lifecycle — operator-managed state transitions on LOCAL entities.
#
# Generic principle: when an agent-output row carries a lifecycle
# state owned by the OPERATOR (triage an alert, accept a
# recommendation, approve a draft communication), the app must let
# the operator transition it — a status column nobody can change is
# decoration, not workflow. States owned by the SYSTEM (a
# communication's ``sent``/``failed``, a scoring band) expose no
# operator transitions.
#
# The canonical agent-output tables get built-in lifecycles below; a
# generated app can override them — or give a CUSTOM local table its
# own lifecycle — by declaring ``lifecycle`` on the manifest entity:
#
#   "lifecycle": {
#     "field": "status",
#     "transitions": {"open": ["closed"], "closed": ["open"]},
#     "stamps": {"closed": {"at": "closed_at", "by": "closed_by"}},
#     "clear_stamps_on": ["open"]
#   }
#
# ``stamps`` columns are set (UTC now / operator email) when ENTERING
# the state; entering a ``clear_stamps_on`` state nulls every stamp
# column so a reopened row doesn't keep a stale "resolved_by".
# ─────────────────────────────────────────────────────────────────────

# v7.40.0 — UNIFIED lifecycle across the three employee outputs (alerts,
# recommendations, decisions). Same state VALUES + same transition MODEL, so the
# surfaces, the state diagram and the filter chips are homogeneous:
#   open → acknowledged → in_progress → done ; dismissed (terminal ✗).
# The «Convertir en encargo» action (to_task) moves any of them to in_progress and
# creates the encargo; when the encargo closes, settle_source_on_complete flips
# the source to done. Classification (severity / priority / decision-type) stays
# a SEPARATE attribute, not a state.
_UNIFIED_TRANSITIONS: dict[str, list[str]] = {
    "open":         ["acknowledged", "in_progress", "done", "dismissed"],
    "acknowledged": ["in_progress", "done", "dismissed", "open"],
    "in_progress":  ["done", "dismissed"],
    "done":         ["open"],
    "dismissed":    ["open"],
}

DEFAULT_LIFECYCLES: dict[str, dict] = {
    "alerts": {
        "field": "status",
        "transitions": _UNIFIED_TRANSITIONS,
        "stamps": {
            "done":      {"at": "resolved_at", "by": "resolved_by"},
            "dismissed": {"at": "resolved_at", "by": "resolved_by"},
        },
        "clear_stamps_on": ["open"],
    },
    "recommendations": {
        "field": "status",
        "transitions": _UNIFIED_TRANSITIONS,
        "stamps": {
            "acknowledged": {"at": "decided_at", "by": "decided_by"},
            "done":         {"at": "decided_at", "by": "decided_by"},
            "dismissed":    {"at": "decided_at", "by": "decided_by"},
        },
        "clear_stamps_on": ["open"],
    },
    "decisions": {
        "field": "status",
        "transitions": _UNIFIED_TRANSITIONS,
        "stamps": {},
        "clear_stamps_on": ["open"],
    },
    # ``communications`` deliberately has NO lifecycle: in this agent the
    # status is purely INFORMATIVE (the channel executor / notifier sets
    # draft→ready→sent/failed automatically as it actually delivers). The
    # operator must NOT be able to flip it by hand, so we expose no
    # transitions — the SPA shows the status column read-only and the
    # lifecycle PATCH/bulk endpoints return 404 for this entity.
}


def lifecycle_for(entity: dict) -> dict | None:
    """Resolve the lifecycle for a LOCAL entity: explicit manifest
    declaration wins, then the built-in default for canonical tables.
    Project entities never have one (read-only by design)."""
    if not _is_local(entity):
        return None
    lc = entity.get("lifecycle") or DEFAULT_LIFECYCLES.get((entity.get("table") or "").lower())
    if not lc or not isinstance(lc.get("transitions"), dict):
        return None
    field = lc.get("field") or "status"
    if not field.replace("_", "").isalnum():
        return None
    states = sorted({*lc["transitions"].keys(),
                     *(s for nxt in lc["transitions"].values() for s in nxt)})
    return {
        "field": field,
        "states": states,
        "transitions": lc["transitions"],
        "stamps": lc.get("stamps") or {},
        "clear_stamps_on": lc.get("clear_stamps_on") or [],
    }


def attach_lifecycle(entity: dict) -> dict:
    """Return the entity enriched with its resolved ``lifecycle`` (or
    without the key when none applies). Used by both ``/_meta`` and
    the agent manifest endpoint so the SPA always knows which
    transitions to offer."""
    lc = lifecycle_for(entity)
    if lc is None:
        return {k: v for k, v in entity.items() if k != "lifecycle"}
    return {**entity, "lifecycle": lc}


# ─────────────────────────────────────────────────────────────────────
# Auto-derived filters for LOCAL (agent-activity) entities (v6.32.0)
#
# Agent-output tables are meant to be WORKED with — triaged, filtered,
# bulk-updated. Authoring a ``filters[]`` block by hand for every one is
# tedious and easy to forget, so for local entities we synthesise the
# obvious filters from the columns the entity actually exposes: the
# lifecycle status, the canonical severity/priority/decision enums and a
# created_at date range. Author-declared filters always win (we never add
# a filter whose id OR (column,kind) already exists), so this is additive
# and override-safe — no overfitting to a specific app.
# ─────────────────────────────────────────────────────────────────────

_SEVERITIES = ["critical", "high", "medium", "low", "info"]
_PRIORITIES = ["urgent", "high", "medium", "low"]
_DECISIONS = ["no_action", "deferred", "escalated"]
# v7.41.0 — the separated policy axes (see app.signal_policy) become auto-filters
# too, so any agent-output table exposing them is filterable without hand-authoring.
_IMPACTS = ["critical", "high", "medium", "low"]
_URGENCIES = ["immediate", "high", "medium", "low"]
_CONFIDENCES = ["high", "medium", "low"]
try:  # keep the reason-code list in lockstep with the runtime catalog
    from app.signal_policy import REASON_CODES as _RC
    _REASON_CODES = list(_RC.keys())
except Exception:  # pragma: no cover
    _REASON_CODES = ["below_threshold", "insufficient_evidence", "duplicate",
                     "out_of_scope", "needs_human", "already_handled",
                     "deferred_timing", "awaiting_data", "no_action_needed"]

# When an alert becomes an encargo (to_task), carry its urgency across: a
# critical alert produces an urgent encargo, etc. Recommendations/decisions have
# no severity and fall back to the default ("medium").
_SEVERITY_TO_PRIORITY = {
    "critical": "urgent", "high": "high", "medium": "medium",
    "low": "low", "info": "low",
}


def _filter_dicts(entity: dict) -> list[dict]:
    """Author-declared filters, defensively limited to well-formed dict
    entries. A hand-authored ``wai-app.json`` sometimes declares
    ``filters`` as a list of bare strings (e.g. ``["status", "severity"]``)
    instead of objects; those malformed entries are ignored so the manifest
    endpoint keeps working instead of 500-ing on ``f.get(...)``."""
    return [f for f in (entity.get("filters") or []) if isinstance(f, dict)]


def _entity_columns(entity: dict) -> set[str]:
    """Physical columns the entity is known to expose (from list_columns
    + declared filters + search + sort + primary key)."""
    cols: set[str] = {entity.get("primary_key", "id")}
    for c in entity.get("list_columns", []) or []:
        if isinstance(c, dict) and c.get("column"):
            cols.add(c["column"])
    for f in _filter_dicts(entity):
        if f.get("column"):
            cols.add(f["column"])
    for c in entity.get("search_columns", []) or []:
        cols.add(c)
    sort = entity.get("default_sort") or {}
    if sort.get("column"):
        cols.add(sort["column"])
    return cols


def _auto_filters(entity: dict, lc: dict | None) -> list[dict]:
    """Build the auto filters for a local entity, skipping any the
    author already declared (matched by id or by column+kind)."""
    existing = _filter_dicts(entity)
    existing_ids = {f.get("id") for f in existing}
    existing_colkind = {(f.get("column"), f.get("kind")) for f in existing}
    cols = _entity_columns(entity)
    out: list[dict] = []

    def add(fid: str, label: str, label_en: str, column: str, kind: str,
            options: list[str] | None = None) -> None:
        if column not in cols:
            return
        if fid in existing_ids or (column, kind) in existing_colkind:
            return
        f = {"id": fid, "label": label, "label_en": label_en,
             "column": column, "kind": kind, "auto": True}
        if options is not None:
            f["options"] = options
        out.append(f)
        existing_ids.add(fid)
        existing_colkind.add((column, kind))

    if lc is not None:
        add("status", "Estado", "Status", lc["field"], "enum", lc["states"])
    elif "status" in cols:
        # Outbox-shaped entities (communications) carry an operator-controlled
        # ``status`` but NO lifecycle — still make it filterable so a surface can
        # page each status lane SERVER-SIDE. Without this the Outbox loaded every
        # row and grouped client-side, which does not scale to thousands of
        # sent/failed communications.
        add("status", "Estado", "Status", "status", "enum",
            ["draft", "ready", "sent", "failed"])
    add("severity", "Severidad", "Severity", "severity", "enum", _SEVERITIES)
    add("priority", "Prioridad", "Priority", "priority", "enum", _PRIORITIES)
    # v7.41.0 separated policy axes (impact/urgency/confidence) + decision reason.
    add("impact", "Impacto", "Impact", "impact", "enum", _IMPACTS)
    add("urgency", "Urgencia", "Urgency", "urgency", "enum", _URGENCIES)
    add("confidence", "Confianza", "Confidence", "confidence", "enum", _CONFIDENCES)
    add("decision", "Decisión", "Decision", "decision", "enum", _DECISIONS)
    add("reason_code", "Motivo", "Reason", "reason_code", "enum", _REASON_CODES)
    add("created_from", "Desde", "From", "created_at", "date_from")
    add("created_to", "Hasta", "To", "created_at", "date_to")
    return out


def enrich_entity(entity: dict) -> dict:
    """Attach the resolved lifecycle AND the auto-derived filters for
    local entities. Author-declared filters keep priority and come
    first; synthesised ones are appended. Used by ``/_meta``, the agent
    manifest endpoint and the list route so the SPA and the query layer
    agree on what is filterable."""
    enriched = attach_lifecycle(entity)
    # Sanitise author-declared filters once (drop any non-dict entries from a
    # malformed manifest) so neither the auto-filter synthesis nor the SPA
    # chokes on bare-string filters.
    declared = _filter_dicts(enriched)
    if _is_local(enriched):
        auto = _auto_filters(enriched, enriched.get("lifecycle"))
        return {**enriched, "filters": [*declared, *auto]}
    if "filters" in enriched:
        return {**enriched, "filters": declared}
    return enriched


def _allowed_columns(entity: dict) -> set[str]:
    cols: set[str] = {entity.get("primary_key", "id")}
    for col in entity.get("list_columns", []) or []:
        if isinstance(col, dict) and col.get("column"):
            cols.add(col["column"])
    for col in entity.get("search_columns", []) or []:
        cols.add(col)
    for f in _filter_dicts(entity):
        if f.get("column"):
            cols.add(f["column"])
    sort = entity.get("default_sort") or {}
    if sort.get("column"):
        cols.add(sort["column"])
    return cols


@router.get("/_meta")
async def meta(_: TokenPayload = Depends(require_admin)):
    return {"entities": [enrich_entity(e) for e in _entities()]}


def _stamp_cols(lc: dict) -> set[str]:
    cols: set[str] = set()
    for cfg in lc["stamps"].values():
        for key in ("at", "by"):
            col = cfg.get(key)
            if col and col.replace("_", "").isalnum():
                cols.add(col)
    return cols


def _transition_set_clause(lc: dict, new_state: str, user_email: str) -> tuple[list[str], dict]:
    """Build the SET fragment + params for entering ``new_state`` (the
    lifecycle field plus any stamp/clear-stamp columns). Shared by the
    single-row and bulk transition endpoints so they never diverge.
    Does NOT include the WHERE/pk binding."""
    from datetime import datetime as _dt
    field = lc["field"]
    sets = [f'"{field}" = :new_state']
    params: dict[str, Any] = {"new_state": new_state}
    stamp_cols = _stamp_cols(lc)
    stamp = lc["stamps"].get(new_state)
    if stamp:
        at_col, by_col = stamp.get("at"), stamp.get("by")
        if at_col and at_col in stamp_cols:
            sets.append(f'"{at_col}" = :__stamp_at')
            params["__stamp_at"] = _dt.utcnow()
        if by_col and by_col in stamp_cols:
            sets.append(f'"{by_col}" = :__stamp_by')
            params["__stamp_by"] = user_email
    elif new_state in lc["clear_stamps_on"]:
        for col in sorted(stamp_cols):
            sets.append(f'"{col}" = NULL')
    return sets, params


@router.patch("/{slug}/{pk}")
async def transition(
    slug: str,
    pk: str,
    body: dict,
    user: TokenPayload = Depends(require_admin),
):
    """Operator-driven lifecycle transition on a LOCAL entity row.

    Body: ``{"status": "<new_state>", "comment": "<optional note>"}`` (the
    status key is always ``status`` regardless of the physical column — the
    lifecycle declares the real field). Validates the transition against the
    entity's lifecycle and stamps audit columns (who/when) where declared. An
    optional ``comment`` is recorded on the entity's comment thread with the
    ``from``/``to`` states, so the thread reads as an annotated WHY-log of every
    state change. Project entities are rejected: the agent-app never writes to
    project data, and neither does the operator through this surface.
    """
    entity = _entity(slug)
    if not _is_local(entity):
        raise HTTPException(status_code=403, detail="project entities are read-only")
    lc = lifecycle_for(entity)
    if lc is None:
        raise HTTPException(status_code=404, detail=f"entity {slug!r} has no lifecycle")

    new_state = str(body.get("status") or body.get("value") or "").strip()
    if new_state not in lc["states"]:
        raise HTTPException(
            status_code=422,
            detail=f"unknown state {new_state!r} — expected one of {lc['states']}",
        )

    pk_col = entity.get("primary_key", "id")
    field = lc["field"]
    table = _qualify(entity["table"], local=True)

    async with app_write() as s:
        res = await s.execute(
            text(f'SELECT * FROM {table} WHERE CAST("{pk_col}" AS TEXT) = :pk LIMIT 1'),
            {"pk": pk},
        )
        row = res.first()
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        record = dict(row._mapping)  # noqa: SLF001

        current = str(record.get(field) or "")
        allowed_next = lc["transitions"].get(current)
        # Unknown current state (agent wrote a value outside the
        # catalogue): allow any declared state rather than dead-locking
        # the row — the operator is the one cleaning it up.
        if allowed_next is not None and new_state not in allowed_next:
            raise HTTPException(
                status_code=409,
                detail=f"cannot move {current!r} → {new_state!r}; allowed: {allowed_next}",
            )

        sets, params = _transition_set_clause(lc, new_state, user.email)
        params["pk"] = pk

        await s.execute(
            text(
                f'UPDATE {table} SET {", ".join(sets)} '
                f'WHERE CAST("{pk_col}" AS TEXT) = :pk'
            ),
            params,
        )
        # Optional note attached to THIS state change → recorded on the
        # entity's comment thread (same transaction) with from/to states.
        comment_text = str(body.get("comment") or "").strip()
        if comment_text:
            from app.app_models.runtime import EntityComment
            s.add(EntityComment(
                entity_slug=slug,
                entity_pk=str(pk),
                author=user.email or "",
                body=comment_text,
                from_status=current,
                to_status=new_state,
            ))
        await s.commit()

        res = await s.execute(
            text(f'SELECT * FROM {table} WHERE CAST("{pk_col}" AS TEXT) = :pk LIMIT 1'),
            {"pk": pk},
        )
        updated = _normalize_row(dict(res.first()._mapping))  # noqa: SLF001

    logger.info(
        "lifecycle: %s/%s %s: %r → %r by %s",
        slug, pk, field, current, new_state, user.email,
    )
    return {"entity": slug, "record": updated}


# ── Business actions (archetype verbs) ──────────────────────────────
# v7.40.0 — after the state unification the ONLY cross-entity verb left here is
# ``to_task`` («Convertir en encargo»): it works for an ALERT, a RECOMMENDATION
# or a DECISION — it creates the encargo the employee runs and moves the source
# to ``in_progress`` (when the encargo closes, settle_source_on_complete flips it
# to ``done``). Everything else is a plain lifecycle transition (PATCH above:
# acknowledge / done / dismiss). The old escalate (alert→recommendation),
# promote (decision→recommendation/encargo) and review verbs are GONE. Outbox
# release/send/retry/cancel stay (communications status is not a lifecycle).

def _json_obj(raw) -> dict:
    """Parse a metadata JSON string (or dict) into a dict. Never raises."""
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


_COMM_STATUS_ACTIONS = {  # action → (required current statuses, new status | None, dispatch?)
    # release/retry move INTO ready and then deliver; send delivers a comm that
    # is ALREADY ready (the common case — the agent stages comms as ready, so
    # without this they had no way to leave the Outbox); cancel just parks it.
    "release": ({"draft"}, "ready", True),
    "send":    ({"ready"}, None, True),
    "retry":   ({"failed"}, "ready", True),
    "cancel":  ({"ready"}, "draft", False),
}


@router.post("/{slug}/{pk}/action")
async def entity_action(
    slug: str,
    pk: str,
    body: dict = Body(default_factory=dict),
    user: TokenPayload = Depends(require_admin),
):
    """Run a business action on a LOCAL entity row. Body: ``{"action": "...",
    ...payload}``. Never writes project data; unknown actions are rejected."""
    import uuid as _uuid
    from datetime import datetime as _dt

    entity = _entity(slug)
    if not _is_local(entity):
        raise HTTPException(status_code=403, detail="project entities are read-only")
    action = str(body.get("action") or "").strip().lower()
    table = _qualify(entity["table"], local=True)
    pk_col = entity.get("primary_key", "id")

    async with app_write() as s:
        row = (await s.execute(
            text(f'SELECT * FROM {table} WHERE CAST("{pk_col}" AS TEXT) = :pk LIMIT 1'),
            {"pk": pk},
        )).first()
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        rec = dict(row._mapping)  # noqa: SLF001

        # ---- outbox: release / send / retry / cancel (a status gate, not lifecycle)
        if action in _COMM_STATUS_ACTIONS:
            required, new_status, do_dispatch = _COMM_STATUS_ACTIONS[action]
            current = str(rec.get("status") or "").strip().lower()
            if current not in required:
                raise HTTPException(
                    status_code=409,
                    detail=f"'{action}' requires status in {sorted(required)}, got {current!r}",
                )
            if new_status is not None:
                await s.execute(
                    text(f'UPDATE {table} SET "status" = :st WHERE CAST("{pk_col}" AS TEXT) = :pk'),
                    {"st": new_status, "pk": pk},
                )
                await s.commit()
                logger.info("action %s: %s/%s → status %s by %s", action, slug, pk, new_status, user.email)
            # v7.37.0 — a manual send must DELIVER now. `release`/`retry` move the
            # row INTO ready first; `send` delivers a comm that is ALREADY ready
            # (the agent's default staging state). The run-scoped drain only fires
            # at the end of an agent run, so without this a pending communication
            # would sit unsent forever. dispatch_communication() flips the row to
            # sent/failed. Only for a real communications row (has a channel).
            delivery = None
            if do_dispatch and str(rec.get("channel") or "").strip():
                try:
                    from app.channels.notifier import dispatch_communication
                    delivery = await dispatch_communication(int(rec.get(pk_col)))
                except Exception:  # noqa: BLE001 — status already changed; report best-effort
                    logger.exception("release/send dispatch failed for %s/%s", slug, pk)
                    delivery = {"status": "error"}
            final_status = (
                str((delivery or {}).get("status") or "") if (do_dispatch and delivery)
                else (new_status or current)
            ) or (new_status or current)
            return {"entity": slug, "action": action, "status": final_status,
                    **({"delivery": delivery} if delivery is not None else {})}

        # ---- UNIFIED TO_TASK (alert / recommendation / decision) → create the
        # encargo the employee runs NOW and move the source to «en curso». -------
        # (v7.40.0) The three "generation" archetypes share ONE cross-entity verb:
        # an operator who wants the employee to act converts the signal into an
        # encargo. The source flips to ``in_progress`` and is traced to the encargo
        # (``metadata.encargo_id``); when the employee closes the encargo it flips
        # to ``done`` with the outcome (settle_source_on_complete). This replaces
        # the old escalate (alert→recommendation) / promote (decision→encargo) /
        # review verbs — an alert now goes straight to encargo, never via a
        # recommendation.
        if action == "to_task":
            from app.app_models.business import ScheduledItem
            from app.routes.scheduling_routes import _emit_delegated_task
            meta = _json_obj(rec.get("metadata_json"))
            if meta.get("encargo_id"):
                raise HTTPException(
                    status_code=409,
                    detail=f"already converted to encargo {meta['encargo_id']}",
                )
            # v7.44.0 — the agent decides, at creation, whether this item warrants
            # an encargo (``metadata.task.allow``). If it never opted in, this
            # signal does not generate work → refuse (the SPA also hides the
            # button, this is defence in depth).
            task_spec = meta.get("task") or {}
            if not task_spec.get("allow"):
                raise HTTPException(
                    status_code=422,
                    detail="este ítem no genera encargo (allow_task no está activado)",
                )
            # The associated encargo the AGENT defined (may differ from the signal
            # itself); fall back to the item's own title/description when absent.
            first_line = (str(rec.get("reason_md") or rec.get("reason") or "").splitlines()[:1] or [""])[0]
            title = (str(task_spec.get("title") or "").strip()
                     or rec.get("title") or first_line or f"{slug} #{pk}")[:200]
            desc = str(task_spec.get("description") or "").strip() or str(
                rec.get("rationale_md") or rec.get("rationale")
                or rec.get("reason_md") or rec.get("reason")
                or rec.get("description") or "")
            # The operator may COMPLEMENT the encargo with extra instructions — a
            # free-text note appended (never replacing the agent's description).
            note = str(body.get("note") or "").strip()
            if note:
                desc = (desc + "\n\n" if desc else "") + f"Indicaciones del operador:\n{note}"
            now_naive = _dt.utcnow()
            item = ScheduledItem(
                idempotency_key=f"task-{slug}-{pk}-{_uuid.uuid4().hex[:12]}",
                title=f"Encargo: {title}"[:200], description=desc, kind="task",
                assignee="agent", origin="user", status="in_progress",
                priority=_SEVERITY_TO_PRIORITY.get(str(rec.get("severity") or "").lower(), "medium"),
                due_at=None, created_by=user.email, fired_at=now_naive,
                metadata_json=json.dumps(
                    {"from": {"entity": slug, "table": entity.get("table"), "pk": pk, "action": "to_task"}},
                    default=str),
            )
            s.add(item)
            await s.commit()
            await s.refresh(item)
            event_id = await _emit_delegated_task(s, item, user.email)
            # Move the SOURCE to «en curso» and trace the encargo. The three
            # canonical tables all carry a ``status`` column after v7.40.0; the
            # ``decided_at``/``decided_by`` stamps are written best-effort (only if
            # the column exists on this table).
            if "status" in rec:
                meta.update({"encargo_id": item.id})
                sets = '"status" = :st, "metadata_json" = :meta'
                params: dict[str, Any] = {"st": "in_progress",
                                          "meta": json.dumps(meta, default=str), "pk": pk}
                if "decided_at" in rec:
                    sets += ', "decided_at" = :at'
                    params["at"] = now_naive
                if "decided_by" in rec:
                    sets += ', "decided_by" = :by'
                    params["by"] = user.email
                await s.execute(
                    text(f'UPDATE {table} SET {sets} WHERE CAST("{pk_col}" AS TEXT) = :pk'), params)
                await s.commit()
            logger.info("action to_task: %s/%s → encargo %s (event=%s) by %s", slug, pk, item.id, event_id, user.email)
            return {"entity": slug, "action": action, "created": {"scheduled_items": item.id}, "event_id": event_id or None}

    raise HTTPException(status_code=422, detail=f"unknown action {action!r}")


@router.post("/{slug}/delete")
async def delete_rows(
    slug: str,
    body: dict = Body(default_factory=dict),
    user: TokenPayload = Depends(require_admin),
):
    """Delete one or MANY LOCAL entity rows — the operator cleans the agent's
    outputs (alerts / recommendations / decisions …). Body: ``{"ids": [...]}``
    (one id = single delete, many = bulk). LOCAL entities only; a COMMUNICATION
    is deletable ONLY while it is a ``draft`` (a sent/ready/failed message stays
    for the audit trail). Returns ``{deleted, skipped}``."""
    entity = _entity(slug)
    if not _is_local(entity):
        raise HTTPException(status_code=403, detail="project entities are read-only")
    raw_ids = body.get("ids")
    ids = [str(x) for x in raw_ids if str(x).strip()] if isinstance(raw_ids, list) else []
    if not ids:
        raise HTTPException(status_code=422, detail="ids must be a non-empty list")

    table = _qualify(entity["table"], local=True)
    pk_col = entity.get("primary_key", "id")
    is_comm = (entity.get("table") or "").lower() == "communications"
    deleted = 0
    skipped = 0
    async with app_write() as s:
        # Communications: only drafts are deletable — protect the audit trail of
        # anything the employee already sent / tried to send.
        if is_comm:
            rows = (await s.execute(
                text(f'SELECT CAST("{pk_col}" AS TEXT) AS pk, "status" AS st FROM {table} '
                     f'WHERE CAST("{pk_col}" AS TEXT) IN :ids')
                .bindparams(bindparam("ids", expanding=True)),
                {"ids": ids},
            )).all()
            deletable = [r._mapping["pk"] for r in rows if str(r._mapping["st"] or "").lower() == "draft"]  # noqa: SLF001
            skipped = len(ids) - len(deletable)
            ids = deletable
        if ids:
            res = await s.execute(
                text(f'DELETE FROM {table} WHERE CAST("{pk_col}" AS TEXT) IN :ids')
                .bindparams(bindparam("ids", expanding=True)),
                {"ids": ids},
            )
            deleted = res.rowcount if (res.rowcount or 0) >= 0 else len(ids)
        await s.commit()
    logger.info("delete %s: %d deleted, %d skipped by %s", slug, deleted, skipped, user.email)
    return {"entity": slug, "deleted": deleted, "skipped": skipped}


# ── Comment thread (per entity row) ─────────────────────────────────
# A decoupled note thread for ANY known domain-entity row. Most useful on
# lifecycle entities (alerts, recommendations): a transition can attach a
# comment (PATCH above), and operators can add free-standing notes here. Stored
# in ``entity_comments`` keyed by (slug, pk) — never touches the agent-written
# entity table.

def _comment_dict(c) -> dict:
    return {
        "id": c.id,
        "author": c.author or "",
        "body": c.body or "",
        "from_status": c.from_status,
        "to_status": c.to_status,
        "created_at": _to_iso_utc(c.created_at),
    }


@router.get("/{slug}/{pk}/comments")
async def list_comments(
    slug: str, pk: str, user: TokenPayload = Depends(require_admin),
):
    """Comment thread for one entity row, oldest first (reads top→bottom)."""
    _entity(slug)  # 404s an unknown slug
    from app.app_models.runtime import EntityComment
    async with app_session() as s:
        rows = (await s.execute(
            select(EntityComment)
            .where(
                EntityComment.entity_slug == slug,
                EntityComment.entity_pk == str(pk),
            )
            .order_by(EntityComment.created_at.asc(), EntityComment.id.asc())
        )).scalars().all()
    return {"items": [_comment_dict(c) for c in rows]}


@router.post("/{slug}/{pk}/comments")
async def add_comment(
    slug: str, pk: str, body: dict,
    user: TokenPayload = Depends(require_admin),
):
    """Add a free-standing comment (no state change) to an entity row."""
    _entity(slug)
    text_ = str(body.get("body") or body.get("comment") or "").strip()
    if not text_:
        raise HTTPException(status_code=422, detail="comment body is required")
    from app.app_models.runtime import EntityComment
    async with app_write() as s:
        c = EntityComment(
            entity_slug=slug, entity_pk=str(pk),
            author=user.email or "", body=text_,
        )
        s.add(c)
        await s.commit()
        await s.refresh(c)
        return _comment_dict(c)


_BULK_MAX = 500


@router.post("/{slug}/bulk")
async def bulk_transition(
    slug: str,
    body: dict,
    user: TokenPayload = Depends(require_admin),
):
    """Apply ONE lifecycle transition to MANY rows of a LOCAL entity.

    Body: ``{"ids": [<pk>, …], "status": "<new_state>"}``. Each row is
    validated against the lifecycle independently — rows that are not
    found, already in ``new_state`` or whose current state forbids the
    move are reported under ``skipped`` (with a reason) instead of
    failing the whole batch. Valid rows are updated in a single
    statement and get the same audit stamps as the per-row endpoint.
    Capped at ``_BULK_MAX`` ids per call to keep the statement bounded.
    """
    entity = _entity(slug)
    if not _is_local(entity):
        raise HTTPException(status_code=403, detail="project entities are read-only")
    lc = lifecycle_for(entity)
    if lc is None:
        raise HTTPException(status_code=404, detail=f"entity {slug!r} has no lifecycle")

    new_state = str(body.get("status") or body.get("value") or "").strip()
    if new_state not in lc["states"]:
        raise HTTPException(
            status_code=422,
            detail=f"unknown state {new_state!r} — expected one of {lc['states']}",
        )

    raw_ids = body.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise HTTPException(status_code=422, detail="ids must be a non-empty list")
    ids: list[str] = []
    seen: set[str] = set()
    for x in raw_ids:
        sx = str(x)
        if sx and sx not in seen:
            seen.add(sx)
            ids.append(sx)
    if len(ids) > _BULK_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"too many ids ({len(ids)}); max {_BULK_MAX} per call",
        )

    pk_col = entity.get("primary_key", "id")
    field = lc["field"]
    table = _qualify(entity["table"], local=True)

    updated: list[str] = []
    skipped: list[dict] = []

    async with app_write() as s:
        res = await s.execute(
            text(
                f'SELECT CAST("{pk_col}" AS TEXT) AS __pk, "{field}" AS __state '
                f'FROM {table} WHERE CAST("{pk_col}" AS TEXT) IN :ids'
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        )
        state_by_pk = {
            r._mapping["__pk"]: str(r._mapping["__state"] or "")  # noqa: SLF001
            for r in res
        }

        valid: list[str] = []
        for pk in ids:
            if pk not in state_by_pk:
                skipped.append({"id": pk, "reason": "not_found"})
                continue
            current = state_by_pk[pk]
            if current == new_state:
                skipped.append({"id": pk, "reason": "noop"})
                continue
            allowed_next = lc["transitions"].get(current)
            if allowed_next is not None and new_state not in allowed_next:
                skipped.append({"id": pk, "reason": "invalid_transition", "from": current})
                continue
            valid.append(pk)

        if valid:
            sets, params = _transition_set_clause(lc, new_state, user.email)
            params["ids"] = valid
            await s.execute(
                text(
                    f'UPDATE {table} SET {", ".join(sets)} '
                    f'WHERE CAST("{pk_col}" AS TEXT) IN :ids'
                ).bindparams(bindparam("ids", expanding=True)),
                params,
            )
            await s.commit()
            updated = valid

    logger.info(
        "lifecycle bulk: %s → %r updated=%d skipped=%d by %s",
        slug, new_state, len(updated), len(skipped), user.email,
    )
    return {
        "entity": slug,
        "new_state": new_state,
        "updated": updated,
        "skipped": skipped,
        "updated_count": len(updated),
        "skipped_count": len(skipped),
    }


# Alerts/recommendations/decisions whose completion is MATERIALIZED from the real
# encargo+run state whenever they are READ (no events, can't drift). See
# ``materialize_signal_statuses``. Cheap: only touches ``in_progress`` rows.
_SIGNAL_TABLES = {"alerts", "recommendations", "decisions"}


async def _materialize_signals(s, entity: dict, local: bool) -> None:
    """Before serving a signal table, settle any «en curso» row whose encargo (or
    its execution) is already terminal — derived from the live photo, not events."""
    if not local:
        return
    tbl = str(entity.get("table") or "").lower()
    if tbl not in _SIGNAL_TABLES:
        return
    try:
        from app.routes.scheduling_routes import materialize_signal_statuses
        await materialize_signal_statuses(s, {tbl})
    except Exception:  # pragma: no cover — a read must never fail on bookkeeping
        logger.debug("signal materialize on read failed", exc_info=True)


async def _automation_context(ctx: dict) -> dict:
    """``{id, title, only_open}`` for the banner that explains the scoping."""
    from app.app_models.automations import Automation
    from app.app_database import app_session
    async with app_session() as s:
        title = (await s.execute(
            select(Automation.title).where(Automation.id == ctx["id"]))).scalar()
    return {**ctx, "title": title or f"#{ctx['id']}"}


def _provenance_filter(entity: dict, request: Request,
                       where_parts: list[str], params: dict) -> dict | None:
    """``?created_by_automation=<id>[&only_open=1]`` — «show me what THIS standing
    order left behind, and above all what is still open».

    Governing autonomous work needs the round trip closed: the panel says "left
    4 incidents, 2 open" and clicking must land on THOSE four rows,
    not on the full list where they are indistinguishable. Until now the chip
    linked to a generic listing, which is the same as not linking.

    No new column: the chain ``automation → automation_fired event → run →
    created_by_run_id`` (or ``source_event_id`` for a 0-token deterministic
    reaction) already exists, and it is expressed as two SUBQUERIES so the SQL
    has constant size — an automation with 3.000 fires would otherwise blow
    SQLite's parameter limit with an ``IN (…)`` of ids.

    ``only_open`` takes its states from the SAME place that produced the "2
    abiertas" figure (``automation_provenance``), so the chip and the list it
    opens can never disagree — two paths to one number is exactly the bug this
    whole change is fixing. Silently ignored for non-local entities (a project
    table has no provenance): a filter that cannot apply must not fake an empty
    list.
    """
    raw = (request.query_params.get("created_by_automation") or "").strip()
    if not raw or not _is_local(entity):
        return None
    try:
        aid = int(raw)
    except ValueError:
        return None
    from app.automation_provenance import traceable_columns

    trace = traceable_columns(entity.get("table") or "")
    ors: list[str] = []
    ev = ("SELECT id FROM events WHERE type = 'automation_fired' "
          "AND source = :__prov_src")
    if trace["run"]:
        ors.append(f'"created_by_run_id" IN (SELECT id FROM agent_runs '
                   f'WHERE event_id IN ({ev}))')
    if trace["event"]:
        ors.append(f'"source_event_id" IN ({ev})')
    if not ors:
        return None
    where_parts.append("(" + " OR ".join(ors) + ")")
    params["__prov_src"] = f"automation:{aid}"

    only_open = False
    if (request.query_params.get("only_open") or "").strip() in ("1", "true", "yes") \
            and trace["status"] and trace["open_states"]:
        keys = []
        for i, st in enumerate(trace["open_states"]):
            keys.append(f":__open{i}")
            params[f"__open{i}"] = st
        where_parts.append(f'"status" IN ({", ".join(keys)})')
        only_open = True
    return {"id": aid, "only_open": only_open}


@router.get("/{slug}")
async def list_rows(
    slug: str,
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    sort: str | None = None,
    direction: str = Query("desc", pattern="^(asc|desc)$"),
    q: str | None = None,
    user: TokenPayload = Depends(require_admin),
):
    entity = enrich_entity(_entity(slug))
    allowed = _allowed_columns(entity)

    sort_col = sort or (entity.get("default_sort") or {}).get("column") or entity.get("primary_key", "id")
    if sort_col not in allowed:
        raise HTTPException(status_code=400, detail=f"cannot sort by {sort_col!r}")

    where_parts: list[str] = []
    params: dict[str, Any] = {}

    if q and entity.get("search_columns"):
        ors = []
        for i, col in enumerate(entity["search_columns"]):
            if col not in allowed:
                continue
            key = f"q{i}"
            # Portable case-insensitive contains: ``LOWER(CAST(col AS TEXT))
            # LIKE LOWER(:key)`` works on BOTH the project Postgres and the
            # local SQLite store. The old ``col::text ILIKE`` is
            # Postgres-only and silently 500s on local entities.
            ors.append(f'LOWER(CAST("{col}" AS TEXT)) LIKE LOWER(:{key})')
            params[key] = f"%{q}%"
        if ors:
            where_parts.append("(" + " OR ".join(ors) + ")")

    for f in _filter_dicts(entity):
        col = f.get("column")
        if not col or col not in allowed:
            continue
        kind = f.get("kind")
        if not f.get("id"):
            continue
        raw = request.query_params.get(f["id"])
        if raw is None or raw == "":
            continue
        if kind == "enum":
            options = f.get("options") or []
            if raw not in options:
                continue
            where_parts.append(f'"{col}" = :{f["id"]}')
            params[f["id"]] = raw
        elif kind == "number_min":
            try:
                params[f["id"]] = float(raw)
            except ValueError:
                continue
            where_parts.append(f'"{col}" >= :{f["id"]}')
        elif kind == "number_max":
            try:
                params[f["id"]] = float(raw)
            except ValueError:
                continue
            where_parts.append(f'"{col}" <= :{f["id"]}')
        elif kind == "date_from":
            where_parts.append(f'"{col}" >= :{f["id"]}')
            params[f["id"]] = raw
        elif kind == "date_to":
            # A bare calendar date (YYYY-MM-DD) should include the WHOLE
            # day, so extend it to the last microsecond — otherwise
            # "Hasta: 2026-06-13" silently drops rows stamped later that
            # same day (created_at carries a time component).
            val = raw
            if len(raw) == 10 and raw.count("-") == 2:
                val = raw + " 23:59:59.999999"
            where_parts.append(f'"{col}" <= :{f["id"]}')
            params[f["id"]] = val
        elif kind == "ilike":
            # Portable (Postgres + local SQLite) — see search note above.
            where_parts.append(f'LOWER(CAST("{col}" AS TEXT)) LIKE LOWER(:{f["id"]})')
            params[f["id"]] = f"%{raw}%"
        else:
            where_parts.append(f'"{col}" = :{f["id"]}')
            params[f["id"]] = raw

    prov_ctx = _provenance_filter(entity, request, where_parts, params)

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    offset = (page - 1) * page_size

    pk_col = entity.get("primary_key", "id")
    local = _is_local(entity)
    if local:
        # v7.38.0 — LOCAL output tables: the business SURFACES read RAW system
        # columns the manifest's ``list_columns`` often do NOT enumerate:
        # ``created_at`` (the card date), ``status``, ``metadata_json`` (the
        # trace links encargo_id/outcome), ``source_event_id``,
        # ``created_by_run_id``, ``target_entity_*``, ``recipient``, ``body_md``…
        # Projecting only ``list_columns`` silently dropped them (why "the date
        # never shows"). Local tables are the agent's own small outputs, so
        # project the WHOLE row + alias any list_column whose id differs from its
        # physical column (the register grid reads by that id).
        extra_aliases: list[str] = []
        seen_alias: set[str] = set()
        for c in entity.get("list_columns", []) or []:
            col, alias = c.get("column"), c.get("id")
            if col and alias and alias != col and alias not in seen_alias:
                extra_aliases.append(f'"{col}" AS "{alias}"')
                seen_alias.add(alias)
        select_cols = "*" + ((", " + ", ".join(extra_aliases)) if extra_aliases else "")
    else:
        # PROJECT (tenant Postgres) tables: keep the explicit minimal projection
        # (SELECT * on a wide tenant table is wasteful; they carry no system
        # columns). ALWAYS include the pk so the SPA can build the detail route
        # (`/domain/<slug>/<pk>`) — else the row lands with no id and a click
        # navigates to `/domain/<slug>/undefined` (the detail-page 500).
        select_parts: list[str] = []
        seen_aliases: set[str] = set()
        for c in entity.get("list_columns", []) or []:
            col, alias = c.get("column"), c.get("id")
            if not col or not alias:
                continue
            select_parts.append(f'"{col}" AS "{alias}"')
            seen_aliases.add(alias)
        if pk_col not in seen_aliases:
            select_parts.append(f'"{pk_col}" AS "{pk_col}"')
        select_cols = ", ".join(select_parts) or "*"

    table = _qualify(entity["table"], local=local)
    sql = (
        f'SELECT {select_cols} FROM {table} {where_sql} '
        f'ORDER BY "{sort_col}" {direction.upper()} '
        f'LIMIT :__limit OFFSET :__offset'
    )
    count_sql = f"SELECT COUNT(*) AS n FROM {table} {where_sql}"

    async with _session_for(entity)() as s:
        await _materialize_signals(s, entity, local)
        rows_res = await s.execute(
            text(sql),
            {**params, "__limit": page_size, "__offset": offset},
        )
        rows = [_normalize_row(dict(r._mapping)) for r in rows_res.all()]  # noqa: SLF001
        total_res = await s.execute(text(count_sql), params)
        total = int(total_res.scalar() or 0)

    out = {
        "entity": entity["slug"],
        "page": page,
        "page_size": page_size,
        "total": total,
        "rows": rows,
    }
    # Who the list is scoped to, resolved HERE and not by the caller: the SPA
    # would otherwise have to fetch the whole automation list just to render
    # "only what «Salud de tenants» left behind". A filter the operator cannot see is
    # a filter he will misread as "these are all of them".
    if prov_ctx:
        out["filtered_by_automation"] = await _automation_context(prov_ctx)
    if local:
        await _stamp_origin(entity, rows, pk_col)
    return out


async def _stamp_origin(entity: dict, rows: list[dict], pk_col: str) -> None:
    """Stamp «this was created by autonomous job X» on every row of the page
    (v7.143.1).

    Until now the stamp only existed on the DETAIL view, so a list of alerts
    gave no way to tell autonomous work from what a person did — the operator
    had to open them one by one. Batched for the PAGE (≤200 rows) with the same
    resolver the detail uses, so both can't tell different stories, and
    best-effort: a badge is never worth a 500.
    """
    if not rows:
        return
    try:
        from app import automation_provenance as prov
        kind = prov.kind_for_table(entity.get("table") or "")
        if not kind:
            return
        ids: list[int] = []
        for r in rows:
            try:
                ids.append(int(r[pk_col]))
            except (KeyError, TypeError, ValueError):
                continue
        found = await prov.origin_of(kind, ids)
        for r in rows:
            origin = found.get(str(r.get(pk_col)))
            if origin:
                r["created_by_automation"] = origin
    except Exception:  # pragma: no cover — a badge never breaks a listing
        logger.debug("row origin stamp failed", exc_info=True)


# Categorical columns worth faceting on the dashboard (v7.45.0).
_FACET_COLS = ("status", "severity", "priority", "impact", "urgency",
               "confidence", "decision", "reason_code")


@router.get("/{slug}/summary")
async def summary(
    slug: str,
    user: TokenPayload = Depends(require_admin),
):
    """Facet COUNTS for a LOCAL agent-output entity (dashboard): the total plus a
    ``COUNT(*) GROUP BY`` for each categorical column it actually exposes
    (status/severity/impact/urgency/confidence/priority/decision/reason_code).
    Declared BEFORE ``/{slug}/{pk}`` so ``…/summary`` never resolves as a pk.
    Local entities only (project data is read-only and not agent-output)."""
    entity = _entity(slug)
    if not _is_local(entity):
        raise HTTPException(status_code=403, detail="summary is only for local entities")
    table = _qualify(entity["table"], local=True)
    by: dict[str, dict] = {}
    async with _session_for(entity)() as s:
        await _materialize_signals(s, entity, True)
        total = int((await s.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar() or 0)
        # Which of the facet columns physically exist on this table.
        present = set((await s.execute(text(f"SELECT * FROM {table} LIMIT 0"))).keys())
        for col in _FACET_COLS:
            if col not in present:
                continue
            res = await s.execute(
                text(f'SELECT "{col}" AS k, COUNT(*) AS n FROM {table} GROUP BY "{col}"'))
            counts = {
                (str(r._mapping["k"]) if r._mapping["k"] not in (None, "") else "—"): int(r._mapping["n"])  # noqa: SLF001
                for r in res.all()
            }
            if counts:
                by[col] = counts
    return {"entity": slug, "total": total, "by": by}


@router.get("/{slug}/producers")
async def producers(
    slug: str,
    user: TokenPayload = Depends(require_admin),
):
    """The autonomous jobs that have produced rows of this entity (v7.143.1).

    Populates the "autonomous work" filter WITHOUT the SPA having to load the
    automation catalogue and guess which of them ever wrote here. Empty list for
    a non-local entity (project data has no provenance) rather than a 403: the
    caller renders "no filter" and never has to special-case an error.
    Declared BEFORE ``/{slug}/{pk}`` so it never resolves as a pk.
    """
    entity = _entity(slug)
    if not _is_local(entity):
        return {"entity": slug, "items": []}
    from app import automation_provenance as prov
    kind = prov.kind_for_table(entity.get("table") or "")
    items = await prov.producers_of(kind) if kind else []
    return {"entity": slug, "items": items}


@router.get("/{slug}/{pk}")
async def detail(
    slug: str,
    pk: str,
    user: TokenPayload = Depends(require_admin),
):
    entity = _entity(slug)
    pk_col = entity.get("primary_key", "id")
    local = _is_local(entity)
    table = _qualify(entity["table"], local=local)

    # Cast the column to text so we don't rely on asyncpg's strict type
    # coercion. Without the cast a numeric ``order_id`` column raises
    # ``DataError: 'str' object cannot be interpreted as an integer``
    # because the URL always carries the pk as a string. ``CAST(... AS
    # TEXT)`` is portable across Postgres (project entities) and SQLite
    # (local entities) and is harmless on text columns.
    async with _session_for(entity)() as s:
        await _materialize_signals(s, entity, local)
        res = await s.execute(
            text(f'SELECT * FROM {table} WHERE CAST("{pk_col}" AS TEXT) = :pk LIMIT 1'),
            {"pk": pk},
        )
        row = res.first()
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        record = _normalize_row(dict(row._mapping))  # noqa: SLF001

        blocks: list[dict] = []
        declared_blocks = entity.get("detail_blocks", []) or []

        for block in declared_blocks:
            kind = block.get("kind")
            if kind == "fields":
                blocks.append({
                    "kind": "fields",
                    "label": block.get("label"),
                    "fields": [
                        {"label": _humanise(col), "key": col, "value": record.get(col)}
                        for col in block.get("columns", [])
                    ],
                })
            elif kind == "table":
                child_table = _qualify(block["table"], local=local)
                fk = block.get("fk_column")
                if not fk or not fk.replace("_", "").isalnum():
                    continue
                cols = block.get("columns", []) or ["*"]
                if cols == ["*"]:
                    select = "*"
                else:
                    select = ", ".join(f'"{c}"' for c in cols)
                child_res = await s.execute(
                    text(
                        f'SELECT {select} FROM {child_table} '
                        f'WHERE CAST("{fk}" AS TEXT) = :pk '
                        f'ORDER BY 1 DESC LIMIT 100'
                    ),
                    {"pk": str(record.get(pk_col)) if record.get(pk_col) is not None else None},
                )
                blocks.append({
                    "kind": "table",
                    "label": block.get("label"),
                    "columns": cols if cols != ["*"] else None,
                    "rows": [
                        _normalize_row(dict(r._mapping))  # noqa: SLF001
                        for r in child_res.all()
                    ],
                })

        # Fallback: when the manifest doesn't declare any ``detail_blocks``,
        # synthesise a single "fields" block with EVERY column of the
        # record so the page is never blank. The earlier UI rendered a
        # blank page in that case because it relied entirely on
        # ``data.blocks`` being non-empty. The contract is now: even
        # without any operator-curated layout, the operator gets a
        # fully populated detail view they can navigate to and from.
        if not blocks:
            blocks.append({
                "kind":  "fields",
                "label": entity.get("label") or slug,
                "fields": [
                    {"label": _humanise(k), "key": k, "value": v}
                    for k, v in record.items()
                ],
            })

    # v7.140.0 — reverse provenance: "this was created by automation X". The
    # forward direction (what did this standing order produce) lives in
    # Operaciones; without this half the operator can see a list of alerts and
    # have no idea which of them are autonomous work. Best-effort and honest: when
    # the chain does not resolve there is simply no stamp — never an invented one.
    origin = None
    if local:
        try:
            from app import automation_provenance
            kind = automation_provenance.kind_for_table(entity.get("table") or "")
            ent_id = record.get(pk_col)
            if kind and ent_id is not None:
                found = await automation_provenance.origin_of(kind, [int(ent_id)])
                origin = found.get(str(int(ent_id)))
        except Exception:  # pragma: no cover — a badge never breaks the detail
            logger.debug("origin stamp failed", exc_info=True)
    return {"entity": slug, "record": record, "blocks": blocks,
            "created_by_automation": origin}


def _humanise(column: str) -> str:
    """Turn ``order_id`` → ``Order id``. Cheap, deterministic and good
    enough for an operator-facing surface; tenants that need full
    i18n can declare ``label`` per detail_block.field instead."""
    if not column:
        return ""
    parts = column.replace("-", "_").split("_")
    return " ".join(p.capitalize() for p in parts if p)
