# src/backend/app/mcp_operations.py
"""The employee's hands on ITS OWN application — parity with the UI.

It was odd: you give someone an employee with chat, they ask "how much have you spent
this month?" and the employee has no way of knowing, even though the data is one click
away on its own Consumption screen. Everything that can be done through the interface
and is reasonable to ask for by talking, can be done here.

Two decisions hold this module up:

1. **The UI's handler is REUSED, not reimplemented.** Each tool calls the same function
   that serves the screen, with a ``TokenPayload`` of the real person
   (``mcp_actor.token_for``). That way chat cannot *exceed* the UI by construction: it is
   its same code path, with its same identity, and when someone changes a business rule
   chat inherits it on its own. Duplicating the logic would have created two truths that
   drift apart at the first fix.
2. **The permission is NOT checked here.** The dispatch's single gate does it
   (``mcp_actor.gate_error`` + ``TOOL_MIN_ROLE``) BEFORE calling anything, and that is why
   this module reads without guessing who can do what: each tool's minimum role is
   declared in one single place and there is a test that cross-checks it with this list.
   A loose check inside each handler is exactly how you end up with a tool left open by
   carelessness.

What is NOT here, on purpose, and the catalog (``self_knowledge``) declares as "screen
only": the OAuth wizards (create/reauthorize a channel, connect a connector), uploading
files, changing passwords and the bulk registration of the project's users — that last
one needs the administrator's SESSION to read the list of users from the platform, and
the employee does not have it. It is better that it says so ("that is done in Usuarios →
Añadir del proyecto") than pretending it can.

The docstrings of the tool functions are their MCP descriptions (the agent reads them).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import desc, func, select

from app.app_database import app_session
from app.mcp_actor import resolve_actor, token_for
from app.timeutil import iso_utc

logger = logging.getLogger(__name__)


def _args(call: Any) -> dict:
    return dict(getattr(call, "arguments", None) or {})


async def _actor(call: Any):
    return await resolve_actor(getattr(call, "run_id", 0) or 0)


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


# ── Knowing itself ───────────────────────────────────────────────────


async def describe_app(call: Any):
    """HOW THIS APPLICATION IS USED — your own application. Use it ALWAYS when the
    person asks where something is seen or done ("where do I see what you spend?",
    "how do I register someone?", "what is in Settings?") instead of assuming it: the
    screens and their names depend on THIS employee.

    Arguments: ``topic`` (what the person is looking for, in their words; empty = full
    index). Returns, for each screen: its route, what it is for, what can be done there,
    whether the person asking can open it, what YOU can do through chat and what can only
    be done on the screen. Respect the notice: if the person asking is not an
    administrator, do not offer them what they will not be able to open — tell them whom
    to ask."""
    from app.self_knowledge import describe
    topic = str(_args(call).get("topic") or "")
    return describe(topic, await _actor(call))


# ── Money: how much the employee spends ──────────────────────────────


async def get_consumption(call: Any):
    """HOW MUCH the employee SPENDS: credits of the month, cost and where it goes (per
    person and per concept). It is the same information as the «Consumo» screen.

    Optional arguments: ``days`` (window for the breakdown, 1-90; default 30). Only an
    administrator can see it — if another person asks you for it, do not summarise it for
    them even approximately: tell them an administrator sees it."""
    from app.budget import budget_status, consumption
    from datetime import datetime, timedelta, timezone

    days = _int(_args(call).get("days"), 30, 1, 90)
    now = datetime.now(timezone.utc)
    async with app_session() as s:
        status = await budget_status(s)
        detail = await consumption(s, now - timedelta(days=days), now)
    return {"ok": True, "ventana_dias": days, "presupuesto": status,
            "desglose": detail}


# ── Technical audit: what it has executed and what has reached it ────


async def list_runs(call: Any):
    """The employee's RUNS (what it has worked on, when, with what result and what it
    cost). Like the «Ejecuciones» screen.

    Optional arguments: ``limit`` (1-50, default 10), ``status``
    (``COMPLETED``/``FAILED``/``RUNNING``…), ``mode`` (``chat``/``autonomous``)."""
    from app.app_models.runtime import AgentRun

    a = _args(call)
    limit = _int(a.get("limit"), 10, 1, 50)
    async with app_session() as s:
        stmt = select(AgentRun).order_by(desc(AgentRun.id)).limit(limit)
        if a.get("status"):
            stmt = stmt.where(AgentRun.status == str(a["status"]).strip().upper())
        if a.get("mode"):
            stmt = stmt.where(AgentRun.mode == str(a["mode"]).strip().lower())
        rows = list((await s.execute(stmt)).scalars().all())
    return {"ok": True, "total": len(rows), "ejecuciones": [_run_row(r) for r in rows]}


async def get_run(call: Any):
    """A specific RUN with its detail (status, cost, tokens, error if there was one).
    Argument: ``id``."""
    from app.app_models.runtime import AgentRun, ToolCallLog

    rid = _int(_args(call).get("id"), 0, 0, 10**9)
    if not rid:
        return {"ok": False, "error": "The run id is missing."}
    async with app_session() as s:
        row = (await s.execute(
            select(AgentRun).where(AgentRun.id == rid)
        )).scalar_one_or_none()
        if row is None:
            return {"ok": False, "error": f"Run {rid} does not exist."}
        tools = list((await s.execute(
            select(ToolCallLog.tool_name, ToolCallLog.executed_at)
            .where(ToolCallLog.run_id == rid)
            .order_by(ToolCallLog.id).limit(100)
        )).all())
    return {"ok": True, "ejecucion": _run_row(row),
            "herramientas_usadas": [
                {"herramienta": t[0], "cuando": iso_utc(t[1]) if t[1] else None}
                for t in tools
            ]}


def _run_row(r: Any) -> dict:
    return {
        "id": r.id,
        "estado": r.status,
        "superficie": r.mode,
        "modelo": getattr(r, "model", None),
        "inicio": iso_utc(r.started_at) if getattr(r, "started_at", None) else None,
        "fin": iso_utc(r.finished_at) if getattr(r, "finished_at", None) else None,
        "duracion_seg": getattr(r, "duration_sec", None),
        "coste_usd": getattr(r, "cost_usd", None),
        "tokens_entrada": getattr(r, "tokens_in", None),
        "tokens_salida": getattr(r, "tokens_out", None),
        "error": getattr(r, "error", None),
    }


async def list_events(call: Any):
    """The QUEUE of events that wakes up the employee's work (what has reached it and in
    what state it is). Like the «Eventos» screen.

    Optional arguments: ``limit`` (1-50, default 10), ``status``
    (``PENDING``/``PROCESSING``/``DONE``/``FAILED``), ``type``."""
    from app.app_models.runtime import Event

    a = _args(call)
    limit = _int(a.get("limit"), 10, 1, 50)
    async with app_session() as s:
        stmt = select(Event).order_by(desc(Event.id)).limit(limit)
        if a.get("status"):
            stmt = stmt.where(Event.status == str(a["status"]).strip().upper())
        if a.get("type"):
            stmt = stmt.where(Event.type == str(a["type"]).strip())
        rows = list((await s.execute(stmt)).scalars().all())
    return {"ok": True, "total": len(rows), "eventos": [{
        "id": e.id, "tipo": e.type, "origen": e.source, "estado": e.status,
        "intentos": e.attempts,
        "detectado": iso_utc(e.detected_at) if e.detected_at else None,
    } for e in rows]}


# ── What happens in the business (no money, no internals) ────────────


async def get_activity(call: Any):
    """WHAT the employee HAS DONE lately, in business language (the same as the
    «Actividad» screen). Optional argument: ``days`` (1-90, default 7)."""
    from app.routes.activity_routes import get_activity as _handler
    days = _int(_args(call).get("days"), 7, 1, 90)
    return await _handler(days=days, user=token_for(await _actor(call)))


async def get_health(call: Any):
    """WHETHER SOMETHING PREVENTS IT FROM WORKING, in business language: autonomous work
    stopped, budget exhausted, channels down, write pressure… Use it when the person says
    "you do nothing" or "are you ok?" before apologising blindly."""
    from app.routes.activity_routes import get_health as _handler
    return await _handler(user=token_for(await _actor(call)))


# ── Deciding what the employee parked ────────────────────────────────


async def approve_action(call: Any):
    """APPROVES an action you left parked waiting for permission, and executes it.

    Arguments: ``id`` (the approval's), optional ``feedback``. Only an administrator or
    someone designated to approve can do it, and it is recorded in the name of the person
    who told you — never in yours. Before calling, SUMMARISE what is going to be approved
    and wait for an explicit yes: this really executes something, it is not a query."""
    from app.routes.approvals_routes import approve as _handler
    a = _args(call)
    aid = _int(a.get("id"), 0, 0, 10**9)
    if not aid:
        return {"ok": False, "error": "The approval id is missing."}
    actor = await _actor(call)
    return await _handler(
        approval_id=aid,
        body={"feedback": a.get("feedback")} if a.get("feedback") else {},
        user=token_for(actor),
    )


async def reject_action(call: Any):
    """REJECTS a parked action (it is not executed). Arguments: ``id``, and ``feedback``
    with the reason — the reason matters: it is what tells you how not to propose it the
    same way again."""
    from app.routes.approvals_routes import reject as _handler
    a = _args(call)
    aid = _int(a.get("id"), 0, 0, 10**9)
    if not aid:
        return {"ok": False, "error": "The approval id is missing."}
    return await _handler(
        approval_id=aid,
        body={"feedback": a.get("feedback")} if a.get("feedback") else {},
        user=token_for(await _actor(call)),
    )


async def list_approvers(call: Any):
    """WHO CAN DECIDE (approve or reject) what you park. Use it when someone lacks
    permission for something: instead of leaving them with a "you can't", tell them whom
    to ask by name."""
    from app.users_directory import list_approvers as _list
    people = await _list()
    return {"ok": True, "total": len(people), "pueden_decidir": people}


# ── Governance of autonomous work ────────────────────────────────────


async def get_automation_history(call: Any):
    """THE FUNNEL of a standing order: how many times it checked the "when", how many
    passed the "only if", how many fired, what was held back and how much it has cost.
    It is what is seen in «Trabajo autónomo → Actividad y gobierno».

    Arguments: ``id`` (of the automation), optional ``days`` (1-90, default 30). Use it
    before giving an opinion on whether an order "works" or "does not fire"."""
    from app.automation_stats import for_automations, resolve_range
    from app.automations_service import eval_log_summary, list_watch_items

    a = _args(call)
    aid = _int(a.get("id"), 0, 0, 10**9)
    if not aid:
        return {"ok": False, "error": "The automation id is missing."}
    days = _int(a.get("days"), 30, 1, 90)
    start, end = resolve_range(f"{days}d")
    summary = await eval_log_summary(aid, since=start, until=end)
    stats = await for_automations([aid], start=start, end=end)
    memory = await list_watch_items(aid, limit=50)
    return {"ok": True, "automatizacion_id": aid, "ventana_dias": days,
            "embudo": summary, "cifras": stats,
            "memoria_anti_repeticion": memory}


async def reset_automation_memory(call: Any):
    """FORGETS what a standing order remembers so as not to repeat itself, so that it
    alerts about that again on the next check. It is exactly what is needed when something
    got stuck marked as "already seen".

    Arguments: ``id`` (of the automation) and optionally ``item_key`` (a single item) or
    ``state``. With neither of the two it forgets EVERYTHING of its own: say so before
    doing it and confirm, because the effect is that it will alert about everything again."""
    from app.automations_service import forget_watch_items

    a = _args(call)
    aid = _int(a.get("id"), 0, 0, 10**9)
    if not aid:
        return {"ok": False, "error": "The automation id is missing."}
    actor = await _actor(call)
    out = await forget_watch_items(
        aid,
        item_key=(str(a.get("item_key")).strip() if a.get("item_key") else None),
        state=(str(a.get("state")).strip() if a.get("state") else None),
        by=actor.email or "operator",
    )
    return {"ok": True, **(out if isinstance(out, dict) else {"resultado": out})}


# ── Settings, channels and silences ──────────────────────────────────


async def get_settings(call: Any):
    """THE employee's SETTINGS as they are now: its model, its language, its autonomy
    level, whether autonomous work is active, its concurrency caps and the business's
    time zone."""
    from app.routes.manifest_routes import get_settings as _handler
    return await _handler(user=token_for(await _actor(call)))


async def set_setting(call: Any):
    """CHANGES a setting of the employee. Arguments: the same names ``get_settings``
    returns (e.g. ``autonomy_level``, ``triggers_active``, ``agent_model``,
    ``max_open_tasks``, ``business_timezone``).

    It changes the employee's BEHAVIOUR for everyone, so say in one sentence what you are
    going to change and from what to what, and wait for confirmation before calling."""
    from app.routes.manifest_routes import patch_settings as _handler
    body = {k: v for k, v in _args(call).items() if k not in ("run_id",)}
    if not body:
        return {"ok": False, "error": "You have not said which setting to change."}
    return await _handler(body=body, user=token_for(await _actor(call)))


async def list_channels(call: Any):
    """THE CHANNELS through which people from outside talk to it (WhatsApp, Telegram,
    email, Slack): which ones there are, whether they are active and whether their
    authorization is still alive. Creating or reauthorizing one can NOT be done through
    chat (it takes OAuth and credentials): that is done on the «Canales» screen."""
    from app.routes.channels_routes import list_channels as _handler
    a = _args(call)
    return await _handler(
        user=token_for(await _actor(call)),
        kind=(str(a.get("kind")).strip().lower() if a.get("kind") else None),
        status=(str(a.get("status")).strip().lower() if a.get("status") else None),
    )


async def unsuppress_signal(call: Any):
    """LIFTS a silence: it alerts again about something that was muted. Argument:
    ``id`` (the rule's, as it comes out in ``list_suppressions``).

    Silencing you can do on your own; lifting it is governance, because it means someone
    gets notices again that they had decided not to receive."""
    from app.app_models.signal_suppressions import SignalSuppression
    from app.routes.suppressions_routes import delete_rule as _handler

    rid = _int(_args(call).get("id"), 0, 0, 10**9)
    if not rid:
        return {"ok": False, "error": "The silence id is missing."}
    async with app_session() as s:
        label = (await s.execute(
            select(SignalSuppression.match_label)
            .where(SignalSuppression.id == rid)
        )).scalars().first()
    out = await _handler(rule_id=rid, user=token_for(await _actor(call)))
    if not (out or {}).get("deleted"):
        return {"ok": False, "error": f"Silence {rid} does not exist."}
    return {"ok": True, "levantado": rid, "era": label,
            "efecto": "It alerts about this again on the next check."}


TOOLS: dict[str, Any] = {
    "describe_app": describe_app,
    "get_consumption": get_consumption,
    "list_runs": list_runs,
    "get_run": get_run,
    "list_events": list_events,
    "get_activity": get_activity,
    "get_health": get_health,
    "approve_action": approve_action,
    "reject_action": reject_action,
    "list_approvers": list_approvers,
    "get_automation_history": get_automation_history,
    "reset_automation_memory": reset_automation_memory,
    "get_settings": get_settings,
    "set_setting": set_setting,
    "list_channels": list_channels,
    "unsuppress_signal": unsuppress_signal,
}

# Pure reads: they do not dirty the audit and they have to work in a chat that has not
# opened any run yet (``READ_TOOLS`` in mcp_server).
READ_ONLY: frozenset[str] = frozenset({
    "describe_app", "get_consumption", "list_runs", "get_run", "list_events",
    "get_activity", "get_health", "list_approvers", "get_automation_history",
    "get_settings", "list_channels",
})

# MANDATORY arguments per tool (the rest of the schema is composed by
# ``mcp_server._schema``, which adds the common ``run_id``).
REQUIRED: dict[str, list[str]] = {
    "get_run": ["id"],
    "approve_action": ["id"],
    "reject_action": ["id"],
    "get_automation_history": ["id"],
    "reset_automation_memory": ["id"],
    "unsuppress_signal": ["id"],
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "describe_app": {"topic": {
        "type": "string",
        "description": ("what the person is looking for, in THEIR words (\"consumption\", "
                        "\"registering users\", \"why didn't you warn me\"). Empty = "
                        "full index of the application"),
    }},
    "get_consumption": {"days": {
        "type": "integer",
        "description": "window of the breakdown in days (1-90, default 30)",
    }},
    "list_runs": {
        "limit": {"type": "integer", "description": "how many (1-50, default 10)"},
        "status": {"type": "string", "description": "COMPLETED | FAILED | RUNNING…"},
        "mode": {"type": "string", "enum": ["chat", "autonomous"]},
    },
    "get_run": {"id": {"type": "integer", "description": "id of the run"}},
    "list_events": {
        "limit": {"type": "integer", "description": "how many (1-50, default 10)"},
        "status": {"type": "string", "description": "PENDING | PROCESSING | DONE | FAILED"},
        "type": {"type": "string", "description": "event type"},
    },
    "get_activity": {"days": {"type": "integer", "description": "days (1-90, default 7)"}},
    "get_health": {},
    "approve_action": {
        "id": {"type": "integer", "description": "id of the approval"},
        "feedback": {"type": "string", "description": "optional note from whoever decides"},
    },
    "reject_action": {
        "id": {"type": "integer", "description": "id of the approval"},
        "feedback": {"type": "string", "description": "REASON for the rejection"},
    },
    "list_approvers": {},
    "get_automation_history": {
        "id": {"type": "integer", "description": "id of the automation"},
        "days": {"type": "integer", "description": "window in days (1-90, default 30)"},
    },
    "reset_automation_memory": {
        "id": {"type": "integer", "description": "id of the automation"},
        "item_key": {"type": "string", "description": "forget ONLY this item"},
        "state": {"type": "string", "description": "forget only the ones in this state"},
    },
    "get_settings": {},
    "set_setting": {
        "autonomy_level": {"type": "string"},
        "triggers_active": {"type": "boolean"},
        "agent_model": {"type": "string"},
        "agent_language": {"type": "string"},
        "business_timezone": {"type": "string"},
        "self_schedule": {"type": "boolean"},
        "max_open_tasks": {"type": "integer"},
        "max_concurrent_runs": {"type": "integer"},
        "max_autonomous_concurrent": {"type": "integer"},
        # 15.6.0 — same knobs as Operations (chat/UI parity), in minutes.
        "chat_turn_max_minutes": {"type": "integer",
                                  "description": "max minutes of ONE chat reply (5-60)"},
        "autonomous_run_max_minutes": {"type": "integer",
                                       "description": "max minutes of ONE background job (5-60)"},
    },
    "list_channels": {
        "kind": {"type": "string", "description": "whatsapp | telegram | email | slack"},
        "status": {"type": "string"},
    },
    "unsuppress_signal": {"id": {"type": "integer", "description": "id of the silence"}},
}
