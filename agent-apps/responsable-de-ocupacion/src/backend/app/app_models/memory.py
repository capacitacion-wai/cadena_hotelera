"""Per-user persistent memory — the agent's long-term knowledge of a person.

Part of the ``wai-agent-app-builder`` SKILL (v6.36.0). The agent app is
multi-user (operators chat with it; external senders write to its
channels). This module gives it an app-native, per-user memory organized
as the professional memory layers the field has converged on (Letta /
MemGPT, mem0, Zep), mapped onto small SQLite tables the Operations page
can fully manage:

* ``UserMemory``  — SEMANTIC + PROCEDURAL layer: durable facts,
                    preferences and learned procedures about a user.
                    Contradiction-managed (``superseded_by``), with
                    provenance and a ``pinned`` flag protecting manual /
                    important entries from automatic replacement.
* ``UserEpisode`` — EPISODIC layer: condensed summaries of past
                    interactions (one line per chat/email exchange) with
                    a timestamp and outcome. TTL/cap applied so the table
                    stays small (the semantic layer retains what matters).
* ``UserFile``    — ARTIFACTS layer (domain-specific, not covered by any
                    library): an index of documents the user sent or the
                    agent produced for them, pointing at a workspace path.

The WORKING / CORE layer is not a table: it is the budget-capped
``memory_block`` the app injects into the prompt (see
``app/agent_sessions.py``), built from pinned facts + recent episodes +
relevant files, mirroring a Letta "memory block".

Identity key is ``user_email`` — the human's address (operator email for
chat, sender for channels). The same person across chat and email shares
one memory because inbound mail from a known operator is unified onto
their interactive scope (see ``resolve_inbound_scope``).

The MCP tools in ``app/mcp_server.py`` (``remember_fact``,
``record_episode``, ``index_file``, ``list_memory``, ``search_memory``)
write/read here; the Operations "Memoria" tab manages it through
``app/routes/memory_routes.py``.
"""

from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, LargeBinary,
    String, Text, UniqueConstraint,
)
from app.app_database import AppBase


class UserMemory(AppBase):
    """Semantic + procedural memory: a durable fact, preference or
    learned procedure about one user.

    ``kind`` is constrained at the application level (the MCP tool
    rejects values outside ``fact|preference|procedure``). A row is
    "live" while ``superseded_by`` is NULL; recording a contradicting
    fact with ``supersedes=<id>`` marks the old row superseded (history
    is kept, it just drops out of the working block). ``pinned`` rows are
    protected from automatic replacement and always surface first.
    """
    __tablename__ = "user_memory"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    created_by_run_id = Column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    user_email = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False, default="fact", index=True)  # fact|preference|procedure
    content = Column(Text, nullable=False, default="")
    tags = Column(String, nullable=True)
    confidence = Column(Float, nullable=True)            # for procedures (0..1)
    pinned = Column(Boolean, nullable=False, default=False, index=True)
    source = Column(String, nullable=False, default="agent")  # chat|email|operator|agent
    provenance = Column(String, nullable=True)           # run/message/episode refs
    superseded_by = Column(
        Integer, ForeignKey("user_memory.id", ondelete="SET NULL"), nullable=True,
    )
    # v6.44.0: usage signal so retention/optimization can favour facts the
    # agent actually leans on. Bumped each time a fact lands in the injected
    # working block (see ``memory_prompt.build_memory_block_detail``). A
    # never-accessed, old, low-value fact is the safest thing to prune.
    access_count = Column(Integer, nullable=False, default=0)
    last_accessed_at = Column(DateTime, nullable=True)
    # v7.29.0 — memory hardening + dense retrieval.
    # ``trust``: how far we believe this, fixed by the RUN ORIGIN server-side
    # (verified|stated|inferred|untrusted). The write tool can lower it, never
    # raise it — a fact extracted from an inbound channel body is capped at
    # ``untrusted`` even if the model asks for more (anti-poisoning).
    trust = Column(String, nullable=True, index=True)
    # ``importance``: 0..1 salience hint (agent + heuristic floor), a term in
    # the retrieval score. NULL = neutral.
    importance = Column(Float, nullable=True)
    # ``content_hash``: normalized-content fingerprint for cross-run dedup
    # (two parallel pool workers asserting the same fact reinforce, not duplicate).
    content_hash = Column(String, nullable=True, index=True)
    # ``embedding``/``embed_model``: dense vector (packed float32 blob) + the
    # model id that produced it, so a model change can re-embed instead of
    # comparing incompatible vectors.
    embedding = Column(LargeBinary, nullable=True)
    embed_model = Column(String, nullable=True)

    __table_args__ = (
        Index("ix_user_memory_user_kind", "user_email", "kind"),
        Index("ix_user_memory_user_pinned", "user_email", "pinned"),
        Index("ix_user_memory_live", "user_email", "superseded_by"),
        Index("ix_user_memory_user_hash", "user_email", "content_hash"),
    )


class UserEpisode(AppBase):
    """Episodic memory: a condensed summary of one past interaction."""
    __tablename__ = "user_episodes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    created_by_run_id = Column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    user_email = Column(String, nullable=False, index=True)
    summary = Column(Text, nullable=False, default="")
    source = Column(String, nullable=False, default="chat")   # chat|email
    source_ref = Column(String, nullable=True)
    outcome = Column(String, nullable=True)
    # v7.29.0 — dense retrieval + distillation state.
    importance = Column(Float, nullable=True)
    embedding = Column(LargeBinary, nullable=True)
    embed_model = Column(String, nullable=True)
    # ``raw`` = a template-captured chat episode (readable, = pre-v7.29.0
    # baseline); ``distilled`` = a real agent-written summary (from the
    # STRUCTURED_RESPONSE memory block). Marks episodes a future job can
    # re-distill; today ``raw`` ones are simply used as-is. NULL = legacy.
    distill_state = Column(String, nullable=True)

    __table_args__ = (
        Index("ix_user_episodes_user_created", "user_email", "created_at"),
    )


class UserFile(AppBase):
    """Artifacts layer: index of a document the user sent or the agent
    produced for them. Bytes live on the workspace PVC; this row only
    points at the ``workspace_path`` so the Operations tab can list and
    download them, and the agent can read them."""
    __tablename__ = "user_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    created_by_run_id = Column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    user_email = Column(String, nullable=False, index=True)
    filename = Column(String, nullable=False)
    workspace_path = Column(String, nullable=False)
    mime = Column(String, nullable=True)
    size = Column(Integer, nullable=True)
    source = Column(String, nullable=False, default="email")   # email|chat|operator|agent
    source_ref = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    # v7.29.0 — embedding of the (semantic) description for dense file recall.
    embedding = Column(LargeBinary, nullable=True)
    embed_model = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_email", "workspace_path", name="uq_user_files_user_path"),
        Index("ix_user_files_user_created", "user_email", "created_at"),
    )


MEMORY_MODEL_REGISTRY: dict[str, type] = {
    "user_memory":   UserMemory,
    "user_episodes": UserEpisode,
    "user_files":    UserFile,
}
