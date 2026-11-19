from datetime import datetime
from sqlalchemy import (
    Column, DateTime, Integer, String, Text, ForeignKey, Index, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.app_database import AppBase


class Event(AppBase):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    fingerprint = Column(String, nullable=False, unique=True, index=True)
    # B9: correlation id assigned at detection time and threaded through
    # to AgentRun.trace_id and the structlog ``bind``. Lets an operator
    # grep one incident across event → run → tool-call logs.
    trace_id = Column(String, nullable=True, index=True)
    type = Column(String, nullable=False, index=True)
    source = Column(String, nullable=False, index=True)
    payload_json = Column(Text, nullable=False, default="{}")
    status = Column(String, nullable=False, default="PENDING", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    detected_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_attempt_at = Column(DateTime, nullable=True)
    next_attempt_at = Column(DateTime, nullable=True)
    processed_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)

    runs = relationship("AgentRun", back_populates="event")

    __table_args__ = (
        Index("ix_events_status_next", "status", "next_attempt_at"),
    )


class WatcherCursor(AppBase):
    __tablename__ = "watcher_cursors"
    watcher_name = Column(String, primary_key=True)
    last_checked_at = Column(DateTime, nullable=True)
    last_seen_value_json = Column(Text, nullable=True)


class WatcherSchedule(AppBase):
    """Operator-controlled overrides for the watchers declared in
    ``wai-app.json``. The watcher loop reads this table on every tick so
    the operator can disable a watcher or change its cadence (interval
    or cron) without redeploying the app. When no row exists, the
    watcher runs with the defaults declared in the manifest.

    ``business_description_json`` stores a per-locale operator-edited
    description in business language ({"es": "...", "en": "..."}). It
    overrides the ``business_description`` declared on the manifest,
    so on-call ops can refine "what business signal this watcher is
    monitoring" without a redeploy. Frontend reads this in the
    ``operations`` page (R-OPS-1).
    """
    __tablename__ = "watcher_schedules"
    watcher_name = Column(String, primary_key=True)
    enabled = Column(Integer, nullable=False, default=1)        # 0/1 (sqlite-friendly)
    interval_sec = Column(Integer, nullable=True)               # overrides manifest interval
    cron_expr = Column(String, nullable=True)                   # overrides manifest cron
    notes = Column(Text, nullable=True)
    business_description_json = Column(Text, nullable=True)     # JSON {lang: text} override of manifest.business_description
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class AgentSession(AppBase):
    """App-owned registry of OpenCode sessions, keyed by SCOPE.

    The autonomous agent pod is multi-user: many operators chat with it
    and many external senders write to its channels. OpenCode itself has
    no per-user/per-origin notion (sessions are anonymous handles bound
    to a single working directory), so the APP owns the mapping:

      * ``interactive:<email>``            — one session per operator,
        so each operator keeps their own conversational context and
        their own workspace folder.
      * ``channel:<channel_id>:<sender>``  — one session per channel
        ORIGIN (e.g. each email sender), so a back-and-forth thread
        with the same person keeps context across messages.

    Watcher / scheduled (system) runs have no user/origin and stay
    ephemeral — they never get a row here.

    ``workspace_subdir`` is the per-scope folder under ``/workspace``
    the agent is told to write into (``sessions/users/<h>/`` or
    ``sessions/channels/<id>/<h>/``), so artefacts produced for one
    user/origin never collide with another's. Isolation is
    organisational + identity-scoped, NOT a hard FS sandbox (every
    session runs in the same pod with the same disk access).

    Lifecycle: ``SessionManager.resolve`` reuses a fresh row, rotates a
    stale/too-long one (``message_count`` cap), and ``gc`` deletes idle
    rows (and the sidecar session) past the idle TTL.
    """
    __tablename__ = "agent_sessions"
    scope_key = Column(String, primary_key=True)
    session_id = Column(String, nullable=False)
    role = Column(String, nullable=False)            # interactive | autonomous
    user_email = Column(String, nullable=True)       # WAI identity bound to the session
    workspace_subdir = Column(String, nullable=False)
    # v7.25.0 — explicit association columns (before, only derivable by
    # parsing scope_key): the PERSON the session belongs to (directory
    # email = memory key; NULL for unmapped senders), the channel it lives
    # on (NULL for chat) and the surface. They make "user ↔ channel ↔
    # session" queryable for the sessions view and the concurrency guard.
    memory_user = Column(String, nullable=True)
    channel_id = Column(Integer, nullable=True)
    origin = Column(String, nullable=False, default="chat")  # chat | channel
    message_count = Column(Integer, nullable=False, default=0)
    # Approximate size (tokens) of the session's context window after the
    # last turn — a "is this session filling up?" indicator shown in the
    # chat header and on the Operations sessions list. NULL until the
    # first turn reports usage.
    context_tokens = Column(Integer, nullable=True)
    # v7.143.0 — seal of the prompt contract this session was PRIMED with
    # (``memory_contract.prompt_contract_stamp``). The instructions injected only once
    # —who it is, its workspace, the two memories— live in the session's history, and
    # sessions survive redeployment: without this seal, a capability added by an EVOLVE
    # only reached sessions created AFTERWARDS (the operator kept talking to yesterday's
    # employee, which denied having its own memory). If the current seal does not match,
    # the session is RE-PRIMED on the next message keeping its history. NULL = primed
    # before this existed ⇒ it is re-primed.
    prompt_contract = Column(String, nullable=True)
    status = Column(String, nullable=False, default="active")  # active (reserved for future use)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_agent_sessions_last_used", "last_used_at"),
    )


