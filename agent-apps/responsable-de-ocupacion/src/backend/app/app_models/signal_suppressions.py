"""v7.64.0 — SIGNAL SUPPRESSIONS ("silencios / no repetir"): stop REGENERATING a
specific recurring alert/recommendation/decision, forever or until a date, even if
its conditions recur.

Own model file (INVARIANT) like ``notify_rules.py`` — a table that must exist in
EVERY app is skill-owned and restored by ``--fix-invariants``, never in the
SCAFFOLD ``business.py`` (which the generator rewrites per app).
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text

from app.app_database import AppBase


class SignalSuppression(AppBase):
    """One 'no repetir' rule, enforced DETERMINISTICALLY at signal-creation time
    (``upsert_alert`` / ``register_recommendation`` / ``log_decision``): if a NEW
    signal's IDENTITY (``match_key``) matches an ACTIVE suppression, the platform
    SKIPS the creation (kills the loop — never relies on the prompt), bumps the
    counter and tells the agent it was suppressed.

    ``match_key`` is the stable identity of the signal (alerts → ``dedupe_key``;
    recs/decisions → an explicit key the agent stashed or ``normalize(title)``
    [+target/ref] — see ``signal_suppress.suppression_key``). ``muted_until`` NULL =
    forever (a HUMAN-only decision); a datetime = a snooze (the agent may only
    create bounded snoozes). Deleting the row re-enables generation immediately.

    v7.140.0 — a silence may carry a CONDITION ("normal on Sundays during the
    backup, but warn me on a Tuesday"). Additive columns, NOT a sibling table: a
    silence is ONE concept in the product and splitting it would mean two gates,
    two managers and two counters. Invariant enforced by the routes: **exactly
    one of ``match_key`` / ``subject_pattern`` carries the scope** — identity
    semantics are untouched, the conditional shape lives in its own columns.
    """
    __tablename__ = "signal_suppressions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_table = Column(String, nullable=False, index=True)   # alerts|recommendations|decisions
    match_key = Column(String, nullable=False, index=True)      # stable signal identity
    match_label = Column(String, nullable=True)                 # human-readable ("Stock bajo — Producto X")
    muted_until = Column(DateTime, nullable=True)               # NULL = forever
    reason = Column(Text, nullable=True)
    created_by = Column(String, nullable=True)                  # email | 'agent'
    created_by_run_id = Column(Integer, nullable=True)
    source_signal_id = Column(Integer, nullable=True)           # the signal row it was created from
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    suppress_count = Column(Integer, nullable=False, default=0)  # how many repeats it blocked
    last_suppressed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ── v7.140.0: governance ──
    # 'active'   → the gate honours it (today's only behaviour).
    # 'proposed' → the EMPLOYEE asked for it; it silences NOTHING until an admin
    #              accepts. Same anti-self-escalation pattern as
    #              ``KnowledgeRequest.proposed_value``: the agent may propose an
    #              indefinite silence, it may never grant itself one.
    review_state = Column(String, nullable=False, default="active", index=True)

    # ── v7.140.0: conditional scope ──
    # Scope by PATTERN instead of exact identity ("any alert about tenant X").
    # NULL = classic identity-only silence.
    subject_pattern = Column(String, nullable=True, index=True)
    # The "solo si" of the silence, same vocabulary as an automation's condition.
    # {"kind": "weekday"|"time_window"|"field"|"always", ...} — see
    # ``signal_conditions.matches``.
    condition_json = Column(Text, nullable=True)
    # HARD GUARD: never silence above this severity. A critical alert must reach
    # the human even if a pattern would have matched it (``signal_conditions``
    # enforces it, not the prompt).
    severity_max = Column(String, nullable=True)
    # A conditional silence SHOULD expire (the environment moves). Distinct from
    # ``muted_until``, which is the classic snooze on an identity.
    expires_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_signal_suppressions_table_key_enabled", "signal_table", "match_key", "enabled"),
        # The conditional lookup: the gate asks "any pattern silence for this
        # table that is in force?" before evaluating conditions in Python.
        Index("ix_signal_suppressions_pattern", "signal_table", "review_state", "enabled"),
    )
