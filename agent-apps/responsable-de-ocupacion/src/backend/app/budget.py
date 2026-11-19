"""Per-employee monthly credit budget (R-BUDGET-1) + project budget gate (7.122.0).

ONE source of truth for the agent's spend vs its budget, used by:
* the enforcement gates (autonomous `process_once`, channel via the same path,
  and boss `chat_with_agent`) — when over budget the employee does NOTHING;
* the `GET /budget` endpoint the Operations "Consumo" section renders
  (same-origin, in this app — the WAI platform does NOT aggregate it cross-pod).

Since skill 7.122.0 the gate is TWO-layered:
* the employee's OWN monthly cap (rolling 30d, local ``agent_runs``), as always;
* the PROJECT budget (account balance): the WAI platform exposes
  ``GET /backend/projects/{PROJECT_ID}/budget-status`` (backend V68) with the
  project-wide accumulated spend vs the cap the tenant/marketplace pushed. When
  ``exhausted`` the whole project is out of balance and EVERY employee stops —
  same barrier as the monthly cap, but with a distinguishable reason so the
  operator knows it is the ACCOUNT, not this employee's own limit.
  FAIL-OPEN by design: an old platform backend (no V68 / no endpoint) or a
  network hiccup must NEVER stop the employees — only an explicit
  ``exhausted: true`` blocks.

Spend is read from the LOCAL ``agent_runs`` table (each employee app owns its
own store), so it's exact and never depends on the tenant ledger. The window is
a rolling 30 days (matching the deploy-time cap's semantics). Spend is split by
CONCEPT into two buckets:

* ``autonomo``  — watcher / automation / scheduled runs (the employee acting on
  its own);
* ``peticion``  — reactive runs: the boss CHAT (``mode == "chat"``) and CHANNEL
  replies (the run's event is ``external_message``).

Credits are USD / ``AGENT_COST_PER_CREDIT`` (the same conversion the tenant uses).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from app.app_database import app_session
from app.app_models.runtime import AgentRun, Event
from app.config import (
    AGENT_COST_PER_CREDIT,
    BACKEND_API_SECRET,
    MONTHLY_CREDITS_BUDGET,
    PROJECT_ID,
    WAI_BACKEND_URL,
)
from app.cost_reporter import usd_to_credits

logger = logging.getLogger("wai.budget")

# Rolling window the cap applies over (days).
WINDOW_DAYS = 30

# ── Project budget (account balance, 7.122.0) ───────────────────────────────
#
# Snapshot of GET {WAI_BACKEND_URL}/backend/projects/{PROJECT_ID}/budget-status,
# cached module-level for 60 s: the gate runs on EVERY chat turn and executor
# tick, and the platform answer only moves as fast as the cost ledger — 60 s of
# staleness is invisible to the operator but keeps the backend out of the hot
# path. Auth is ``X-API-Secret: BACKEND_API_SECRET`` — the SAME service
# mechanism this pod already uses against the platform (``skills_catalog``,
# ``runtime_policy_sync``): the pod has no user JWT to forward on autonomous
# ticks, and the platform's ``get_current_user_or_service`` accepts the secret.
_PROJECT_BUDGET_TTL_S = 60.0
_project_budget_cache: dict = {"at": 0.0, "snap": None}


def _project_budget_configured() -> bool:
    return bool(WAI_BACKEND_URL and BACKEND_API_SECRET and PROJECT_ID)


async def project_budget_snapshot() -> Optional[dict]:
    """The platform's project budget-status, or ``None`` when unknowable.

    ``None`` means "no verdict" (env not injected, old backend without the
    endpoint/V68, network failure) and the caller MUST treat it as NOT
    exhausted — fail-open: an old platform can never stop the employees.
    Failures are cached for the same TTL so a dead backend is probed at most
    once a minute instead of on every gate check."""
    if not _project_budget_configured():
        return None
    now = time.monotonic()
    if now - _project_budget_cache["at"] < _PROJECT_BUDGET_TTL_S:
        return _project_budget_cache["snap"]
    snap: Optional[dict] = None
    try:
        import httpx
        url = f"{WAI_BACKEND_URL.rstrip('/')}/backend/projects/{int(PROJECT_ID)}/budget-status"
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url, headers={"X-API-Secret": BACKEND_API_SECRET})
        if resp.status_code == 200:
            body = resp.json()
            if isinstance(body, dict) and "exhausted" in body:
                snap = body
        else:
            # 404 = old backend (no V68/endpoint): fail-open, no noise.
            logger.info("project budget-status HTTP %s (fail-open)", resp.status_code)
    except Exception as e:  # noqa: BLE001 — a budget probe must never raise into a gate
        logger.warning("project budget-status unreachable (fail-open): %s", e)
    _project_budget_cache["at"] = now
    _project_budget_cache["snap"] = snap
    return snap


def _to_usd(raw) -> float:
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


async def _consumed_usd_by_concept(session) -> tuple[float, float]:
    """Return (autonomo_usd, peticion_usd) over the rolling window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    rows = (await session.execute(
        select(AgentRun.cost_usd, AgentRun.mode, Event.type)
        .join(Event, AgentRun.event_id == Event.id, isouter=True)
        .where(AgentRun.started_at >= cutoff)
    )).all()
    autonomo = peticion = 0.0
    for cost_usd, mode, event_type, *_ in rows:
        usd = _to_usd(cost_usd)
        if usd <= 0:
            continue
        # CHAT (boss) or an external channel message → reactive "peticion".
        if (mode or "") == "chat" or (event_type or "") == "external_message":
            peticion += usd
        else:
            autonomo += usd
    return autonomo, peticion


