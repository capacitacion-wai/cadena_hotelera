# src/backend/app/mcp_server.py
"""Local MCP server — the agent's hands.

Every tool exposed here is a NAMED, DOMAIN-SHAPED action the agent
can take to MATERIALISE its findings into the app's local store. The
chat surface, the operations page, the dashboard widgets and the
domain pages all read from the same tables, so an action taken here
is immediately visible in the UI.

v6.4.0 replaces the v6.3.x ``local.write`` generic upsert with a
catalogue of first-class output tools, one per canonical output kind:

* ``upsert_alert``           — operational signal
* ``register_recommendation`` — concrete suggested next step
* ``log_communication``       — drafted / sent message to a human
* ``upsert_scoring``          — periodic per-target score
* ``log_decision``            — explicit "considered, did not act"

The catch-all ``local.write`` stays as a documented escape hatch for
domain-specific tables not covered by the canon (e.g. a custom
``welcome_queue``). Prefer the named tools whenever possible — they
validate inputs, set sensible defaults, populate the audit triplet
(``idempotency_key``, ``created_by_run_id``, ``created_at``) and are
the only ones the operator-facing Operations page knows how to render
with a friendly description.

Authentication: the agent pod connects with
``Authorization: Bearer ${APP_MCP_TOKEN}`` (a per-app secret minted
by the deployer). The agent receives the URL via ``APP_MCP_URL`` and
the token via env injection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
# ``select`` is used at module scope by ``_automations_guard`` and
# ``get_automation_catalog`` (and several tools lazy-import it locally). It MUST
# be importable here: without it those bare ``select(...)`` calls raise
# NameError, which the guard's broad ``except`` swallowed into a permanent
# "deny" — silently breaking automation management from the boss chat.
from sqlalchemy import select, text

from app.app_database import app_session, app_write
from app.app_models.business import (
    Alert, Approval, Communication, Decision, Recommendation,
    ScheduledItem, Scoring,
)
from app.app_models.memory import UserEpisode, UserFile, UserMemory
from app.agent_locale import localize_tool_schema, resolve_agent_language
# v7.146.0 — the SINGLE permission gate of the tools. The MCP channel arrives with a
# SERVICE token, so who asks for something is DERIVED from the run on the server and
# checked against the same functions the UI uses. See mcp_actor.
from app.mcp_actor import (
    TOOL_MIN_ROLE, Actor, gate_error, is_real_human_admin, resolve_actor,
    run_mode, run_serving_user,
)
from app.autonomous.tool_idempotency import _replay_or_execute
from app.deliverables import sanitize_attachment_list
# v7.97.2 — tool results serialise datetimes via iso_utc (explicit Z): a bare
# ``.isoformat()`` on a naive-UTC row loses the timezone and the agent quotes
# UTC digits as if they were business wall-clock time.
from app.timeutil import iso_utc
from app.mcp_memory_bind import (
    AGENT_MEMORY_KINDS,
    MEMORY_KINDS,
    MEMORY_TOOLS,
    SELF_MEMORY_TOOLS,
    _artifacts_from,
    _bound_memory_user,
    _memory_bind_result,
    _parse_artifacts,
)

APP_MCP_TOKEN = os.environ.get("APP_MCP_TOKEN", "")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _check_token(authorization: str = Header(...)):
    if not APP_MCP_TOKEN:
        raise HTTPException(status_code=503, detail="APP_MCP_TOKEN not configured")
    if authorization != f"Bearer {APP_MCP_TOKEN}":
        raise HTTPException(status_code=401, detail="invalid token")


class ToolCall(BaseModel):
    run_id: int
    # v12.0.0 — the REST path shares the service token with /mcp: without this, a guessed
    # integer `run_id` was enough to act as another person. It is resolved with
    # `mcp_actor.resolver_run` same as in JSON-RPC (chat requires a token; autonomous admits an id).
    run_token: Optional[str] = None
    step_idx: int
    name: str
    arguments: dict
    # IS3 (B5 follow-up): the agent caller passes "chat" or
    # "autonomous" so chat-side and autonomous-side calls never
    # collide on the tool_call_log idempotency_key UNIQUE constraint.
    # Defaults to autonomous for back-compat with older agents that
    # don't set the field.
    mode: str = "autonomous"


@router.post("/invoke", dependencies=[Depends(_check_token)])
async def invoke(call: ToolCall):
    handler = TOOLS.get(call.name)
    if handler is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"unknown tool {call.name!r}. Registered tools: "
                f"{sorted(TOOLS.keys())}"
            ),
        )
    # Same gate as in JSON-RPC: this REST path shares the service token, so without the
    # gate it would be the shortcut to skip the permissions.
    # v12.0.0 — same gate as JSON-RPC: the run is resolved by `run_token` (chat) or by
    # `run_id` only if the run is autonomous. A chat run named by its integer → 400.
    from app.mcp_actor import resolver_run
    resolved = await resolver_run(call.run_token, call.run_id)
    if resolved is None and (call.run_token or call.run_id):
        raise HTTPException(
            status_code=400,
            detail="invalid run: in a conversation pass run_token ([INTERNO] line); "
                   "run_id is only valid for autonomous runs",
        )
    call.run_id = int(resolved or 0)
    denied = await gate_error(call.name, await resolve_actor(call.run_id))
    if denied is not None:
        return denied
    return await handler(call)


@router.get("/agent-runtime", dependencies=[Depends(_check_token)])
async def agent_runtime_mcp():
    """Resolved opencode ``mcp`` block for the autonomous agent to MERGE into
    its ``opencode.json`` at boot (and on refresh).

    Returns the external Composio connectors (Gmail/Drive/Calendar/…) already
    resolved per connector — each a local ``streaming-http-proxy`` bridge to a
    signed Composio URL scoped to that connector's own identity. opencode
    cannot talk Composio's hosted MCP directly, so this is what makes the
    autonomous agent's connectors actually usable. ``app-tools`` is wired
    separately by the deployer and is intentionally NOT included here.

    Auth: the agent pod calls this with ``Authorization: Bearer ${APP_MCP_TOKEN}``
    (same guard as ``/mcp/invoke``).
    """
    from app.mcp_runtime import resolve_runtime_mcp_block
    mcp_block = await resolve_runtime_mcp_block()
    # v7.87.0 — the operator's session-level REASONING EFFORT (Operations → effort,
    # AppSetting ``agent_effort``). The sidecar's resolve_mcp.py injects it as
    # per-model ``options.reasoningEffort`` at boot/refresh; effort is config-only
    # (no per-prompt field), so a change takes effect when the sidecar's refresher
    # restarts opencode (it watches this value in its change signature).
    effort = ""
    extra_skills: list = []
    try:
        import json as _json
        from sqlalchemy import select as _sel
        from app.app_models.runtime import AppSetting
        async with app_session() as _s:
            effort = ((await _s.execute(
                _sel(AppSetting.value).where(AppSetting.key == "agent_effort")
            )).scalar_one_or_none() or "").strip().lower()
            # v7.89.0 — extra FUNCTIONAL skills the operator attached in caliente
            # (Operations → Skills, AppSetting ``agent_extra_skills`` = JSON list
            # of {id, name, display_name, files}). The sidecar's resolve_mcp.py
            # writes each into /workspace/.opencode/skills and restarts opencode
            # when the set changes (it watches this in its change signature).
            raw = ((await _s.execute(
                _sel(AppSetting.value).where(AppSetting.key == "agent_extra_skills")
            )).scalar_one_or_none() or "").strip()
        if raw:
            parsed = _json.loads(raw)
            for sk in (parsed if isinstance(parsed, list) else []):
                name = (sk.get("name") or "").strip("/")
                files = sk.get("files")
                if name and isinstance(files, dict) and files:
                    extra_skills.append({"name": name, "files": files})
    except Exception:  # noqa: BLE001 — never break the runtime block on a read
        pass
    # The models this employee may end up using. They go here because the agent pod declares
    # on EACH ONE the reasoning levels (variants) that can be requested per request: if they
    # were only declared on the model in use, changing the model in Operations would leave
    # the levels without effect and without warning.
    from app.config import AVAILABLE_MODELS as _MODELOS
    return {
        "mcp": mcp_block,
        "reasoning_effort": effort,
        "extra_skills": extra_skills,
        "available_models": list(_MODELOS),
    }


@router.get("/agent-runtime/ready", dependencies=[Depends(_check_token)])
async def agent_runtime_ready():
    """Warm-up readiness of every CONNECTED connector's MCP upstream.

    A Composio tool-router session does not serve its meta-tools the instant it
    is minted, and stock opencode fixes a session's tool list at CREATION and
    never re-lists — so a session opencode creates before the bridge is warm
    stays permanently without that connector's tools (only a NEW session
    recovers, and nothing forced one). The entrypoint holds ``opencode serve``
    until this reports ``ready`` (bounded — see ``entrypoint.sh``) so the agent's
    FIRST session is already warm. ``ready`` is true when every connector with an
    active account currently serves ``>=1`` tool; a connector with no account is
    "ready" (correctly disabled, nothing to wait for).

    Auth: ``Authorization: Bearer ${APP_MCP_TOKEN}`` (same guard as /agent-runtime).
    """
    from app.mcp_runtime import list_connector_status
    from app.routes.mcp_proxy_routes import probe_upstream_tools
    try:
        status = await list_connector_status()
    except Exception:  # noqa: BLE001 — never block the boot on a status read
        return {"ready": True, "connectors": {}}
    connectors: dict[str, Any] = {}
    all_ready = True
    for c in status:
        name = c.get("name")
        connected = bool(c.get("connected"))
        ready = True
        tools = None
        if connected:
            tools = await probe_upstream_tools(name)
            ready = bool(tools and tools > 0)
            if not ready:
                all_ready = False
        connectors[name] = {
            "connected": connected, "ready": ready,
            "tools": tools if tools is not None else 0,
        }
    return {"ready": all_ready, "connectors": connectors}


# ── Helpers ──────────────────────────────────────────────────────────


def _content_step_idx(run_id: int, name: str, arguments: dict) -> int:
    """Deterministic step index derived from the call CONTENT.

    The JSON-RPC bridge has no stable step from the agent, so we hash
    (run_id, name, args) into a fixed step. Two byte-identical calls in
    the same run therefore land on the SAME idempotency key — a re-sent
    ``tools/call`` replays instead of re-executing. Distinct calls (other
    tool or other args) hash to different steps and run independently.
    """
    blob = json.dumps(
        {"r": run_id, "n": name, "a": arguments},
        sort_keys=True,
        default=str,
    ).encode()
    return int(hashlib.sha256(blob).hexdigest(), 16) % 1_000_000_000


def _idempotency_key(call: ToolCall) -> str:
    """Stable hash over (run_id, step_idx, name, args).

    ``step_idx`` is itself derived from (run_id, name, args) by
    ``_content_step_idx``, so the same logical call replayed by the agent
    after a network retry yields the SAME key, and the ``UNIQUE`` index on
    each business table makes the second insert a silent no-op.
    """
    blob = json.dumps(
        {
            "r": call.run_id,
            "s": call.step_idx,
            "n": call.name,
            "a": call.arguments,
        },
        sort_keys=True,
        default=str,
    ).encode()
    return hashlib.sha256(blob).hexdigest()


def _require(args: dict, key: str) -> Any:
    if key not in args:
        raise HTTPException(
            status_code=400, detail=f"missing required argument {key!r}",
        )
    val = args[key]
    if val is None or (isinstance(val, str) and not val.strip()):
        raise HTTPException(
            status_code=400, detail=f"argument {key!r} must not be empty",
        )
    return val


def _enum(args: dict, key: str, allowed: tuple[str, ...], default: str) -> str:
    val = (args.get(key) or default).strip().lower()
    if val not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"argument {key!r} must be one of {allowed}, got {val!r}",
        )
    return val


def _serialise_extra(value: Any) -> str:
    """Coerce free-form ``evidence`` / ``metadata`` to JSON text.

    The agent may pass either a real dict or a JSON string already; we
    accept both and always end up with valid JSON so the row reads
    cleanly from the SPA.
    """
    if value is None:
        return "{}"
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except Exception:
            return json.dumps({"raw": value})
    try:
        return json.dumps(value, default=str)
    except Exception:
        return json.dumps({"raw": str(value)})


async def _persist(call: ToolCall, factory: Callable[[str], Any],
                   notify_table: str | None = None,
                   suppress_table: str | None = None):
    """Common write path: idempotency-aware INSERT.

    ``factory`` receives the precomputed idempotency key and returns
    a fully-built ORM instance. The wrapping ``_replay_or_execute``
    plus the ``UNIQUE`` index on ``idempotency_key`` give us at-most-
    once semantics whether the agent retries inside the same run or
    across different runs.

    B6: an FK / NOT NULL / CHECK violation used to bubble up as a
    500 to the caller (the agent), which then retried the same call
    forever or aborted the whole turn. We now translate
    ``IntegrityError`` into a structured ``{ok: false, error:
    "constraint", detail: ...}`` response the agent prompt knows how
    to handle (skip / pick a different tool / surface to the
    operator).
    """
    from sqlalchemy.exc import IntegrityError

    key = _idempotency_key(call)
    async with app_write() as session:
        async def _do():
            row = factory(key)
            # v7.64.0 — NO REPETIR gate: if this signal's identity is muted by an
            # active suppression, SKIP creation (kill the loop) and tell the agent.
            if suppress_table:
                from app.signal_suppress import gate_signal_creation
                suppressed = await gate_signal_creation(session, suppress_table, row)
                if suppressed is not None:
                    return suppressed
            session.add(row)
            notify_row = None
            try:
                await session.flush()
                # Capture the fields for notify rules BEFORE commit expires the row.
                if notify_table:
                    notify_row = {
                        "id": getattr(row, "id", None),
                        "title": getattr(row, "title", None) or getattr(row, "decision", None),
                        "description": (getattr(row, "description", None)
                                        or getattr(row, "rationale_md", None)
                                        or getattr(row, "reason_md", None)
                                        or getattr(row, "reason", None)),
                        "impact": getattr(row, "impact", None),
                        "urgency": getattr(row, "urgency", None),
                        "confidence": getattr(row, "confidence", None),
                        "severity": getattr(row, "severity", None) or getattr(row, "priority", None),
                    }
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                return {
                    "ok": False,
                    "error": "constraint",
                    "detail": str(exc.orig) if getattr(exc, "orig", None) else str(exc),
                    "idempotency_key": key,
                }
            if notify_table and notify_row:
                from app.signal_notify import evaluate_signal_rules
                await evaluate_signal_rules(session, notify_table, notify_row, call.run_id)
            return {
                "ok": True,
                "id": notify_row["id"] if notify_row else getattr(row, "id", None),
                "idempotency_key": key,
            }

        # Pass call.mode if present so chat and autonomous never
        # collide on the tool_call_log idempotency_key (B5).
        result, replayed = await _replay_or_execute(
            session,
            run_id=call.run_id,
            step_idx=call.step_idx,
            tool_name=call.name,
            args=call.arguments,
            do_work=_do,
            mode=getattr(call, "mode", "autonomous"),
        )
        return {"result": result, "replayed": replayed}


# ── Tools (named, domain-shaped) ─────────────────────────────────────


SEVERITIES = ("info", "low", "medium", "high", "critical")
PRIORITIES = ("low", "medium", "high", "urgent")
# v7.41.0 — the SEPARATED policy axes (see app.signal_policy). severity/priority
# are DERIVED from impact × urgency (↓ confidence); the agent writes the axes.
from app.signal_policy import (  # noqa: E402  (grouped with the domain enums)
    IMPACT as IMPACT_BANDS, URGENCY as URGENCY_BANDS,
    CONFIDENCE as CONFIDENCE_BANDS, REASON_CODES as REASON_CODE_CATALOG,
)
# v7.38.0 — ``whatsapp`` was MISSING, so an agent that wanted to log a WhatsApp
# message had no valid channel and fell back to ``sms`` (marked wrong AND not
# deliverable — there is no sms adapter). Deliverable channels first
# (email/whatsapp/slack have adapters → ``status='ready'`` really sends);
# ticket/webhook/sms are log-only, ``internal`` is an in-app notice.
# v7.155.0 — "telegram" was MISSING: the employee could not record or send a communication
# through Telegram even though the channel was configured and working for receiving. Same
# oversight as in v7.38.0 with whatsapp.
COMM_CHANNELS = ("email", "whatsapp", "telegram", "slack", "sms", "ticket",
                 "webhook", "internal")
COMM_STATUSES = ("draft", "ready", "sent", "failed")
DECISIONS = ("no_action", "deferred", "escalated")
# v7.40.0 — alerts, recommendations and decisions share ONE lifecycle
# vocabulary: open → acknowledged → in_progress → done ; dismissed (terminal ✗).
# ``in_progress`` is what «Convertir en encargo» (to_task) leaves the source in;
# it flips to ``done`` when the encargo closes. The dedicated close/transition
# tools (resolve_alert / decide_recommendation / decide_decision) accept these so
# the agent NEVER has to fall back to local.write to change a status.
UNIFIED_STATUSES = ("open", "acknowledged", "in_progress", "done", "dismissed")
ALERT_STATUSES = UNIFIED_STATUSES
RECOMMENDATION_STATUSES = UNIFIED_STATUSES
DECISION_STATUSES = UNIFIED_STATUSES


def _resolve_intensity(args: dict, derive):
    """Shared policy resolution for alerts/recommendations (v7.41.0).

    Returns ``(impact, urgency, confidence, band, override)`` where ``band`` is
    the DERIVED risk band (``severity`` for alerts via ``derive_severity``,
    ``priority`` for recommendations via ``derive_priority``). The agent writes
    the separated axes; the band is computed. Back-compat: an agent that still
    passes only the legacy band (``severity``/``priority``) gets the axes
    back-derived and the band honoured as an explicit OVERRIDE.
    """
    from app import signal_policy
    params = signal_policy.policy_params()
    impact_in, urgency_in = args.get("impact"), args.get("urgency")
    confidence = signal_policy.coerce_confidence(args.get("confidence"), "medium")
    band_in = args.get("severity") if "severity" in args else args.get("priority")
    if impact_in is None and urgency_in is None and band_in:
        impact, urgency = signal_policy.severity_to_impact_urgency(band_in) \
            if "severity" in args else _priority_to_impact_urgency(band_in)
    else:
        impact = signal_policy.coerce_impact(impact_in) if impact_in is not None else "medium"
        urgency = signal_policy.coerce_urgency(urgency_in) if urgency_in is not None else "medium"
    derived = derive(impact, urgency, confidence, params)
    band, override = derived, False
    if band_in:
        b = str(band_in).strip().lower()
        allowed = SEVERITIES if "severity" in args else PRIORITIES
        if b in allowed:
            band, override = b, (b != derived)
    return impact, urgency, confidence, band, override


def _priority_to_impact_urgency(priority: str) -> tuple[str, str]:
    """Legacy inverse for recommendations that only set ``priority``."""
    return {"low": ("low", "low"), "medium": ("medium", "medium"),
            "high": ("high", "high"), "urgent": ("critical", "high")}.get(
        str(priority or "").strip().lower(), ("medium", "medium"))


def _task_meta(args: dict) -> dict:
    """The agent's ASSOCIATED-ENCARGO spec, folded into ``metadata_json.task``
    (v7.44.0). ``allow_task`` gates the operator's «Convertir en encargo» button
    — HIDDEN by default, shown only when the agent says the item warrants one.
    ``task_title``/``task_description`` are the encargo to create (it may differ
    from the signal itself — e.g. an alert "recurring non-payment" → task "call the
    customer and renegotiate"). Returns ``{}`` when the agent said nothing (⇒ no
    button), so nothing is emitted for legacy/quiet items."""
    allow = bool(args.get("allow_task"))
    title = str(args.get("task_title") or "").strip()
    desc = str(args.get("task_description") or "").strip()
    if not (allow or title or desc):
        return {}
    task: dict = {"allow": allow}
    if title:
        task["title"] = title[:200]
    if desc:
        task["description"] = desc
    return {"task": task}


async def upsert_alert(call: ToolCall):
    """Raise an operational alert — a RISK/incident with evidence.

    Required: ``title``, ``description``.
    Policy axes (v7.41.0 — prefer these; ``severity`` is DERIVED from them):
      * ``impact`` (low|medium|high|critical) — magnitude of the consequence.
      * ``urgency`` (low|medium|high|immediate) — how soon action is needed.
      * ``confidence`` (low|medium|high) — how sure you are (low downgrades
        severity one band).
    Deduplication (v7.41.0 — AVOID duplicates): pass ``dedupe_key`` (a stable id
    for this problem) and/or ``root_cause``. If an OPEN/acknowledged/in-progress
    alert with the same key already exists, this UPDATES it (bumps its occurrence
    counter and ``last_seen_at``, keeps the WORST impact/urgency, refreshes
    evidence) instead of creating a duplicate. When it reappears, call this again
    with the same key.
    Optional: ``severity`` (explicit OVERRIDE of the derived band),
    ``source_event_id``, ``target_entity_kind``/``target_entity_id``,
    ``evidence`` (free-form dict).

    Returns ``{ok, id, updated, occurrences, severity, dedupe_key}``.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app import signal_policy
    from app.timeutil import now_utc
    args = call.arguments or {}
    title = _require(args, "title")
    description = args.get("description", "") or ""
    impact, urgency, confidence, severity, override = _resolve_intensity(
        args, signal_policy.derive_severity)
    params = signal_policy.policy_params()
    dedupe_key = signal_policy.normalize_dedupe_key(
        args.get("dedupe_key"), args.get("root_cause"),
        args.get("target_entity_kind"), args.get("target_entity_id"))
    root_cause = (str(args["root_cause"]).strip() or None) if args.get("root_cause") else None
    meta = {"severity_override": severity} if override else {}
    meta.update(_task_meta(args))
    key = _idempotency_key(call)

    async with app_write() as session:
        async def _do():
            now = now_utc().replace(tzinfo=None)
            # v7.64.0 — NO REPETIR gate BEFORE dedup/insert: a muted identity is not
            # re-created nor re-opened, even if its conditions recur.
            from app.signal_suppress import gate_signal_creation
            suppressed = await gate_signal_creation(session, "alerts", {
                "dedupe_key": dedupe_key, "title": title,
                "target_entity_kind": args.get("target_entity_kind"),
                "target_entity_id": args.get("target_entity_id"),
            })
            if suppressed is not None:
                return suppressed
            # ---- Semantic dedup: update the matching NON-terminal alert --------
            if dedupe_key:
                existing = (await session.execute(
                    select(Alert).where(
                        Alert.dedupe_key == dedupe_key,
                        Alert.status.in_(("open", "acknowledged", "in_progress")),
                    ).order_by(Alert.created_at.desc()).limit(1)
                )).scalar_one_or_none()
                window = signal_policy.dedupe_window_days(params)
                if existing is not None and window is not None and existing.created_at \
                        and (now - existing.created_at).days > window:
                    existing = None
                if existing is not None:
                    existing.occurrences = (existing.occurrences or 1) + 1
                    existing.last_seen_at = now
                    if existing.first_seen_at is None:
                        existing.first_seen_at = existing.created_at or now
                    existing.impact = signal_policy.higher_band(existing.impact, impact, signal_policy.IMPACT)
                    existing.urgency = signal_policy.higher_band(existing.urgency, urgency, signal_policy.URGENCY)
                    existing.confidence = confidence or existing.confidence
                    existing.severity = severity if override else signal_policy.derive_severity(
                        existing.impact, existing.urgency, existing.confidence, params)
                    if root_cause and not existing.root_cause:
                        existing.root_cause = root_cause
                    if description:
                        existing.description = str(description)
                    ev = _serialise_extra(args.get("evidence"))
                    if ev and ev != "{}":
                        existing.evidence_json = ev
                    tm = _task_meta(args)
                    if tm:  # refresh the associated-encargo spec on reappearance
                        try:
                            em = json.loads(existing.metadata_json or "{}") or {}
                        except Exception:
                            em = {}
                        em.update(tm)
                        existing.metadata_json = json.dumps(em, default=str)
                    await session.commit()
                    return {"ok": True, "id": existing.id, "updated": True,
                            "occurrences": existing.occurrences,
                            "severity": existing.severity, "dedupe_key": dedupe_key}
            # ---- No match → INSERT a fresh alert ------------------------------
            row = Alert(
                idempotency_key=key, created_by_run_id=call.run_id,
                severity=severity, impact=impact, urgency=urgency, confidence=confidence,
                title=str(title), description=str(description),
                source_event_id=args.get("source_event_id"),
                target_entity_kind=args.get("target_entity_kind"),
                target_entity_id=(str(args["target_entity_id"])
                                  if args.get("target_entity_id") is not None else None),
                evidence_json=_serialise_extra(args.get("evidence")),
                dedupe_key=dedupe_key, root_cause=root_cause,
                occurrences=1, first_seen_at=now, last_seen_at=now,
                metadata_json=json.dumps(meta, default=str) if meta else "{}",
                status="open",
            )
            session.add(row)
            try:
                await session.flush()
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                return {"ok": False, "error": "constraint",
                        "detail": str(getattr(exc, "orig", None) or exc),
                        "idempotency_key": key}
            # v7.62.0 — fire operator notify rules for this NEW alert (not on a
            # dedup-update, to avoid re-notifying on every reappearance).
            from app.signal_notify import evaluate_signal_rules
            await evaluate_signal_rules(session, "alerts", {
                "id": row.id, "title": row.title, "description": row.description,
                "impact": row.impact, "urgency": row.urgency,
                "confidence": row.confidence, "severity": row.severity,
            }, call.run_id)
            return {"ok": True, "id": row.id, "updated": False, "occurrences": 1,
                    "severity": severity, "dedupe_key": dedupe_key}

        result, replayed = await _replay_or_execute(
            session, run_id=call.run_id, step_idx=call.step_idx, tool_name=call.name,
            args=call.arguments, do_work=_do, mode=getattr(call, "mode", "autonomous"),
        )
        return {"result": result, "replayed": replayed}


