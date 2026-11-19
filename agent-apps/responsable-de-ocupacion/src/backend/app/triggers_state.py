"""Global on/off switch for the agent's AUTONOMOUS execution.

This is the ONE master switch. It gates the autonomous loop — the single entry
point that fires cron/scheduled automations AND drains the scheduled-items queue
— so turning it off stops EVERYTHING the agent does on its own (scheduled tasks,
automations, autonomous work, event reactions). It does NOT touch chat, channel
replies, or manual actions; those run through a different path and keep working.
(Manifest "watchers" were removed as a product in v7.27.0; only cron/automations
and the scheduled-items drain remain behind this switch.)

Source of truth, in order:
  1. The operator's stored choice (``app_settings.triggers_active``), once set —
     so a toggle in Operations persists across restarts.
  2. Otherwise the manifest default (``agent_app.triggers.start_active`` in
     ``wai-app.json``), which DEFAULTS TO FALSE — a freshly created agent starts
     with its triggers OFF until someone turns them on.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.app_database import app_session
from app.app_models.runtime import AppSetting
from app.config import AGENT_APP

logger = logging.getLogger(__name__)

SETTING_KEY = "triggers_active"
_TRUEISH = {"1", "true", "yes", "on"}


def manifest_triggers_start_active() -> bool:
    """The deploy-time default for whether triggers start active.

    An EXPLICIT manifest choice always wins (``agent_app.triggers.start_active``
    or the flat ``triggers_start_active``). When neither is pinned, the default
    is derived from the employee's AUTONOMY LEVEL (``observe``/``propose`` → OFF;
    ``act_with_approval``/``act_autonomous`` → ON) — see ``autonomy_state``. The
    operator's runtime toggle (``app_settings.triggers_active``) still overrides
    everything in ``triggers_active()`` below."""
    t = AGENT_APP.get("triggers")
    if isinstance(t, dict) and "start_active" in t:
        return bool(t.get("start_active"))
    if "triggers_start_active" in AGENT_APP:
        return bool(AGENT_APP.get("triggers_start_active"))
    try:
        from app.autonomy_state import autonomy_defaults, manifest_autonomy_level
        return bool(autonomy_defaults(manifest_autonomy_level())["triggers_start_active"])
    except Exception:  # pragma: no cover — fall back to the safe default
        return False


async def triggers_active() -> bool:
    """True when autonomous triggers may fire right now. Never raises."""
    try:
        async with app_session() as s:
            raw = (await s.execute(
                select(AppSetting.value).where(AppSetting.key == SETTING_KEY)
            )).scalar_one_or_none()
    except Exception:  # pragma: no cover — never block the loop on a read error
        logger.debug("triggers_active read failed; using manifest default", exc_info=True)
        return manifest_triggers_start_active()
    if raw is None:
        # Operator hasn't decided yet → manifest default (OFF unless opted in).
        return manifest_triggers_start_active()
    return str(raw).strip().lower() in _TRUEISH
