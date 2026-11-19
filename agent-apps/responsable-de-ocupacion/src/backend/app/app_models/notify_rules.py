"""v7.62.0 — operator NOTIFICATION RULES on the alerts/recommendations/decisions
screens: "when a NEW <signal> meets these intensity thresholds, message this
directory user via this channel".

Kept in its OWN model file (INVARIANT), NOT in ``business.py`` (which is a
SCAFFOLD the generator rewrites with each app's domain) — a rule table that must
exist in EVERY app has to be skill-owned and restored by ``--fix-invariants``.
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text

from app.app_database import AppBase


class SignalNotifyRule(AppBase):
    """One operator rule. Evaluated the instant a signal is created
    (``upsert_alert`` / ``register_recommendation`` / ``log_decision``) — no
    polling. Governance-safe: the OPERATOR authors the rule (recipient + channel),
    so honouring it does not break the "the agent never picks recipients" rule.

    ``conditions_json`` = ``{"impact_min": "high", "urgency_min": "medium",
    "confidence_min": "high", "match": "all"|"any"}`` — each threshold is a
    MINIMUM band ("X o más"); absent = any. ``match='all'`` needs every present
    threshold (AND), ``'any'`` needs one (OR).
    """
    __tablename__ = "signal_notify_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_table = Column(String, nullable=False, index=True)   # alerts|recommendations|decisions
    name = Column(String, nullable=True)
    conditions_json = Column(Text, nullable=False, default="{}")
    channel_id = Column(Integer, nullable=False)                # which channel delivers
    recipient_user = Column(String, nullable=False)             # directory user email
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_fired_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_signal_notify_rules_table_enabled", "signal_table", "enabled"),
    )