async def register_recommendation(call: ToolCall):
    """Register a concrete suggested next step (an ACTION for a human to accept).

    Required: ``title``, ``rationale``.
    Policy axes (v7.41.0 — prefer these; ``priority`` is DERIVED from them):
      * ``impact`` (low|medium|high|critical) — size of the expected benefit.
      * ``urgency`` (low|medium|high|immediate) — how soon it should be acted on.
      * ``confidence`` (low|medium|high) — how sure you are of the suggestion.
    Optional: ``priority`` (explicit OVERRIDE of the derived band),
    ``expected_impact`` (free-text business headline, e.g. "recovers 3 orders"),
    ``target_entity_kind``/``target_entity_id``, ``metadata``.
    Deduplication (v7.100.0 — AVOID duplicates, SAME as ``upsert_alert``): pass a
    stable ``dedupe_key`` (or rely on the fallback to ``target_kind:target_id``).
    If an OPEN/acknowledged/in-progress recommendation with the same key already
    exists, this UPDATES it (bumps ``occurrences``/``last_seen_at``, keeps the
    WORST impact/urgency, refreshes the rationale) instead of piling up a new
    "review…" card every autonomous pass. Returns
    ``{ok, id, updated, occurrences, priority, dedupe_key}``.

    Optional: ``root_cause`` (the cause you have identified, so the recommendation can be understood without rereading the alert).
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app import signal_policy
    from app.timeutil import now_utc
    args = call.arguments or {}
    title = _require(args, "title")
    rationale = _require(args, "rationale")
    impact, urgency, confidence, priority, override = _resolve_intensity(
        args, signal_policy.derive_priority)
    params = signal_policy.policy_params()
    # A recommendation is the pending ACTION on a target: there can only be one open per
    # target, so the key IS the target (target_only). If the key carried the concern the
    # agent writes, two ways of saying the same thing ("Review replenishment of X" / "Define
    # the replenishment of X") would create two twin cards.
    dedupe_key = signal_policy.normalize_dedupe_key(
        args.get("dedupe_key"), args.get("root_cause"),
        args.get("target_entity_kind"), args.get("target_entity_id"),
        target_only=True)
    meta = _serialise_extra(args.get("metadata"))
    task_meta = _task_meta(args)
    if override or task_meta:
        try:
            m = json.loads(meta) or {}
        except Exception:
            m = {}
        if override:
            m["priority_override"] = priority
        m.update(task_meta)
        meta = json.dumps(m, default=str)
    key = _idempotency_key(call)

    async with app_write() as session:
        async def _do():
            now = now_utc().replace(tzinfo=None)
            # NO REPETIR gate BEFORE dedup/insert (mirrors upsert_alert): a muted
            # identity is not re-created nor re-opened even if its conditions recur.
            from app.signal_suppress import gate_signal_creation
            suppressed = await gate_signal_creation(session, "recommendations", {
                "dedupe_key": dedupe_key, "title": title,
                "target_entity_kind": args.get("target_entity_kind"),
                "target_entity_id": args.get("target_entity_id"),
            })
            if suppressed is not None:
                return suppressed
            # ---- Semantic dedup: update the matching NON-terminal recommendation.
            if dedupe_key:
                existing = (await session.execute(
                    select(Recommendation).where(
                        Recommendation.dedupe_key == dedupe_key,
                        Recommendation.status.in_(("open", "acknowledged", "in_progress")),
                    ).order_by(Recommendation.created_at.desc()).limit(1)
                )).scalar_one_or_none()
                window = signal_policy.dedupe_window_days(params)
                if existing is not None and window is not None and existing.created_at \
                        and (now - existing.created_at).days > window:
                    existing = None
                if existing is not None:
                    existing.occurrences = (existing.occurrences or 1) + 1
                    existing.last_seen_at = now
                    if existing.first_seen_at is None:
                        existing.first_seen_at = existing.created_at or now
                    existing.impact = signal_policy.higher_band(existing.impact, impact, signal_policy.IMPACT)
                    existing.urgency = signal_policy.higher_band(existing.urgency, urgency, signal_policy.URGENCY)
                    existing.confidence = confidence or existing.confidence
                    existing.priority = priority if override else signal_policy.derive_priority(
                        existing.impact, existing.urgency, existing.confidence, params)
                    if rationale:
                        existing.rationale_md = str(rationale)
                    if args.get("expected_impact"):
                        existing.expected_impact = args.get("expected_impact")
                    tm = _task_meta(args)
                    if tm:  # refresh the associated-encargo spec on reappearance
                        try:
                            em = json.loads(existing.metadata_json or "{}") or {}
                        except Exception:
                            em = {}
                        em.update(tm)
                        existing.metadata_json = json.dumps(em, default=str)
                    await session.commit()
                    return {"ok": True, "id": existing.id, "updated": True,
                            "occurrences": existing.occurrences,
                            "priority": existing.priority, "dedupe_key": dedupe_key}
            # ---- No match → INSERT a fresh recommendation --------------------
            row = Recommendation(
                idempotency_key=key,
                created_by_run_id=call.run_id,
                title=str(title),
                rationale_md=str(rationale),
                priority=priority,
                impact=impact, urgency=urgency, confidence=confidence,
                target_entity_kind=args.get("target_entity_kind"),
                target_entity_id=(
                    str(args["target_entity_id"])
                    if args.get("target_entity_id") is not None else None
                ),
                expected_impact=args.get("expected_impact"),
                dedupe_key=dedupe_key, occurrences=1,
                first_seen_at=now, last_seen_at=now,
                metadata_json=meta,
                status="open",  # v7.40.0 unified lifecycle entry state
            )
            session.add(row)
            try:
                await session.flush()
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                return {"ok": False, "error": "constraint",
                        "detail": str(getattr(exc, "orig", None) or exc),
                        "idempotency_key": key}
            # Fire operator notify rules only for a NEW recommendation (not on a
            # dedup-update, to avoid re-notifying on every reappearance).
            from app.signal_notify import evaluate_signal_rules
            await evaluate_signal_rules(session, "recommendations", {
                "id": row.id, "title": row.title, "description": row.rationale_md,
                "impact": row.impact, "urgency": row.urgency,
                "confidence": row.confidence, "severity": row.priority,
            }, call.run_id)
            return {"ok": True, "id": row.id, "updated": False, "occurrences": 1,
                    "priority": priority, "dedupe_key": dedupe_key}

        result, replayed = await _replay_or_execute(
            session, run_id=call.run_id, step_idx=call.step_idx, tool_name=call.name,
            args=call.arguments, do_work=_do, mode=getattr(call, "mode", "autonomous"),
        )
        return {"result": result, "replayed": replayed}


async def log_communication(call: ToolCall):
    """Record an outbound message to a human.

    Required: ``channel``, ``recipient``, ``body``.
    Optional: ``subject``, ``status`` (default ``draft``),
    ``attachments`` (list of {filename, workspace_path|url|content_base64,
    mime_type?} — to deliver a generated PPTX/PDF/CSV).

    Channel: use ``whatsapp`` for WhatsApp, ``email`` for email, ``slack`` for
    Slack — these are DELIVERABLE (``status='ready'`` actually sends through the
    configured channel). ``sms``/``ticket``/``webhook`` are log-only (no
    delivery adapter); ``internal`` is an in-app notice to the operator. Do NOT
    put a WhatsApp/phone message under ``sms`` — use ``whatsapp``. Messages with
    no subject concept (whatsapp/sms/slack) can omit ``subject``.

    Status semantics (IMPORTANT — these now drive REAL delivery):
      * ``draft`` — staged for human review, NOT sent.
      * ``ready`` — the platform WILL deliver it through the matching
        channel to the OPERATOR-configured recipients (you never choose
        the recipients; ``recipient`` is recorded for the audit only).
        The row moves to ``sent`` (with ``delivery_id``) or ``failed``
        (with the reason) automatically after the run.
      * ``sent`` — use only when you already delivered the message via a
        ``notify`` block / channel reply in THIS run and just want to log
        it; the platform reconciles it too.

    To deliver a FILE by channel, set ``status='ready'`` and pass the
    file in ``attachments`` with ``workspace_path`` pointing at a real
    file under ``/workspace/exports/`` (NEVER a link). Equivalent to
    using a ``notify`` block with attachments — pick one, not both.


    Returns ``{"ok": true, "id": <created id>, "idempotency_key": …}``. If a constraint prevents it, ``{"ok": false, "error": "constraint", …}`` — read it: **do not assume it was saved**. If the signal is silenced ``{"suppressed": true}`` arrives and you must NOT retry it.
    With ``status='ready'`` the row moves to *sent* or *failed* depending on whether the channel delivers it — **check it**: creating the row is not having delivered it.
    """
    args = call.arguments or {}
    channel = _enum(args, "channel", COMM_CHANNELS, "email")
    recipient = _require(args, "recipient")
    # Normalise the person reference through the directory when it resolves
    # (name/alias/phone → canonical email) so the audit row points at a real
    # ficha; an EXTERNAL correspondent (inbound sender not in the directory)
    # is kept verbatim — recipients of a ``ready`` delivery are the
    # operator's, never this field (see docstring).
    recipient = (await _resolve_directory_email(str(recipient))) or str(recipient)
    body = _require(args, "body")
    status = _enum(args, "status", COMM_STATUSES, "draft")
    attachments = sanitize_attachment_list(args.get("attachments"))
    attachments_json = (
        json.dumps(attachments) if attachments else None
    )

    # v7.107.3 — a communication recorded ALREADY AS SENT (the agent delivered it in this
    # pass with a `notify` block/channel reply) must leave a RECORD of the delivery at the
    # moment it is recorded. Before, `sent` was stored with `sent_at` NULL and the reconcile
    # did not touch it (it only looks at draft/ready), so the outbox piled up sends without
    # proof: "it is recorded that it went out, but not when nor with which id" (AGO-83).
    # The final `delivery_id` is stamped by the reconcile if the channel returns one; here it
    # stays marked as self-reported by the agent, which is exactly what it is.
    from app.timeutil import now_utc as _now_utc
    now = _now_utc().replace(tzinfo=None)
    entregada = status == "sent"

    return await _persist(
        call,
        lambda key: Communication(
            idempotency_key=key,
            created_by_run_id=call.run_id,
            channel=channel,
            recipient=str(recipient),
            subject=args.get("subject"),
            body_md=str(body),
            status=status,
            sent_at=now if entregada else None,
            delivery_id="agent-reported" if entregada else None,
            attachments_json=attachments_json,
        ),
    )


async def upsert_scoring(call: ToolCall):
    """Record a periodic per-target score.

    Required: ``target_kind``, ``target_id``, ``metric``, ``value``.
    Optional: ``window_days`` (default 30), ``band``,
    ``explanation``, ``metadata``.

    The ``UniqueConstraint(target_kind, target_id, metric, window)``
    in the model + the idempotency_key on the audit triplet guarantee
    we never duplicate the same score row inside one run.


    Returns ``{"ok": true, "id": <created id>, "idempotency_key": …}``. If a constraint prevents it, ``{"ok": false, "error": "constraint", …}`` — read it: **do not assume it was saved**. If the signal is silenced ``{"suppressed": true}`` arrives and you must NOT retry it.
    """
    args = call.arguments or {}
    target_kind = _require(args, "target_kind")
    target_id = _require(args, "target_id")
    metric = _require(args, "metric")
    value_raw = _require(args, "value")
    try:
        value = float(value_raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail=f"value must be numeric, got {value_raw!r}",
        )
    window_days = int(args.get("window_days") or 30)

    return await _persist(
        call,
        lambda key: Scoring(
            idempotency_key=key,
            created_by_run_id=call.run_id,
            target_kind=str(target_kind),
            target_id=str(target_id),
            metric=str(metric),
            window_days=window_days,
            value=value,
            band=args.get("band"),
            explanation_md=str(args.get("explanation") or ""),
            metadata_json=_serialise_extra(args.get("metadata")),
        ),
    )


async def log_decision(call: ToolCall):
    """Audit an explicit "I evaluated this and I am NOT acting now, and why".

    Required: ``decision`` (the coarse DISPOSITION — ``no_action|deferred|
    escalated``), ``reason`` (free text).
    Structured why (v7.41.0 — prefer this): ``reason_code`` — one of the
    domain-agnostic catalog codes ``below_threshold | insufficient_evidence |
    low_confidence | duplicate | out_of_scope | needs_human | already_handled |
    deferred_timing | awaiting_data | no_action_needed``. If omitted it is
    inferred from the disposition. An invalid/disabled code is ignored.
    Reference (v7.41.0): ``ref_kind`` + ``ref_id`` point at the finding you
    evaluated or the action you avoided (e.g. ``ref_kind="alerts", ref_id=42`` or
    ``ref_kind="recommendation"``). Also optional: ``confidence`` (low|medium|
    high), ``source_event_id``, ``metadata``.

    Use this when a signal arrives, the agent investigates, and records how it
    chose to treat it (including staying silent) — without this row the operator
    cannot tell whether the agent processed the signal at all. The decision
    enters the shared lifecycle at ``open``.


    Returns ``{"ok": true, "id": <created id>, "idempotency_key": …}``. If a constraint prevents it, ``{"ok": false, "error": "constraint", …}`` — read it: **do not assume it was saved**. If the signal is silenced ``{"suppressed": true}`` arrives and you must NOT retry it.
    """
    from app import signal_policy
    args = call.arguments or {}
    decision = _enum(args, "decision", DECISIONS, "no_action")
    reason = _require(args, "reason")
    params = signal_policy.policy_params()
    reason_code = signal_policy.validate_reason_code(args.get("reason_code"), params) \
        or signal_policy.reason_code_from_disposition(decision)
    confidence = signal_policy.coerce_confidence(args.get("confidence"), "medium")
    ref_kind = (str(args["ref_kind"]).strip() or None) if args.get("ref_kind") else None
    ref_id = (str(args["ref_id"]).strip() or None) if args.get("ref_id") is not None else None
    meta = _serialise_extra(args.get("metadata"))
    task_meta = _task_meta(args)
    if task_meta:
        try:
            m = json.loads(meta) or {}
        except Exception:
            m = {}
        m.update(task_meta)
        meta = json.dumps(m, default=str)

    return await _persist(
        call,
        lambda key: Decision(
            idempotency_key=key,
            created_by_run_id=call.run_id,
            source_event_id=args.get("source_event_id"),
            decision=decision,
            reason_code=reason_code,
            confidence=confidence,
            ref_kind=ref_kind,
            ref_id=ref_id,
            reason_md=str(reason),
            metadata_json=meta,
            status="open",  # v7.40.0 unified lifecycle entry state
        ),
        notify_table="decisions",
        suppress_table="decisions",
    )


# ── Suppressions ("no repetir") ─────────────────────────────────────
_SUPPRESS_KINDS = ("alert", "recommendation", "decision")
_SUPPRESS_KIND_TABLE = {"alert": "alerts", "recommendation": "recommendations", "decision": "decisions"}
_SUPPRESS_MODEL = {"alerts": Alert, "recommendations": Recommendation, "decisions": Decision}


async def suppress_signal(call: ToolCall):
    """Silence a recurring alert/recommendation/decision so it is NOT regenerated
    even if its conditions recur again — for a BOUNDED time (you may only create
    TEMPORARY snoozes; a permanent mute is a human decision in the app UI).

    Required: ``signal_kind`` (alert|recommendation|decision) + EITHER ``signal_id``
    (the row you just handled/closed — preferred) OR ``dedupe_key`` (its stable id).
    Optional: ``days`` (snooze length; default 14, max 30) and ``reason``.

    Use it when you keep raising the SAME thing a human already handled, or it is
    noise for a while: after this, the platform SKIPS future creations with this
    identity (and tells you so — don't retry them). See what's muted with
    ``list_suppressions``. Removing a mute (human, in the UI) re-enables it.


    Returns ``{"ok": true, "suppressed": true, "rule_id": …}``.
    """
    from app.signal_suppress import (
        suppression_key, signal_label, create_suppression, AGENT_MAX_SNOOZE_DAYS,
    )
    from app.timeutil import iso_utc, now_utc
    from datetime import timedelta
    args = call.arguments or {}
    kind = _enum(args, "signal_kind", _SUPPRESS_KINDS, "alert")
    table = _SUPPRESS_KIND_TABLE[kind]
    try:
        days = int(args.get("days")) if args.get("days") is not None else 14
    except (TypeError, ValueError):
        days = 14
    days = max(1, min(days, AGENT_MAX_SNOOZE_DAYS))
    reason = str(args.get("reason") or "").strip() or "Silenciada por el empleado (no repetir)."
    async with app_write() as session:
        key = label = None
        source_id = None
        sid = args.get("signal_id")
        if sid is not None:
            try:
                model = _SUPPRESS_MODEL[table]
                obj = (await session.execute(select(model).where(model.id == int(sid)))).scalar_one_or_none()
            except (TypeError, ValueError):
                obj = None
            if obj is None:
                return {"ok": False, "error": "not_found", "detail": f"{kind} {sid!r} no existe"}
            key, label, source_id = suppression_key(table, obj), signal_label(table, obj), int(sid)
        else:
            dk = str(args.get("dedupe_key") or "").strip()
            if not dk:
                return {"ok": False, "error": "need_identity",
                        "detail": "Pasa signal_id o dedupe_key."}
            # v7.68.0 — normalize IDENTICALLY to the gate (`_norm`: collapse
            # whitespace + truncate 160), else a key with a double space / >160
            # chars would never match at creation-time and the mute would silently
            # never fire.
            from app.signal_suppress import _TABLE_TO_PREFIX, _norm
            key, label = f"{_TABLE_TO_PREFIX[table]}:{_norm(dk)}", dk
        muted_until = now_utc() + timedelta(days=days)
        rule = await create_suppression(
            session, table=table, key=key, label=label, muted_until=muted_until,
            reason=reason, created_by="agent", created_by_run_id=call.run_id,
            source_signal_id=source_id,
        )
        await session.commit()
        # v7.68.0 — be HONEST when the rule stays disabled: create_suppression
        # won't re-enable a mute a HUMAN turned off (the agent may only strengthen,
        # never override a person). Tell the agent it was NOT (re)activated instead
        # of returning a misleading muted_until/days.
        if not rule.enabled:
            return {"ok": True, "suppressed": False, "rule_id": rule.id,
                    "signal_table": table, "match_key": key,
                    "note": "There is already a silence rule that a manager DISABLED; "
                            "you do not reactivate it. The signal will keep being generated "
                            "unless a human silences it again."}
        return {"ok": True, "suppressed": True, "rule_id": rule.id,
                "signal_table": table, "match_key": key,
                "muted_until": iso_utc(rule.muted_until), "days": days}


async def list_suppressions(call: ToolCall):
    """List active 'no repetir' rules (optionally filter by ``signal_kind``).
    Consult it to know what you must NOT re-raise. Returns, per rule: what is muted,
    until when (or 'forever'), whether it is still active, and how many repeats
    it has already blocked."""
    from app.app_models.signal_suppressions import SignalSuppression
    from app.signal_suppress import is_active
    from app.timeutil import iso_utc
    args = call.arguments or {}
    kind = str(args.get("signal_kind") or "").strip().lower()
    stmt = select(SignalSuppression).order_by(
        SignalSuppression.signal_table.asc(), SignalSuppression.id.desc())
    if kind in _SUPPRESS_KIND_TABLE:
        stmt = stmt.where(SignalSuppression.signal_table == _SUPPRESS_KIND_TABLE[kind])
    async with app_session() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return {"items": [{
        "id": r.id, "signal_table": r.signal_table,
        "muted": r.match_label or r.match_key,
        "muted_until": iso_utc(r.muted_until) or "forever",
        "active": is_active(r), "suppressed_count": r.suppress_count,
        "reason": r.reason,
    } for r in rows]}


# ── Lifecycle transitions (close / decide — no local.write needed) ──
#
# Creating an alert/recommendation is ``upsert_alert`` /
# ``register_recommendation``; CLOSING one is its own first-class tool so
# the agent never has to hand-craft a raw ``local.write`` (and rediscover
# idempotency_key / created_at / *_json by trial and error). These UPDATE
# an existing row by id, like ``complete_scheduled_item``.


async def resolve_alert(call: ToolCall):
    """Close or transition an EXISTING alert — use this instead of
    local.write to change an alert's status.

    Use it to ACKNOWLEDGE, mark IN PROGRESS, close as DONE or DISMISS an
    alert you (or a previous run) raised — e.g. after you acted on it or it
    no longer applies. Find the ``alert_id`` with ``list_alerts``.

    Required: ``alert_id``, ``status`` (``acknowledged`` | ``in_progress`` |
    ``done`` | ``dismissed``; pass ``open`` to reopen).
    Optional: ``resolved_by`` (who closed it; default ``agent``).

    Terminal statuses (``done`` / ``dismissed``) stamp ``resolved_at``
    automatically; ``open`` clears it. (v7.40.0 unified lifecycle — the old
    ``resolved`` status is now ``done``.)


    Returns ``{"result": …, "replayed": <bool>}``. ``replayed: true`` means this same call had already been executed before and you are given the result from then: **do not repeat it**.
    """
    from sqlalchemy import select, update as _update
    args = call.arguments or {}
    alert_id = _require(args, "alert_id")
    try:
        alert_id = int(alert_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="alert_id must be an integer")
    status = _enum(args, "status", ALERT_STATUSES, "done")
    resolved_by = str(args.get("resolved_by") or "agent")[:120]

    async with app_write() as session:
        async def _do():
            from app.timeutil import now_utc
            found = (await session.execute(
                select(Alert.id).where(Alert.id == alert_id)
            )).scalar_one_or_none()
            if found is None:
                return {"ok": False, "error": "not_found", "alert_id": alert_id}
            values: dict[str, Any] = {"status": status}
            if status in ("done", "dismissed"):
                values["resolved_at"] = now_utc().replace(tzinfo=None)
                values["resolved_by"] = resolved_by
            elif status == "open":
                values["resolved_at"] = None
                values["resolved_by"] = None
            await session.execute(
                _update(Alert).where(Alert.id == alert_id).values(**values)
            )
            await session.commit()
            return {"ok": True, "alert_id": alert_id, "status": status}

        result, replayed = await _replay_or_execute(
            session,
            run_id=call.run_id,
            step_idx=call.step_idx,
            tool_name=call.name,
            args=args,
            do_work=_do,
            mode=getattr(call, "mode", "autonomous"),
        )
        return {"result": result, "replayed": replayed}


async def decide_recommendation(call: ToolCall):
    """Close or transition an EXISTING recommendation — use this instead
    of local.write to change a recommendation's status.

    Use it to ACKNOWLEDGE a recommendation, mark it IN PROGRESS, close it as
    DONE or DISMISS it (or back to ``open``) — e.g. after the operator acted
    on it or you carried it out. Find the ``recommendation_id`` with
    ``list_recommendations``.

    Required: ``recommendation_id``, ``status`` (``acknowledged`` |
    ``in_progress`` | ``done`` | ``dismissed``; pass ``open`` to reopen).
    Optional: ``decided_by`` (who decided; default ``agent``).

    Any non-``open`` status stamps ``decided_at`` automatically; ``open``
    clears it. (v7.40.0 unified lifecycle — the old ``accepted``/``rejected``
    statuses are now ``acknowledged``/``dismissed``.)


    Returns ``{"result": …, "replayed": <bool>}``. ``replayed: true`` means this same call had already been executed before and you are given the result from then: **do not repeat it**.
    """
    from sqlalchemy import select, update as _update
    args = call.arguments or {}
    rec_id = _require(args, "recommendation_id")
    try:
        rec_id = int(rec_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="recommendation_id must be an integer",
        )
    status = _enum(args, "status", RECOMMENDATION_STATUSES, "done")
    decided_by = str(args.get("decided_by") or "agent")[:120]

    async with app_write() as session:
        async def _do():
            from app.timeutil import now_utc
            found = (await session.execute(
                select(Recommendation.id).where(Recommendation.id == rec_id)
            )).scalar_one_or_none()
            if found is None:
                return {"ok": False, "error": "not_found",
                        "recommendation_id": rec_id}
            values: dict[str, Any] = {"status": status}
            if status == "open":
                values["decided_at"] = None
                values["decided_by"] = None
            else:
                values["decided_at"] = now_utc().replace(tzinfo=None)
                values["decided_by"] = decided_by
            await session.execute(
                _update(Recommendation)
                .where(Recommendation.id == rec_id)
                .values(**values)
            )
            await session.commit()
            return {"ok": True, "recommendation_id": rec_id, "status": status}

        result, replayed = await _replay_or_execute(
            session,
            run_id=call.run_id,
            step_idx=call.step_idx,
            tool_name=call.name,
            args=args,
            do_work=_do,
            mode=getattr(call, "mode", "autonomous"),
        )
        return {"result": result, "replayed": replayed}


async def decide_decision(call: ToolCall):
    """Close or transition an EXISTING decision — use this instead of
    local.write to change a decision's status.

    Use it to ACKNOWLEDGE a logged decision, mark it IN PROGRESS, close it
    as DONE or DISMISS it (or back to ``open``). This moves the decision
    along the SAME lifecycle as alerts and recommendations; the ``decision``
    classification (no_action / deferred / escalated) is a separate attribute
    and is NOT changed here. Find the ``decision_id`` with ``list_decisions``.

    Required: ``decision_id``, ``status`` (``acknowledged`` | ``in_progress``
    | ``done`` | ``dismissed``; pass ``open`` to reopen). (v7.40.0)


    Returns ``{"result": …, "replayed": <bool>}``. ``replayed: true`` means this same call had already been executed before and you are given the result from then: **do not repeat it**.
    """
    from sqlalchemy import select, update as _update
    args = call.arguments or {}
    dec_id = _require(args, "decision_id")
    try:
        dec_id = int(dec_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="decision_id must be an integer",
        )
    status = _enum(args, "status", DECISION_STATUSES, "done")

    async with app_write() as session:
        async def _do():
            found = (await session.execute(
                select(Decision.id).where(Decision.id == dec_id)
            )).scalar_one_or_none()
            if found is None:
                return {"ok": False, "error": "not_found", "decision_id": dec_id}
            await session.execute(
                _update(Decision).where(Decision.id == dec_id).values(status=status)
            )
            await session.commit()
            return {"ok": True, "decision_id": dec_id, "status": status}

        result, replayed = await _replay_or_execute(
            session,
            run_id=call.run_id,
            step_idx=call.step_idx,
            tool_name=call.name,
            args=args,
            do_work=_do,
            mode=getattr(call, "mode", "autonomous"),
        )
        return {"result": result, "replayed": replayed}


# ── Calendar / tasks / approvals (time-anchored work) ───────────────


TASK_KINDS = ("task", "followup", "event", "reminder")
APPROVAL_RISKS = ("low", "medium", "high")


def _parse_due(args: dict, key: str, required: bool = True):
    """Parse an ISO datetime argument into a naive-UTC datetime. A
    string without an offset is interpreted in the business tz."""
    from app.timeutil import parse_to_utc
    raw = args.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise HTTPException(
                status_code=400, detail=f"missing required argument {key!r}",
            )
        return None
    try:
        return parse_to_utc(str(raw))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"argument {key!r} must be an ISO-8601 datetime, got {raw!r}",
        )


async def schedule_task(call: ToolCall):
    """Schedule a ONE-OFF future task for yourself (the AI employee).

    Use this to remember to do something ONCE later — a follow-up, a
    deferred check — instead of trying to do everything now. When ``due_at``
    arrives the system creates an autonomous run so you actually do the work;
    close it with ``complete_scheduled_item``.

    For RECURRING work (a periodic report/check), do NOT use this — ask the
    boss to set up a standing order ("Automatización") on a schedule; those
    re-fire deterministically and stay under the boss's control.

    Required: ``title``, ``due_at`` (ISO-8601; bare wall-clock time is
    read in the business timezone the prompt tells you).
    Optional: ``description``, ``kind`` (``task``|``followup``, default
    ``task``), ``priority`` (default ``medium``).


    Returns ``{"ok": true, "id": <created id>, "idempotency_key": …}``. If a constraint prevents it, ``{"ok": false, "error": "constraint", …}`` — read it: **do not assume it was saved**. If the signal is silenced ``{"suppressed": true}`` arrives and you must NOT retry it.
    It may be rejected with ``{"ok": false, "error": "self_scheduling_disabled"}`` or ``"open_task_cap_reached"`` (with ``max_open_tasks``): they are the manager's limits, not failures — read them and adapt the plan.
    """
    args = call.arguments or {}
    title = _require(args, "title")
    due_at = _parse_due(args, "due_at", required=True)
    kind = _enum(args, "kind", ("task", "followup"), "task") if args.get("kind") else "task"
    priority = _enum(args, "priority", PRIORITIES, "medium") if args.get("priority") else "medium"
    origin = "followup" if kind == "followup" else "agent"

    # Self-scheduling guardrail (deterministic, operator-owned — see
    # app/scheduling_policy.py). Refusals are returned (not raised) so the
    # employee reads the reason and adapts (ask the operator / put it on a
    # human's calendar with assign_task_to_user). Not persisted / not replayed:
    # the cap may clear later, so a refusal must never be cached.
    from app.scheduling_policy import (
        can_self_schedule, max_open_tasks as _max_open, count_open_agent_tasks,
    )
    if not await can_self_schedule():
        return {
            "ok": False, "error": "self_scheduling_disabled",
            "message": (
                "Self-scheduling is disabled by your manager: you cannot schedule future "
                "tasks for yourself. Ask your manager for it, or if the task is for a "
                "person use assign_task_to_user (it does not self-execute)."
            ),
        }
    _cap = await _max_open()
    if _cap and await count_open_agent_tasks() >= _cap:
        return {
            "ok": False, "error": "open_task_cap_reached", "max_open_tasks": _cap,
            "message": (
                f"You have reached the maximum of your own open tasks ({_cap}). Complete or "
                "cancel some scheduled task before creating another (or ask your manager to "
                "raise the limit)."
            ),
        }

    return await _persist(
        call,
        lambda key: ScheduledItem(
            idempotency_key=key,
            created_by_run_id=call.run_id,
            title=str(title),
            description=str(args.get("description") or ""),
            kind=kind,
            assignee="agent",
            origin=origin,
            status="scheduled",
            priority=priority,
            due_at=due_at,
            # No ``recurrence`` here: the drain fires an agent item ONCE. Recurring
            # self-work belongs to boss-enabled schedule automations (which re-fire
            # deterministically and stay bounded), not to an unbounded self-loop.
            last_run_id=call.run_id,
        ),
    )


async def assign_task_to_user(call: ToolCall):
    """Put a task or reminder on a HUMAN's calendar — and PLAN A MESSAGE to them.

    Use this to ask a person to do something (or remind them) at a given time, or
    to schedule an outbound heads-up to a user for later. It is THEIR item — you
    do NOT auto-execute it. When ``due_at`` arrives the system **sends them a
    message on their preferred channel** (WhatsApp if they have a phone and a
    WhatsApp channel is active, else email) with the title + description, and
    marks the item DONE. If they can't be reached (no channel) it flips to
    ``overdue`` so the operator sees it. So this is how you PLAN COMMUNICATIONS to
    users at a future time; for an IMMEDIATE message use ``log_communication``
    (status='ready') or the notify block.

    ``assigned_to`` accepts a name/alias/phone (resolved against the directory) —
    you do NOT need the raw email. ``description`` is what the person will read.

    Required: ``title``, ``assigned_to``, ``due_at``.
    Optional: ``description``, ``kind`` (``task``|``reminder``, default
    ``reminder``), ``priority`` (default ``medium``).


    Returns ``{"ok": true, "id": <created id>, "idempotency_key": …}``. If a constraint prevents it, ``{"ok": false, "error": "constraint", …}`` — read it: **do not assume it was saved**. If the signal is silenced ``{"suppressed": true}`` arrives and you must NOT retry it.
    If the recipient cannot be resolved against the directory it returns ``{"error": "…"}`` and **nothing is created**.
    """
    args = call.arguments or {}
    title = _require(args, "title")
    assigned_to = str(_require(args, "assigned_to")).strip()
    # Resolve a name/alias/nickname/phone against the user directory → the
    # person's email (so "assign to Juan" works, not only a raw email). If it
    # doesn't resolve to a real address, FAIL rather than persist a bogus
    # assignee like "juan" (which would silently go to no-one).
    if "@" not in assigned_to:
        resolved = await _resolve_directory_email(assigned_to)
        if not resolved:
            return {"error": (
                f"could not resolve '{assigned_to}' to a known person. Use "
                "find_user to look them up, then pass their email (or add them "
                "with create_user first). If several people match, be specific."
            )}
        assigned_to = resolved
    due_at = _parse_due(args, "due_at", required=True)
    kind = _enum(args, "kind", ("task", "reminder"), "reminder") if args.get("kind") else "reminder"
    priority = _enum(args, "priority", PRIORITIES, "medium") if args.get("priority") else "medium"

    return await _persist(
        call,
        lambda key: ScheduledItem(
            idempotency_key=key,
            created_by_run_id=call.run_id,
            title=str(title),
            description=str(args.get("description") or ""),
            kind=kind,
            assignee="user",
            origin="agent",
            status="pending",
            priority=priority,
            due_at=due_at,
            assigned_to=str(assigned_to).strip().lower(),
            last_run_id=call.run_id,
        ),
    )


async def complete_scheduled_item(call: ToolCall):
    """Close a scheduled item you just handled.

    Call this at the end of a ``SCHEDULED_TASK_DUE`` run (the prompt
    gives you the ``item_id``) so it stops showing as in-progress.

    Required: ``item_id``.
    Optional: ``result`` (a short note on what you did), ``status``
    (``done`` default, or ``cancelled``).


    Returns ``{"result": …, "replayed": <bool>}``. ``replayed: true`` means this same call had already been executed before and you are given the result from then: **do not repeat it**.
    """
    from sqlalchemy import select, update as _update
    args = call.arguments or {}
    item_id = _require(args, "item_id")
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="item_id must be an integer")
    status = _enum(args, "status", ("done", "cancelled"), "done") if args.get("status") else "done"

    async with app_write() as session:
        async def _do():
            from app.timeutil import now_utc
            found = (await session.execute(
                select(ScheduledItem.id).where(ScheduledItem.id == item_id)
            )).scalar_one_or_none()
            if found is None:
                return {"ok": False, "error": "not_found", "item_id": item_id}
            await session.execute(
                _update(ScheduledItem)
                .where(ScheduledItem.id == item_id)
                .values(
                    status=status,
                    completed_at=now_utc().replace(tzinfo=None),
                    result_md=str(args.get("result") or ""),
                    last_run_id=call.run_id,
                )
            )
            await session.commit()
            # v7.38.0 — settle a linked recommendation/decision so the whole
            # chain (recommendation/decision → task → result) is traceable.
            try:
                from app.routes.scheduling_routes import settle_source_on_complete
                item_obj = (await session.execute(
                    select(ScheduledItem).where(ScheduledItem.id == item_id)
                )).scalar_one_or_none()
                if item_obj is not None:
                    await settle_source_on_complete(session, item_obj)
            except Exception:
                logger.debug("settle_source_on_complete hook failed", exc_info=True)
            return {"ok": True, "item_id": item_id, "status": status}

        result, replayed = await _replay_or_execute(
            session,
            run_id=call.run_id,
            step_idx=call.step_idx,
            tool_name=call.name,
            args=args,
            do_work=_do,
            mode=getattr(call, "mode", "autonomous"),
        )
        return {"result": result, "replayed": replayed}


async def request_approval(call: ToolCall):
    """Park a consequential action for the operator's go/no-go.

    Use this — instead of acting — when an action is risky, irreversible
    or beyond your delegated authority (sending money, deleting data,
    emailing a VIP, anything you are unsure you are allowed to do). The
    operator approves it (then the system asks you to carry it out) or
    rejects it with feedback you will remember.

    Required: ``title``, ``action_type``, ``summary`` (what you want to do
    and WHY, for the operator).
    Optional: ``action_payload`` (dict), ``risk``
    (``low``|``medium``|``high``, default ``medium``), ``requested_for``
    (email the action concerns).

    **WHO EXECUTES IT — read this before choosing ``action_type``.**

    Two values are RESERVED and the platform runs them itself on approve:

    * ``tool_call`` — minted by the engine when a connector write hits a
      write-policy gate. You never write this one by hand. If you try, this
      tool REJECTS it and tells you what to do instead: invoke the connector
      tool normally and let the platform park it for you. Deciding by yourself
      that a write "needs approval", without attempting it, produces an
      approval nobody can execute — the operator decides and nothing happens.
    * ``automation_reaction`` — the reaction of an automation you defined.

    **Any other value is just a LABEL for the operator** (``send_email``,
    ``load_products``, ``cancel_order``…). Pick whatever describes the action
    in plain words. But be clear about what that means:

    * **YOU execute it**, in the turn that follows the approval. Nothing else
      will.
    * ``action_payload`` is **your own note-to-self** so that future turn has
      what it needs. **The platform does not read it, interpret it, or act on
      it.** Putting a file path, a record count or a "scope" in there does not
      make anything happen — it is a reminder, not an instruction.

    So never park an action you do not know how to carry out. If you are
    missing the capability, the data or the permission, **say so now** and
    record it with ``log_decision`` instead of asking for approval: an
    approved action nobody can execute wastes the operator's decision and
    leaves the task silently unfinished.


    Returns ``{"ok": true, "id": <created id>, "idempotency_key": …}``. If a constraint prevents it, ``{"ok": false, "error": "constraint", …}`` — read it: **do not assume it was saved**. If the signal is silenced ``{"suppressed": true}`` arrives and you must NOT retry it.
    It stays ``pending`` until the operator decides.
    """
    args = call.arguments or {}
    title = _require(args, "title")
    action_type = _require(args, "action_type")
    summary = _require(args, "summary")

    # v7.171.0 (INC-AGO-136) — DETERMINISM of the approval protocol.
    #
    # A `tool_call` is only executable if the platform parked the real request BEFORE: the
    # `request_ref` (Composio) or the `model`/`method` (engine gate) is what is replayed on
    # approval. A prudent agent sometimes skips the step —it sees "this connector asks for
    # approval", never gets to invoke the tool and records the approval by hand—: it looked
    # like an employee that sometimes parks and sometimes does not, and the resulting approval
    # died in a 422 WHEN THE OPERATOR HAD ALREADY DECIDED. Rejecting it here, with the path
    # written out, turns that drift into an error correctable in the same turn.
    if str(action_type).strip() == "tool_call":
        _payload = args.get("action_payload")
        _p = _payload if isinstance(_payload, dict) else {}
        _reproducible = bool(str(_p.get("request_ref") or "").strip()) or bool(
            str(_p.get("model") or "").strip() and str(_p.get("method") or "").strip())
        if not _reproducible:
            return {
                "ok": False,
                "error": "approval_not_reproducible",
                "message": (
                    "Do not record an approval of type 'tool_call' yourself: without the "
                    "request the platform parks, on approval there would be nothing to execute "
                    "and the operator's decision would be lost. INVOKE the connector's tool "
                    "NORMALLY: if approval is needed, the system intercepts the call, parks it "
                    "and returns the EXACT payload (with its request_ref) for you to record. If "
                    "what you want approved is NOT a tool call, use another descriptive "
                    "action_type."
                ),
            }
    risk = _enum(args, "risk", APPROVAL_RISKS, "medium") if args.get("risk") else "medium"
    # Same soft normalisation as log_communication: a directory match becomes
    # the canonical email, an external reference stays verbatim.
    requested_for = None
    if args.get("requested_for"):
        raw = str(args["requested_for"]).strip().lower()
        requested_for = (await _resolve_directory_email(raw)) or raw

    return await _persist(
        call,
        lambda key: Approval(
            idempotency_key=key,
            created_by_run_id=call.run_id,
            title=str(title),
            summary_md=str(summary),
            action_type=str(action_type),
            action_payload_json=_serialise_extra(args.get("action_payload")),
            risk=risk,
            requested_for=requested_for,
            status="pending",
        ),
    )


async def mark_approval_executed(call: ToolCall):
    """Close an APPROVED action's loop after you actually carry it out.

    Call this at the end of an ``APPROVAL_GRANTED`` run (the prompt gives you
    the ``approval_id``) once you've performed the action, so the operator can
    tell "approved" from "actually done". Sets the approval to ``executed``.

    Required: ``approval_id``.  Optional: ``result`` (short note of what you did).


    Returns ``{"result": …, "replayed": <bool>}``. ``replayed: true`` means this same call had already been executed before and you are given the result from then: **do not repeat it**.
    """
    from sqlalchemy import select, update as _update
    args = call.arguments or {}
    approval_id = _require(args, "approval_id")
    try:
        approval_id = int(approval_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="approval_id must be an integer")

    async with app_write() as session:
        async def _do():
            from app.timeutil import now_utc
            found = (await session.execute(
                select(Approval.status).where(Approval.id == approval_id)
            )).scalar_one_or_none()
            if found is None:
                return {"ok": False, "error": "not_found", "approval_id": approval_id}
            # Only an APPROVED action may be marked executed. Idempotent on a
            # re-call. A pending/rejected approval must NEVER jump to executed —
            # that would erase the operator's go/no-go and log an unapproved
            # action as done. Status is operator-owned; the agent only closes
            # the loop on what the operator already approved.
            if found == "executed":
                return {"ok": True, "approval_id": approval_id, "status": "executed", "note": "already executed"}
            if found != "approved":
                return {
                    "ok": False, "error": "not_approvable", "status": found,
                    "message": (
                        f"You cannot mark as executed an approval in state '{found}'. "
                        "Only what your manager has approved is executed."
                    ),
                }
            await session.execute(
                _update(Approval)
                .where(Approval.id == approval_id)
                .values(
                    status="executed",
                    executed_at=now_utc().replace(tzinfo=None),
                    executed_by_run_id=call.run_id,
                    execution_result=str(args.get("result") or ""),
                )
            )
            await session.commit()
            return {"ok": True, "approval_id": approval_id, "status": "executed"}

        result, replayed = await _replay_or_execute(
            session,
            run_id=call.run_id,
            step_idx=call.step_idx,
            tool_name=call.name,
            args=args,
            do_work=_do,
            mode=getattr(call, "mode", "autonomous"),
        )
        return {"result": result, "replayed": replayed}


# ── Automatizaciones: conversational management (boss, interactive only) ──
#
# The boss configures the employee's standing orders by TALKING to it. These
# wrap the shared service (same store as the forms). Gated to the BOSS CHAT so
# an external channel user / an autonomous run can never reprogram the employee.
# The LLM does the natural-language → structured translation itself, validating
# against ``get_automation_catalog`` before saving, and confirms with the boss.

async def _automations_guard(call: ToolCall):
    """Allow only when the calling run is a BOSS CHAT turn.

    The agent always reaches the app MCP over JSON-RPC with a hardcoded
    ``mode`` (that path carries no role signal), so we derive the REAL surface
    from the calling run instead: chat turns persist ``agent_runs.mode='chat'``
    and inject their ``run_id`` into the prompt, whereas autonomous + channel
    runs are ``'autonomous'``. Deny-by-default when the run is missing or
    unresolved — an external/channel/autonomous caller must never be able to
    create, change, enable or even enumerate the employee's standing orders."""
    run_id = getattr(call, "run_id", None)
    if run_id:
        try:
            from app.app_models.runtime import AgentRun
            async with app_session() as s:
                mode = (await s.execute(
                    select(AgentRun.mode).where(AgentRun.id == int(run_id))
                )).scalar_one_or_none()
            if mode == "chat":
                # v7.88.0 — a chat turn is necessary but NOT sufficient. Authoring/
                # changing/enabling/running the employee's standing orders is a
                # GOVERNING change, so require a REAL HUMAN ADMIN (the boss),
                # mirroring set_knowledge + the approvals gate. A rank-and-file
                # member's chat also persists mode='chat', so without this any
                # member could create/edit/run automations.
                actor = await _run_serving_user(call.run_id)
                # human_surface: this branch only runs for mode='chat' rows,
                # which are minted exclusively by the boss-chat endpoint — a
                # human typed this. Without it, deployments whose autonomous
                # email coincides with the boss's (admin@wai.com) locked the
                # real admin out of their own employee's automations.
                if await _is_real_human_admin(actor, human_surface=True):
                    return None
                return {"error": "Only an administrator can manage the employee's "
                                 "automations."}
        except Exception:  # deny on any lookup failure, but make it VISIBLE —
            # a silent ``pass`` here once turned a missing ``select`` import into
            # a permanent "deny" that looked like a policy block.
            logger.warning("automations guard: run %s lookup failed", run_id, exc_info=True)
    return {"error": "Automations are only managed from the chat with your manager."}


async def get_automation_catalog(call: ToolCall):
    """List the REAL building blocks (signals, reactions, entities, channels)
    so you never invent an automation the employee can't actually run."""
    guard = await _automations_guard(call)
    if guard:
        return guard
    from app.config import AGENT_APP
    from app.routes.manifest_routes import _SYSTEM_EVENT_SPECS
    from app import automations_service as asvc
    from app.automations_service import OFFERED_SIGNAL_TYPES as _AUTOMATION_SIGNALS, REACTION_TYPES as _AUTOMATION_REACTIONS
    aa = AGENT_APP if isinstance(AGENT_APP, dict) else {}
    entities = [
        {"slug": e.get("slug") or e.get("table"), "label": e.get("label") or e.get("slug"),
         "table": e.get("table") or e.get("slug"), "source": e.get("source") or "project"}
        for e in (aa.get("domain_entities") or [])
    ]
    # v7.134.0 — only OFFER events usable as a "when" trigger. DELEGATED_TASK /
    # APPROVAL_GRANTED are internal plumbing (triggerable=False) → hidden here.
    events = [{"type": t} for t in (aa.get("event_types") or [])] + [
        {"type": ev, "label": meta["label"], "system": True}
        for ev, meta in _SYSTEM_EVENT_SPECS.items()
        if meta.get("triggerable", True)
    ]
    # Channels (for the notify reaction) + whether the project DB is reachable
    # (gates the "project" data source for conditions). We read the composed
    # connection URL (empty unless host+database are set), NOT the
    # PROJECT_DB_RO credential dict, which belongs to app.database's factory.
    from app.config import DATABASE_URL as _PROJECT_DB_URL
    channels = []
    try:
        from app.app_models.channels import Channel
        async with app_session() as s:
            for c in (await s.execute(select(Channel))).scalars().all():
                channels.append({"id": c.id, "label": c.display_name, "kind": c.kind})
    except Exception:
        pass
    return {
        "signal_types": list(_AUTOMATION_SIGNALS),
        # OUTCOMES (what the employee does). Approval is a separate axis
        # (requires_approval); cost is a separate axis (execution_mode).
        "reaction_types": list(_AUTOMATION_REACTIONS),
        "execution_modes": ["deterministic", "agent"],
        "entities": entities,
        "events": events,
        "channels": channels,
        "project_data_available": bool(_PROJECT_DB_URL),
        # COST-FIRST: condition modes ordered CHEAPEST → costliest. Prefer the
        # cheapest that answers the question; ``agent`` (an LLM run on EVERY check)
        # is the LAST resort, only for irreducible judgement.
        "condition_modes": ["always", "watch", "checks", "data", "sql", "agent"],
        "condition_cost": {
            "always": "0 tokens", "checks": "0 tokens (calls MCP tools in API mode)",
            "watch": "0 tokens (1 MCP tool; per-item dedup: fires ONLY on new/escalated)",
            "data": "0 tokens (SQL)", "sql": "0 tokens (SELECT)",
            "agent": "EXPENSIVE: 1 LLM run on EVERY check",
        },
        # execution_mode per reaction: deterministic (templated, 0 tokens) is
        # available for create_task/notify; act/generate_deliverable need the agent.
        "reaction_cost": {
            "create_task": "deterministic = 0 tokens (template) · agent = 1 run",
            "notify": "deterministic = 0 tokens (sends reaction_spec.message as is "
                      "through the channel) · agent = 1 run (writes it with context)",
            "act": "1 run", "generate_deliverable": "1 run",
        },
        # v7.134.0 — tokens ``_render_templates`` substitutes in args/message (e.g. the
        # notify's reaction_spec.message). The rest is left literal.
        "context_variables": [
            {"token": "{{tenant}}", "label": "Business name"},
            {"token": "{{now}}", "label": "Current date/time (offset: {{now+2h}}, {{now-30m}})"},
        ],
        "aggregates": ["count", "sum", "avg", "min", "max"],
        "operators": ["gt", "lt", "gte", "lte", "eq", "no_activity"],
        "check_operators": ["gte", "gt", "lte", "lt", "eq", "non_empty", "empty",
                            "contains", "not_contains"],
        "defaults": {"enabled": False, "requires_approval": True,
                     "execution_mode": "agent", "combine": "all", "on_error": "alert"},
        # Real columns of the LOCAL store tables — when you write a data/sql
        # condition over them, use THESE column names (e.g. channel_messages
        # uses received_at, NOT created_at). Never invent a column.
        "local_schema": await asvc.local_schema(),
    }


async def list_automations(call: ToolCall):
    """List the employee's standing orders (with their live state) so you can
    explain/diagnose them to the boss."""
    guard = await _automations_guard(call)
    if guard:
        return guard
    from app import automations_service as asvc
    return {"items": await asvc.list_automations()}


async def create_automation(call: ToolCall):
    """Create a standing order. Build a STRUCTURED automation from the boss's
    request using ``get_automation_catalog`` (don't invent entities/channels),
    CONFIRM the 'When → only if it checks → then' with the boss, then call this.

    The model has THREE parts:
      • WHEN (``signal_type`` + ``signal_spec``) — the trigger that WAKES it:
        ``schedule`` ({"cron": "0 9 * * 1"}), ``business_event`` ({"event_type"} from
        the ones the catalog returns), or ``manual``. (An inbound message is NOT a
        trigger: the employee already handles it — there is no ``channel_message``.)
      • IF (``condition_spec``) — a SECOND gate, evaluated after the signal.
        **GOLDEN RULE — COST FIRST: ALWAYS use the CHEAPEST option that answers the
        condition.** If it is a signal checkable with a tool/data, use
        ``checks``/``data``/``sql`` (0 tokens); keep ``agent`` (LLM on EVERY check,
        expensive) ONLY for irreducible subjective judgement. Modes:
          - {"mode":"checks", "combine":"all|any", "on_error":"alert",
             "checks":[{"name":"errors in logs","connector":"<catalog mcp>",
             "tool":"query_logs","arguments":{"namespace":"{{tenant}}","level":"error",
             "since_seconds":900},"extract":"count","op":"gte","threshold":1}, …]}
             → calls MCP tools in API MODE (no LLM, 0 tokens) and only lets it through
             if the signals are met. It is the cheap gate for "watch and go in if…".
             ``op``: gte/gt/lte/lt/eq/non_empty/empty/contains. ``extract``: path to the
             value (empty = smart: list→length, dict with count→count).
          - {"mode":"watch","watch":{"connector":"<mcp>","tool":"<tool that returns a LIST>",
             "arguments":{…},"items_at":"<path to the list>","key":"<id field of each item>",
             "signature":"<optional field: if it changes = escalated → re-fires>","resolve_when_absent":true,
             "ignore_keys":["noise*","known"],"silence_until":"<ISO: silences deploy/maintenance>"}}
             → WATCH WITH MEMORY (0 tokens, 1 MCP tool): fires ONLY for NEW or escalated
             items and auto-resolves the ones that disappear. For recurring monitors that must
             NOT re-alert about the same thing every time (incidents, leads, tickets…). The
             reaction receives ``fresh_items``. GENERIC: only change the tool/key per domain.
        ⚠️ **NEVER GUESS `items_at`/`key`/`signature`/`extract`.** They are paths into the
        tool's REAL response, not generic names. BEFORE setting them: call the tool ONCE
        (with these SAME arguments) and LOOK at the JSON — use the exact path to the list
        and the EXACT field names of each item. `items`/`count` are almost always WRONG
        (e.g. `tenant_error_logs` returns `{tenants_with_errors:[{tenant,
        incident_count,…}]}` → items_at=`tenants_with_errors`, key=`tenant`,
        signature=`incident_count`; NOT `items`/`count`). `create_automation` VALIDATES
        against the real response and REJECTS (422) with the available fields if they do not
        match — do not try to dodge it by guessing; fix the path by looking at the JSON. You
        can test the condition (dry-run) to see the response, how `items_at` resolves and the
        per-item classification before saving.
          - {"mode":"data","source":"local|project","table","aggregate","column",
             "operator","threshold"} — SQL aggregate. {"mode":"sql","sql":"<SELECT
             true/false>"}. {"mode":"always"}.
          - {"mode":"agent","instructions":"<what to check in natural language>"} —
             LAST resort (expensive). Before using it, consider a ``checks``.
        Any mode can carry a cheap guard ``"pre": {…checks/data/sql…}`` that must hold
        FIRST: that way an ``agent`` runs the LLM ONLY when the cheap pre-gate has
        already given green. Use ``project`` source ONLY when the catalog says
        ``project_data_available``.
      • THEN — 4 OUTCOMES (``reaction_type``) + independent axes:
        ``notify`` (notify; ``reaction_spec.channel_id`` + ``recipient`` + the message
        in ``reaction_spec.message`` —it admits {{tenant}}/{{now}}—),
        ``create_task`` (a TASK to DELEGATE to a person, POSTPONE or leave in FOLLOW-UP
        —NOT for what the employee can do now, that is what ``act`` is for—;
        ``reaction_spec.title``/``description``/``assignee`` (agent|user)/
        ``assigned_to``/``priority``),
        ``act`` (execute now; ``reaction_instructions``), ``generate_deliverable``.
        Separate axes: ``requires_approval`` (does it ask for your OK? — this IS the old
        "draft & approve" = act+approval), notify-when-done (``reaction_spec.notify``),
        and **``execution_mode``**: ``deterministic`` = direct template, 0 tokens
        (for ``create_task``/``notify`` whose content is a template — PREFER IT)
        vs ``agent`` = 1 run (needed for ``act``/``generate_deliverable`` or if it has to
        be written). A templated ``create_task`` must NOT spend a run: use
        ``execution_mode:"deterministic"``.

    FORMAT: write ``reaction_instructions`` (and the ``agent`` condition) in MARKDOWN —
    ``##`` headings, lists, numbered steps — structured like a skill. Plain text is neither
    understood nor displayed well.
    SCHEDULE: a ``schedule``'s ``cron`` is interpreted in the employee's TIME ZONE (the
    catalog's, ``business_tz``); for another zone add ``signal_spec.tz`` with an IANA name
    (e.g. ``"America/Mexico_City"``).

    Required: ``title``, ``description``, ``signal_type``, ``reaction_type``. Optional:
    ``signal_spec``, ``condition_spec``, ``signal_summary``, ``reaction_spec``,
    ``reaction_instructions``, ``cooldown_sec``, ``serves_user_email``,
    ``enabled`` (default false — confirm before enabling).

    ⚠️ ``description`` is MANDATORY (HTML) and must be **STRUCTURED and DETAILED**, not a poor
    sentence. A single line like "Every 15 min check restarts; only if there is a new one
    investigate" is NOT acceptable. Use THREE sections with a heading (``<b>`` or ``<h4>``) and
    rich content:
      • **When** — the signal and its exact cadence (e.g. "every 30 min", the cron/time zone).
      • **Only if** — what is checked, with which tool/data, which threshold and how it
        combines (any/all); if it is NOT met, what does NOT happen.
      • **What it does** — the steps of the reaction, what it produces, whom it notifies and
        whether it asks for approval.
    Simple HTML (``<p>``, ``<ul><li>``, ``<b>``, ``<h4>``); no scripts. It is what a human reads
    to understand the automation at a glance — invest real detail in it. Write it in the
    language of the people who will read it (the operator's).

    MANDATORY VALIDATION (do not dodge it): create/update validate the structure AND the
    COMPLETENESS and reject with 422 + a clear message when something is missing or wrong —
    a ``checks`` without checks, a check without connector/tool, a comparator that needs a
    threshold with the threshold EMPTY (e.g. ``contains`` without text or ``gte`` without a
    number → a check that compares nothing), an invalid ``combine``, or ``items_at``/``key``/
    ``extract`` paths that do not match the real response. If you get a 422, READ the message
    and FIX the config (look at the tool's real JSON); do not retry blindly. Test the
    condition (dry-run) before saving to see the calls, the results, the extracted value and
    how the any/all reaches the verdict.

    For a ``data`` or ``sql`` condition over the LOCAL store, ALWAYS take column
    names from the catalog's ``local_schema`` (``{table: [columns]}``) — never
    guess. A frequent mistake: ``channel_messages`` has NO ``created_at``; its
    timestamp column is ``received_at``. If a column isn't in ``local_schema``,
    it doesn't exist.
    """
    guard = await _automations_guard(call)
    if guard:
        return guard
    from app import automations_service as asvc
    args = call.arguments or {}
    try:
        out = await asvc.create_automation(args, source="chat")
    except ValueError as exc:
        return {"error": str(exc)}
    # SOFT GUARD (cost-first steering, non-blocking): if the LLM authored a costly
    # gate for something that looks deterministically checkable, nudge it toward the
    # cheap option. Never rejects — the automation is created; the hint rides along.
    hint = _automation_cost_hint(args)
    if hint and isinstance(out, dict):
        out["cost_hint"] = hint
    return out


def _automation_cost_hint(args: dict) -> "str | None":
    """A gentle 'you may be spending tokens you don't need to' nudge for the
    employee's LLM (see get_automation_catalog → condition_cost / reaction_cost)."""
    cond = args.get("condition_spec") or {}
    mode = str(cond.get("mode") or "").lower()
    has_pre = isinstance(cond.get("pre"), dict) and bool(cond.get("pre"))
    if mode == "agent" and not has_pre:
        return ("COST: the condition is 'agent' → it launches an LLM run on EVERY check. "
                "If what you check is a signal measurable with a tool/data, change it to "
                "'checks' (0 tokens) or add a cheap 'pre' guard; keep 'agent' only for "
                "subjective judgement.")
    rtype = str(args.get("reaction_type") or "").lower()
    if rtype == "create_task" and str(args.get("execution_mode") or "").lower() != "deterministic":
        return ("COST: create_task with execution_mode 'agent' spends a run just to write "
                "a task. If the title/description are a template, use "
                "execution_mode:'deterministic' (0 tokens).")
    return None


async def update_automation(call: ToolCall):
    """Change a standing order (e.g. threshold, recipients, enable/disable).
    Required: ``id`` + the fields to change.

    Its ORIGIN is sealed: ``template_key``, ``template_version`` and
    ``derived_from_id`` are set when the automation is created and can never be
    changed — sending a different value is rejected. A standing order that came
    from a DEPLOYMENT template is not edited into something else: call
    ``evolve_automation`` to create a variant derived from it, and edit that."""
    guard = await _automations_guard(call)
    if guard:
        return guard
    args = call.arguments or {}
    aid = _require(args, "id")
    from app import automations_service as asvc
    body = {k: v for k, v in args.items() if k != "id"}
    out = await asvc.update_automation(int(aid), body)
    if out is None:
        return {"error": f"automation {aid} not found"}
    return out


async def evolve_automation(call: ToolCall):
    """Create a NEW standing order derived from an existing one, with your changes.

    This is the way to "change" something that came from a DEPLOYMENT template:
    the original is left untouched and a copy is born pointing at it
    (``derived_from_id``), keeping the template it came from so the lineage is
    not lost. The copy is born DISABLED — a person reviews an evolution before
    it fires.

    Required: ``id`` (the one to evolve). Optional: any automation field to
    change in the copy (``title``, ``condition_spec``, ``reaction_spec``…). If
    you don't rename it, the copy is suffixed so the two are told apart."""
    guard = await _automations_guard(call)
    if guard:
        return guard
    args = call.arguments or {}
    aid = _require(args, "id")
    from app import automations_service as asvc
    changes = {k: v for k, v in args.items() if k != "id"}
    out = await asvc.evolve_automation(int(aid), changes)
    if out is None:
        return {"error": f"automation {aid} not found"}
    return {**out, "note": ("Created as an evolution: the original has not been touched. "
                            "It is born disabled — review it and enable it when appropriate.")}


async def set_automation_enabled(call: ToolCall):
    """Activate or pause a standing order. Required: ``id``, ``enabled`` (bool).
    Confirm with the boss before enabling one that ACTS without approval."""
    guard = await _automations_guard(call)
    if guard:
        return guard
    args = call.arguments or {}
    aid = _require(args, "id")
    from app import automations_service as asvc
    out = await asvc.update_automation(int(aid), {"enabled": bool(args.get("enabled"))})
    if out is None:
        return {"error": f"automation {aid} not found"}
    return out


async def test_automation(call: ToolCall):
    """Dry-run a data-condition automation now (does NOT run the reaction).
    Required: ``id``."""
    guard = await _automations_guard(call)
    if guard:
        return guard
    args = call.arguments or {}
    aid = int(_require(args, "id"))
    from app import automations_service as asvc
    a = await asvc.get_automation(aid)
    if a is None:
        return {"error": f"automation {aid} not found"}
    if a["signal_type"] != "data_condition":
        return {"evaluable": False, "reason": "not a pollable data condition"}
    value, ctx = await asvc.evaluate_data_condition(a["signal_spec"])
    return {"would_fire": asvc.condition_met(value, a["signal_spec"]), "value": value, "context": ctx}


async def run_automation(call: ToolCall):
    """Fire a standing order NOW (the boss said "run it"/"launch it"). Works
    even for a ``manual`` automation that's disabled — it's an explicit command.
    The reaction is queued as a run. Required: ``id``."""
    guard = await _automations_guard(call)
    if guard:
        return guard
    args = call.arguments or {}
    aid = int(_require(args, "id"))
    from app import automations_service as asvc
    out = await asvc.fire_now(aid, by="boss-chat")
    if out is None:
        return {"error": f"automation {aid} not found"}
    return out


# ── Read tools (duplicate prevention) ───────────────────────────────
#
# v6.15.0: before creating an alert / recommendation / decision the
# agent MUST look at what already exists — otherwise every run re-files
# the same anomaly and the operator drowns in duplicates. These tools
# are read-only, return a COMPACT projection (never the full row) and
# are NOT logged through _replay_or_execute (reads need no audit row).

_LIST_CAP = 50


def _clip(s: Any, n: int = 280) -> str:
    text_val = str(s or "")
    return text_val if len(text_val) <= n else text_val[: n - 1] + "…"


async def _list_rows(model, order_col, filters: list, limit: int) -> list:
    from sqlalchemy import select
    async with app_session() as session:
        stmt = select(model).order_by(order_col.desc()).limit(limit)
        for cond in filters:
            stmt = stmt.where(cond)
        return list((await session.execute(stmt)).scalars().all())


async def _count_by(model, group_col, filters: list) -> dict:
    """``{value: count}`` grouped by ``group_col`` over ALL matching rows
    (NO limit). Lets a list tool report accurate totals even when the row
    sample is capped — so "how many X are there?" is answered exactly
    instead of "at least N" once the cap is hit."""
    from sqlalchemy import func, select
    async with app_session() as session:
        stmt = select(group_col, func.count()).group_by(group_col)
        for cond in filters:
            stmt = stmt.where(cond)
        rows = (await session.execute(stmt)).all()
        return {str(k): int(v) for k, v in rows if k is not None}


def _empty_sample_note(
    sample_count: int, status_filter: str, grand_total: int,
) -> Optional[str]:
    """Hint for the common "I queried the default status and got nothing,
    so I assumed there were none" trap. When the status-filtered SAMPLE
    is empty but rows DO exist in other statuses, tell the agent to widen
    the query (``status='all'``) or read ``totals`` — instead of
    concluding the table is empty / has only one row."""
    if sample_count == 0 and status_filter not in ("all", "") and grand_total > 0:
        return (
            f"0 with status={status_filter!r}, but there are {grand_total} in total "
            "in other statuses — repeat with status='all' or use the "
            "'totals' block to see them."
        )
    return None


async def list_alerts(call: ToolCall):
    """List existing alerts so you do NOT create duplicates.

    Call this BEFORE ``upsert_alert``. If an OPEN alert already covers
    the same problem (same target + same root cause), do not create
    another one — mention the existing id in your summary instead.

    Optional filters: ``status`` (default ``open``; pass ``all`` for
    every status), ``severity``, ``target_entity_id``, ``q`` (substring
    on title), ``limit`` (default 20, max 50).

    The response ALWAYS includes a ``totals`` block with the exact count
    of ALL alerts and the breakdown by status and by severity (ignoring
    the ``status``/``severity``/``limit`` filters, honouring only
    ``q``/``target_entity_id``). Use ``totals`` — not ``count`` — to
    answer "how many alerts are there?": ``count`` is only the size of
    the returned sample (capped at 50, and filtered by status). When the
    default ``status=open`` sample is empty but alerts exist in other
    statuses, a ``note`` tells you to pass ``status='all'`` so you don't
    wrongly conclude there are none.

    Optional filters: ``dedupe_key`` (the exact deduplication key), ``impact`` and ``urgency``.
    """
    args = call.arguments or {}
    status = (args.get("status") or "open").strip().lower()
    limit = min(int(args.get("limit") or 20), _LIST_CAP)
    filters = []
    if status != "all":
        filters.append(Alert.status == status)
    if args.get("severity"):
        filters.append(Alert.severity == str(args["severity"]).lower())
    if args.get("impact"):
        filters.append(Alert.impact == str(args["impact"]).lower())
    if args.get("urgency"):
        filters.append(Alert.urgency == str(args["urgency"]).lower())
    if args.get("dedupe_key"):
        filters.append(Alert.dedupe_key == str(args["dedupe_key"]).strip().lower()[:200])
    if args.get("target_entity_id"):
        filters.append(Alert.target_entity_id == str(args["target_entity_id"]))
    if args.get("q"):
        filters.append(Alert.title.ilike(f"%{args['q']}%"))
    rows = await _list_rows(Alert, Alert.created_at, filters, limit)
    # Accurate totals over the WHOLE table (only the content filters
    # q/target apply) so the agent never under-reports because of the
    # default open-status filter or the 50-row cap.
    base_filters = []
    if args.get("target_entity_id"):
        base_filters.append(Alert.target_entity_id == str(args["target_entity_id"]))
    if args.get("q"):
        base_filters.append(Alert.title.ilike(f"%{args['q']}%"))
    by_status = await _count_by(Alert, Alert.status, base_filters)
    by_severity = await _count_by(Alert, Alert.severity, base_filters)
    out = {
        "count": len(rows),
        "totals": {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_severity": by_severity,
        },
        "alerts": [
            {
                "id": r.id,
                "severity": r.severity,
                "impact": r.impact,
                "urgency": r.urgency,
                "confidence": r.confidence,
                "status": r.status,
                "title": r.title,
                "description": _clip(r.description),
                "dedupe_key": r.dedupe_key,
                "occurrences": r.occurrences,
                "target_entity_kind": r.target_entity_kind,
                "target_entity_id": r.target_entity_id,
                "created_at": iso_utc(r.created_at),
                "created_by_run_id": r.created_by_run_id,
            }
            for r in rows
        ],
    }
    note = _empty_sample_note(len(rows), status, sum(by_status.values()))
    if note:
        out["note"] = note
    return out


async def list_recommendations(call: ToolCall):
    """List existing recommendations so you do NOT repeat them.

    Call this BEFORE ``register_recommendation``. If an OPEN
    recommendation already proposes the same action for the same
    target, skip it (or reference its id).

    Optional filters: ``status`` (default ``open``; ``all`` for
    every status), ``priority``, ``target_entity_id``, ``q``
    (substring on title), ``limit`` (default 20, max 50).

    The response ALWAYS includes a ``totals`` block with the exact count
    of ALL recommendations and the breakdown by status and by priority
    (ignoring the ``status``/``priority``/``limit`` filters, honouring
    only ``q``/``target_entity_id``). Use ``totals`` — not ``count`` — to
    answer "how many recommendations are there?": ``count`` is only the
    size of the returned sample. When the default ``status=open``
    sample is empty but other statuses exist, a ``note`` tells you to
    pass ``status='all'`` (so you don't conclude "there's only one").

    Optional filters: ``impact`` and ``urgency``.
    """
    args = call.arguments or {}
    status = (args.get("status") or "open").strip().lower()
    limit = min(int(args.get("limit") or 20), _LIST_CAP)
    filters = []
    if status != "all":
        filters.append(Recommendation.status == status)
    if args.get("priority"):
        filters.append(Recommendation.priority == str(args["priority"]).lower())
    if args.get("impact"):
        filters.append(Recommendation.impact == str(args["impact"]).lower())
    if args.get("urgency"):
        filters.append(Recommendation.urgency == str(args["urgency"]).lower())
    if args.get("target_entity_id"):
        filters.append(Recommendation.target_entity_id == str(args["target_entity_id"]))
    if args.get("q"):
        filters.append(Recommendation.title.ilike(f"%{args['q']}%"))
    rows = await _list_rows(Recommendation, Recommendation.created_at, filters, limit)
    # Accurate totals over the WHOLE table (only the content filters
    # q/target apply) so the agent never under-reports because of the
    # default pending-status filter or the 50-row cap.
    base_filters = []
    if args.get("target_entity_id"):
        base_filters.append(
            Recommendation.target_entity_id == str(args["target_entity_id"])
        )
    if args.get("q"):
        base_filters.append(Recommendation.title.ilike(f"%{args['q']}%"))
    by_status = await _count_by(Recommendation, Recommendation.status, base_filters)
    by_priority = await _count_by(Recommendation, Recommendation.priority, base_filters)
    out = {
        "count": len(rows),
        "totals": {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_priority": by_priority,
        },
        "recommendations": [
            {
                "id": r.id,
                "priority": r.priority,
                "impact": r.impact,
                "urgency": r.urgency,
                "confidence": r.confidence,
                "status": r.status,
                "title": r.title,
                "rationale": _clip(r.rationale_md),
                "expected_impact": r.expected_impact,
                "target_entity_kind": r.target_entity_kind,
                "target_entity_id": r.target_entity_id,
                "created_at": iso_utc(r.created_at),
                "created_by_run_id": r.created_by_run_id,
            }
            for r in rows
        ],
    }
    note = _empty_sample_note(len(rows), status, sum(by_status.values()))
    if note:
        out["note"] = note
    return out


async def list_decisions(call: ToolCall):
    """List recent logged decisions (no_action / deferred / escalated).

    Call this BEFORE ``log_decision`` when handling a recurring signal:
    if the same situation was already deliberately skipped recently,
    reference that decision instead of logging an identical one.

    Optional filters: ``decision`` (the disposition), ``reason_code`` (the
    structured why), ``status`` (the v7.40.0 lifecycle — ``open`` |
    ``acknowledged`` | ``in_progress`` | ``done`` | ``dismissed`` | ``all``
    default), ``source_event_id``, ``q`` (substring on reason), ``limit``
    (default 20, max 50).
    """
    args = call.arguments or {}
    limit = min(int(args.get("limit") or 20), _LIST_CAP)
    status = (args.get("status") or "all").strip().lower()
    filters = []
    if args.get("decision"):
        filters.append(Decision.decision == str(args["decision"]).lower())
    if args.get("reason_code"):
        filters.append(Decision.reason_code == str(args["reason_code"]).lower())
    if status != "all":
        filters.append(Decision.status == status)
    if args.get("source_event_id"):
        filters.append(Decision.source_event_id == int(args["source_event_id"]))
    if args.get("q"):
        filters.append(Decision.reason_md.ilike(f"%{args['q']}%"))
    rows = await _list_rows(Decision, Decision.created_at, filters, limit)
    return {
        "count": len(rows),
        "decisions": [
            {
                "id": r.id,
                "decision": r.decision,
                "reason_code": r.reason_code,
                "confidence": r.confidence,
                "status": r.status,
                "reason": _clip(r.reason_md),
                "ref_kind": r.ref_kind,
                "ref_id": r.ref_id,
                "source_event_id": r.source_event_id,
                "created_at": iso_utc(r.created_at),
                "created_by_run_id": r.created_by_run_id,
            }
            for r in rows
        ],
    }


_ACTIVE_SCHEDULED = ("pending", "scheduled", "in_progress", "overdue")


async def list_scheduled_items(call: ToolCall):
    """See the shared calendar / agenda: scheduled tasks, delegated
    tasks (encargos), follow-ups and calendar entries.

    This is how you READ the time-anchored items behind
    ``schedule_task`` (your own future work), ``assign_task_to_user``
    (a person's task or reminder) and the entries the operator creates
    on the calendar. Use it to answer "what's on the calendar?" / "what
    do I have pending?", to avoid duplicating something already
    scheduled, or to find an ``item_id`` to close with
    ``complete_scheduled_item``.

    Optional filters: ``status`` (default ``active`` =
    pending/scheduled/in_progress/overdue; pass ``all`` or a concrete
    status like ``done``), ``assignee`` (``agent`` | ``user`` | ``all``
    default), ``kind`` (task|followup|reminder|event), ``when``
    (``upcoming`` | ``past`` | ``all`` default, by ``due_at``), ``q``
    (substring on title), ``limit`` (default 20, max 50). Items come
    back soonest-due first.

    The response includes a ``totals`` block (exact count by status over
    everything matching the content filters) so you can report numbers
    accurately even when the sample is capped.
    """
    from sqlalchemy import select
    args = call.arguments or {}
    status = (args.get("status") or "active").strip().lower()
    assignee = (args.get("assignee") or "all").strip().lower()
    when = (args.get("when") or "all").strip().lower()
    limit = min(int(args.get("limit") or 20), _LIST_CAP)
    now = datetime.utcnow()  # stored due_at is naive-UTC

    filters = []
    if status == "active":
        filters.append(ScheduledItem.status.in_(_ACTIVE_SCHEDULED))
    elif status != "all":
        filters.append(ScheduledItem.status == status)
    if assignee in ("agent", "user"):
        filters.append(ScheduledItem.assignee == assignee)
    if args.get("kind"):
        filters.append(ScheduledItem.kind == str(args["kind"]).lower())
    if args.get("q"):
        filters.append(ScheduledItem.title.ilike(f"%{args['q']}%"))
    if when == "upcoming":
        filters.append(ScheduledItem.due_at >= now)
    elif when == "past":
        filters.append(ScheduledItem.due_at < now)

    async with app_session() as session:
        stmt = select(ScheduledItem).order_by(ScheduledItem.due_at.asc()).limit(limit)
        for cond in filters:
            stmt = stmt.where(cond)
        rows = list((await session.execute(stmt)).scalars().all())

    # Exact totals by status (only the content filters apply, never the
    # status/when/limit) so "how many do I have?" is answered precisely.
    base_filters = []
    if assignee in ("agent", "user"):
        base_filters.append(ScheduledItem.assignee == assignee)
    if args.get("kind"):
        base_filters.append(ScheduledItem.kind == str(args["kind"]).lower())
    if args.get("q"):
        base_filters.append(ScheduledItem.title.ilike(f"%{args['q']}%"))
    by_status = await _count_by(ScheduledItem, ScheduledItem.status, base_filters)

    return {
        "count": len(rows),
        "totals": {"total": sum(by_status.values()), "by_status": by_status},
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "description": _clip(r.description),
                "kind": r.kind,
                "assignee": r.assignee,
                "origin": r.origin,
                "status": r.status,
                "priority": r.priority,
                "due_at": iso_utc(r.due_at),
                "assigned_to": r.assigned_to,
                "recurrence": r.recurrence,
                "result": _clip(r.result_md),
                "last_run_id": r.last_run_id,
                "created_at": iso_utc(r.created_at),
            }
            for r in rows
        ],
    }


