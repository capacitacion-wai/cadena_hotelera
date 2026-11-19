"""FastAPI entrypoint for the agent-app.

Composition:
* lifespan
  - create_all on the LOCAL SQLite (project Postgres is read-only and
    its schema is owned by the platform, not us).
  - recover stale runs (CLAIMED/IN_PROGRESS → PENDING) so a hard
    restart never strands work.
  - construct the singleton AgentProxy and stash it in app.state.
  - launch the watcher background task (emits Events) AND the executor
    background task (claims PENDING events and dispatches them to the
    sister agent pod). Without BOTH loops events stay PENDING forever.
* mounts the API router and the compiled React bundle.

Do NOT inline anything else here. Routers register themselves; new
features go in their own module under ``routes/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import sqlalchemy
from sqlalchemy import text

from app import app_models  # noqa: F401  side-effect: registers SQLAlchemy models
from app.agent_proxy import AgentProxy, set_agent_proxy
from app.app_database import AppBase, WriteBusy, app_engine
from app.autonomous.executor import run_forever as executor_run_forever
from app.autonomous.recovery import recover_stale_runs
from app.autonomous.watcher import run_forever as watcher_run_forever
from app.channels.poller import run_forever as channels_run_forever
from app.mcp_server import mount_mcp
from app.routes.agent_memory_routes import router as agent_memory_router
from app.routes.agent_routes import router as agent_router
from app.routes.chat_threads_routes import router as chat_threads_router
from app.routes.voz_routes import router as voz_router
from app.routes.approvals_routes import router as approvals_router
from app.routes.auth_routes import router as auth_router
from app.routes.channels_routes import router as channels_router
from app.routes.colleague_routes import router as colleague_router
from app.routes.channel_media_routes import router as channel_media_router
from app.routes.dashboard_routes import router as dashboard_router
from app.routes.data_routes import router as data_router
from app.routes.domain_routes import router as domain_router
from app.routes.events_routes import router as events_router
from app.routes.health_routes import router as health_router
from app.routes.knowledge_routes import router as knowledge_router
from app.routes.knowledge_docs_routes import router as knowledge_docs_router
from app.routes.automations_routes import router as automations_router
from app.routes.notify_rules_routes import router as notify_rules_router
from app.routes.suppressions_routes import router as suppressions_router
from app.routes.comms_feed_routes import router as comms_feed_router
from app.routes.capabilities_routes import router as capabilities_router
from app.routes.activity_routes import router as activity_router
from app.routes.manifest_routes import router as manifest_router
from app.routes.mcp_proxy_routes import router as mcp_proxy_router
from app.routes.mcp_connectors_routes import router as mcp_connectors_router
from app.routes.memory_routes import router as memory_router
from app.routes.runs_routes import router as runs_router
from app.routes.scheduling_routes import router as scheduling_router
from app.routes.workspace_routes import router as workspace_router
from app.routes.users_routes import router as users_router
from app.routes.devices_routes import router as devices_router

from app.logging_setup import setup_logging  # Wave 3: the employee's structured JSON logging
setup_logging()
logger = logging.getLogger(__name__)

# Monotonic skill schema generation — bumped on each release that adds
# additive columns/indexes via ``_ensure_columns`` / ``_ensure_indexes``.
SCHEMA_VERSION = 654

STATIC_DIR = Path("/app/static")


async def _ensure_columns(conn) -> None:
    """IS5: lightweight schema migration runner.

    The skill uses ``Base.metadata.create_all`` which only CREATEs new
    tables — it does NOT add columns to existing tables. Running an
    upgraded image against an existing PVC would otherwise crash on
    the first SELECT touching a missing column. We add columns
    idempotently with SQLite's ``ADD COLUMN IF NOT EXISTS`` (≥3.35.0,
    shipped in the canonical Dockerfile). Only NULLABLE additions
    are safe here; if you need a new NOT NULL column, ship a real
    migration runner.
    """
    if conn.dialect.name != "sqlite":
        return  # PostgreSQL deploys are out of scope for this runner.
    pending = (
        ("events",     "trace_id",  "TEXT"),
        ("agent_runs", "trace_id",  "TEXT"),
        # v9.1.0 — which THREAD the turn belongs to. Without this, an existing PVC would blow
        # up on the first SELECT of the runs listing. NULL in earlier rows, which read as
        # "that person's default thread" — and it is correct: until now there was only one
        # conversation per person.
        ("agent_runs", "thread_id", "TEXT"),
        ("agent_runs", "attempts",  "INTEGER NOT NULL DEFAULT 1"),
        # v12.0.0 — RANDOM identifier of the run for the boundary with the model (see
        # `AgentRun.run_token`). NULL in earlier runs: tokens are only minted for new ones,
        # and the old ones are no longer in progress.
        ("agent_runs", "run_token", "TEXT"),
        # v12.0.0 — the default conversation is a REAL row per person, marked like this
        # (see `ChatThread.is_default`). 0 on everything existing; the backfill marks the right one.
        ("chat_threads", "is_default", "INTEGER NOT NULL DEFAULT 0"),
        # v12.4.0 — when the thread was compacted (see `ChatThread.compacted_at`). NULL on
        # everything existing, which is the truth: no record of earlier compactions was left
        # anywhere, so where to fold cannot be invented.
        ("chat_threads", "compacted_at", "TIMESTAMP"),
        # v12.7.0 — who asked for the run and through where (see `AgentRun.solicitante`).
        # NULL on everything existing, and it is the truth: no record of who asked was left
        # anywhere for earlier runs, so there is nothing to fill in.
        ("agent_runs", "solicitante", "TEXT"),
        ("agent_runs", "origen", "TEXT"),
        ("agent_runs", "canal_kind", "TEXT"),
        ("agent_runs", "canal_id", "INTEGER"),
        # 15.4.0 — the use of a capability (see `AgentRun.capacidad`). NULL on everything existing.
        ("agent_runs", "capacidad", "TEXT"),
        ("agent_runs", "uso", "TEXT"),
        ("agent_runs", "argumentos_json", "TEXT"),
        # 7.164.0 — closing the learning loop: how the runs that carried this entry inside
        # ended. Without this, an existing PVC would blow up on the first SELECT of the
        # memory block.
        ("agent_memory", "used_ok", "INTEGER NOT NULL DEFAULT 0"),
        ("agent_memory", "used_ko", "INTEGER NOT NULL DEFAULT 0"),
        # R-OPS-1: per-locale operator-edited business descriptions
        # for watchers and actions. Nullable JSON ({"es": "...",
        # "en": "..."}) so older PVCs keep booting cleanly.
        ("watcher_schedules", "business_description_json", "TEXT"),
        # v6.11.1: outbound reply content persisted on the message row
        # so the SPA shows the full thread (inbound + answer).
        ("channel_messages", "reply_subject", "TEXT"),
        ("channel_messages", "reply_body", "TEXT"),
        ("channel_messages", "reply_attachments_json", "TEXT"),
        # v6.14.0: proactive notifications. notify_config holds the
        # operator-set recipients/policy; direction tags outbound
        # notification rows reusing the channel_messages table.
        ("channels", "notify_config", "TEXT NOT NULL DEFAULT '{}'"),
        ("channel_messages", "direction", "TEXT NOT NULL DEFAULT 'inbound'"),
        # v6.26.0: "process messages since" floor. Nullable — NULL means
        # process all history (legacy behaviour); a datetime means only
        # react to messages received at/after it. Existing PVCs keep
        # NULL on upgrade, preserving their current behaviour.
        ("channels", "process_since", "TIMESTAMP"),
        # v6.27.0: optional attachments staged on a communication so a
        # 'ready' communication can deliver a real file (PPTX/PDF/CSV).
        ("communications", "attachments_json", "TEXT"),
        # v6.28.0: approximate context-window size per session (chat
        # header + Operations "session filling up" indicator).
        ("agent_sessions", "context_tokens", "INTEGER"),
        # v6.33.0: full inbound body (whole quoted chain). Kept apart from
        # the short body_excerpt preview so the agent sees the entire
        # conversation while the SPA list stays light. NULL on legacy rows.
        ("channel_messages", "body_full", "TEXT"),
        # v6.34.0: inbound email attachments staged into the origin's
        # workspace inbox/ folder. JSON list of
        # {"filename","workspace_path","mime","size"}; NULL when none.
        ("channel_messages", "inbound_attachments_json", "TEXT"),
        # v6.45.0: deliverable files captured from /workspace/exports/ at
        # run finish so the SPA can download them from the run, encargo or
        # calendar item. JSON list of {"filename","workspace_path","size"}.
        ("agent_runs", "artifacts_json", "TEXT"),
        ("scheduled_items", "artifacts_json", "TEXT"),
        # v6.44.0: per-fact usage signal (frequency/recency) feeding
        # retention. Defaulted so legacy rows count as never-accessed.
        ("user_memory", "access_count", "INTEGER NOT NULL DEFAULT 0"),
        ("user_memory", "last_accessed_at", "TIMESTAMP"),
        # F0: close the approval loop — track real execution of an approved action.
        ("approvals", "executed_at", "TIMESTAMP"),
        ("approvals", "executed_by_run_id", "INTEGER"),
        ("approvals", "execution_result", "TEXT"),
        # v7+: the automation's SECOND gate (agent condition), decoupled from
        # the signal. JSON {mode: always|data|sql, ...}. NULL on legacy rows.
        ("automations", "condition_spec", "TEXT"),
        # v7.138.0: mandatory functional description (HTML) — WHEN (signal), the
        # "only if" and WHAT it does. Nullable in DB (legacy rows keep NULL and
        # boot fine); REQUIRED at the API layer for new/edited automations.
        ("automations", "description", "TEXT"),
        # v7.139.0: which template this automation was instantiated from (its
        # ``key`` in automation_templates.{es,en}.json), so the UI can mark a
        # template as "already added" and the API rejects adding it twice. NULL for
        # manually/chat-authored rules.
        ("automations", "template_key", "TEXT"),
        # v7.25.0: explicit user↔channel↔session association (sessions view
        # + per-session concurrency guard). Legacy rows keep NULLs and are
        # rotated out naturally by the idle TTL.
        ("agent_sessions", "memory_user", "TEXT"),
        ("agent_sessions", "channel_id", "INTEGER"),
        ("agent_sessions", "origin", "TEXT NOT NULL DEFAULT 'chat'"),
        # v7.143.0: seal of the prompt contract the session was primed with. NULL on
        # existing rows ⇒ they are re-primed on their next message, which is exactly what
        # makes an EVOLVE reach an open session.
        ("agent_sessions", "prompt_contract", "TEXT"),
        # v7.29.0: memory hardening (trust) + dense retrieval (embeddings) +
        # cross-run dedup (content_hash) + distillation state. All nullable;
        # backfilled once at boot (memory_utils.backfill_memory).
        ("user_memory", "trust", "TEXT"),
        ("user_memory", "importance", "REAL"),
        ("user_memory", "content_hash", "TEXT"),
        ("user_memory", "embedding", "BLOB"),
        ("user_memory", "embed_model", "TEXT"),
        ("user_episodes", "importance", "REAL"),
        ("user_episodes", "embedding", "BLOB"),
        ("user_episodes", "embed_model", "TEXT"),
        ("user_episodes", "distill_state", "TEXT"),
        ("user_files", "embedding", "BLOB"),
        ("user_files", "embed_model", "TEXT"),
        # v7.38.0: alerts carry a small metadata bag so a converted alert can link
        # to the encargo it spawned (bidirectional trace + block a second
        # conversion). Legacy rows default to NULL → treated as "{}".
        ("alerts", "metadata_json", "TEXT"),
        # v7.40.0: the state unification. Recommendations & decisions carry the
        # same metadata bag ({encargo_id, outcome}) and decisions gain the shared
        # ``status`` lifecycle column. All nullable; the vocabulary is remapped by
        # _backfill_unified_statuses at boot. Defensive ALTERs so a PVC created
        # before these columns existed keeps booting.
        ("recommendations", "metadata_json", "TEXT"),
        ("decisions", "metadata_json", "TEXT"),
        ("decisions", "status", "TEXT"),
        # v7.41.0: the universal signal policy — separated axes (impact/urgency/
        # confidence), semantic dedup on alerts, and structured decision reasons.
        # All nullable; derived/backfilled by _backfill_signal_policy at boot.
        ("alerts", "impact", "TEXT"),
        ("alerts", "urgency", "TEXT"),
        ("alerts", "confidence", "TEXT"),
        ("alerts", "dedupe_key", "TEXT"),
        ("alerts", "root_cause", "TEXT"),
        ("alerts", "occurrences", "INTEGER NOT NULL DEFAULT 1"),
        ("alerts", "first_seen_at", "TIMESTAMP"),
        ("alerts", "last_seen_at", "TIMESTAMP"),
        ("recommendations", "impact", "TEXT"),
        ("recommendations", "urgency", "TEXT"),
        ("recommendations", "confidence", "TEXT"),
        # v7.100.0: recommendations gain the SAME semantic dedup as alerts so a
        # recurring suggestion (same dedupe_key / target) UPDATES its open row and
        # bumps ``occurrences`` instead of piling up duplicate cards each run. All
        # nullable/defaulted; backfilled by _backfill_signal_policy at boot.
        ("recommendations", "dedupe_key", "TEXT"),
        ("recommendations", "occurrences", "INTEGER NOT NULL DEFAULT 1"),
        ("recommendations", "first_seen_at", "TIMESTAMP"),
        ("recommendations", "last_seen_at", "TIMESTAMP"),
        ("decisions", "reason_code", "TEXT"),
        ("decisions", "confidence", "TEXT"),
        ("decisions", "ref_kind", "TEXT"),
        ("decisions", "ref_id", "TEXT"),
        # v7.48.0: business parameters (knowledge_requests) carry an English
        # translation of the title/question so the operator sees them in their
        # own locale. The agent fills both languages via request_knowledge; the
        # table is created by create_all, but PVCs seeded on 7.43.0–7.47.0 need
        # these two nullable columns added. NULL ⇒ fall back to the Spanish text.
        ("knowledge_requests", "label_en", "TEXT"),
        ("knowledge_requests", "question_en", "TEXT"),
        # v7.63.0: check-before-approve. An ``agent`` (natural-language) condition
        # combined with requires_approval now runs a READ-ONLY verification BEFORE
        # parking the approval, so the operator only ever approves a VERIFIED,
        # evidenced firing. Defaulted ON (1) so upgraded PVCs adopt the safer order;
        # set 0 per-automation to keep the cheaper legacy "approve → then check".
        ("automations", "check_before_approval", "BOOLEAN NOT NULL DEFAULT 1"),
        # v7.84.0: an automation may declare reference-material tags it depends on
        # (``requires_knowledge``, comma-separated). At reaction time the employee
        # is reminded to consult the DOCUMENT LIBRARY (search_knowledge) for those
        # tags so a standing order that needs a price/policy uses the OFFICIAL
        # source instead of guessing. NULL ⇒ no dependency.
        ("automations", "requires_knowledge", "TEXT"),
        # v7.126.0: reaction COST axis. "deterministic" = a templated notify/
        # create_task done directly (0 tokens); "agent" (default, back-compat) = an
        # LLM run. Added additively so an upgraded employee keeps behaving as before
        # until a rule opts into the cheap path.
        ("automations", "execution_mode", "TEXT NOT NULL DEFAULT 'agent'"),
        # v7.151.0: LINEAGE of the automations. `template_key` (above) says from which
        # DEPLOYMENT template a row was born; these two close the seal:
        #   · template_version — with which version of that template it was born, to be able
        #     to warn when the descriptor evolves and the copy falls behind.
        #   · derived_from_id  — which EXISTING automation it evolves from. It is what allows
        #     "do not touch the one that came from the deployment: make a new one from it"
        #     without the duplicates guard mistaking it for a repeated registration.
        ("automations", "template_version", "TEXT"),
        ("automations", "derived_from_id", "INTEGER"),
        # v7.128.0: Telegram @username on the directory person → resolves a Telegram
        # sender to its owner (email_for_telegram), like phones do for WhatsApp.
        ("app_users", "telegram", "TEXT"),
        # v7.145.0: who may DECIDE a parked action without being an admin. Defaults
        # to 0, so an upgraded employee keeps exactly today's rule (admins decide)
        # until an admin designates somebody in Usuarios.
        ("app_users", "can_approve", "INTEGER NOT NULL DEFAULT 0"),
        # v7.140.0 — CONDITIONAL + PROPOSED silences. All nullable/defaulted, so an
        # upgraded employee keeps its existing silences behaving EXACTLY as before
        # (review_state defaults to 'active' = what a pre-7.140 row meant, and a
        # NULL condition means "unconditional", which is today's semantics).
        #   review_state    : 'active' | 'proposed' (proposed silences nothing)
        #   subject_pattern : broader scope than an exact match_key
        #   condition_json  : the conditional logic ("normal on Sundays 02-05h")
        #   severity_max    : HARD guard — never silence above this severity
        #   expires_at      : a conditional silence should not outlive its context
        ("signal_suppressions", "review_state", "TEXT NOT NULL DEFAULT 'active'"),
        ("signal_suppressions", "subject_pattern", "TEXT"),
        ("signal_suppressions", "condition_json", "TEXT"),
        ("signal_suppressions", "severity_max", "TEXT"),
        ("signal_suppressions", "expires_at", "TIMESTAMP"),
        # v7.140.0 — closes the provenance gap of DETERMINISTIC automation
        # reactions: the Encargo they create now points at the event that caused
        # it, so it stops being invisible in "what did this automation produce".
        ("scheduled_items", "source_event_id", "INTEGER"),
    )
    for table, col, decl in pending:
        try:
            await conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN {col} {decl}"
            ))
        except Exception as exc:  # pragma: no cover — already exists
            msg = str(exc).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            logger.warning("schema migration: %s.%s — %s", table, col, exc)


async def _backfill_unified_statuses(conn) -> None:
    """v7.40.0 — idempotent status-vocabulary migration.

    Alerts, recommendations and decisions moved to ONE lifecycle
    (open → acknowledged → in_progress → done ; dismissed ✗). Remap the legacy
    per-table vocabularies onto it so existing PVCs keep their triage state:

      * alerts:          ``resolved`` → ``done``.
      * recommendations: ``pending`` → ``open``, ``accepted`` → ``acknowledged``,
                         ``rejected`` → ``dismissed`` (done / in_progress kept).
      * decisions:       ``status`` is NEW (NULL on legacy rows) — derived once
                         from the ``decision`` classification: executed → done,
                         reviewed/no_action → dismissed, escalated → in_progress,
                         everything else (deferred, unknown) → open.

    Every statement is a no-op once applied (the legacy values no longer match),
    so it is safe to run on every boot.
    """
    if conn.dialect.name != "sqlite":
        return
    stmts = (
        "UPDATE alerts SET status='done' WHERE status='resolved'",
        "UPDATE recommendations SET status='open' WHERE status='pending'",
        "UPDATE recommendations SET status='acknowledged' WHERE status='accepted'",
        "UPDATE recommendations SET status='dismissed' WHERE status='rejected'",
        "UPDATE decisions SET status='done' "
        "WHERE (status IS NULL OR status='') AND decision='executed'",
        "UPDATE decisions SET status='dismissed' "
        "WHERE (status IS NULL OR status='') AND decision IN ('reviewed','no_action')",
        "UPDATE decisions SET status='in_progress' "
        "WHERE (status IS NULL OR status='') AND decision='escalated'",
        "UPDATE decisions SET status='open' WHERE status IS NULL OR status=''",
    )
    for sql in stmts:
        try:
            await conn.execute(text(sql))
        except Exception as exc:  # pragma: no cover — table/column may be absent
            logger.debug("status backfill skipped (%s): %s", sql, exc)


async def _backfill_signal_policy(conn) -> None:
    """v7.41.0 — idempotent backfill of the separated policy axes on legacy rows.

    Existing alerts/recommendations only carry the DERIVED band (severity /
    priority); the new impact/urgency/confidence axes are NULL. We back-derive
    the axes from the legacy band (the inverse of ``signal_policy``), default
    confidence to ``medium``, and seed the dedup counters — so an upgraded PVC
    renders and filters exactly like a fresh one. Decisions get a ``reason_code``
    inferred from their disposition. Only NULL/empty cells are written, so every
    statement is a no-op after the first boot. ``dedupe_key`` is deliberately
    left NULL on legacy alerts (they never participate as a dedup target).
    """
    if conn.dialect.name != "sqlite":
        return
    stmts = (
        # alerts: severity → (impact, urgency) [inverse of _SEVERITY_MATRIX seed]
        "UPDATE alerts SET impact = CASE lower(severity) "
        "WHEN 'info' THEN 'low' WHEN 'low' THEN 'low' WHEN 'medium' THEN 'medium' "
        "WHEN 'high' THEN 'high' WHEN 'critical' THEN 'critical' ELSE 'medium' END "
        "WHERE impact IS NULL OR impact=''",
        "UPDATE alerts SET urgency = CASE lower(severity) "
        "WHEN 'info' THEN 'low' WHEN 'low' THEN 'medium' WHEN 'medium' THEN 'medium' "
        "WHEN 'high' THEN 'high' WHEN 'critical' THEN 'high' ELSE 'medium' END "
        "WHERE urgency IS NULL OR urgency=''",
        "UPDATE alerts SET confidence='medium' WHERE confidence IS NULL OR confidence=''",
        "UPDATE alerts SET occurrences=1 WHERE occurrences IS NULL",
        "UPDATE alerts SET first_seen_at=created_at WHERE first_seen_at IS NULL",
        "UPDATE alerts SET last_seen_at=created_at WHERE last_seen_at IS NULL",
        # recommendations: priority → (impact, urgency)
        "UPDATE recommendations SET impact = CASE lower(priority) "
        "WHEN 'low' THEN 'low' WHEN 'medium' THEN 'medium' WHEN 'high' THEN 'high' "
        "WHEN 'urgent' THEN 'critical' ELSE 'medium' END "
        "WHERE impact IS NULL OR impact=''",
        "UPDATE recommendations SET urgency = CASE lower(priority) "
        "WHEN 'low' THEN 'low' WHEN 'medium' THEN 'medium' WHEN 'high' THEN 'high' "
        "WHEN 'urgent' THEN 'high' ELSE 'medium' END "
        "WHERE urgency IS NULL OR urgency=''",
        "UPDATE recommendations SET confidence='medium' WHERE confidence IS NULL OR confidence=''",
        # v7.100.0 — seed the dedup counters, and backfill dedupe_key on OPEN legacy
        # recommendations from their target so that pre-7.100 duplicates converge:
        # the NEXT reappearance UPDATES the existing open row instead of adding yet
        # another card. Only touched when a target exists and the key is still NULL.
        "UPDATE recommendations SET occurrences=1 WHERE occurrences IS NULL",
        "UPDATE recommendations SET first_seen_at=created_at WHERE first_seen_at IS NULL",
        "UPDATE recommendations SET last_seen_at=created_at WHERE last_seen_at IS NULL",
        "UPDATE recommendations SET dedupe_key = lower(target_entity_kind || ':' || target_entity_id) "
        "WHERE (dedupe_key IS NULL OR dedupe_key='') "
        "AND status IN ('open','acknowledged','in_progress') "
        "AND target_entity_kind IS NOT NULL AND target_entity_kind<>'' "
        "AND target_entity_id IS NOT NULL AND target_entity_id<>''",
        # decisions: disposition → reason_code
        "UPDATE decisions SET reason_code = CASE lower(decision) "
        "WHEN 'no_action' THEN 'no_action_needed' WHEN 'deferred' THEN 'deferred_timing' "
        "WHEN 'escalated' THEN 'needs_human' ELSE 'no_action_needed' END "
        "WHERE reason_code IS NULL OR reason_code=''",
        "UPDATE decisions SET confidence='medium' WHERE confidence IS NULL OR confidence=''",
    )
    for sql in stmts:
        try:
            await conn.execute(text(sql))
        except Exception as exc:  # pragma: no cover — table/column may be absent
            logger.debug("signal-policy backfill skipped (%s): %s", sql, exc)


async def _backfill_canonical_dedupe_keys(conn) -> None:
    """v7.107.1 — rewrites the dedup keys to the canonical form and MERGES the duplicates
    that already exist. It works on SQLite AND on Postgres.

    The old keys were written by the agent by hand (``reposicion:product.product:21`` one
    pass, ``reabastecimiento-sin-regla:warehouse:1:product:21`` the next), so the same
    problem lived in two rows the operator saw as duplicates. Fixing the normalizer only
    prevents NEW duplicates: the rows already created are still there with their divergent
    keys. This migration converges them:

      1. Recomputes the key of every ACTIVE signal that declares a target:
         recommendations → the canonical target; alerts → ``<concern>:<target>``.
      2. If after recomputing two active signals share a key, the OLDEST survives (the one
         the operator already knows), it adds up the reappearances and the others become
         ``dismissed`` with a record in ``metadata_json`` of which row absorbed them — what
         the runtime would have done if the key had converged.

    Idempotent: on the second start there are no divergent keys nor duplicates and it
    writes nothing. **No dialect guard on purpose**: the app can run on SQLite (local PVC)
    or Postgres (APP_DATABASE_URL), and in 7.107.0 the `sqlite` guard inherited from the old
    migrations left the backfill unexecuted exactly where it was needed. The JSON is composed
    in Python (not `json_patch`, which is SQLite's).
    """
    from app import signal_policy

    activos = ("open", "acknowledged", "in_progress")
    # `root_cause` ONLY exists in `alerts` (the Recommendation model does not have it):
    # asking for it in both made the recommendations SELECT fail, the except swallowed it as
    # "table not available" and the backfill SILENTLY skipped exactly the table that had the
    # duplicates. Each table declares its columns.
    for tabla, target_only, tiene_root_cause in (("alerts", False, True),
                                                 ("recommendations", True, False)):
        rc_col = "root_cause" if tiene_root_cause else "NULL AS root_cause"
        try:
            filas = (await conn.execute(text(
                f"SELECT id, dedupe_key, {rc_col}, target_entity_kind, target_entity_id, "
                f"COALESCE(occurrences, 1) AS occ, created_at, metadata_json "
                f"FROM {tabla} WHERE status IN :activos"
            ).bindparams(sqlalchemy.bindparam("activos", expanding=True)),
                {"activos": list(activos)})).mappings().all()
        except Exception as exc:  # noqa: BLE001 — table missing in very old apps
            logger.warning("dedupe backfill: %s no disponible (%s) — "
                           "REVISA este aviso: si la tabla existe, el backfill se está "
                           "saltando en silencio", tabla, exc)
            continue

        canonica: dict[int, str] = {}
        for f in filas:
            if not (f["target_entity_kind"] and f["target_entity_id"]):
                continue                      # without a target there is no canonical form to impose
            nueva = signal_policy.normalize_dedupe_key(
                f["dedupe_key"], f["root_cause"],
                f["target_entity_kind"], f["target_entity_id"], target_only=target_only)
            if nueva:
                canonica[f["id"]] = nueva

        # 1) keys to the canonical form
        for fid, nueva in canonica.items():
            await conn.execute(
                text(f"UPDATE {tabla} SET dedupe_key = :k WHERE id = :id "
                     f"AND COALESCE(dedupe_key, '') <> :k"), {"k": nueva, "id": fid})

        # 2) merge what now collides (the oldest wins)
        grupos: dict[str, list[dict]] = {}
        for f in filas:
            clave = canonica.get(f["id"]) or f["dedupe_key"]
            if clave:
                grupos.setdefault(clave, []).append(dict(f))
        for clave, miembros in grupos.items():
            if len(miembros) < 2:
                continue
            miembros.sort(key=lambda r: (str(r["created_at"] or ""), r["id"]))
            superviviente = miembros[0]
            total = sum(int(m["occ"] or 1) for m in miembros)
            await conn.execute(
                text(f"UPDATE {tabla} SET occurrences = :n WHERE id = :id"),
                {"n": total, "id": superviviente["id"]})
            for m in miembros[1:]:
                try:
                    meta = json.loads(m["metadata_json"] or "{}") or {}
                except Exception:  # noqa: BLE001 — corrupt metadata: it does not block the merge
                    meta = {}
                meta.update({"merged_into": superviviente["id"],
                             "merged_reason": "dedupe_key canónica (v7.107.x)"})
                await conn.execute(
                    text(f"UPDATE {tabla} SET status = 'dismissed', metadata_json = :m "
                         f"WHERE id = :id"),
                    {"m": json.dumps(meta, default=str), "id": m["id"]})
            logger.info("dedupe backfill: %s — %s duplicada(s) fusionada(s) en id=%s (clave %s)",
                        tabla, len(miembros) - 1, superviviente["id"], clave)


async def _backfill_communication_delivery_trace(conn) -> None:
    """v7.107.3 — seals the delivery record of the `sent` communications that were left
    WITHOUT a trace (AGO-83).

    The agent can record an already delivered communication (`status='sent'`); until
    7.107.2 that was stored with `sent_at`/`delivery_id` NULL and the reconcile did not reach
    them (it only looked at draft/ready). The outbox piled up sends without proof: it is
    recorded that it went out, but not when nor with which id — unauditable.

    With the creation path and the reconcile fixed, the old rows remain: they are given
    `sent_at` = their `created_at` (the moment the agent declared the delivery; it is the
    honest data we have, no date is invented) and `delivery_id='agent-reported'`, which says
    exactly what it is: a self-reported delivery, without a channel id. It only touches
    `sent` rows without a trace — idempotent and it never invents a delivery on a draft/failed.
    """
    try:
        await conn.execute(text(
            "UPDATE communications SET "
            "sent_at = COALESCE(sent_at, created_at), "
            "delivery_id = COALESCE(delivery_id, 'agent-reported') "
            "WHERE status = 'sent' AND (sent_at IS NULL OR delivery_id IS NULL)"))
    except Exception as exc:  # noqa: BLE001 — table missing in apps without channels
        logger.warning("delivery-trace backfill: communications no disponible (%s) — "
                       "REVISA este aviso: si la tabla existe, se está saltando en silencio",
                       exc)


async def _ensure_indexes(conn) -> None:
    """Idempotent index creation for existing PVCs (create_all skips indexes
    on tables that already exist)."""
    if conn.dialect.name != "sqlite":
        return
    plain = (
        ("ix_user_memory_live", "user_memory", "user_email, superseded_by"),
        (
            "ix_channel_messages_notify_day",
            "channel_messages",
            "channel_id, direction, received_at",
        ),
        # v7.100.0 — recommendations semantic dedup lookup (mirrors alerts).
        ("ix_recommendations_dedupe_status", "recommendations", "dedupe_key, status"),
        # ── v7.140.0 ──
        # Self-memory: the prompt block is built on EVERY autonomous run, so its
        # three access paths are indexed (live rows per kind, per subject, and the
        # review queue). Without these the block would table-scan on every fire.
        ("ix_agent_memory_live", "agent_memory", "superseded_by, kind"),
        ("ix_agent_memory_subject_kind", "agent_memory", "subject, kind"),
        ("ix_agent_memory_hash_subject", "agent_memory", "content_hash, subject"),
        ("ix_agent_memory_review", "agent_memory", "review_state, kind"),
        ("ix_agent_episodes_subject_at", "agent_episodes", "subject, created_at"),
        # Bidirectional traceability: BOTH directions are indexed because both
        # are hot — the queue reads by state, and every listing does the reverse
        # lookup (target, target_id) to stamp its "learned from" badge.
        # Columns MUST mirror ``app_models/learning_effects.py``: on a fresh DB
        # ``create_all`` builds these, here they are backfilled on an existing
        # PVC — same name with a different shape would mean two different
        # indexes depending on the install's age.
        ("ix_learning_effects_learning", "learning_effects", "learning_id, state"),
        ("ix_learning_effects_target", "learning_effects", "target, target_id, state"),
        ("ix_learning_effects_state", "learning_effects", "state, created_at"),
        # The "why didn't it warn me?" feed: by rule and by recency.
        ("ix_signal_silence_log_rule", "signal_silence_log", "suppression_id, occurred_at"),
        ("ix_signal_silence_log_at", "signal_silence_log", "occurred_at"),
        # Conditional-silence lookup done by the deterministic gate.
        ("ix_signal_suppressions_pattern", "signal_suppressions",
         "signal_table, review_state, enabled"),
        # Automation governance: the daily rollup is read by (automation, day)
        # ranges, and the funnel filters the ledger by (automation, at).
        ("ix_automation_daily_stats_day", "automation_daily_stats", "day"),
        ("ix_evallog_auto_at", "automation_eval_log", "automation_id, at"),
        # THE index that makes provenance viable. Every business entity carries
        # ``created_by_run_id``, and it is the column you must enter by to answer
        # "what did this automation produce, and how much of it is still open" —
        # yet it was indexed NOWHERE, so each count scanned the whole table. The
        # composite with ``status`` covers the "abiertas" count from the index
        # alone. Additive and idempotent, like every entry here.
        ("ix_alerts_run_status", "alerts", "created_by_run_id, status"),
        ("ix_recommendations_run_status", "recommendations", "created_by_run_id, status"),
        ("ix_decisions_run_status", "decisions", "created_by_run_id, status"),
        ("ix_scheduled_items_run_status", "scheduled_items", "created_by_run_id, status"),
        ("ix_approvals_run_status", "approvals", "created_by_run_id, status"),
        ("ix_communications_run_status", "communications", "created_by_run_id, status"),
        ("ix_scoring_run", "scoring", "created_by_run_id"),
        # ``events.source`` alone was indexed, so a range query had to read EVERY
        # event of that automation ever. With ``detected_at`` in the index the
        # window is entered by the small set instead of filtered afterwards.
        ("ix_events_source_detected", "events", "source, detected_at"),
        # v7.140.0 — the deterministic-fire chain: ScheduledItem now links to the
        # event that created it (same pattern as alerts/decisions).
        ("ix_scheduled_items_source_event", "scheduled_items", "source_event_id"),
    )
    unique = (
        (
            "uq_channel_message_provider",
            "channel_messages",
            "channel_id, provider_message_id",
        ),
        # v12.0.0 — the run's token is the key the model names it with: unique, and indexed
        # because it is resolved on EVERY tool call.
        ("uq_agent_runs_run_token", "agent_runs", "run_token"),
    )
    for name, table, cols in plain:
        try:
            await conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})"
            ))
        except Exception as exc:  # pragma: no cover
            logger.warning("schema index: %s — %s", name, exc)
    for name, table, cols in unique:
        try:
            await conn.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({cols})"
            ))
        except Exception as exc:  # pragma: no cover
            logger.warning("schema unique index: %s — %s", name, exc)
    # v12.0.0 — ONE default conversation per person, guaranteed by the database and not by
    # the code that creates it (which is exactly where the single row for the whole pod
    # slipped in). PARTIAL unique index: it only counts the rows marked `is_default = 1`.
    try:
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_threads_default "
            "ON chat_threads (user_email) WHERE is_default = 1"
        ))
    except Exception as exc:  # pragma: no cover
        logger.warning("schema unique index: uq_chat_threads_default — %s", exc)


async def _backfill_default_threads_per_user(conn) -> None:
    """v12.0.0 — the default conversation becomes ONE ROW PER PERSON.

    Until now it was a single row with the literal id ``"default"`` for the whole pod: since
    ``chat_threads.id`` is the only PK, only the first user who wrote got to have it (the
    others' INSERTs failed and were swallowed), and EVERYONE's turns carried the same
    ``thread_id='default'`` — they crossed between people. This migration leaves the state
    the new code expects, without losing anything:

      1. The legacy row ``id='default'`` (if it exists) becomes its owner's default row: its
         ``id`` changes to the per-person id (``hilo_semilla_de``) and it is marked
         ``is_default=1``. It keeps title, lock and dates. If its owner already had the new
         row, the legacy one is marked deleted (no history is lost: the runs are rewritten).
      2. For each person with turns under ``thread_id='default'`` (by their ``memory_user``,
         which the chat stamps when starting the turn), their default row is created if
         missing and THOSE turns are rewritten to their per-person id. Turns without
         ``memory_user`` (very old) are left as they are: they cannot be attributed without
         guessing whose they were.

    Idempotent: on the second start there is no legacy row nor turns with ``'default'`` and
    it writes nothing. SQLite only (the light migrations runner is SQLite; on Postgres it
    warns).
    """
    if conn.dialect.name != "sqlite":
        logger.warning("default-threads backfill: solo SQLite; dialecto %s — revisar a mano",
                       conn.dialect.name)
        return
    from app.agent_sessions import hilo_semilla_de

    try:
        # 1) Legacy 'default' row → its owner's default row.
        legado = (await conn.execute(text(
            "SELECT user_email FROM chat_threads WHERE id = 'default'"
        ))).first()
        if legado is not None and (legado[0] or "").strip():
            duena = legado[0].strip().lower()
            nuevo = hilo_semilla_de(duena)
            existe = (await conn.execute(text(
                "SELECT 1 FROM chat_threads WHERE id = :id"), {"id": nuevo})).first()
            if existe is None:
                await conn.execute(text(
                    "UPDATE chat_threads SET id = :nuevo, is_default = 1, user_email = :e "
                    "WHERE id = 'default'"), {"nuevo": nuevo, "e": duena})
            else:
                await conn.execute(text(
                    "UPDATE chat_threads SET deleted_at = CURRENT_TIMESTAMP "
                    "WHERE id = 'default'"))
            logger.info("default-threads backfill: fila legado 'default' → %s", nuevo)

        # 2) Turns under 'default': one row per person and their turns rewritten.
        personas = (await conn.execute(text(
            "SELECT DISTINCT lower(json_extract(request_meta_json, '$.memory_user')) "
            "FROM agent_runs WHERE thread_id = 'default' AND mode = 'chat' "
            "AND json_extract(request_meta_json, '$.memory_user') IS NOT NULL"
        ))).all()
        for (email,) in personas:
            e = (email or "").strip().lower()
            if not e:
                continue
            nuevo = hilo_semilla_de(e)
            await conn.execute(text(
                "INSERT OR IGNORE INTO chat_threads "
                "(id, user_email, title, title_locked, is_default, created_at, last_used_at) "
                "VALUES (:id, :e, 'Conversación', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ), {"id": nuevo, "e": e})
            await conn.execute(text(
                "UPDATE chat_threads SET is_default = 1 WHERE id = :id"), {"id": nuevo})
            res = await conn.execute(text(
                "UPDATE agent_runs SET thread_id = :nuevo WHERE thread_id = 'default' "
                "AND mode = 'chat' "
                "AND lower(json_extract(request_meta_json, '$.memory_user')) = :e"
            ), {"nuevo": nuevo, "e": e})
            logger.info("default-threads backfill: %s → %s (%s turnos)",
                        e, nuevo, res.rowcount)
    except Exception as exc:  # noqa: BLE001 — a migration cannot prevent starting
        logger.warning("default-threads backfill: %s — REVISA este aviso", exc)


async def _ensure_schema_version(conn) -> None:
    """Track the applied skill schema generation for operator diagnostics."""
    if conn.dialect.name != "sqlite":
        return
    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
    """))
    row = (await conn.execute(text(
        "SELECT version FROM schema_version WHERE id = 1"
    ))).first()
    if row is None:
        await conn.execute(
            text("INSERT INTO schema_version (id, version) VALUES (1, :v)"),
            {"v": SCHEMA_VERSION},
        )
    elif int(row[0]) < SCHEMA_VERSION:
        await conn.execute(
            text(
                "UPDATE schema_version SET version = :v, "
                "applied_at = CURRENT_TIMESTAMP WHERE id = 1"
            ),
            {"v": SCHEMA_VERSION},
        )