async def budget_status(session) -> dict:
    """Full budget snapshot for the rolling window (credits).

    Since 7.122.0 the payload also carries (ADDITIVE — old consumers keep
    working; the Operations "Consumo" page reads them to explain WHY the
    barrier is up):

    * ``project`` — the platform's project budget-status snapshot
      (``{project_id, budget_usd, consumed_usd, exhausted}``) or ``None`` when
      the platform can't tell (old backend / no cap → fail-open);
    * ``blocked_reason`` — ``None`` | ``"employee_monthly"`` (this employee's
      own cap, existing semantics of ``over_budget``) | ``"project_budget"``
      (account balance exhausted: the PROJECT ran out of balance).
      Employee cap wins when both apply: it is the more specific message.
    """
    autonomo_usd, peticion_usd = await _consumed_usd_by_concept(session)
    cr_auto = usd_to_credits(autonomo_usd)
    cr_pet = usd_to_credits(peticion_usd)
    consumed = round(cr_auto + cr_pet, 4)
    budget = round(float(MONTHLY_CREDITS_BUDGET or 0), 4)
    capped = budget > 0
    over = capped and consumed >= budget
    project = await project_budget_snapshot()
    blocked_reason = None
    if over:
        blocked_reason = "employee_monthly"
    elif project and project.get("exhausted"):
        blocked_reason = "project_budget"
    return {
        "window_days": WINDOW_DAYS,
        "unlimited": not capped,
        "budget_credits": budget,
        "consumed_credits": consumed,
        "by_concept": {
            "autonomo": round(cr_auto, 4),
            "peticion": round(cr_pet, 4),
        },
        "remaining_credits": round(max(0.0, budget - consumed), 4) if capped else None,
        "pct": round(min(100.0, consumed / budget * 100.0), 1) if capped else None,
        "over_budget": over,
        "cost_per_credit": AGENT_COST_PER_CREDIT,
        # 7.122.0 (additive): PROJECT budget layer + reason for the block.
        "project": project,
        "blocked_reason": blocked_reason,
    }


def _strip_wa(s: str) -> str:
    """``whatsapp:+34600111222`` → ``+34600111222`` for a readable by-user label."""
    s = (s or "").strip()
    return s[len("whatsapp:"):] if s.lower().startswith("whatsapp:") else s