async def list_communications(call: ToolCall):
    """List communications (messages drafted/sent to humans or systems).

    Use this to SEE what you have already written or delivered and avoid
    re-sending: ``draft`` is NOT sent, ``ready`` is queued for delivery,
    ``sent`` was delivered, ``failed`` errored. Answers "what have I sent
    / drafted?".

    Optional filters: ``status`` (default ``all``; or draft|ready|sent|
    failed), ``channel`` (email|slack|ticket|webhook|sms), ``recipient``,
    ``q`` (substring on subject/body), ``limit`` (default 20, max 50).

    The response includes a ``totals`` block with the exact count by
    status and by channel (over everything matching the content
    filters), so you can report numbers precisely even when the sample
    is capped.
    """
    from sqlalchemy import or_
    args = call.arguments or {}
    status = (args.get("status") or "all").strip().lower()
    limit = min(int(args.get("limit") or 20), _LIST_CAP)

    def _content_filters() -> list:
        f = []
        if args.get("channel"):
            f.append(Communication.channel == str(args["channel"]).lower())
        if args.get("recipient"):
            f.append(Communication.recipient == str(args["recipient"]))
        if args.get("q"):
            like = f"%{args['q']}%"
            f.append(or_(Communication.subject.ilike(like),
                         Communication.body_md.ilike(like)))
        return f

    filters = _content_filters()
    if status != "all":
        filters = [Communication.status == status, *filters]
    rows = await _list_rows(Communication, Communication.created_at, filters, limit)
    base = _content_filters()
    by_status = await _count_by(Communication, Communication.status, base)
    by_channel = await _count_by(Communication, Communication.channel, base)
    return {
        "count": len(rows),
        "totals": {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_channel": by_channel,
        },
        "communications": [
            {
                "id": r.id,
                "channel": r.channel,
                "recipient": r.recipient,
                "subject": r.subject,
                "body": _clip(r.body_md),
                "status": r.status,
                "sent_at": iso_utc(r.sent_at),
                "error": _clip(r.error) if r.error else None,
                "created_at": iso_utc(r.created_at),
                "created_by_run_id": r.created_by_run_id,
            }
            for r in rows
        ],
    }


