# src/backend/app/mcp_actor.py
"""Who is asking for this — the SINGLE gate of the employee's tools.

The agent reaches the backend through ``POST /mcp`` with a SERVICE secret
(``APP_MCP_TOKEN``): that channel **does not carry the human's identity**. While the
tools only materialized business findings it did not matter, but since v7.146.0 the
agent can do through chat what is done through the UI (consumption, approving, audit,
settings…) and then "not knowing who is asking" is a leak: a ``member`` would ask for
the spend through chat and get it, because the tool has nobody to say no to.

This module closes that with three decisions:

1. **The identity is DERIVED on the server, from the run.** Never from an argument the
   model passes (forgeable) nor from a pod header (it is a service one):
   ``agent_runs.request_meta_json.memory_user`` is stamped by the chat endpoint when
   starting the turn, and ``agent_runs.mode`` says on which surface it happened. It
   fails CLOSED: without a resolvable run there is no human and no permission.
2. **Only chat proves that a person is present.** ``mode='chat'`` rows are minted
   EXCLUSIVELY by the manager's chat endpoint; autonomous and channel runs are
   ``'autonomous'`` (in a channel, moreover, on the other side there is a phone, not an
   authenticated session). So "human present" ≡ chat turn, which is exactly the
   criterion the automations guard already used.
3. **The permission is decided with the SAME functions as the UI** (``require_admin``,
   ``require_approver`` from ``auth.py``, over the employee's directory). There is no
   second definition of "who can": if tomorrow the UI's changes, chat follows it on its
   own. It is Single Definition applied to permissions.

And a fourth one that is what prevents the future accident: **every tool MUST declare
its minimum role** in ``TOOL_MIN_ROLE``. The dispatch looks this table up, and an
undeclared tool is DENIED (with an error log, because it is a bug of the skill, not of
the user). A new tool cannot be born open by carelessness: either you declare it, or it
does not work — and ``tests/test_tool_permissions.py`` checks it table by table.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select

from app.app_database import app_session

logger = logging.getLogger(__name__)

# ── Levels ───────────────────────────────────────────────────────────
#
# Ordered from least to most demanding. The name says WHO, not WHAT:
#
#   agent    — the employee can do it on its own, also in an autonomous run
#              (at 3 in the morning there is nobody to ask).
#              It is the level of everything that is ITS work: recording an alert,
#              reading its data, remembering something.
#   human    — requires a person present (chat turn with a resolved email).
#              For what is done ON BEHALF of someone.
#   approver — an administrator or whoever they designated ("can approve").
#   admin    — a REAL administrator of the employee, governance of the employee.
LEVEL_AGENT = "agent"
LEVEL_HUMAN = "human"
LEVEL_APPROVER = "approver"
LEVEL_ADMIN = "admin"
LEVELS = (LEVEL_AGENT, LEVEL_HUMAN, LEVEL_APPROVER, LEVEL_ADMIN)


@dataclass(frozen=True)
class Actor:
    """Who is behind this call, resolved on the server.

    ``email``/``role`` are empty in an autonomous run: there is nobody there, and that is
    NOT an error — it is the employee working alone. What cannot happen is that case
    slipping through a door that requires a person.
    """

    email: Optional[str] = None
    surface: Optional[str] = None      # chat | autonomous | channel | None
    role: Optional[str] = None         # admin | member | None (no human)
    can_approve: bool = False

    @property
    def is_human(self) -> bool:
        """Is there a person present who has typed this? Only chat proves it."""
        return bool(self.email) and self.surface == "chat"

    @property
    def is_admin(self) -> bool:
        return self.is_human and self.role == "admin"

    @property
    def may_read_as_admin(self) -> bool:
        """Can it QUERY administrator things without being in the chat?

        Deliberate read/write asymmetry (7.153.0). Chat is the only surface that proves
        there is an authenticated person in front, and that is why it is still the only
        one that can CHANGE something. But denying an administrator a read-only query
        because they write from Telegram —when their handle is in their record and the
        directory identifies them— is a door that protects nothing and confuses: the
        employee answered "this is only for administrators, ask admin@…" to the admin
        themselves.

        What authorizes here is the DIRECTORY: the handle is in the record because an
        administrator put it there. Same criterion already used to give them their
        memory. To MUTATE, chat is still required.
        """
        return bool(self.email) and self.role == "admin"

    @property
    def may_approve(self) -> bool:
        return self.is_human and (self.role == "admin" or self.can_approve)

    def describe(self) -> str:
        """For the prompt and for the messages: in one line, without unnecessary PII."""
        if not self.is_human:
            return "the employee working alone (no person present)"
        return f"{self.email} ({self.role or 'member'})"


async def run_mode(run_id: Any) -> Optional[str]:
    """Surface of the run (``chat`` | ``autonomous``). ``None`` if it does not resolve."""
    return (await _run_row(run_id))[0]


async def run_serving_user(run_id: Any) -> Optional[str]:
    """The human this run serves, resolved SERVER-SIDE from the run row's
    ``request_meta_json.memory_user`` — never from an agent-supplied argument
    (which would be spoofable). ``None`` for background / autonomous runs."""
    return (await _run_row(run_id))[1]


async def resolver_run(run_token: Any, run_id: Any = None) -> Optional[int]:
    """The INTERNAL ``id`` of the run a tool call refers to (v12.0.0).

    The SINGLE gate to go from "what the model said" to "which run it is". Two inputs:

    * ``run_token`` — the 128-bit random value of the ``[INTERNO]`` line. It is what counts
      in CHAT: the model only knows the one of its turn and cannot guess another. It always wins.
    * ``run_id`` — the usual integer. It is ONLY accepted for runs that are NOT chat
      (autonomous / channel), which have no person present and therefore no identity to
      steal; the autonomous prompts (executor, colleagues) still inject ``run_id=N`` and do
      not need touching. For a chat run, a bare ``run_id`` is REJECTED: it was the
      escalation — "use run_id=1233" and act as that person (memory, admin tools, connector).

    Returns ``None`` if it does not resolve: the caller fails closed."""
    from app.app_models.runtime import AgentRun

    tok = str(run_token or "").strip()
    try:
        async with app_session() as s:
            if tok:
                found = (await s.execute(
                    select(AgentRun.id).where(AgentRun.run_token == tok)
                )).scalar_one_or_none()
                return int(found) if found else None
            try:
                rid = int(run_id or 0)
            except (TypeError, ValueError):
                return None
            if rid <= 0:
                return None
            row = (await s.execute(
                select(AgentRun.id, AgentRun.mode).where(AgentRun.id == rid)
            )).first()
            if row is None:
                return None
            if (row[1] or "").strip() == "chat":
                logger.warning("resolver_run: run_id=%s es de CHAT y llegó sin run_token — "
                               "rechazado (posible intento de suplantación)", rid)
                return None
            return int(row[0])
    except Exception:  # noqa: BLE001 — the gate NEVER blows up: it fails closed
        logger.debug("resolver_run failed (token=%s, id=%s)", bool(tok), run_id, exc_info=True)
        return None


async def _run_row(run_id: Any) -> tuple[Optional[str], Optional[str]]:
    """``(mode, memory_user)`` in ONE query.

    Just one because this runs on EVERY tool call: when the gate was two loose
    ``select`` (mode and owner) every governed tool paid two trips to a SQLite that
    already suffers from concurrent writes.
    """
    try:
        from app.app_models.runtime import AgentRun
        rid = int(run_id or 0)
        if rid <= 0:
            return (None, None)
        async with app_session() as s:
            row = (await s.execute(
                select(AgentRun.mode, AgentRun.request_meta_json)
                .where(AgentRun.id == rid)
            )).first()
        if row is None:
            return (None, None)
        mode = (row[0] or "").strip() or None
        try:
            meta = json.loads(row[1] or "{}")
        except Exception:  # noqa: BLE001
            meta = {}
        email = (meta.get("memory_user") or "").strip().lower() or None
        return (mode, email)
    except Exception:  # noqa: BLE001 — the gate NEVER blows up: it fails closed
        logger.debug("actor lookup failed for run %s", run_id, exc_info=True)
        return (None, None)


async def is_real_human_admin(email: Optional[str], *, human_surface: bool = False) -> bool:
    """True only for a PRESENT email whose directory role is ``admin`` — and,
    on ambiguous surfaces, NOT the autonomous service identity.

    The autonomous identity resolves to ``admin`` for anti-lockout, but on a
    background run it is the employee serving ITSELF, never a human
    authorising a change — hence the exclusion. HOWEVER, many deployments set
    ``AGENT_AUTONOMOUS_USER_EMAIL`` to a REAL admin's email (e.g. both
    ``admin@wai.com``): a plain email comparison then misclassifies the human
    boss as "not a person" and locks them out of automations/knowledge from
    their own chat (v7.93.1). The unambiguous discriminator is the RUN
    SURFACE, not the email: pass ``human_surface=True`` when the caller
    already proved a present human typed the request (``agent_runs.mode ==
    'chat'`` rows are minted ONLY by the boss-chat endpoint) and the
    exclusion is skipped; on autonomous/channel surfaces it stands. Fails
    closed."""
    e = (email or "").strip().lower()
    if not e:
        return False
    if not human_surface:
        auto = (os.environ.get("AGENT_AUTONOMOUS_USER_EMAIL") or "").strip().lower()
        if e == auto:
            return False
    try:
        from app.users_directory import resolve_role
        return (await resolve_role(e)) == "admin"
    except Exception:  # noqa: BLE001
        return False


async def resolve_actor(run_id: Any) -> Actor:
    """The actor of this call. Single source for the gates and for the prompt."""
    mode, email = await _run_row(run_id)
    if not email:
        # Nobody identified: autonomous run or a run that does not resolve.
        return Actor(email=email, surface=mode)
    if mode != "chat":
        # Channel (Telegram/WhatsApp/email) with the sender IDENTIFIED by the directory:
        # it is not "person present" —it cannot change anything— but we do know who it is,
        # so it can QUERY what is theirs. See `may_read_as_admin`.
        try:
            from app.users_directory import resolve_role
            rol = await resolve_role(email)
        except Exception:  # noqa: BLE001 — falla cerrado
            rol = None
        return Actor(email=email, surface=mode, role=rol)
    try:
        from app.users_directory import can_approve, resolve_role
        role = await resolve_role(email)
        approver = await can_approve(email)
    except Exception:  # noqa: BLE001 — fails closed: a person without privileges
        logger.debug("actor role lookup failed for %s", email, exc_info=True)
        role, approver = "member", False
    return Actor(email=email, surface=mode, role=role, can_approve=approver)


# ── Minimum role per tool (MANDATORY DECLARATION) ────────────────────
#
# Rule: EVERY entry of ``mcp_server.TOOLS`` has to be here. The dispatch denies what is
# not declared (fail-closed) and the static test ``test_tool_permissions.py`` breaks the
# build if an entry is missing or left over.
#
# When declaring a new tool ask yourself who uses it: if the employee needs it to work at
# night it is ``agent``; if it queries or changes the employee's GOVERNANCE (money,
# permissions, technical audit, settings, silences) it is ``admin``; if it DECIDES a
# parked action it is ``approver``.
TOOL_MIN_ROLE: dict[str, str] = {
    # Asking for the location requires a PERSON present: it is being asked of them. In an
    # autonomous or channel run there is nobody to ask, and the tool says so instead of hanging.
    "pedir_ubicacion": LEVEL_HUMAN,
    # ── Its work: reading and materializing business. It has to work alone.
    "list_alerts": LEVEL_AGENT,
    "list_recommendations": LEVEL_AGENT,
    "list_decisions": LEVEL_AGENT,
    "list_scheduled_items": LEVEL_AGENT,
    "list_communications": LEVEL_AGENT,
    "list_scoring": LEVEL_AGENT,
    "list_approvals": LEVEL_AGENT,
    "local.read": LEVEL_AGENT,
    "local.write": LEVEL_AGENT,
    "describe_entities": LEVEL_AGENT,
    "upsert_alert": LEVEL_AGENT,
    "register_recommendation": LEVEL_AGENT,
    "log_communication": LEVEL_AGENT,
    "upsert_scoring": LEVEL_AGENT,
    "log_decision": LEVEL_AGENT,
    "resolve_alert": LEVEL_AGENT,
    "decide_recommendation": LEVEL_AGENT,
    "decide_decision": LEVEL_AGENT,
    "schedule_task": LEVEL_AGENT,
    "assign_task_to_user": LEVEL_AGENT,
    "complete_scheduled_item": LEVEL_AGENT,
    # Asking for permission and noting that it executed what was approved is its work; DECIDING is not.
    "request_approval": LEVEL_AGENT,
    "mark_approval_executed": LEVEL_AGENT,
    # Silencing and proposing are its own (reversible and traced); lifting is governance.
    "suppress_signal": LEVEL_AGENT,
    "list_suppressions": LEVEL_AGENT,
    "propose_silence": LEVEL_AGENT,
    # Knowledge: asking for it is its own; pinning it degrades to a PROPOSAL if not admin
    # (the tool itself resolves it, that is why it is not ``admin`` here: raising it would
    # turn into an "I can't" what today is "I leave it proposed").
    "request_knowledge": LEVEL_AGENT,
    "set_knowledge": LEVEL_AGENT,
    "search_knowledge": LEVEL_AGENT,
    "list_knowledge_docs": LEVEL_AGENT,
    "get_knowledge_doc": LEVEL_AGENT,
    # Memory: the owner is bound by the run (``mcp_memory_bind``), not by the role.
    "remember_fact": LEVEL_AGENT,
    "record_episode": LEVEL_AGENT,
    "index_file": LEVEL_AGENT,
    "forget_fact": LEVEL_AGENT,
    "compact_memory": LEVEL_AGENT,
    "list_memory": LEVEL_AGENT,
    "search_memory": LEVEL_AGENT,
    "remember_learning": LEVEL_AGENT,
    "search_learnings": LEVEL_AGENT,
    "forget_learning": LEVEL_AGENT,
    "compact_learnings": LEVEL_AGENT,
    # Directory: looking and adding a contact yes (always as member, it never escalates);
    # the bulk registration from the project is governance.
    "find_user": LEVEL_AGENT,
    "list_users": LEVEL_AGENT,
    "create_user": LEVEL_AGENT,
    "list_approvers": LEVEL_AGENT,
    # Knowing HOW it uses itself can be asked by anyone; what is filtered by role is the
    # CONTENT (the tool itself does it with the actor).
    "describe_app": LEVEL_AGENT,
    # What happens in the business, in business language: no money nor internals.
    "get_activity": LEVEL_AGENT,
    "get_health": LEVEL_AGENT,
    # ── Governance of the employee: real administrator, in chat.
    "get_automation_catalog": LEVEL_ADMIN,
    "list_automations": LEVEL_ADMIN,
    "create_automation": LEVEL_ADMIN,
    "update_automation": LEVEL_ADMIN,
    # Evolving = creating a derived one; same level as creating (7.151.0).
    "evolve_automation": LEVEL_ADMIN,
    "set_automation_enabled": LEVEL_ADMIN,
    "test_automation": LEVEL_ADMIN,
    "run_automation": LEVEL_ADMIN,
    "get_automation_history": LEVEL_ADMIN,
    "reset_automation_memory": LEVEL_ADMIN,
    "get_consumption": LEVEL_ADMIN,
    "list_runs": LEVEL_ADMIN,
    "get_run": LEVEL_ADMIN,
    "list_events": LEVEL_ADMIN,
    "list_channels": LEVEL_ADMIN,
    "get_settings": LEVEL_ADMIN,
    "set_setting": LEVEL_ADMIN,
    "unsuppress_signal": LEVEL_ADMIN,
    # ── Deciding a parked action: admin or designated.
    "approve_action": LEVEL_APPROVER,
    "reject_action": LEVEL_APPROVER,
}


async def _who_to_ask(level: str) -> list[str]:
    """Whom to send the person to when the gate closes on them.

    A bare "you don't have permission" leaves the user in a dead end and the model making
    up whom to ask. We return real names from the directory.
    """
    try:
        from app.users_directory import list_approvers
        people = await list_approvers()
    except Exception:  # noqa: BLE001
        return []
    if level == LEVEL_ADMIN:
        people = [p for p in people if p.get("role") == "admin"]
    return [str(p.get("display_name") or p.get("email")) for p in people][:5]


# 7.153.0 — Administrator tools that ONLY READ and are therefore allowed to an administrator
# identified by the directory even if they write from a channel. The list is EXPLICIT on
# purpose: nothing gets in here for looking like a read, it gets in because someone checked
# it mutates nothing. Everything that changes something still requires chat.
ADMIN_READ_TOOLS = frozenset({
    "get_consumption",          # "how much have I consumed?" — the case that motivated it
    "get_automation_catalog", "list_automations", "get_automation_history",
    "list_runs", "get_run", "list_events", "list_channels",
    # DELIBERATELY OUT: `get_settings` (it can expose sensitive configuration) and
    # everything that mutates — create/update/evolve/set_*/test/run/reset/unsuppress.
})


async def gate_error(tool: str, actor: Actor) -> Optional[dict[str, Any]]:
    """``None`` if it can; otherwise the ACTIONABLE reason the model will see.

    The text is in the employee's first person because the model paraphrases it to the user
    (in their language): if we put «403 Forbidden» here, that is what a person ends up
    reading in the chat.
    """
    level = TOOL_MIN_ROLE.get(tool)
    if level is None:
        # Undeclared tool: it is a bug of the skill. It is denied anyway (an undeclared
        # capability is an unreviewed capability) and it is shouted in the log.
        logger.error(
            "tool %r has no declared minimum role in TOOL_MIN_ROLE — denied", tool,
        )
        return {"ok": False, "denied": True, "tool": tool,
                "error": "That capability is not authorized in this employee."}
    if level == LEVEL_AGENT:
        return None
    if not actor.is_human:
        # 7.153.0 — READ/WRITE ASYMMETRY. From a channel (Telegram, WhatsApp, email)
        # there is no authenticated session, so nothing can be CHANGED. But if the
        # directory identifies the sender as an administrator, denying them a read-only
        # QUERY protects nothing: the employee answered "this is only for administrators,
        # ask admin@…" to the very admin who was writing to it.
        if tool in ADMIN_READ_TOOLS and actor.may_read_as_admin:
            return None
        desde_canal = bool(actor.email) and actor.surface not in (None, "chat")
        return {"ok": False, "denied": True, "tool": tool, "required": level,
                "error": ("This requires changing something, and for security I only do it "
                          "from the chat, where your session is authenticated. Write to me "
                          "there and we will do it."
                          if desde_canal else
                          "I can only do this when a person identified in the chat asks "
                          "me to, not on my own.")}
    if level == LEVEL_HUMAN:
        return None
    if level == LEVEL_ADMIN and actor.is_admin:
        return None
    if level == LEVEL_APPROVER and actor.may_approve:
        return None

    who = await _who_to_ask(level)
    ask = f" You can ask {' or '.join(who)}." if who else ""
    if level == LEVEL_APPROVER:
        return {"ok": False, "denied": True, "tool": tool, "required": level,
                "who_can": who,
                "error": ("Only an administrator or someone designated to approve can "
                          "take this decision." + ask)}
    return {"ok": False, "denied": True, "tool": tool, "required": level,
            "who_can": who,
            "error": ("This is part of the employee's governance and only an "
                      "administrator can see or change it." + ask)}


def token_for(actor: Actor):
    """The actor, in the shape the REST handlers expect.

    The parity tools REUSE the UI's handler instead of reimplementing it, and that requires
    a ``TokenPayload``. We are not granting access: this person ALREADY passed
    ``require_project_access`` when their chat session was created, so here only what they
    already proved is rematerialized. ``is_superuser=False`` on purpose: the role that rules
    is the one of the employee's DIRECTORY (INC-AGO-39), and marking it would sneak in any
    platform superuser as admin of this employee.
    """
    from app.auth import TokenPayload
    from app.config import DB_SCHEMA, PROJECT_ID
    return TokenPayload(
        email=actor.email or "",
        is_superuser=False,
        project_ids=[PROJECT_ID],
        data_areas=[DB_SCHEMA] if DB_SCHEMA else [],
    )
