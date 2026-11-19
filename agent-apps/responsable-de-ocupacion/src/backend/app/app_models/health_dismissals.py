"""v7.170.0 — STATUS NOTICES: what each person has dismissed, and until when.

The employee's notices ("it is watching nothing", "there are connections down") are STATES,
not events: they are recomputed on every query from reality. That is why the
first problem —"does this stay there forever?"— solves itself: **as soon as the
condition stops being true, the notice disappears**, without anybody having to mark it
as read. This table exists for the OTHER case: the notice that is still true and that
the person has already seen and decided to live with ("I know there are no active
routines, it is on purpose").

Three decisions that stop dismissing from turning into hiding:

* **A SITUATION is dismissed, not a notice type.** The key is
  ``fingerprint`` = the type + what makes it concrete (which channels are down,
  how many approvals are waiting). If the situation changes materially, the
  fingerprint changes and the notice **reappears**: silencing "email is down" must
  not silence "email AND WhatsApp are down".
* **An ERROR cannot be dismissed.** Only `warning` severity notices. An error is
  "you need to do something"; allowing it to be covered removes the only meaning it
  has. The routes enforce it, not this table.
* **The silence EXPIRES** (``muted_until``). A dismissal is not a permanent product
  decision: after 30 days the notice comes back so someone looks at it again. That
  something was accepted in March does not mean it still is in June.

It is PER PERSON (``user_email``): one administrator living with a notice does not
decide for the others.

Own model file (INVARIANT) like ``signal_suppressions.py`` — a table that must exist in
ALL apps belongs to the skill and is restored by ``--fix-invariants``, never to the
scaffold ``business.py`` that the generator rewrites per app.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, String

from app.app_database import AppBase


class HealthDismissal(AppBase):
    """A status notice THIS person has dismissed, until ``muted_until``."""

    __tablename__ = "health_dismissals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_email = Column(String, nullable=False, index=True)
    # Notice type (`triggers_off`, `channels_down`…). Stored in addition to the
    # fingerprint so we can list/clean by type without decomposing the fingerprint.
    key = Column(String, nullable=False)
    # Identity of the concrete SITUATION: type + what makes it unique. If it changes,
    # the dismissal stops matching and the notice reappears — which is exactly what is
    # wanted when the problem gets worse or changes shape.
    fingerprint = Column(String, nullable=False, index=True)
    # When it is shown again. Never NULL on purpose: there is no "forever"
    # dismissal — see the module header.
    muted_until = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_health_dismissals_lookup", "user_email", "fingerprint"),
    )
