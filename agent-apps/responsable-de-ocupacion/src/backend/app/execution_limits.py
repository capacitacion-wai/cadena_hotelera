"""Runtime, operator-tunable EXECUTION concurrency (v7.45.0) and turn TIME limits (15.6.0).

Two knobs, resolved LIVE (stored operator choice → manifest default → deploy
config), so a change in Operations takes effect WITHOUT a redeploy:

  * ``max_concurrent_runs``       — total events IN EXECUTION at once (1..8).
    Each slot is a live OpenCode session on the sister agent pod, so the ceiling
    (8) protects its CPU/memory.
  * ``max_autonomous_concurrent`` — of those, how many may be AUTONOMOUS
    (background: watcher/cron/scheduled) work at once (1..max_concurrent_runs).
    The remainder (max − autonomous) is ALWAYS available for INBOUND channel/chat
    replies, so a person waiting for an answer is never queued behind background
    runs. This replaces the old fixed ``RESERVED_INBOUND_SLOTS`` with a single
    operator-visible knob (reserved-for-inbound = max_concurrent_runs − autonomous).

And two TIME knobs on the same path (15.6.0), in minutes [5, 60]:

  * ``chat_turn_max_minutes``      — how long ONE chat reply may run (default 30);
    on reaching it the chat ABORTS the run and says so with ``TURN_TIME_LIMIT``.
  * ``autonomous_run_max_minutes`` — how long ONE background job may run (default
    15, or ``AGENT_AUTONOMOUS_TIMEOUT_SEC`` when the deploy set it).

How it stays live: the executor over-provisions its worker pool to the ceiling
and every worker calls :func:`acquire` before claiming an event. ``acquire`` /
``release`` do NO I/O and NO awaits, so under asyncio's cooperative scheduler
they are atomic — the in-flight counters can't race. The effective limits are
read from ``app_settings`` with a short cache (``invalidate_cache`` clears it on
a settings PATCH so the change is felt immediately).
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select

from app.app_database import app_session
from app.app_models.runtime import AppSetting
from app.config import AGENT_APP, MAX_CONCURRENT_RUNS, RESERVED_INBOUND_SLOTS

logger = logging.getLogger(__name__)

CEILING = 8
MAX_RUNS_KEY = "max_concurrent_runs"
MAX_AUTO_KEY = "max_autonomous_concurrent"
_TTL = 5.0

# In-flight counters for THIS pod. Mutated only by acquire/release, which run
# without awaits ⇒ atomic under asyncio.
_total = 0
_auto = 0
_cache: dict = {"at": 0.0, "runs": None, "auto": None}


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _exec_cfg() -> dict:
    ec = AGENT_APP.get("execution")
    return ec if isinstance(ec, dict) else {}


def manifest_max_runs() -> int:
    """Deploy-time default for total concurrency (``agent_app.execution.
    max_concurrent_runs`` → env ``AGENT_MAX_CONCURRENT_RUNS``). Clamped [1, 8]."""
    ec = _exec_cfg()
    try:
        return _clamp(int(ec.get("max_concurrent_runs", MAX_CONCURRENT_RUNS)), 1, CEILING)
    except (TypeError, ValueError):
        return MAX_CONCURRENT_RUNS


def manifest_max_auto(total: int) -> int:
    """Deploy-time default for the autonomous cap. Explicit
    ``agent_app.execution.max_autonomous_concurrent`` wins; otherwise reserve the
    deploy-configured inbound slots (``RESERVED_INBOUND_SLOTS``) for people."""
    ec = _exec_cfg()
    raw = ec.get("max_autonomous_concurrent")
    if raw is not None:
        try:
            return _clamp(int(raw), 1, total)
        except (TypeError, ValueError):
            pass
    return _clamp(total - max(0, RESERVED_INBOUND_SLOTS), 1, total)


async def _setting(key: str):
    try:
        async with app_session() as s:
            return (await s.execute(
                select(AppSetting.value).where(AppSetting.key == key)
            )).scalar_one_or_none()
    except Exception:  # pragma: no cover — never block the executor on a read
        return None


async def effective_limits() -> tuple[int, int]:
    """The EFFECTIVE ``(max_concurrent_runs, max_autonomous_concurrent)`` right
    now — stored operator choice over manifest/config, short-cached. Autonomous
    is always clamped to ``[1, total]``."""
    now = time.monotonic()
    if _cache["runs"] is not None and (now - _cache["at"]) < _TTL:
        return _cache["runs"], _cache["auto"]
    total = manifest_max_runs()
    r = await _setting(MAX_RUNS_KEY)
    if r is not None:
        try:
            total = _clamp(int(r), 1, CEILING)
        except (TypeError, ValueError):
            pass
    auto = manifest_max_auto(total)
    a = await _setting(MAX_AUTO_KEY)
    if a is not None:
        try:
            auto = int(a)
        except (TypeError, ValueError):
            pass
    auto = _clamp(auto, 1, total)
    _cache.update(at=now, runs=total, auto=auto)
    return total, auto


def acquire(max_total: int, max_auto: int) -> str | None:
    """Take an execution slot (ATOMIC — no awaits). Returns:
      * ``"auto"``    — worker may run AUTONOMOUS or inbound work;
      * ``"inbound"`` — the autonomous cap is full, worker serves inbound only;
      * ``None``      — the pod is at total capacity, worker should back off.
    Only ``"auto"`` slots ever run autonomous events, so autonomous in-flight is
    bounded by ``max_auto`` and total in-flight by ``max_total``."""
    global _total, _auto
    if _total >= max_total:
        return None
    if _auto < max_auto:
        _total += 1
        _auto += 1
        return "auto"
    _total += 1
    return "inbound"


def release(role: str | None) -> None:
    """Return a slot taken by :func:`acquire`. ``None`` role is a no-op."""
    global _total, _auto
    if role is None:
        return
    _total = max(0, _total - 1)
    if role == "auto":
        _auto = max(0, _auto - 1)


def snapshot() -> dict:
    """Current in-flight counts for THIS pod (for the Operations indicator)."""
    return {"in_flight_total": _total, "in_flight_autonomous": _auto}


# ── Maximum time of one turn (15.6.0) ──────────────────────────────
#
# Measured on 06/11/2026 in cobsa («Cora»): the chat cut at EXACTLY 600 s (three times in
# one afternoon) while the agent was legitimately working —80 steps of ERP lookups— and
# the person read «stream timeout». The ceiling was hard-coded in `agent_routes`; the one
# of autonomous work could only move through an env var, i.e. with a redeploy. Now they
# are two more knobs of THIS module, on the concurrency's own path: the operator's choice
# in Operations → the manifest default (`agent_app.execution.*`) → the deploy config,
# read LIVE on every turn.
#
# In MINUTES because that is the unit someone decides this in («half an hour»), and with a
# closed range: under 5 an ordinary lookup no longer fits; over 60 a stuck turn burns an
# hour of model with nobody watching.
CHAT_TURN_KEY = "chat_turn_max_minutes"
AUTONOMOUS_RUN_KEY = "autonomous_run_max_minutes"
TIME_LIMIT_MIN = 5
TIME_LIMIT_MAX = 60
CHAT_TURN_DEFAULT_MIN = 30
# 15 = the 900 s of always (`AGENT_AUTONOMOUS_TIMEOUT_SEC`): a deployed employee behaves
# the same until someone touches the knob.
AUTONOMOUS_RUN_DEFAULT_MIN = 15

_time_cache: dict = {"at": 0.0, "chat": None, "auto": None}


def _minutes(raw, default: int) -> int:
    """Whole minutes inside the range; anything unreadable is ``default`` (never raises)."""
    try:
        return _clamp(int(round(float(raw))), TIME_LIMIT_MIN, TIME_LIMIT_MAX)
    except (TypeError, ValueError, OverflowError):
        return default


def manifest_chat_turn_minutes() -> int:
    """Deploy-time default of the chat ceiling: ``agent_app.execution.
    chat_turn_max_minutes`` → 30. No env var on purpose: it never had one (it was 600 s
    written in the code) and a second way in would be a second place to look."""
    raw = _exec_cfg().get("chat_turn_max_minutes")
    return CHAT_TURN_DEFAULT_MIN if raw is None else _minutes(raw, CHAT_TURN_DEFAULT_MIN)


def manifest_autonomous_run_minutes() -> int:
    """Deploy-time default of the autonomous ceiling: ``agent_app.execution.
    autonomous_run_max_minutes`` → ``AGENT_AUTONOMOUS_TIMEOUT_SEC`` (seconds; the deploy
    config layer that already existed, read in ONE place: ``agent_proxy``) → 15."""
    raw = _exec_cfg().get("autonomous_run_max_minutes")
    if raw is not None:
        return _minutes(raw, AUTONOMOUS_RUN_DEFAULT_MIN)
    from app import agent_proxy as _ap  # lazy: agent_proxy imports this module lazily too
    return _minutes(float(_ap.AUTONOMOUS_TIMEOUT_SEC) / 60.0, AUTONOMOUS_RUN_DEFAULT_MIN)


async def effective_time_limits() -> tuple[int, int]:
    """The EFFECTIVE ``(chat, autonomous)`` ceilings in minutes right now — the stored
    choice over manifest/config, with the same short cache as the concurrency."""
    now = time.monotonic()
    if _time_cache["chat"] is not None and (now - _time_cache["at"]) < _TTL:
        return _time_cache["chat"], _time_cache["auto"]
    chat = manifest_chat_turn_minutes()
    c = await _setting(CHAT_TURN_KEY)
    if c is not None:
        chat = _minutes(c, chat)
    auto = manifest_autonomous_run_minutes()
    a = await _setting(AUTONOMOUS_RUN_KEY)
    if a is not None:
        auto = _minutes(a, auto)
    _time_cache.update(at=now, chat=chat, auto=auto)
    return chat, auto


async def chat_turn_seconds() -> float:
    """Ceiling of ONE chat reply (and of what runs through its path: invocations)."""
    return float((await effective_time_limits())[0] * 60)


async def autonomous_run_seconds() -> float:
    """Ceiling of ONE background job (routines, events, replies through channels)."""
    return float((await effective_time_limits())[1] * 60)


def invalidate_cache() -> None:
    """Drop the cached limits so the next claim re-reads app_settings — call
    after a settings PATCH so the change is felt at once."""
    _cache["runs"] = None
    _time_cache["chat"] = None
