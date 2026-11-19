"""Shared logic for Automatizaciones — used by the REST routes, the watcher
(signal evaluation), the executor (reaction) and the chat MCP tools, so all
four paths agree on ONE behaviour.

Design: the LLM/form produces a STRUCTURED automation at authoring time; this
module evaluates it DETERMINISTICALLY at runtime (no per-tick LLM) and builds
the reaction prompt. Data-condition signals reuse the watcher's safe query
helpers (read-only project DB, identifier validation).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete as sql_delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.app_database import app_session, app_write
from app.app_models.automations import Automation, AutomationState
from app.timeutil import iso_utc

logger = logging.getLogger(__name__)

# VALID signals (accepted when creating/editing). It includes ``channel_message`` and the
# legacy ``data_condition`` so as NOT to break existing automations nor the watcher's
# arrival-matched machinery (which keeps firing those rows).
SIGNAL_TYPES = ("schedule", "data_condition", "channel_message", "business_event", "manual")
# v7.134.0 — signals OFFERED when creating a new one (UI + the code agent's catalog).
# Without ``channel_message``: an inbound message is ALREADY handled by the employee (reply
# flow), so "when a message arrives" was redundant and confused the operator. Legacy rows
# are still accepted, evaluated and edited; it just stops being offered as an option for
# new automations.
OFFERED_SIGNAL_TYPES = ("schedule", "data_condition", "business_event", "manual")
# The 4 orthogonal OUTCOMES (what the employee does). Approval is a separate axis
# (requires_approval); cost is a separate axis (execution_mode). "draft_and_approve"
# was act+approval and is MIGRATED away — kept only as an input alias for old rows.
REACTION_TYPES = ("notify", "create_task", "act", "generate_deliverable")
# Legacy reaction_type → canonical, applied on write/read so pre-existing employees
# and old chat calls keep working. draft_and_approve also forces requires_approval.
_REACTION_ALIASES = {"draft_and_approve": "act"}
# Condition modes for condition_spec.mode. "checks" = the deterministic MCP-tool
# checklist (0 tokens); "agent" = the LLM check (costly). Any mode may also carry a
# cheap deterministic ``pre`` guard (data|sql|checks) that must hold first.
CONDITION_MODES = ("always", "data", "sql", "checks", "agent", "watch")
# How a reaction is produced (COST axis).
EXECUTION_MODES = ("agent", "deterministic")
# Numeric comparisons supported by data_condition (deterministic, cheap).
_COMPARATORS = ("gt", "lt", "gte", "lte", "eq", "no_activity")
# Extra comparators for MCP-tool checks (on top of _COMPARATORS): existence/text.
_CHECK_OPS = _COMPARATORS + ("non_empty", "empty", "contains", "not_contains")
# Comparators that are MEANINGLESS without a threshold/value — the validator rejects them empty
# (a "contains" with empty needle or a "gte" with no umbral is a mis-configured, silently-broken
# check). ``non_empty``/``empty`` are the ONLY ops that need no threshold.
_OPS_NEED_THRESHOLD = ("gt", "lt", "gte", "lte", "eq", "no_activity", "contains", "not_contains")
# v7.138.1 — the validation of PATHS (which EXECUTES the tool) is best-effort and TIME-BOUNDED:
# a slow tool (e.g. a tenants sweep of 60-120 s) CANNOT block saving (it gave 504 at the
# gateway). Per-path timeout + TOTAL budget, WELL below the proxy's timeout (~60 s). The
# STRUCTURAL validation (shape, empty threshold…) is instantaneous and is mandatory.
_VALIDATE_PATH_TIMEOUT_S = 8
_VALIDATE_PATHS_BUDGET_S = 20
# v7.148.0 — checks/watch runtime: the MCP proxy holds 120 s; cluster-mcp sweep = 120 s.
# Before 30 s → the tenants sweep was cut (~25-30 s) and the automation did NOT see
# tenant_error_logs / scan_incomplete / skipped_slow (false "incomplete coverage").
_MCP_TOOL_CALL_TIMEOUT_S = 130.0


def canonical_reaction(rtype: str) -> str:
    """Map a (possibly legacy) reaction_type to one of the canonical 4."""
    r = (rtype or "notify").strip().lower()
    return _REACTION_ALIASES.get(r, r)


# ── (de)serialisation ────────────────────────────────────────────────
def _json(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def row_to_dict(a: Automation, st: Optional[AutomationState] = None,
                last_fire_status: Optional[str] = None) -> dict:
    out = {
        "id": a.id,
        "title": a.title,
        "description": a.description or "",   # v7.138.0 — functional description (HTML)
        "template_key": a.template_key,        # v7.139.0 — source template (for "already added"/dedup)
        # v7.151.0 — sealed lineage: with which template version it was born and which
        # automation it evolves from. Read-only: fixed on creation (see SEALED_FIELDS).
        "template_version": getattr(a, "template_version", None),
        "derived_from_id": getattr(a, "derived_from_id", None),
        "area_tag": a.area_tag,
        "enabled": bool(a.enabled),
        "serves_user_email": a.serves_user_email,
        "priority": a.priority,
        "signal_type": a.signal_type,
        "signal_spec": _json(a.signal_spec),
        "signal_summary": a.signal_summary,
        "check_interval_sec": a.check_interval_sec,
        "condition_spec": _json(a.condition_spec) if a.condition_spec else {},
        # Migrate legacy reaction_type on read so every consumer (UI, executor,
        # chat) sees one of the canonical 4 — old rows storing draft_and_approve
        # surface as act (their requires_approval already carries the approval).
        "reaction_type": canonical_reaction(a.reaction_type),
        # Cost axis; default "agent" for rows written before the column existed.
        "execution_mode": (getattr(a, "execution_mode", None) or "agent"),
        "reaction_spec": _json(a.reaction_spec),
        "reaction_instructions": a.reaction_instructions,
        "requires_knowledge": getattr(a, "requires_knowledge", None),
        "requires_approval": bool(a.requires_approval),
        # v7.63.0 — default True even on rows written before the column existed
        # (getattr guard: a legacy AutomationState-less read never crashes).
        "check_before_approval": bool(getattr(a, "check_before_approval", True)),
        "autonomous_ok": bool(a.autonomous_ok),
        "cooldown_sec": a.cooldown_sec,
        "rearm_on_recovery": bool(a.rearm_on_recovery),
        "source": a.source,
        "updated_at": iso_utc(a.updated_at),
        "sentence": _sentence(a),
    }
    if st is not None:
        out["state"] = {
            "state": st.state,
            "last_checked_at": iso_utc(st.last_checked_at),
            "last_value": st.last_value,
            "last_value_json": _json(st.last_value_json) if st.last_value_json else None,
            "fire_count": st.fire_count,
            "last_fired_at": iso_utc(st.last_fired_at),
            "last_run_id": st.last_run_id,
            "last_error": st.last_error,
        }
    # v7.36.0 — the LAST FIRING's lifecycle beyond the arming state: whether it
    # is waiting on the operator, already executed, or was rejected. This is what
    # the operator wants to see per standing order ("where is it?"). Derived from
    # the automation's most recent Approval (requires_approval) or its run.
    # Values: pending_approval | executed | rejected | ran | None (never fired).
    out["last_fire_status"] = last_fire_status
    return out


def _sentence(a: Automation) -> str:
    """Plain-language 'When … → then …' for the UI / chat."""
    return f"Cuando {a.signal_summary or a.signal_type} → {a.reaction_type}"


# ── HTML description (v7.138.0) — mandatory, sanitized ───────────────
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_DANGER_RE = re.compile(
    r"(?is)<\s*(script|style|iframe|object|embed)\b.*?<\s*/\s*\1\s*>"   # bloques peligrosos
    r"|<\s*(?:script|style|iframe|object|embed)\b[^>]*/?>"              # their loose openings
    r"|\son\w+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)"                     # on*= handlers
    r"|javascript:")                                                     # URLs javascript:


def _sanitize_html(html: str) -> str:
    """Removes dangerous constructs (script/style/iframe/object/embed, on*=, javascript:) from
    the HTML description. Defence in depth: it is written by an admin or the agent, but it is
    RENDERED in the SPA (the SPA also sanitizes when painting)."""
    return _HTML_DANGER_RE.sub("", html or "")


def _html_text(html: str) -> str:
    """Plain text of an HTML — to check the description is NOT empty of real content (a
    «<p></p>» or only spaces does not count as a description)."""
    return _HTML_TAG_RE.sub("", html or "").replace("&nbsp;", " ").strip()


# ── CRUD ─────────────────────────────────────────────────────────────
# v7.151.0 — The PROVENANCE of an automation. It is fixed on creation and read-only from
# then on: neither the UI nor the AGENT (they share this service) can rewrite it. Without
# the seal, an `update` could change `template_key` and make a row say it came from a
# deployment template that is not its own.
SEALED_FIELDS = ("template_key", "template_version", "derived_from_id")

# v7.152.0 — An automation that came from the DEPLOYMENT (born from a template and not an
# evolution of anyone) is read-only in its CONTENT: what defines what it does is set by the
# descriptor, and changing it is creating an evolution, not rewriting the original.
# These are the OPERATION LEVERS, which stay free on purpose: without them you could not
# pause one that is bothering you without duplicating it first, which is a worse remedy
# than the illness. Everything else (title, description, signal, condition, reaction) is locked.
OPERATIONAL_FIELDS = frozenset({
    "enabled", "cooldown_sec", "priority", "serves_user_email",
})


def _sealed_value(key: str, raw: Any) -> Any:
    """Normalizes a provenance field the same way when creating as when comparing."""
    if raw in (None, ""):
        return None
    if key == "derived_from_id":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return str(raw).strip() or None


def _validate(body: dict, *, partial: bool) -> dict:
    out: dict[str, Any] = {}
    if "title" in body or not partial:
        t = str(body.get("title") or "").strip()
        if not t:
            raise ValueError("title is required")
        out["title"] = t[:200]
    if "signal_type" in body or not partial:
        s = str(body.get("signal_type") or "manual").strip().lower()
        if s not in SIGNAL_TYPES:
            raise ValueError(f"signal_type must be one of {SIGNAL_TYPES}")
        out["signal_type"] = s
    if "reaction_type" in body or not partial:
        raw_r = str(body.get("reaction_type") or "notify").strip().lower()
        r = canonical_reaction(raw_r)   # migrate legacy (draft_and_approve → act)
        if r not in REACTION_TYPES:
            raise ValueError(f"reaction_type must be one of {REACTION_TYPES}")
        out["reaction_type"] = r
        # draft_and_approve WAS act+approval: preserve the approval intent unless the
        # caller set requires_approval explicitly in the same body.
        if raw_r == "draft_and_approve" and "requires_approval" not in body:
            out["requires_approval"] = True
    if "execution_mode" in body or not partial:
        m = str(body.get("execution_mode") or "agent").strip().lower()
        if m not in EXECUTION_MODES:
            raise ValueError(f"execution_mode must be one of {EXECUTION_MODES}")
        out["execution_mode"] = m
    # v7.138.0 — MANDATORY functional description (HTML). On create ALWAYS; on update only if
    # it comes in the body, but if it comes it can NOT end up empty (deleting it is not
    # allowed). It must explain WHEN it fires (the signal), the "only if" (what it checks)
    # and WHAT it does (the reaction).
    if "description" in body or not partial:
        desc = _sanitize_html(str(body.get("description") or ""))
        if not _html_text(desc):
            raise ValueError(
                "La descripción es OBLIGATORIA: describe funcionalmente la automatización — "
                "CUÁNDO se dispara (la señal), el «solo si» (qué comprueba) y QUÉ hace (la reacción).")
        out["description"] = desc[:8000]
    for k in ("signal_spec", "reaction_spec", "condition_spec"):
        if k in body:
            out[k] = json.dumps(body.get(k) or {}, ensure_ascii=False)
    for k in ("signal_summary", "reaction_instructions", "area_tag",
              "serves_user_email", "requires_knowledge"):
        if k in body:
            out[k] = (str(body.get(k) or "").strip() or None)
    # v7.151.0 — ORIGIN SEAL: the provenance is fixed on CREATION and NEVER rewritten. On
    # `update` (partial=True) these fields do not even enter the values: the
    # `update_automation` guard compares against the row and rejects if they try to change them.
    if not partial:
        for k in SEALED_FIELDS:
            if k in body:
                out[k] = _sealed_value(k, body.get(k))
    for k in ("enabled", "requires_approval", "autonomous_ok", "rearm_on_recovery",
              "check_before_approval"):
        if k in body:
            out[k] = 1 if body.get(k) else 0 if k == "enabled" else bool(body.get(k))
    for k in ("priority", "check_interval_sec", "cooldown_sec"):
        if k in body:
            try:
                out[k] = int(body.get(k) or 0)
            except (TypeError, ValueError):
                out[k] = 0
    return out


async def list_automations() -> list[dict]:
    from app.app_models.business import Approval
    from app.app_models.runtime import AgentRun, Event
    from app.automation_provenance import automation_id_of, receipt_of_live_automation
    async with app_session() as s:
        rows = list((await s.execute(
            select(Automation).order_by(Automation.priority.desc(), Automation.id.asc())
        )).scalars().all())
        states = {
            st.automation_id: st for st in (await s.execute(select(AutomationState))).scalars().all()
        }
        # Most-recent Approval per automation (idempotency_key = autoappr:{aid}:{bucket})
        appr = (await s.execute(
            select(Approval.idempotency_key, Approval.status)
            .where(Approval.idempotency_key.like("autoappr:%"))
            .order_by(Approval.created_at.desc(), Approval.id.desc())
        )).all()
        # The LAST run of each automation — the operator wants to see whether the
        # standing order is executing RIGHT NOW vs already finished. We derive it
        # from the AUTHORITATIVE ``automation_fired`` event → ``agent_run`` link
        # (the same join the history uses), NOT ``AutomationState.last_run_id``:
        # that field is not always populated (manual "Ejecutar ahora", legacy
        # rows), which made the card wrongly show "Not run yet" and
        # fall back to "Disabled" even for a run in progress. Newest run per
        # automation = highest AgentRun.id (runs are created monotonically; a
        # RUNNING retry updates its row in place, so no duplicates).
        # Select only the light columns (never the large prompt/response_md) so
        # the list endpoint stays cheap even with a long run history.
        # v7.158.0 — ONLY the latest ones are requested, not all. Before, the WHOLE history
        # of automation runs was loaded to keep the most recent of each one in Python:
        # correct and cheap with 120 rows, unsustainable with a year's worth. Now the engine
        # returns one row per automation.
        _ultimas = (
            select(func.max(AgentRun.id))
            .join(Event, AgentRun.event_id == Event.id)
            .where(Event.type == "automation_fired", receipt_of_live_automation(Event.source))
            .group_by(Event.source)
        )
        run_pairs = (await s.execute(
            select(AgentRun.id, AgentRun.status, AgentRun.started_at,
                   AgentRun.finished_at, AgentRun.error, Event.source)
            .join(Event, AgentRun.event_id == Event.id)
            .where(AgentRun.id.in_(_ultimas))
            .order_by(AgentRun.id.desc())
        )).all()
        # v7.137.0 — the LAST eval per automation (from the ledger): it lets the row say
        # "the WHEN fired → did the ONLY IF pass?" EVEN IF the condition cut and there is no run.
        from app.app_models.automations import AutomationEvalLog as _EvalLog
        # Same case: one row per automation, not the whole ledger (it grows ~250 rows/day
        # with 8 automations being checked every 5 minutes).
        _ultimos_eval = select(func.max(_EvalLog.id)).group_by(_EvalLog.automation_id)
        eval_rows = (await s.execute(
            select(_EvalLog.automation_id, _EvalLog.at, _EvalLog.mode, _EvalLog.met,
                   _EvalLog.fired, _EvalLog.error)
            .where(_EvalLog.id.in_(_ultimos_eval))
            .order_by(_EvalLog.at.desc())
        )).all()
        last_eval_by_aid: dict[int, dict] = {}
        for _aid, _at, _mode, _met, _fired, _err in eval_rows:
            if _aid not in last_eval_by_aid:   # desc → first seen = most recent
                last_eval_by_aid[_aid] = {"at": iso_utc(_at), "mode": _mode,
                                          "met": bool(_met), "fired": bool(_fired),
                                          "error": _err or None}
        last_run_by_aid: dict[int, dict] = {}
        for rid, rstatus, started, finished, rerr, source in run_pairs:
            aid = automation_id_of(source)
            if aid is None:
                continue
            if aid not in last_run_by_aid:   # desc order → first seen is the newest
                last_run_by_aid[aid] = {
                    "id": rid, "status": rstatus,
                    "started_at": iso_utc(started), "finished_at": iso_utc(finished),
                    "error": (rerr or None),
                }
    latest_status: dict[int, str] = {}
    for idem, status in appr:
        try:
            aid = int(str(idem).split(":")[1])
        except (IndexError, ValueError):
            continue
        latest_status.setdefault(aid, str(status))  # desc order → first seen is newest

    def _fire_status(a: Automation, st: Optional[AutomationState]) -> Optional[str]:
        s_appr = latest_status.get(a.id)
        if s_appr == "pending":
            return "pending_approval"
        if s_appr in ("approved", "executed"):
            return "executed"
        if s_appr == "rejected":
            return "rejected"
        if st and (st.fire_count or 0) > 0:
            return "ran"          # fired & executed directly (no approval required)
        return None               # never fired yet

    def _last_run(aid: int) -> Optional[dict]:
        return last_run_by_aid.get(aid)

    def _runtime_state(a: Automation, st: Optional[AutomationState],
                       fire: Optional[str], last_run: Optional[dict]) -> str:
        """ONE current-state the operator can read at a glance, distinct from the
        design-time on/off (``enabled``) and the arming machine (``state``):
          * ``running``          — its last run is in flight right now;
          * ``pending_approval`` — a firing is parked waiting for the operator;
          * ``disabled``         — the standing order is switched off;
          * ``idle``             — armed and waiting for its next trigger.
        Precedence is runtime-first: a disabled order can still have a run
        finishing or an approval pending, and THAT is what the operator must see.
        """
        if last_run and str(last_run.get("status") or "").upper() == "RUNNING":
            return "running"
        if fire == "pending_approval":
            return "pending_approval"
        if not a.enabled:
            return "disabled"
        return "idle"

    out: list[dict] = []
    for a in rows:
        st = states.get(a.id)
        fire = _fire_status(a, st)
        d = row_to_dict(a, st, fire)
        lr = _last_run(a.id)
        d["last_run"] = lr
        d["last_eval"] = last_eval_by_aid.get(a.id)   # v7.137.0 — last check of the only-if
        d["runtime_state"] = _runtime_state(a, st, fire, lr)
        out.append(d)
    return out


async def get_automation(aid: int) -> Optional[dict]:
    async with app_session() as s:
        a = (await s.execute(select(Automation).where(Automation.id == aid))).scalar_one_or_none()
        if a is None:
            return None
        st = (await s.execute(
            select(AutomationState).where(AutomationState.automation_id == aid)
        )).scalar_one_or_none()
    return row_to_dict(a, st)


async def _reject_if_duplicate(values: dict) -> None:
    """v7.139.0 — avoids registering the SAME autonomous work TWICE (the user could add the
    SAME template several times). It rejects (422) if there already is: (a) an automation of
    the SAME template (``template_key``), or (b) an IDENTICAL one. v7.139.3: "identical"
    includes the SAME TITLE — two automations with a different NAME are NOT duplicates even if
    they share signal/reaction (a user may want "notify sales" and "notify support" with the
    same trigger). Before, the fingerprint ignored the title and blocked too much. It gives
    the existing one's title so it can be edited."""
    tkey = (values.get("template_key") or "").strip()
    # v7.151.0 — an EVOLUTION can share a template with its origin: it is intentional ("do not
    # touch the deployment's one, make one from it"), not a repeated registration by mistake,
    # which is what this guard exists to stop. The "identical" check (same
    # title+signal+reaction) still applies, so an unchanged copy is stopped.
    if values.get("derived_from_id"):
        tkey = ""
    new_title = (values.get("title") or "").strip().lower()

    def _norm(v):
        try:
            return json.loads(v) if isinstance(v, str) else (v or {})
        except (ValueError, TypeError):
            return {}
    new_ss, new_cs, new_rs = (_norm(values.get("signal_spec")), _norm(values.get("condition_spec")),
                              _norm(values.get("reaction_spec")))
    async with app_session() as s:
        rows = (await s.execute(select(Automation))).scalars().all()
    for a in rows:
        if tkey and (a.template_key or "").strip() == tkey:
            raise ValueError(
                f"Esta plantilla ya está añadida como «{a.title}». Edítala en vez de duplicarla "
                f"(o bórrala antes si quieres recrearla).")
        if ((a.title or "").strip().lower() == new_title and new_title
                and a.signal_type == values.get("signal_type") and a.reaction_type == values.get("reaction_type")
                and _norm(a.signal_spec) == new_ss and _norm(a.condition_spec) == new_cs
                and _norm(a.reaction_spec) == new_rs):
            raise ValueError(
                f"Ya existe un trabajo autónomo idéntico: «{a.title}» (mismo nombre, señal, «solo si» "
                f"y acción). Edítalo en vez de crear un duplicado.")


