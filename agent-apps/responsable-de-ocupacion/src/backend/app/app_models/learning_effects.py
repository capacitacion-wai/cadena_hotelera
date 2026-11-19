"""Learning EFFECTS registry — where each learned rule came from (v7.140.0).

The problem this solves: once the employee can propose rules, Conocimiento /
Silencios / Trabajo autónomo slowly fill with entries nobody dares delete
because nobody knows why they appeared. So every time a learning MATERIALIZES
into one of the four existing homes we append a row here with the WHY, the WHO
and the WHERE, plus the handle to undo it.

Two things it is NOT, and both matter:

1. **Not authoritative** over whether the rule is active — the TARGET table is
   (``signal_suppressions.review_state``, ``knowledge_requests.value``,
   ``scoped_knowledge``, ``automations.enabled``). This is the link + the audit
   trail, so there is exactly one truth about "is this in force?".
2. **Not the inhibition log.** ``signal_silence_log`` answers "why didn't it
   warn me?" (one row per TIME a silence acted). This answers "where did this
   rule come from?" (one row per RULE, with its lifecycle). Different questions,
   different volumes: this table holds dozens of rows in an employee's life, so
   it is never pruned.

It is also what makes the BIDIRECTIONAL trace possible with the link stored
ONCE: ``(target, target_id) → learning_id`` resolved by
``learning_effects_service.origin_for`` and rendered by the ``LearnedFromChip``
in each of the four managers.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text

from app.app_database import AppBase

# The four homes a learning can materialize into. Deliberately the concepts the
# product ALREADY has — a learning never invents a mechanism.
TARGET_SUPPRESSION = "suppression"   # silencio (deterministic gate)
TARGET_PARAMETER = "parameter"       # parámetro de negocio (knowledge_requests)
TARGET_INSTRUCTION = "instruction"   # instrucción operativa (scoped_knowledge)
TARGET_AUTOMATION = "automation"     # automatización (the engine runs it)
TARGETS = (TARGET_SUPPRESSION, TARGET_PARAMETER, TARGET_INSTRUCTION, TARGET_AUTOMATION)

# Lifecycle. ``proposed`` mirrors a target row that is not in force yet.
STATE_PROPOSED = "proposed"
STATE_ACTIVE = "active"
STATE_REVERTED = "reverted"
STATE_EXPIRED = "expired"


class LearningEffect(AppBase):
    """One rule created out of one learning, with everything needed to audit
    and undo it."""
    __tablename__ = "learning_effects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)

    # ── The WHY ──
    learning_id = Column(
        Integer, ForeignKey("agent_memory.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Frozen copy of the learning text AS IT WAS when the rule was approved.
    # The learning may later be corrected or superseded; the audit trail must
    # keep saying what the operator actually agreed to.
    learning_text = Column(Text, nullable=False, default="")

    # ── The WHAT ──
    # Same reason as ``state``: covered by the composite index below.
    target = Column(String, nullable=False)   # see TARGETS
    target_id = Column(String, nullable=False, default="")  # str: parameters key by slug
    label = Column(String, nullable=False, default="")      # business language

    # ── The WHO / WHERE / WHEN ──
    proposed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    approved_by = Column(String, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    # 'chat'  → accepted in-thread, with a human present (the main path)
    # 'queue' → accepted later from the Aprendizaje inbox (no human at the time)
    approved_where = Column(String, nullable=True)
    # The conversation/run where it was agreed — the link that answers
    # "when did we decide this?".
    origin_run_id = Column(Integer, nullable=True, index=True)

    # No column-level index: the composite ``ix_learning_effects_state`` below
    # already leads with it (and an auto-named one would collide with it).
    state = Column(String, nullable=False, default=STATE_PROPOSED)
    reverted_by = Column(String, nullable=True)
    reverted_at = Column(DateTime, nullable=True)
    revert_reason = Column(Text, nullable=True)
    # What "undo" means for THIS effect, captured when it was applied — e.g. the
    # value a business parameter held before. Without it, reverting a parameter
    # could only blank it, which is not the same as putting back what was there.
    undo_json = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    __table_args__ = (
        # The reverse lookup ("this silence — where did it come from?") is the
        # hot one: it runs for every row of the four managers' listings.
        Index("ix_learning_effects_target", "target", "target_id", "state"),
        Index("ix_learning_effects_state", "state", "created_at"),
    )