async def list_scoring(call: ToolCall):
    """List stored scores (per-target metrics: churn_risk, health, …).

    Use this to SEE the scores you have already computed and their bands,
    or to find the latest value for a target before recomputing.

    Optional filters: ``metric``, ``target_kind``, ``target_id``,
    ``band`` (low|medium|high|critical), ``q`` (substring on
    explanation), ``limit`` (default 20, max 50). Newest first.

    The response includes a ``totals`` block (counts by metric and by
    band over everything matching the content filters).
    """
    args = call.arguments or {}
    limit = min(int(args.get("limit") or 20), _LIST_CAP)

    def _content_filters() -> list:
        f = []
        if args.get("metric"):
            f.append(Scoring.metric == str(args["metric"]))
        if args.get("target_kind"):
            f.append(Scoring.target_kind == str(args["target_kind"]))
        if args.get("target_id"):
            f.append(Scoring.target_id == str(args["target_id"]))
        return f

    filters = _content_filters()
    if args.get("band"):
        filters.append(Scoring.band == str(args["band"]).lower())
    if args.get("q"):
        filters.append(Scoring.explanation_md.ilike(f"%{args['q']}%"))
    rows = await _list_rows(Scoring, Scoring.created_at, filters, limit)
    base = _content_filters()
    by_metric = await _count_by(Scoring, Scoring.metric, base)
    by_band = await _count_by(Scoring, Scoring.band, base)
    return {
        "count": len(rows),
        "totals": {
            "total": sum(by_metric.values()),
            "by_metric": by_metric,
            "by_band": by_band,
        },
        "scoring": [
            {
                "id": r.id,
                "target_kind": r.target_kind,
                "target_id": r.target_id,
                "metric": r.metric,
                "window_days": r.window_days,
                "value": r.value,
                "band": r.band,
                "explanation": _clip(r.explanation_md),
                "created_at": iso_utc(r.created_at),
                "created_by_run_id": r.created_by_run_id,
            }
            for r in rows
        ],
    }


async def list_approvals(call: ToolCall):
    """List approval requests (actions parked for an operator's go/no-go).

    Use this to SEE what is awaiting sign-off and not re-request the same
    thing: ``pending`` awaits the operator, ``approved`` was granted,
    ``rejected`` was refused, ``executed`` already ran. Answers "what is
    pending my operator's approval?".

    Optional filters: ``status`` (default ``pending``; or approved|
    rejected|executed|all), ``action_type``, ``risk`` (low|medium|high),
    ``requested_for``, ``q`` (substring on title), ``limit`` (default 20,
    max 50). The response includes a ``totals`` block (counts by status
    over everything matching the content filters).
    """
    args = call.arguments or {}
    status = (args.get("status") or "pending").strip().lower()
    limit = min(int(args.get("limit") or 20), _LIST_CAP)

    def _content_filters() -> list:
        f = []
        if args.get("action_type"):
            f.append(Approval.action_type == str(args["action_type"]))
        if args.get("risk"):
            f.append(Approval.risk == str(args["risk"]).lower())
        if args.get("requested_for"):
            f.append(Approval.requested_for == str(args["requested_for"]).strip().lower())
        if args.get("q"):
            f.append(Approval.title.ilike(f"%{args['q']}%"))
        return f

    filters = _content_filters()
    if status != "all":
        filters = [Approval.status == status, *filters]
    rows = await _list_rows(Approval, Approval.created_at, filters, limit)
    by_status = await _count_by(Approval, Approval.status, _content_filters())
    return {
        "count": len(rows),
        "totals": {"total": sum(by_status.values()), "by_status": by_status},
        "approvals": [
            {
                "id": r.id,
                "title": r.title,
                "summary": _clip(r.summary_md),
                "action_type": r.action_type,
                "risk": r.risk,
                "status": r.status,
                "requested_for": r.requested_for,
                "decided_by": r.decided_by,
                "decided_at": iso_utc(r.decided_at),
                "feedback": _clip(r.feedback) if r.feedback else None,
                "created_at": iso_utc(r.created_at),
                "created_by_run_id": r.created_by_run_id,
            }
            for r in rows
        ],
    }


async def describe_entities(call: ToolCall):
    """List the local business entities THIS app defines — so you are
    fully aware of the data you own, INCLUDING any custom entities
    specific to this agent.

    The local entities are NOT fixed: every agent can declare its own in
    its manifest (`domain_entities`), beyond the canonical alerts /
    recommendations / communications / scoring / decisions /
    scheduled_items / approvals. This tool reads THIS app's manifest and
    tells you exactly which entities exist and how to touch each one.

    Returns:
    - ``catalogue``: an always-correct markdown block (read-only project
      data + your internal entities), each line naming the exact tool to
      read/write it. Rendered straight from the manifest, so it never
      drifts.
    - ``entities``: the structured list of your LOCAL entities — each
      ``{slug, table, label, read_tool, write_tool}``. Canonical tables
      map to a named tool; custom ones map to `local.read` / `local.write`.
    - ``project_tables``: the read-only project tables (queried via
      `wai-data`), for completeness.

    Call this when asked what data / entities you manage, or before
    working with a custom table you are unsure about.
    """
    try:
        from app.routes.manifest_routes import _load_wai_app
        cfg = _load_wai_app() or {}
    except Exception:  # pragma: no cover — manifest missing/unreadable
        cfg = {}
    from app.app_context import _LOCAL_TOOLS, build_data_catalogue
    try:
        lang = await resolve_agent_language()
    except Exception:  # pragma: no cover — locale resolution best-effort
        lang = "es"
    agent_app = cfg.get("agent_app", cfg) or {}
    ents = agent_app.get("domain_entities", []) or []

    def _lbl(e: dict) -> str:
        labels = e.get("labels") or {}
        return labels.get(lang) or e.get("label") or e.get("slug") or e.get("table") or "?"

    local_rows = []
    project_rows = []
    for e in ents:
        if not isinstance(e, dict):
            continue
        if e.get("source") == "local":
            tbl = e.get("table") or ""
            write_tool, read_tool = _LOCAL_TOOLS.get(
                tbl, ("local.write", "local.read"),
            )
            local_rows.append({
                "slug": e.get("slug"),
                "table": tbl,
                "label": _lbl(e),
                "read_tool": read_tool,
                "write_tool": write_tool,
            })
        else:
            project_rows.append({
                "slug": e.get("slug"),
                "table": e.get("table"),
                "label": _lbl(e),
            })

    try:
        catalogue = build_data_catalogue(cfg, lang)
    except Exception:  # pragma: no cover — never fail the tool on render
        catalogue = ""
    return {
        "catalogue": catalogue,
        "entities": local_rows,
        "project_tables": project_rows,
    }


# ── Per-user memory (semantic / episodic / artifacts) ───────────────
#
# The agent app is multi-user; these tools give it a persistent,
# per-user memory so it can personalise across chat + email and over
# time. ``user_email`` is ALWAYS required — it is the person's address
# (the system prompt tells the agent which one to use). Writes are
# idempotency-keyed like every other tool; ``remember_fact`` supports
# contradiction management via ``supersedes``.

async def remember_fact(call: ToolCall):
    """Store a durable FACT / PREFERENCE / PROCEDURE about a user.

    Use this when you learn something lasting and useful about the person
    (their role, their goals, how they like answers, a recurring
    workflow). Do NOT store transient chit-chat or one-off values.

    Required: ``user_email``, ``content``.
    Optional: ``kind`` (``fact`` default | ``preference`` | ``procedure``),
    ``tags`` (comma-separated), ``pinned`` (bool — protect from automatic
    replacement and always surface first), ``confidence`` (0..1, for
    procedures), ``supersedes`` (id of a previous fact this one
    corrects — that row is marked superseded and drops out of the
    working memory).

    Optional: ``importance`` (0..1), ``source`` (where the fact comes from) and ``trust`` (how much you trust it).

    Returns ``{"ok": true, "id": <created id>, "idempotency_key": …}``. If a constraint prevents it, ``{"ok": false, "error": "constraint", …}`` — read it: **do not assume it was saved**. If the signal is silenced ``{"suppressed": true}`` arrives and you must NOT retry it.
    If an equivalent fact already existed it returns ``{"ok": true, "id": …, "reinforced": true}``: it is reinforced, not duplicated.
    """
    args = call.arguments or {}
    user_email = await _bound_memory_user(call, args)
    denied = await _memory_bind_result(call, args, user_email)
    if denied:
        return denied
    content = _require(args, "content")
    kind = _enum(args, "kind", MEMORY_KINDS, "fact")
    pinned = bool(args.get("pinned") or False)
    tags = args.get("tags")
    confidence = args.get("confidence")
    # Self-containment: store any linked workspace artifacts in provenance
    # so the fact carries its evidence (e.g. exports/factura_687.pdf).
    artifacts = _parse_artifacts(args, owner=user_email)
    provenance = artifacts if artifacts else f"run={call.run_id}"

    # v7.29.0 — trust (clamped to the run-origin CEILING, model can't raise it),
    # importance, content_hash + embedding.
    from app.memory_utils import enrich_memory_write, reinforce_duplicate_fact
    enr = await enrich_memory_write(
        call.run_id, str(content), kind,
        requested_trust=args.get("trust"),
        requested_importance=args.get("importance"),
    )
    # Cross-run dedup: if the same live fact exists, REINFORCE it instead of
    # inserting a duplicate (two parallel pool workers, retries).
    dup_id = await reinforce_duplicate_fact(user_email, enr["content_hash"] or "", enr["trust"])
    if dup_id and not args.get("supersedes"):
        return {"ok": True, "id": int(dup_id), "reinforced": True}

    result = await _persist(
        call,
        lambda key: UserMemory(
            idempotency_key=key,
            created_by_run_id=call.run_id,
            user_email=user_email,
            kind=kind,
            content=str(content),
            tags=str(tags) if tags else None,
            confidence=float(confidence) if confidence is not None else None,
            pinned=pinned,
            source=str(args.get("source") or "agent"),
            provenance=provenance,
            trust=enr["trust"],
            importance=enr["importance"],
            content_hash=enr["content_hash"],
            embedding=enr["embedding"],
            embed_model=enr["embed_model"],
        ),
    )

    # Contradiction management: mark the superseded fact so it leaves the
    # working block (history kept). GOVERNED BY TRUST (v7.29.0): a lower-trust
    # fact can NOT supersede a higher-trust / pinned one — this blocks the
    # inbound-poisoning "forget that the limit is X" delete.
    supersedes = args.get("supersedes")
    inner = (result or {}).get("result") or {}
    new_id = inner.get("id")
    if supersedes and inner.get("ok") and new_id:
        from sqlalchemy import update as _update
        from app import memory_trust as _t
        try:
            async with app_write() as s:
                old = (await s.execute(
                    select(UserMemory.trust, UserMemory.pinned).where(
                        UserMemory.id == int(supersedes),
                        UserMemory.user_email == user_email,
                    )
                )).first()
                if old is not None and _t.can_supersede(
                    enr["trust"], old[0], old_pinned=bool(old[1]),
                ):
                    await s.execute(
                        _update(UserMemory)
                        .where(
                            UserMemory.id == int(supersedes),
                            UserMemory.user_email == user_email,
                        )
                        .values(superseded_by=int(new_id))
                    )
                    await s.commit()
                else:
                    logger.warning(
                        "supersede blocked by trust: new=%s old_id=%s",
                        enr["trust"], supersedes,
                    )
        except Exception:  # pragma: no cover — supersede is best-effort
            pass

    # v7.29.0 — activity-driven maintenance: enqueue a (deduped, low-priority)
    # maintenance run from the WRITE path, not only at session prime — so an
    # active user's memory actually gets consolidated. Best-effort; the 5-min
    # bucket fingerprint collapses bursts.
    try:
        from app.memory_prompt import maybe_enqueue_autocompact
        await maybe_enqueue_autocompact(user_email)
    except Exception:  # pragma: no cover
        pass
    return result


async def record_episode(call: ToolCall):
    """Record a one-line EPISODE summarising this interaction.

    Call this at the end of handling a chat turn or an email when
    something noteworthy happened, so future conversations have continuity
    ("last week we discussed the Q3 stock report"). Keep it to a single
    sentence. The episodic table is capped per user — old episodes are
    pruned automatically; durable knowledge belongs in ``remember_fact``.

    Required: ``user_email``, ``summary``.
    Optional: ``source`` (``chat`` | ``email``), ``source_ref``,
    ``outcome``.


    Returns ``{"ok": true, "id": <created id>, "idempotency_key": …}``. If a constraint prevents it, ``{"ok": false, "error": "constraint", …}`` — read it: **do not assume it was saved**. If the signal is silenced ``{"suppressed": true}`` arrives and you must NOT retry it.
    """
    args = call.arguments or {}
    user_email = await _bound_memory_user(call, args)
    denied = await _memory_bind_result(call, args, user_email)
    if denied:
        return denied
    summary = _require(args, "summary")
    source = (args.get("source") or "chat").strip().lower()
    # Self-containment: prefer linked artifacts JSON in source_ref; fall
    # back to a free-form source_ref string if no artifacts were given.
    artifacts = _parse_artifacts(args, owner=user_email)
    source_ref = artifacts or (
        str(args["source_ref"]) if args.get("source_ref") else None
    )

    result = await _persist(
        call,
        lambda key: UserEpisode(
            idempotency_key=key,
            created_by_run_id=call.run_id,
            user_email=user_email,
            summary=str(summary),
            source=source if source in ("chat", "email") else "chat",
            source_ref=source_ref,
            outcome=str(args["outcome"]) if args.get("outcome") else None,
        ),
    )
    # Episodic cap (forgetting policy): keep only the most recent N.
    try:
        from app.config import AGENT_MEMORY_EPISODE_KEEP
        await _prune_episodes(user_email, AGENT_MEMORY_EPISODE_KEEP)
    except Exception:  # pragma: no cover — never fail the write on prune
        pass
    return result


