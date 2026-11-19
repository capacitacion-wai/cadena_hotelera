"""Business-shaped local tables — the OUTPUTS the agent produces.

This module is part of the ``wai-agent-app-builder`` SKILL (v6.4.0).
It ships canonical templates for the four output kinds every action-
taking agent eventually needs:

* ``Alert``           — operational signals (high severity events, SLA
                        breaches, anomalous KPI deltas, …).
* ``Recommendation``  — concrete suggested next steps with rationale,
                        priority and a target entity.
* ``Communication``   — drafted / sent messages to humans or external
                        systems (email, Slack, ticketing).
* ``Scoring``         — periodic per-target scores (customer_churn,
                        product_health, lead_temperature, …).
* ``Decision``        — explicit "agent considered acting and decided
                        not to" log so an operator can audit silence.

Every model carries the same audit triplet:

    id                INTEGER PK
    idempotency_key   TEXT UNIQUE  — sha256(run_id, step_idx, payload)
    created_by_run_id INTEGER FK → agent_runs.id (nullable, manual ops)
    created_at        DATETIME default now

The MCP tools in ``app/mcp_server.py`` write here (``upsert_alert``,
``register_recommendation``, ``log_communication``, ``upsert_scoring``,
``log_decision``, …) and read back through the matching ``list_*`` tools
(``list_alerts``, …) — every canonical entity has BOTH a write and a read
tool so the agent can answer about its own state, not just write. The SPA
also reads via the generic ``/api/domain/<slug>`` endpoint when the table
is declared under ``domain_entities`` with ``source: "local"``.

These seven are PLATFORM SUBSTRATE / examples — NOT the employee's
business entities. ``Alert``/``Recommendation``/``Scoring``/``Decision`` are
generic placeholders; ``ScheduledItem``/``Approval``/``Communication`` are
plumbing behind calendar/approvals/outbound. A real employee's entities are
DOMAIN-SHAPED and 100% business (e.g. ``risk_invoices``, ``detected_oppor-
tunities``, ``resolved_tickets``) and are DERIVED FROM the objective + the
semantic layer at generation (see ``patterns/step-0-design-proposal.md`` and
``patterns/domain-entity-page.md``). Do NOT ship an employee whose only
entities are the generic substrate — the verifier warns on it (F2). Rename/
replace these examples with the employee's real business objects, keeping the
audit triplet; ``ScheduledItem``/``Approval`` may stay as-is (substrate).

Custom / per-agent entities
---------------------------
The set of local entities is NOT fixed: an agent may define its OWN
business tables beyond the seven above. When it does, keep the agent
conscious of them by following the contract in
``patterns/mcp-server.md`` → "Custom / variable entities":

* ALWAYS declare the new table under ``domain_entities``
  (``source: "local"``) in ``wai-app.json`` — that is the single source
  of truth. ``app.app_context.build_data_catalogue`` and the
  ``describe_entities`` MCP tool render the awareness from it, so it never
  has to be hand-maintained.
* If the entity deserves first-class tools, add a model here + a
  ``BUSINESS_MODEL_REGISTRY`` entry + a named write/read tool in
  ``mcp_server`` (and an ``app_context._LOCAL_TOOLS`` mapping). Otherwise
  it is reached generically with ``local.write`` / ``local.read``.

The generator is **encouraged to keep the columns minimal and
domain-shaped** — extend the table with extra business columns when
the user needs them (e.g. ``Alert.affected_revenue_usd`` for an
ecommerce agent). Adding columns is a no-op as long as
``Base.metadata.create_all`` runs at startup (it does — see
``main.py``).
"""

from datetime import datetime
from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from app.app_database import AppBase