async def _apply_pragmas(_conn) -> None:
    """Deprecated in P3 — pragmas now run on EVERY new connection via
    the ``connect`` event listener registered in ``app_database.py``.
    This shim stays for back-compat with patterns/agents-md.md
    examples that still call it explicitly; it is safe to remove
    in a future release. SQLAlchemy fires the listener before any
    SQL on a fresh connection so the contract is stronger than the
    boot-only call this used to perform."""
    return None


# Tables that reference agent_runs.id WITHOUT ondelete (app_models/runtime.py).
_HIJAS_DE_AGENT_RUNS = ("tool_use_log", "tool_call_log")


async def _cleanup_old_chat_runs() -> None:
    """R6: chat runs are written on every operator message (B8). The
    table grows unbounded if we don't prune; keep 30 days, drop the
    rest. Run once a day from the lifespan housekeeping loop."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    viejos = ("SELECT id FROM agent_runs WHERE mode = 'chat' AND finished_at IS NOT NULL "
              "AND finished_at < :cutoff")
    async with app_engine.begin() as conn:
        # The tool ledgers hang from the run WITHOUT ondelete: deleting the run first fails
        # with "FOREIGN KEY constraint failed" and the table stops being pruned forever (only
        # a daily WARNING remained). The other children are SET NULL and let go on their own.
        for hija in _HIJAS_DE_AGENT_RUNS:
            await conn.execute(text(f"DELETE FROM {hija} WHERE run_id IN ({viejos})"),
                               {"cutoff": cutoff})
        await conn.execute(text(f"DELETE FROM agent_runs WHERE id IN ({viejos})"),
                           {"cutoff": cutoff})


async def _cleanup_idle_agent_sessions() -> None:
    """Drop server-side OpenCode sessions (chat per operator + channel
    per origin) idle past the TTL, deleting the sidecar session too so
    the ACL doesn't leak. See ``app.agent_sessions.SessionManager.gc``."""
    from app.agent_sessions import get_session_manager
    await get_session_manager().gc()