async def create_automation(body: dict, *, by: str = "", source: str = "form") -> dict:
    values = _validate(body, partial=False)
    # v7.136.0 — FAIL-LOUD: validates the paths of a watch/checks against the tool's REAL
    # answer BEFORE creating, so the automation is NOT born dead. It raises ValueError (→422)
    # with the available fields if items_at/key/signature/extract do not match.
    await validate_condition_config(body.get("condition_spec") or {})
    # v7.139.0 — do not register the same work twice (template already added / identical).
    await _reject_if_duplicate(values)
    # When the author didn't pin requires_approval, take the employee's autonomy
    # default (observe/propose/act-with-approval → requires approval; only the
    # fully-autonomous level lets a new rule act without it). ``enabled`` is never
    # derived from autonomy — every automation is still born disabled.
    if "requires_approval" not in values:
        try:
            from app.autonomy_state import autonomy_defaults, autonomy_level
            values["requires_approval"] = bool(
                autonomy_defaults(await autonomy_level())["automation_requires_approval"]
            )
        except Exception:  # pragma: no cover — model default (True) is the safe fallback
            pass
    now = datetime.now(timezone.utc)
    a = Automation(
        source=source, created_by=by or None, updated_by=by or None,
        created_at=now, updated_at=now, **values,
    )
    async with app_write() as s:
        s.add(a)
        await s.commit()
        await s.refresh(a)
        s.add(AutomationState(automation_id=a.id, state="armed"))
        await s.commit()
    return await get_automation(a.id)


async def _reject_if_reseals_origin(aid: int, body: dict) -> None:
    """The provenance is read-only: it is fixed on creation and stays there.

    It rejects fail-loud if the caller —UI or AGENT, which go through here alike— tries to
    change which deployment template or which automation the row was born from. If the SAME
    value arrives nothing happens (the UI resends the whole object when saving). The message
    says the right path, because whoever runs into it most is the agent.
    """
    presentes = {k: _sealed_value(k, body.get(k)) for k in SEALED_FIELDS if k in body}
    actual = await get_automation(aid)
    if not actual:
        return
    for k, nuevo in presentes.items():
        viejo = _sealed_value(k, actual.get(k))
        if nuevo != viejo:
            raise ValueError(
                f"'{k}' no se puede cambiar: es el ORIGEN de la automatización y queda "
                f"sellado al crearla (ahora vale {viejo!r}). Lo que vino del despliegue no "
                f"se modifica: si necesitas una variante, créala como EVOLUCIÓN de esta "
                f"(evolve_automation) y edita la copia."
            )
    # v7.152.0 — the CONTENT of a deployment automation is not touched either.
    if not is_from_deployment(actual):
        return
    tocados = sorted(
        k for k, v in (body or {}).items()
        if k not in OPERATIONAL_FIELDS and k not in SEALED_FIELDS
        and _sealed_value(k, v) != _sealed_value(k, actual.get(k))
    )
    if tocados:
        raise ValueError(
            f"Esta automatización viene del DESPLIEGUE (plantilla "
            f"'{actual.get('template_key')}') y su contenido es de solo lectura; intentaste "
            f"cambiar: {', '.join(tocados)}. Puedes activarla, pausarla, ajustar su pausa "
            f"entre avisos, su prioridad y a quién sirve. Para cambiar lo demás, créala como "
            f"EVOLUCIÓN (evolve_automation) y edita la copia: la original se queda intacta."
        )


def is_from_deployment(a: dict | None) -> bool:
    """Did this row come from the DEPLOYMENT and nobody has derived it yet?

    It is born from a descriptor template (`template_key`) and is NOT an evolution of another
    (`derived_from_id` empty). A derived one is the user's even if it keeps the lineage.
    """
    if not a:
        return False
    return bool((a.get("template_key") or "").strip()) and not a.get("derived_from_id")


async def evolve_automation(aid: int, changes: dict, *, by: str = "") -> Optional[dict]:
    """Creates a NEW automation from another one, applying `changes`.

    It is the allowed path to "change" something that came from the deployment: the original
    stays intact and the copy is born with `derived_from_id` pointing at it, keeping which
    template it came from (`template_key`/`template_version`) so as not to lose the lineage.
    It is born DISABLED on purpose: an evolution is reviewed by a person before it fires.
    """
    origen = await get_automation(aid)
    if not origen:
        return None
    base = {k: v for k, v in origen.items()
            if k not in ("id", "created_at", "updated_at", "created_by", "updated_by",
                         "last_fired_at", "state", "stats")}
    base.update(changes or {})
    if str(base.get("title") or "").strip() == str(origen.get("title") or "").strip():
        # Two rows with the same title are indistinguishable in the list (and the duplicates
        # guard would reject them): the evolution names itself if nobody renamed it.
        base["title"] = f"{origen.get('title')} (evolución)"
    base["derived_from_id"] = int(aid)
    base["template_key"] = origen.get("template_key")
    base["template_version"] = origen.get("template_version")
    base["enabled"] = False
    return await create_automation(base, by=by)


async def update_automation(aid: int, body: dict, *, by: str = "") -> Optional[dict]:
    await _reject_if_reseals_origin(aid, body)
    values = _validate(body, partial=True)
    if not values:
        return await get_automation(aid)
    if "condition_spec" in values:  # v7.136.0 — same fail-loud validation when editing
        await validate_condition_config(body.get("condition_spec") or {})
    values["updated_by"] = by or None
    values["updated_at"] = datetime.now(timezone.utc)
    async with app_write() as s:
        res = await s.execute(update(Automation).where(Automation.id == aid).values(**values))
        if res.rowcount == 0:
            return None
        await s.commit()
    return await get_automation(aid)


async def delete_automation(aid: int) -> bool:
    """Deletes an automation and UNLINKS its trail.

    SQLite reuses the id of a deleted row (``INTEGER PRIMARY KEY`` without
    ``AUTOINCREMENT``), and the provenance is resolved by the event's marker
    (``Event.source == "automation:{id}"``). Without unlinking, the NEXT automation that gets
    that id inherits the previous one's whole history: its card declares incidents, tasks
    and spend it never produced. It is not a lab oddity —deleting an automation and creating
    another is the most normal thing in the world— and the operator has no way of knowing
    the numbers belong to another.

    The receipts are NOT deleted (the spend happened and the history is auditable): they are
    relabelled to ``automation:{id}:deleted``, which can no longer collide with any live id
    (two deleted ones sharing a mark does not matter: nobody asks for the card of an
    automation that no longer exists; what matters is not contaminating the next one). The
    satellite tables do go, as they describe nothing without their row.
    """
    from sqlalchemy import text as _text
    async with app_write() as s:
        res = await s.execute(sql_delete(Automation).where(Automation.id == aid))
        if res.rowcount:
            marca = f"automation:{aid}:deleted"
            await s.execute(_text(
                "UPDATE events SET source = :marca "
                "WHERE type = 'automation_fired' AND source = :viejo"
            ), {"marca": marca, "viejo": f"automation:{aid}"})
            for tabla in ("automation_state", "automation_watch_items",
                          "automation_eval_log", "automation_daily_stats"):
                try:
                    await s.execute(_text(
                        f"DELETE FROM {tabla} WHERE automation_id = :aid"), {"aid": aid})
                except Exception:  # noqa: BLE001 — a table that does not exist does not prevent deleting
                    logger.debug("delete_automation: no se pudo limpiar %s", tabla, exc_info=True)
        await s.commit()
    return bool(res.rowcount)


