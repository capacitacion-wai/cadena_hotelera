"""Bounds on the employee's SELF-SCHEDULING (auto-programación en el tiempo).

The employee can park future work for itself with the ``schedule_task`` MCP
tool — a follow-up, a recurring report, a deferred check. When the due time
arrives the drain creates an autonomous run, so the employee actually does
the work. This is high value (it can organise its own time) but also the main
COST lever: nothing else lets one run silently fan out into many future runs.

This module is the deterministic guardrail. Two knobs, both operator-owned:

  * ``self_schedule`` (bool) — may the employee create scheduled tasks for
    ITSELF at all? When off, ``schedule_task`` refuses (the employee can still
    be told what to do, and can still put items on a HUMAN's calendar via
    ``assign_task_to_user`` — that does not auto-execute).
  * ``max_open_tasks`` (int) — hard cap on how many agent-assigned scheduled
    items may be OPEN at once. A new ``schedule_task`` past the cap refuses
    with a clear reason, so a loop cannot inflate cost unbounded. ``0`` means
    "no cap" (explicit opt-out). A recurring item counts as ONE open task.

Source of truth, in order (mirrors ``triggers_state`` / ``autonomy_state``):
  1. The operator's stored choice (``app_settings.self_schedule`` /
     ``app_settings.max_open_tasks``), once set.
  2. Otherwise the manifest default (``agent_app.scheduling.{self_schedule,
     max_open_tasks}`` in ``wai-app.json``).

Defaults: self-scheduling ON, cap = 20 open tasks — useful out of the box,
bounded by default. The operator widens or closes it from Operations.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.app_database import app_session
from app.app_models.runtime import AppSetting
from app.config import AGENT_APP

logger = logging.getLogger(__name__)

SELF_SCHEDULE_KEY = "self_schedule"
MAX_OPEN_TASKS_KEY = "max_open_tasks"
DEFAULT_MAX_OPEN_TASKS = 20
_TRUEISH = {"1", "true", "yes", "on"}
# An agent-assigned item is "open" (counts against the cap) while in one of
# these non-terminal states. ``complete_scheduled_item`` moves it out.
OPEN_STATUSES = ("scheduled", "pending")


def _scheduling_cfg() -> dict:
    sc = AGENT_APP.get("scheduling")
    return sc if isinstance(sc, dict) else {}


def manifest_self_schedule() -> bool:
    """Deploy-time default for self-scheduling. Defaults to TRUE."""
    sc = _scheduling_cfg()
    if "self_schedule" in sc:
        return bool(sc.get("self_schedule"))
    return bool(AGENT_APP.get("self_schedule", True))


def manifest_max_open_tasks() -> int:
    """Deploy-time default cap. Defaults to ``DEFAULT_MAX_OPEN_TASKS``; 0 = no cap."""
    sc = _scheduling_cfg()
    raw = sc.get("max_open_tasks", AGENT_APP.get("max_open_tasks", DEFAULT_MAX_OPEN_TASKS))
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_OPEN_TASKS


async def _setting(key: str):
    try:
        async with app_session() as s:
            return (await s.execute(
                select(AppSetting.value).where(AppSetting.key == key)
            )).scalar_one_or_none()
    except Exception:  # pragma: no cover — never block a run on a read error
        logger.debug("scheduling_policy read failed for %s", key, exc_info=True)
        return None


async def can_self_schedule() -> bool:
    """True when the employee may schedule tasks for itself right now."""
    raw = await _setting(SELF_SCHEDULE_KEY)
    if raw is None:
        return manifest_self_schedule()
    return str(raw).strip().lower() in _TRUEISH


async def max_open_tasks() -> int:
    """Effective open-task cap right now (0 = no cap)."""
    raw = await _setting(MAX_OPEN_TASKS_KEY)
    if raw is None:
        return manifest_max_open_tasks()
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return manifest_max_open_tasks()


async def count_open_agent_tasks() -> int:
    """How many agent-assigned scheduled items are currently OPEN."""
    from app.app_models.business import ScheduledItem
    try:
        async with app_session() as s:
            return int((await s.execute(
                select(func.count()).select_from(ScheduledItem).where(
                    ScheduledItem.assignee == "agent",
                    ScheduledItem.status.in_(OPEN_STATUSES),
                )
            )).scalar_one() or 0)
    except Exception:  # pragma: no cover
        logger.debug("count_open_agent_tasks failed", exc_info=True)
        return 0