async def _prune_episodes(user_email: str, keep: int) -> None:
    from sqlalchemy import select as _select
    from sqlalchemy import delete as _delete
    if keep <= 0:
        return
    async with app_write() as s:
        ids = list((await s.execute(
            _select(UserEpisode.id)
            .where(UserEpisode.user_email == user_email)
            .order_by(UserEpisode.created_at.desc())
            .offset(keep)
        )).scalars().all())
        if ids:
            await s.execute(_delete(UserEpisode).where(UserEpisode.id.in_(ids)))
            await s.commit()


async def index_file(call: ToolCall):
    """Index a FILE in the user's artifacts memory.

    Use this for documents the user sent or that you produced for them
    and that should persist beyond this message (so they appear in the
    user's memory and in the Operations tab). The bytes must already live
    in the workspace; this only records a pointer.

    ``description`` is the file's SEMANTICS — say WHAT it contains (key
    figures, entities, period) and WHEN to use it; that text is what memory
    search matches later. v7.96.0: if the file is already indexed (the
    system auto-indexes deliverables with a heuristic description), the call
    UPSERTS — your better description/mime/size replace the heuristic ones —
    and the description is embedded for dense recall.

    Required: ``user_email``, ``filename``, ``workspace_path``
    (relative to ``/workspace``).
    Optional: ``mime``, ``size``, ``description``, ``source``
    (``email`` | ``chat`` | ``agent``).


    Returns ``{"ok": true, "id": <created id>, "idempotency_key": …}``. If a constraint prevents it, ``{"ok": false, "error": "constraint", …}`` — read it: **do not assume it was saved**. If the signal is silenced ``{"suppressed": true}`` arrives and you must NOT retry it.
    If the file was already indexed it returns ``{"ok": true, "id": …, "updated": true}`` instead of creating another one.
    """
    args = call.arguments or {}
    user_email = await _bound_memory_user(call, args)
    denied = await _memory_bind_result(call, args, user_email)
    if denied:
        return denied
    filename = _require(args, "filename")
    workspace_path = _require(args, "workspace_path")
    from app.memory_utils import reject_workspace_path
    if reject_workspace_path(str(workspace_path)):
        return {"ok": False, "error": "invalid workspace_path"}
    size = args.get("size")
    desc = str(args["description"]) if args.get("description") else None
    blob = emodel = None
    if desc:
        try:
            from app import memory_embeddings as _e
            blob = _e.pack(_e.embed(desc))
            emodel = _e.model_id()
        except Exception:  # pragma: no cover — embedding is best-effort
            blob = emodel = None

    # Already indexed (auto-index or an earlier call)? ENRICH instead of
    # colliding with the (user_email, workspace_path) uniqueness: the agent's
    # semantics beat the heuristic floor.
    from sqlalchemy import select as _select, update as _update
    wp = str(workspace_path).strip().lstrip("/")
    async with app_write() as session:
        existing_id = (await session.execute(_select(UserFile.id).where(
            UserFile.user_email == user_email,
            UserFile.workspace_path.in_([wp, f"/{wp}", str(workspace_path)]),
        ))).scalars().first()
        if existing_id is not None:
            values: dict = {}
            if desc:
                values.update(description=desc[:2000], embedding=blob, embed_model=emodel)
            if args.get("mime"):
                values["mime"] = str(args["mime"])
            if size is not None:
                values["size"] = int(size)
            if values:
                await session.execute(_update(UserFile).where(UserFile.id == existing_id).values(**values))
                await session.commit()
            return {"ok": True, "id": existing_id, "updated": True}

    return await _persist(
        call,
        lambda key: UserFile(
            idempotency_key=key,
            created_by_run_id=call.run_id,
            user_email=user_email,
            filename=str(filename),
            workspace_path=str(workspace_path),
            mime=str(args["mime"]) if args.get("mime") else None,
            size=int(size) if size is not None else None,
            source=str(args.get("source") or "agent"),
            description=desc,
            embedding=blob,
            embed_model=emodel,
        ),
    )


async def list_memory(call: ToolCall):
    """List what is remembered about a user (live facts + recent episodes
    + indexed files). Call this at the START of handling a known user to
    refresh context beyond the injected block.

    Required: ``user_email``.
    Optional: ``kind`` (filter facts), ``limit`` (default 30, max 50).
    """
    from sqlalchemy import select
    args = call.arguments or {}
    user_email = await _bound_memory_user(call, args)
    denied = await _memory_bind_result(call, args, user_email)
    if denied:
        return denied
    limit = min(int(args.get("limit") or 30), _LIST_CAP)
    kind = (args.get("kind") or "").strip().lower()
    async with app_session() as session:
        fstmt = (
            select(UserMemory)
            .where(
                UserMemory.user_email == user_email,
                UserMemory.superseded_by.is_(None),
            )
            .order_by(UserMemory.pinned.desc(), UserMemory.updated_at.desc())
            .limit(limit)
        )
        if kind in MEMORY_KINDS:
            fstmt = fstmt.where(UserMemory.kind == kind)
        facts = list((await session.execute(fstmt)).scalars().all())
        episodes = list((await session.execute(
            select(UserEpisode)
            .where(UserEpisode.user_email == user_email)
            .order_by(UserEpisode.created_at.desc())
            .limit(limit)
        )).scalars().all())
        files = list((await session.execute(
            select(UserFile)
            .where(UserFile.user_email == user_email)
            .order_by(UserFile.created_at.desc())
            .limit(limit)
        )).scalars().all())
    return {
        "user_email": user_email,
        "facts": [
            {"id": f.id, "kind": f.kind, "content": _clip(f.content),
             "pinned": bool(f.pinned), "tags": f.tags,
             "artifacts": _artifacts_from(f.provenance)}
            for f in facts
        ],
        "episodes": [
            {"id": e.id, "summary": _clip(e.summary),
             "artifacts": _artifacts_from(e.source_ref),
             "created_at": iso_utc(e.created_at)}
            for e in episodes
        ],
        "files": [
            {"id": x.id, "filename": x.filename,
             "workspace_path": x.workspace_path, "mime": x.mime}
            for x in files
        ],
    }


async def search_memory(call: ToolCall):
    """Search a user's memory across all layers (facts, episodes, files) by
    RELEVANCE — a BM25 keyword index (FTS5, accent-insensitive) with an
    embedding/substring fallback — not just substring. Use it when you need a
    specific detail you don't see in the injected block — e.g. "how they prefer
    emails" finds "prefers short messages".

    Required: ``user_email``, ``query``.
    Optional: ``limit`` (default 20, max 50).
    """
    from sqlalchemy import select, or_
    args = call.arguments or {}
    user_email = await _bound_memory_user(call, args)
    denied = await _memory_bind_result(call, args, user_email)
    if denied:
        return denied
    query = str(_require(args, "query"))
    limit = min(int(args.get("limit") or 20), _LIST_CAP)
    like = f"%{query}%"
    # v7.29.0 — dense retrieval: fetch a candidate set (substring OR live) and
    # re-rank by embedding cosine + trust + recency. Falls back to substring
    # order when dense retrieval is disabled.
    from app.config import AGENT_MEMORY_DENSE_RETRIEVAL
    from app import memory_embeddings as _e, memory_trust as _t
    qvec = _e.embed(query) if AGENT_MEMORY_DENSE_RETRIEVAL else None

    def _rerank(rows):
        if qvec is None:
            return rows[:limit]
        scored = []
        for r in rows:
            rel = _e.cosine(qvec, _e.unpack(getattr(r, "embedding", None)))
            tw = _t.weight(getattr(r, "trust", None)) if hasattr(r, "trust") else 0.6
            scored.append((0.75 * rel + 0.25 * tw, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:limit]]

    async with app_session() as session:
        # v7.56.0 — FTS5 keyword relevance per layer (BM25). When it has hits we
        # fetch exactly those rows ranked by relevance (× trust for facts); else
        # we fall back to the embedding re-rank / substring path below.
        from app import memory_fts
        fts_f = await memory_fts.search(session, "user_memory", query, user_email, 200)
        fts_e = await memory_fts.search(session, "user_episodes", query, user_email, 200)
        fts_x = await memory_fts.search(session, "user_files", query, user_email, 200)

        if fts_f:
            frows = list((await session.execute(
                select(UserMemory).where(
                    UserMemory.user_email == user_email,
                    UserMemory.superseded_by.is_(None),
                    UserMemory.id.in_(list(fts_f.keys())),
                )
            )).scalars().all())
            frows.sort(key=lambda r: 0.75 * fts_f.get(r.id, 0.0)
                       + 0.25 * _t.weight(getattr(r, "trust", None)), reverse=True)
            facts = frows[:limit]
        else:
            fcand = list((await session.execute(
                select(UserMemory)
                .where(
                    UserMemory.user_email == user_email,
                    UserMemory.superseded_by.is_(None),
                )
                .order_by(UserMemory.pinned.desc(), UserMemory.updated_at.desc())
                .limit(200 if qvec is not None else limit)
                if qvec is not None else
                select(UserMemory)
                .where(
                    UserMemory.user_email == user_email,
                    UserMemory.superseded_by.is_(None),
                    UserMemory.content.ilike(like),
                )
                .order_by(UserMemory.pinned.desc(), UserMemory.updated_at.desc())
                .limit(limit)
            )).scalars().all())
            facts = _rerank(fcand)

        if fts_e:
            erows = list((await session.execute(
                select(UserEpisode).where(
                    UserEpisode.user_email == user_email,
                    UserEpisode.id.in_(list(fts_e.keys())),
                )
            )).scalars().all())
            erows.sort(key=lambda r: fts_e.get(r.id, 0.0), reverse=True)
            episodes = erows[:limit]
        else:
            ecand = list((await session.execute(
                select(UserEpisode)
                .where(UserEpisode.user_email == user_email)
                .order_by(UserEpisode.created_at.desc())
                .limit(200 if qvec is not None else limit)
                if qvec is not None else
                select(UserEpisode)
                .where(
                    UserEpisode.user_email == user_email,
                    UserEpisode.summary.ilike(like),
                )
                .order_by(UserEpisode.created_at.desc())
                .limit(limit)
            )).scalars().all())
            episodes = _rerank(ecand)

        if fts_x:
            xrows = list((await session.execute(
                select(UserFile).where(
                    UserFile.user_email == user_email,
                    UserFile.id.in_(list(fts_x.keys())),
                )
            )).scalars().all())
            xrows.sort(key=lambda r: fts_x.get(r.id, 0.0), reverse=True)
            files = xrows[:limit]
        else:
            files = list((await session.execute(
                select(UserFile)
                .where(
                    UserFile.user_email == user_email,
                    or_(UserFile.filename.ilike(like), UserFile.description.ilike(like)),
                )
                .order_by(UserFile.created_at.desc())
                .limit(limit)
            )).scalars().all())
    return {
        "user_email": user_email,
        "query": query,
        "facts": [{"id": f.id, "kind": f.kind, "content": _clip(f.content),
                   "artifacts": _artifacts_from(f.provenance)} for f in facts],
        "episodes": [{"id": e.id, "summary": _clip(e.summary),
                      "artifacts": _artifacts_from(e.source_ref)} for e in episodes],
        "files": [{"id": x.id, "filename": x.filename,
                   "workspace_path": x.workspace_path} for x in files],
    }


async def forget_fact(call: ToolCall):
    """Permanently delete a fact/preference/procedure from a user's memory.

    Use this to remove something wrong or obsolete (prefer ``supersedes``
    on ``remember_fact`` when you are *correcting* it instead). Either pass
    ``fact_id`` (precise) or ``content`` (deletes facts whose content
    matches exactly, case-insensitive) for the given ``user_email``.

    Required: ``user_email`` and one of ``fact_id`` | ``content``.


    Returns ``{"ok": true, "forgotten": <how many>}``.
    """
    from sqlalchemy import delete as _delete, func as _func, select as _select
    args = call.arguments or {}
    user_email = await _bound_memory_user(call, args)
    denied = await _memory_bind_result(call, args, user_email)
    if denied:
        return denied
    fact_id = args.get("fact_id")
    content = (args.get("content") or "").strip()
    if not fact_id and not content:
        raise HTTPException(
            status_code=400, detail="pass fact_id or content to forget",
        )
    # v7.29.0 — forgetting is soft + trust-governed: an inbound/untrusted run
    # can NOT delete a verified/pinned memory (blocks the poisoning "forget
    # that X" delete). We SUPERSEDE (tombstone) rather than hard-delete, so
    # every removal stays auditable and reversible.
    from datetime import datetime, timezone
    from sqlalchemy import update as _update
    from app import memory_trust as _t
    run_ceiling = await _t.trust_ceiling_for_run(call.run_id)
    async with app_write() as s:
        q = _select(UserMemory.id, UserMemory.trust, UserMemory.pinned).where(
            UserMemory.user_email == user_email,
            UserMemory.superseded_by.is_(None),
        )
        if fact_id:
            q = q.where(UserMemory.id == int(fact_id))
        else:
            q = q.where(_func.lower(UserMemory.content) == content.lower())
        rows = list((await s.execute(q)).all())
        removable = [
            int(r[0]) for r in rows
            if _t.can_supersede(run_ceiling, r[1], old_pinned=bool(r[2]))
        ]
        blocked = len(rows) - len(removable)
        if removable:
            # Tombstone: point at self so it drops out of the live block but
            # the row (and its history) survives for audit/rollback.
            for rid in removable:
                await s.execute(
                    _update(UserMemory).where(UserMemory.id == rid).values(
                        superseded_by=rid,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
            await s.commit()
    out = {"ok": True, "deleted": len(removable), "user_email": user_email}
    if blocked:
        out["blocked_by_trust"] = blocked
    return out


def _norm_text(s: str | None) -> str:
    """Normalise fact content for dedupe: lowercase, collapse whitespace,
    drop trailing punctuation."""
    import re as _re
    t = (s or "").strip().lower()
    t = _re.sub(r"\s+", " ", t)
    return t.rstrip(".;,: ")


async def compact_memory(call: ToolCall):
    """Optimise a user's memory deterministically.

    Performs safe maintenance and returns a report so you can then merge
    semantically (e.g. `remember_fact(..., supersedes=...)`):
    - dedupes identical live facts (same kind + content), keeping the
      pinned/newest and marking the rest superseded;
    - dedupes indexed files by workspace_path (keeps newest);
    - prunes episodes beyond the retention cap.

    Required: ``user_email``.
    """
    args = call.arguments or {}
    user_email = await _bound_memory_user(call, args)
    denied = await _memory_bind_result(call, args, user_email)
    if denied:
        return denied
    return await _compact_user_memory(user_email)


async def _compact_user_memory(user_email: str) -> dict:
    """Deterministic memory maintenance for ``user_email`` (no model call).
    Shared by the ``compact_memory`` MCP tool and the operator
    ``POST /api/agent/memory/compact`` route. ``user_email`` must already be
    a validated human address."""
    from sqlalchemy import delete as _delete, select as _select, update as _update

    merged_facts = 0
    capped_facts = 0
    deduped_files = 0
    pruned_episodes = 0
    ttl_pruned_episodes = 0
    async with app_write() as s:
        # 1) Dedupe live facts by (kind, normalized content).
        facts = list((await s.execute(
            _select(UserMemory)
            .where(
                UserMemory.user_email == user_email,
                UserMemory.superseded_by.is_(None),
            )
            .order_by(UserMemory.pinned.desc(), UserMemory.updated_at.desc())
        )).scalars().all())
        seen: dict[tuple[str, str], int] = {}
        superseded_ids: set[int] = set()
        for f in facts:
            key = (f.kind or "fact", _norm_text(f.content))
            if not key[1]:
                continue
            if key in seen:
                # keep the first (pinned/newest), supersede this duplicate
                await s.execute(
                    _update(UserMemory)
                    .where(UserMemory.id == f.id)
                    .values(superseded_by=seen[key])
                )
                superseded_ids.add(f.id)
                merged_facts += 1
            else:
                seen[key] = f.id

        # 1a) v7.29.0 — NEAR-duplicate collapse by embedding (Tier A, no LLM):
        # merge facts that MEAN the same thing but are worded differently,
        # keeping the higher-trust / pinned one. Trust-governed (a lower-trust
        # fact never eats a higher-trust one). O(n²) over the live set, which is
        # capped, so it's cheap.
        try:
            from app import memory_embeddings as _e, memory_trust as _t
            kept = [f for f in facts if f.id not in superseded_ids]
            by_id = {f.id: f for f in kept}
            vecs = {f.id: _e.unpack(getattr(f, "embedding", None)) for f in kept}
            for i, a in enumerate(kept):
                if a.id in superseded_ids or vecs.get(a.id) is None:
                    continue
                for b in kept[i + 1:]:
                    if b.id in superseded_ids or vecs.get(b.id) is None:
                        continue
                    if (a.kind or "fact") != (b.kind or "fact"):
                        continue
                    if _e.cosine(vecs[a.id], vecs[b.id]) < 0.92:
                        continue
                    # Decide survivor by trust, then pinned, then recency.
                    keep, drop = a, b
                    if _t.rank(getattr(b, "trust", None)) > _t.rank(getattr(a, "trust", None)) \
                       or (bool(b.pinned) and not bool(a.pinned)):
                        keep, drop = b, a
                    if not _t.can_supersede(
                        getattr(keep, "trust", None), getattr(drop, "trust", None),
                        old_pinned=bool(drop.pinned),
                    ):
                        continue
                    await s.execute(
                        _update(UserMemory).where(UserMemory.id == drop.id)
                        .values(superseded_by=keep.id)
                    )
                    superseded_ids.add(drop.id)
                    merged_facts += 1
        except Exception:  # pragma: no cover — near-dup is best-effort
            logger.debug("near-dup collapse failed for %s", user_email, exc_info=True)

        # 1b) Growth cap: keep at most FACTS_KEEP live, non-pinned facts.
        #     Auto-supersede the oldest beyond the cap (history kept; the
        #     pinned facts are never capped). Reversible via "show obsolete".
        try:
            from app.config import AGENT_MEMORY_FACTS_KEEP
            keep_facts = int(AGENT_MEMORY_FACTS_KEEP)
        except Exception:
            keep_facts = 0
        if keep_facts > 0:
            remaining = [f for f in facts if f.id not in superseded_ids]
            rollup_id = remaining[0].id if remaining else None
            live_unpinned = [f for f in remaining if not f.pinned]
            if rollup_id is not None and len(live_unpinned) > keep_facts:
                # v6.44.0: rank by VALUE, not just age — keep the facts the
                # agent actually leans on. A fact used often / recently
                # survives; a never-touched stale one is the first to roll up.
                # Tie-break on recency (updated_at) so behaviour matches the
                # old "newest wins" for the common all-zero-access case.
                def _value(f) -> tuple:
                    used = int(getattr(f, "access_count", 0) or 0)
                    seen_at = getattr(f, "last_accessed_at", None) or f.updated_at
                    return (used, seen_at or f.created_at)
                ranked = sorted(live_unpinned, key=_value, reverse=True)
                # keep the top keep_facts by value; roll up the rest.
                for f in ranked[keep_facts:]:
                    if f.id == rollup_id:
                        continue
                    await s.execute(
                        _update(UserMemory)
                        .where(UserMemory.id == f.id)
                        .values(superseded_by=rollup_id)
                    )
                    capped_facts += 1

        # 2) Dedupe files by workspace_path (keep newest row).
        files = list((await s.execute(
            _select(UserFile)
            .where(UserFile.user_email == user_email)
            .order_by(UserFile.created_at.desc())
        )).scalars().all())
        seen_paths: set[str] = set()
        dup_ids: list[int] = []
        for x in files:
            wp = (x.workspace_path or "").strip()
            if wp in seen_paths:
                dup_ids.append(x.id)
            else:
                seen_paths.add(wp)
        if dup_ids:
            await s.execute(_delete(UserFile).where(UserFile.id.in_(dup_ids)))
            deduped_files = len(dup_ids)

        await s.commit()

    # 3) Prune episodes beyond the retention cap.
    try:
        from app.config import AGENT_MEMORY_EPISODE_KEEP
        async with app_write() as s:
            ids = list((await s.execute(
                _select(UserEpisode.id)
                .where(UserEpisode.user_email == user_email)
                .order_by(UserEpisode.created_at.desc())
                .offset(max(int(AGENT_MEMORY_EPISODE_KEEP), 0))
            )).scalars().all())
            if ids:
                await s.execute(_delete(UserEpisode).where(UserEpisode.id.in_(ids)))
                await s.commit()
                pruned_episodes = len(ids)
    except Exception:  # pragma: no cover — prune is best-effort
        pass

    # 4) Optional time-based episode TTL (on top of the count cap).
    try:
        from app.config import AGENT_MEMORY_EPISODE_TTL_DAYS
        ttl_days = int(AGENT_MEMORY_EPISODE_TTL_DAYS)
        if ttl_days > 0:
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            cutoff = _dt.now(_tz.utc) - _td(days=ttl_days)
            async with app_write() as s:
                old_ids = list((await s.execute(
                    _select(UserEpisode.id)
                    .where(
                        UserEpisode.user_email == user_email,
                        UserEpisode.created_at < cutoff,
                    )
                )).scalars().all())
                if old_ids:
                    await s.execute(
                        _delete(UserEpisode).where(UserEpisode.id.in_(old_ids))
                    )
                    await s.commit()
                    ttl_pruned_episodes = len(old_ids)
    except Exception:  # pragma: no cover — TTL is best-effort
        pass

    return {
        "ok": True,
        "user_email": user_email,
        "merged_facts": merged_facts,
        "capped_facts": capped_facts,
        "deduped_files": deduped_files,
        "pruned_episodes": pruned_episodes,
        "ttl_pruned_episodes": ttl_pruned_episodes,
    }


# ── SELF-memory (what the employee learned about its ENVIRONMENT) ────
#
# v7.140.0 — the second memory axis. These tools take NO ``user_email``: the
# identity here is the ``subject`` (``job:indexing``, ``deployment:x``), not a
# person, which is exactly why they work on an autonomous run where the per-user
# memory tools legitimately refuse to write.
#
# The governance line runs right through this section: ``remember_learning``
# stores knowledge (soft — informs judgment) while ``propose_silence`` ASKS for
# behaviour (hard — and an admin must accept it). The employee can never cross
# from one to the other on its own.

async def remember_learning(call: ToolCall):
    """Store something you learned about THIS ENVIRONMENT (not about a person).

    Use it when you find out how the system you watch actually behaves: how long
    something normally takes here, what is routine noise, which pieces exist, how
    a check is best done. This is what stops you from re-raising as an incident
    something that is normal in this environment.

    Required: ``subject`` (canonical identity of what you observed —
    ``job:indexing``, ``deployment:wai-engine-api``, ``tenant:pabisan``) and
    ``content``: ONE atomic, TIMELESS sentence (~240 chars max). No dates, no
    today's figures — "the sweep normally takes 30-45 min", never "on 28 July
    2026 it took 41 min". The when/how-many-times/which-run are recorded for
    you; putting them in the text means it can never be reinforced tomorrow.
    Dated or oversized content is REJECTED with the reason so you can rewrite
    it (a one-off event belongs in ``episode``, not here).
    Optional: ``kind`` (``observation`` default | ``baseline`` | ``lesson`` |
    ``procedure`` | ``entity``), ``valid_until`` (days, or an ISO date — put one
    on a baseline: the environment changes), ``confidence`` (0..1),
    ``importance`` (0..1), ``tags``.

    Re-asserting the SAME content on the SAME subject REINFORCES it (evidence
    counter +1), it does not duplicate — that is how an anecdote becomes a
    baseline. A MORE COMPLETE version of something already known EXTENDS it in
    place instead of opening a second entry. Storing a learning does NOT change
    what you do: to stop warning about something, use ``propose_silence``.

    ``supersedes``: list of learning ids this one REPLACES — the distilled
    baseline retires the raw observations it summarises. They are not deleted:
    they stay as history and stop being injected. This is what actually cleans
    the memory up, so a maintenance run that distils MUST pass it.
    """
    args = call.arguments or {}
    from app.config import AGENT_SELF_MEMORY_ENABLED
    if not AGENT_SELF_MEMORY_ENABLED:
        return {"ok": False, "skipped": True,
                "reason": "self-memory is disabled for this employee"}
    from app.agent_memory_utils import remember
    res = await remember(
        content=str(_require(args, "content")),
        subject=str(_require(args, "subject")),
        kind=_enum(args, "kind", AGENT_MEMORY_KINDS, "observation"),
        run_id=call.run_id, source="agent",
        importance=args.get("importance"),
        confidence=args.get("confidence"),
        valid_until=args.get("valid_until"),
        tags=(str(args["tags"]) if args.get("tags") else None),
        supersedes=args.get("supersedes"),
    )
    if not res.get("id"):
        if res.get("rejected"):
            # Rejection with an actionable reason: the agent can rewrite it in the same turn,
            # which is exactly what did not happen when it was stored badly.
            return {"ok": False, "rejected": True, "error": res["rejected"],
                    "note": "It has not been saved. Rewrite it and call me again."}
        return {"ok": False, "error": res.get("error") or "write failed"}
    nota = (res.get("note")
            or ("Reinforced: you already knew it and now you have one more observation "
                "(and its validity is renewed)."
                if res.get("reinforced") else
                "Saved as UNVALIDATED: it informs you, but it does not change your "
                "behaviour until a manager validates it."))
    out = {"ok": True, "id": res["id"], "reinforced": bool(res.get("reinforced")),
           "matched_by": res.get("matched_by"), "note": nota}
    if res.get("superseded"):
        out["superseded"] = res["superseded"]
        out["note"] = f"{nota} Retired {res['superseded']} entry(ies) it summarises."
    return out


async def search_learnings(call: ToolCall):
    """Search what you have learned about this environment, by relevance (BM25
    keyword index, accent-insensitive) with a substring fallback.

    Use it when you need a detail that is not in your injected block — e.g.
    "how long the sweep takes" finds the baseline you recorded weeks ago.

    Required: ``query``. Optional: ``subject`` (narrow to one thing), ``kind``,
    ``limit`` (default 20, max 50).
    """
    from sqlalchemy import or_, select as _select
    from app.config import AGENT_SELF_MEMORY_ENABLED
    if not AGENT_SELF_MEMORY_ENABLED:
        return {"items": [], "skipped": True}
    from app.app_models.agent_memory import AgentMemory
    from app.agent_memory_utils import normalize_subject
    args = call.arguments or {}
    query = str(_require(args, "query"))
    limit = min(int(args.get("limit") or 20), _LIST_CAP)
    subject = normalize_subject(args.get("subject")) or None
    kind = str(args.get("kind") or "").strip().lower() or None
    like = f"%{query}%"
    async with app_session() as s:
        from app import memory_fts
        hits = await memory_fts.search(s, "agent_memory", query, None, 200)
        base = _select(AgentMemory).where(
            AgentMemory.superseded_by.is_(None),
            AgentMemory.review_state != "rejected",
        )
        if subject:
            base = base.where(AgentMemory.subject == subject)
        if kind in AGENT_MEMORY_KINDS:
            base = base.where(AgentMemory.kind == kind)
        if hits:
            rows = list((await s.execute(
                base.where(AgentMemory.id.in_(list(hits.keys())))
            )).scalars().all())
            rows.sort(key=lambda r: hits.get(r.id, 0.0), reverse=True)
            rows = rows[:limit]
        else:
            rows = list((await s.execute(
                base.where(or_(AgentMemory.content.ilike(like),
                               AgentMemory.subject.ilike(like)))
                .order_by(AgentMemory.observations.desc(),
                          AgentMemory.updated_at.desc())
                .limit(limit)
            )).scalars().all())
    return {"query": query, "items": [{
        "id": r.id, "kind": r.kind, "subject": r.subject,
        "content": _clip(r.content), "observations": r.observations,
        "validated": (r.review_state == "validated"),
        # Through iso_utc like every other tool result: "vigente hasta" decides
        # whether the agent still trusts a baseline, so a naive stamp read in the
        # wrong zone is a wrong answer, not a cosmetic detail.
        "valid_until": iso_utc(r.valid_until),
        "applied_as_rule": r.behavior_effect,
    } for r in rows]}


async def forget_learning(call: ToolCall):
    """Drop a learning about the environment that turned out to be wrong.

    Prefer recording the CORRECTED version with ``remember_learning`` (same
    ``subject``) when you are updating what "normal" means — that keeps the
    history. Use this only for something plainly wrong.

    Required: ``learning_id`` (``id`` is accepted as an alias). Optional:
    ``reason`` — why it stopped being true, kept as the trace of the decision.

    A learning that already backs an approved rule can NOT be forgotten here:
    removing the rule is the operator's decision (Aprendizaje → Revertir), not
    yours.
    """
    from sqlalchemy import select as _select, update as _update
    from datetime import datetime as _dt
    from app.app_models.agent_memory import AgentMemory
    args = call.arguments or {}
    # v7.158.0 — the maintenance prompt had been ordering `forget_learning(id, reason=...)`
    # since 7.140.0, and this tool required `learning_id` and ignored `reason`. Result: EVERY
    # attempt to retire an obsolete learning failed with "learning_id required" and the
    # reason was lost. It is the same pattern as `supersedes` (7.153.0): the prompt promising
    # an interface the code did not have. The alias is accepted and the reason stored,
    # instead of rewriting the prompt, because `id` is the natural thing for the model and
    # breaking it again would be a matter of time.
    if "learning_id" not in args and args.get("id") is not None:
        args = {**args, "learning_id": args.get("id")}
    lid = _require(args, "learning_id")
    try:
        lid = int(lid)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="learning_id must be an integer")
    async with app_write() as s:
        row = (await s.execute(
            _select(AgentMemory).where(AgentMemory.id == lid)
        )).scalar_one_or_none()
        if row is None:
            return {"ok": False, "error": "not_found"}
        if row.behavior_effect:
            return {"ok": False, "error": "has_active_rule",
                    "detail": ("This learning backs an approved rule "
                               f"({row.behavior_effect}). Removing it is a manager's "
                               "decision from Aprendizaje → Revertir.")}
        # Tombstone (point at self) instead of deleting: the trail of what the
        # employee believed and when must survive, same discipline as facts.
        motivo = str(args.get("reason") or "").strip()[:400]
        await s.execute(
            _update(AgentMemory).where(AgentMemory.id == lid)
            .values(superseded_by=lid, updated_at=_dt.utcnow(),
                    # WHY it stopped being true is half the value of forgetting: without it,
                    # tomorrow nobody knows whether that was retired with judgement.
                    provenance=(f"olvidado: {motivo}" if motivo else "olvidado"))
        )
        await s.commit()
    from app.agent_memory_utils import bump_memory_version
    bump_memory_version()
    return {"ok": True, "forgotten": lid, "reason": motivo or None}


async def compact_learnings(call: ToolCall):
    """Tidy your environment memory deterministically (no model call): expire
    what is past its validity, prune the oldest raw observations beyond the cap,
    and report which subjects have enough observations to be worth distilling
    into a baseline.

    Then do the semantic part yourself: for each reported subject, write ONE
    ``baseline`` with ``remember_learning`` summarising what normal looks like.
    """
    from sqlalchemy import func as _func, select as _select
    from app.agent_memory_utils import compact, stats
    from app.app_models.agent_memory import AgentMemory
    from app.config import AGENT_SELF_MEMORY_CONSOLIDATE_AFTER
    report = await compact()
    async with app_session() as s:
        ripe = (await s.execute(
            _select(AgentMemory.subject, _func.count(AgentMemory.id))
            .where(AgentMemory.kind == "observation",
                   AgentMemory.superseded_by.is_(None))
            .group_by(AgentMemory.subject)
            .having(_func.count(AgentMemory.id) >= max(
                int(AGENT_SELF_MEMORY_CONSOLIDATE_AFTER), 2))
        )).all()
    return {
        "ok": True, **report, "stats": await stats(),
        "ready_to_distil": [{"subject": r[0], "observations": int(r[1])} for r in ripe],
    }


async def propose_silence(call: ToolCall):
    """PROPOSE that a recurring signal stop being raised, because you have
    learned it is normal here — for an admin to accept.

    This is the GOVERNED path, and the difference from ``suppress_signal``
    matters: that one creates a bounded snooze you are allowed to grant yourself;
    this one is for an INDEFINITE or CONDITIONAL silence, which changes behaviour
    and therefore needs a human's OK. Until an admin accepts it, it silences
    NOTHING.

    Required: ``signal_kind`` (alert|recommendation|decision) and the scope —
    either ``signal_id`` / ``dedupe_key`` (this exact signal) or
    ``subject_pattern`` (a scope, e.g. ``*tenant-x*``).
    Recommended: ``learning_id`` (the learning that justifies it — that is what
    makes it auditable and revertible) and ``reason`` in business language.
    Optional: ``condition`` (the "only if" — ``{"kind":"weekday","days":[6,7]}``,
    ``{"kind":"time_window","from":"01:00","to":"05:30"}``,
    ``{"kind":"field","field":"target_entity_id","op":"eq","value":"x"}``, or
    ``{"kind":"all","of":[…]}``; add ``"effect":"defer","defer_minutes":60`` to
    WAIT AND RE-CHECK instead of going quiet), ``severity_max`` (never silence
    above this; a critical signal is never silenced whatever you pass),
    ``expires_days``.

    The result includes the dry-run ("Test"): how many of the last real signals
    this would have affected. SAY THAT NUMBER when you explain the proposal — it
    is what lets a human approve conditional behaviour honestly.


    Returns ``{"ok": true, "proposed": true, "rule_id": …}``. It stays PENDING until an administrator approves it: the signal keeps being raised until then.
    """
    from datetime import timedelta
    from app.signal_suppress import (
        _TABLE_TO_PREFIX, _norm, create_suppression, signal_label, suppression_key,
    )
    from app.timeutil import now_utc
    args = call.arguments or {}
    kind = _enum(args, "signal_kind", _SUPPRESS_KINDS, "alert")
    table = _SUPPRESS_KIND_TABLE[kind]
    reason = (str(args.get("reason") or "").strip()
              or "El empleado ha aprendido que esto es normal en este entorno.")
    pattern = str(args.get("subject_pattern") or "").strip() or None
    condition = args.get("condition")
    if isinstance(condition, str):
        try:
            condition = json.loads(condition)
        except ValueError:
            condition = None
    if not isinstance(condition, dict):
        condition = None
    severity_max = str(args.get("severity_max") or "").strip() or None
    try:
        expires_days = int(args["expires_days"]) if args.get("expires_days") else None
    except (TypeError, ValueError):
        expires_days = None
    expires_at = (now_utc() + timedelta(days=max(1, expires_days))
                  if expires_days else None)

    async with app_write() as session:
        key = label = None
        source_id = None
        sid = args.get("signal_id")
        if sid is not None:
            try:
                model = _SUPPRESS_MODEL[table]
                obj = (await session.execute(
                    select(model).where(model.id == int(sid))
                )).scalar_one_or_none()
            except (TypeError, ValueError):
                obj = None
            if obj is None:
                return {"ok": False, "error": "not_found",
                        "detail": f"{kind} {sid!r} no existe"}
            key, label, source_id = (suppression_key(table, obj),
                                     signal_label(table, obj), int(sid))
        elif args.get("dedupe_key"):
            dk = str(args["dedupe_key"]).strip()
            key, label = f"{_TABLE_TO_PREFIX[table]}:{_norm(dk)}", dk
        elif pattern:
            # A pattern rule has no exact identity; the key stays empty on
            # purpose (the invariant is "exactly one of the two carries scope").
            key, label = "", f"señales que coincidan con «{pattern}»"
        else:
            return {"ok": False, "error": "need_scope",
                    "detail": ("Pass signal_id, dedupe_key or subject_pattern "
                               "to bound what would be silenced.")}
        rule = await create_suppression(
            session, table=table, key=key or "", label=label,
            muted_until=None, reason=reason, created_by="agent",
            created_by_run_id=call.run_id, source_signal_id=source_id,
            subject_pattern=pattern, condition=condition,
            severity_max=severity_max, expires_at=expires_at,
            # THE governance bit: proposed, so the gate ignores it entirely.
            review_state="proposed",
        )
        await session.commit()
        rule_id = rule.id

    # The dry run over REAL history — the only honest way to approve something
    # conditional is to see its effect, not to read the condition.
    from app.signal_conditions import preview as _preview
    test = await _preview({
        "signal_table": table, "match_key": key or "",
        "match_label": label, "subject_pattern": pattern,
        "condition": condition, "severity_max": severity_max,
    })

    # Provenance: the effect row exists from the moment of the PROPOSAL, so the
    # operator can always see where it came from — even before accepting.
    effect = None
    lid = args.get("learning_id")
    if lid:
        try:
            from app import learning_effects_service as _lfx
            effect = await _lfx.materialize(
                learning_id=int(lid), target=_lfx.TARGET_SUPPRESSION,
                target_id=rule_id, label=str(label or key),
                origin_run_id=call.run_id, state="proposed",
            )
        except (TypeError, ValueError):
            effect = None
    return {
        "ok": True, "proposed": True, "rule_id": rule_id,
        "signal_table": table, "match_key": key or None,
        "subject_pattern": pattern, "condition": condition,
        "effect_id": (effect or {}).get("id"),
        "preview": test,
        "note": ("Proposal recorded: it does NOT silence anything yet. An "
                 "administrator has to accept it (it appears in the "
                 "conversation if they are present, and in Aprendizaje if not). "
                 "Tell them what you have learned and the result of the \"Test\"."),
    }


# ── Escape hatch (raw upsert) ────────────────────────────────────────


# v7.85.0 — local.* TABLE GUARD. The generic local.read/local.write escape hatch
# must NEVER reach internal PLATFORM tables (per-user memory, operator settings,
# approvals/grants governance, channels/directory PII, knowledge, audit). Without
# this, a prompt-injection could `local.read {table:"user_memory"}` (read ANY
# user's memory, bypassing the memory tools' per-user binding) or `local.write`
# over `app_settings`/`approvals`/`write_grants` (tamper guardrails, forge an
# approval, self-escalate). Access is restricted to the app's DECLARED domain
# output tables (manifest domain_entities, source=local) + the canonical business
# tables the named tools own — and an internal table is ALWAYS denied even if a
# manifest mistakenly declared it.
_LOCAL_BUSINESS_CANON = frozenset({
    "alerts", "recommendations", "communications", "scoring",
    "decisions", "scheduled_items",
})
_LOCAL_TABLE_DENY = frozenset({
    # per-user memory — only reachable via the binding memory tools
    "user_memory", "user_episodes", "user_files",
    # v7.140.0 — SELF-memory + the learned-rule audit trail. Same reasoning as
    # above: the governed tools are the only way in. A raw ``local.write`` here
    # would let a prompt injection forge an approved learning, stamp a
    # ``behavior_effect`` on it and silence signals without any admin OK — i.e.
    # walk around the entire governance this feature is built on.
    "agent_memory", "agent_episodes", "learning_effects", "signal_silence_log",
    # governance / guardrails / audit — the agent uses the named tools, never SQL
    "approvals", "write_grants", "action_overrides", "pending_tool_requests",
    "app_settings", "automations", "automation_state", "agent_persona",
    "agent_runs", "tool_call_log", "events", "event_audit", "watcher_cursors",
    "watcher_schedules", "schema_version", "mcp_connectors", "mcp_tools_cache",
    # knowledge + operator-authored config
    "knowledge_documents", "knowledge_chunks", "knowledge_requests",
    "scoped_knowledge", "notify_rules", "signal_suppressions",
    # channels + directory — inbound PII
    "channels", "channel_messages", "channel_contacts", "app_users",
    "agent_sessions",
})


def _allowed_local_tables() -> frozenset:
    """The DECLARED domain output tables (manifest domain_entities, source=local),
    lower-cased, plus the canonical business tables — minus anything on the
    internal denylist. Derived from the manifest so it stays correct as the app's
    domain_entities evolve (no hand-maintained list)."""
    allowed = {t.lower() for t in _LOCAL_BUSINESS_CANON}
    try:
        from app.config import AGENT_APP
        for e in (AGENT_APP.get("domain_entities") or []):
            if isinstance(e, dict) and e.get("source") == "local" and e.get("table"):
                allowed.add(str(e["table"]).strip().lower())
    except Exception:  # pragma: no cover — manifest shape guard
        pass
    return frozenset(allowed - _LOCAL_TABLE_DENY)


def _guard_local_table(table: str) -> None:
    """Raise 403 unless ``table`` is a declared domain/business table AND not an
    internal platform table. This is the trust boundary for the local.* escape
    hatch (prompt-injection hardening); the named tools remain the way to touch
    governed data (memory/approvals/…)."""
    t = (table or "").strip().lower()
    if t in _LOCAL_TABLE_DENY or t not in _allowed_local_tables():
        raise HTTPException(
            status_code=403,
            detail=(
                f"table {table!r} is not accessible via local.read/local.write. "
                "Use the named tools for governed data (memory/approvals/etc.), or "
                "declare it as a domain entity (source=local) in the manifest."
            ),
        )


async def local_write(call: ToolCall):
    """Generic upsert into a local table — escape hatch only.

    Prefer one of the named tools above. This handler exists for DECLARED
    domain tables the canon does not cover (e.g. ``welcome_queue``,
    ``customer_segments``) — those must be declared as ``domain_entities``
    (``source: "local"``). Internal platform tables (memory, settings,
    approvals, grants, channels…) are NOT reachable here.

    Parameters (each one documented on its own line ON PURPOSE: this docstring
    IS the interface the model reads before calling, and a set literal like
    ``{"table", "values", "pk"}`` tells it the names but not what they mean or
    what happens when they are wrong):

    * ``table`` — REQUIRED. Name of the declared domain table. Treated as a
      literal: it must be an identifier (letters, digits, ``_``) AND be in the
      allowlist. Anything else is rejected with 400 — it is never interpolated
      blindly into SQL.
    * ``values`` — REQUIRED. Non-empty ``{column: value}`` dict with the row to
      insert or update. Column names get the same identifier check; the VALUES
      always travel as bound parameters.
    * ``pk`` — Conflict column that decides insert-vs-update
      (``ON CONFLICT(pk) DO UPDATE``). Default ``id``. Same identifier check.

    Returns ``{"result": {"ok": true, "table": ...}, "replayed": bool}``.
    **``replayed`` matters**: ``true`` means this exact step already ran in this
    run and the write was NOT repeated — the idempotency guard answered with the
    previous outcome. Reading it as "it didn't work" and retrying is precisely
    the loop this guard exists to prevent.
    """
    args = call.arguments  # {"table": ..., "values": {...}, "pk": ...}
    table = str(args.get("table") or "")
    pk = str(args.get("pk") or "id")
    values = args.get("values") or {}
    if not table.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail=f"invalid table {table!r}")
    _guard_local_table(table)
    if not pk.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail=f"invalid pk {pk!r}")
    if not isinstance(values, dict) or not values:
        raise HTTPException(status_code=400, detail="values must be a non-empty dict")
    for col in values:
        if not col.replace("_", "").isalnum():
            raise HTTPException(status_code=400, detail=f"invalid column {col!r}")

    async with app_write() as session:
        async def _do():
            stmt = text(
                f"INSERT INTO {table} ({','.join(values)}) "
                f"VALUES ({','.join(':'+k for k in values)}) "
                f"ON CONFLICT({pk}) DO UPDATE SET "
                + ", ".join(f"{k}=excluded.{k}" for k in values)
            )
            await session.execute(stmt, values)
            await session.commit()
            return {"ok": True, "table": table}

        result, replayed = await _replay_or_execute(
            session,
            run_id=call.run_id,
            step_idx=call.step_idx,
            tool_name="local.write",
            args=args,
            do_work=_do,
            mode=getattr(call, "mode", "autonomous"),
        )
        return {"result": result, "replayed": replayed}


