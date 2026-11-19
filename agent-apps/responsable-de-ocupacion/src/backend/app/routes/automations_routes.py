"""Automations — CRUD + capability catalog + dry-run.

Business-facing standing orders (WHEN→THEN). The same service backs the
chat MCP tools, so chat and forms write one store.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select

from app import automations_service as svc
from app.app_database import app_session
from app.auth import require_project_access, require_admin, TokenPayload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent/automations", tags=["automations"])

# Business standing-order TEMPLATES are authored PER-AGENT by the code agent at
# generation time — real, domain-specific, pro use cases for THIS employee —
# and shipped as ``automation_templates.{es,en}.json`` next to this app's code.
# (There is intentionally NO generic hardcoded template here; an employee's
# templates must fit its business. See SKILL.md / patterns/automations.md.)
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent  # …/app


# What a template has to bring to BE of any use. The form is filled ENTIRELY from `body`
# (`setEditing({...tpl.body})`), so a template with a half `body` saves no work: it leaves
# the operator in front of a blank form with two mandatory fields and no hint of what to
# write. Measured on 18/08/2026 in three workshop employees: the 8 templates brought
# `label` and `description` and an EMPTY `body`.
_CAMPOS_DE_PLANTILLA = ("title", "signal_type", "reaction_type", "reaction_instructions")


def _plantilla_completa(t: object) -> bool:
    """Does this template fill the form, or leave it blank?"""
    if not isinstance(t, dict):
        return False
    if not str(t.get("key") or "").strip() or not str(t.get("label") or "").strip():
        return False
    cuerpo = t.get("body")
    if not isinstance(cuerpo, dict):
        return False
    return all(str(cuerpo.get(c) or "").strip() for c in _CAMPOS_DE_PLANTILLA)


def _load_automation_templates(lang: str) -> list:
    """Load this employee's business templates for ``lang`` (fallback es→en→[]).
    Each file is a JSON list: [{key, label, description, body:{…automation…}}].

    Incomplete ones are NOT served. It is preferable not to offer a template than to offer
    one that opens an empty form: the latter looks like a product failure in front of the
    customer, and on top of that it costs more than starting from scratch, because you have
    to guess what it intended. It is logged with its key so it can be fixed."""
    for cand in (lang, "es", "en"):
        p = _TEMPLATES_DIR / f"automation_templates.{cand}.json"
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("automation_templates.%s.json is malformed", cand)
            return []
        if not isinstance(data, list):
            return []
        buenas = [t for t in data if _plantilla_completa(t)]
        if len(buenas) != len(data):
            faltan = [str((t or {}).get("key") or (t or {}).get("label") or "?")
                      for t in data if not _plantilla_completa(t)]
            logger.warning(
                "automation_templates.%s.json: %d de %d plantillas incompletas y NO se "
                "sirven (%s). Cada una necesita key, label y un body con %s.",
                cand, len(faltan), len(data), ", ".join(faltan[:8]),
                ", ".join(_CAMPOS_DE_PLANTILLA))
        return buenas
    return []


async def _agent_language() -> str:
    """The employee's language for its texts (``agent_locale.resolve_agent_language``: the
    operator's choice, else the Definition's primary language, else es)."""
    from app.agent_locale import resolve_agent_language
    return await resolve_agent_language()


@router.get("")
async def list_automations(user: TokenPayload = Depends(require_project_access)):
    """The list the table view renders. Deliberately WITHOUT the funnel/cost
    numbers: this endpoint is polled every 30s, and the range-aware stats are a
    separate ``/stats`` call the UI makes once per range change (see the module
    docstring of ``automation_stats``)."""
    items = await svc.list_automations()
    # v7.140.0 — ``learned_from``: when a standing order exists because the
    # employee LEARNED something, the row says so. One batch lookup; a row a
    # human created simply comes back without a badge.
    try:
        from app import learning_effects_service as lfx
        origins = await lfx.origin_for(
            lfx.TARGET_AUTOMATION, [i.get("id") for i in items])
        for it in items:
            found = origins.get(str(it.get("id")))
            if found:
                it["learned_from"] = found
    except Exception:  # pragma: no cover — a missing badge never 500s the list
        logger.debug("learned_from enrichment failed", exc_info=True)
    return {"items": items}


@router.get("/stats")
async def automations_stats(
    range: str = "7d", since: str | None = None, until: str | None = None,
    user: TokenPayload = Depends(require_project_access),
):
    """Funnel + accumulated cost per automation over a RANGE (1d/7d/30d/90d/all
    or a custom ``since``/``until``).

    Separate from the list on purpose: the operator changes the range far less
    often than the list refreshes, and these are aggregate queries. Returns the
    resolved window so the UI can label exactly what it is showing, and
    ``rollup_from`` when part of the window comes from the daily summary instead
    of the row-by-row ledger.
    """
    from app import automation_provenance, automation_stats
    from app.config import AUTOMATION_EVAL_LOG_RETENTION_DAYS
    out = await automation_stats.for_automations(
        None, preset=range, since=since, until=until,
        retention_days=AUTOMATION_EVAL_LOG_RETENTION_DAYS)
    # "How many of its things are still open" travels WITH the rows so the table can
    # show it as a column — batched (one query per entity kind for ALL
    # automations), never one lookup per row. Lifetime, not range-scoped: what is
    # still open is a question about NOW.
    try:
        # Automations with lifetime produced/open but ZERO funnel activity in the
        # window used to appear as `{produced, open}` only — setdefault created an
        # empty slot and VIG-52 failed ("embudo no informa 'checks'"). Seed the
        # same zeroed funnel shape `for_automations` would have emitted.
        _empty_funnel = {
            "checks": 0, "met": 0, "fired": 0, "silenced": 0, "errors": 0,
            "held": 0, "runs": 0, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0,
            "met_rate": None, "fire_rate": None, "cost_per_fire": None,
            "rollup_from": None,
        }
        for aid, counts in (await automation_provenance.open_counts_batch()).items():
            slot = out["items"].setdefault(str(aid), dict(_empty_funnel))
            slot["produced"] = counts.get("produced", 0)
            slot["open"] = counts.get("open", 0)
    except Exception:  # pragma: no cover — a missing column never 500s the table
        logger.debug("open-counts batch failed", exc_info=True)
    return out


@router.get("/catalog")
async def catalog(user: TokenPayload = Depends(require_project_access)):
    """Building blocks for THIS employee so the wizard/chat offer real options
    (never invents). Sourced from the manifest + channels + system events."""
    from app.config import AGENT_APP, DATABASE_URL as _PROJECT_DB_URL, APP_TIMEZONE
    aa = AGENT_APP if isinstance(AGENT_APP, dict) else {}
    # Is the project DB reachable (i.e. the wai-data connector is wired)? When
    # not (e.g. a SaaS-operator employee like Kommo), the editor disables the
    # "project data" source for conditions. We read the composed connection URL
    # (empty unless host+database are set — see _build_project_database_url),
    # NOT the PROJECT_DB_RO credential dict, which belongs to app.database's
    # read-only session factory.
    project_data_available = bool(_PROJECT_DB_URL)
    # Data entities (for data_condition signals): manifest domain_entities.
    entities = []
    for e in (aa.get("domain_entities") or []):
        entities.append({
            "slug": e.get("slug") or e.get("table"),
            "label": e.get("label") or e.get("slug") or e.get("table"),
            "table": e.get("table") or e.get("slug"),
            "source": e.get("source") or "project",
        })
    # Business events: manifest event_types + the platform system events.
    events = [{"type": t, "label": t} for t in (aa.get("event_types") or [])]
    from app.routes.manifest_routes import _SYSTEM_EVENT_SPECS
    for ev, meta in _SYSTEM_EVENT_SPECS.items():
        # v7.134.0 — only OFFER events that make sense as a "when" trigger.
        # DELEGATED_TASK / APPROVAL_GRANTED are internal plumbing (redundant as
        # triggers) → triggerable=False → hidden here (still documented as a
        # capability by the manifest).
        if not meta.get("triggerable", True):
            continue
        events.append({"type": ev, "label": meta["label"], "system": True})
    # Channels (for channel_message signals + notify/deliverable reactions).
    channels = []
    try:
        from app.app_models.channels import Channel
        async with app_session() as s:
            for c in (await s.execute(select(Channel))).scalars().all():
                channels.append({"id": c.id, "label": c.display_name, "kind": c.kind})
    except Exception:
        pass
    return {
        "signals": {
            "schedule": {"available": True},
            "data_condition": {
                "available": bool(entities),
                "entities": entities,
                "aggregates": ["count", "sum", "avg", "min", "max"],
                "operators": ["gt", "lt", "gte", "lte", "eq", "no_activity"],
                "descriptive_supported": False,
            },
            "channel_message": {"available": bool(channels), "channels": channels},
            "business_event": {"available": True, "events": events},
            "manual": {"available": True},
        },
        "reactions": {
            "notify": {"available": True, "channels": channels},
            "draft_and_approve": {"available": True},
            "create_task": {"available": True},
            "generate_deliverable": {
                "available": True,
                "formats": [
                    {"id": "pdf", "label": "PDF"},
                    {"id": "xlsx", "label": "Excel"},
                    {"id": "pptx", "label": "Presentación"},
                ],
            },
            "act": {"available": True, "requires_optin": True},
        },
        # 7.127.0 — cost-first building blocks for the UI/chat: condition modes
        # ordered cheapest→costliest, the check comparators, and the reaction cost axis.
        "condition_modes": ["watch", "checks", "data", "sql", "agent"],
        "check_operators": ["gte", "gt", "lte", "lt", "eq", "non_empty", "empty",
                            "contains", "not_contains"],
        "execution_modes": ["deterministic", "agent"],
        "defaults": {"enabled": False, "requires_approval": True,
                     "execution_mode": "agent", "combine": "all", "on_error": "alert"},
        # Employee's business timezone — the DEFAULT for a schedule rule's
        # wall clock. A rule may override it per-automation via signal_spec.tz.
        "business_tz": APP_TIMEZONE,
        # v7.134.0 — context variables the "What to notify" (notify) message editor can
        # INSERT. These are the tokens ``_render_templates`` actually substitutes
        # (so a templated/0-token notify sends a real value); anything else is left
        # verbatim. When the notify is composed by the employee (execution_mode=
        # "agent") it ALSO folds in the detected data automatically — that isn't a
        # literal token, it's noted to the operator in the UI.
        "context_variables": [
            {"token": "{{tenant}}", "label": "Nombre del negocio"},
            {"token": "{{now}}", "label": "Fecha y hora actual"},
            {"token": "{{now+1d}}", "label": "Desplazado: {{now+2h}}, {{now-30m}}…"},
        ],
        "project_data_available": project_data_available,
        # Real columns of the LOCAL store tables, so a data/sql condition (or
        # the agent in chat) references columns that exist (e.g.
        # channel_messages.received_at, not created_at).
        "local_schema": await svc.local_schema(),
        # Business standing-order templates — REAL, domain-specific, pro use
        # cases authored per-agent by the code agent at generation time and
        # shipped as automation_templates.{es,en}.json. Returned in the
        # employee's configured language (fallback es→en→[]). The wizard/chat
        # prefill from these. NO generic fallback: an empty list just means the
        # code agent hasn't authored this employee's templates yet.
        "templates": _load_automation_templates(await _agent_language()),
    }


@router.post("")
async def create_automation(
    body: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    try:
        return await svc.create_automation(body, by=getattr(user, "email", ""), source="form")
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/evaluate-condition")
async def evaluate_condition_adhoc(
    body: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    """AD-HOC 'Test' of a condition WITHOUT saving it: evaluates the ``condition_spec``
    exactly as it is in the editor and returns the result BY PARTS (mode-aware). For
    ``checks`` it includes each signal (value, met, error). 0 tokens except ``agent``
    (which is not pre-evaluated). Body: ``{condition_spec: {...}}`` (or the spec directly)."""
    spec = body.get("condition_spec") if isinstance(body.get("condition_spec"), dict) else body
    return await svc.evaluate_condition_detail(spec or {})


@router.patch("/{aid}")
async def update_automation(
    aid: int,
    body: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(require_admin),
):
    try:
        out = await svc.update_automation(aid, body, by=getattr(user, "email", ""))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if out is None:
        raise HTTPException(404, f"automation {aid} not found")
    return out


@router.delete("/{aid}")
async def delete_automation(aid: int, user: TokenPayload = Depends(require_admin)):
    return {"deleted": await svc.delete_automation(aid)}


@router.get("/{aid}/history")
async def automation_history(
    aid: int, limit: int = 50, user: TokenPayload = Depends(require_project_access),
):
    """Every time this automation fired and what the employee did about it —
    each entry links to the run (full traceability) and the event."""
    a = await svc.get_automation(aid)
    if a is None:
        raise HTTPException(404, f"automation {aid} not found")
    return {"items": await svc.automation_history(aid, limit)}


@router.get("/{aid}/evals")
async def automation_evals(
    aid: int, limit: int = 100, only_notable: bool = False,
    range: str = "7d", since: str | None = None, until: str | None = None,
    user: TokenPayload = Depends(require_project_access),
):
    """Trace of EACH evaluation of the 'only if' (incl. the ones that did NOT fire): when it
    was checked, what it gave (checks/watch), whether it fired and why. Different from
    /history (which are the fires with their run). `only_notable`=True leaves only
    fires/errors/resolutions — so as not to drown in 'all ok'.

    v7.140.0 — same range as the governance panel (`range`/`since`/`until`), and the
    summary comes as a FUNNEL. If no explicit range is requested, everything is returned:
    earlier callers do not change behaviour."""
    a = await svc.get_automation(aid)
    if a is None:
        raise HTTPException(404, f"automation {aid} not found")
    from app.automation_stats import resolve_range
    scoped = bool((since or "").strip() or (until or "").strip() or range != "7d")
    start, end, label = resolve_range(range, since, until) if scoped else (None, None, "all")
    return {
        "items": await svc.list_eval_log(
            aid, limit=limit, only_notable=only_notable, since=start, until=end),
        "summary": await svc.eval_log_summary(aid, since=start, until=end),
        "range": label,
    }


@router.get("/{aid}/stats")
async def automation_stats_one(
    aid: int, range: str = "7d", since: str | None = None, until: str | None = None,
    user: TokenPayload = Depends(require_project_access),
):
    """The funnel + cost of ONE automation in the chosen range, plus WHAT it has
    generated (incidents, tasks, decisions… and how many are still open).

    It is the governance card: "this standing order has been checked 340 times, the
    only-if passed 12, it fired 4, silenced 8, it has cost 0.42 USD and has left 3
    incidents (1 open)". Without this, deciding whether it is worth it is a hunch."""
    a = await svc.get_automation(aid)
    if a is None:
        raise HTTPException(404, f"automation {aid} not found")
    from app import automation_provenance, automation_stats
    from app.config import AUTOMATION_EVAL_LOG_RETENTION_DAYS
    stats = await automation_stats.for_automations(
        [aid], preset=range, since=since, until=until,
        retention_days=AUTOMATION_EVAL_LOG_RETENTION_DAYS)
    return {
        "range": stats["range"],
        "funnel": stats["items"].get(str(aid), {}),
        # Provenance is lifetime, not range-scoped: "what is still open because
        # of this automation" is a question about NOW, not about a window.
        "produced": await automation_provenance.produced_by(aid),
    }


