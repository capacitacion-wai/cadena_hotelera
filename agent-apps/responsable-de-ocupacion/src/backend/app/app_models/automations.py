"""Automations — the boss's standing orders to the employee.

One row = ``WHEN <signal> → THEN <reaction>`` with business-tunable
config. The LLM translates the boss's natural-language order into a STRUCTURED
automation ONCE (at authoring, via chat or the form); at runtime the watcher
evaluates the structured signal DETERMINISTICALLY (cheap, no per-tick LLM) and
the executor runs the configured reaction over the existing rails (approvals,
scheduled-items, notifier, channels).

Two tables: the definition (``automations``) and the evaluation state
(``automation_state``) kept apart so cooldown / re-arm / live-value /
diagnostics never touch the operator-edited definition.
"""

from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from app.app_database import AppBase


class Automation(AppBase):
    """A standing order: when a signal fires, run a reaction."""
    __tablename__ = "automations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    # v7.138.0: MANDATORY functional description (HTML) — WHEN (signal), the "only if" and WHAT it does.
    # Nullable in the DB (legacy rows still boot); REQUIRED by the API on create/edit.
    description = Column(Text, nullable=True)
    # v7.139.0: source template (its `key`) to mark "already added" and avoid duplicates. NULL
    # if it was created by hand/through chat.
    template_key = Column(String, nullable=True, index=True)
    # v7.151.0 — ORIGIN SEAL. Templates come from the DEPLOYMENT (files
    # ``automation_templates.{es,en}.json`` baked into the image) and nobody edits them:
    # neither the UI nor the agent has any way to write them. What did need sealing is
    # the PROVENANCE of each row: `template_key`/`template_version`/`derived_from_id` are
    # fixed on creation and CANNOT be rewritten afterwards (see `_validate`), so that an
    # edit cannot pass an automation off as something it is not.
    template_version = Column(String, nullable=True)
    # Which EXISTING automation this one evolves from. It is the allowed path when you want
    # to change something that came from the deployment: you do not modify it, you create a derived one.
    derived_from_id = Column(Integer, nullable=True, index=True)
    area_tag = Column(String, nullable=True, index=True)        # functional area (business)
    enabled = Column(Integer, nullable=False, default=0)        # 0/1 — OFF by default
    serves_user_email = Column(String, nullable=True, index=True)  # which boss it serves
    priority = Column(Integer, nullable=False, default=0)

    # ── Signal (WHEN) ─────────────────────────────────────────────────
    # schedule | data_condition | channel_message | business_event | manual
    signal_type = Column(String, nullable=False, default="manual", index=True)
    signal_spec = Column(Text, nullable=False, default="{}")    # JSON, structured per type
    signal_summary = Column(Text, nullable=True)                # business sentence ("When…")
    check_interval_sec = Column(Integer, nullable=False, default=300)

    # ── Agent condition (IF) — second verification ───────────────────
    # Separate from the signal/trigger above: once the signal fires, the agent
    # ALSO checks this. JSON {"mode": ..., ...}:
    #   "data"   = structured aggregate over the local/project SQL DB;
    #   "sql"    = a SELECT returning true/false;
    #   "checks" = a flat CHECKLIST of MCP-tool checks combined ALL/ANY, each one
    #              a deterministic tools/call (0 tokens) + a predicate — the cheap
    #              pre-gate that calls the employee's MCPs in API mode and only
    #              lets the run proceed if the signals hold (see evaluate_mcp_condition);
    #   "agent"  = a NATURAL-LANGUAGE check the employee performs with an LLM run
    #              (the costly one — reserve for irreducible judgement).
    # data/sql carry "source": "local" | "project". A cheap deterministic guard
    # (data/sql/checks) may also ride on ANY mode via ``condition_spec.pre`` so an
    # ``agent`` (LLM) check runs ONLY when the pre-gate already held. Nullable so
    # legacy rows (signal_type=data_condition) still work.
    condition_spec = Column(Text, nullable=True)

    # ── Reaction (THEN) ───────────────────────────────────────────────
    # WHAT the employee does (4 orthogonal OUTCOMES):
    #   notify (informar) | create_task (crear tarea) | act (actuar) | generate_deliverable
    # Approval is a SEPARATE axis (requires_approval) — the old "draft_and_approve"
    # was just act+approval; "notify"-as-outcome is inform-and-nothing-else (the
    # "let me know when done" of any outcome rides on reaction_spec.notify).
    reaction_type = Column(String, nullable=False, default="notify", index=True)
    # HOW the outcome is produced — the COST axis:
    #   "agent"        = an LLM run reasons/executes (needed for act/generate_deliverable,
    #                    and for notify/create_task when the content must be reasoned);
    #   "deterministic"= a templated notify/create_task done DIRECTLY, 0 tokens (no run).
    # Default "agent" preserves legacy behaviour; the cost-first authoring path
    # prefers "deterministic" whenever the content is a template.
    execution_mode = Column(String, nullable=False, default="agent", index=True)
    reaction_spec = Column(Text, nullable=False, default="{}")  # JSON, per type
    reaction_instructions = Column(Text, nullable=True)         # NL guidance for the agent
    # v7.84.0 — reference-material tags this order depends on (comma-separated,
    # matched against the DOCUMENT LIBRARY). At reaction time the employee is told
    # to consult search_knowledge for these so it uses the OFFICIAL source. NULL ⇒ none.
    requires_knowledge = Column(String, nullable=True)
    requires_approval = Column(Boolean, nullable=False, default=True)
    autonomous_ok = Column(Boolean, nullable=False, default=False)  # explicit opt-in to act alone
    # v7.63.0 — CHECK-BEFORE-APPROVE. Only meaningful for a NATURAL-LANGUAGE
    # (``condition_spec.mode == 'agent'``) condition combined with
    # ``requires_approval``: the watcher can't evaluate prose, so historically the
    # approval was parked FIRST and the employee checked the condition only AFTER
    # the operator approved (approving blind, and wasting an approval when the
    # condition didn't hold). With this ON (the default), the employee runs a
    # READ-ONLY verification FIRST; the approval is parked — enriched with the
    # evidence found — ONLY if the condition holds. OFF restores the cheaper legacy
    # order (approve → then check at reaction time). No effect on data/sql/always
    # conditions (those are already gated deterministically before the approval).
    check_before_approval = Column(Boolean, nullable=False, default=True)

    # ── Anti-spam ─────────────────────────────────────────────────────
    cooldown_sec = Column(Integer, nullable=False, default=0)
    rearm_on_recovery = Column(Boolean, nullable=False, default=True)

    # ── Audit ─────────────────────────────────────────────────────────
    source = Column(String, nullable=False, default="form")    # form|chat|manifest_seed
    created_by = Column(String, nullable=True)
    updated_by = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    __table_args__ = (
        Index("ix_automations_enabled_type", "enabled", "signal_type"),
    )