class AppSetting(AppBase):
    """Operator-editable key/value settings for the app, persisted in
    the local store so they survive restarts without a redeploy.

    Current keys:
      * ``agent_language`` — 'es' | 'en'. Language the agent must use
        for HUMAN-FACING text it produces in autonomous runs that are
        NOT channel replies: alert/recommendation/decision rows, the
        run summary and proactive notifications. Channel replies keep
        adapting to the sender's language. Default: 'es'.
    """
    __tablename__ = "app_settings"
    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScopedKnowledge(AppBase):
    """Operator-added knowledge / instructions, persisted locally and injected
    into the agent's prompt according to a SCOPE. Lets the operator teach the
    employee durable rules without a redeploy and control WHEN they apply.

    ``scope`` ∈:
      * ``all``        — every run.
      * ``autonomous`` — only background runs (watchers/cron), where the agent
                         acts on its own with no human being served.
      * ``attending``  — only when serving a human (chat or an inbound channel
                         message from a person).
      * ``user``       — only for a specific human, matched on ``user_email``
                         (the operator's email in chat, the sender for channels).

    Distinct from ``user_memory`` (facts the agent LEARNED): this is what the
    operator EXPLICITLY tells the agent to know/do.
    """
    __tablename__ = "scoped_knowledge"
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    scope = Column(String, nullable=False, default="all", index=True)  # all|autonomous|attending|user
    user_email = Column(String, nullable=True, index=True)             # required when scope == "user"
    enabled = Column(Integer, nullable=False, default=1)               # 0/1 (sqlite-friendly)
    position = Column(Integer, nullable=False, default=0)              # operator-controlled order
    created_by = Column(String, nullable=True)
    updated_by = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class KnowledgeRequest(AppBase):
    """A structured BUSINESS PARAMETER the employee needs to function — a
    threshold, a target, a cutoff — that it cannot infer from the data and so
    REQUESTS from the operator. It is the INVERSE of ``ScopedKnowledge`` (which
    the operator pushes as free text) and distinct from ``user_memory`` (facts
    the agent learned): here the agent asks for a specific, TYPED value.

    Flow: the agent calls ``request_knowledge`` (get-or-create by ``key``) and
    always gets a usable value back (the operator's answer, or its own
    ``suggested`` default) so work never stalls; the operator fills the value in
    Operations → Conocimiento; the CONFIRMED value is injected into the prompt as
    a business parameter (``scoped_knowledge.build_business_knowledge_block``).

    Governance: only a REAL human ADMIN sets the value that governs behaviour —
    via the UI, or in chat via ``set_knowledge``. A member (or the agent on a
    background run) can only PROPOSE (``proposed_value``); the proposal never
    overwrites ``value`` until an admin confirms it. This mirrors the platform's
    anti-escalation rule (the agent path can never self-escalate to admin).
    """
    __tablename__ = "knowledge_requests"
    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, nullable=False, unique=True, index=True)   # stable slug the agent reads
    label = Column(String, nullable=False)                          # human title (Spanish, primary)
    question = Column(Text, nullable=False, default="")             # what / why the agent needs it (Spanish)
    # Same two fields in English. The UI shows label/question or these by the
    # viewer's active locale (i18n.language). The agent passes BOTH when it calls
    # ``request_knowledge`` so the parameter is legible to any operator; when the
    # English pair is absent we fall back to the Spanish one on read.
    label_en = Column(String, nullable=True)                        # human title (English)
    question_en = Column(Text, nullable=True)                       # what / why the agent needs it (English)
    value_type = Column(String, nullable=False, default="text")     # text|number|boolean|list|date
    unit = Column(String, nullable=True)                            # €, days, % …
    options_json = Column(Text, nullable=True)                      # JSON array of choices (list type)
    suggested = Column(String, nullable=True)                       # value the agent uses meanwhile
    value = Column(String, nullable=True)                           # CONFIRMED value (null ⇒ pending)
    status = Column(String, nullable=False, default="pending", index=True)  # pending|provided
    origin = Column(String, nullable=False, default="agent")        # agent|operator (who created it)
    # A proposal from a non-admin (chat) awaiting an admin's confirmation. It
    # NEVER overwrites ``value`` on its own.
    proposed_value = Column(String, nullable=True)
    proposed_by = Column(String, nullable=True)
    proposed_at = Column(DateTime, nullable=True)
    requested_by_run_id = Column(Integer, nullable=True)
    requested_at = Column(DateTime, nullable=True)
    provided_by = Column(String, nullable=True)
    provided_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class PendingCostReport(AppBase):
    """Durable retry buffer for tenant billing rows that failed to POST.

    Cost reporting is fire-and-forget (a billing failure must never break a
    run), but a transient genai-api outage shouldn't silently LOSE the spend
    either. When the POST fails we stash the payload here and drain it
    opportunistically on the next successful report + from housekeeping.
    """
    __tablename__ = "pending_cost_reports"
    id = Column(Integer, primary_key=True, autoincrement=True)
    payload_json = Column(Text, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    last_attempt_at = Column(DateTime, nullable=True)


class AgentPersona(AppBase):
    """Operator-editable "AI employee" identity, persisted locally so it
    survives restarts and can be changed without a redeploy.

    The manifest (``wai-app.json`` → ``agent_app.persona``) ships the
    INITIAL identity at generation time. This table stores per-field
    OVERRIDES the operator sets from the Operations → "Identidad del
    empleado IA" card. The manifest endpoint merges them field-by-field
    (override wins; absent fields fall back to the manifest), exactly
    like ``WatcherSchedule`` / ``ActionOverride`` do for their copy.

    Single-row table keyed by a constant ``id='default'`` (each app pod
    represents one employee). Locale-aware fields (role/tone/bio/traits/
    greeting) are stored as JSON text so a single column can hold either
    a plain string or a ``{"es": "...", "en": "..."}`` dict, matching the
    manifest persona contract.
    """
    __tablename__ = "agent_persona"
    id = Column(String, primary_key=True, default="default")
    name = Column(String, nullable=True)
    role_json = Column(Text, nullable=True)        # str | {lang: str}
    tone_json = Column(Text, nullable=True)        # str | {lang: str}
    bio_json = Column(Text, nullable=True)         # str | {lang: str}
    traits_json = Column(Text, nullable=True)      # JSON list[str | {lang: str}]
    greeting_json = Column(Text, nullable=True)    # str | {lang: str}
    # Avatar / "icono". ``emoji`` is the simplest assignable icon (e.g.
    # "🔧"); ``image_url`` allows a mascot path/URL/data-URI; ``color``
    # seeds the monogram gradient. When all are empty the SPA shows the
    # persona's first initial (small avatars) or the built-in mascot.
    avatar_emoji = Column(String, nullable=True)
    avatar_color = Column(String, nullable=True)
    avatar_image_url = Column(Text, nullable=True)
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ActionOverride(AppBase):
    """Operator-edited overrides for ``event_type_specs[]`` (the
    "actions" tab in Operations). Today we only persist a per-locale
    business description so an operator can rewrite the action card
    text without redeploying. Same shape as WatcherSchedule's
    business_description_json. Keyed by event_type (i.e. spec.type).
    """
    __tablename__ = "action_overrides"
    event_type = Column(String, primary_key=True)
    business_description_json = Column(Text, nullable=True)
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatThread(AppBase):
    """A named conversation. DURABLE (v9.1.0).

    The thread and the OpenCode session are TWO objects, and that separation is the whole
    design:

      · The **thread** is this: a name and an identity. Only a person deletes it.
      · The **session** (``AgentSession``) is the live context. It rotates every N messages
        and the collector picks it up on inactivity, as always.

    Without separating them, the collector would delete named conversations and people would
    call it —rightly— data loss. With them separated, what expires is the agent's short-term
    memory; the thread stays in place and is re-primed when writing in it again.

    A thread belongs to ONE person: it is not shared. What all the threads of the same person
    do share is their file folder and their memory — that was already so and does not change.
    """
    __tablename__ = "chat_threads"
    id = Column(String, primary_key=True)
    user_email = Column(String, nullable=False, index=True)
    # v12.0.0 — this person's DEFAULT conversation is a REAL row like any other, with its
    # own id per user (`hilo_semilla_de(email)`), and this marks it. Before it was a single
    # row with the literal id "default" for THE WHOLE pod: since `id` is the only PK, only
    # the first user got to have it (the rest of the INSERTs failed and were swallowed) and
    # their turns, with a shared `thread_id='default'`, crossed between people. One per
    # person (partial unique index in `main._ensure_indexes`). The client can still send the
    # alias "default": the server resolves it to the caller's row.
    is_default = Column(Integer, nullable=False, default=0)
    # Functional name. It is set on creation with the first words (cost 0, and that way there
    # is NEVER a thread without a name) and the model improves it after the first exchange.
    title = Column(String, nullable=False, default="")
    # When someone renames it by hand, the model never touches it again.
    title_locked = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    # Archived: out of the list, without losing the history. Deleted: out completely.
    archived_at = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)
    # v12.4.0 — when it was last compacted. Compacting summarises the thread INSIDE the pod:
    # the model stops remembering in detail what came before this mark, but the conversation
    # is still whole on screen. It lives HERE, and not in each client, because it is a
    # property of the CONVERSATION: if each one noted it on its own, the mobile app and the
    # web would not agree on where the employee lost the detail —and the one that did not
    # compact would never know—. NULL = never compacted.
    compacted_at = Column(DateTime, nullable=True)


