"""The BUSINESS timezone — now OPERATOR-EDITABLE at runtime.

The whole app stores time in UTC and renders it in the BUSINESS timezone (cron
schedules, calendar, encargo due-dates, the chat clock, every date the SPA
shows). Historically that timezone was FIXED at deploy time
(``config.APP_TIMEZONE`` ← ``wai-app.json`` ``agent_app.locale.timezone``).

v7.39.0 makes it part of the employee's IDENTITY: the operator can change the
company's wall clock from the Identity page without a redeploy. Resolution:
operator override (``app_settings.business_timezone``) → deploy default.

To keep the hot sync path (``timeutil.business_tz`` is called everywhere) free of
per-call DB reads, the override is cached in-process: warmed at boot
(``refresh_cache``) and re-read on every PATCH. ``current_timezone`` is the sync
accessor; ``effective_timezone`` is the async one that refreshes from the store.
This module imports only ``config`` at load time (the DB models are imported
lazily inside the async helpers) so it never creates an import cycle with
``timeutil``.
"""
from __future__ import annotations

import logging

from app.config import APP_TIMEZONE

logger = logging.getLogger(__name__)

SETTING_KEY = "business_timezone"

# Operator override (a validated IANA name) or None → use the deploy default.
_OVERRIDE: str | None = None

# A curated shortlist for the Identity selector (any valid IANA name is
# accepted by the API, but these cover the common business cases).
COMMON_TIMEZONES: list[str] = [
    "UTC",
    "Europe/Madrid", "Europe/Lisbon", "Europe/London", "Europe/Paris",
    "Europe/Berlin", "Europe/Rome", "Europe/Amsterdam", "Europe/Zurich",
    "Europe/Athens", "Europe/Warsaw", "Europe/Istanbul", "Europe/Moscow",
    "Atlantic/Canary",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Mexico_City", "America/Bogota", "America/Lima", "America/Santiago",
    "America/Sao_Paulo", "America/Argentina/Buenos_Aires",
    "Africa/Cairo", "Africa/Johannesburg",
    "Asia/Dubai", "Asia/Jerusalem", "Asia/Kolkata", "Asia/Shanghai",
    "Asia/Tokyo", "Asia/Singapore",
    "Australia/Sydney",
]


def manifest_timezone() -> str:
    """The deploy-time default (``APP_TIMEZONE`` env / ``wai-app.json`` locale)."""
    return APP_TIMEZONE


def is_valid_tz(tz: str) -> bool:
    """True when ``tz`` is a valid IANA timezone name."""
    try:
        from zoneinfo import ZoneInfo  # local import — cheap, avoids top-level cost
        ZoneInfo(tz)
        return True
    except Exception:
        return False


def current_timezone() -> str:
    """The effective business timezone from the IN-MEMORY cache (no DB read).
    Used by the hot sync path (``timeutil``). Warmed at boot + on PATCH."""
    return _OVERRIDE or APP_TIMEZONE


def _set_cache(tz: str | None) -> None:
    global _OVERRIDE
    _OVERRIDE = tz if (tz and is_valid_tz(tz)) else None


async def refresh_cache() -> str:
    """Read the stored override into the in-memory cache. Call at boot and after
    every PATCH. Never raises — a read failure keeps the last known value."""
    try:
        from sqlalchemy import select
        from app.app_database import app_session
        from app.app_models.runtime import AppSetting
        async with app_session() as s:
            raw = (await s.execute(
                select(AppSetting.value).where(AppSetting.key == SETTING_KEY)
            )).scalar_one_or_none()
        _set_cache((str(raw).strip() if raw else None) or None)
    except Exception:  # pragma: no cover — never block on a read error
        logger.debug("business timezone refresh failed; keeping current", exc_info=True)
    return current_timezone()


async def effective_timezone() -> str:
    """The business timezone RIGHT NOW (refreshes the cache from the store)."""
    return await refresh_cache()