@router.get("/{aid}/memory")
async def automation_memory(
    aid: int, state: str | None = None, limit: int = 200,
    user: TokenPayload = Depends(require_project_access),
):
    """WHAT THE AUTOMATION REMEMBERS so as not to repeat itself (per-item state).

    Until now this memory was invisible: if the employee stopped alerting about something
    because "it already saw it", the operator had no way to check it or reset it. Here
    each watched item is seen, in what state it is, how many times it has been seen and
    when it fired — and with the DELETE it can be forgotten."""
    a = await svc.get_automation(aid)
    if a is None:
        raise HTTPException(404, f"automation {aid} not found")
    return await svc.list_watch_items(aid, state=state, limit=limit)


@router.delete("/{aid}/memory")
async def automation_memory_forget(
    aid: int, item_key: str | None = None, state: str | None = None,
    user: TokenPayload = Depends(require_admin),
):
    """FORGET the anti-repetition memory — ADMIN.

    Three scopes, from the finest to the coarsest: one item (``item_key``), those of a
    state (``state``, e.g. ``resolved`` to clean historical noise) or all. HONEST
    consequence the UI must warn about: what is forgotten may alert again on the next
    check — which is exactly what the operator wants when resetting by hand."""
    a = await svc.get_automation(aid)
    if a is None:
        raise HTTPException(404, f"automation {aid} not found")
    return await svc.forget_watch_items(
        aid, item_key=item_key, state=state,
        by=getattr(user, "email", None) or "operator")