class AgentRun(AppBase):
    __tablename__ = "agent_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    # B9: stable correlation id threaded through event → run → tool
    # call → log so an operator can grep all artifacts of one
    # incident in one query. Defaults to ``idempotency_key`` for
    # autonomous runs and a fresh uuid for chat runs.
    trace_id = Column(String, nullable=True, index=True)
    # v12.0.0 — SECURITY. The identifier that travels to the MODEL in the `[INTERNO]` line
    # and with which the tools resolve "which run is this". Random (128 bits) and IMPOSSIBLE
    # to guess: the autoincrement `id` could be guessed ("use run_id=1233") and with it a
    # person read/wrote another's memory, opened admin tools if that run was an admin's, and
    # signed a `wai_app` connector as another person. The integer `id` is still the INTERNAL
    # key; this is only the boundary with the model. NULL in earlier runs.
    run_token = Column(String, nullable=True, index=True)
    opencode_session_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default="RUNNING", index=True)
    mode = Column(String, nullable=False, default="autonomous", index=True)  # 'autonomous' | 'chat'
    # B2: retries UPDATE the same row in place. ``attempts`` is the
    # snapshot of the Event.attempts counter at the time of (re)start
    # so the Run-detail page can show "attempt 2 of 3".
    attempts = Column(Integer, nullable=False, default=1)
    # v9.1.0 — which THREAD this turn belongs to. Rotation changes the
    # ``opencode_session_id`` in the middle of a conversation, so the session is NOT useful
    # to rebuild a thread: this own identifier is needed. Earlier rows stay NULL and that
    # is correct —before there was only one conversation per person—, so NULL reads as
    # "that user's default thread".
    thread_id = Column(String, nullable=True, index=True)
    prompt = Column(Text, nullable=False)
    request_meta_json = Column(Text, nullable=True)   # JSON: provider_id, model_id, system_prompt_hash, parts_count, idempotency_key
    response_md = Column(Text, nullable=True)
    structured_json = Column(Text, nullable=True)     # JSON tail parsed by structured_response.parse_structured_tail
    tokens_in = Column(Integer, nullable=True)
    tokens_out = Column(Integer, nullable=True)
    cost_usd = Column(String, nullable=True)
    model = Column(String, nullable=True)
    duration_sec = Column(Integer, nullable=True)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)
    # v12.7.0 — WHO asked for this and THROUGH WHERE. It is recorded HERE, at the moment,
    # and not deduced later, because the two sources it would have to be deduced from get
    # lost: the `opencode_session_id` ROTATES mid-conversation (that is why `thread_id`
    # exists) and the `agent_sessions` rows are COLLECTED after idleness. A run from last week
    # was left with nothing to look at.
    #
    # `solicitante` is always "who asked for it", whatever the surface: the person's email in
    # the chat, the SENDER's ADDRESS in a channel (which often matches no WAI user) and the
    # colleague that called. NULL = nobody asked for it, the employee acted on its own.
    solicitante = Column(String, nullable=True, index=True)
    # chat | canal | automatizacion | vigia | programado | companero. NULL in rows prior to
    # this version: it cannot be backfilled without inventing it.
    origen = Column(String, nullable=True, index=True)
    # Only when `origen == "canal"`. The two loose fields are stored —not the painted
    # sentence— to be able to filter by type and to link to the specific channel.
    canal_kind = Column(String, nullable=True)        # email | whatsapp | ...
    canal_id = Column(Integer, nullable=True)
    # The USE of a capability (docs/red-de-integraciones.md): which capability, which declared use of the caller (an
    # application or another employee, see `origen`) and its arguments as data. This run IS the audit of that call;
    # the platform keeps only an index pointing here.
    capacidad = Column(String, nullable=True, index=True)
    uso = Column(String, nullable=True)
    argumentos_json = Column(Text, nullable=True)

    # v6.45.0: deliverable files written to /workspace/exports/ during the
    # run (mtime within the run window). JSON list of
    # {"filename","workspace_path","size"} so the SPA can offer a direct
    # download from the run / encargo / calendar item. NULL when none.
    artifacts_json = Column(Text, nullable=True)

    event = relationship("Event", back_populates="runs")
    tool_calls = relationship("ToolCallLog", back_populates="run")
    tool_uses = relationship("ToolUseLog", back_populates="run")