async def fire_now(aid: int, *, by: str = "", check_condition: bool = False) -> Optional[dict]:
    """Manually run an automation NOW (operator Play / boss "run it"), even if
    it's a ``manual`` signal that never auto-fires and is left disabled.

    TWO MODES (v7.139.0):
      * ``check_condition=False`` (FORCE) — fires the reaction IGNORING the "only if" (the
        usual explicit forcing). Useful for "run it now whatever happens".
      * ``check_condition=True`` (RUN CHECKING) — evaluates the "only if" and ONLY fires if
        it is met; if not, it does NOT act and returns it (``fired=False, met=False``). It
        is a manual tick identical to the automatic one: it honours the condition (and then
        ``requires_approval``). The ``agent`` mode self-checks in the run (met=True here).

    Emits an ``automation_fired`` event the executor turns into a run. Bypasses the
    signal AND the cooldown, tags the context ``manual: True`` so the executor runs even
    when ``enabled == 0``. A unique per-click bucket makes each manual run fire. Returns
    ``{"ok": True, "fired": bool, "met": bool|None, ...}`` or ``None`` if it doesn't exist.
    """
    from app.autonomous.watcher import _fire_automation
    # The "only if" is evaluated OUTSIDE the write queue, and this was the most expensive
    # thing inside it: `evaluate_condition` calls the cluster's MCP —the sweep that takes 20
    # to 60 s, the same one the ingress has a 180 s ceiling for—. With the lock held during
    # that call, NO other writer of the process can advance: not the channel poller, not the
    # event claim, not an operator's HTTP request. It is the cause of the 14 s waits KOM-44
    # measured and that 7.170.2 did not manage to close by only moving the embeddings out.
    #
    # In exchange, reading-and-writing under one same lock is lost, so the row is RE-READ
    # inside the block: if someone deleted or changed it while we were querying the cluster,
    # we do not act on a stale snapshot.
    async with app_session() as s:
        a = (await s.execute(
            select(Automation).where(Automation.id == aid)
        )).scalar_one_or_none()
        if a is None:
            return None
        snapshot = row_to_dict(a)
        cond_mode = (_json(a.condition_spec).get("mode") if a.condition_spec else "always")
        titulo = a.title
    now = datetime.now(timezone.utc)
    met, cctx = None, None
    if check_condition:
        # Evaluates the "only if" (same evaluator as the watcher) and ACTS only if it is met.
        try:
            met, cctx = await evaluate_condition(snapshot)
        except Exception as exc:  # noqa: BLE001 — a check failure must not break the button
            met, cctx = False, {"error": str(exc)[:300]}

    async with app_write() as s:
        a = (await s.execute(
            select(Automation).where(Automation.id == aid)
        )).scalar_one_or_none()
        if a is None:
            # It was deleted while the cluster was being queried: there is nothing to fire.
            return None
        if check_condition:
            await record_eval(s, aid, mode=cond_mode, met=bool(met), fired=bool(met),
                              ctx={**(cctx or {}), "manual": True, "triggered_by": by})
            if not met:
                await s.commit()
                return {"ok": True, "fired": False, "met": False, "automation_id": aid,
                        "title": titulo, "detail": _eval_detail(cctx or {}),
                        "reason": (cctx or {}).get("error") or "el «solo si» no se cumple → no procedía actuar"}
        # Microsecond bucket → unique fingerprint per manual click (no dedup
        # against earlier runs; the operator may re-run on demand).
        bucket = f"manual-{now.timestamp():.6f}"
        await _fire_automation(
            s, a, {"manual": True, "triggered_by": by, "condition_checked": bool(check_condition)},
            now, bucket=bucket,
        )
        # Reflect the manual run on the automation's state for traceability.
        st = (await s.execute(
            select(AutomationState).where(AutomationState.automation_id == aid)
        )).scalar_one_or_none()
        if st is None:
            st = AutomationState(automation_id=aid, state="armed")
            s.add(st)
        st.fire_count = (st.fire_count or 0) + 1
        st.last_fired_at = now.replace(tzinfo=None)
        await s.commit()
    # `titulo` and not `a.title`: outside the `async with` the instance is DETACHED from its
    # session, and touching an attribute can raise DetachedInstanceError. It was captured
    # when reading it, which is moreover the title the operator saw when pressing.
    return {"ok": True, "fired": True, "met": (True if check_condition else None),
            "automation_id": aid, "title": titulo}


async def local_schema(max_tables: int = 80) -> dict[str, list[str]]:
    """Real column names of the employee's LOCAL store tables, introspected via
    PRAGMA so they always match the live schema. The catalog hands this to the
    chat agent / wizard so a ``data`` or ``sql`` condition references columns
    that ACTUALLY exist — the classic trap is the agent inventing
    ``channel_messages.created_at`` (the real timestamp column is
    ``received_at``). Returns ``{table: [columns]}``; never raises."""
    from sqlalchemy import text
    out: dict[str, list[str]] = {}
    try:
        async with app_session() as s:
            tables = [r[0] for r in (await s.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ))).all()]
            for t in tables[:max_tables]:
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", t):
                    continue  # never interpolate an unsafe identifier into PRAGMA
                cols = (await s.execute(text(f'PRAGMA table_info("{t}")'))).all()
                # PRAGMA table_info row = (cid, name, type, notnull, dflt, pk)
                out[t] = [c[1] for c in cols]
    except Exception as exc:  # pragma: no cover — catalog must never 500
        logger.warning("local_schema introspection failed: %s", exc)
    return out


async def automation_history(aid: int, limit: int = 50) -> list[dict]:
    """Full firing trail of one automation: every time it fired and what the
    employee DID about it. Returns newest first.

    Two kinds of firing are merged into ONE timeline (v7.38.0):
      * a NORMAL fire → an ``automation_fired`` event (source
        ``automation:{aid}``), joined to its ``agent_run`` (the forensic view:
        prompt, tools, outputs, cost) via ``event_id``;
      * an APPROVAL-GATED fire → a parked ``Approval`` (idempotency
        ``autoappr:{aid}:{bucket}``). Before it is approved there is NO event, so
        these firings were INVISIBLE in the history — the operator saw "requires
        approval" in the list but nothing here. Now each parked approval shows
        up with its status (pending/approved/rejected) and a link to the
        approval; once approved, the executor emits the event with the SAME
        ``bucket`` — so we attach the approval to that event and the run becomes
        the EXECUTION link.
    """
    from app.app_models.runtime import AgentRun, Event
    from app.app_models.business import Approval, ScheduledItem
    cap = max(1, min(int(limit or 50), 200))
    async with app_session() as s:
        events = list((await s.execute(
            select(Event).where(
                Event.type == "automation_fired",
                Event.source == f"automation:{aid}",
            ).order_by(Event.detected_at.desc()).limit(cap)
        )).scalars().all())
        ev_ids = [e.id for e in events]
        runs: dict[int, Any] = {}
        if ev_ids:
            for r in (await s.execute(
                select(AgentRun).where(AgentRun.event_id.in_(ev_ids))
            )).scalars().all():
                prev = runs.get(r.event_id)
                if prev is None or r.id > prev.id:
                    runs[r.event_id] = r
        approvals = list((await s.execute(
            select(Approval).where(Approval.idempotency_key.like(f"autoappr:{aid}:%"))
            .order_by(Approval.created_at.desc()).limit(cap)
        )).scalars().all())
        # The ENCARGO (ScheduledItem) each firing's run created, so the history
        # can deep-link straight to the delegated task the standing order spawned
        # (not only to the raw run). Keyed by the run that created it.
        run_ids = [r.id for r in runs.values()]
        item_by_run: dict[int, ScheduledItem] = {}
        if run_ids:
            for it in (await s.execute(
                select(ScheduledItem).where(ScheduledItem.created_by_run_id.in_(run_ids))
            )).scalars().all():
                prev = item_by_run.get(it.created_by_run_id)
                if prev is None or it.id < prev.id:  # the first item the run created
                    item_by_run[it.created_by_run_id] = it

    by_bucket: dict[str, dict] = {}
    out: list[dict] = []
    for e in events:
        r = runs.get(e.id)
        # A short, business-readable outcome: the error if it failed, else the
        # head of the agent's own narration. The full detail is one click away.
        summary = (r.error or r.response_md or "").strip()[:280] if r is not None else ""
        item = item_by_run.get(r.id) if r is not None else None
        entry = {
            "id": f"evt{e.id}",
            "event_id": e.id,
            "fired_at": iso_utc(e.detected_at),
            "event_status": e.status,
            "run_id": r.id if r is not None else None,
            "run_status": r.status if r is not None else None,
            "scheduled_item_id": item.id if item is not None else None,
            "scheduled_item_title": (item.title if item is not None else None),
            "summary": summary,
            "cost_usd": r.cost_usd if r is not None else None,
            "duration_sec": r.duration_sec if r is not None else None,
            "requires_approval": False,
            "approval_id": None,
            "approval_status": None,
        }
        bucket = _json(e.payload_json).get("bucket")
        if bucket:
            by_bucket[str(bucket)] = entry
        out.append(entry)

    # v7.63.0 — READ-ONLY pre-check firings (check-before-approve). Each
    # ``automation_check`` event is the employee VERIFYING the condition before an
    # approval is (or isn't) parked. Surface it as its own timeline entry with the
    # verdict, so the operator sees "it checked and it {did|did not} apply". A holds-check
    # then shows as a SEPARATE approval entry (same bucket); a not-holds check
    # shows only here (+ a decision).
    async with app_session() as s2:
        check_events = list((await s2.execute(
            select(Event).where(
                Event.type == "automation_check",
                Event.source == f"automation:{aid}",
            ).order_by(Event.detected_at.desc()).limit(cap)
        )).scalars().all())
        chk_ev_ids = [e.id for e in check_events]
        chk_runs: dict[int, Any] = {}
        if chk_ev_ids:
            for r in (await s2.execute(
                select(AgentRun).where(AgentRun.event_id.in_(chk_ev_ids))
            )).scalars().all():
                prev = chk_runs.get(r.event_id)
                if prev is None or r.id > prev.id:
                    chk_runs[r.event_id] = r
        for e in check_events:
            r = chk_runs.get(e.id)
            holds: Optional[bool] = None
            evidence = ""
            if r is not None and r.structured_json:
                try:
                    verdict = _parse_check_verdict(json.loads(r.structured_json))
                    holds, evidence = verdict["holds"], verdict["evidence"]
                except Exception:
                    pass
            is_test = bool((_json(e.payload_json).get("context") or {}).get("test_only"))
            running = r is None or (r.status or "").upper() == "RUNNING"
            verdict_txt = ("comprobación en curso" if running
                           else "se cumple" if holds is True
                           else "no se cumple" if holds is False
                           else "sin veredicto claro")
            head = "Prueba de comprobación" if is_test else "Comprobación previa a la aprobación"
            summary = f"{head}: {verdict_txt}." + (f" {evidence}" if evidence else "")
            out.append({
                "id": f"chk{e.id}",
                "event_id": e.id,
                "fired_at": iso_utc(e.detected_at),
                "event_status": e.status,
                "run_id": r.id if r is not None else None,
                "run_status": r.status if r is not None else None,
                "scheduled_item_id": None,
                "scheduled_item_title": None,
                "summary": summary[:280],
                "cost_usd": r.cost_usd if r is not None else None,
                "duration_sec": r.duration_sec if r is not None else None,
                "requires_approval": False,
                "approval_id": None,
                "approval_status": None,
                "kind": "check",
            })

    for ap in approvals:
        bucket = _json(ap.action_payload_json).get("bucket")
        info = {
            "requires_approval": True,
            "approval_id": ap.id,
            "approval_status": (ap.status or "pending").lower(),
            "approval_decided_at": iso_utc(ap.decided_at),
        }
        entry = by_bucket.get(str(bucket)) if bucket else None
        if entry is not None:
            # The event for this bucket was emitted ON approval → its run is the
            # EXECUTION of the approved action.
            entry.update(info)
        else:
            # Parked approval with no event yet (pending / rejected) — surface it.
            out.append({
                "id": f"appr{ap.id}",
                "event_id": None,
                "fired_at": iso_utc(ap.created_at),
                "event_status": None,
                "run_id": None,
                "run_status": None,
                "scheduled_item_id": None,
                "scheduled_item_title": None,
                "summary": "",
                "cost_usd": None,
                "duration_sec": None,
                **info,
            })

    out.sort(key=lambda x: x.get("fired_at") or "", reverse=True)
    return out[:cap]


# ── Signal evaluation (deterministic) ────────────────────────────────
async def evaluate_data_condition(spec: dict) -> tuple[Optional[float], dict]:
    """Run the structured data condition and return (value, context). Reuses
    the watcher's safe query path (read-only project DB, identifier
    validation). Returns (None, {error}) on any failure — never raises."""
    from app.autonomous.watcher import _safe_ident, _AGG_SQL, _query_scalar
    table = spec.get("table")
    column = spec.get("column")
    agg = (spec.get("aggregate") or "count").lower()
    source = (spec.get("source") or "project")
    if not _safe_ident(table):
        return None, {"error": f"unsafe table {table!r}"}
    agg_sql = _AGG_SQL.get(agg)
    if agg_sql is None:
        return None, {"error": f"unsupported aggregate {agg!r}"}
    # Only COUNT works without a column. SUM/AVG/MIN/MAX over "*" is invalid SQL,
    # which would be swallowed and make the automation silently never fire — so
    # require a safe column for those (mirrors the watcher's _agg_select).
    has_col = bool(column and _safe_ident(column))
    if agg != "count" and not has_col:
        return None, {"error": f"aggregate {agg!r} requires a valid column"}
    col_expr = f'"{column}"' if has_col else "*"
    sql = f'SELECT {agg_sql}({col_expr}) FROM {table}'
    # Optional time window: only rows in the last N days (safe, structured).
    window_days = spec.get("window_days")
    date_col = spec.get("date_column")
    if window_days and date_col and _safe_ident(date_col):
        try:
            n = int(window_days)
            if source == "project":
                sql += f" WHERE \"{date_col}\" >= NOW() - INTERVAL '{n} days'"
            else:
                sql += f" WHERE \"{date_col}\" >= datetime('now','-{n} days')"
        except (TypeError, ValueError):
            pass
    try:
        async with app_session() as s:
            raw = await _query_scalar(s, {"source": source}, sql)
        val = float(raw) if raw is not None else None
        return val, {"sql": sql, "value": val}
    except Exception as exc:  # pragma: no cover — never break the loop
        logger.warning("automation data_condition query failed: %s", exc)
        return None, {"error": str(exc)[:200], "sql": sql}


def condition_met(value: Optional[float], spec: dict) -> bool:
    """Apply the comparator to the current value."""
    op = (spec.get("operator") or "gt").lower()
    # A None value means the measurement FAILED (query error / unsupported agg),
    # not "zero". Never fire on a failed measurement — otherwise a transient DB
    # error would spuriously trigger a `no_activity` rule's reaction.
    if value is None:
        return False
    if op == "no_activity":
        return value <= 0
    try:
        threshold = float(spec.get("threshold") or 0)
    except (TypeError, ValueError):
        return False
    if op == "gt":  return value > threshold
    if op == "lt":  return value < threshold
    if op == "gte": return value >= threshold
    if op == "lte": return value <= threshold
    if op == "eq":  return value == threshold
    return False


# ── MCP-tool checks — the deterministic CHEAP pre-gate (0 tokens) ─────
# A "check" calls ONE read-only MCP tool of the employee (via the app PEP, so the
# write-policy stays in the loop) with FIXED arguments, extracts a value from the
# structured result and compares it — all WITHOUT an LLM. A checklist combines
# several with ALL/ANY. This is what lets an autonomous rule POLL cheaply
# ("are there errors in the logs?") and spend a run ONLY when a signal actually holds.
# Correlating one call's result into another is deliberately NOT supported here —
# that is the LLM's job once this cheap gate decides it is worth waking it.
class McpCheckError(RuntimeError):
    """A check could not be evaluated (MCP unreachable / tool error). The caller
    applies ``on_error`` (alert|skip|fire) — never fires the agent on a guess."""


_TEMPLATE_RE = re.compile(r"\{\{\s*(tenant|now(?:[+-]\d+[smhd])?)\s*\}\}", re.IGNORECASE)
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _now_ctx() -> dict:
    return {"tenant": (os.environ.get("TENANT_NAME") or os.environ.get("WAI_TENANT") or ""),
            "now_ts": datetime.now(timezone.utc).timestamp()}


def _render_templates(value: Any, ctx: dict) -> Any:
    """Substitute {{tenant}} and {{now}} / {{now-15m}} in string args (recurses
    dict/list). Unknown tokens are left as-is. Args are FIXED by the author, never
    LLM-built — so there is no injection surface."""
    if isinstance(value, str):
        def _sub(m: "re.Match") -> str:
            tok = m.group(1).lower()
            if tok == "tenant":
                return str(ctx.get("tenant") or "")
            mm = re.match(r"now(?:([+-]\d+)([smhd]))?$", tok)
            base = float(ctx.get("now_ts") or 0)
            if mm and mm.group(1):
                base += int(mm.group(1)) * _UNIT_S[mm.group(2)]
            return str(int(base))
        return _TEMPLATE_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _render_templates(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_templates(v, ctx) for v in value]
    return value


