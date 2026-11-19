"""Centralised timezone handling for the agent-app.

Policy (single source of truth — do NOT re-implement elsewhere):

* Everything is STORED in UTC (naive-UTC datetimes in SQLite, which is
  what ``datetime.utcnow`` defaults produced historically — we treat a
  naive datetime read back from the DB as UTC).
* Everything is COMPUTED against the BUSINESS timezone (``APP_TIMEZONE``)
  so cron expressions, calendar items and due-dates match the operator's
  wall clock instead of the pod's UTC.
* Everything is SERIALISED to the SPA as ISO-8601 WITH an explicit
  ``Z``/offset so the browser never has to guess (the long-standing bug
  was bare ``2026-06-16T08:00:00`` strings that JS parsed as *local*).

Use :func:`iso_utc` whenever you hand a datetime to the SPA, and
:func:`now_local` / :func:`business_tz` when you reason about "what time
is it for the business right now".
"""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo

from app.business_timezone import current_timezone


def business_tz() -> tzinfo:
    """Return the EFFECTIVE business timezone (operator override → deploy
    default), falling back to UTC. v7.39.0 — reads the in-memory runtime override
    so an operator can change the company's wall clock from the Identity page
    without a redeploy; the value is cached (no per-call DB read)."""
    try:
        return ZoneInfo(current_timezone())
    except Exception:  # pragma: no cover — validated on set, defensive
        return timezone.utc


def tz_for(name: str | None) -> tzinfo:
    """Resolve an explicit IANA timezone name, falling back to the employee's
    business timezone when ``name`` is empty or invalid. Used by per-automation
    schedule overrides (``signal_spec.tz``) so a rule can run on a different
    wall clock than the app default."""
    raw = (name or "").strip()
    if not raw:
        return business_tz()
    try:
        return ZoneInfo(raw)
    except Exception:
        return business_tz()


def now_utc() -> datetime:
    """Timezone-aware *now* in UTC."""
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    """Timezone-aware *now* in the business timezone."""
    return datetime.now(business_tz())


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Coerce a datetime to an aware UTC datetime.

    A naive datetime is assumed to already be UTC (that is how the DB
    stores it). An aware datetime is converted to UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a (naive-UTC or aware) datetime to the business tz."""
    aware = as_utc(dt)
    if aware is None:
        return None
    return aware.astimezone(business_tz())


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Serialise a datetime as ISO-8601 in UTC with a trailing ``Z``.

    This is the ONLY function routes should use to emit a timestamp to
    the SPA. Returns ``None`` for ``None`` so it is safe to map over
    optional columns.
    """
    aware = as_utc(dt)
    if aware is None:
        return None
    # ``isoformat`` on a UTC-aware dt yields ``+00:00``; normalise to the
    # shorter, unambiguous ``Z`` suffix the SPA expects.
    return aware.isoformat().replace("+00:00", "Z")


def parse_to_utc(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string from the SPA into a naive-UTC datetime
    ready to store. Accepts a trailing ``Z`` and explicit offsets; a
    string with NO offset is interpreted in the BUSINESS timezone (the
    SPA sends wall-clock pickers without an offset).

    Returns ``None`` for empty input and raises ``ValueError`` for a
    malformed string so the caller can surface a 422.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    candidate = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(candidate)
    if dt.tzinfo is None:
        # No offset → interpret in the business timezone, then store UTC.
        dt = dt.replace(tzinfo=business_tz())
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def local_stamp(dt: Optional[datetime]) -> str:
    """Render an INSTANT in the business timezone with the tz spelled out
    (``2026-07-11 16:01 (Europe/Madrid, UTC+02:00)``) — the form agent
    PROMPTS must use for any timestamp (v7.97.2). A naive-UTC DB datetime
    rendered bare (``.isoformat()``) loses its timezone: the agent reads the
    UTC digits as wall-clock time and quotes inbound WhatsApp/email times
    shifted by the whole tz offset."""
    local = to_local(dt)
    if local is None:
        return ""
    return (
        f"{local.strftime('%Y-%m-%d %H:%M')} "
        f"({current_timezone()}, UTC{local.strftime('%z')[:3]}:"
        f"{local.strftime('%z')[3:]})"
    )


def local_clock_line() -> str:
    """One-line human description of the current business time + tz, for
    injection into the agent prompt so it reasons in the operator's
    wall clock (e.g. scheduling 'tomorrow at 9')."""
    return local_stamp(now_utc())