class ToolUseLog(AppBase):
    """EVERYTHING the employee touched in a run, not only what it wrote.

    It exists because a run's «Herramientas» tab showed ONE line when the employee had made
    six calls. It read `tool_call_log`, which is something else: the IDEMPOTENCY ledger of
    the app's own writes —so as not to repeat an `upsert_alert` on retry— and by construction
    it knows nothing about what the agent queried outside. The result was a manager looking
    at "one tool" while the employee questioned the PMS and a colleague. Measured on
    18/08/2026 in the hotel workshop: 6 real calls, 1 on screen.

    Here what is SEEN going by is recorded: each invocation with its server, its status and
    how long it took. It is a record, not control — nothing here decides whether something
    runs, so a failure writing cannot bring a turn down.

    One row per invocation, identified by `call_id`: opencode re-emits the same call as it
    progresses (pending → running → finished) and without that key three would come out.
    """
    __tablename__ = "tool_use_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("agent_runs.id"), nullable=False, index=True)
    # The identifier opencode gives the invocation. Empty only if it did not send it.
    call_id = Column(String, nullable=False, default="")
    step_idx = Column(Integer, nullable=False, default=0)
    tool_name = Column(String, nullable=False)
    # Which connector it comes from, derived from the `{server}_{tool}` prefix. It is stored
    # computed and not deduced when painting: whoever looks at the screen wants to see
    # "hotel-pms" without having to know the convention.
    server = Column(String, nullable=True)
    status = Column(String, nullable=False, default="running")  # running | ok | error
    args_json = Column(Text, nullable=True)
    result_json = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)

    run = relationship("AgentRun", back_populates="tool_uses")

    __table_args__ = (
        UniqueConstraint("run_id", "call_id", name="uq_tool_use_run_call"),
    )