async def _cleanup_old_channel_messages() -> None:
    """v7.85.0 — opt-in retention for channel messages + their staged attachment
    files. When ``AGENT_CHANNEL_MESSAGE_RETENTION_DAYS`` > 0, prune settled
    channel_messages older than that (by ``received_at``) and remove their inbound
    attachment files from the workspace. In-flight rows (``pending``/``processing``)
    are never pruned. No-op when the knob is 0 (keep-forever default)."""
    from app.config import AGENT_CHANNEL_MESSAGE_RETENTION_DAYS as _RET
    if _RET <= 0:
        return
    import json as _json
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete as _sql_delete, select as _select
    from app.app_database import app_write
    from app.app_models.channels import ChannelMessage
    cutoff = datetime.now(timezone.utc) - timedelta(days=_RET)
    async with app_write() as s:
        rows = (await s.execute(
            _select(ChannelMessage.id, ChannelMessage.inbound_attachments_json)
            .where(ChannelMessage.received_at < cutoff,
                   ChannelMessage.status.notin_(("pending", "processing")))
            .limit(5000)
        )).all()
        if not rows:
            return
        # Delete staged attachment files first (confined to the workspace root
        # so a tampered path can't escape). A row whose file removal FAILS is
        # kept (retried by tomorrow's run) — deleting it anyway would orphan
        # the attachment on the PVC, the exact PII/disk leak retention exists
        # to prevent (v7.89.1; this also NameError'd on an unimported `os`).
        try:
            from app import workspace_fs as wfs
            ws_ok = wfs.workspace_root() is not None
        except Exception:
            ws_ok = False
        failed_ids: set = set()
        for _id, att_json in rows:
            for att in (_json.loads(att_json) if att_json else []) or []:
                rel = (att or {}).get("workspace_path") if isinstance(att, dict) else None
                if not rel:
                    continue
                if not ws_ok:
                    # Workspace unresolvable → we can't delete this row's file,
                    # so we must not delete the row either (v7.89.2: this path
                    # used to prune ALL rows, orphaning every attachment).
                    failed_ids.add(_id)
                    continue
                try:
                    p = Path(wfs.resolve(rel, must_exist=False))
                    if p.exists():
                        p.unlink()
                except Exception:
                    failed_ids.add(_id)
                    logger.warning("retention: could not remove attachment %s — keeping message row for retry", rel, exc_info=True)
        if not ws_ok and failed_ids:
            logger.warning(
                "retention: workspace root unresolvable — keeping %d message row(s) "
                "with staged attachments for retry", len(failed_ids),
            )
        ids = [r[0] for r in rows if r[0] not in failed_ids]
        if not ids:
            return
        await s.execute(_sql_delete(ChannelMessage).where(ChannelMessage.id.in_(ids)))
        await s.commit()
        logger.info("channel-message retention: pruned %d messages older than %d days (%d kept for retry)",
                    len(ids), _RET, len(failed_ids))