@router.post("/{aid}/evolve")
async def automation_evolve(
    aid: int, body: dict = Body(default_factory=dict),
    user: TokenPayload = Depends(require_admin),
):
    """EVOLVE: creates a NEW automation from this one — ADMIN.

    It is the path to "change" something that came from the deployment: the original is
    not touched and the copy is born pointing at it (`derived_from_id`), keeping which
    template it came from. It is born DISABLED: an evolution is reviewed by a person
    before it fires."""
    out = await svc.evolve_automation(
        aid, body or {}, by=getattr(user, "email", None) or "operator")
    if out is None:
        raise HTTPException(404, f"automation {aid} not found")
    return out


@router.post("/{aid}/reset-cooldown")
async def automation_reset_cooldown(
    aid: int, user: TokenPayload = Depends(require_admin),
):
    """RE-ARM: clears the cooldown without executing the reaction — ADMIN.

    Until now the only way to unstick an automation in cooldown was to press Play (which
    bypasses it by EXECUTING), that is, to cause the work in order to re-arm it. Here the
    two things are separated. A trace stays in the history."""
    a = await svc.get_automation(aid)
    if a is None:
        raise HTTPException(404, f"automation {aid} not found")
    return await svc.reset_cooldown(aid, by=getattr(user, "email", None) or "operator")