def _tool_text(result: dict) -> str:
    return "\n".join(c.get("text", "") for c in (result.get("content") or [])
                     if isinstance(c, dict) and c.get("text"))


def _extract_value(result: Any, path: Optional[str]) -> Any:
    """Pull the comparable value out of a tool result. Empty path is smart: a list
    → its length; a dict with 'count' → that; else the result itself. 'len' /
    'length(@)' → length. A dotted path walks dicts/list indices."""
    p = (path or "").strip()
    if not p or p == "@":
        if isinstance(result, list):
            return len(result)
        if isinstance(result, dict) and "count" in result:
            return result["count"]
        return result
    if p in ("len", "length", "length(@)", "count(@)"):
        try:
            return len(result)
        except TypeError:
            return None
    cur = result
    for part in p.split("."):
        part = part.strip()
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def check_met(value: Any, spec: dict) -> bool:
    """Predicate of ONE check: existence/text ops, or the numeric comparators
    (reusing ``condition_met``). ``op`` field (falls back to ``operator``)."""
    op = (spec.get("op") or spec.get("operator") or "non_empty").lower()
    if op in ("non_empty", "empty"):
        empty = (value is None or value is False or value == 0
                 or (hasattr(value, "__len__") and len(value) == 0))
        return (not empty) if op == "non_empty" else empty
    if op in ("contains", "not_contains"):
        needle = str(spec.get("value", spec.get("threshold", "")) or "")
        hay = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        has = bool(needle) and needle in hay
        return has if op == "contains" else (not has)
    # numeric: coerce (a list/dict compares by length)
    try:
        num = float(value if not isinstance(value, bool) else int(value))
    except (TypeError, ValueError):
        try:
            num = float(len(value))  # type: ignore[arg-type]
        except TypeError:
            return False
    return condition_met(num, {"operator": op, "threshold": spec.get("threshold")})


def _summarize(value: Any) -> Any:
    if isinstance(value, (int, float, bool, str)) or value is None:
        return value
    if isinstance(value, list):
        return {"len": len(value)}
    if isinstance(value, dict):
        # v7.148.0 — prioritizes the coverage/identity fields of the cluster-mcp sweep so the
        # agent's plan sees scan_incomplete/skipped_slow even if the dict has many keys.
        prefer = ("tenants_scanned", "tenants_checked", "scan_incomplete", "skipped_slow",
                  "logs_skipped_slow", "healthy", "coverage_note", "tenants_with_errors",
                  "tenants_with_problems", "tenants_with_candidate_errors", "candidate_count",
                  "stuck_count", "configured")
        out = {k: value[k] for k in prefer if k in value}
        for k in value:
            if k not in out and len(out) >= 10:
                break
            if k not in out:
                v = value[k]
                out[k] = v if not isinstance(v, (list, dict)) else _summarize(v)
        return out
    return str(value)[:200]


async def _mcp_tool_call(connector: str, tool: str, arguments: dict) -> Any:
    """ONE deterministic tools/call to the employee's MCP THROUGH the PEP (so the
    write-policy applies). Returns the parsed tool result (structuredContent
    preferred). Raises McpCheckError on transport/tool failure. No LLM."""
    import httpx
    from app.routes.mcp_proxy_routes import upstream_for, _parse_sse_payloads
    try:
        upstream = await upstream_for(connector)
    except Exception as exc:  # HTTPException (503) or resolution failure
        raise McpCheckError(f"connector {connector!r} not resolved: {exc}")
    headers = {"content-type": "application/json",
               "accept": "application/json, text/event-stream", **upstream["headers"]}
    call_body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": tool, "arguments": arguments or {}}}
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(_MCP_TOOL_CALL_TIMEOUT_S, connect=10.0)) as client:
            r1 = await client.post(upstream["url"], headers=headers, json={
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "wai-automation-gate", "version": "1.0"}}})
            sid = r1.headers.get("mcp-session-id")
            if sid:
                headers["mcp-session-id"] = sid
            await client.post(upstream["url"], headers=headers,
                              json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            r2 = await client.post(upstream["url"], headers=headers, json=call_body)
            raw = r2.content
            ctype = r2.headers.get("content-type", "")
    except Exception as exc:
        raise McpCheckError(f"MCP {connector!r} unreachable: {exc}")
    result = None
    if "text/event-stream" in ctype:
        for p in _parse_sse_payloads(raw):
            if isinstance(p, dict) and p.get("result") is not None:
                result = p["result"]; break
            if isinstance(p, dict) and p.get("error") is not None:
                raise McpCheckError(str(p["error"])[:200])
    else:
        try:
            parsed = json.loads(raw or b"{}")
        except Exception:
            raise McpCheckError(f"unparseable MCP response (HTTP {r2.status_code})")
        if parsed.get("error"):
            raise McpCheckError(str(parsed["error"])[:200])
        result = parsed.get("result")
    if not isinstance(result, dict):
        raise McpCheckError("MCP tool returned no result")
    if result.get("isError"):
        raise McpCheckError(_tool_text(result) or "tool error")
    sc = result.get("structuredContent")
    if sc is not None:
        # FastMCP wraps a non-dict return as {"result": ...} — unwrap that shape.
        if isinstance(sc, dict) and set(sc) == {"result"}:
            return sc["result"]
        return sc
    txt = _tool_text(result)
    try:
        return json.loads(txt)
    except (ValueError, TypeError):
        return {"text": txt}


def _op_symbol(op: str) -> str:
    return {"gt": ">", "lt": "<", "gte": "≥", "lte": "≤", "eq": "=",
            "no_activity": "sin actividad durante ≥", "non_empty": "tiene valor",
            "empty": "está vacío", "contains": "contiene", "not_contains": "no contiene"}.get(op, op)


def _explain_check(value: Any, op: str, threshold: Any, met: bool) -> str:
    """Plain SENTENCE of a check: "value OP threshold → yes/no it is met". So the operator
    understands WHY, not just a boolean."""
    v = _summarize(value)
    verdict = "SÍ se cumple" if met else "NO se cumple"
    sym = _op_symbol(op)
    if op in ("non_empty", "empty"):
        return f"«{v}» {sym} → {verdict}"
    return f"«{v}» {sym} «{threshold}» → {verdict}"


async def evaluate_mcp_check(check: dict, ctx: Optional[dict] = None,
                             *, verbose: bool = False) -> tuple[bool, dict]:
    """Evaluate ONE check → (met, detail). Raises McpCheckError on failure so the caller can apply
    ``on_error``. The detail ALWAYS carries the path (extract), the comparator (op), the
    threshold, the extracted value, the verdict and a SENTENCE (`explain`). ``verbose`` (for
    the UI's "Test") adds the ARGS sent and the SHAPE + SAMPLE of the result — so the operator
    sees the real call and why."""
    ctx = ctx or _now_ctx()
    connector = (check.get("connector") or "").strip()
    tool = (check.get("tool") or "").strip()
    if not connector or not tool:
        raise McpCheckError("check needs 'connector' and 'tool'")
    args = _render_templates(check.get("arguments") or {}, ctx)
    result = await _mcp_tool_call(connector, tool, args)
    extract = (check.get("extract") or "").strip()
    value = _extract_value(result, extract)
    met = check_met(value, check)
    op = (check.get("op") or check.get("operator") or "non_empty").lower()
    threshold = check.get("threshold") if check.get("threshold") not in (None, "") else check.get("value")
    detail = {"name": check.get("name"), "connector": connector, "tool": tool,
              "extract": extract or "(auto)", "op": op, "threshold": threshold,
              "value": _summarize(value), "met": met,
              "explain": _explain_check(value, op, threshold, met)}
    if verbose:
        detail["arguments"] = args
        detail["result_shape"] = _describe_shape(result)
        detail["result_sample"] = _summarize(result)
    return met, detail


async def evaluate_checks(cond: dict, ctx: Optional[dict] = None,
                          *, verbose: bool = False) -> tuple[bool, dict]:
    """Evaluate a CHECKLIST (mode 'checks' or a 'pre' guard): a flat list of checks
    combined ALL/ANY with short-circuit. ``on_error`` (alert|skip|fire, default
    alert) decides what a failed check means — alert/skip → NOT met (don't spend a
    run); fire → met. The error is always recorded so the watcher can surface it.
    ``verbose`` (for "Test") asks each check for its real call + sample."""
    ctx = ctx or _now_ctx()
    checks = cond.get("checks") or []
    combine = (cond.get("combine") or "all").lower()
    on_error = (cond.get("on_error") or "alert").lower()
    details: list[dict] = []
    errored: Optional[str] = None
    outcomes: list[bool] = []
    evaluated = 0
    for chk in checks:
        try:
            met, detail = await evaluate_mcp_check(chk, ctx, verbose=verbose)
        except McpCheckError as exc:
            errored = str(exc)
            met = True if on_error == "fire" else False
            detail = {"name": chk.get("name"), "connector": chk.get("connector"),
                      "tool": chk.get("tool"),
                      "op": (chk.get("op") or chk.get("operator") or "non_empty").lower(),
                      "threshold": chk.get("threshold") if chk.get("threshold") not in (None, "")
                                   else chk.get("value"),
                      "error": errored, "met": met,
                      "explain": f"No se pudo comprobar ({errored}) → on_error={on_error} ⇒ "
                                 f"{'cuenta como cumplida' if met else 'cuenta como NO cumplida'}"}
        details.append(detail)
        outcomes.append(met)
        evaluated += 1
        # Short-circuit: ALL stops at the first false, ANY at the first true.
        if combine == "all" and not met:
            break
        if combine == "any" and met:
            break
    met = (any(outcomes) if combine == "any" else all(outcomes)) if outcomes else False
    # Sentence of the TOTAL: how the any/all leads to the verdict (and whether there was a short-circuit).
    combine_txt = "CUALQUIERA (any)" if combine == "any" else "TODAS (all)"
    verdict = "SE CUMPLE" if met else "NO se cumple"
    tail = "" if evaluated >= len(checks) else f" (se cortó en la {evaluated}ª: el resto no se evaluó)"
    explain = (f"Combinación: {combine_txt} de {len(checks)} comprobación(es) ⇒ {verdict}{tail}."
               if checks else "Sin comprobaciones.")
    return met, {"checks": details, "combine": combine, "on_error": on_error,
                 "error": errored, "met": met, "evaluated": evaluated,
                 "total_checks": len(checks), "explain": explain}


async def _evaluate_guard(guard: dict) -> tuple[bool, dict]:
    """A cheap deterministic guard (the ``pre`` of any condition): data | sql |
    checks. Never an LLM."""
    gmode = (guard.get("mode") or "checks").lower()
    if gmode == "data":
        value, ctx = await evaluate_data_condition(guard)
        return condition_met(value, guard), ctx
    if gmode == "sql":
        val, ctx = await _eval_sql_bool(guard.get("sql") or "", guard.get("source") or "local")
        return bool(val), ctx
    return await evaluate_checks(guard)


# ── Agent CONDITION (the SECOND gate) — always | data | sql | checks ──
# Read-only safety for the advanced SQL condition: a single SELECT/WITH, no
# second statement, no mutating/DDL/pragma keywords. The agent or the operator
# can author it, but it can never write.
_SQL_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|attach|detach|"
    r"pragma|grant|revoke|replace|vacuum|reindex|merge)\b",
    re.IGNORECASE,
)


async def _eval_sql_bool(sql: str, source: str) -> tuple[Optional[bool], dict]:
    """Run an operator/agent-authored SELECT that returns true/false (any
    truthy scalar = condition holds). SELECT-only, single statement, read-only.
    ``source`` = ``local`` (employee store) | ``project`` (project DB)."""
    from app.autonomous.watcher import _query_scalar
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        return None, {"error": "empty sql"}
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return None, {"error": "only SELECT is allowed"}
    if ";" in s:
        return None, {"error": "single statement only"}
    if _SQL_FORBIDDEN.search(s):
        return None, {"error": "only read-only SELECT is allowed"}
    try:
        async with app_session() as sess:
            raw = await _query_scalar(sess, {"source": source}, s)
        return (raw is not None and bool(raw)), {"sql": s, "value": raw}
    except Exception as exc:  # pragma: no cover — never break the loop
        logger.warning("automation sql condition failed: %s", exc)
        return None, {"error": str(exc)[:200], "sql": s}


async def evaluate_condition(automation: dict) -> tuple[bool, dict]:
    """The agent's check, evaluated AFTER the signal fires. Returns (met, ctx).

    * ``always`` (or no condition) → always met.
    * ``data``  → the structured aggregate condition (entity/agg/column/op/
      threshold) over the chosen source.
    * ``sql``   → an operator/agent SELECT returning true/false.
    * ``agent`` → a NATURAL-LANGUAGE check the EMPLOYEE performs with its own
      tools/MCPs at reaction time (e.g. "check in the CRM whether there are
      opportunities without follow-up"). The watcher cannot evaluate prose, so
      this always returns ``True`` here (the signal fires the run) and the
      check is injected as the FIRST step of the reaction prompt by
      ``build_reaction_prompt`` — the employee then self-gates. Costlier than
      data/sql: it launches a run on every trigger to do the check.

    Back-compat: a legacy ``signal_type='data_condition'`` automation with no
    ``condition_spec`` uses its ``signal_spec`` as a ``data`` condition."""
    cond = automation.get("condition_spec") or {}
    mode = (cond.get("mode") or "").lower()
    # CHEAP DETERMINISTIC PRE-GATE (0 tokens), upstream of ALL cost: any mode
    # may carry a ``pre`` guard (data|sql|checks) that must hold FIRST. If it does
    # not, we return False here → no event, no pre-check, no run. This is what makes
    # "poll cheaply, spend a run only on a real signal" expressible even on an
    # ``agent`` (LLM) condition. Absent on always/legacy rows → no-op.
    pre = cond.get("pre")
    if isinstance(pre, dict) and pre:
        pre_met, pre_ctx = await _evaluate_guard(pre)
        if not pre_met:
            return False, {"gated_by": "pre", "pre": pre_ctx}
    if not mode:
        if automation.get("signal_type") == "data_condition":
            spec = automation.get("signal_spec") or {}
            value, ctx = await evaluate_data_condition(spec)
            return condition_met(value, spec), ctx
        return True, {}
    if mode == "always":
        return True, {}
    if mode == "data":
        value, ctx = await evaluate_data_condition(cond)
        return condition_met(value, cond), ctx
    if mode == "sql":
        val, ctx = await _eval_sql_bool(cond.get("sql") or "", cond.get("source") or "local")
        return bool(val), ctx
    if mode == "checks":
        # The deterministic MCP checklist IS the gate — 0 tokens, fires the run only
        # when the signals hold. ``then_agent`` (if present) rides in the reaction
        # prompt as an extra confirmation, but the GATE here spends nothing.
        return await evaluate_checks(cond)
    if mode == "agent":
        # The employee checks it itself; the watcher just lets it fire and
        # build_reaction_prompt turns the prose into a pre-action gate.
        return True, {"agent_check": (cond.get("instructions") or "").strip()}
    if mode == "watch":
        # The watch IS the gate: the watcher calls resolve_watch (with a session), which brings
        # the items, dedups per item and fires ONLY on what is new/escalated. Here we just let it pass.
        return True, {"mode": "watch"}
    return True, {}