async def _housekeeping_run_forever(interval_s: float = 86400.0) -> None:
    """Daily housekeeping. Runs cheaply but only once a day so it
    doesn't compete with watcher / executor for the SQLite lock."""
    while True:
        try:
            await _cleanup_old_chat_runs()
        except Exception:
            logger.warning("chat-run cleanup failed", exc_info=True)
        try:
            await _cleanup_idle_agent_sessions()
        except Exception:
            logger.warning("agent-session gc failed", exc_info=True)
        try:
            await _cleanup_old_channel_messages()
        except Exception:
            logger.warning("channel-message retention failed", exc_info=True)
        try:
            # Drain any billing rows that piled up during a genai-api outage
            # (also drained opportunistically after each successful report).
            from app.cost_reporter import flush_pending_cost_reports
            sent = await flush_pending_cost_reports(limit=200)
            if sent:
                logger.info("housekeeping: flushed %d buffered cost reports", sent)
        except Exception:
            logger.warning("pending cost-report flush failed", exc_info=True)
        # v7.140.0 — SUMMARISE BEFORE PRUNING, and do NOT prune if the summary
        # failed. The order is the whole point: the operator's "how many times did
        # this fire last month?" is answered from the daily rollup, so pruning the
        # detail without having frozen the totals would silently shrink history.
        # A prune that is skipped for a day costs a few MB; one that runs early
        # costs the answer forever.
        try:
            from app import automations_service as _asvc
            from app.config import (
                AUTOMATION_EVAL_LOG_RETENTION_DAYS, AUTOMATION_STATS_ROLLUP,
            )
            rolled_up = True
            if AUTOMATION_STATS_ROLLUP:
                try:
                    # Re-summarise the WHOLE window that still has detail, not
                    # just the last 3 days. Each day is an upsert, so re-running
                    # is free of duplicates and it makes the summary SELF-HEALING:
                    # any day whose frozen totals are wrong gets rewritten from
                    # the rows while they still exist. That is what repaired the
                    # days 7.141.x had already frozen as 0/1 (SUM over a Boolean
                    # column — see `automation_stats.rows_where`); without it the
                    # only fix would have been a one-shot migration, and a wrong
                    # total becomes unrecoverable the moment the detail is pruned.
                    # Bounded cost: one indexed grouped query over ≤14 days.
                    n = await _asvc.rollup_eval_log(
                        days_back=AUTOMATION_EVAL_LOG_RETENTION_DAYS)
                    if n:
                        logger.info("housekeeping: rolled up %d automation-day rows", n)
                except Exception:
                    rolled_up = False
                    logger.warning("automation stats rollup failed — SKIPPING the "
                                   "eval-log prune to avoid losing history",
                                   exc_info=True)
            if rolled_up:
                pruned = await _asvc.prune_eval_log(
                    days=AUTOMATION_EVAL_LOG_RETENTION_DAYS)
                if pruned:
                    logger.info("housekeeping: pruned %d old automation eval-log rows",
                                pruned)
        except Exception:
            logger.warning("automation eval-log housekeeping failed", exc_info=True)
        # v7.140.0 — the non-action log ("why didn't it warn me?") is a narrative,
        # not an accounting record: its permanent number lives in the silence's own
        # counter, so it can be pruned without losing anything auditable.
        try:
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            from sqlalchemy import delete as _delete
            from app.app_database import app_session as _sess
            from app.app_models.signal_silence_log import SignalSilenceLog
            from app.config import SIGNAL_SILENCE_LOG_RETENTION_DAYS
            cutoff = (_dt.now(_tz.utc).replace(tzinfo=None)
                      - _td(days=max(int(SIGNAL_SILENCE_LOG_RETENTION_DAYS), 1)))
            async with _sess() as s:
                res = await s.execute(_delete(SignalSilenceLog).where(
                    SignalSilenceLog.occurred_at < cutoff))
                await s.commit()
            if res.rowcount:
                logger.info("housekeeping: pruned %d old silence-log rows", res.rowcount)
        except Exception:
            logger.warning("silence-log prune failed", exc_info=True)
        # v7.140.0 — deterministic self-memory maintenance: expire learnings past
        # their ``valid_until`` and cap raw observations. The SEMANTIC half (merging
        # observations into a baseline) is the agent's job in a
        # ``self_memory_maintenance`` run, enqueued from the block build.
        try:
            from app.agent_memory_utils import compact as _self_mem_compact
            res = await _self_mem_compact()
            if any(res.get(k) for k in ("expired", "observations_pruned",
                                        "episodes_pruned")):
                logger.info("housekeeping: self-memory compacted %s", res)
        except Exception:
            logger.warning("self-memory compaction failed", exc_info=True)
        # v7.140.0 — one line that decides whether the new composite indexes get
        # USED: SQLite's planner leans on ANALYZE statistics, and this release adds
        # several composite indexes (provenance, funnel). Without fresh stats the
        # planner can keep choosing a scan on a table that just grew.
        try:
            from sqlalchemy import text as _text
            from app.app_database import app_engine as _eng
            async with _eng.begin() as conn:
                await conn.execute(_text("PRAGMA optimize"))
        except Exception:
            logger.debug("PRAGMA optimize failed (ignored)", exc_info=True)
        await asyncio.sleep(interval_s)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail-fast without the introspection URL (14.0.0): the token of whoever signs in is
    # checked by asking the platform, and with nobody to ask EVERY request would give 503.
    # Better that the pod does not start and the reason shows in the deployment's log.
    #
    # The Composio part (WAI_ENGINE_URL + WAI_AGENT_RUNTIME_JWT) does NOT block startup: only
    # the Composio connectors need it, and without them the Connectors screen says "not
    # available" (`mcp_connectors.is_enabled`) while the rest of the employee works.
    from app.config import WAI_INTROSPECCION_URL
    if not WAI_INTROSPECCION_URL:
        raise RuntimeError(
            "WAI_INTROSPECCION_URL is empty — the app cannot verify any "
            "user token. The deployer injects it on every deploy "
            "({WAI_BACKEND_URL}/backend/auth/introspeccion); redeploy with "
            "a current deployer."
        )

    async with app_engine.begin() as conn:
        await _apply_pragmas(conn)
        await conn.run_sync(AppBase.metadata.create_all)
        await _ensure_columns(conn)
        # v12.0.0 — BEFORE the indexes: it creates the default thread's partial unique index
        # on an already cleaned state (one row per person), not on the legacy one.
        await _backfill_default_threads_per_user(conn)
        await _backfill_unified_statuses(conn)
        await _backfill_signal_policy(conn)
        await _backfill_canonical_dedupe_keys(conn)
        await _backfill_communication_delivery_trace(conn)
        await _ensure_indexes(conn)
        # v7.56.0 — FTS5 lexical index over memory (facts/episodes/files) +
        # sync triggers. Idempotent; backfills existing rows once. No-op (logged)
        # if the SQLite build lacks FTS5 — memory then uses embeddings/recency.
        try:
            from app.memory_fts import ensure_fts
            await ensure_fts(conn)
        except Exception:
            logger.debug("memory FTS setup failed at boot (ignored)", exc_info=True)
        # v7.84.0 — FTS5 index over the DOCUMENT KNOWLEDGE LIBRARY chunks + sync
        # triggers. Same idempotent/backfill/no-op-without-FTS5 semantics.
        try:
            from app.knowledge_fts import ensure_fts as ensure_knowledge_fts
            await ensure_knowledge_fts(conn)
        except Exception:
            logger.debug("knowledge FTS setup failed at boot (ignored)", exc_info=True)
        await _ensure_schema_version(conn)

    await recover_stale_runs()

    # v7.170.4 — APPROVED approvals whose event never went out. Approving stopped holding
    # the write lock during the call to the CRM (15 s peaks blocking every other writer),
    # and that leaves a window: dying between "mark approved" and "emit the event". The
    # window already existed before splitting —the Composio path committed before calling—,
    # but nobody repaired it: the operator gave the sign-off and the employee never found
    # out. Idempotent by the event's fingerprint.
    try:
        from app.routes.approvals_routes import dispatch_orphan_approvals
        await dispatch_orphan_approvals()
    except Exception:  # pragma: no cover — it must never prevent startup
        logger.debug("orphan-approval dispatch failed at boot (ignored)", exc_info=True)

    # v7.53.0 — materialize any alert/recommendation/decision «en curso» whose
    # encargo (or its execution) already reached a terminal state, from the REAL
    # photo (no events). Runs on every read too; this boot pass just warms it.
    try:
        from app.app_database import app_session
        from app.routes.scheduling_routes import materialize_signal_statuses
        async with app_session() as _s:
            await materialize_signal_statuses(_s)
    except Exception:
        logger.debug("signal↔encargo materialize at boot failed", exc_info=True)

    # v7.39.0 — warm the operator-set business timezone into the in-memory cache
    # so timeutil (cron/calendar/clock) uses the right wall clock from tick #1.
    try:
        from app.business_timezone import refresh_cache as _warm_business_tz
        await _warm_business_tz()
    except Exception:
        logger.debug("business timezone warm-up failed (using deploy default)", exc_info=True)

    # Channels are configured IN THE APP (Canales page wizard) — deploy-time
    # seeding (CHANNELS_SEED) was retired in skill 7.91.0: the employee ships
    # the channels CAPABILITY, the operator wires the actual mailboxes/numbers
    # after deploy.

    # Admin bootstrap ONLY: the employee is born with ONE user, the deploy
    # identity, as admin (anti-lockout). Mirroring the project's roster was
    # retired in 7.145.0 — the operator adds people on purpose (in bulk from the
    # project, or as a channel-only contact). Never aborts boot.
    try:
        from app.users_directory import bootstrap_directory
        await bootstrap_directory()
    except Exception:
        logger.warning("users directory bootstrap failed (boot continues)", exc_info=True)

    proxy = AgentProxy()
    set_agent_proxy(proxy)

    # v7.29.0 — one-time backfill of trust (from the legacy ``source`` column)
    # + embeddings for pre-existing memory. Runs in the BACKGROUND so a large
    # store never delays boot; idempotent (only touches rows missing the field).
    async def _memory_backfill() -> None:
        try:
            from app.memory_utils import backfill_memory
            await backfill_memory()
        except Exception:
            logger.warning("memory backfill failed (non-fatal)", exc_info=True)
    backfill_task = asyncio.create_task(_memory_backfill(), name="memory-backfill")

    # v7.84.0 — re-index any knowledge document a pod restart left mid-flight
    # (status uploading/indexing). The bytes are on the PVC; run in the BACKGROUND
    # so a queue of documents never delays boot. No-op when the library is empty.
    async def _knowledge_resume() -> None:
        try:
            from app.knowledge_library import resume_pending
            await resume_pending()
        except Exception:
            logger.warning("knowledge resume failed (non-fatal)", exc_info=True)
    knowledge_task = asyncio.create_task(_knowledge_resume(), name="knowledge-resume")

    # Re-seed the operator's LOCAL write-policy hardening into the platform
    # runtime store so the WAI-native wai-odoo gate keeps enforcing it after a
    # pod restart / redeploy (the store is keyed by AGENT_ID; a fresh deploy
    # would otherwise leave the engine reading only the JWT floor until the
    # operator re-saves). Background + best-effort (INC-AGO-61/64).
    async def _runtime_policy_reseed() -> None:
        try:
            from app.runtime_policy_sync import push_local_write_policy
            await push_local_write_policy()
        except Exception:
            logger.warning("runtime write-policy re-seed failed (non-fatal)", exc_info=True)
    policy_seed_task = asyncio.create_task(
        _runtime_policy_reseed(), name="runtime-policy-reseed",
    )

    watcher_task = asyncio.create_task(watcher_run_forever(), name="watcher")
    executor_task = asyncio.create_task(executor_run_forever(), name="executor")
    channels_task = asyncio.create_task(channels_run_forever(), name="channels-poller")
    # 14.14.0 — the phones of whoever may decide hear about every new approval.
    from app.avisos_de_aprobacion import run_forever as _avisos_de_aprobacion
    approval_notices_task = asyncio.create_task(_avisos_de_aprobacion(), name="approval-notices")
    from app.config import AGENT_HOUSEKEEPING_INTERVAL_HOURS as _hk_h
    housekeeping_task = asyncio.create_task(
        _housekeeping_run_forever(float(_hk_h) * 3600.0), name="housekeeping",
    )
    logger.info("housekeeping: cada %.1f h (AGENT_HOUSEKEEPING_INTERVAL_HOURS)", _hk_h)
    try:
        yield
    finally:
        # R11: graceful shutdown — give the executor a moment to drain
        # in-flight runs before we cancel. K8s sends SIGTERM with a
        # 30 s grace period by default; we use up to 25 s so the final
        # cleanup never gets cut off. The one-shot boot tasks (backfill /
        # knowledge resume) are cancelled too — normally long done, but a
        # shutdown mid-backfill must not leave them writing during teardown.
        for task in (watcher_task, channels_task, housekeeping_task,
                     backfill_task, knowledge_task, policy_seed_task, approval_notices_task):
            task.cancel()
        try:
            await asyncio.wait_for(executor_task, timeout=25.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            executor_task.cancel()
        for task in (watcher_task, executor_task, channels_task,
                     housekeeping_task, backfill_task, knowledge_task,
                     policy_seed_task, approval_notices_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await proxy.close()
        set_agent_proxy(None)


def _resolve_app_title() -> str:
    """API/docs title = the EMPLOYEE's name, resolved from the manifest
    (persona name → app name → fallback). Replaces the old literal
    ``{App Display Name}`` placeholder that shipped in every generated app."""
    try:
        from app.config import AGENT_APP
        aa = AGENT_APP if isinstance(AGENT_APP, dict) else {}
        persona = aa.get("persona") if isinstance(aa.get("persona"), dict) else {}
        name = persona.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        n = aa.get("name")
        if isinstance(n, str) and n.strip():
            return n.strip()
    except Exception:
        pass
    return "WAI Agent App"


app = FastAPI(
    title=_resolve_app_title(),
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


# INC-VIG-59 — a write that couldn't get through is NOT a crash, and saying so is
# the difference between "try again" and an opaque 500 the operator can't act on.
# 503 + Retry-After is also the honest signal for a client: nothing was saved, and
# retrying is the right move (a 500 means "don't bother, it's broken").
@app.exception_handler(WriteBusy)
async def _write_busy_handler(_request, exc: WriteBusy):
    logger.warning("write queue saturated: %s", exc)
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "2"},
        content={"detail": f"La base del empleado está ocupada y no pude guardar {exc}. "
                           "No se ha cambiado nada — reinténtalo en unos segundos."},
    )


# G-2 (P3): security headers middleware. The SPA renders agent
# output (markdown, structured payloads, tool results) and any of
# those surfaces could in principle host an injection. We use
# react-markdown's default escaping as the primary defence; this
# middleware is the second line. CSP is restrictive enough to
# block remote script/style/media but lenient enough that base64
# images embedded by the agent (the wai-html-export pipeline)
# render correctly.
@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    # `form-action` admits, besides its own origin, that of ITS application (12.54.0): the
    # chat link is a form the server answers with a 303 to it, and the browser applies
    # `form-action` ALSO to that redirect — without this, the click does nothing and only the
    # console tells. Only on HTML documents, which are the ones that apply it; the query is
    # cached and never raises (`app/mi_aplicacion.py`).
    form_action = "'self'"
    if "text/html" in response.headers.get("content-type", ""):
        from app.mi_aplicacion import origen as origen_de_su_aplicacion
        o = await origen_de_su_aplicacion()
        if o:
            form_action += f" {o}"
    # Don't override headers an upstream layer (ingress) already set.
    response.headers.setdefault(
        "Content-Security-Policy",
        # default-src self covers the SPA bundle.
        # data: blobs allow base64 images / fonts the agent embeds.
        # 'unsafe-inline' on style is required by MUI's emotion CSS.
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        # `api.openai.com` for the voice: the browser negotiates the connection with the
        # model by sending its offer there. The AUDIO does not go through this header —it
        # goes through WebRTC, which CSP does not govern—, but without this line the
        # negotiation is blocked and the voice dies before starting, with a network error
        # that does not say what about.
        "connect-src 'self' https://api.openai.com; "
        # blob: so the SPA's PDF/HTML previews render — the workspace viewer
        # loads files as SAME-ORIGIN blobs into an iframe. Without an explicit
        # frame-src/object-src these fall back to default-src 'self', which
        # excludes blob: → Chrome blanks the frame with "This content is
        # blocked" (v7.93.0). Remote frames stay blocked (no http(s) source).
        "frame-src 'self' blob:; "
        "object-src 'self' blob:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        f"form-action {form_action}",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # `microphone=(self)` and NOT `microphone=()`.
    #
    # `()` is "forbidden for everyone, including this origin": the browser returns
    # `NotAllowedError` WITHOUT even asking the user, so the permission dialog does not
    # even show. With the voice deployed, the symptom was "Permission denied" on a screen
    # that had just asked for the microphone — and the block was not in the browser or in
    # the system, it was in this line.
    #
    # `(self)` still denies the microphone to any embedded third party: since moreover
    # `frame-ancestors 'none'` and `X-Frame-Options: DENY` prevent this app from living in
    # a foreign iframe, what is opened here is the permission for ITSELF and nothing else.
    # The camera and geolocation stay closed: today they are not used from the web.
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(self), camera=()")
    return response

mount_mcp(app)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(manifest_router)
app.include_router(mcp_proxy_router)
app.include_router(agent_router)
app.include_router(chat_threads_router)
app.include_router(voz_router)
# Static channel sub-routes (media) MUST be registered before the dynamic
# ``/channels/{channel_id}`` router so "/channels/media" isn't captured as
# a channel id.
app.include_router(users_router)
app.include_router(channel_media_router)
app.include_router(channels_router)
# The door through which another employee talks to this one. Separate from `/mcp`
# (app-tools) on purpose: that one is our internal toolbox.
app.include_router(colleague_router)
app.include_router(mcp_connectors_router)
app.include_router(knowledge_router)
app.include_router(knowledge_docs_router)
app.include_router(automations_router)
app.include_router(notify_rules_router)
app.include_router(suppressions_router)
app.include_router(comms_feed_router)
app.include_router(capabilities_router)
app.include_router(activity_router)
app.include_router(dashboard_router)
app.include_router(domain_router)
app.include_router(data_router)
app.include_router(events_router)
app.include_router(runs_router)
app.include_router(workspace_router)
app.include_router(memory_router)
# v7.140.0 — the employee's OWN memory (what it learned about the environment)
# and the traceability of the rules that came out of it. A sibling router of
# ``memory_router``, never a section inside it: the two memories have different
# owners (a person vs the environment) and different governance.
app.include_router(agent_memory_router)
app.include_router(scheduling_router)
app.include_router(approvals_router)
app.include_router(devices_router)

if STATIC_DIR.is_dir():
    # G-5 (v7.2): immutable cache for hashed assets.
    #
    # Vite emits content-hashed filenames in ``/assets``
    # (``Operations-abc123.js``). Their content never changes — a
    # new build produces new filenames — so we can safely tell
    # browsers and CDNs to cache them for a year. This dramatically
    # reduces traffic on the SPA's startup path and, critically,
    # lets us pair it with no-cache on ``index.html`` (below) to
    # avoid the "stale chunk" failure mode where a tab caches an
    # old ``index.html`` that points to hashed assets we've since
    # deleted.
    class _ImmutableStatic(StaticFiles):
        def file_response(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
            response = super().file_response(*args, **kwargs)
            response.headers.setdefault(
                "Cache-Control", "public, max-age=31536000, immutable",
            )
            return response

    app.mount(
        "/assets",
        _ImmutableStatic(directory=str(STATIC_DIR / "assets")),
        name="assets",
    )

    # ``index.html`` and the SPA fallback must NEVER be cached
    # past validation. The browser may keep its copy but must
    # ``If-Modified-Since`` on every navigation so a deploy is
    # picked up immediately. Without this header, a tab opened
    # before a deploy keeps loading the old ``index.html`` from
    # its disk cache and tries to dynamic-import bundle hashes
    # we've already purged.
    _INDEX_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(
            str(STATIC_DIR / "index.html"),
            headers=_INDEX_HEADERS,
        )

    @app.get("/{path:path}", include_in_schema=False)
    async def spa_fallback(path: str) -> FileResponse:
        # An /api/ route that does not exist answers 404 (JSON), never the page with 200: with the page, a
        # platform call to a wrong route looks like one that works (04/11/2026, /openapi.json answered HTML and a
        # connector agent looped). The contract: wai-architecture/contratos_de_plataforma.json «si_falta».
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="No such endpoint")
        # v7.88.0 — CONFINE to STATIC_DIR. This catch-all is unauthenticated (it
        # serves the SPA), so it must never serve a file OUTSIDE /app/static: a
        # request like ``/%2e%2e/app-data/app.sqlite`` reaches here as
        # ``path="../app-data/app.sqlite"`` and ``STATIC_DIR / path`` would
        # otherwise resolve+serve the app DB / source / any readable file
        # (unauthenticated arbitrary file read). ``resolve()`` collapses ``..`` and
        # symlinks; anything not strictly under the root falls back to index.html
        # so SPA client-side routes still work.
        root = STATIC_DIR.resolve()
        candidate = (STATIC_DIR / path).resolve()
        if (candidate == root or root in candidate.parents) and candidate.is_file():
            # Non-hashed files served directly from /static (e.g.
            # favicon.ico, robots.txt). Use the same no-cache
            # rules as ``index.html`` since they don't carry a
            # content hash and may change between deploys.
            return FileResponse(str(candidate), headers=_INDEX_HEADERS)
        return FileResponse(
            str(root / "index.html"),
            headers=_INDEX_HEADERS,
        )