class Alert(AppBase):
    """An operational signal raised by the agent: a RISK/incident with evidence.

    v7.41.0 — the alert carries the SEPARATE policy axes (``impact``, ``urgency``,
    ``confidence``); its ``severity`` (the risk band) is DERIVED from impact ×
    urgency by ``app.signal_policy`` (downgraded when confidence is low) so alert
    intensity is a single, auditable source of truth — the agent may override it
    explicitly, recorded in ``metadata_json.severity_override``. Semantic dedup
    (``dedupe_key`` + ``root_cause``) makes a recurring problem UPDATE the same
    alert (bumping ``occurrences`` / ``last_seen_at``) instead of piling up rows.
    All axis values are constrained by the MCP tool, not the DB, so new tiers are
    a one-line policy change with no Alembic migration.
    """
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    created_by_run_id = Column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    severity = Column(String, nullable=False, default="medium", index=True)  # DERIVED (policy)
    # v7.41.0 separated policy axes (the agent writes these; severity is derived):
    impact = Column(String, nullable=True, index=True)       # low|medium|high|critical
    urgency = Column(String, nullable=True, index=True)      # low|medium|high|immediate
    confidence = Column(String, nullable=True, index=True)   # low|medium|high
    title = Column(String, nullable=False)
    description = Column(Text, nullable=False, default="")
    source_event_id = Column(
        Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    target_entity_kind = Column(String, nullable=True)   # e.g. "order", "customer"
    target_entity_id = Column(String, nullable=True)
    evidence_json = Column(Text, nullable=False, default="{}")
    # v7.41.0 semantic dedup: a stable key + the underlying cause; when the same
    # problem reappears the MCP tool UPDATES the matching open alert instead of
    # inserting a duplicate, bumping the occurrence counter and the seen stamps.
    dedupe_key = Column(String, nullable=True, index=True)
    root_cause = Column(String, nullable=True)
    occurrences = Column(Integer, nullable=False, default=1)
    first_seen_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    # v7.40.0: trace bag — when the alert is converted to an encargo (to_task) we
    # record {"encargo_id", "outcome": {...}} so the card links to it and blocks a
    # second conversion. v7.41.0 also holds {"severity_override": ...} when set.
    metadata_json = Column(Text, nullable=False, default="{}")
    # v7.40.0 unified lifecycle: open → acknowledged → in_progress → done ; dismissed.
    status = Column(String, nullable=False, default="open", index=True)
    resolved_at = Column(DateTime, nullable=True)
    resolved_by = Column(String, nullable=True)

    __table_args__ = (
        Index("ix_alerts_status_created", "status", "created_at"),
        Index("ix_alerts_severity_created", "severity", "created_at"),
        Index("ix_alerts_dedupe_status", "dedupe_key", "status"),
    )


class Recommendation(AppBase):
    """A concrete suggested next step (an ACTION) the agent surfaces to a human.

    v7.41.0 — carries the same separated policy axes as an alert (``impact``,
    ``urgency``, ``confidence``); its ``priority`` is DERIVED from impact ×
    urgency by ``app.signal_policy`` (downgraded when confidence is low), so a
    recommendation's importance is computed the SAME way an alert's severity is.
    ``expected_impact`` stays as the free-text business headline (e.g. "recupera
    3 pedidos"); ``impact`` is the structured band the policy consumes. Its
    acceptance/execution state is the unified lifecycle ``status``.
    """
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    created_by_run_id = Column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    title = Column(String, nullable=False)
    rationale_md = Column(Text, nullable=False, default="")    # markdown
    priority = Column(String, nullable=False, default="medium", index=True)  # DERIVED (policy)
    # v7.41.0 separated policy axes (the agent writes these; priority is derived):
    impact = Column(String, nullable=True, index=True)       # low|medium|high|critical
    urgency = Column(String, nullable=True, index=True)      # low|medium|high|immediate
    confidence = Column(String, nullable=True, index=True)   # low|medium|high
    target_entity_kind = Column(String, nullable=True)
    target_entity_id = Column(String, nullable=True)
    expected_impact = Column(String, nullable=True)            # short business label
    metadata_json = Column(Text, nullable=False, default="{}")
    # v7.40.0 unified lifecycle: open → acknowledged → in_progress → done ; dismissed.
    status = Column(String, nullable=False, default="open", index=True)
    decided_at = Column(DateTime, nullable=True)
    decided_by = Column(String, nullable=True)
    # v7.100.0 — semantic dedup, SAME as alerts: a recurring suggestion (same
    # ``dedupe_key`` — falls back to ``target_kind:target_id``) UPDATES the open
    # recommendation and bumps ``occurrences`` instead of piling up a new card
    # every run. Before this, ``register_recommendation`` had NO dedup at all and
    # every autonomous pass duplicated the same "revisar reposición…" row.
    dedupe_key = Column(String, nullable=True, index=True)
    occurrences = Column(Integer, nullable=False, default=1)
    first_seen_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_recommendations_dedupe_status", "dedupe_key", "status"),
    )


class Communication(AppBase):
    """A drafted or sent message to a human (or external system).

    The agent populates this row whenever it would otherwise need to
    send an email / Slack / ticket on the operator's behalf. Sending
    is a separate step (a downstream worker reads ``status=='ready'``
    rows and dispatches them) — so the agent stays observable and the
    operator can review before fan-out.
    """
    __tablename__ = "communications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    created_by_run_id = Column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    channel = Column(String, nullable=False, index=True)       # email|slack|ticket|webhook|sms
    recipient = Column(String, nullable=False)                 # email / slack handle / system id
    subject = Column(String, nullable=True)
    body_md = Column(Text, nullable=False, default="")
    status = Column(String, nullable=False, default="draft", index=True)  # draft|ready|sent|failed
    sent_at = Column(DateTime, nullable=True)
    delivery_id = Column(String, nullable=True)                # carrier-side id once sent
    error = Column(Text, nullable=True)
    # Optional files to deliver with the message. JSON list of
    # {filename, workspace_path|url|content_base64, mime_type?} — same
    # shape as reply.attachments, so a 'ready' communication can carry a
    # real attachment (a generated PPTX/PDF/CSV in /workspace/exports/).
    attachments_json = Column(Text, nullable=True)