class AutomationState(AppBase):
    """Per-automation evaluation state (re-arm, cooldown, live value,
    diagnostics). Separated from the definition on purpose."""
    __tablename__ = "automation_state"

    automation_id = Column(
        Integer, ForeignKey("automations.id", ondelete="CASCADE"), primary_key=True,
    )
    last_checked_at = Column(DateTime, nullable=True)
    last_value = Column(Float, nullable=True)            # last numeric reading (data_condition)
    last_value_json = Column(Text, nullable=True)        # richer context for the UI/diagnosis
    # armed (ready to fire) | triggered (condition currently true) | cooldown | error
    state = Column(String, nullable=False, default="armed")
    fire_count = Column(Integer, nullable=False, default=0)
    last_fired_at = Column(DateTime, nullable=True)
    last_run_id = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)


class AutomationWatchItem(AppBase):
    """PER-ITEM lifecycle of an automation in ``watch`` mode (GENERIC dedup).

    One row per ``(automation_id, item_key)``. It turns "the condition matches a SET"
    into "fire ONCE per NEW or escalated item, and auto-resolve when the item
    disappears". It is domain-agnostic: the ``item_key`` can be a cluster incident
    (tenant+fingerprint), a lead (lead_id), a ticket (ticket_id)… The Operator is
    just a CONFIGURATION on top of this; there is nothing cluster-specific here."""
    __tablename__ = "automation_watch_items"

    automation_id = Column(
        Integer, ForeignKey("automations.id", ondelete="CASCADE"), primary_key=True,
    )
    item_key = Column(String, primary_key=True)
    signature = Column(String, nullable=True)      # changes → escalated → re-fires once
    state = Column(String, nullable=False, default="open")  # open | recurring | resolved
    times_seen = Column(Integer, nullable=False, default=1)
    first_seen_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    fired_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    sample = Column(Text, nullable=True)           # sample for the reaction / the UI

    __table_args__ = (
        Index("ix_watch_items_auto_state", "automation_id", "state"),
    )


class AutomationEvalLog(AppBase):
    """Trace of EACH evaluation of the 'only if' (incl. the 0-token ones that do NOT fire). It
    gives the traceability the operator asks for: when it was checked, what it gave, whether it
    fired and why. Generic (any automation, not just the Operator). Volume bounded by retention
    (daily housekeeping prunes old rows)."""
    __tablename__ = "automation_eval_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    automation_id = Column(
        Integer, ForeignKey("automations.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    mode = Column(String, nullable=True)                     # condition mode evaluated
    met = Column(Boolean, nullable=False, default=False)     # the condition/gate was met
    fired = Column(Boolean, nullable=False, default=False)   # it really fired (after dedup/silence)
    fresh = Column(Integer, nullable=True)                   # watch: new/escalated items
    resolved = Column(Integer, nullable=True)                # watch: auto-resueltos
    silenced = Column(Boolean, nullable=False, default=False)
    error = Column(Text, nullable=True)                      # check/watch error (if any)
    detail = Column(Text, nullable=True)                     # compact JSON (check values / counts)
    run_id = Column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_evallog_auto_at", "automation_id", "at"),
    )


AUTOMATION_MODEL_REGISTRY = {
    "automations": Automation,
    "automation_state": AutomationState,
    "automation_watch_items": AutomationWatchItem,
    "automation_eval_log": AutomationEvalLog,
}