class ToolCallLog(AppBase):
    __tablename__ = "tool_call_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("agent_runs.id"), nullable=False, index=True)
    step_idx = Column(Integer, nullable=False)
    tool_name = Column(String, nullable=False)
    arguments_hash = Column(String, nullable=False)
    idempotency_key = Column(String, nullable=False, unique=True)
    result_hash = Column(String, nullable=True)
    result_json = Column(Text, nullable=True)
    executed_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    run = relationship("AgentRun", back_populates="tool_calls")

    # NOTE: dedup is keyed EXCLUSIVELY on ``idempotency_key`` (unique
    # above), which encodes mode:run:step:tool:args_hash. A secondary
    # UNIQUE on (run_id, step_idx) would contradict that contract (B5):
    # chat and autonomous flows may share run_id/step_idx and the same
    # step may legitimately call different tools or args.


class EventAudit(AppBase):
    """Forensic trail for manual operations on events (reprocess /
    cancel / force-fail). Required by the events detail page; without
    it the operator cannot answer *who* moved the event back to PENDING.
    See ``patterns/events-page.md`` for the routes that write here."""
    __tablename__ = "event_audit"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    event_id     = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    actor_email  = Column(String,  nullable=False)
    action       = Column(String,  nullable=False)   # reprocess | cancel | force_fail
    from_status  = Column(String,  nullable=False)
    to_status    = Column(String,  nullable=False)
    note         = Column(Text,    nullable=True)
    at           = Column(DateTime, nullable=False, default=datetime.utcnow)