class Scoring(AppBase):
    """Periodic per-target score (e.g. customer churn risk).

    Idempotency is keyed on (target_kind, target_id, metric, window) so
    a re-run on the same window updates the score in place rather than
    inserting duplicate rows.
    """
    __tablename__ = "scoring"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    created_by_run_id = Column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    target_kind = Column(String, nullable=False, index=True)
    target_id = Column(String, nullable=False, index=True)
    metric = Column(String, nullable=False, index=True)        # churn_risk|health|lead_temp|...
    window_days = Column(Integer, nullable=False, default=30)
    value = Column(Float, nullable=False)
    band = Column(String, nullable=True)                       # low|medium|high|critical (optional)
    explanation_md = Column(Text, nullable=False, default="")
    metadata_json = Column(Text, nullable=False, default="{}")

    __table_args__ = (
        UniqueConstraint(
            "target_kind", "target_id", "metric", "window_days",
            name="uq_scoring_target_metric_window",
        ),
        Index("ix_scoring_metric_created", "metric", "created_at"),
    )


class Decision(AppBase):
    """An explicit "I evaluated this and I am NOT acting now, and why" log.

    Written by ``log_decision`` whenever the agent decides how to treat a
    signal — including deciding it does NOT warrant acting. Two separate axes:
    ``decision`` is the coarse DISPOSITION (no_action | deferred | escalated);
    ``reason_code`` (v7.41.0) is the STRUCTURED, domain-agnostic reason from the
    ``signal_policy.REASON_CODES`` catalog (below_threshold, insufficient_evidence,
    duplicate, out_of_scope, needs_human, already_handled, deferred_timing,
    awaiting_data, no_action_needed …). ``ref_kind``/``ref_id`` point at the
    finding evaluated or the action avoided (e.g. the alert it looked at, or the
    kind of recommendation it chose NOT to make). The v7.40.0 ``status`` is the
    operator-facing LIFECYCLE shared with alerts and recommendations, so a
    decision can be triaged, converted to an encargo and closed just like them.
    """
    __tablename__ = "decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    created_by_run_id = Column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    source_event_id = Column(
        Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    decision = Column(String, nullable=False)                  # disposition: no_action|deferred|escalated
    # v7.41.0 structured reason + confidence + reference to the finding/avoided action:
    reason_code = Column(String, nullable=True, index=True)    # signal_policy.REASON_CODES
    confidence = Column(String, nullable=True, index=True)     # low|medium|high
    ref_kind = Column(String, nullable=True)                   # e.g. "alerts" | "recommendation"
    ref_id = Column(String, nullable=True)                     # id/label of the finding evaluated
    reason_md = Column(Text, nullable=False, default="")
    metadata_json = Column(Text, nullable=False, default="{}")
    # v7.40.0 unified lifecycle: open → acknowledged → in_progress → done ; dismissed.
    status = Column(String, nullable=False, default="open", index=True)


