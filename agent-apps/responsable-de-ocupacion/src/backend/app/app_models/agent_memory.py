"""Agent SELF-memory — what the employee has learned about its ENVIRONMENT.

Part of the ``wai-agent-app-builder`` SKILL (v7.140.0). This is the SECOND
memory axis and it is deliberately NOT the same thing as ``memory.py``:

* ``memory.py``       → memory OF A PERSON (``user_email`` is the identity).
* this module         → memory OF THE ENVIRONMENT the employee operates on
                        (``subject`` is the identity: ``job:indexing``,
                        ``deployment:wai-engine-api``, ``tenant:pabisan``).

Why a sibling layer instead of reusing ``user_memory`` with a synthetic
owner: ``memory_utils.valid_memory_user`` REFUSES the autonomous identity on
purpose ("so the synthetic bot identity never gets a personal memory bucket"),
the per-run owner bind requires a human, and the whole REST/UI surface is
keyed by person. So an autonomous run (``memory_user`` is None) is memoryless
by design — exactly the gap this fixes for the cluster watcher: "this sweep
taking 40 min is NORMAL here, don't raise an alert".

The machinery IS reused (FTS5, embeddings, trust, multifactor ranking, the
budget-capped block, compaction), only the identity and the governance are
new. Two layers, and the split is the point:

* what it KNOWS (this table)  → soft. Injected in the prompt, INFORMS judgment.
* what it DOES (a silence, a
  parameter, an instruction, an
  automation)                 → hard. Deterministic gate / strong injection /
                                the automations engine, and an admin confirms
                                it. Provenance lives in ``learning_effects``.

``review_state`` + ``behavior_effect`` are what keep the two apart: a brand new
lesson is rendered "⚠ SIN VALIDAR" and CANNOT change behaviour by itself.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, LargeBinary,
    String, Text,
)

from app.app_database import AppBase


class AgentMemory(AppBase):
    """One thing the employee learned about its environment.

    ``kind``:
      * ``observation`` — a single data point ("the sweep took 38 min today").
      * ``baseline``    — what is NORMAL here, distilled from N observations
                          ("the sweep normally takes 30-45 min"). The kind that
                          prevents false positives.
      * ``lesson``      — a conclusion that changes how it should judge
                          ("a sweep over 30 min is not an incident by itself").
      * ``procedure``   — a way of working that WORKED ("to check indexing,
                          look at the job pod, not the deployment").
      * ``entity``      — a durable fact about a piece of the environment
                          ("tenant pabisan has 3 heavy nightly jobs").

    ``subject`` is the canonical identity of what was observed, normalized with
    the same discipline as ``dedupe_key`` (see ``agent_memory_utils.normalize_subject``):
    it is what makes reinforcement possible instead of piling up duplicates, and
    what lets the UI group "everything I know about X".

    ``valid_until`` is the honest part: a baseline is only true while the
    environment holds. Past that stamp the row stays (history) but drops out of
    the injected block, so a stale "this is normal" can never silence forever.

    ``observations`` is the EVIDENCE the operator judges by: 1 observation is an
    anecdote, 40 are a baseline. Every re-assertion of the same content bumps it
    instead of inserting a near-duplicate.
    """
    __tablename__ = "agent_memory"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    created_by_run_id = Column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    # observation|baseline|lesson|procedure|entity
    kind = Column(String, nullable=False, default="observation", index=True)
    subject = Column(String, nullable=False, default="", index=True)
    content = Column(Text, nullable=False, default="")
    tags = Column(String, nullable=True)

    # ── Evidence: how many times this was confirmed, and when ──
    observations = Column(Integer, nullable=False, default=1)
    first_seen_at = Column(DateTime, nullable=True)
    last_confirmed_at = Column(DateTime, nullable=True)
    # NULL = no expiry. A baseline SHOULD carry one (the environment moves).
    valid_until = Column(DateTime, nullable=True, index=True)

    confidence = Column(Float, nullable=True)     # 0..1
    importance = Column(Float, nullable=True)     # 0..1 salience
    trust = Column(String, nullable=True, index=True)
    pinned = Column(Boolean, nullable=False, default=False, index=True)
    superseded_by = Column(
        Integer, ForeignKey("agent_memory.id", ondelete="SET NULL"), nullable=True,
    )
    source = Column(String, nullable=False, default="agent")  # agent|operator|chat
    provenance = Column(String, nullable=True)

    # ── Governance: soft by default, hard only after a human says so ──
    # new → the block marks it "SIN VALIDAR" and it can change nothing.
    review_state = Column(String, nullable=False, default="new", index=True)
    reviewed_by = Column(String, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    # Set when this learning HAS been materialized into a real rule, e.g.
    # ``suppression:42`` / ``parameter:umbral_barrido`` / ``instruction:7`` /
    # ``automation:3``. Rendered "(already applied as a rule)" so the agent does
    # not propose the same thing again. Provenance detail → ``learning_effects``.
    behavior_effect = Column(String, nullable=True, index=True)
    # v7.140.0 — set when an operator REVERTED the effect this learning had
    # produced. The learning survives (it still knows what it observed) but it
    # must not be proposed again, or reverting would start a weekly loop.
    repropose_blocked = Column(Boolean, nullable=False, default=False)

    # ── Retrieval machinery (same as per-user memory) ──
    access_count = Column(Integer, nullable=False, default=0)
    # 7.164.0 — did it HELP or not. `access_count` only says the entry ENTERED the
    # context; these two say how the run that carried it ended. It is the difference
    # between optimizing popularity and optimizing usefulness: without them, the
    # lifecycle keeps what is injected most, not what helps most.
    used_ok = Column(Integer, nullable=False, default=0)
    used_ko = Column(Integer, nullable=False, default=0)
    last_accessed_at = Column(DateTime, nullable=True)
    content_hash = Column(String, nullable=True, index=True)
    embedding = Column(LargeBinary, nullable=True)
    embed_model = Column(String, nullable=True)

    __table_args__ = (
        # The block builder reads by (subject, kind) and by kind+recency; the
        # viewer groups by subject. See the efficiency note in the skill docs:
        # the block is built from INDEXES, never from FTS.
        Index("ix_agent_memory_subject_kind", "subject", "kind"),
        Index("ix_agent_memory_live", "superseded_by", "kind"),
        Index("ix_agent_memory_hash_subject", "content_hash", "subject"),
        Index("ix_agent_memory_review", "review_state", "kind"),
    )


class AgentMemoryUse(AppBase):
    """WHICH learnings EACH run carried inside (7.164.0).

    The missing link to close the loop. Until now memory knew that an entry had
    entered the context (`access_count`) but not what happened afterwards, so
    "used" meant *was selected*, not *helped*. With this table, when a run finishes
    its outcome can be attributed to what it carried.

    It cleans itself up: it is for attributing, not for auditing. The accumulated
    truth lives in the entry's own `used_ok`/`used_ko` counters.
    """
    __tablename__ = "agent_memory_use"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, nullable=False, index=True)
    entry_id = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_agent_memory_use_run", "run_id", "entry_id"),
    )


class AgentEpisode(AppBase):
    """Episodic self-memory: what happened in the employee's OWN runs.

    Not "an interaction with a person" (that is ``user_episodes``) but "what I
    did on this environment and how it went" — the raw material a later
    consolidation turns into a ``baseline``.
    """
    __tablename__ = "agent_episodes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    created_by_run_id = Column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    subject = Column(String, nullable=False, default="", index=True)
    summary = Column(Text, nullable=False, default="")
    outcome = Column(String, nullable=True)
    # autonomous|automation|channel|chat — which run shape produced it.
    source = Column(String, nullable=False, default="autonomous")
    source_ref = Column(String, nullable=True)
    importance = Column(Float, nullable=True)
    embedding = Column(LargeBinary, nullable=True)
    embed_model = Column(String, nullable=True)
    distill_state = Column(String, nullable=True)   # raw|distilled

    __table_args__ = (
        Index("ix_agent_episodes_subject_created", "subject", "created_at"),
    )


AGENT_MEMORY_MODEL_REGISTRY: dict[str, type] = {
    "agent_memory":   AgentMemory,
    "agent_episodes": AgentEpisode,
}