def _attribute_user(mode, meta_json, event_type, payload_json, sender_lookup=None) -> str:
    """Who generated a *petición* run — for the by-user breakdown.

    The resolved HUMAN identity is persisted on EVERY interactive run — boss
    chat AND channel — as ``request_meta_json.memory_user`` (see
    ``persist_run_memory_user`` / ``resolve_inbound_scope``): the operator
    email for chat, the sender email for an email channel, and the
    operator-mapped email (``ChannelContact``) for a WhatsApp number. Prefer
    it for BOTH modes — that is why the old channel branch showed "canal": it
    ignored ``memory_user`` and read a ``sender`` the event payload never
    carried.

    When no identity was resolved (e.g. a WhatsApp number with no
    ``ChannelContact`` mapping), fall back to the RAW channel sender so the
    row is at least identified by number/address instead of a generic bucket:
    the ``sender`` on the event payload, else the ``ChannelMessage.sender``
    resolved via ``(channel_id, provider_message_id)`` in ``sender_lookup``.
    """
    # 1) Resolved identity (email, or WhatsApp number mapped to an email) —
    #    persisted for chat AND channel runs alike.
    try:
        who = (json.loads(meta_json or "{}").get("memory_user") or "").strip().lower()
        if who:
            return who
    except Exception:
        pass
    # 2) Channel fallback: raw sender (payload → ChannelMessage), else bucket.
    if (event_type or "") == "external_message":
        try:
            p = json.loads(payload_json or "{}")
            for k in ("sender", "from", "from_addr", "from_email", "author"):
                who = (p.get(k) or "").strip().lower()
                if who:
                    return _strip_wa(who)
            if sender_lookup is not None:
                ref = (p.get("channel_id"), str(p.get("provider_message_id") or ""))
                who = (sender_lookup.get(ref) or "").strip().lower()
                if who:
                    return _strip_wa(who)
        except Exception:
            pass
        return "canal"
    # 3) Boss chat with no recorded operator identity (rare).
    if (mode or "") == "chat":
        return "chat"
    return "otros"