async def local_read(call: ToolCall):
    """Generic read of a local table — escape hatch for CUSTOM domain
    tables not covered by the named ``list_*`` readers above.

    Prefer the named readers (``list_alerts``, ``list_scheduled_items``, …).
    Use this only for extra tables the agent created via ``local.write``.

    Parameters:

    * ``table`` — REQUIRED. Name of the declared domain table. Identifier-shape
      check plus allowlist, same as ``local.write``.
    * ``where`` — ``{column: value}`` equality filters. Column names are
      validated; values always travel as bound parameters.
    * ``order_by`` — Column name; prefix with ``-`` for descending. Default:
      newest first by ``rowid``.
    * ``limit`` — Rows to return. Default 20, capped at 50.

    Returns the matching rows. An empty list means the table has no row that
    matches — NOT that the table is missing: a table that does not exist, or is
    not on the allowlist, is rejected with 400 instead.
    """
    args = call.arguments or {}
    table = str(args.get("table") or "")
    where = args.get("where") or {}
    order_by = str(args.get("order_by") or "").strip()
    limit = min(int(args.get("limit") or 20), _LIST_CAP)
    if not table.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail=f"invalid table {table!r}")
    _guard_local_table(table)
    if not isinstance(where, dict):
        raise HTTPException(status_code=400, detail="where must be a dict")
    desc = order_by.startswith("-")
    order_col = order_by[1:] if desc else order_by
    if order_col and not order_col.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail=f"invalid order_by {order_by!r}")
    conds: list[str] = []
    params: dict[str, Any] = {}
    for i, (col, val) in enumerate(where.items()):
        if not str(col).replace("_", "").isalnum():
            raise HTTPException(status_code=400, detail=f"invalid column {col!r}")
        params[f"w{i}"] = val
        conds.append(f"{col} = :w{i}")
    sql = f"SELECT * FROM {table}"  # nosec B608 — identifiers validated above
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += f" ORDER BY {order_col} {'DESC' if desc else 'ASC'}" if order_col \
        else " ORDER BY rowid DESC"
    sql += f" LIMIT {limit}"
    async with app_session() as session:
        try:
            res = await session.execute(text(sql), params)
            rows = [dict(r._mapping) for r in res]
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"read failed: {exc}")
    return {"count": len(rows), "table": table, "rows": rows}


# ── Managed users — the employee's contact directory (v7.20.0) ───────
async def _resolve_directory_email(term: str) -> Optional[str]:
    """Thin alias — the canonical resolver lives in ``users_directory``
    (v7.24.0) so routes and MCP tools share ONE person-resolution rule."""
    from app.users_directory import resolve_directory_email
    return await resolve_directory_email(term)


async def find_user(call: ToolCall):
    """Find people in your USER DIRECTORY by email, phone, name, alias or
    nickname. Use it to resolve "send this to Juan" → Juan's email / WhatsApp
    number BEFORE sending. Arg: ``q`` (search text; empty = list all). Returns
    the matching users with email + phones + aliases."""
    from sqlalchemy import select as _select
    from app.app_database import app_session
    from app.app_models.users import AppUser
    from app.users_directory import loads_list, loads_meta
    q = (str((call.arguments or {}).get("q") or "")).strip().lower()
    async with app_session() as s:
        rows = list((await s.execute(
            _select(AppUser).where(AppUser.active == 1)
        )).scalars().all())

    def _match(r) -> bool:
        if not q:
            return True
        hay = " ".join(str(x) for x in (
            r.email, r.display_name, r.alias,
            " ".join(loads_list(r.nicknames_json)),
            " ".join(loads_list(r.phones_json)),
        ) if x).lower()
        return q in hay

    def _row(r) -> dict:
        # ``preferred_channel`` (email|whatsapp|telegram) hints WHERE to reach them
        # when they have more than one address — surface it so you don't have to
        # guess. Absent/``auto`` = use your judgement.
        from app.users_directory import REACHABLE_CHANNELS
        pref = (loads_meta(r.metadata_json).get("preferred_channel") or "").strip().lower()
        d = {
            "id": r.id, "email": r.email, "phones": loads_list(r.phones_json),
            "display_name": r.display_name, "alias": r.alias,
            "nicknames": loads_list(r.nicknames_json), "role": r.role,
        }
        if pref in REACHABLE_CHANNELS:
            d["preferred_channel"] = pref
        return d

    out = [_row(r) for r in rows if _match(r)]
    return {"users": out[:50], "count": len(out)}


async def list_users(call: ToolCall):
    """List everyone in your USER DIRECTORY (contacts). Same fields as
    find_user. Answers "who do I know / who can I write to?"."""
    return await find_user(call)


async def create_user(call: ToolCall):
    """Add a person to your USER DIRECTORY so you can reach them later
    (email/phone) and recognise them by alias/nickname. Args: ``email``,
    ``phone`` or ``phones`` (E.164), ``display_name``, ``alias``,
    ``nicknames``, ``preferred_channel`` (``email``|``whatsapp``|``telegram`` —
    where to reach them by default). NOTE: new contacts are always MEMBERS — you
    cannot grant the admin role (only an operator can)."""
    from app.app_models.users import SOURCE_AGENT
    from app.users_directory import REACHABLE_CHANNELS, upsert_directory_user
    args = call.arguments or {}
    email = (str(args.get("email") or "")).strip().lower() or None
    phones = args.get("phones") or ([args["phone"]] if args.get("phone") else [])
    if not email and not phones:
        return {"error": "need an email or a phone to create a user"}
    pref = (str(args.get("preferred_channel") or "")).strip().lower()
    meta = {"preferred_channel": pref} if pref in REACHABLE_CHANNELS else None
    # Upsert by email (no duplicates) — agent path NEVER escalates the role
    # (allow_role omitted → member); bad phones are dropped silently.
    row = await upsert_directory_user(
        email=email, phones=phones,
        display_name=args.get("display_name"),
        alias=args.get("alias"),
        nicknames=args.get("nicknames") or [],
        source=SOURCE_AGENT, created_by="agent", metadata=meta,
    )
    return {"created": True, "id": row.id, "email": row.email}


# ── Discovery (read-only) ────────────────────────────────────────────


@router.get("/tools", dependencies=[Depends(_check_token)])
async def list_tools():
    """Return the catalogue of registered tools so the operator-facing
    Operations page can render examples without the SPA having to
    keep the list in sync. Hits this endpoint at startup and caches
    the result client-side."""
    return {
        "tools": [
            {"name": name, "doc": (fn.__doc__ or "").strip().split("\n\n")[0]}
            for name, fn in TOOLS.items()
        ],
    }


# ── Business knowledge the employee REQUESTS from the operator ───────
#
# When the employee needs a business parameter it cannot infer (a threshold, a
# target, a cutoff), it asks for it instead of guessing. ``request_knowledge``
# registers the need AND returns a usable value (the operator's answer, or the
# agent's own ``suggested`` default) so work never stalls; the operator fills the
# TYPED value in Operations → Conocimiento and it is injected into the prompt.
# ``set_knowledge`` lets a REAL human ADMIN change a value from chat — anyone else
# (a member, or the agent on a background run) can only PROPOSE it.
_KNOWLEDGE_VALUE_TYPES = ("text", "number", "boolean", "list", "date")
_KNOWLEDGE_PENDING_CAP = 50  # anti-spam: stop creating NEW pending rows past this


def _knowledge_key(raw: Any) -> str:
    """Normalise an agent-supplied key to a stable slug (lowercase, a-z0-9_)."""
    import re
    s = re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")
    return s[:80] or "param"


# v7.146.0 — identity resolution and the gates live in ``mcp_actor`` (one single definition
# for the 69 tools, not one guard per place). The historical private names are kept as
# aliases: they are the ones the automations guard, ``set_knowledge`` and
# ``tests/test_admin_gates.py`` use.
_run_serving_user = run_serving_user
_is_real_human_admin = is_real_human_admin
_run_mode = run_mode


async def request_knowledge(call: ToolCall):
    """Ask the operator for a business PARAMETER you need but cannot infer from
    the data — a threshold, a target, a cutoff, a list of exceptions, etc.

    Required: ``key`` (a STABLE slug you will reuse to read the value, e.g.
    ``high_risk_invoice_threshold``), ``label`` (human title for the operator, in
    SPANISH). Optional: ``question`` (what you need and WHY — shown to the
    operator, in SPANISH),
    ``value_type`` (``text|number|boolean|list|date``; default ``text``). Use
    ``list`` when the answer is ONE OR SEVERAL values (e.g. "priority entity
    types": leads, contacts…) — the operator can pick several or type
    their own; pass the allowed/suggested set in ``options`` and a
    comma-separated ``suggested`` (e.g. ``"leads, contacts"``).
    ``unit`` (``€``, ``days``, ``%``…), ``options`` (allowed/suggested values for
    a ``list``), ``suggested`` (the value you will use meanwhile).

    INTERNATIONALIZATION — ALWAYS provide BOTH languages so any operator can read
    the parameter: pass ``label`` / ``question`` in Spanish AND ``label_en`` /
    ``question_en`` with the English translation. The UI shows the pair matching
    the operator's active language. If you omit the English pair we reuse the
    Spanish text, but you should translate it yourself.

    This is GET-OR-CREATE by ``key`` and NEVER blocks you: it returns
    ``{status, value, suggested, effective}`` where ``effective`` is the
    operator's confirmed value if given, else your ``suggested`` — use
    ``effective`` and keep working. The request is registered so the operator can
    fill it in Operations → Conocimiento; once answered the value is also injected
    into your prompt automatically, so you usually won't need to call this twice.
    """
    from sqlalchemy import func
    from app.timeutil import now_utc
    from app.app_models.runtime import KnowledgeRequest
    args = call.arguments or {}
    key = _knowledge_key(_require(args, "key"))
    label = str(_require(args, "label"))[:200]
    question = str(args.get("question") or "")
    label_en = (str(args.get("label_en") or "").strip()[:200] or None)
    question_en = (str(args.get("question_en") or "").strip() or None)
    value_type = str(args.get("value_type") or "text").strip().lower()
    if value_type not in _KNOWLEDGE_VALUE_TYPES:
        value_type = "text"
    unit = (str(args.get("unit") or "").strip() or None)
    suggested = args.get("suggested")
    suggested = str(suggested) if suggested is not None else None
    opts = args.get("options")
    options_json = json.dumps([str(o) for o in opts]) if isinstance(opts, list) and opts else None

    async with app_write() as session:
        async def _do():
            now = now_utc().replace(tzinfo=None)
            row = (await session.execute(
                select(KnowledgeRequest).where(KnowledgeRequest.key == key)
            )).scalar_one_or_none()
            if row is None:
                pending = (await session.execute(
                    select(func.count()).select_from(KnowledgeRequest)
                    .where(KnowledgeRequest.status == "pending")
                )).scalar() or 0
                if pending >= _KNOWLEDGE_PENDING_CAP:
                    return {"key": key, "status": "pending", "value": None,
                            "suggested": suggested, "unit": unit, "provided": False,
                            "effective": suggested,
                            "note": "too many pending knowledge requests; using suggested"}
                row = KnowledgeRequest(
                    key=key, label=label, question=question,
                    label_en=label_en, question_en=question_en, value_type=value_type,
                    unit=unit, options_json=options_json, suggested=suggested,
                    status="pending", origin="agent",
                    requested_by_run_id=call.run_id, requested_at=now,
                )
                session.add(row)
                await session.commit()
                await session.refresh(row)
            else:
                # Refresh the metadata the agent may have improved, but NEVER the
                # confirmed value (only an admin sets that).
                changed = False
                for attr, val in (("label", label), ("question", question),
                                  ("label_en", label_en), ("question_en", question_en),
                                  ("suggested", suggested), ("unit", unit),
                                  ("options_json", options_json)):
                    if val and getattr(row, attr) != val:
                        setattr(row, attr, val); changed = True
                if row.requested_by_run_id is None:
                    row.requested_by_run_id = call.run_id; row.requested_at = now; changed = True
                if changed:
                    await session.commit()
                    await session.refresh(row)
            provided = row.status == "provided" and (row.value not in (None, ""))
            return {
                "key": row.key, "status": row.status,
                "value": row.value if provided else None,
                "suggested": row.suggested, "unit": row.unit, "provided": provided,
                "effective": (row.value if provided else row.suggested),
            }

        result, replayed = await _replay_or_execute(
            session, run_id=call.run_id, step_idx=call.step_idx, tool_name=call.name,
            args=args, do_work=_do, mode=getattr(call, "mode", "autonomous"),
        )
        return {"result": result, "replayed": replayed}


async def set_knowledge(call: ToolCall):
    """Change the value of a business parameter — e.g. the boss tells you in chat
    "the high-risk invoice threshold is now 3000€".

    Required: ``key`` (the parameter's slug), ``value`` (the new value).

    GOVERNANCE: only a REAL human ADMIN's word takes effect. If the person you
    are serving is an admin, the value is applied immediately (same as editing it
    in Operations → Conocimiento). Otherwise — a non-admin, or you on a background
    run — it is recorded as a PROPOSAL that an admin must confirm; it does NOT
    change the value that governs your behaviour. The response tells you which
    happened (``applied: true/false``).
    """
    from app.timeutil import now_utc
    from app.app_models.runtime import KnowledgeRequest
    args = call.arguments or {}
    key = _knowledge_key(_require(args, "key"))
    value = str(_require(args, "value"))
    actor = await _run_serving_user(call.run_id)
    # human_surface only when this is an interactive chat turn — on channel /
    # autonomous runs the autonomous-identity exclusion must stand so the
    # employee can never self-apply a governing parameter.
    is_admin = await _is_real_human_admin(
        actor, human_surface=(await _run_mode(call.run_id)) == "chat",
    )

    async with app_write() as session:
        async def _do():
            now = now_utc().replace(tzinfo=None)
            row = (await session.execute(
                select(KnowledgeRequest).where(KnowledgeRequest.key == key)
            )).scalar_one_or_none()
            created = False
            if row is None:
                row = KnowledgeRequest(
                    key=key, label=key.replace("_", " ").title(), question="",
                    value_type="text", status="pending", origin="agent",
                    requested_by_run_id=call.run_id, requested_at=now,
                )
                session.add(row)
                created = True
            if is_admin:
                row.value = value
                row.status = "provided"
                row.provided_by = actor
                row.provided_at = now
                row.proposed_value = None
                row.proposed_by = None
                row.proposed_at = None
                await session.commit()
                await session.refresh(row)
                return {"applied": True, "key": row.key, "status": "provided", "value": row.value}
            # Non-admin (or background) → proposal only.
            row.proposed_value = value
            row.proposed_by = actor or "chat"
            row.proposed_at = now
            await session.commit()
            await session.refresh(row)
            return {"applied": False, "key": row.key, "status": row.status,
                    "proposed_value": value, "created": created,
                    "note": "recorded as a proposal — a human admin must confirm it"}

        result, replayed = await _replay_or_execute(
            session, run_id=call.run_id, step_idx=call.step_idx, tool_name=call.name,
            args=args, do_work=_do, mode=getattr(call, "mode", "autonomous"),
        )
        return {"result": result, "replayed": replayed}