class EntityComment(AppBase):
    """Operator (and agent) comment thread for a LOCAL domain-entity ROW.

    Decouples notes from the physical entity table: any entity with a
    lifecycle (alerts, recommendations, or any manifest entity that declares
    one) gets a thread keyed by ``(entity_slug, entity_pk)`` without altering
    the agent-written schema. ``from_status``/``to_status`` are filled when the
    comment accompanies a lifecycle transition, so the thread reads as an
    annotated audit log of WHY each state change happened. See
    ``routes/domain_routes.py`` (the comments endpoints + the transition that
    records one)."""
    __tablename__ = "entity_comments"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    # Domain entity slug (e.g. "alert", "recommendation") + the row PK as TEXT
    # (entity PKs may be ints or strings; we compare as text everywhere).
    entity_slug  = Column(String,  nullable=False, index=True)
    entity_pk    = Column(String,  nullable=False, index=True)
    author       = Column(String,  nullable=False, default="")   # operator email | "agent"
    body         = Column(Text,    nullable=False, default="")
    # Set ONLY when the comment was attached to a state change.
    from_status  = Column(String,  nullable=True)
    to_status    = Column(String,  nullable=True)
    created_at   = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_entity_comments_entity", "entity_slug", "entity_pk"),
    )


class PendingToolRequest(AppBase):
    """A write-with-approval MCP request the agent made and the policy proxy
    PARKED (v7.29.4). We store the EXACT original JSON-RPC request body so, on
    approval, we REPLAY it verbatim to the upstream — never hand-building a
    Composio call (whose meta-tool schema we can't verify offline). The
    ``request_ref`` (a hash of the body) is the integrity binding: an approval
    can only execute the exact request it references, and the connector must
    match. Rows are consumed on execution and TTL-swept if never approved."""
    __tablename__ = "pending_tool_requests"
    request_ref = Column(String, primary_key=True)     # sha256(body)[:32]
    connector   = Column(String, nullable=False, index=True)
    tool_slugs  = Column(String, nullable=True)         # comma-joined, for display
    raw_body    = Column(Text,   nullable=False)        # the verbatim JSON-RPC request
    created_at  = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class RecentToolExecution(AppBase):
    """Loop-guard (v7.34.0): when an approved connector write is REPLAYED
    (executed) on approval, we record its metadata-stable ``signature`` +
    result here for a short TTL. If the agent RE-ATTEMPTS the same write in the
    APPROVAL_GRANTED follow-up run (which would re-park → loop), the proxy finds
    this row and returns the cached result instead of parking again."""
    __tablename__ = "recent_tool_executions"
    signature  = Column(String, primary_key=True)      # mcp_policy.write_signature
    connector  = Column(String, nullable=False, index=True)
    result     = Column(Text,   nullable=True)         # JSON of the tool result
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class WriteGrant(AppBase):
    """Run-scoped coalescence grant (v7.35.0): when the operator approves an
    automation REACTION (Gate 2 / initiative), the writes that reaction makes
    must NOT raise the connector write gate (Gate 1) AGAIN — that would ask
    twice for one logical action. So approving mints one grant; the executor
    binds it to the pre-approved reaction RUN's session; the trusted sidecar
    plugin injects it (``__wai_write_grant``) into that session's connector tool
    calls; and the proxy honours it (allows a ``write_with_approval`` write for
    that run only — ``read_only`` still denies). The LLM never sees or sets it
    (same trust boundary as ``acting_user``). Short-lived + TTL-swept."""
    __tablename__ = "write_grants"
    grant_id   = Column(String, primary_key=True)      # opaque nonce
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
