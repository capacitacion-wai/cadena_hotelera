"""Daily rollup of the automation funnel (v7.140.0).

``automation_eval_log`` already records one row per evaluation — checks, met
("solo si" passed), fired, silenced, errors — so the counters the operator asks
for are ALREADY in the data. The only problem is retention: the detail is pruned
after 14 days, so "how many times did this fire last month?" would quietly lie.

So the housekeeping SUMMARISES each complete day here BEFORE pruning, and the
prune only runs if the summary succeeded. Result: the row-by-row detail stays
bounded (14 days) while the totals are exact forever.

Deliberately narrow: ONLY what the prune destroys is rolled up. Cost and tokens
are NOT summarised — they are computed live from ``agent_runs``, which is never
pruned for autonomous runs, so there is exactly one source per number and two
figures can never contradict each other.

Volume: one row per automation per day (an automation running for 5 years is
~1.800 rows).
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.app_database import AppBase


class AutomationDailyStats(AppBase):
    """One row per ``(automation_id, day)``. ``day`` is an ISO date string in the
    BUSINESS timezone, because the operator reads "yesterday" as their day, not
    as a UTC window."""
    __tablename__ = "automation_daily_stats"

    automation_id = Column(
        Integer, ForeignKey("automations.id", ondelete="CASCADE"), primary_key=True,
    )
    day = Column(String, primary_key=True)   # YYYY-MM-DD (business tz)

    checks = Column(Integer, nullable=False, default=0)    # evaluations run
    met = Column(Integer, nullable=False, default=0)       # passed the "solo si"
    fired = Column(Integer, nullable=False, default=0)     # actually reacted
    silenced = Column(Integer, nullable=False, default=0)  # held back by dedup/cooldown
    errors = Column(Integer, nullable=False, default=0)

    # When this day was summarised. Re-running the rollup for the same day
    # OVERWRITES the counters (idempotent upsert) instead of adding to them —
    # that is what makes it safe to run on every boot and every night.
    rolled_up_at = Column(DateTime, nullable=False, default=datetime.utcnow)
