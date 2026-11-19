"""Accountability & operational health — what the employee DID, and what
is blocking it RIGHT NOW, both in business language.

Two read-only surfaces, both ``require_project_access`` and both keyed off
the same business tables the agent already writes through its MCP tools —
NOT the technical ``agent_runs`` (that is the /runs page's job):

* ``GET /api/agent/activity?days=N`` — the business-activity view: per-action
  totals over the window, a daily histogram and a merged, newest-first feed
  of the concrete things the employee did (alerts opened, recommendations
  made, communications sent, approvals decided, tasks completed, decisions
  logged, scorings, automations fired). This is the "rendición de cuentas":
  a human narrative, not cost/latency.
* ``GET /api/agent/health`` — the operational blockers the operator must
  know about, each as a stable ``key`` + structured params (so the SPA
  localises the copy): a stuck approval, a channel down, dormant triggers.
  The connector-down blocker is added by the SPA (it already probes the
  Composio connectors and would otherwise double the probe here).

Both degrade gracefully: a table that does not exist yet (fresh app) or a
read error yields zeros / an empty feed, never a 500 — this is the operator's
diagnostic surface and must always render.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select

from app.app_database import app_session, app_write
# 14.18.0 — WHAT A MEMBER SEES. A member USES the employee (chat, manual, its own encargos);
# it does not operate it. These screens expose everyone's work (1,125 conversations of the
# whole team on the Panel of a member, 04/10/2026), so the door is HERE and the menu only
# reflects it.
from app.auth import TokenPayload, require_admin, require_project_access
from app.timeutil import as_utc, iso_utc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["activity"])


# Each feed source: (kind, Model, timestamp column name, title column name,
# status column name or None, the timestamp that marks "it happened"). We
# keep this declarative so adding a domain entity to the feed is one row.
def _sources():
    from app.app_models.business import (
        Alert, Recommendation, Communication, Scoring, Decision,
        ScheduledItem, Approval,
    )
    return [
        # kind,                model,           when_col,        title_col,   status_col
        ("alert",             Alert,           "created_at",    "title",     "status"),
        ("recommendation",    Recommendation,  "created_at",    "title",     "status"),
        ("communication",     Communication,   "created_at",    "subject",   "status"),
        ("scoring",           Scoring,         "created_at",    "metric",    None),
        ("decision",          Decision,        "created_at",    "decision",  None),
        ("scheduled_item",    ScheduledItem,   "created_at",    "title",     "status"),
        ("approval",          Approval,        "created_at",    "title",     "status"),
    ]


async def _count_since(s, model, col, since) -> int:
    try:
        return int((await s.execute(
            select(func.count()).select_from(model).where(getattr(model, col) >= since)
        )).scalar_one() or 0)
    except Exception:  # pragma: no cover — table may not exist yet
        return 0


@router.get("/activity")
async def get_activity(
    days: int = Query(7, ge=1, le=90),
    user: TokenPayload = Depends(require_admin),
) -> dict[str, Any]:
    since = datetime.utcnow() - timedelta(days=days)  # naive: matches stored utcnow()
    totals: dict[str, int] = {}
    feed: list[dict[str, Any]] = []

    async with app_session() as s:
        for kind, model, when_col, title_col, status_col in _sources():
            totals[kind] = await _count_since(s, model, when_col, since)
            try:
                rows = list((await s.execute(
                    select(model).where(getattr(model, when_col) >= since)
                    .order_by(getattr(model, when_col).desc()).limit(30)
                )).scalars().all())
            except Exception:  # pragma: no cover
                rows = []
            for r in rows:
                feed.append({
                    "kind": kind,
                    "id": getattr(r, "id", None),
                    "title": str(getattr(r, title_col, "") or "")[:160],
                    "status": (str(getattr(r, status_col)) if status_col else None),
                    "at": iso_utc(as_utc(getattr(r, when_col, None))),
                    # Deep-link target for the SPA: every business row records
                    # the run that produced it (created_by_run_id) and some the
                    # event that triggered it (source_event_id). The Actividad
                    # feed turns each item into a link to its run detail (the
                    # outcome-first /runs/<id>), falling back to /events/<id>.
                    "run_id": getattr(r, "created_by_run_id", None),
                    "event_id": getattr(r, "source_event_id", None),
                })

        # Automations that fired in the window (an event, not a business row).
        autos = 0
        try:
            from app.app_models.runtime import Event
            from app.autonomous.executor import AUTOMATION_FIRED_EVENT_TYPE
            autos = int((await s.execute(
                select(func.count()).select_from(Event).where(
                    Event.type == AUTOMATION_FIRED_EVENT_TYPE,
                    Event.detected_at >= since,
                )
            )).scalar_one() or 0)
        except Exception:  # pragma: no cover
            autos = 0
    totals["automation_fired"] = autos

    feed.sort(key=lambda x: x["at"] or "", reverse=True)
    feed = feed[:40]

    # Daily histogram from the feed (already business actions).
    by_day: dict[str, int] = {}
    for it in feed:
        if it["at"]:
            by_day[it["at"][:10]] = by_day.get(it["at"][:10], 0) + 1

    return {
        "window_days": days,
        "since": iso_utc(as_utc(since)),
        "totals": totals,
        "total_actions": sum(totals.values()),
        "feed": feed,
        "by_day": [{"day": d, "count": c} for d, c in sorted(by_day.items())],
    }


# Pending approvals older than this read as "stuck" (the employee is waiting
# on the boss). 24h is the default; it is a heads-up, not a hard SLA.
_APPROVAL_STUCK_HOURS = 24


# ── Status notices: whom they matter to and how long they last ─────────────
#
# v7.170.0. These notices stop living only on the Operations screen and move to the top
# bar, visible from anywhere. That forces answering two questions that were not asked
# before:
#
# **Who sees them?** Almost all of them are about administration —configuring autonomous
# work, fixing a channel, the saturated database— and a `member` can do nothing with them:
# showing them would worry them about something they cannot touch, and on top of that leak
# to them how the employee is built inside. It is filtered on the SERVER, not on the
# screen: an admin notice must not even travel to a member.
#
# **When do they go away?** They are STATES, not events: they are recomputed on every
# query, so as soon as the condition stops being true the notice disappears ON ITS OWN.
# Nobody has to mark anything as read, and no history accumulates. The only thing stored
# is the explicit dismissal —"I know, it is on purpose"— and even that expires
# (see ``app_models/health_dismissals.py``).

# How long a dismissal lasts. There is no "forever": something being accepted today does
# not mean it still will be a month from now, and a permanent silence turns a notice into
# invisible noise.
_DISMISS_DAYS = 30

# What makes each situation UNIQUE, so it can be dismissed without dismissing its
# relatives. Only STABLE parameters go here: putting in one that changes on its own
# —`count`, `wait_ms_max`— would make the dismissal never stick, because the fingerprint
# would be different a second later. A type that does not appear here is identified
# only by its key: its situation is binary (it is there or not).
_HUELLA_PARAMS: dict[str, tuple[str, ...]] = {
    # Another channel going down is a NEW situation: silencing "email is down" cannot
    # silence "email and WhatsApp are down".
    "channels_down": ("names",),
    # Degraded by an old deployment or by a platform that does not answer are different
    # things and are fixed differently.
    "platform_degraded": ("reason",),
}


def _huella(blocker: dict[str, Any]) -> str:
    """Stable identity of the SITUATION (not of the notice type)."""
    params = blocker.get("params") or {}
    partes = [blocker["key"]]
    for nombre in _HUELLA_PARAMS.get(blocker["key"], ()):
        v = params.get(nombre)
        # Sorted: the provider returning the channels in another order cannot invent
        # a new situation.
        if isinstance(v, (list, tuple)):
            v = ",".join(sorted(str(x) for x in v))
        partes.append(f"{nombre}={v}")
    # Severity is part of the identity: the same type in `error` is something else than in
    # `warning`, and dismissing the mild notice cannot cover the serious one.
    partes.append(f"sev={blocker.get('severity')}")
    return "|".join(partes)


async def _visibles_para(blockers: list[dict[str, Any]],
                         user: TokenPayload) -> list[dict[str, Any]]:
    """Filters by ROLE and by current dismissals, and stamps the fingerprint.

    The order matters: first the role (a notice that does not concern you should not even
    reach you) and then the dismissal (which is a decision of yours about something that
    does concern you).
    """
    from app.users_directory import is_admin

    es_admin = await is_admin(user.email)
    # `can_approve` = admin OR explicitly designated in Usuarios ("can approve"), the same
    # criterion `require_approver` uses to let someone decide an approval. It is reused
    # instead of reimplemented: if they diverged, there would be people who get the notice
    # and cannot act, or the other way round.
    try:
        from app.users_directory import can_approve
        es_aprobador = await can_approve(user.email)
    except ImportError:  # pragma: no cover — directory from an older skill
        es_aprobador = es_admin

    def _le_toca(b: dict[str, Any]) -> bool:
        aud = b.get("audience", "admin")
        if aud == "all":
            return True
        if aud == "approver":
            return es_aprobador
        return es_admin

    míos = [b for b in blockers if _le_toca(b)]
    for b in míos:
        b["fingerprint"] = _huella(b)
        # An ERROR cannot be dismissed: it is "you need to do something", and letting it be
        # covered removes the only meaning it has. The screen uses it to not even offer the
        # button.
        b["dismissible"] = b.get("severity") != "error"

    silenciadas = await _descartes_vigentes(user.email)
    return [b for b in míos if b["fingerprint"] not in silenciadas]


async def _descartes_vigentes(email: str) -> set[str]:
    """Fingerprints THIS person has dismissed that are still within their term."""
    try:
        from app.app_models.health_dismissals import HealthDismissal
        async with app_session() as s:
            filas = (await s.execute(
                select(HealthDismissal.fingerprint).where(
                    HealthDismissal.user_email == email,
                    HealthDismissal.muted_until > datetime.utcnow(),
                )
            )).scalars().all()
        return set(filas)
    except Exception:  # pragma: no cover
        # Without the table (app older than 7.170.0) there are no dismissals: all are seen.
        # Failing towards SHOWING is correct — the failure cannot hide notices.
        logger.debug("health: no se pudieron leer los descartes", exc_info=True)
        return set()


@router.post("/health/dismiss")
async def dismiss_health(
    payload: dict[str, Any],
    user: TokenPayload = Depends(require_admin),
) -> dict[str, Any]:
    """Dismisses a status notice for THE CALLER, for 30 days.

    The severity is NOT accepted from the client: the notices are recomputed and the
    fingerprint is looked for among those that person can really see. That way neither an
    error can be dismissed (even if the frontend asked for it), nor a notice that does not
    concern you.
    """
    huella = (payload or {}).get("fingerprint") or ""
    if not huella:
        raise HTTPException(status_code=400, detail="fingerprint requerido")

    actuales = (await get_health(user=user))["blockers"]
    objetivo = next((b for b in actuales if b.get("fingerprint") == huella), None)
    if objetivo is None:
        # It no longer applies (it got resolved while you were looking) or it is not yours.
        # In both cases there is nothing to silence and it is not an error of the caller.
        return {"dismissed": False, "reason": "el aviso ya no está activo para ti"}
    if not objetivo.get("dismissible"):
        raise HTTPException(
            status_code=409,
            detail="Este aviso requiere una acción y no se puede descartar.",
        )

    from app.app_models.health_dismissals import HealthDismissal
    # `app_write`, not `app_session`: THIS WRITES. It was born with `app_session` —the READ
    # session, which does not take the queue— and that is exactly the failure described in
    # the header of app_database.py: a writer outside the queue does not wait its turn, it
    # plays the lottery of SQLite's busy_handler, and under sustained load it can lose
    # repeatedly. Writing a tiny row does not exempt it: what matters is not how much you
    # write, it is that SQLite does not see two writers at once.
    async with app_write() as s:
        s.add(HealthDismissal(
            user_email=user.email, key=objetivo["key"], fingerprint=huella,
            muted_until=datetime.utcnow() + timedelta(days=_DISMISS_DAYS),
        ))
        await s.commit()
    return {"dismissed": True, "days": _DISMISS_DAYS}


@router.get("/health")
async def get_health(
    user: TokenPayload = Depends(require_admin),
) -> dict[str, Any]:
    """Operational blockers in business language. Each blocker: ``key`` (the
    SPA resolves the copy from i18n), ``severity`` and structured ``params``."""
    blockers: list[dict[str, Any]] = []

    # 1) Autonomous work that ISN'T happening — the two ways it can be dormant.
    #
    #    v7.108.0 — this used to be gated on ``AGENT_APP["watchers"]``, but watchers were
    #    RETIRED as a product in v7.27.0 (the employee's vigilance is AUTOMATIONS now), so
    #    for every modern agent ``has_watchers`` is False and this blocker could NEVER
    #    fire: the operator was never told the employee was asleep. The real question is
    #    whether there is anything ARMED to fire:
    #      * master switch OFF with routines defined  → paused (the old intent, now live);
    #      * master switch ON but ZERO routines armed → the panel says "autonomous work
    #        active" and the employee watches NOTHING (INC-KOM-49). Pausing routines is
    #        legitimate; announcing it as active autonomy is not.
    try:
        from app.triggers_state import triggers_active
        from app import automations_service as _asvc
        active = await triggers_active()
        items = await _asvc.list_automations()
        enabled = sum(1 for a in items if a.get("enabled"))
        if items and not active:
            blockers.append({
                "key": "triggers_off", "severity": "warning", "audience": "admin",
                "link": "/operations#disparadores", "params": {},
            })
        elif active and not enabled:
            blockers.append({
                "key": "no_active_automations", "severity": "warning", "audience": "admin",
                "link": "/operations#disparadores",
                "params": {"total": len(items)},
            })
    except Exception:  # pragma: no cover
        logger.debug("health: autonomous-work check failed", exc_info=True)

    # 1.bis) Platform pieces the app CANNOT verify at build time (v7.89.1,
    #    CHANGELOG 7.89.0 §Migration): an older deployer that doesn't inject
    #    WAI_BACKEND_URL/BACKEND_API_SECRET leaves the functional-skills
    #    section silently degraded (empty catalog, no hot-attach) — and since
    #    v7.89.2 the MCP bridge also fails closed without the proxy env, so
    #    this blocker is the operator's signal. ``reason`` distinguishes
    #    "env never injected" (old deployer) from "env set but the platform
    #    doesn't answer / rejects the secret" (TTL-cached live probe).
    try:
        from app.skills_catalog import platform_status
        _pstatus = await platform_status()
        if _pstatus != "ok":
            blockers.append({
                "key": "platform_degraded", "severity": "warning", "audience": "admin",
                "link": "/operations", "params": {"reason": _pstatus},
            })
    except Exception:  # pragma: no cover
        logger.debug("health: platform-deps check failed", exc_info=True)

    async with app_session() as s:
        # 2) Stuck approvals — pending past the threshold.
        try:
            from app.app_models.business import Approval
            floor = datetime.utcnow() - timedelta(hours=_APPROVAL_STUCK_HOURS)
            stuck = list((await s.execute(
                select(Approval).where(
                    Approval.status == "pending", Approval.created_at <= floor
                ).order_by(Approval.created_at.asc()).limit(50)
            )).scalars().all())
            if stuck:
                oldest = as_utc(stuck[0].created_at)
                blockers.append({
                    "key": "approvals_stuck", "severity": "warning", "audience": "approver",
                    "link": "/operations", "params": {
                        "count": len(stuck),
                        "oldest_at": iso_utc(oldest),
                        "hours": _APPROVAL_STUCK_HOURS,
                    },
                })
        except Exception:  # pragma: no cover
            logger.debug("health: approvals check failed", exc_info=True)

        # 3) Channels down — provider rejected creds or transient errors.
        try:
            from app.app_models.channels import Channel
            bad = list((await s.execute(
                select(Channel).where(
                    Channel.is_active == 1,
                    or_(Channel.status == "error", Channel.status == "unauthorized"),
                ).limit(50)
            )).scalars().all())
            if bad:
                blockers.append({
                    "key": "channels_down", "severity": "error", "audience": "admin",
                    "link": "/channels", "params": {
                        "count": len(bad),
                        "names": [c.display_name for c in bad][:5],
                    },
                })
        except Exception:  # pragma: no cover
            logger.debug("health: channels check failed", exc_info=True)

    # 6) The employee's database is choking on itself (INC-VIG-59). Until now this
    #    pathology was ONLY seen as a traceback in the pod's log: the operator saw a 500
    #    when switching autonomy off and had no way of knowing why. If any write has
    #    actually failed because of locking, or the write queue piles up long waits, it
    #    is said here and in business language.
    try:
        from app.app_database import write_contention_stats
        _w = write_contention_stats()
        # v7.169.2 — the notice EXPIRES, and it tells present from past.
        #
        # Before it was enough for `wait_ms_max` —a historical maximum that only goes up—
        # to exceed the threshold: the notice appeared and never went away, worded in the
        # present ("writes ARE waiting"). It could come from a one-off load hours ago with
        # the database calm since then, and there was no way of knowing by looking at the
        # screen.
        #
        # Now there are three states, and they are different things:
        #   · queueing NOW          → present, and the only urgent one
        #   · waits a moment ago    → it happened and may happen again
        #   · waits a while ago     → not notified: it no longer adds anything
        # v7.170.4 — the window is measured on the PEAK, not on the last wait.
        #
        # 7.169.2 looked at `last_wait_seconds_ago`, and that kept alive a notice that was
        # no longer true: the figure shown is `wait_ms_max` —the HISTORICAL peak— but the
        # freshness was checked against ANY wait, including one of milliseconds. Measured
        # on the Kommo: a 15 s peak **17 minutes** ago, a trivial wait 4 seconds ago, and the
        # notice kept announcing the 15 s as if they had just happened. A trickle of minimal
        # waits is enough for an old peak to never expire — the same failure 7.169.2 came to
        # fix, half-fixed.
        #
        # Now: the peak is notified only if the PEAK is recent. The three states are still
        # different:
        #   · queueing NOW              → present, and the only urgent one
        #   · the PEAK was a moment ago → it happened and may happen again
        #   · the peak is old           → not notified, even if the trickle continues
        VENTANA_S = 900  # 15 min since the PEAK ⇒ the notice switches off
        _pico_reciente = (_w.get("max_wait_seconds_ago") is not None
                          and _w["max_wait_seconds_ago"] <= VENTANA_S)
        _ahora = _w["queue_waiting"] > 0
        if _w["locked_errors"] or (_w["wait_ms_max"] >= 5000 and (_pico_reciente or _ahora)):
            blockers.append({
                "key": "db_write_contention",
                "severity": "error" if _w["locked_errors"] else "warning",
                "audience": "admin",
                "link": "/operations", "params": {
                    "locked_errors": _w["locked_errors"],
                    "wait_ms_max": _w["wait_ms_max"],
                    "queue_waiting": _w["queue_waiting"],
                    # What allows wording it in the past once it has passed.
                    "happening_now": _ahora,
                    "max_wait_seconds_ago": _w.get("max_wait_seconds_ago"),
                    "last_wait_seconds_ago": _w.get("last_wait_seconds_ago"),
                    # v7.171.0 — WHO made the others wait. Saying "there is contention"
                    # without saying who holds the lock leaves the operator (and whoever
                    # maintains this) guessing: it is the difference between a notice and
                    # something that can be fixed.
                    "hold_ms_max": _w.get("hold_ms_max"),
                    "hold_max_owner": _w.get("hold_max_owner"),
                    "hold_max_seconds_ago": _w.get("hold_max_seconds_ago"),
                    "slow_holds": _w.get("slow_holds"),
                },
            })
    except Exception:  # pragma: no cover
        logger.debug("health: write-contention check failed", exc_info=True)

    visibles = await _visibles_para(blockers, user)
    return {"blockers": visibles, "ok": not visibles}