class ScheduledItem(AppBase):
    """Unified time-anchored work item — the substrate behind the
    Calendar, the "Encargos" (delegated tasks) and the agent's own
    follow-ups / self-scheduled work.

    ONE table backs several product surfaces, discriminated by two axes:

    * ``assignee`` — WHO must do it: ``agent`` (the AI employee) or
      ``user`` (a human; a reminder/task the agent put on someone's
      calendar).
    * ``origin`` — WHY it exists: ``user`` (a person delegated it),
      ``agent`` (the employee self-scheduled future work) or
      ``followup`` (a loose end the employee will revisit).

    Time policy: ``due_at`` / ``start_at`` / ``end_at`` are stored in
    **UTC** (naive-UTC, like the rest of the app) and rendered in the
    business timezone by the SPA. The scheduled-items watcher fires a
    ``SCHEDULED_TASK_DUE`` event when an agent-assigned item passes
    ``due_at``; user-assigned items just flip to ``overdue``.
    """
    __tablename__ = "scheduled_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    created_by_run_id = Column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True,
    )
    # v7.140.0 — the event that caused it, when there is one. Same pattern as
    # Alert/Decision, and it exists for one reason: a DETERMINISTIC automation
    # reaction (0 tokens) creates the Encargo without a run, so without this the
    # provenance chain broke and "what did this standing order produce?" simply
    # missed those items instead of admitting it.
    source_event_id = Column(
        Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    title = Column(String, nullable=False)
    description = Column(Text, nullable=False, default="")
    # task | followup | event | reminder
    kind = Column(String, nullable=False, default="task", index=True)
    # agent | user
    assignee = Column(String, nullable=False, default="agent", index=True)
    # user | agent | followup
    origin = Column(String, nullable=False, default="user", index=True)
    # pending | scheduled | in_progress | done | cancelled | overdue
    status = Column(String, nullable=False, default="pending", index=True)
    priority = Column(String, nullable=False, default="medium")  # low|medium|high|urgent

    # When the item is DUE / scheduled (UTC). ``due_at`` drives the
    # watcher; ``start_at``/``end_at`` describe a calendar block.
    due_at = Column(DateTime, nullable=True, index=True)
    start_at = Column(DateTime, nullable=True)
    end_at = Column(DateTime, nullable=True)
    all_day = Column(Integer, nullable=False, default=0)  # 0/1
    # Optional 5-field cron for a recurring item (evaluated in business tz).
    recurrence = Column(String, nullable=True)

    created_by = Column(String, nullable=True)     # email of the human who created it
    assigned_to = Column(String, nullable=True)    # email of the human it is for (user tasks)

    # Bookkeeping when the watcher fires / the run closes the item.
    fired_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    result_md = Column(Text, nullable=True)        # agent's closing note
    last_run_id = Column(Integer, nullable=True)   # AgentRun that handled it
    # v6.45.0: deliverable files produced by last_run_id, mirrored from the
    # run so the board/calendar can offer a direct download. JSON list of
    # {"filename","workspace_path","size"}; NULL when none.
    artifacts_json = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=False, default="{}")

    __table_args__ = (
        Index("ix_scheduled_status_due", "status", "due_at"),
        Index("ix_scheduled_assignee_status", "assignee", "status"),
    )


class Approval(AppBase):
    """A consequential action the AI employee proposes and parks for an
    operator's go/no-go before it is carried out.

    The agent creates one with the ``request_approval`` tool instead of
    acting directly when an action is risky/irreversible or above its
    delegated authority. The operator approves (→ the app emits an
    ``APPROVAL_GRANTED`` event so the agent actually executes it) or
    rejects with feedback (→ recorded to the requester's memory so the
    employee learns the boundary). Status is OPERATOR-owned.
    """
    __tablename__ = "approvals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    created_by_run_id = Column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    title = Column(String, nullable=False)
    summary_md = Column(Text, nullable=False, default="")     # what + why, for the operator
    action_type = Column(String, nullable=False, index=True)  # free label: send_email|issue_refund|...
    action_payload_json = Column(Text, nullable=False, default="{}")  # everything needed to execute
    risk = Column(String, nullable=False, default="medium")   # low|medium|high
    requested_for = Column(String, nullable=True)             # email the action concerns / requester
    # pending|approved|rejected|executed|failed. v7.85.0 — ``failed`` is set by the
    # executor when the run that was to CARRY OUT an approved action permanently
    # fails, so the operator sees pending → approved → failed instead of an approval
    # stuck at ``approved`` forever (the durable Event retry already happened).
    status = Column(String, nullable=False, default="pending", index=True)
    decided_by = Column(String, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    feedback = Column(Text, nullable=True)                    # operator note on reject/approve
    # Closure of the loop: once the agent actually CARRIES OUT an approved
    # action (via ``mark_approval_executed``), the approval flips to
    # ``executed`` so an operator can tell "approved" from "actually done".
    executed_at = Column(DateTime, nullable=True)
    executed_by_run_id = Column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True,
    )
    execution_result = Column(Text, nullable=True)            # short note of what was done

    __table_args__ = (
        Index("ix_approvals_status_created", "status", "created_at"),
    )


# ─────────────────────────────────────────────────────────────────────
# Public catalogue used by mcp_server.py to map a tool name to its
# target ORM class. Keeping this in one place makes it easy for the
# generator to add a new domain-shaped output (e.g. ``Lead``,
# ``WelcomeQueue``) without touching the MCP routing logic — append
# the new model and a matching tool in ``mcp_server.TOOLS``.
# ─────────────────────────────────────────────────────────────────────

BUSINESS_MODEL_REGISTRY: dict[str, type] = {
    "alerts":          Alert,
    "recommendations": Recommendation,
    "communications":  Communication,
    "scoring":         Scoring,
    "decisions":       Decision,
    "scheduled_items": ScheduledItem,
    "approvals":       Approval,
}
