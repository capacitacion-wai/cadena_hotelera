"""14.14.0 — THE PHONES of the people who use this employee, and the approvals already notified.

The end-of-turn notice (`aviso_push.py`) needs no table: the token travels with the message.
An approval is the opposite case: it is born WITHOUT anybody asking (an automation, a run at
night) and it is for whoever may DECIDE, who is usually not the person who triggered it. So the
pod has to know, beforehand, which phones each person has: the mobile app registers them
(`PUT /api/agent/devices`) every time it lists its employees.

* ``PushDevice`` — one row per phone token; ``email`` is the person signed in on that phone,
  ``idioma`` the phone app's language (the notice is written in it). A token Expo reports as
  no longer registered is deleted when sending.
* ``ApprovalNotice`` — an approval already notified (or already pending when this started):
  the notifier never notifies the same approval twice, whatever restarts in between.

Own model file (INVARIANT): `avisos_de_aprobacion.py` imports it, so it is registered on
``AppBase.metadata`` before ``create_all`` even though the scaffold ``app_models/__init__.py``
of an older employee does not list it.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.app_database import AppBase


class PushDevice(AppBase):
    """A phone that receives this employee's notices for ``email``."""
    __tablename__ = "push_devices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String, nullable=False, unique=True, index=True)
    email = Column(String, nullable=False, index=True)
    idioma = Column(String, nullable=False, default="es", server_default="es")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ApprovalNotice(AppBase):
    """An approval whose notice has been sent (``recipients`` phones; 0 = nobody to tell, or
    it was already pending when the notifier started)."""
    __tablename__ = "approval_notices"

    approval_id = Column(Integer, primary_key=True)
    noticed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    recipients = Column(Integer, nullable=False, default=0, server_default="0")