@router.delete("/{aid}/silence")
async def automation_clear_silence(
    aid: int, user: TokenPayload = Depends(require_admin),
):
    """Remove the watch's temporary silence — ADMIN.

    The ``silence_until`` lives inside ``condition_spec.watch`` and was not visible on any
    screen: an automation silenced for a maintenance window looked too much like one that
    has nothing to say."""
    a = await svc.get_automation(aid)
    if a is None:
        raise HTTPException(404, f"automation {aid} not found")
    return await svc.clear_silence(aid, by=getattr(user, "email", None) or "operator")


@router.post("/{aid}/test")
async def test_automation(aid: int, user: TokenPayload = Depends(require_admin)):
    """Dry-run: evaluate the agent's CONDITION (the second gate) now and report
    whether it WOULD hold, without running the reaction. The signal itself
    (schedule/channel/event) is not pollable on demand, but the data/sql
    condition is; the natural-language ``agent`` condition is decided by the
    employee at fire time, so it can't be dry-run here."""
    a = await svc.get_automation(aid)
    if a is None:
        raise HTTPException(404, f"automation {aid} not found")
    cond = a.get("condition_spec") or {}
    mode = (cond.get("mode") or "").lower()
    # Legacy: a data_condition signal carries its check in signal_spec.
    if not mode and a["signal_type"] == "data_condition":
        value, ctx = await svc.evaluate_data_condition(a["signal_spec"])
        return {"evaluable": True, "would_fire": svc.condition_met(value, a["signal_spec"]),
                "value": value, "context": ctx}
    if mode == "agent":
        # v7.63.0 — a NATURAL-LANGUAGE condition can now be dry-run: queue a
        # READ-ONLY verification run. It reports its verdict on the automation's
        # history WITHOUT parking an approval or acting (test_only).
        if not (cond.get("instructions") or "").strip():
            return {"evaluable": False,
                    "reason": "Añade primero qué debe comprobar el empleado."}
        res = await svc.run_condition_check_now(aid, by=getattr(user, "email", ""), test_only=True)
        return {"evaluable": True, "queued": bool(res and res.get("queued")),
                "async": True,
                "reason": "Comprobación lanzada — el empleado la está verificando. "
                          "Verás el resultado en el historial en unos instantes."}
    if mode == "watch":
        # v7.136.0 — FAITHFUL dry-run of the watch with the saved automation's real MEMORY
        # (same extract+diff as the real fire, read-only): tool response, items_at
        # resolution, per-item classification (new/escalated/already seen/ignored), verdict + why.
        detail = await svc.dry_run_watch((cond.get("watch") or {}), aid)
        return {"evaluable": bool(detail.get("evaluable", True)),
                "would_fire": bool(detail.get("would_fire")), **detail}
    if mode in ("", "always"):
        return {"evaluable": True, "would_fire": True, "value": None, "context": {"mode": mode or "always"}}
    met, ctx = await svc.evaluate_condition(a)
    return {"evaluable": True, "would_fire": met, "value": ctx.get("value"), "context": ctx}


@router.post("/{aid}/run")
async def run_automation(aid: int, check: bool = False,
                         user: TokenPayload = Depends(require_admin)):
    """Runs this automation NOW (manual Play). It works even on a disabled ``manual``
    automation (it is an explicit order from the operator). TWO MODES (v7.139.0):
      * ``check=false`` (default, FORCE) — fires the reaction IGNORING the "only if".
      * ``check=true`` (RUN CHECKING) — evaluates the "only if" and only fires if it is met;
        if not, it does not act and returns ``fired=false, met=false``.
    The reaction is queued as a run; follow it in Trabajo autónomo → history."""
    result = await svc.fire_now(aid, by=user.email or "", check_condition=bool(check))
    if result is None:
        raise HTTPException(404, f"automation {aid} not found")
    return result