# ── WATCH — PER-ITEM lifecycle (generic dedup) ────────────────────────
# It turns "the condition matches a SET" into "fire ONCE per NEW or escalated item,
# auto-resolve when it disappears". Domain-agnostic: the cluster Operator, a leads chaser
# or a tickets watcher use EXACTLY this, only the `watch` of their condition_spec changes:
#   condition_spec = {"mode":"watch", "watch": {"connector","tool","arguments","items_at","key",
#                     "signature"(opt, escalation), "resolve_when_absent"(def True), "on_error"(def alert)}}

from app.watch_lifecycle import diff_watch_items, key_ignored  # noqa: E402 — PURE logic (separate test)


def has_watch(condition_spec: dict) -> bool:
    return isinstance((condition_spec or {}).get("watch"), dict) and bool(condition_spec["watch"])


def _silenced_until(ts) -> bool:
    """In a silence window (maintenance/deploy)? ISO8601; naive = UTC. During the silence the
    lifecycle keeps being updated but it does NOT fire (avoids false alarms from our deploys)."""
    if not ts:
        return False
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < t
    except Exception:  # noqa: BLE001 — a malformed silence_until never silences
        return False


async def resolve_watch(session, automation, ctx: dict) -> tuple[bool, dict]:
    """Applies the per-item dedup if the automation has `watch`; if not, NO-OP → (True, ctx)
    (identical behaviour for existing employees). It makes ONE MCP call (0 LLM tokens) to the
    watch's tool, extracts the list of items, compares it with automation_watch_items and
    returns (is there anything NEW or escalated?, ctx + fresh_items). It persists the
    lifecycle (seen/new/resolved). on_error: a failing check is NEVER 'healthy' → alert/fire
    fire a notice (anti-FALSE-NEGATIVES)."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.app_models.automations import AutomationWatchItem

    cond = _json(automation.condition_spec)
    watch = cond.get("watch") if isinstance(cond, dict) else None
    if not isinstance(watch, dict) or not watch:
        return True, ctx  # no-op: without `watch`, it fires as always

    key_field = watch.get("key") or "key"
    sig_field = watch.get("signature")
    on_error = (watch.get("on_error") or "alert").lower()
    resolve_absent = watch.get("resolve_when_absent", True)
    ignore = list(watch.get("ignore_keys") or [])            # allowlist of known noise
    silenced = _silenced_until(watch.get("silence_until"))    # maintenance/deploy window

    config_warn = None
    try:
        result = await _mcp_tool_call(watch.get("connector") or "", watch.get("tool") or "",
                                      _render_templates(watch.get("arguments") or {}, _now_ctx()))
        raw = _extract_value(result, watch.get("items_at"))
        # v7.136.0 — fail-loud on DRIFT: if the tool changed and items_at stops returning a
        # list, it is recorded as an error in the ledger (visible in /evals) instead of failing
        # silently. (The "born broken" case is already blocked by validate_condition_config on create.)
        if raw is not None and not isinstance(raw, list):
            config_warn = (f"«Ruta a la lista» = «{watch.get('items_at') or '(vacío)'}» ya no "
                           f"devuelve una lista — la respuesta tiene: {_describe_shape(result)}")
        items = raw if isinstance(raw, list) else ([] if raw is None else [raw])
        if ignore:
            items = [it for it in items if not key_ignored(
                (str(it.get(key_field)) if isinstance(it, dict) and it.get(key_field) is not None
                 else None), ignore)]
    except McpCheckError as exc:
        # anti-FN: not being able to observe is NOT 'healthy'. alert/fire → fires (warns); skip → no.
        fire = on_error in ("alert", "fire")
        return fire, {**ctx, "watch_error": str(exc), "watch": {"error": str(exc)}}

    rows = (await session.execute(select(AutomationWatchItem).where(
        AutomationWatchItem.automation_id == automation.id))).scalars().all()
    by_key = {r.item_key: r for r in rows}
    stored = {r.item_key: {"signature": r.signature, "state": r.state} for r in rows}
    d = diff_watch_items(stored, items, key_field, sig_field)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for k, meta in d["seen"].items():
        r = by_key.get(k)
        if r is None:
            session.add(AutomationWatchItem(
                automation_id=automation.id, item_key=k, signature=meta["signature"],
                state="open", first_seen_at=now, last_seen_at=now, fired_at=now,
                sample=json.dumps(meta["item"], default=str)[:500]))
        else:
            r.last_seen_at = now
            r.times_seen = (r.times_seen or 0) + 1
            if r.state == "resolved":
                r.state, r.fired_at = "recurring", now          # reappeared → re-fires
            elif sig_field and r.signature != meta["signature"]:
                r.signature, r.fired_at = meta["signature"], now  # escalated → re-fires
    if resolve_absent:
        for k in d["resolved"]:
            r = by_key.get(k)
            if r is not None:
                r.state, r.resolved_at = "resolved", now

    fresh = d["fresh"] + d["escalated"]
    should_fire = bool(fresh) and not silenced  # in silence the lifecycle is followed but it does NOT fire
    return should_fire, {**ctx, "fresh_items": fresh[:50], "resolved_items": d["resolved"][:50],
                         "error": config_warn,
                         "watch": {"fresh": len(d["fresh"]), "escalated": len(d["escalated"]),
                                   "ongoing": len(d["ongoing"]), "resolved": len(d["resolved"]),
                                   "silenced": silenced, "shape": _describe_shape(result)}}


async def list_watch_items(automation_id: int, state: str | None = None,
                           limit: int = 200) -> dict:
    """v7.140.0 — an automation's anti-repetition MEMORY, visible.

    This table is the reason the employee "does not alert about the same thing again". While
    it could not be seen, the operator could not tell "nothing is happening" from "it is
    happening but it is remembered as seen" — two opposite situations with the same silence.
    It also returns the count per state to frame it (open / recurring / resolved) without
    dumping thousands of rows.
    """
    from sqlalchemy import func
    from app.app_models.automations import AutomationWatchItem
    async with app_session() as s:
        counts = {st: int(n) for st, n in (await s.execute(
            select(AutomationWatchItem.state, func.count())
            .where(AutomationWatchItem.automation_id == automation_id)
            .group_by(AutomationWatchItem.state)
        )).all()}
        q = select(AutomationWatchItem).where(
            AutomationWatchItem.automation_id == automation_id)
        if (state or "").strip():
            q = q.where(AutomationWatchItem.state == state.strip())
        rows = (await s.execute(
            q.order_by(AutomationWatchItem.last_seen_at.desc())
            .limit(min(int(limit or 200), 1000))
        )).scalars().all()
    return {
        "items": [{
            "item_key": r.item_key,
            "signature": r.signature,
            "state": r.state,
            "times_seen": r.times_seen or 0,
            "first_seen_at": iso_utc(r.first_seen_at),
            "last_seen_at": iso_utc(r.last_seen_at),
            "fired_at": iso_utc(r.fired_at),
            "resolved_at": iso_utc(r.resolved_at),
            "sample": _json(r.sample) if r.sample else None,
        } for r in rows],
        "counts": counts,
        "total": sum(counts.values()),
    }


async def forget_watch_items(automation_id: int, item_key: str | None = None,
                             state: str | None = None,
                             by: str = "operator") -> dict:
    """Deletes anti-repetition memory (one item, one state or all). Returns how many rows it
    forgot.

    It is intentionally a DELETE and not a flag: the table's semantics is "what I already
    saw", so forgetting = not being there. The consequence (it may alert again on the next
    check) is exactly what is asked for when resetting, and it stays in the log so the later
    silence is not a mystery.
    """
    from sqlalchemy import delete
    from app.app_models.automations import AutomationWatchItem
    q = delete(AutomationWatchItem).where(
        AutomationWatchItem.automation_id == automation_id)
    scope = "all"
    if (item_key or "").strip():
        q, scope = q.where(AutomationWatchItem.item_key == item_key.strip()), "item"
    elif (state or "").strip():
        q, scope = q.where(AutomationWatchItem.state == state.strip()), f"state:{state}"
    async with app_write() as s:
        res = await s.execute(q)
        await s.commit()
    n = res.rowcount or 0
    logger.info("watch memory forgotten: automation=%s scope=%s rows=%s by=%s",
                automation_id, scope, n, by)
    await _trace_manual_reset(automation_id, action="forget_watch_items",
                             by=by, detail={"scope": scope, "forgotten": n})
    return {"forgotten": n, "scope": scope}


async def _trace_manual_reset(automation_id: int, *, action: str, by: str,
                              detail: dict) -> None:
    """Leaves the MANUAL reset in the same history the operator already reads.

    Without this, forgetting the anti-repetition memory would be invisible: tomorrow the
    automation would alert again about something "it had already alerted about" and nobody
    could explain why. It is written as one more row of the eval-log with
    ``mode='manual_reset'`` — zero new audit tables, and it shows in the same evaluations tab.
    """
    from app.app_models.automations import AutomationEvalLog
    try:
        async with app_write() as s:
            s.add(AutomationEvalLog(
                automation_id=automation_id,
                at=datetime.now(timezone.utc).replace(tzinfo=None),
                mode="manual_reset", met=False, fired=False, silenced=False,
                detail=json.dumps({"action": action, "by": by, **(detail or {})},
                                  ensure_ascii=False, default=str)[:1500],
            ))
            await s.commit()
    except Exception:  # noqa: BLE001 — the trace never brings the reset down
        logger.debug("manual-reset trace failed (ignored)", exc_info=True)


async def reset_cooldown(automation_id: int, by: str = "operator") -> dict:
    """RE-ARMS the automation: clears the cooldown and leaves it ready to fire on the next
    check.

    Until now the cooldown could only be skipped with the manual Play (which bypasses it),
    which forced executing the reaction to "unstick" it. This separates the two things:
    re-arming the state without executing anything.
    """
    async with app_write() as s:
        st = (await s.execute(
            select(AutomationState).where(
                AutomationState.automation_id == automation_id)
        )).scalar_one_or_none()
        if st is None:
            return {"rearmed": False, "reason": "sin estado guardado"}
        was = {"state": st.state, "last_fired_at": iso_utc(st.last_fired_at)}
        st.state, st.last_fired_at, st.last_error = "armed", None, None
        await s.commit()
    await _trace_manual_reset(automation_id, action="reset_cooldown",
                             by=by, detail={"previous": was})
    return {"rearmed": True, "previous": was}


async def clear_silence(automation_id: int, by: str = "operator") -> dict:
    """Removes the watch's TEMPORARY silence (``silence_until``).

    It lives buried in ``condition_spec.watch`` and was not visible on any screen: an
    automation silenced for a maintenance window looked like "nothing is happening".
    Removing it here is the visible counterpart of having set it.
    """
    async with app_write() as s:
        a = (await s.execute(
            select(Automation).where(Automation.id == automation_id)
        )).scalar_one_or_none()
        if a is None:
            return {"cleared": False, "reason": "no existe"}
        spec = _json(a.condition_spec) if a.condition_spec else {}
        watch = (spec or {}).get("watch") or {}
        previous = watch.get("silence_until")
        if not previous:
            return {"cleared": False, "reason": "no estaba silenciada"}
        watch.pop("silence_until", None)
        spec["watch"] = watch
        a.condition_spec = json.dumps(spec, ensure_ascii=False, default=str)
        a.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await s.commit()
    await _trace_manual_reset(automation_id, action="clear_silence",
                             by=by, detail={"previous_silence_until": str(previous)})
    return {"cleared": True, "previous_silence_until": str(previous)}


# ── Rigour/determinism/traceability (v7.136.0) — the author does NOT guess, "Test" does not lie ──

def _describe_shape(result: Any, max_keys: int = 12) -> str:
    """Human list of the REAL fields of a tool response — for the fail-loud error and the
    dry-run ("'items' does not exist; available: tenants_scanned, tenants_with_errors[]…").
    It is what makes the agent/operator self-correct instead of guessing."""
    if isinstance(result, dict):
        parts = []
        for k in list(result.keys())[:max_keys]:
            v = result[k]
            if isinstance(v, list):
                s = next((x for x in v if isinstance(x, dict)), None)
                parts.append(f"{k}[] (lista de {len(v)}"
                             + (f"; campos: {', '.join(list(s.keys())[:8])})" if s else ")"))
            elif isinstance(v, dict):
                parts.append(f"{k}{{}} ({', '.join(list(v.keys())[:6])})")
            else:
                parts.append(k)
        return ", ".join(parts) or "(respuesta vacía)"
    if isinstance(result, list):
        s = next((x for x in result if isinstance(x, dict)), None)
        return (f"una lista de {len(result)} con campos: {', '.join(list(s.keys())[:8])}"
                if s else f"una lista de {len(result)}")
    return f"un valor {type(result).__name__}"


async def dry_run_watch(watch: dict, automation_id: Optional[int] = None) -> dict:
    """READ-ONLY evaluation of a `watch` for "Test the condition": it runs the SAME logic as
    ``resolve_watch`` (tool call + extract + ``diff_watch_items``) but WITHOUT persisting.
    It returns a rich trace: shape of the response, how ``items_at`` was resolved, per-ITEM
    classification (new/escalated/already seen/ignored) against the stored memory, verdict +
    WHY, and WARNINGS when ``items_at``/``key``/``signature`` do not match the real response.
    Determinism: same extract+diff as the real fire → "it is met" in test ⟺ it fires in
    production."""
    connector = (watch.get("connector") or "").strip()
    tool = (watch.get("tool") or "").strip()
    key_field = watch.get("key") or "key"
    sig_field = watch.get("signature")
    ignore = list(watch.get("ignore_keys") or [])
    silenced = _silenced_until(watch.get("silence_until"))
    on_error = (watch.get("on_error") or "alert").lower()
    out: dict = {"mode": "watch", "connector": connector, "tool": tool, "warnings": []}
    if not connector or not tool:
        return {**out, "evaluable": False, "warnings": ["Falta conector o acción (tool)."]}
    try:
        result = await _mcp_tool_call(
            connector, tool, _render_templates(watch.get("arguments") or {}, _now_ctx()))
    except McpCheckError as exc:
        fire = on_error in ("alert", "fire")
        return {**out, "evaluable": True, "would_fire": fire, "error": str(exc),
                "why": f"La acción falló; con «si falla»='{on_error}' "
                       + ("avisaría." if fire else "no dispararía.")}
    out["response_shape"] = _describe_shape(result)
    out["response_sample"] = json.dumps(result, ensure_ascii=False, default=str)[:1200]
    raw = _extract_value(result, watch.get("items_at"))
    if not isinstance(raw, list):
        out["warnings"].append(
            f"«Ruta a la lista» = «{watch.get('items_at') or '(vacío)'}» no devuelve una LISTA "
            f"(devuelve {'nada' if raw is None else type(raw).__name__}). "
            f"La respuesta tiene: {out['response_shape']}.")
        items = []
    else:
        items = raw
    out["items_found"] = len(items)
    sample = next((it for it in items if isinstance(it, dict)), None)
    if sample is not None:
        if key_field not in sample:
            out["warnings"].append(
                f"«Campo que identifica cada caso» = «{key_field}» no existe en los ítems. "
                f"Campos: {', '.join(list(sample.keys())[:10])}.")
        if sig_field and sig_field not in sample:
            out["warnings"].append(
                f"«Campo de escalado» = «{sig_field}» no existe en los ítems. "
                f"Campos: {', '.join(list(sample.keys())[:10])}.")
    ignored = [it for it in items if isinstance(it, dict) and it.get(key_field) is not None
               and key_ignored(str(it.get(key_field)), ignore)]
    kept = [it for it in items if it not in ignored]
    stored: dict = {}
    if automation_id is not None:
        try:
            from sqlalchemy import select as _sel
            from app.app_models.automations import AutomationWatchItem
            async with app_session() as s:
                rows = (await s.execute(_sel(AutomationWatchItem).where(
                    AutomationWatchItem.automation_id == automation_id))).scalars().all()
                stored = {r.item_key: {"signature": r.signature, "state": r.state} for r in rows}
        except Exception:  # noqa: BLE001 — leer memoria es best-effort
            logger.debug("dry_run_watch: no se pudo leer la memoria del watch", exc_info=True)
    d = diff_watch_items(stored, kept, key_field, sig_field)
    fresh_keys = {str(it.get(key_field)) for it in d["fresh"] if isinstance(it, dict)}
    esc_keys = {str(it.get(key_field)) for it in d["escalated"] if isinstance(it, dict)}

    def _cls(it: Any) -> str:
        k = str(it.get(key_field)) if isinstance(it, dict) else None
        return "nuevo" if k in fresh_keys else "escalado" if k in esc_keys else "ya visto"

    per = [{"key": (str(it.get(key_field)) if isinstance(it, dict) else None),
            "signature": (str(it.get(sig_field)) if sig_field and isinstance(it, dict) else None),
            "class": _cls(it)} for it in kept[:50]]
    per += [{"key": (str(it.get(key_field)) if isinstance(it, dict) else None),
             "class": "ignorado"} for it in ignored[:20]]
    would_fire = bool(d["fresh"] or d["escalated"]) and not silenced
    disp = sorted(fresh_keys | esc_keys)
    return {**out, "evaluable": True, "would_fire": would_fire, "silenced": silenced,
            "first_run": not stored,
            "counts": {"nuevos": len(d["fresh"]), "escalados": len(d["escalated"]),
                       "ya_vistos": len(d["ongoing"]), "resueltos": len(d["resolved"]),
                       "ignorados": len(ignored)},
            "items": per,
            "why": ("Dispararía por: " + ", ".join(disp[:8]) if would_fire
                    else "En silencio (ventana de mantenimiento)." if silenced
                    else "No dispararía: nada nuevo ni escalado (todo ya visto)." if items
                    else "No dispararía: la lista de casos está vacía.")}


async def validate_condition_config(cond: dict) -> None:
    """v7.136.0 — FAIL-LOUD on create/edit. For `watch`/`checks` it runs the tool ONCE and
    validates the author's paths against the REAL response; it raises ValueError (→422) with
    the available fields when ``items_at``/``key``/``signature``/``extract`` do not match → the
    automation is NOT born dead (and the agent self-corrects). If the tool itself fails
    (connector down), it does NOT block: it is not rejected over a transient outage; the
    runtime ``on_error`` covers it."""
    # v7.138.0 — defensive COERCION: create/update serialize condition_spec to a JSON string
    # BEFORE calling here (_validate line ~166); 7.136.0 passed that str → cond.get() blew up
    # with a 500 and the fail-loud validation NEVER ran. We accept dict or str and normalize.
    if isinstance(cond, str):
        try:
            cond = json.loads(cond or "{}")
        except (ValueError, TypeError):
            raise ValueError("La condición (condition_spec) no es un JSON válido.")
    cond = cond if isinstance(cond, dict) else {}
    mode = (cond.get("mode") or "always").lower()
    if mode not in CONDITION_MODES:
        raise ValueError(f"Modo del «solo si» «{mode}» desconocido. Válidos: {', '.join(CONDITION_MODES)}.")
    pre = cond.get("pre")
    if isinstance(pre, dict) and (pre.get("checks") is not None):
        _validate_checks_shape(pre.get("checks") or [], "la comprobación previa (pre)")
        await _validate_checks_paths(pre.get("checks") or [])
    if mode == "watch":
        w = cond.get("watch") or {}
        if not (w.get("connector") and w.get("tool")):
            raise ValueError("La vigilancia necesita un conector y una acción (tool).")
        try:  # v7.138.1 — time-bounded: a slow watch tool does not block saving (504).
            result = await asyncio.wait_for(
                _mcp_tool_call(w["connector"], w["tool"],
                               _render_templates(w.get("arguments") or {}, _now_ctx())),
                timeout=_VALIDATE_PATH_TIMEOUT_S)
        except (McpCheckError, asyncio.TimeoutError, Exception):  # noqa: BLE001
            return  # cannot be validated in time → allowed (on_error covers it; the dry-run validates thoroughly)
        raw = _extract_value(result, w.get("items_at"))
        if not isinstance(raw, list):
            raise ValueError(
                f"«Ruta a la lista» = «{w.get('items_at') or '(vacío)'}» no devuelve una lista. "
                f"La respuesta de «{w['tool']}» tiene: {_describe_shape(result)}. "
                f"Pon la ruta REAL a la lista.")
        sample = next((it for it in raw if isinstance(it, dict)), None)
        if sample is not None:
            keyf = w.get("key") or "key"
            if keyf not in sample:
                raise ValueError(
                    f"«Campo que identifica cada caso» = «{keyf}» no existe en los ítems. "
                    f"Campos disponibles: {', '.join(list(sample.keys())[:12])}.")
            sigf = w.get("signature")
            if sigf and sigf not in sample:
                raise ValueError(
                    f"«Campo de escalado» = «{sigf}» no existe en los ítems. "
                    f"Campos disponibles: {', '.join(list(sample.keys())[:12])}.")
    elif mode == "checks":
        checks = cond.get("checks") or []
        if not checks:
            raise ValueError("El «solo si» por comprobaciones necesita AL MENOS una comprobación.")
        combine = (cond.get("combine") or "all").lower()
        if combine not in ("all", "any"):
            raise ValueError(
                f"«Cómo combinar» = «{combine}» no vale (usa «all» = todas, o «any» = cualquiera).")
        _validate_checks_shape(checks, "el «solo si»")
        await _validate_checks_paths(checks)
    elif mode == "data":
        if not (cond.get("metric") or cond.get("sql") or cond.get("signal") or cond.get("source")):
            raise ValueError("El «solo si» por dato necesita QUÉ medir (una métrica o consulta).")
        _require_threshold_op(cond, "el «solo si» por dato")
    elif mode == "sql":
        if not (cond.get("sql") or "").strip():
            raise ValueError("El «solo si» por SQL necesita una consulta SELECT.")
    # 'always' and 'agent' have no extra structural requirements (agent is decided when firing).


def _validate_checks_shape(checks: Any, where: str) -> None:
    """v7.138.0 — VALIDATES the SHAPE of a checklist BEFORE touching the network: that it is a
    list of objects, each with connector+tool and a comparator with a threshold when the
    comparator requires it. It catches the author's (incl. the AGENT's) most common mistakes:
    malformed check, EMPTY threshold (e.g. "contains" without text or "gte" without a number
    → a silently broken check)."""
    if not isinstance(checks, list):
        raise ValueError(f"{where}: las comprobaciones deben ser una lista.")
    for i, chk in enumerate(checks, 1):
        if not isinstance(chk, dict):
            raise ValueError(f"{where}: la comprobación #{i} está mal formada "
                             f"(debe ser un objeto, llegó {type(chk).__name__}).")
        name = chk.get("name") or chk.get("tool") or f"#{i}"
        if not (chk.get("connector") and chk.get("tool")):
            raise ValueError(f"La comprobación «{name}» necesita conector y acción (tool).")
        _require_threshold_op(chk, f"la comprobación «{name}»")


def _require_threshold_op(spec: dict, label: str) -> None:
    """v7.138.0 — the comparator exists and, if it needs it, carries a NON-empty
    threshold/value. The empty threshold was a footgun: "contains" with empty text or "gte"
    without a number give a check that checks nothing (and made an "any" look always met)."""
    op = (spec.get("op") or spec.get("operator") or "non_empty").lower()
    if op not in _CHECK_OPS:
        raise ValueError(f"En {label}, el comparador «{op}» no existe. Válidos: {', '.join(_CHECK_OPS)}.")
    if op in _OPS_NEED_THRESHOLD:
        thr, val = spec.get("threshold"), spec.get("value")
        if thr in (None, "") and val in (None, ""):
            raise ValueError(
                f"En {label}, el comparador «{op}» necesita un umbral/valor y está VACÍO. "
                f"Indica el valor a comparar (p. ej. 1 para «gte», o el texto para «contains»).")


async def _validate_checks_paths(checks: list) -> None:
    """Validates the extraction PATHS by EXECUTING the tool — best-effort and TIME-BOUNDED
    (v7.138.1): a slow tool (60-120 s sweep) NEVER blocks saving (before it gave 504 at the
    gateway). The shape/threshold were already validated separately (instant, mandatory).
    Here each path has a short timeout and there is a TOTAL budget; if a tool does not answer
    in time (or fails), it is ALLOWED — the runtime ``on_error`` covers a bad path and the
    user can validate it thoroughly with the dry-run."""
    to_check = [c for c in (checks or []) if (c.get("extract") or "").strip()]
    if not to_check:
        return

    async def _one(chk: dict) -> Optional[str]:
        try:
            result = await asyncio.wait_for(
                _mcp_tool_call(chk["connector"], chk["tool"],
                               _render_templates(chk.get("arguments") or {}, _now_ctx())),
                timeout=_VALIDATE_PATH_TIMEOUT_S)
        except (McpCheckError, asyncio.TimeoutError, Exception):  # noqa: BLE001
            return None  # could not be validated in time → allowed (saving is not blocked)
        extract = (chk.get("extract") or "").strip()
        if extract and _extract_value(result, extract) is None:
            return (f"En la señal «{chk.get('name') or chk['tool']}», «Campo a mirar» = «{extract}» "
                    f"no devuelve valor. La respuesta tiene: {_describe_shape(result)}.")
        return None

    try:  # TOTAL budget: if there is no time to validate all the paths, it is allowed (it does not block)
        msgs = await asyncio.wait_for(asyncio.gather(*[_one(c) for c in to_check]),
                                      timeout=_VALIDATE_PATHS_BUDGET_S)
    except asyncio.TimeoutError:
        return
    for msg in msgs:
        if msg:
            raise ValueError(msg)


# ── Evaluations ledger — traceability of EACH tick (incl. the ones that do NOT fire) ────

def _eval_detail(ctx: dict) -> dict:
    """COMPACT summary of a tick for the trace (without dumping the whole ctx)."""
    ctx = ctx or {}
    out: dict = {}
    if isinstance(ctx.get("checks"), list):
        out["checks"] = [{"name": c.get("name"), "value": c.get("value"), "met": c.get("met"),
                          "error": c.get("error")} for c in ctx["checks"][:8]]
    if ctx.get("watch"):
        out["watch"] = ctx["watch"]
    fresh = ctx.get("fresh_items")
    if fresh:
        out["fresh_keys"] = [(i.get("tenant") or i.get("key") or i.get("fingerprint") or i.get("id"))
                             for i in fresh if isinstance(i, dict)][:10]
    if ctx.get("value") is not None:
        out["value"] = ctx["value"]
    return out


async def record_eval(session, automation_id: int, *, mode, met: bool, fired: bool,
                      ctx: dict, run_id: Optional[int] = None) -> None:
    """Best-effort: inserts ONE trace row per evaluation. It NEVER breaks the watcher loop."""
    from datetime import datetime, timezone
    from app.app_models.automations import AutomationEvalLog
    try:
        watch = (ctx or {}).get("watch") or {}
        session.add(AutomationEvalLog(
            automation_id=automation_id,
            at=datetime.now(timezone.utc).replace(tzinfo=None),
            mode=mode or None, met=bool(met), fired=bool(fired),
            fresh=watch.get("fresh"), resolved=watch.get("resolved"),
            silenced=bool(watch.get("silenced")),
            error=(ctx or {}).get("error") or (ctx or {}).get("watch_error"),
            detail=json.dumps(_eval_detail(ctx), ensure_ascii=False, default=str)[:1500],
            run_id=run_id))
        await session.flush()
    except Exception:  # noqa: BLE001 — the trace never brings the evaluation down
        logger.debug("eval-log insert failed (ignored)", exc_info=True)


async def list_eval_log(automation_id: int, limit: int = 100,
                        only_notable: bool = False,
                        since: datetime | None = None,
                        until: datetime | None = None) -> list[dict]:
    """Evaluations history (most recent first). `only_notable`=True leaves only the ones that
    fired, errored or resolved something (so as not to drown in the 'all ok').

    v7.140.0 — it accepts a range (`since`/`until`) so the list and the figures of the
    governance panel talk about the SAME period; without it the operator compares a 7-day
    funnel with a detail of "the last 100 ticks", which does not add up.
    """
    from sqlalchemy import or_
    from app.app_models.automations import AutomationEvalLog
    async with app_session() as s:
        q = select(AutomationEvalLog).where(AutomationEvalLog.automation_id == automation_id)
        if only_notable:
            q = q.where(or_(AutomationEvalLog.fired.is_(True),
                            AutomationEvalLog.error.isnot(None),
                            AutomationEvalLog.resolved > 0))
        if since is not None:
            q = q.where(AutomationEvalLog.at >= since)
        if until is not None:
            q = q.where(AutomationEvalLog.at < until)
        q = q.order_by(AutomationEvalLog.at.desc()).limit(min(int(limit or 100), 500))
        rows = (await s.execute(q)).scalars().all()
    return [{"id": r.id, "at": iso_utc(r.at), "mode": r.mode, "met": r.met, "fired": r.fired,
             "fresh": r.fresh, "resolved": r.resolved, "silenced": r.silenced, "error": r.error,
             "detail": _json(r.detail) if r.detail else None, "run_id": r.run_id} for r in rows]


async def eval_log_summary(automation_id: int, since: datetime | None = None,
                           until: datetime | None = None) -> dict:
    """v7.137.0 — totals of the trace to FRAME the UI without dumping thousands of rows.

    v7.140.0 — it reads as a FUNNEL (checks → passed the "only if" → fired → silenced) and
    accepts a range. `met` and `silenced` were missing, and they are exactly the two numbers
    that explain the gap between "I checked it" and "I warned you": without them, "it fired 4
    of 340" does not say whether the condition was not met or whether it was met and the
    anti-repetition held it back.
    """
    from sqlalchemy import func
    from app.app_models.automations import AutomationEvalLog
    def _scoped(stmt):
        if since is not None:
            stmt = stmt.where(AutomationEvalLog.at >= since)
        if until is not None:
            stmt = stmt.where(AutomationEvalLog.at < until)
        return stmt
    async with app_session() as s:
        base = _scoped(select(func.count(AutomationEvalLog.id)).where(
            AutomationEvalLog.automation_id == automation_id))
        total = (await s.execute(base)).scalar() or 0
        met = (await s.execute(base.where(AutomationEvalLog.met.is_(True)))).scalar() or 0
        fired = (await s.execute(base.where(AutomationEvalLog.fired.is_(True)))).scalar() or 0
        silenced = (await s.execute(
            base.where(AutomationEvalLog.silenced.is_(True)))).scalar() or 0
        errors = (await s.execute(base.where(AutomationEvalLog.error.isnot(None)))).scalar() or 0
        last = (await s.execute(_scoped(
            select(AutomationEvalLog.at).where(AutomationEvalLog.automation_id == automation_id)
        ).order_by(AutomationEvalLog.at.desc()).limit(1))).scalar()
    # `checks` is the governance funnel name (VIG-52/56); keep `total` as an
    # alias so the SPA copy that still interpolates {{total}} keeps working.
    n = int(total)
    return {"checks": n, "total": n, "met": int(met), "fired": int(fired),
            "silenced": int(silenced), "errors": int(errors),
            # Same derivation as the governance funnel (`automation_stats.funnel`),
            # so the two screens can never tell different stories about the gap
            # between "the condition held" and "you were told".
            "held": max(0, int(met) - int(fired) - int(silenced)),
            "last_at": iso_utc(last) if last else None}


async def rollup_eval_log(days_back: int = 3) -> int:
    """v7.140.0 — SUMMARISE the last ``days_back`` COMPLETE days into
    ``automation_daily_stats`` (one row per automation per day).

    Why this exists: the operator asks "how many times did this fire in the last
    month?", but the row-by-row ledger is pruned at 14 days. Without a rollup the
    answer would silently shrink as the detail expired — a traceability tool that
    quietly lies is worse than none. So the totals are frozen here BEFORE the
    prune, and the prune only runs if this succeeded (see ``main.py``).

    Cheap and idempotent: ONE grouped query over an indexed window, and each day
    is written with ``ON CONFLICT DO UPDATE`` (overwrite, never accumulate), so
    re-running it — on every boot, several times a night — cannot double-count.
    Only COMPLETE days are summarised; today is always computed live from the
    ledger, which still holds it.

    Cost/tokens are deliberately NOT rolled up: they live in ``agent_runs``,
    which is not pruned for autonomous runs, so there is one source per number.
    """
    from datetime import timedelta
    from sqlalchemy import case, func
    from app.app_models.automations import AutomationEvalLog
    from app.app_models.automation_daily_stats import AutomationDailyStats
    from app.app_database import app_write
    from app.automation_stats import rows_where as _rows
    from app.timeutil import business_tz

    tz = business_tz()
    today_local = datetime.now(tz).date()
    first_day = today_local - timedelta(days=max(int(days_back), 1))
    # Window in UTC (stored naive-UTC): from the start of the first day we
    # summarise, up to the start of TODAY in business time.
    start_utc = datetime.combine(
        first_day, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = datetime.combine(
        today_local, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    if end_utc <= start_utc:
        return 0
    # SQLite's ``date()`` with a fixed offset gives the BUSINESS day directly, so
    # the grouping happens in the DB (one pass) instead of in Python row by row.
    offset_h = (datetime.now(tz).utcoffset() or timedelta(0)).total_seconds() / 3600.0
    day_expr = func.date(AutomationEvalLog.at, f"{offset_h:+.2f} hours")
    async with app_write() as s:
        rows = (await s.execute(
            select(
                AutomationEvalLog.automation_id,
                day_expr.label("day"),
                func.count(AutomationEvalLog.id),
                # Boolean columns are counted with CASE, never SUMmed: SUM over a
                # Boolean comes back through SQLAlchemy's Boolean processor and a
                # day with 12 fires would be frozen here as `1` — irreversibly,
                # since the row-by-row detail is pruned at 14 days. See
                # `automation_stats.rows_where`.
                func.sum(_rows(AutomationEvalLog.met)),
                func.sum(_rows(AutomationEvalLog.fired)),
                func.sum(_rows(AutomationEvalLog.silenced)),
                func.sum(case((AutomationEvalLog.error.isnot(None), 1), else_=0)),
            )
            .where(AutomationEvalLog.at >= start_utc, AutomationEvalLog.at < end_utc)
            .group_by(AutomationEvalLog.automation_id, day_expr)
        )).all()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for aid, day, checks, met, fired, silenced, errors, *_ in rows:
            vals = {"checks": int(checks or 0), "met": int(met or 0),
                    "fired": int(fired or 0), "silenced": int(silenced or 0),
                    "errors": int(errors or 0), "rolled_up_at": now}
            await s.execute(sqlite_insert(AutomationDailyStats).values(
                automation_id=int(aid), day=str(day), **vals,
            ).on_conflict_do_update(
                index_elements=[AutomationDailyStats.automation_id,
                                AutomationDailyStats.day],
                set_=vals,
            ))
        await s.commit()
    return len(rows)


async def prune_eval_log(days: int = 14) -> int:
    """Prunes trace rows older than `days` (volume control). Returns how many it deleted.

    v7.140.0 — the caller must run ``rollup_eval_log`` BEFORE and NOT prune if it failed: the
    historical totals live in the daily summary, so pruning without summarising would lose
    forever "how many times it fired last month".
    """
    from datetime import timedelta
    from sqlalchemy import delete
    from app.app_models.automations import AutomationEvalLog
    from app.app_database import app_write
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(int(days), 1))
    async with app_write() as s:
        res = await s.execute(delete(AutomationEvalLog).where(AutomationEvalLog.at < cutoff))
        await s.commit()
        return res.rowcount or 0


async def evaluate_condition_detail(condition_spec: dict) -> dict:
    """AD-HOC, per-part evaluation of a condition_spec for the UI 'Probar' — WITHOUT
    a saved automation. Mode-aware: for ``checks`` returns EACH signal's value/met/
    error (so the operator sees exactly which one failed and why) + the combined
    verdict; for data/sql the value + verdict; ``agent`` is not cheaply dry-runnable.
    A ``pre`` guard is evaluated first and short-circuits (0 tokens for checks/data/sql)."""
    cond = condition_spec or {}
    mode = (cond.get("mode") or "always").lower()
    pre_detail = None
    pre = cond.get("pre")
    if isinstance(pre, dict) and pre:
        pmet, pctx = await _evaluate_guard(pre)
        pre_detail = {"met": pmet, **(pctx if isinstance(pctx, dict) else {})}
        if not pmet:
            return {"mode": mode, "evaluable": True, "met": False,
                    "gated_by": "pre", "pre": pre_detail}
    if mode == "checks":
        met, ctx = await evaluate_checks(cond, verbose=True)
        return {"mode": "checks", "evaluable": True, "met": met,
                "combine": ctx.get("combine"), "checks": ctx.get("checks", []),
                "explain": ctx.get("explain"), "evaluated": ctx.get("evaluated"),
                "total_checks": ctx.get("total_checks"),
                "error": ctx.get("error"), "pre": pre_detail}
    if mode == "data":
        val, ctx = await evaluate_data_condition(cond)
        return {"mode": "data", "evaluable": val is not None or not ctx.get("error"),
                "met": condition_met(val, cond), "value": val, "detail": ctx, "pre": pre_detail}
    if mode == "sql":
        v, ctx = await _eval_sql_bool(cond.get("sql") or "", cond.get("source") or "local")
        return {"mode": "sql", "evaluable": v is not None, "met": bool(v),
                "detail": ctx, "pre": pre_detail}
    if mode == "agent":
        return {"mode": "agent", "evaluable": False, "pre": pre_detail,
                "note": ("La comprobación por IA se decide en el momento del disparo "
                         "(lanza una ejecución). No se puede pre-evaluar sin coste.")}
    if mode == "watch":
        # v7.136.0 — FAITHFUL dry-run: same logic as the real fire (extract + diff), read-only,
        # with a rich trace (response, items_at resolution, per-item classification, warnings).
        # Adhoc (no saved automation) → no memory = simulates the first sweep.
        detail = await dry_run_watch(cond.get("watch") or {}, None)
        return {**detail, "met": bool(detail.get("would_fire")), "pre": pre_detail}
    return {"mode": mode or "always", "evaluable": True, "met": True, "pre": pre_detail}


def wants_precheck(a_dict: dict) -> bool:
    """v7.63.0 — should this firing be VERIFIED (read-only) BEFORE parking the
    approval, instead of parking it blind and checking at reaction time?

    True only when ALL hold:
      * the condition is ``agent`` (natural language) — data/sql/always are
        already gated deterministically by the watcher before the approval, so
        they need no pre-check;
      * there ARE instructions to check;
      * the automation ``requires_approval`` — with no approval there is no
        "approve before check" problem to fix (the single reaction run already
        checks-then-acts optimally), so a separate pre-check would only double
        the runs for no governance gain;
      * ``check_before_approval`` is on (the default; the operator can opt out).
    """
    cond = a_dict.get("condition_spec") or {}
    if (cond.get("mode") or "").lower() != "agent":
        return False
    if not (cond.get("instructions") or "").strip():
        return False
    if not a_dict.get("requires_approval"):
        return False
    return bool(a_dict.get("check_before_approval", True))


async def _settled_fire_event(session, *, aid: int, title: str, bucket: str,
                              detail: dict) -> int | None:
    """v7.140.0 — leave the SAME footprint a normal fire leaves, for a fire that
    spent 0 tokens.

    A deterministic reaction skipped the event+run on purpose (that is the whole
    saving), and the side effect was that the created entity had NO provenance:
    "who created this task?" had no answer, and the automation's history showed a
    gap where a fire really happened. So we write the ``automation_fired`` event
    ALREADY SETTLED (``COMPLETED`` + ``processed_at``): the executor's claim query
    only looks at ``PENDING``/``RETRY_SCHEDULED``, so this can never turn into a
    run — it is a receipt, not a work order.

    Idempotent on the same fingerprint the normal path uses (type+source+bucket),
    so a retry reuses the existing receipt instead of duplicating it. Returns the
    event id (existing or new), or ``None`` if it could not be written — a
    missing receipt must never break a reaction that already succeeded.
    """
    try:
        from app.app_models.runtime import Event
        from app.autonomous.watcher import _fingerprint
        fp = _fingerprint("automation_fired", f"automation:{aid}", bucket)
        now = datetime.now(timezone.utc)
        payload = {"automation_id": aid, "title": title, "bucket": bucket,
                   "deterministic": True, "detail": detail}
        await session.execute(sqlite_insert(Event).values(
            fingerprint=fp, type="automation_fired", source=f"automation:{aid}",
            payload_json=json.dumps(payload, default=str),
            status="COMPLETED", attempts=0, max_attempts=1,
            detected_at=now, processed_at=now,
        ).on_conflict_do_nothing(index_elements=[Event.fingerprint]))
        await session.flush()
        return (await session.execute(
            select(Event.id).where(Event.fingerprint == fp))).scalar()
    except Exception:  # pragma: no cover — provenance is best-effort
        logger.debug("settled fire event failed (ignored)", exc_info=True)
        return None


# ── Reaction prompt (for the executor) ───────────────────────────────
async def execute_deterministic_reaction(a_dict: dict, context: dict) -> tuple[bool, dict]:
    """Perform a TEMPLATED reaction DIRECTLY — 0 tokens, NO LLM run — for the cheap
    outcomes. Returns (done, detail): ``done=False`` means "not eligible → fall back
    to the normal LLM run". Idempotent per (automation, bucket).

    Today: ``create_task`` (a follow-up item is a pure DB insert — spending a run
    just to write a note is the cost smell this removes) and ``notify`` (v7.134.0:
    the operator-authored message is delivered straight through the pinned channel
    with 0 tokens — see the notify branch). Both fall back to the agent path when
    they can't complete deterministically (missing message/channel, send failure)."""
    rtype = canonical_reaction(a_dict.get("reaction_type"))
    rspec = a_dict.get("reaction_spec") or {}
    ctx = _now_ctx()
    bucket = (context or {}).get("bucket") or ""
    if rtype == "create_task":
        from app.app_models.business import ScheduledItem
        title = _render_templates(str(rspec.get("title") or a_dict.get("title") or "Encargo"), ctx)
        desc = _render_templates(
            str(rspec.get("description") or a_dict.get("reaction_instructions") or ""), ctx)
        assignee = (rspec.get("assignee") or "agent").strip().lower()
        assignee = assignee if assignee in ("agent", "user") else "agent"
        idem = f"autodet:{a_dict.get('id')}:{bucket}"
        aid = int(a_dict.get("id") or 0)
        async with app_write() as s:
            # The receipt goes FIRST so the task can carry its ``source_event_id``
            # and provenance resolves by the same join as an agent-produced task.
            ev_id = await _settled_fire_event(
                s, aid=aid, title=str(a_dict.get("title") or ""), bucket=bucket,
                detail={"reaction": "create_task"})
            stmt = sqlite_insert(ScheduledItem).values(
                idempotency_key=idem, title=title[:200], description=desc,
                kind="task", assignee=assignee, origin="automation",
                status="pending", priority=(rspec.get("priority") or "medium"),
                assigned_to=(rspec.get("assigned_to") or None),
                source_event_id=ev_id,
            ).on_conflict_do_nothing(index_elements=[ScheduledItem.idempotency_key])
            res = await s.execute(stmt)
            await s.commit()
        return True, {"reaction": "create_task", "deterministic": True,
                      "created": bool(res.rowcount), "title": title[:200],
                      "event_id": ev_id}
    if rtype == "notify":
        # v7.134.0 — direct TEMPLATED notify (0 tokens): the operator wrote the message in the
        # «Qué avisar» editor (reaction_spec.message) with {{tenant}}/{{now}} tokens; we send it
        # as is through the set channel, without spending an AI run. If the message or the
        # channel is missing, or sending fails, done=False is returned and it falls to the
        # agent path (which writes it with context). Idempotent by (automation, bucket) via the
        # channel's provider_id.
        from app.app_models.channels import Channel
        from app.channels.notifier import (
            NotifyIntent, _channel_notify_cfg, _notify_one_channel,
        )
        msg = _render_templates(
            str(rspec.get("message") or a_dict.get("reaction_instructions") or "").strip(), ctx)
        chan_id = rspec.get("channel_id")
        if not msg or not chan_id:
            return False, {"reaction": "notify", "deterministic": False,
                           "reason": "notify sin mensaje o sin canal → lo redacta el empleado"}
        async with app_session() as s:
            channel = (await s.execute(
                select(Channel).where(Channel.id == int(chan_id)))).scalar_one_or_none()
        if channel is None:
            return False, {"reaction": "notify", "deterministic": False,
                           "reason": "canal no encontrado → lo redacta el empleado"}
        notify_cfg, policy, max_per_day = _channel_notify_cfg(channel)
        recipient = str(rspec.get("recipient") or "").strip()
        notify_to = [recipient] if recipient else list(notify_cfg or [])
        severity = str(rspec.get("severity") or "info").lower()
        if not notify_to:
            return False, {"reaction": "notify", "deterministic": False,
                           "reason": "sin destinatario configurado → lo redacta el empleado"}
        if policy == "never" or (policy == "critical_only" and severity != "critical"):
            # The channel itself would block the send (paused / critical only): it is a valid
            # fire but without a send — do not retry through the agent.
            return True, {"reaction": "notify", "deterministic": True, "delivered": False,
                          "reason": f"canal en política '{policy}'"}
        title = _render_templates(str(rspec.get("title") or a_dict.get("title") or "").strip(), ctx)
        subject = (" ".join(title.split()) or "Aviso del empleado")[:180]
        intent = NotifyIntent(severity=severity, subject=subject, body=msg[:4000],
                              attachments=[], channels=[channel.kind])
        provider_id = f"auto_notify:a{a_dict.get('id')}-{bucket}:{channel.id}"
        try:
            sent_ids, errs = await _notify_one_channel(
                channel, notify_to, max_per_day, intent, 0, provider_id)
        except Exception:  # pragma: no cover — never break the loop; it falls to the agent
            logger.debug("deterministic notify dispatch failed", exc_info=True)
            return False, {"reaction": "notify", "deterministic": False,
                           "reason": "fallo de envío → lo reintenta el empleado"}
        return True, {"reaction": "notify", "deterministic": True,
                      "delivered": bool(sent_ids), "errors": errs or None, "subject": subject}
    return False, {"reaction": rtype, "deterministic": False,
                   "reason": "not eligible for deterministic execution"}