# ── Document knowledge library (read tools) ──────────────────────────
async def _knowledge_run_context(call: ToolCall) -> tuple[str, bool]:
    """Resolve ``(user_email, attending)`` for a knowledge search BOUND TO THE RUN,
    server-side — never from a spoofable LLM argument.

    The served human is the run's persisted owner (``_run_serving_user`` reads
    ``request_meta_json.memory_user``), exactly like the identity binding the memory
    tools use. A run with an owner is an attended run (sees ``all`` + ``attending`` +
    that person's own ``user`` docs); a background/autonomous run has no owner →
    ``attending=False`` (sees ``all`` + ``autonomous``). This blocks a prompt-injected
    ``user_email`` from reading another person's ``user``-scoped material or escalating
    an autonomous run to ``attending``. When the agent omits ``run_id`` on a read the
    context degrades to the safe background scope (no leak, just narrower)."""
    owner = await _run_serving_user(getattr(call, "run_id", 0) or 0)
    owner = (owner or "").strip().lower()
    return (owner, True) if owner else ("", False)


def _doc_in_scope(doc, user_email: str, attending: bool) -> bool:
    if doc.scope == "all":
        return True
    if attending:
        if doc.scope == "attending":
            return True
        if doc.scope == "user":
            return bool(user_email) and (doc.user_email or "").strip().lower() == user_email
        return False
    return doc.scope == "autonomous"


async def search_knowledge(call: ToolCall):
    """Search the employee's DOCUMENT LIBRARY (files the operator uploaded:
    catalogue, prices, policies, requirements, FAQs, scripts…) for OFFICIAL
    content, and ALWAYS cite the source document.

    Use this whenever a person asks something whose answer should come from
    official material — a price, a condition, a policy, a requirement, a date —
    instead of guessing or relying on generic knowledge. This is NOT the CRM
    (live data lives in the connectors) and NOT your per-user memory (things you
    learned); it is the operator's reference material.

    Required: ``query`` (what you need, in natural language). Optional: ``limit``
    (default from config, max 20). Pass ``run_id`` (from your prompt) so the search
    is scoped to the right context — the system resolves WHO you are serving from
    the run itself, so material scoped to a person applies automatically.

    Returns ranked ``results`` — each ``{document_id, title, snippet, relevance,
    visibility}``. RULES: (1) answer FROM the snippets, do not invent; (2) always
    tell the person WHICH document it came from (the ``title``); (3) a result with
    ``visibility='internal'`` is for YOUR reasoning only — never quote or forward
    it to a person, only use ``shareable`` documents verbatim; (4) if ``results``
    is empty, say you don't have it documented — do NOT make it up. For more
    context from one document, call ``get_knowledge_doc``.
    """
    from app import knowledge_library as klib
    args = call.arguments or {}
    query = str(_require(args, "query"))
    raw_limit = int(args.get("limit") or 0)
    limit = min(raw_limit, 20) if raw_limit > 0 else None
    user_email, attending = await _knowledge_run_context(call)
    hits = await klib.search(query, attending=attending, user_email=user_email, limit=limit)
    return {
        "count": len(hits),
        "results": hits,
        "note": (
            "Answer only from these snippets and name the source document. "
            "Do not quote a 'internal' document to a person. A result with "
            "stale=true is EXPIRED (its valid_until has passed): don't quote it as "
            "current — warn or find the up-to-date version. If empty, say it is "
            "not documented — never invent an official figure."
        ) if hits else (
            "No document matched. Tell the person it is not documented rather "
            "than guessing, or ask the operator to upload the reference."
        ),
    }


async def list_knowledge_docs(call: ToolCall):
    """List the reference DOCUMENTS available to you right now (enabled, indexed
    and in scope for this run) so you know what official material exists before
    you answer. Optional ``q`` filters by title/tag substring.

    Returns ``documents`` — each ``{document_id, title, tags, visibility, summary,
    valid_until}``. To actually read content, call ``search_knowledge``."""
    from sqlalchemy import select as _select
    from app.app_models.knowledge_library import KnowledgeDocument
    from app.timeutil import iso_utc
    args = call.arguments or {}
    q = (args.get("q") or "").strip().lower()
    user_email, attending = await _knowledge_run_context(call)
    async with app_session() as s:
        rows = (await s.execute(
            _select(KnowledgeDocument)
            .where(KnowledgeDocument.enabled == 1, KnowledgeDocument.status == "ready")
            .order_by(KnowledgeDocument.title.asc())
        )).scalars().all()
    out = []
    for r in rows:
        if not _doc_in_scope(r, user_email, attending):
            continue
        if q and q not in (r.title or "").lower() and q not in (r.tags or "").lower():
            continue
        out.append({
            "document_id": r.id, "title": r.title, "tags": r.tags,
            "visibility": r.visibility, "summary": r.summary,
            "valid_until": iso_utc(r.valid_until) if r.valid_until else None,
        })
    return {"count": len(out), "documents": out}


async def get_knowledge_doc(call: ToolCall):
    """Read MORE of ONE reference document when a single snippet is not enough.

    Required: ``document_id``. Optional: ``query`` (rank the document's own chunks
    by relevance to it) and ``limit`` (chunks, default 6, max 20). Returns the
    document metadata + its most relevant text. Honours the same visibility rule:
    a ``visibility='internal'`` document is for your reasoning only — never quote
    it to a person."""
    from sqlalchemy import select as _select
    from app.app_models.knowledge_library import KnowledgeChunk, KnowledgeDocument
    from app.timeutil import iso_utc
    args = call.arguments or {}
    doc_id = int(_require(args, "document_id"))
    query = str(args.get("query") or "").strip()
    limit = min(int(args.get("limit") or 6), 20)
    user_email, attending = await _knowledge_run_context(call)
    async with app_session() as s:
        doc = (await s.execute(
            _select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
        )).scalar_one_or_none()
        if doc is None or not doc.enabled or doc.status != "ready":
            return {"found": False, "note": "No such available document."}
        if not _doc_in_scope(doc, user_email, attending):
            return {"found": False, "note": "That document is not in scope for this run."}
        ranked_ids: list[int] = []
        if query:
            from app import knowledge_fts
            hits = await knowledge_fts.search(
                s, query, attending=attending, user_email=user_email,
                shareable_only=False, limit=40,
            )
            ranked_ids = [h["ordinal"] for h in hits if h["document_id"] == doc_id]
        stmt = _select(KnowledgeChunk.ordinal, KnowledgeChunk.text).where(
            KnowledgeChunk.document_id == doc_id
        ).order_by(KnowledgeChunk.ordinal.asc())
        chunks = {int(o): t for o, t in (await s.execute(stmt)).all()}
    if ranked_ids:
        order = ranked_ids + [o for o in sorted(chunks) if o not in ranked_ids]
    else:
        order = sorted(chunks)
    text = "\n\n".join(chunks[o] for o in order[:limit] if o in chunks)
    return {
        "found": True, "document_id": doc.id, "title": doc.title,
        "visibility": doc.visibility, "tags": doc.tags,
        "valid_until": iso_utc(doc.valid_until) if doc.valid_until else None,
        "content": text,
        "note": ("This is an INTERNAL document — use it to reason, do not quote it "
                 "to a person.") if doc.visibility == "internal" else None,
    }


# ── Registry ─────────────────────────────────────────────────────────

# Imported down here (not in the header) because ``mcp_operations`` reuses handlers from
# ``app.routes.*`` that in turn import from this module: at the registry level everything
# is already defined and the cycle does not close.
from app.mcp_operations import (  # noqa: E402
    READ_ONLY as _OPERATIONS_READ_ONLY,
    REQUIRED as _OPERATIONS_REQUIRED,
    SCHEMAS as _OPERATIONS_SCHEMAS,
    TOOLS as _OPERATIONS_TOOLS,
)

async def pedir_ubicacion(call: ToolCall):
    """Ask the person you are talking to for their CURRENT location, and wait for it.

    Use it ONLY when the task genuinely needs where they are (nearest branch, travel time,
    "what's around me"). Ask ONCE per conversation and reuse what you get; asking again for
    every message is intrusive even when it is silent.

    Optional: ``motivo`` — one short sentence saying what you need it for. It is shown to the
    person, so make it honest and specific ("to look for restaurants nearby").

    Returns ``{"ok": true, "lat":…, "lon":…, "precision_m":…, "antiguedad_s":…}`` when it was
    shared, or ``{"ok": false, "motivo": "..."}`` when it was not (the client cannot do it,
    the person did not allow it, or it timed out). **A "no" is a normal answer**, and it comes
    with ``di_a_la_persona``: tell the person CLEARLY that you tried and could not get it and
    ask them to share it (the location button, or in words) — never just ask "which city?"
    as if you had not tried. Never invent a location, and
    never reuse one you remember from another day as if it were current.
    """
    args = call.arguments or {}
    from app import ubicacion as ubic
    from app.agent_sessions import interactive_scope
    from app.chat_runtime import get_turn
    from app.mcp_actor import run_serving_user

    persona = await run_serving_user(call.run_id)
    if not persona:
        # Without a person present (autonomous run, channel): there is nobody to ask.
        return {"ok": False, "motivo": "there is nobody in front in this run"}

    # The THREAD decides the scope, and the scope decides which turn is alive. Without this, a
    # conversation in a secondary thread would look for the default thread's turn.
    from sqlalchemy import select as _select
    from app.app_database import app_session as _sess
    from app.app_models.runtime import AgentRun as _AR
    async with _sess() as _s:
        hilo = (await _s.execute(
            _select(_AR.thread_id).where(_AR.id == int(call.run_id)))).scalar_one_or_none()
    scope = interactive_scope(persona, hilo or None)
    turn = await get_turn(scope.scope_key)
    if turn is None or not turn.is_running:
        return {"ok": False, "motivo": "the conversation is no longer active"}

    # THE FILTER GOES HERE, IN THE CALL, and not by removing the tool from the catalog: opencode
    # fixes a session's tools WHEN CREATING IT, so a dynamic list would leave the capability
    # granted later invisible, and without saying so (it already happened with colleagues).
    if "ubicacion" not in (turn.capacidades or []):
        return {"ok": False,
                "motivo": ("this client cannot give the location (e.g. the web, or a phone "
                           "without the permission enabled). Ask for it in words if you need it")}

    peticion_id, futuro = ubic.abrir(persona)
    await turn.append({
        "type": "location_request",
        "request_id": peticion_id,
        "motivo": str(args.get("motivo") or "").strip()[:200],
    })
    datos = await ubic.esperar(peticion_id, futuro)

    # The trail is recorded in the RUN so it survives in the history (`_finish_chat_run`
    # attaches it to the stored text). Live it is already seen through the event above.
    try:
        import json as _json
        from sqlalchemy import update as _update
        from app.app_database import app_write as _write
        async with _write() as _s:
            _raw = (await _s.execute(
                _select(_AR.request_meta_json).where(_AR.id == int(call.run_id)))
            ).scalar_one_or_none()
            _meta = _json.loads(_raw or "{}") if _raw else {}
            _meta["ubicacion"] = (datos if datos and not datos.get("no_disponible")
                                  else {"no_disponible": (datos or {}).get("no_disponible")
                                        or "sin_respuesta"})
            await _s.execute(_update(_AR).where(_AR.id == int(call.run_id))
                             .values(request_meta_json=_json.dumps(_meta, ensure_ascii=False)))
            await _s.commit()
    except Exception:  # noqa: BLE001 — the trail never breaks the tool
        logger.debug("ubicacion: no se pudo anotar el rastro", exc_info=True)

    if not datos or datos.get("no_disponible"):
        # SAY IT CLEARLY (24/09/2026). Without this the model carried on as if nothing and asked
        # "which city are you in?", and the person did not know whether the phone had sent
        # anything or not. The sentence travels in the result —not only in the docstring—
        # because it is where the model has it in front of it right when deciding what to answer.
        return {"ok": False,
                "motivo": (datos or {}).get("no_disponible") or "it did not arrive in time",
                "di_a_la_persona": ("I tried to get your location, but I couldn't. "
                                    "Give it to me —with the location button or by telling me "
                                    "where you are— so I can answer you."),
                "como_seguir": ("Tell them that (or nearly), in their language, and wait for "
                                "their location: do not invent one nor ask for \"the city\" "
                                "without explaining that you could not get it.")}
    return {
        "ok": True,
        "lat": datos.get("lat"), "lon": datos.get("lon"),
        "precision_m": datos.get("precision"),
        "antiguedad_s": datos.get("antiguedad_s"),
        "nota": ("It is their position NOW. Use it as is, do not replace it with any you "
                 "remember from before, and take the accuracy into account before stating a street."),
    }


TOOLS: dict[str, Callable[[ToolCall], Awaitable[Any]]] = {
    # Ask where the person writing is (only if their client can give it).
    "pedir_ubicacion":         pedir_ubicacion,
    # Read tools — full visibility of your local DB; consult BEFORE
    # writing to avoid duplicates, and to answer "what do I have?".
    "list_alerts":             list_alerts,
    "list_recommendations":    list_recommendations,
    "list_decisions":          list_decisions,
    "list_scheduled_items":    list_scheduled_items,
    "list_communications":     list_communications,
    "list_scoring":            list_scoring,
    "list_approvals":          list_approvals,
    "local.read":              local_read,
    # Self-describe: which entities (incl. custom) THIS app defines.
    "describe_entities":       describe_entities,
    # Per-user memory read tools.
    "list_memory":             list_memory,
    "search_memory":           search_memory,
    # Self-memory read tool (what it learned about the ENVIRONMENT).
    "search_learnings":        search_learnings,
    # User directory (contacts): resolve/lookup people and add new contacts.
    "find_user":               find_user,
    "list_users":              list_users,
    "create_user":             create_user,
    # Canonical action tools — ALWAYS prefer these over local.write.
    "upsert_alert":            upsert_alert,
    "register_recommendation": register_recommendation,
    "log_communication":       log_communication,
    "upsert_scoring":          upsert_scoring,
    "log_decision":            log_decision,
    # Lifecycle transitions — close/decide existing rows (no local.write).
    "resolve_alert":           resolve_alert,
    "decide_recommendation":   decide_recommendation,
    "decide_decision":         decide_decision,
    # "No repetir": stop regenerating a recurring signal (temporary snooze).
    "suppress_signal":         suppress_signal,
    "list_suppressions":       list_suppressions,
    # Business knowledge the employee needs (thresholds/targets/criteria).
    "request_knowledge":       request_knowledge,
    "set_knowledge":           set_knowledge,
    # Document knowledge library — official reference material, WITH citation.
    "search_knowledge":        search_knowledge,
    "list_knowledge_docs":     list_knowledge_docs,
    "get_knowledge_doc":       get_knowledge_doc,
    # Time-anchored work: calendar, delegated tasks, follow-ups, approvals.
    "schedule_task":            schedule_task,
    "assign_task_to_user":      assign_task_to_user,
    "complete_scheduled_item":  complete_scheduled_item,
    "request_approval":         request_approval,
    "mark_approval_executed":   mark_approval_executed,
    # Automatizaciones — conversational management (interactive/boss only).
    "get_automation_catalog":   get_automation_catalog,
    "list_automations":         list_automations,
    "create_automation":        create_automation,
    "update_automation":        update_automation,
    "evolve_automation":        evolve_automation,
    "set_automation_enabled":   set_automation_enabled,
    "test_automation":          test_automation,
    "run_automation":           run_automation,
    # Per-user memory write tools.
    "remember_fact":           remember_fact,
    "record_episode":          record_episode,
    "index_file":              index_file,
    "forget_fact":             forget_fact,
    "compact_memory":          compact_memory,
    # Self-memory write tools + the GOVERNED path from a learning to a rule.
    "remember_learning":       remember_learning,
    "forget_learning":         forget_learning,
    "compact_learnings":       compact_learnings,
    "propose_silence":         propose_silence,
    # Escape hatch for domain-specific tables not covered by the canon.
    "local.write":             local_write,
    # v7.146.0 — PARITY with the UI: knowing itself (describe_app), money, audit, deciding
    # approvals, governance of autonomous work, settings, channels and silences. They live in
    # ``mcp_operations`` because they REUSE each screen's handler instead of reimplementing
    # it; each one's permission is declared in ``mcp_actor.TOOL_MIN_ROLE`` and the dispatch
    # applies it.
    **_OPERATIONS_TOOLS,
}

# Read-only tools: dispatched directly (no run_id resolution, no
# tool_call_log row — reads must work even before the first run and
# never pollute the audit trail).
READ_TOOLS = frozenset({
    "list_alerts", "list_recommendations", "list_decisions",
    "list_scheduled_items", "list_communications", "list_scoring",
    "list_approvals", "local.read", "describe_entities",
    # Automation config tools manage their own sessions (no run audit) and
    # must work in interactive chat regardless of an autonomous run id.
    "get_automation_catalog", "list_automations", "create_automation",
    "update_automation", "evolve_automation", "set_automation_enabled",
    "test_automation", "run_automation",
    # Directory lookups are pure reads (usable in chat before any run).
    "find_user", "list_users",
    # Reading what's muted is a pure read.
    "list_suppressions",
    # Searching its OWN learning is a pure read — and it must work in chat
    # ("what have you learned about the cluster?") with no autonomous run in play.
    "search_learnings",
    # Consulting the document library is a pure read (usable in chat + autonomous).
    "search_knowledge", "list_knowledge_docs", "get_knowledge_doc",
}) | _OPERATIONS_READ_ONLY


# ── MCP protocol layer (JSON-RPC over streamable HTTP) ──────────────
#
# The OpenCode runtime in the sister agent pod consumes MCP servers
# declared in opencode.json with ``{"type": "remote", "url": ...}`` —
# that transport speaks MCP JSON-RPC (initialize / tools/list /
# tools/call) POSTed to the URL, NOT the bare REST endpoints above.
# Without this layer the agent literally has no app tools: the REST
# surface (`/mcp/invoke`, `/mcp/tools`) stays for the SPA and for
# server-side callers, while ``POST /mcp`` below is what the deployer
# registers in the agent pod's config (entry name ``app-tools``).
#
# Stateless by design: no session ids, every request self-contained.
# ``tools/call`` arguments accept an optional ``run_id`` (the executor
# prompt tells the agent its run id) so rows keep the audit triplet;
# ``step_idx`` is synthesised per call — idempotent replay across MCP
# retries is preserved by Composio-style at-most-once writes keyed on
# (run_id, step_idx, args) only within one call, which is the same
# guarantee the REST path gives a retrying agent.

_COMMON_PROPS: dict[str, Any] = {
    # v12.0.0 — SECURITY. In CHAT the run is named with `run_token` (random, it comes in the
    # [INTERNO] line); the integer `run_id` is only still valid for autonomous/channel runs,
    # which have no person present. A chat run named only by `run_id` is rejected: it was
    # guessable and with it one person acted as another. See `mcp_actor.resolver_run`.
    "run_token": {
        "type": "string",
        "description": (
            "Token of the run in progress. The prompt gives it to you in the "
            "[INTERNO] line as run_token=…. ALWAYS pass it, as is, on every "
            "call: it identifies the turn and the person you are serving."
        ),
    },
    "run_id": {
        "type": "integer",
        "description": (
            "ONLY in autonomous runs (no person): id of the run "
            "(run_id=N in the prompt). In a conversation use run_token."
        ),
    },
}


def _schema(required: list[str], props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {**props, **_COMMON_PROPS},
        "required": required,
    }


