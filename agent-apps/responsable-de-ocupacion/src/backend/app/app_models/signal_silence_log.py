"""The NON-ACTION log — "why didn't it warn me?" (v7.140.0).

``signal_suppressions`` only keeps counters (``suppress_count``,
``last_suppressed_at``): enough to say "this blocked 12 repeats", useless to
answer *which* ones, *when*, and *because of what condition*. Without that, a
conditional silence is an act of faith — and a silence you can't audit is worse
than no silence.

So the gate appends ONE row here every time it inhibits a signal. You audit by
EFFECTS (what did it actually silence?) instead of by reading rules.

Volume discipline (same principle as ``automation_eval_log``): this grows with
signal volume, so the daily housekeeping PRUNES it — while the permanent total
survives in ``signal_suppressions.suppress_count``. The log carries the recent
narrative, the counter carries the number forever.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text

from app.app_database import AppBase

# What the gate did instead of creating the signal.
EFFECT_SKIP = "skip"          # silenced outright
EFFECT_DEFERRED = "deferred"  # wait and re-check: if it persists, it warns


class SignalSilenceLog(AppBase):
    """One row per TIME a silence acted. Append-only, prunable."""
    __tablename__ = "signal_silence_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    suppression_id = Column(
        Integer, ForeignKey("signal_suppressions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    occurred_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    signal_table = Column(String, nullable=False)      # alerts|recommendations|decisions
    match_key = Column(String, nullable=True)          # identity of the signal that was blocked
    # The signal AS THE OPERATOR WOULD HAVE SEEN IT ("Indexación lenta — tenant X").
    # Stored denormalized on purpose: the signal row was never created, so there
    # is nothing to join to. Without this the feed could not be read.
    label = Column(String, nullable=True)
    severity = Column(String, nullable=True)           # what it would have been
    effect_applied = Column(String, nullable=False, default=EFFECT_SKIP)
    # Why it matched: 'identity' (exact match_key) or the evaluated condition.
    reason = Column(String, nullable=True)
    payload_summary = Column(Text, nullable=True)
    run_id = Column(Integer, nullable=True)            # the run that tried to create it

    __table_args__ = (
        Index("ix_silence_log_suppression_at", "suppression_id", "occurred_at"),
    )