def build_reaction_prompt(a_dict: dict, context: dict) -> str:
    """The business prompt the executor sends when the automation fires."""
    rtype = canonical_reaction(a_dict.get("reaction_type"))  # legacy → one of the 4
    rspec = a_dict.get("reaction_spec") or {}
    instr = a_dict.get("reaction_instructions") or ""
    sig = a_dict.get("signal_summary") or a_dict.get("signal_type")
    head = (
        f"AUTOMATION FIRED: «{a_dict.get('title')}».\n"
        f"Signal: {sig}.\n"
        f"Detected context (JSON): {json.dumps(context, ensure_ascii=False)}\n\n"
    )
    # This reaction reaches the agent ONLY when it is allowed to execute:
    # either the automation doesn't require approval, or it does and the
    # operator already approved it (hard gate in watcher._park_automation_
    # approval → approvals_routes.approve emits this fire with approved=True).
    # So when approved, tell the agent to EXECUTE and NOT ask for approval again.
    approved = bool((context or {}).get("approved"))
    if approved:
        head += (
            "✅ Your manager has ALREADY APPROVED this action — go ahead and EXECUTE it now. "
            "Do NOT ask for approval again.\n\n"
        )
    # Natural-language CONDITION (mode "agent"): the watcher couldn't evaluate
    # prose, so the employee must check it FIRST, using its own tools/MCPs, and
    # only proceed if it holds. data/sql/always conditions were already gated
    # deterministically by the watcher, so they need no in-prompt gate.
    #
    # v7.63.0 — with check-before-approve, the condition was ALREADY verified by a
    # read-only pre-check BEFORE this approval was parked (context.precheck_holds).
    # In that case DON'T make the employee re-run the check: hand it the evidence
    # already found and tell it to proceed — the operator approved precisely THIS
    # verified firing.
    cond = a_dict.get("condition_spec") or {}
    # v7.68.0 — only treat the condition as ALREADY verified when the pre-check
    # returned a CLEAR "holds" verdict. An UNCONFIRMED pre-check (the run couldn't
    # return a usable verdict) was parked fail-safe, so the reaction MUST re-verify
    # rather than act on a false "you already checked" — otherwise it executes a
    # consequential action on a condition that was never actually confirmed.
    precheck_holds = (bool((context or {}).get("precheck_holds"))
                      and not bool((context or {}).get("check_unconfirmed")))
    check_evidence = str((context or {}).get("check_evidence") or "").strip()
    if (cond.get("mode") or "").lower() == "agent" and (cond.get("instructions") or "").strip():
        if precheck_holds:
            head += (
                "YOU ALREADY CHECKED (before the approval) that the condition is met: "
                f"«{cond['instructions'].strip()}».\n"
                + (f"Evidence found: {check_evidence[:800]}\n" if check_evidence else "")
                + "Do not repeat that check; go straight on with:\n\n"
            )
        else:
            head += (
                "BEFORE ACTING, CHECK the following with your tools/MCPs: "
                f"«{cond['instructions'].strip()}».\n"
                "If it is NOT met, do nothing else: leave a brief record that you "
                "checked it and it did not apply. Only if it IS met, carry on with:\n\n"
            )
    if rtype == "notify":
        # Notify ALWAYS goes out through a configured channel (email/WhatsApp)
        # to a recipient. recipient = a specific value (email / E.164 phone) or,
        # if blank, the channel's default recipient (notify_config.notify_to).
        chan = rspec.get("channel_label") or rspec.get("channel") or "the configured channel"
        who = rspec.get("recipient") or rspec.get("recipients") or "the channel's default recipient"
        # v7.134.0 — the operator writes WHAT to notify in the «Qué avisar» editor
        # (reaction_spec.message, with {{tenant}}/{{now}} tokens). On the agent path we take it
        # as the base message and ask it to complete it with the concrete data of what was
        # detected; without a message, it writes a free summary.
        msg = _render_templates(str(rspec.get("message") or "").strip(), _now_ctx())
        if msg:
            body = (
                f"Notify {who} through {chan}. The message your manager wants is:\n"
                f"«{msg}»\n"
                "Send it respecting that text and complete it with the concrete data of "
                "what was detected (figures, names, links) when they add something."
            )
        else:
            body = (
                "Write a brief, useful summary of what was detected and SEND IT through "
                f"{chan} to {who}."
            )
    elif rtype == "create_task":
        # NOTE: with execution_mode="deterministic" this outcome is done DIRECTLY
        # at fire time (0 tokens, see execute_deterministic_reaction) and never
        # reaches this prompt. This branch is the "agent" path (needs reasoning to
        # compose the follow-up).
        # v7.134.0 — create_task = DELEGATE to a person / POSTPONE / leave in FOLLOW-UP; it
        # is NOT "do it now" (for what can be done now there is `act`).
        body = (
            "Record a follow-up TASK with `schedule_task` for what was "
            "detected —to delegate it to a person, postpone it or leave it in "
            "follow-up—. Do NOT execute it now."
        )
    elif rtype == "generate_deliverable":
        fmt = rspec.get("format") or "pdf"
        body = (
            f"Generate a deliverable in {fmt} format with what was detected, save it in "
            "`exports/` and deliver it through the configured channel."
        )
    elif rtype == "act":
        body = (
            "Execute the configured action now (it is already APPROVED by your manager)."
            if approved else
            "Execute the configured action ONLY if it is within your permissions; if "
            "it is consequential or you are in doubt, use `request_approval` instead."
        )
    else:
        body = "Handle what was detected according to your judgement."
    if instr.strip():
        # Instructions are authored in MARKDOWN (like a skill). Hand them to the
        # employee as a fenced section so the structure (headings, lists, steps)
        # survives verbatim and reads clearly.
        body += (
            "\n\n## Your manager's instructions\n"
            f"{instr.strip()}"
        )
    # 7.92.0 — "and let me know when done": ANY action can carry an optional notice
    # (``reaction_spec.notify = {channel_id, channel_label, channel_kind, recipient}``, the
    # "Y cómo te informa" section of the form). It is the COMMUNICATION axis, separate from
    # WHAT IT DOES: before only the ``notify`` reaction could notify, so "do it AND let me
    # know" was not expressible. Empty recipient = the channel's default recipient
    # (notify_config).
    _nspec = rspec.get("notify") if isinstance(rspec.get("notify"), dict) else None
    if _nspec and rtype != "notify" and (_nspec.get("channel_id") or _nspec.get("channel_label")):
        _chan = _nspec.get("channel_label") or "the configured channel"
        _who = _nspec.get("recipient") or "the channel's default recipient"
        body += (
            "\n\n## Notify when done\n"
            f"When you finish, communicate the RESULT (what you did or what you found, "
            f"brief and in business language, in the recipient's language) through {_chan} "
            f"to {_who}."
        )
    # v7.84.0 — reference-material dependency: if this order declares knowledge
    # tags, tell the employee to consult the DOCUMENT LIBRARY (search_knowledge)
    # for the OFFICIAL content it needs, and to cite the source — never guess.
    rk = (a_dict.get("requires_knowledge") or "").strip()
    if rk:
        tags = ", ".join(t.strip() for t in rk.replace("\n", ",").split(",") if t.strip())
        if tags:
            body += (
                "\n\n## Reference material\n"
                f"Before answering or acting, consult the reference material about: "
                f"**{tags}** with `search_knowledge`, use the official content and cite the "
                "document. If you find nothing, say so — do not make it up."
            )
    return head + body