MCP_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "pedir_ubicacion": _schema(
        [],
        {
            "motivo": {
                "type": "string",
                "description": ("one short sentence saying WHAT you need it FOR, in the person's "
                                "language; it is shown to the person, so be specific and "
                                "honest (\"to look for restaurants nearby\")"),
            },
        },
    ),
    # v7.167.5 — `create_user` WRITES to the directory and fell into the generic schema: the
    # agent had to guess the field names by reading the docstring's prose. The schema is
    # what validates its call, so an undeclared field is a field that is silently lost.
    "create_user": _schema(
        [],
        {
            "email": {"type": "string",
                      "description": "the person's email; it is their identifier"},
            "phone": {"type": "string",
                      "description": "phone in E.164 (+34…). Use `phones` if they have several"},
            "phones": {"type": "array", "items": {"type": "string"},
                       "description": "several phones in E.164"},
            "display_name": {"type": "string", "description": "display name"},
            "alias": {"type": "string",
                      "description": "how they are usually called in the company"},
            "nicknames": {"type": "array", "items": {"type": "string"},
                          "description": "other ways of naming them, to recognize them"},
            "preferred_channel": {"type": "string",
                                  "enum": ["email", "whatsapp", "telegram"],
                                  "description": "how to reach them by default"},
        },
    ),
    "evolve_automation": _schema(
        ["id"],
        {"id": {"type": "integer", "description": "id of the automation to evolve"}},
    ),

    "upsert_alert": _schema(
        ["title", "description"],
        {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "impact": {"type": "string", "enum": list(IMPACT_BANDS),
                       "description": "magnitude of the consequence (default medium)"},
            "urgency": {"type": "string", "enum": list(URGENCY_BANDS),
                        "description": "how urgent it is (default medium)"},
            "confidence": {"type": "string", "enum": list(CONFIDENCE_BANDS),
                           "description": "how sure you are (default medium; low lowers the severity)"},
            "dedupe_key": {"type": "string",
                           "description": "stable id of the problem; if there is already an open alert with this key, it is UPDATED instead of duplicated"},
            "root_cause": {"type": "string", "description": "root cause (groups reappearances)"},
            "severity": {"type": "string", "enum": list(SEVERITIES),
                         "description": "optional: explicit OVERRIDE of the derived severity"},
            "source_event_id": {"type": "integer"},
            "target_entity_kind": {"type": "string"},
            "target_entity_id": {"type": "string"},
            "evidence": {"type": "object",
                         "description": "free dict with the evidence"},
            "allow_task": {"type": "boolean",
                           "description": "can the operator turn this into a TASK? By default NO (the button is hidden). Set it to true ONLY if it generates actionable work"},
            "task_title": {"type": "string",
                           "description": "title of the task to create if allow_task (it may differ from the alert; e.g. alert 'recurring non-payment' → task 'call the customer')"},
            "task_description": {"type": "string",
                                 "description": "what the employee must do in that task"},
        },
    ),
    "register_recommendation": _schema(
        ["title", "rationale"],
        {
            "title": {"type": "string"},
            "rationale": {"type": "string"},
            "impact": {"type": "string", "enum": list(IMPACT_BANDS),
                       "description": "size of the expected benefit (default medium)"},
            "urgency": {"type": "string", "enum": list(URGENCY_BANDS),
                        "description": "how urgent acting is (default medium)"},
            "confidence": {"type": "string", "enum": list(CONFIDENCE_BANDS),
                           "description": "how sure you are (default medium)"},
            "priority": {"type": "string", "enum": list(PRIORITIES),
                         "description": "optional: explicit OVERRIDE of the derived priority"},
            "target_entity_kind": {"type": "string"},
            "target_entity_id": {"type": "string"},
            "dedupe_key": {"type": "string",
                           "description": "STABLE id of the problem so as NOT to duplicate (same recurring suggestion ⇒ updates). If you omit it, it is derived from target_kind:target_id. Use a fixed slug (e.g. 'reposicion:product:21'), do NOT put figures that change between passes"},
            "root_cause": {"type": "string",
                           "description": "stable root cause; used as dedupe_key if you do not pass one"},
            "expected_impact": {"type": "string", "description": "free business headline (e.g. 'recovers 3 orders')"},
            "allow_task": {"type": "boolean",
                           "description": "can the operator turn this into a TASK? By default NO. Set it to true ONLY if it generates actionable work"},
            "task_title": {"type": "string", "description": "title of the task to create if allow_task (it may differ)"},
            "task_description": {"type": "string", "description": "what the employee must do in that task"},
            "metadata": {"type": "object"},
        },
    ),
    "log_communication": _schema(
        ["channel", "recipient", "body"],
        {
            "channel": {"type": "string", "enum": list(COMM_CHANNELS)},
            "recipient": {"type": "string"},
            "body": {"type": "string"},
            "subject": {"type": "string"},
            "status": {"type": "string", "enum": list(COMM_STATUSES),
                       "description": "draft=not sent; ready=the system "
                       "delivers it to the channel's recipients"},
            "attachments": {
                "type": "array",
                "description": "files to attach; each item "
                               "{filename, workspace_path|url|content_base64, "
                               "mime_type?}",
                "items": {"type": "object"},
            },
        },
    ),
    "upsert_scoring": _schema(
        ["target_kind", "target_id", "metric", "value"],
        {
            "target_kind": {"type": "string"},
            "target_id": {"type": "string"},
            "metric": {"type": "string"},
            "value": {"type": "number"},
            "window_days": {"type": "integer", "description": "default 30"},
            "band": {"type": "string"},
            "explanation": {"type": "string"},
            "metadata": {"type": "object"},
        },
    ),
    "log_decision": _schema(
        ["decision", "reason"],
        {
            "decision": {"type": "string", "enum": list(DECISIONS),
                         "description": "coarse disposition"},
            "reason": {"type": "string"},
            "reason_code": {"type": "string", "enum": list(REASON_CODE_CATALOG.keys()),
                            "description": "structured reason (if omitted, it is inferred from decision)"},
            "confidence": {"type": "string", "enum": list(CONFIDENCE_BANDS),
                           "description": "how sure you are (default medium)"},
            "ref_kind": {"type": "string",
                         "description": "type of the evaluated finding / avoided action (e.g. 'alerts')"},
            "ref_id": {"type": "string", "description": "id/label of the evaluated finding"},
            "allow_task": {"type": "boolean",
                           "description": "can the operator turn this into a TASK? By default NO. Set it to true ONLY if it generates actionable work"},
            "task_title": {"type": "string", "description": "title of the task to create if allow_task"},
            "task_description": {"type": "string", "description": "what the employee must do in that task"},
            "source_event_id": {"type": "integer"},
            "metadata": {"type": "object"},
        },
    ),
    "resolve_alert": _schema(
        ["alert_id", "status"],
        {
            "alert_id": {"type": "integer"},
            "status": {"type": "string", "enum": list(ALERT_STATUSES),
                       "description": "acknowledged | in_progress | done | "
                                      "dismissed | open (reopen)"},
            "resolved_by": {"type": "string",
                            "description": "who closes it; default agent"},
        },
    ),
    "suppress_signal": _schema(
        ["signal_kind"],
        {
            "signal_kind": {"type": "string", "enum": list(_SUPPRESS_KINDS),
                            "description": "alert | recommendation | decision"},
            "signal_id": {"type": "integer",
                          "description": "id of the row you have just handled (preferred)"},
            "dedupe_key": {"type": "string",
                           "description": "alternative to signal_id: the signal's stable key"},
            "days": {"type": "integer",
                     "description": "duration of the silence in days (default 14, max 30). "
                                    "Permanent only from the app by a human"},
            "reason": {"type": "string", "description": "why it must not repeat"},
        },
    ),
    "list_suppressions": _schema(
        [],
        {
            "signal_kind": {"type": "string", "enum": list(_SUPPRESS_KINDS),
                            "description": "optional: filter by type"},
        },
    ),
    "decide_recommendation": _schema(
        ["recommendation_id", "status"],
        {
            "recommendation_id": {"type": "integer"},
            "status": {"type": "string", "enum": list(RECOMMENDATION_STATUSES),
                       "description": "acknowledged | in_progress | done | "
                                      "dismissed | open (reopen)"},
            "decided_by": {"type": "string",
                           "description": "who decides; default agent"},
        },
    ),
    "decide_decision": _schema(
        ["decision_id", "status"],
        {
            "decision_id": {"type": "integer"},
            "status": {"type": "string", "enum": list(DECISION_STATUSES),
                       "description": "acknowledged | in_progress | done | "
                                      "dismissed | open (reopen)"},
        },
    ),
    "request_knowledge": _schema(
        ["key", "label"],
        {
            "key": {"type": "string",
                    "description": "STABLE slug to re-read the value (e.g. umbral_factura_alto_riesgo)"},
            "label": {"type": "string", "description": "readable title for the operator (in SPANISH)"},
            "question": {"type": "string", "description": "what you need and why, in SPANISH (we show it to the operator)"},
            "label_en": {"type": "string", "description": "the same title translated into ENGLISH (always pass it)"},
            "question_en": {"type": "string", "description": "the same question translated into ENGLISH (always pass it)"},
            "value_type": {"type": "string", "enum": list(_KNOWLEDGE_VALUE_TYPES),
                           "description": "default text; use 'list' when the answer is one or SEVERAL values"},
            "unit": {"type": "string", "description": "unit: €, days, %…"},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "allowed/suggested values when value_type=list (the operator can pick several or type others)"},
            "suggested": {"type": "string",
                          "description": "value you will use while the operator does not answer (for list, separate them with commas)"},
        },
    ),
    "set_knowledge": _schema(
        ["key", "value"],
        {
            "key": {"type": "string", "description": "slug of the parameter to change"},
            "value": {"type": "string",
                      "description": "new value; only a human admin applies it, otherwise it stays as a proposal"},
        },
    ),
    "search_knowledge": _schema(
        ["query"],
        {
            "query": {"type": "string",
                      "description": "what you need, in natural language (price, condition, requirement…)"},
            "limit": {"type": "integer",
                      "description": "maximum number of fragments (default config; max 20)"},
        },
    ),
    "list_knowledge_docs": _schema(
        [],
        {
            "q": {"type": "string",
                  "description": "optional: filter by title/tag (substring)"},
        },
    ),
    "get_knowledge_doc": _schema(
        ["document_id"],
        {
            "document_id": {"type": "integer",
                            "description": "id of the document (from search_knowledge / list_knowledge_docs)"},
            "query": {"type": "string",
                      "description": "optional: sort the document's fragments by relevance to this"},
            "limit": {"type": "integer",
                      "description": "number of fragments to return (default 6; max 20)"},
            "user_email": {"type": "string",
                           "description": "email of the person you are serving (for documents scoped to a person / when attending)"},
        },
    ),
    "local.write": _schema(
        ["table", "values"],
        {
            "table": {"type": "string"},
            "values": {"type": "object"},
            "pk": {"type": "string", "description": "default id"},
        },
    ),
    "schedule_task": _schema(
        ["title", "due_at"],
        {
            "title": {"type": "string"},
            "due_at": {"type": "string",
                       "description": "ISO-8601; wall-clock time in the "
                                      "business time zone of the prompt"},
            "description": {"type": "string"},
            "kind": {"type": "string", "enum": ["task", "followup"],
                     "description": "default task"},
            "priority": {"type": "string", "enum": list(PRIORITIES),
                         "description": "default medium"},
        },
    ),
    "assign_task_to_user": _schema(
        ["title", "assigned_to", "due_at"],
        {
            "title": {"type": "string"},
            "assigned_to": {"type": "string", "description": "name/alias/email/phone of the person (resolved in the directory)"},
            "due_at": {"type": "string", "description": "ISO-8601 (business time zone)"},
            "description": {"type": "string"},
            "kind": {"type": "string", "enum": ["task", "reminder"],
                     "description": "default reminder"},
            "priority": {"type": "string", "enum": list(PRIORITIES),
                         "description": "default medium"},
        },
    ),
    "complete_scheduled_item": _schema(
        ["item_id"],
        {
            "item_id": {"type": "integer"},
            "result": {"type": "string", "description": "note of what was done"},
            "status": {"type": "string", "enum": ["done", "cancelled"],
                       "description": "default done"},
        },
    ),
    "request_approval": _schema(
        ["title", "action_type", "summary"],
        {
            "title": {"type": "string"},
            "action_type": {"type": "string",
                            "description": "short label, e.g. send_email"},
            "summary": {"type": "string",
                        "description": "what you want to do and why (for the operator, in their language)"},
            "action_payload": {"type": "object",
                               "description": "everything needed to execute it later"},
            "risk": {"type": "string", "enum": list(APPROVAL_RISKS),
                     "description": "default medium"},
            "requested_for": {"type": "string", "description": "affected email"},
        },
    ),
    # WITHOUT this explicit schema mark_approval_executed fell back to the
    # run_id-only default, so the agent could never pass ``approval_id`` and
    # the approval close-loop failed ("missing required argument") — leaving
    # every approval stuck in ``approved`` forever.
    "mark_approval_executed": _schema(
        ["approval_id"],
        {
            "approval_id": {"type": "integer",
                            "description": "id of the already granted approval you have just executed"},
            "result": {"type": "string",
                       "description": "short note of what you did (optional)"},
        },
    ),
    "list_alerts": _schema(
        [],
        {
            "status": {"type": "string",
                       "description": "open (default) | acknowledged | in_progress | done | dismissed | all"},
            "severity": {"type": "string", "enum": list(SEVERITIES)},
            "impact": {"type": "string", "enum": list(IMPACT_BANDS)},
            "urgency": {"type": "string", "enum": list(URGENCY_BANDS)},
            "dedupe_key": {"type": "string", "description": "to see reappearances of the same problem"},
            "target_entity_id": {"type": "string"},
            "q": {"type": "string", "description": "substring of the title"},
            "limit": {"type": "integer", "description": "default 20, max 50"},
        },
    ),
    "list_recommendations": _schema(
        [],
        {
            "status": {"type": "string",
                       "description": "open (default) | acknowledged | in_progress | done | dismissed | all"},
            "priority": {"type": "string", "enum": list(PRIORITIES)},
            "impact": {"type": "string", "enum": list(IMPACT_BANDS)},
            "urgency": {"type": "string", "enum": list(URGENCY_BANDS)},
            "target_entity_id": {"type": "string"},
            "q": {"type": "string", "description": "substring of the title"},
            "limit": {"type": "integer", "description": "default 20, max 50"},
        },
    ),
    "list_decisions": _schema(
        [],
        {
            "decision": {"type": "string", "enum": list(DECISIONS)},
            "reason_code": {"type": "string", "enum": list(REASON_CODE_CATALOG.keys())},
            "status": {"type": "string",
                       "description": "all (default) | open | acknowledged | in_progress | done | dismissed"},
            "source_event_id": {"type": "integer"},
            "q": {"type": "string", "description": "substring of the reason"},
            "limit": {"type": "integer", "description": "default 20, max 50"},
        },
    ),
    "list_scheduled_items": _schema(
        [],
        {
            "status": {"type": "string",
                       "description": "active (default) | pending | scheduled | "
                                      "in_progress | done | cancelled | overdue | all"},
            "assignee": {"type": "string",
                         "description": "agent | user | all (default)"},
            "kind": {"type": "string",
                     "description": "task | followup | reminder | event"},
            "when": {"type": "string",
                     "description": "upcoming | past | all (default), by due_at"},
            "q": {"type": "string", "description": "substring of the title"},
            "limit": {"type": "integer", "description": "default 20, max 50"},
        },
    ),
    "list_communications": _schema(
        [],
        {
            "status": {"type": "string",
                       "description": "all (default) | draft | ready | sent | failed"},
            "channel": {"type": "string", "enum": list(COMM_CHANNELS)},
            "recipient": {"type": "string"},
            "q": {"type": "string", "description": "substring of subject/body"},
            "limit": {"type": "integer", "description": "default 20, max 50"},
        },
    ),
    "list_scoring": _schema(
        [],
        {
            "metric": {"type": "string"},
            "target_kind": {"type": "string"},
            "target_id": {"type": "string"},
            "band": {"type": "string"},
            "q": {"type": "string", "description": "substring of the explanation"},
            "limit": {"type": "integer", "description": "default 20, max 50"},
        },
    ),
    "list_approvals": _schema(
        [],
        {
            "status": {"type": "string",
                       "description": "pending (default) | approved | rejected | "
                                      "executed | all"},
            "action_type": {"type": "string"},
            "risk": {"type": "string", "enum": list(APPROVAL_RISKS)},
            "requested_for": {"type": "string", "description": "affected email"},
            "q": {"type": "string", "description": "substring of the title"},
            "limit": {"type": "integer", "description": "default 20, max 50"},
        },
    ),
    "local.read": _schema(
        ["table"],
        {
            "table": {"type": "string"},
            "where": {"type": "object",
                      "description": "equality filters column:value"},
            "order_by": {"type": "string",
                         "description": "column; prefix - for descending"},
            "limit": {"type": "integer", "description": "default 20, max 50"},
        },
    ),
    "describe_entities": _schema([], {}),
    "remember_fact": _schema(
        ["user_email", "content"],
        {
            "user_email": {"type": "string",
                           "description": "the person's address (memory key)"},
            "content": {"type": "string"},
            "kind": {"type": "string", "enum": list(MEMORY_KINDS),
                     "description": "fact (default) | preference | procedure"},
            "tags": {"type": "string", "description": "comma-separated tags"},
            "pinned": {"type": "boolean",
                       "description": "protected from automatic replacement and always first"},
            "confidence": {"type": "number"},
            "importance": {"type": "number",
                           "description": "0..1 relevance of the fact (optional; there is a heuristic floor)"},
            "supersedes": {"type": "integer",
                           "description": "id of the previous fact this one corrects"},
            "artifacts": {
                "type": "array",
                "description": (
                    "workspace files this fact references, so it is "
                    "self-contained: [{\"filename\":..., "
                    "\"workspace_path\":\"sessions/.../exports/x.pdf\"}]"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "workspace_path": {"type": "string"},
                    },
                },
            },
        },
    ),
    "record_episode": _schema(
        ["user_email", "summary"],
        {
            "user_email": {"type": "string"},
            "summary": {"type": "string", "description": "one single sentence"},
            "source": {"type": "string", "enum": ["chat", "email"]},
            "source_ref": {"type": "string"},
            "outcome": {"type": "string"},
            "artifacts": {
                "type": "array",
                "description": "workspace files this episode references",
                "items": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "workspace_path": {"type": "string"},
                    },
                },
            },
        },
    ),
    "index_file": _schema(
        ["user_email", "filename", "workspace_path"],
        {
            "user_email": {"type": "string"},
            "filename": {"type": "string"},
            "workspace_path": {"type": "string",
                               "description": "relative to /workspace"},
            "mime": {"type": "string"},
            "size": {"type": "integer"},
            "description": {"type": "string",
                            "description": "WHAT it contains (key data) and "
                                           "WHEN to use it — it is what the "
                                           "memory searches later; if the "
                                           "file is already indexed it is "
                                           "updated (upsert)"},
            "source": {"type": "string",
                       "description": "email | chat | agent (default agent)"},
        },
    ),
    "list_memory": _schema(
        ["user_email"],
        {
            "user_email": {"type": "string"},
            "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
            "limit": {"type": "integer", "description": "default 30, max 50"},
        },
    ),
    "search_memory": _schema(
        ["user_email", "query"],
        {
            "user_email": {"type": "string"},
            "query": {"type": "string"},
            "limit": {"type": "integer", "description": "default 20, max 50"},
        },
    ),
    "forget_fact": _schema(
        ["user_email"],
        {
            "user_email": {"type": "string"},
            "fact_id": {"type": "integer", "description": "exact id to delete"},
            "content": {"type": "string",
                        "description": "deletes facts whose content matches (exactly)"},
        },
    ),
    "compact_memory": _schema(
        ["user_email"],
        {
            "user_email": {"type": "string"},
        },
    ),
    # ── OWN memory (learning about the environment) ─────────────────────
    # Without ``user_email``: the identity here is the ``subject``, not a person —
    # that is why they work in an autonomous run, where people's memory does not.
    "remember_learning": _schema(
        ["subject", "content"],
        {
            "subject": {"type": "string",
                        "description": ("canonical identity of what was observed: "
                                        "job:indexing, deployment:wai-engine-api, "
                                        "tenant:pabisan. Repeating the same subject "
                                        "with the same content REINFORCES the "
                                        "evidence instead of duplicating it")},
            "content": {"type": "string", "description": "one atomic sentence"},
            "kind": {"type": "string", "enum": list(AGENT_MEMORY_KINDS),
                     "description": ("observation (default) = a data point from today | "
                                     "baseline = what is normal here | lesson = a "
                                     "conclusion that changes how you judge | "
                                     "procedure = a way of reviewing that "
                                     "worked | entity = a piece of the environment")},
            "valid_until": {"type": "string",
                            "description": ("days (\"30\") or an ISO date. Set it on "
                                            "a baseline: the environment changes and "
                                            "what has expired stops being injected")},
            "confidence": {"type": "number"},
            "importance": {"type": "number"},
            "tags": {"type": "string"},
        },
    ),
    "search_learnings": _schema(
        ["query"],
        {
            "query": {"type": "string"},
            "subject": {"type": "string", "description": "narrow to one subject"},
            "kind": {"type": "string", "enum": list(AGENT_MEMORY_KINDS)},
            "limit": {"type": "integer", "description": "default 20, max 50"},
        },
    ),
    "forget_learning": _schema(
        ["learning_id"],
        {
            "learning_id": {"type": "integer"},
        },
    ),
    "compact_learnings": _schema([], {}),
    "propose_silence": _schema(
        ["signal_kind"],
        {
            "signal_kind": {"type": "string", "enum": list(_SUPPRESS_KINDS)},
            "signal_id": {"type": "integer",
                          "description": "the specific signal you have just reviewed"},
            "dedupe_key": {"type": "string",
                           "description": "stable identity of the signal"},
            "subject_pattern": {"type": "string",
                                "description": ("scope by pattern instead of "
                                                "exact identity, e.g. "
                                                "*tenant-x*")},
            "learning_id": {"type": "integer",
                            "description": ("the learning that justifies it — "
                                            "pass it: it is what makes the rule "
                                            "auditable and revertible")},
            "reason": {"type": "string",
                       "description": "in business language, for the manager (in their language)"},
            "condition": {"type": "object",
                          "description": ("the \"only if\": "
                                          "{\"kind\":\"weekday\",\"days\":[6,7]} | "
                                          "{\"kind\":\"time_window\",\"from\":\"01:00\","
                                          "\"to\":\"05:30\"} | {\"kind\":\"field\","
                                          "\"field\":\"target_entity_id\",\"op\":\"eq\","
                                          "\"value\":\"x\"} | {\"kind\":\"all\",\"of\":[…]}. "
                                          "Add \"effect\":\"defer\" with "
                                          "\"defer_minutes\" to WAIT AND "
                                          "RE-EVALUATE instead of going quiet")},
            "severity_max": {"type": "string",
                             "description": ("do not silence above this "
                                             "severity (a critical signal is never "
                                             "silenced)")},
            "expires_days": {"type": "integer",
                             "description": "expiry of the rule in days"},
        },
    ),
    # ── Automations (conversational management, boss chat only) ───────
    # WITHOUT these explicit schemas these tools fell back to the run_id-only
    # default schema, so the agent could only pass ``run_id`` and never the
    # automation definition — "create" always failed with "title is required".
    "get_automation_catalog": _schema([], {}),
    "list_automations": _schema([], {}),
    "create_automation": _schema(
        ["title"],
        {
            "title": {"type": "string", "description": "title of the order"},
            "signal_type": {
                "type": "string",
                "enum": ["schedule", "channel_message", "business_event", "manual"],
                "description": "WHEN it fires. 'schedule' = at a time (cron).",
            },
            "signal_spec": {
                "type": "object",
                "description": (
                    "signal config per signal_type: schedule → {\"cron\": \"30 9 * * *\"} "
                    "(9:30 every day) — the cron is interpreted in the employee's TIME ZONE; "
                    "for another zone add {\"tz\": \"Europe/Madrid\"} (IANA name); "
                    "channel_message → {\"channel_id\": <channel id>} (empty = any channel); "
                    "business_event → {\"event_type\": \"<event type>\"}."
                ),
            },
            "signal_summary": {"type": "string", "description": "the when in business language"},
            "condition_spec": {
                "type": "object",
                "description": ("IF (2nd verification). COST FIRST — use the CHEAPEST: "
                    "{\"mode\":\"checks\",\"combine\":\"all|any\",\"on_error\":\"alert\","
                    "\"checks\":[{\"name\":..,\"connector\":\"<mcp>\",\"tool\":..,\"arguments\":{..},"
                    "\"extract\":\"count\",\"op\":\"gte\",\"threshold\":1}]} = calls MCP tools in "
                    "API mode, 0 tokens; {\"mode\":\"data|sql\",..} 0 tokens; {\"mode\":\"agent\","
                    "\"instructions\":..} EXPENSIVE (1 run per check, last resort). Any "
                    "mode admits \"pre\":{..checks/data/sql..} as a cheap prior guard."),
            },
            "reaction_type": {
                "type": "string",
                # The 4 canonical outcomes. draft_and_approve is a tolerated LEGACY
                # alias (migrated to act + requires_approval) — do NOT author with it.
                "enum": ["notify", "create_task", "act", "generate_deliverable", "draft_and_approve"],
                "description": "THEN (the WHAT): notify=inform · create_task=create a task · act=execute · generate_deliverable=file. Approval is separate (requires_approval).",
            },
            "execution_mode": {
                "type": "string",
                "enum": ["deterministic", "agent"],
                "description": "COST of the reaction: 'deterministic' = direct template, 0 tokens (PREFER IT for templated create_task/notify); 'agent' = 1 run (act/generate_deliverable or if it has to be written).",
            },
            "reaction_spec": {
                "type": "object",
                "description": "reaction config; notify → {\"channel_id\":..., \"recipient\":...}; create_task → {\"title\":..,\"description\":..,\"assignee\":\"agent|user\"}",
            },
            "reaction_instructions": {"type": "string", "description": "what it must do. Write it in MARKDOWN (## headings, lists, numbered steps), structured like a skill — plain text is neither understood nor displayed well."},
            "requires_knowledge": {"type": "string", "description": "optional: tags of the REFERENCE MATERIAL this order needs (comma-separated, e.g. \"rates, returns policy\"). When it fires, you will be reminded to consult search_knowledge for those topics."},
            "requires_approval": {"type": "boolean", "description": "whether the action needs approval (this IS the old 'draft & approve' = act+approval)"},
            "enabled": {"type": "boolean", "description": "default false: it is born DISABLED"},
            "cooldown_sec": {"type": "integer", "description": "anti-spam pause between fires"},
            "serves_user_email": {"type": "string"},
        },
    ),
    "update_automation": _schema(
        ["id"],
        {
            "id": {"type": "integer", "description": "id of the automation to change"},
            "title": {"type": "string"},
            "signal_type": {
                "type": "string",
                "enum": ["schedule", "channel_message", "business_event", "manual"],
            },
            "signal_spec": {"type": "object", "description": "schedule cron in the employee's time zone; {\"tz\": \"IANA\"} for another zone"},
            "signal_summary": {"type": "string"},
            "condition_spec": {"type": "object", "description": "cost first: checks/data/sql = 0 tokens; agent = 1 run per check (last resort). Admits a cheap 'pre' guard."},
            "reaction_type": {
                "type": "string",
                "enum": ["notify", "create_task", "act", "generate_deliverable", "draft_and_approve"],
            },
            "execution_mode": {"type": "string", "enum": ["deterministic", "agent"],
                               "description": "deterministic = 0 tokens (templated create_task/notify); agent = 1 run"},
            "reaction_spec": {"type": "object"},
            "reaction_instructions": {"type": "string", "description": "in MARKDOWN (headings, lists, steps), like a skill"},
            "requires_knowledge": {"type": "string", "description": "tags of the reference material it needs (commas); empty removes it"},
            "requires_approval": {"type": "boolean"},
            "enabled": {"type": "boolean"},
            "cooldown_sec": {"type": "integer"},
        },
    ),
    "set_automation_enabled": _schema(
        ["id", "enabled"],
        {
            "id": {"type": "integer"},
            "enabled": {"type": "boolean", "description": "true = enable, false = pause"},
        },
    ),
    "test_automation": _schema(
        ["id"],
        {"id": {"type": "integer", "description": "id to test (dry-run of the condition)"}},
    ),
    "run_automation": _schema(
        ["id"],
        {"id": {"type": "integer", "description": "id of the automation to run now (launches the reaction)"}},
    ),
    # v7.146.0 — parity with the UI: the schemas live next to their handlers in
    # ``mcp_operations`` (a new tool is added in ONE file, not in three).
    **{
        name: _schema(_OPERATIONS_REQUIRED.get(name, []), props)
        for name, props in _OPERATIONS_SCHEMAS.items()
    },
}


def _mcp_tool_descriptors() -> list[dict[str, Any]]:
    """Build MCP tool list; schemas are localized at call time."""
    return _mcp_tool_descriptors_sync("es")


# The catalog is announced ONCE when opening the session, when there is still no run and
# therefore it is not known who will ask. It cannot be filtered by person, but it CAN say
# what each tool requires: that way the employee warns BEFORE ("an administrator sees
# that") instead of trying it, getting a rejection and improvising the excuse.
_LEVEL_NOTE = {
    "human": "\n\n[PERMISSION] Only when a person identified in the chat asks you.",
    "approver": ("\n\n[PERMISSION] Only an administrator or someone designated to "
                 "approve. If they are not, tell them whom to ask (list_approvers)."),
    "admin": ("\n\n[PERMISSION] Only an ADMINISTRATOR of the employee, from the chat. If "
              "whoever asks is not one, do not try it: explain it and tell them whom "
              "to ask (list_approvers)."),
}


def _mcp_tool_descriptors_sync(lang: str) -> list[dict[str, Any]]:
    out = []
    for name, fn in TOOLS.items():
        base = MCP_TOOL_SCHEMAS.get(
            name, {"type": "object", "properties": dict(_COMMON_PROPS)},
        )
        doc = (fn.__doc__ or "").strip()
        out.append({
            "name": name,
            "description": doc + _LEVEL_NOTE.get(TOOL_MIN_ROLE.get(name, ""), ""),
            "inputSchema": localize_tool_schema(name, base, lang),
        })
    return out


async def _mcp_tool_descriptors_for_locale() -> list[dict[str, Any]]:
    lang = await resolve_agent_language()
    propias = _mcp_tool_descriptors_sync(lang)
    # Colleagues are NO longer announced here. Each callable employee is an MCP server of
    # the catalog (`empleado-<slug>`, served by the platform) attached to the agent's
    # version like any other connector. Announcing them from `app-tools` too was having TWO
    # paths for the same thing, and the one here had a defect that cannot be fixed: the list
    # was dynamic, but opencode fixes a session's tools when creating it — so a colleague
    # granted later stayed invisible without saying so. Being an attached server, the set
    # only changes when re-attaching, which is a new version and a redeployment.
    return propias


async def _resolve_run_id(requested: int) -> Optional[int]:
    """Exact ``run_id`` only — mutating tools must not fall back to another run."""
    if requested <= 0:
        return None
    from sqlalchemy import select
    from app.app_models.runtime import AgentRun

    async with app_session() as session:
        found = (await session.execute(
            select(AgentRun.id).where(AgentRun.id == requested)
        )).scalar_one_or_none()
        return int(found) if found else None


def _rpc_result(req_id: Any, result: dict[str, Any]):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


async def _mcp_dispatch(msg: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Handle one JSON-RPC message. Returns None for notifications."""
    method = msg.get("method") or ""
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        return _rpc_result(req_id, {
            # Echo the client's protocol version — this server is
            # stateless and method-compatible across revisions.
            "protocolVersion": params.get("protocolVersion") or "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "app-tools", "version": "1.0.0"},
        })
    if method == "ping":
        return _rpc_result(req_id, {})
    if method == "tools/list":
        return _rpc_result(req_id, {"tools": await _mcp_tool_descriptors_for_locale()})
    if method == "tools/call":
        name = params.get("name") or ""
        arguments = dict(params.get("arguments") or {})
        # Colleagues are resolved BEFORE looking at TOOLS: they are not this employee's tools,
        # they are another employee. Their permission gate is the relation (platform) plus
        # the callee's directory — not `TOOL_MIN_ROLE`, which governs what is OURS.
        handler = TOOLS.get(name)
        if handler is None:
            return _rpc_error(req_id, -32602, f"unknown tool {name!r}")
        # v12.0.0 — SECURITY: from "what the model said" to "which run it is" through the SINGLE
        # gate `resolver_run`: `run_token` (chat, unguessable) wins; a bare `run_id` is only
        # valid for autonomous runs. A chat run named by its integer is rejected (it was the
        # escalation). Both are REMOVED from the arguments: they are not the tool's parameters.
        run_token_raw = arguments.pop("run_token", None)
        run_id_raw = arguments.pop("run_id", 0)
        from app.mcp_actor import resolver_run
        run_id = (await resolver_run(run_token_raw, run_id_raw)) or 0
        if name in MEMORY_TOOLS:
            run_id = await _resolve_run_id(run_id)
            if run_id is None:
                return _rpc_result(req_id, {
                    "content": [{"type": "text", "text": json.dumps({
                        "ok": False,
                        "error": (
                            "memory tools require a valid run_token from the "
                            "[INTERNO] line — pass run_token on every call"
                        ),
                    })}],
                    "isError": True,
                })
        elif name in READ_TOOLS:
            # Reads need no audit row and must work even on an empty
            # runs table — don't gate them on run_id resolution.
            run_id = run_id or 0
        elif name in SELF_MEMORY_TOOLS:
            # SELF-memory: needs a real run (for provenance and the audit
            # triplet) but NO owner bind. This branch is explicit rather than
            # falling into the generic one below, because routing these through
            # ``MEMORY_TOOLS`` would make them unusable exactly where they
            # matter: an autonomous run has no human memory owner, which is why
            # the watcher is memoryless without this layer.
            run_id = await _resolve_run_id(run_id)
            if run_id is None:
                return _rpc_result(req_id, {
                    "content": [{"type": "text", "text": json.dumps({
                        "ok": False,
                        "error": (
                            "self-memory tools require a valid run_token from the "
                            "[INTERNO] line — pass run_token on every call"
                        ),
                    })}],
                    "isError": True,
                })
        else:
            run_id = await _resolve_run_id(run_id)
            if run_id is None:
                return _rpc_result(req_id, {
                    "content": [{"type": "text", "text": json.dumps({
                        "ok": False,
                        "error": (
                            "mutating tools require a valid run_token from "
                            "the [INTERNO] line — pass run_token on every call"
                        ),
                    })}],
                    "isError": True,
                })
        # ── SINGLE permission gate (v7.146.0) ────────────────────────
        #
        # Here, in the dispatch, and not inside each handler: a new tool cannot be born open
        # by carelessness, because what is not declared in TOOL_MIN_ROLE is denied. The
        # identity does NOT come from the model nor from the pod's token (it is a service
        # one): it is derived from the run.
        denied = await gate_error(name, await resolve_actor(run_id))
        if denied is not None:
            return _rpc_result(req_id, {
                "content": [{"type": "text", "text": json.dumps(denied, default=str)}],
                "isError": True,
            })
        call = ToolCall(
            run_id=run_id,
            # DETERMINISTIC over (run_id, name, args) — NOT a wall-clock
            # stamp. The agent doesn't pass a stable step, and a time-based
            # value gave every retry a fresh key, so the idempotency layer
            # never replayed and a re-sent ``tools/call`` (lost response,
            # pod blip) re-executed → duplicate alerts / double-sent email.
            # Content-derived means an identical retry collides on the
            # ``tool_call_log`` UNIQUE key and replays the stored result;
            # the atomic INSERT…ON CONFLICT barrier in _replay_or_execute
            # still serialises genuinely-concurrent identical calls. (A
            # deliberate same-args call twice in one run is treated as a
            # retry — acceptable: for mutating tools identical args ≈ a dup.)
            step_idx=_content_step_idx(run_id, name, arguments),
            name=name,
            arguments=arguments,
            mode="autonomous",
        )
        try:
            result = await handler(call)
        except HTTPException as exc:
            return _rpc_result(req_id, {
                "content": [{"type": "text", "text": json.dumps(
                    {"ok": False, "error": exc.detail}, default=str,
                )}],
                "isError": True,
            })
        except Exception as exc:  # pragma: no cover — defensive
            return _rpc_result(req_id, {
                "content": [{"type": "text", "text": json.dumps(
                    {"ok": False, "error": str(exc)}, default=str,
                )}],
                "isError": True,
            })
        return _rpc_result(req_id, {
            "content": [{"type": "text",
                         "text": json.dumps(result, default=str)}],
            "isError": False,
        })
    return _rpc_error(req_id, -32601, f"method not found: {method}")


@router.post("")
async def mcp_jsonrpc(request: Request, authorization: str = Header(default="")):
    """MCP streamable-HTTP endpoint (``POST /mcp``)."""
    from fastapi.responses import JSONResponse, Response

    _check_token(authorization)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            _rpc_error(None, -32700, "parse error"), status_code=400,
        )
    if isinstance(body, list):  # JSON-RPC batch
        responses = [r for r in [await _mcp_dispatch(m) for m in body] if r]
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses)
    resp = await _mcp_dispatch(body)
    if resp is None:  # notification — acknowledge without a body
        return Response(status_code=202)
    return JSONResponse(resp)


@router.get("")
async def mcp_get_stream():
    """Streamable-HTTP clients may probe GET for a server event stream;
    this server is stateless and has none — 405 tells the client to
    keep using plain POST responses."""
    from fastapi.responses import Response
    return Response(status_code=405)


def mount_mcp(app) -> None:
    """Wire the local MCP router under `/mcp`.

    Called from `main.py` during FastAPI lifespan setup. Exposes BOTH
    surfaces: the MCP JSON-RPC protocol at ``POST /mcp`` (consumed by
    the OpenCode runtime via the ``app-tools`` entry the deployer
    registers in the agent pod's opencode.json) and the original REST
    endpoints (``POST /mcp/invoke``, ``GET /mcp/tools``) used by the
    SPA and server-side callers.
    """
    app.include_router(router)