async def consumption(session, dt_from: datetime, dt_to: datetime) -> dict:
    """Date-ranged consumption analytics (credits): a daily time-series split by
    concept, the by-concept totals, and the by-user breakdown of *petición*
    spend (who ran the boss chat / which channel sender). Separate from
    ``budget_status`` (which is the fixed rolling-30d gauge the cap enforces)."""
    rows = (await session.execute(
        select(
            AgentRun.cost_usd, AgentRun.mode, AgentRun.started_at,
            AgentRun.request_meta_json, Event.type, Event.payload_json,
            AgentRun.solicitante,
        )
        .join(Event, AgentRun.event_id == Event.id, isouter=True)
        .where(AgentRun.started_at >= dt_from)
        .where(AgentRun.started_at < dt_to)
    )).all()

    # Pre-resolve channel senders for runs whose identity wasn't persisted
    # (e.g. an unmapped WhatsApp number): the sender lives on ChannelMessage,
    # keyed by (channel_id, provider_message_id) from the event payload. One
    # bulk query so the by-user breakdown is retroactive for old rows too.
    refs = set()
    # Unpacked with `*_`: this loop only looks at the event type and its payload, and
    # tying it to the exact number of columns broke it when `solicitante` was added to the
    # select —a 500 in the consumption view, the whole query down because of a comma—.
    for _c, _m, _s, _meta, _etype, _pj, *_ in rows:
        if (_etype or "") == "external_message":
            try:
                p = json.loads(_pj or "{}")
                cid, pmid = p.get("channel_id"), str(p.get("provider_message_id") or "")
                if cid is not None and pmid:
                    refs.add((cid, pmid))
            except Exception:
                pass
    sender_lookup: dict = {}
    if refs:
        from app.app_models.channels import ChannelMessage
        cids = {c for c, _ in refs}
        pmids = {m for _, m in refs}
        cm_rows = (await session.execute(
            select(
                ChannelMessage.channel_id,
                ChannelMessage.provider_message_id,
                ChannelMessage.sender,
            )
            .where(ChannelMessage.channel_id.in_(cids))
            .where(ChannelMessage.provider_message_id.in_(pmids))
        )).all()
        for cid, pmid, sender in cm_rows:
            sender_lookup[(cid, str(pmid or ""))] = sender or ""

    series_usd: dict[str, dict] = {}
    auto_usd = pet_usd = orden_usd = voz_usd = 0.0
    by_user_usd: dict[str, dict] = {}
    for cost_usd, mode, started_at, meta_json, event_type, payload_json, solicitante, *_ in rows:
        usd = _to_usd(cost_usd)
        if usd <= 0:
            continue
        day = (started_at.date().isoformat() if started_at else dt_from.date().isoformat())
        bucket = series_usd.setdefault(
            day, {"autonomo": 0.0, "peticion": 0.0, "ordenar": 0.0, "voz": 0.0})
        # 12.12.0 — FOURTH concept: TALKING to the employee. It is split from «petición»
        # because it is paid differently —audio costs twenty times more than text— and
        # because the question that must be answerable is "how much is it costing us that
        # people talk to it?". Mixed with the written chat, that question cannot be
        # answered and the decision of whether it pays off is taken blind.
        if (mode or "") == "voz":
            voz_usd += usd
            bucket["voz"] += usd
            quien = (solicitante or "").strip().lower() or "?"
            u = by_user_usd.setdefault(quien, {"usd": 0.0, "runs": 0, "voz_usd": 0.0})
            u["usd"] += usd
            u["voz_usd"] = u.get("voz_usd", 0.0) + usd
            u["runs"] += 1
            continue
        # 7.154.0 — THIRD concept: what it costs for the employee to keep itself TIDY
        # (distilling its memory and its learning), separate from what it costs to work.
        # Mixed, it could not be seen, and that is how 14 consolidation rounds burned tokens
        # without merging anything and nobody noticed. Now it has its own line.
        if (event_type or "") == "self_memory_maintenance" or (event_type or "") == "memory_maintenance":
            orden_usd += usd
            bucket["ordenar"] += usd
            continue
        is_peticion = (mode or "") == "chat" or (event_type or "") == "external_message"
        if is_peticion:
            pet_usd += usd
            bucket["peticion"] += usd
            who = _attribute_user(mode, meta_json, event_type, payload_json, sender_lookup)
            u = by_user_usd.setdefault(who, {"usd": 0.0, "runs": 0, "voz_usd": 0.0})
            u["usd"] += usd
            u["runs"] += 1
        else:
            auto_usd += usd
            bucket["autonomo"] += usd

    # Continuous daily series (fill empty days) so the chart has no gaps.
    series: list[dict] = []
    day = dt_from.date()
    end = dt_to.date()
    guard = 0
    while day <= end and guard < 400:
        b = series_usd.get(day.isoformat(),
                           {"autonomo": 0.0, "peticion": 0.0, "ordenar": 0.0, "voz": 0.0})
        series.append({
            "date": day.isoformat(),
            "autonomo": round(usd_to_credits(b["autonomo"]), 4),
            "peticion": round(usd_to_credits(b["peticion"]), 4),
            "ordenar": round(usd_to_credits(b.get("ordenar", 0.0)), 4),
            "voz": round(usd_to_credits(b.get("voz", 0.0)), 4),
        })
        day = day + timedelta(days=1)
        guard += 1

    by_user = sorted(
        (
            {
                "who": w,
                "credits": round(usd_to_credits(v["usd"]), 4),
                # Per person BOTH figures are needed: how much of their spend is talking and
                # how much is writing. With only the total you cannot tell for whom it pays off.
                "credits_voz": round(usd_to_credits(v.get("voz_usd", 0.0)), 4),
                "credits_peticion": round(
                    usd_to_credits(max(0.0, v["usd"] - v.get("voz_usd", 0.0))), 4),
                "runs": v["runs"],
            }
            for w, v in by_user_usd.items()
        ),
        key=lambda x: x["credits"],
        reverse=True,
    )
    return {
        "from": dt_from.isoformat(),
        "to": dt_to.isoformat(),
        "consumed_credits": round(usd_to_credits(auto_usd + pet_usd + orden_usd + voz_usd), 4),
        "by_concept": {
            "autonomo": round(usd_to_credits(auto_usd), 4),
            "peticion": round(usd_to_credits(pet_usd), 4),
            # "Tidying up what was learned": distilling its memory. It is maintenance, not work.
            "ordenar": round(usd_to_credits(orden_usd), 4),
            # "Talking": voice conversation. See the comment above.
            "voz": round(usd_to_credits(voz_usd), 4),
        },
        "series": series,
        "by_user": by_user,
        "cost_per_credit": AGENT_COST_PER_CREDIT,
    }