# ── Check-before-approve (v7.63.0) ───────────────────────────────────
# The READ-ONLY verification run: for an ``agent`` condition + requires_approval,
# the employee first VERIFIES the natural-language condition with its tools/MCPs
# WITHOUT acting, and reports a structured verdict. Only if it holds do we park
# the approval (enriched with the evidence). The executor gives this run no write
# grant and none of the act-oriented trailers (traceability/delivery/notify), so
# it is effectively side-effect-free on the consequential (external) side.
def build_condition_check_prompt(a_dict: dict, context: dict) -> str:
    cond = a_dict.get("condition_spec") or {}
    instructions = (cond.get("instructions") or "").strip()
    sig = a_dict.get("signal_summary") or a_dict.get("signal_type")
    rtype = a_dict.get("reaction_type")
    return (
        f"PRIOR CHECK (only verify, do NOT act) of the automation "
        f"«{a_dict.get('title')}».\n"
        f"Signal that fired it: {sig}.\n"
        f"Detected context (JSON): {json.dumps(context, ensure_ascii=False)}\n\n"
        "Your ONLY task now is to CHECK, with your read tools/MCPs, "
        f"whether this condition is met: «{instructions}».\n"
        "Do NOT execute the action, do NOT send anything, do NOT create or modify anything, do "
        "NOT ask for approval: only query and decide whether the condition is really met NOW. "
        "If your manager approves it later, you will execute the planned "
        f"action ({rtype}); now you only verify.\n\n"
        "ALWAYS answer in the STRUCTURED_RESPONSE with an `automation_check` object:\n"
        '{"automation_check": {"condition_holds": true|false, '
        '"evidence": "brief, concrete explanation of what you found (figures, '
        'names, ids), in your manager\'s language", "confidence": "high|medium|low"}}\n'
        "Be honest: if you cannot check it or there are doubts, say so with "
        "confidence=low and condition_holds according to your best conservative judgement."
    )


def _parse_check_verdict(structured: dict | None) -> dict:
    """Normalise the agent's ``automation_check`` block into
    {holds: bool|None, evidence: str, confidence: str}. ``holds=None`` means the
    run didn't return a usable verdict (missing/garbled) → the caller fails SAFE
    by still parking the approval, but flags it as unconfirmed."""
    block = (structured or {}).get("automation_check")
    if not isinstance(block, dict):
        return {"holds": None, "evidence": "", "confidence": ""}
    raw = block.get("condition_holds")
    if isinstance(raw, str):
        holds = raw.strip().lower() in ("true", "yes", "sí", "si", "1")
    elif raw is None:
        holds = None
    else:
        holds = bool(raw)
    return {
        "holds": holds,
        "evidence": str(block.get("evidence") or "").strip()[:1000],
        "confidence": str(block.get("confidence") or "").strip().lower(),
    }


async def settle_condition_check(
    automation_id: int, bucket: str, context: dict, structured: dict | None,
    check_run_id: int, *, test_only: bool = False,
) -> dict:
    """Post-process a completed condition-check run (v7.63.0).

    * Always records the verdict on the automation state (last check + run).
    * ``test_only`` (the operator's "Probar") → state only; never parks an
      approval nor logs a decision.
    * Real pre-check: if the condition HOLDS (or the verdict is unconfirmed →
      fail-safe) → park the approval, enriched with the evidence and marked
      ``precheck_holds`` so the reaction run skips re-checking. If it does NOT
      hold → log a ``no_action`` decision so the operator sees the employee
      checked and nothing was needed (and NO approval is requested).
    Best-effort: never raises into the executor.
    """
    from app.autonomous.watcher import _park_automation_approval
    verdict = _parse_check_verdict(structured)
    holds, evidence, confidence = verdict["holds"], verdict["evidence"], verdict["confidence"]
    async with app_write() as s:
        a = (await s.execute(select(Automation).where(Automation.id == automation_id))).scalar_one_or_none()
        if a is None:
            return {"settled": False, "reason": "automation gone"}
        st = (await s.execute(
            select(AutomationState).where(AutomationState.automation_id == automation_id)
        )).scalar_one_or_none()
        if st is None:
            st = AutomationState(automation_id=automation_id, state="armed")
            s.add(st)
        st.last_run_id = check_run_id
        st.last_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
        st.last_value_json = json.dumps(
            {"check": {"holds": holds, "evidence": evidence, "confidence": confidence,
                       "test_only": bool(test_only)}}, default=str,
        )
        st.last_error = None

        if test_only:
            await s.commit()
            return {"settled": True, "test_only": True, "holds": holds}

        if holds is False:
            # Verified: the condition does NOT hold → no approval, log a decision
            # so the trail shows the employee checked and nothing was needed.
            from app.app_models.business import Decision
            reason = evidence or "El empleado comprobó la condición y no se cumplía."
            idem = f"autocheck-noop:{automation_id}:{bucket}"
            await s.execute(
                sqlite_insert(Decision).values(
                    idempotency_key=idem,
                    created_by_run_id=check_run_id,
                    decision="no_action",
                    reason_code="no_action_needed",
                    confidence=(confidence or "medium"),
                    ref_kind="automation",
                    ref_id=str(automation_id),
                    reason_md=(
                        f"Automatización «{a.title}»: comprobación previa a la "
                        f"aprobación. La condición NO se cumple, así que no se pide "
                        f"aprobación ni se actúa.\n\n{reason}"
                    ),
                    status="dismissed",
                ).on_conflict_do_nothing(index_elements=[Decision.idempotency_key])
            )
            await s.commit()
            logger.info(
                "automation %s pre-check: condition NOT met — no approval parked (bucket=%s)",
                automation_id, bucket,
            )
            return {"settled": True, "holds": False}

        # holds is True OR None (unconfirmed) → park the approval. When
        # unconfirmed, we still park it (never silently drop a firing) but say so.
        enriched = {
            **(context or {}),
            "precheck_holds": True,
            "check_evidence": evidence,
            "check_run_id": check_run_id,
            "check_confidence": confidence,
            "check_unconfirmed": holds is None,
        }
        parked = await _park_automation_approval(
            s, a, enriched, bucket,
            check_evidence=evidence, check_run_id=check_run_id,
            unconfirmed=(holds is None),
        )
        await s.commit()
        logger.info(
            "automation %s pre-check: condition %s — approval %s (bucket=%s)",
            automation_id, "unconfirmed" if holds is None else "met",
            "parked" if parked else "deduped", bucket,
        )
        return {"settled": True, "holds": holds, "parked": parked}


async def run_condition_check_now(aid: int, *, by: str = "", test_only: bool = True) -> Optional[dict]:
    """Queue a READ-ONLY condition-check run NOW (the operator's "Probar" on an
    ``agent`` condition). Returns ``{"queued": True, ...}`` or None if missing.

    With ``test_only`` (the default for the button) the check reports its verdict
    on the automation state/history WITHOUT parking an approval or acting."""
    from app.autonomous.watcher import _emit_condition_check
    async with app_write() as s:
        a = (await s.execute(select(Automation).where(Automation.id == aid))).scalar_one_or_none()
        if a is None:
            return None
        now = datetime.now(timezone.utc)
        bucket = f"check-{now.timestamp():.6f}"
        ctx = {"test_only": bool(test_only), "triggered_by": by, "manual": True}
        emitted = await _emit_condition_check(s, a, ctx, bucket)
        await s.commit()
    return {"queued": bool(emitted), "automation_id": aid, "title": a.title, "bucket": bucket}