async def budget_block_reason() -> Optional[str]:
    """Why the budget barrier is up, or ``None`` when the employee may act.

    ``"employee_monthly"`` — this employee's OWN rolling-30d credit cap
    (R-BUDGET-1, the historical gate).
    ``"project_budget"``  — the PROJECT's balance is exhausted (backend V68
    budget-status): the account ran dry, so every employee of the project
    stops, distinguishable from the employee's own limit.

    Both layers are fail-open on read errors: a broken/old platform or a local
    DB hiccup must never hard-block the agent."""
    try:
        budget = round(float(MONTHLY_CREDITS_BUDGET or 0), 4)
        if budget > 0:
            async with app_session() as s:
                autonomo_usd, peticion_usd = await _consumed_usd_by_concept(s)
            # Same rounding as budget_status: the gauge and the gate must match.
            consumed = round(usd_to_credits(autonomo_usd) + usd_to_credits(peticion_usd), 4)
            if consumed >= budget:
                return "employee_monthly"
    except Exception:  # never let a budget-read failure hard-block the agent
        pass
    try:
        snap = await project_budget_snapshot()
        if snap and snap.get("exhausted"):
            return "project_budget"
    except Exception:  # fail-open (snapshot already is, belt and braces)
        pass
    return None


async def is_over_budget() -> bool:
    """Cheap gate used by the enforcement paths. False when uncapped.

    Since 7.122.0 it is true for EITHER layer (employee monthly cap OR project
    balance exhausted) — the barrier behaves identically; callers that need to
    word the message use ``budget_block_reason()``."""
    return (await budget_block_reason()) is not None


async def _user_peticion_usd(session, email: str) -> float:
    """Rolling-window *petición* USD attributed to one human. Mirrors
    ``budget_status``' window + concept split; attribution via the persisted
    ``memory_user`` (present on every interactive run — chat AND channel)."""
    e = (email or "").strip().lower()
    if not e:
        return 0.0
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    rows = (await session.execute(
        select(AgentRun.cost_usd, AgentRun.mode, AgentRun.request_meta_json,
               Event.type, Event.payload_json)
        .join(Event, AgentRun.event_id == Event.id, isouter=True)
        .where(AgentRun.started_at >= cutoff)
    )).all()
    total = 0.0
    for cost_usd, mode, meta_json, event_type, payload_json, *_ in rows:
        usd = _to_usd(cost_usd)
        if usd <= 0:
            continue
        if not ((mode or "") == "chat" or (event_type or "") == "external_message"):
            continue
        if _attribute_user(mode, meta_json, event_type, payload_json) == e:
            total += usd
    return total


async def user_cap_status(email: str) -> Optional[dict]:
    """Per-user credit cap gate. Returns None when the caller has NO cap set (or
    no email); else ``{cap, consumed, over}`` (credits, rolling window). The cap
    is an OPTIONAL per-person limit an operator sets in the Usuarios page —
    stored in the directory entry's ``metadata_json.credit_cap`` — so one boss/
    member can't drain the whole employee's shared budget. Independent of, and
    additional to, the global ``MONTHLY_CREDITS_BUDGET``."""
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return None
    try:
        from app.app_models.users import AppUser
        from app.users_directory import loads_meta
        async with app_session() as s:
            meta = (await s.execute(
                select(AppUser.metadata_json)
                .where(AppUser.email == e, AppUser.active == 1)
            )).scalars().first()
            cap = float(loads_meta(meta).get("credit_cap") or 0)
            if cap <= 0:
                return None
            consumed = round(usd_to_credits(await _user_peticion_usd(s, e)), 4)
        return {"cap": round(cap, 4), "consumed": consumed,
                "remaining": round(max(0.0, cap - consumed), 4),
                "over": consumed >= cap}
    except Exception:  # a cap-read failure must never hard-block the agent
        return None


async def is_user_over_cap(email: str) -> bool:
    """True when this human has a personal cap and has reached it."""
    st = await user_cap_status(email)
    return bool(st and st["over"])
