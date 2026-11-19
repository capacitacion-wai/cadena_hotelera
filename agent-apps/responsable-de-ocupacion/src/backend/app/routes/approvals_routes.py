"""Approvals API — operator go/no-go on consequential agent actions.

The employee parks an action with the ``request_approval`` MCP tool
instead of acting. Here the operator:

* **Approves** → the app emits an ``APPROVAL_GRANTED`` event so the
  employee actually carries out the action (executor → autonomous run).
* **Rejects** → the app records the operator's feedback to the affected
  user's memory (a pinned ``preference``) so the employee learns the
  boundary, and marks the row rejected.

Status is OPERATOR-owned; the agent never flips it.

Who may decide (v7.145.0): ``require_approver`` — an admin of the employee, or a
person an admin designated in Usuarios. Deleting a request stays admin-only: that
is housekeeping of the record, not the business decision.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import delete as sql_delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.app_database import app_session, app_write
from app.app_models.business import Approval
from app.app_models.memory import UserMemory
from app.app_models.runtime import Event
from app.auth import (
    TokenPayload, require_admin, require_approver, require_project_access,
)
from app.timeutil import iso_utc, now_utc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/approvals", tags=["approvals"])

APPROVAL_GRANTED_EVENT_TYPE = "APPROVAL_GRANTED"
_STATUSES = ("pending", "approved", "rejected", "executed", "failed")


def _serialize(a: Approval) -> dict[str, Any]:
    try:
        payload = json.loads(a.action_payload_json or "{}")
    except Exception:
        payload = {}
    return {
        "id": a.id,
        "title": a.title,
        "summary_md": a.summary_md or "",
        "action_type": a.action_type,
        "action_payload": payload,
        "risk": a.risk,
        "requested_for": a.requested_for,
        "status": a.status,
        "decided_by": a.decided_by,
        "decided_at": iso_utc(a.decided_at),
        "feedback": a.feedback,
        "created_by_run_id": a.created_by_run_id,
        # Closure of the loop: the run that actually CARRIED OUT the approved
        # action (set by ``mark_approval_executed``) → the Approvals UI links to
        # it ("Ver ejecución").
        "executed_by_run_id": a.executed_by_run_id,
        "executed_at": iso_utc(a.executed_at),
        "execution_result": a.execution_result,
        "created_at": iso_utc(a.created_at),
    }


def lo_que_se_envia(raw_body: str | None) -> list[dict[str, Any]]:
    """The parked request as the approver needs it: each tool that will run and its
    arguments, exactly as they will be sent (14.15.0).

    The approval only carried ``request_ref``: what would actually be posted —the text of the
    reply, the comment it answers, the record it creates— was stored here and shown nowhere,
    so people approved blind. A Composio router call (``COMPOSIO_MULTI_EXECUTE_TOOL``) is
    unwrapped into its real tools; the agent's own notes in it (``thought``…) are not
    shown."""
    try:
        d = json.loads(raw_body or "null")
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for m in (d if isinstance(d, list) else [d]):
        p = (m or {}).get("params") if isinstance(m, dict) else None
        if not isinstance(p, dict):
            continue
        args = p.get("arguments") if isinstance(p.get("arguments"), dict) else {}
        varias = args.get("tools")
        if isinstance(varias, list):
            for t in varias:
                if isinstance(t, dict):
                    out.append({"tool": str(t.get("tool_slug") or t.get("tool") or ""),
                                "arguments": t.get("arguments") if isinstance(t.get("arguments"), dict) else {}})
        else:
            out.append({"tool": str(p.get("name") or ""), "arguments": args})
    return out


async def _con_peticion(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adds ``request`` (see :func:`lo_que_se_envia`) to each connector write still parked."""
    refs = {str((i.get("action_payload") or {}).get("request_ref") or "")
            for i in items if i.get("action_type") == "tool_call"} - {""}
    if not refs:
        return items
    from app.app_models.runtime import PendingToolRequest
    async with app_session() as s:
        filas = (await s.execute(select(PendingToolRequest.request_ref, PendingToolRequest.raw_body)
                                 .where(PendingToolRequest.request_ref.in_(refs)))).all()
    por_ref = {r: lo_que_se_envia(b) for r, b in filas}
    for i in items:
        ref = str((i.get("action_payload") or {}).get("request_ref") or "")
        if ref in por_ref:
            i["request"] = por_ref[ref]
    return items


@router.get("/producers")
async def approval_producers(_: TokenPayload = Depends(require_approver)):
    """Automations that have created approvals — populates the provenance filter."""
    from app import automation_provenance as prov

    return {"items": await prov.producers_of("approvals")}


@router.get("")
async def list_approvals(
    request: Request,
    status: Optional[str] = Query(None),
    # 14.18.0 — only who may DECIDE sees approvals (decision of the user: a member who cannot
    # approve has nothing to do on this screen). Before, they saw the ones requested for them.
    user: TokenPayload = Depends(require_approver),
):
    from app import automation_provenance as prov
    from app.users_directory import can_approve

    aid, only_open = prov.parse_provenance_query(request)
    # SECURITY: whoever does NOT decide only sees what was requested FOR THEM. The
    # `action_payload` is the parked request as is (e.g. the email draft written in another
    # person's chat); with plain `require_project_access` any member read everyone's.
    # Deciding was already gated (`require_approver`); reading was not.
    solo_mias = not await can_approve(user.email)
    async with app_session() as s:
        stmt = select(Approval)
        if solo_mias:
            stmt = stmt.where(Approval.requested_for == (user.email or "").strip().lower())
        if status in _STATUSES:
            stmt = stmt.where(Approval.status == status)
        if aid is not None:
            clause = prov.sqlalchemy_provenance_filter(Approval, aid, only_open=only_open)
            if clause is not None:
                stmt = stmt.where(clause)
        stmt = stmt.order_by(Approval.created_at.desc()).limit(500)
        rows = (await s.execute(stmt)).scalars().all()
    items = await _con_peticion([_serialize(r) for r in rows])
    items = await prov.attach_origin("approvals", items)
    ctx = await prov.provenance_context(aid, only_open=only_open) if aid else None
    return {"items": items, "filtered_by_automation": ctx}


@router.get("/{approval_id}")
async def get_approval(
    approval_id: int,
    user: TokenPayload = Depends(require_approver),
):
    """Single approval for the DETAIL view — so a «Ver aprobación #X» link
    resolves to the full request (title, risk, summary, payload, decision and
    the run that executed it) instead of the flat list."""
    from app.users_directory import can_approve
    async with app_session() as s:
        a = (await s.execute(
            select(Approval).where(Approval.id == approval_id)
        )).scalar_one_or_none()
        if a is None:
            raise HTTPException(status_code=404, detail="not found")
        # SECURITY: same rule as the list — whoever does not decide only sees their own. 404
        # and not 403: saying "it exists but it is not yours" already tells something about another.
        if not await can_approve(user.email) and \
                (a.requested_for or "").strip().lower() != (user.email or "").strip().lower():
            raise HTTPException(status_code=404, detail="not found")
        fila = _serialize(a)
    return (await _con_peticion([fila]))[0]


@router.post("/{approval_id}/approve")
async def approve(
    approval_id: int,
    body: dict = Body(default_factory=dict),
    user: TokenPayload = Depends(require_approver),
):
    """Approve a parked action and ask the employee to execute it."""
    async with app_write() as s:
        a = (await s.execute(
            select(Approval).where(Approval.id == approval_id)
        )).scalar_one_or_none()
        if a is None:
            raise HTTPException(status_code=404, detail="not found")
        if a.status not in ("pending",):
            raise HTTPException(
                status_code=409,
                detail=f"approval already {a.status}",
            )
        now = now_utc()
        a.status = "approved"
        a.decided_by = user.email
        a.decided_at = now.replace(tzinfo=None)
        a.feedback = str(body.get("feedback") or "") or None

        try:
            payload_action = json.loads(a.action_payload_json or "{}")
        except Exception:
            payload_action = {}

        # A deferred automation reaction (parked by the hard approval gate in
        # watcher._park_automation_approval): now that it's approved, emit the
        # ``automation_fired`` event that actually runs the reaction — reusing
        # the normal executor path. The ``approved`` flag tells
        # build_reaction_prompt to EXECUTE (not ask for approval again).
        if a.action_type == "automation_reaction":
            ap = payload_action or {}
            aid = ap.get("automation_id")
            # v7.35.0 COALESCENCE — mint a run-scoped WRITE GRANT so the writes
            # this approved reaction makes don't raise the connector write gate
            # AGAIN (one approval, not two). Bound to this run via the session
            # the executor is about to create; the trusted sidecar plugin injects
            # it into connector calls; the proxy honours it (write_with_approval
            # only; read_only still denies). The LLM never sees it.
            from app.app_models.runtime import WriteGrant
            grant_id = uuid.uuid4().hex
            await s.execute(
                sqlite_insert(WriteGrant).values(grant_id=grant_id, created_at=now.replace(tzinfo=None))
                .on_conflict_do_nothing(index_elements=[WriteGrant.grant_id])
            )
            ctx = {**(ap.get("context") or {}), "approved": True, "approval_id": a.id,
                   "write_grant": grant_id}
            seed = f"autoappr-run::{a.id}::{now.isoformat()}"
            fp = hashlib.sha256(seed.encode("utf-8")).hexdigest()
            await s.execute(
                sqlite_insert(Event).values(
                    fingerprint=fp,
                    type="automation_fired",
                    source=f"automation:{aid}",
                    payload_json=json.dumps(
                        {"automation_id": aid, "title": a.title, "context": ctx, "bucket": ap.get("bucket")},
                        default=str,
                    ),
                    status="PENDING",
                    attempts=0,
                    detected_at=now.replace(tzinfo=None),
                ).on_conflict_do_nothing(index_elements=[Event.fingerprint])
            )
            event_id = (await s.execute(
                select(Event.id).where(Event.fingerprint == fp)
            )).scalar_one_or_none()
            await s.commit()
            result = _serialize(a)
            logger.info("automation approval %s approved by %s → fired (event=%s)", approval_id, user.email, event_id)
            return {**result, "event_id": int(event_id) if event_id else None}

        # ── END OF SECTION 1 ───────────────────────────────────────────────
        # Approving NO LONGER holds the write lock while talking to the
        # outside. Before, this block opened in `app_write()` and did not close until
        # AFTER calling the CRM (Composio) and the platform (write-grant).
        # Measured in `demo`: peaks of **15 s** of waiting for any other writer
        # of the process —the channel poller, the event claim, the stop
        # switch— while an approval travelled over the network (KOM-44 / INC-VIG-25).
        #
        # And it did not buy atomicity: the Composio path ALREADY did `commit()` before
        # executing, so the "approved but without event" window existed anyway. The
        # lock did not close it: it only made everyone wait inside it.
        #
        # Three sections: (1) mark approved and commit — here; (2) the network, WITHOUT
        # the lock; (3) persist the result and emit the event. The window between (1)
        # and (3) is closed by `dispatch_orphan_approvals` at startup, and that is why the
        # event's fingerprint is derived from `decided_at` —DURABLE data— and not from an
        # unrepeatable `now()`: that way the reconciler computes the SAME fingerprint and
        # `on_conflict_do_nothing` makes retrying harmless.
        await s.commit()
        decidido_en = a.decided_at
        titulo, resumen, tipo, para_quien = (a.title, a.summary_md or "",
                                             a.action_type, a.requested_for)

    # ── SECTION 2: the network, OUTSIDE the lock ───────────────────────────
    # v7.26.0 — write-policy approval (``action_type="tool_call"``): the
    # PLATFORM executes the exact recorded call itself — deterministic,
    # args-hash bound, the model never re-enters the loop for the
    # execution (same philosophy as notify recipients). The employee is
    # then informed through the normal APPROVAL_GRANTED run so it can
    # follow up on the result.
    exec_note: Optional[str] = None
    ejecutado_en = None
    resultado_ejecucion = None
    if tipo == "tool_call":
        ap = payload_action or {}
        connector = str(ap.get("connector") or "").strip().lower()
        request_ref = str(ap.get("request_ref") or "").strip()
        model = ap.get("model")
        method = ap.get("method")
        op_payload = ap.get("payload")
        if connector and request_ref:
            # v7.29.4 — Composio proxy path: REPLAY the agent's exact original
            # request (staged at veto time), so we never hand-build a Composio
            # call. The request_ref is the integrity binding: it maps to one
            # exact body and the connector must match.
            from app.routes.mcp_proxy_routes import replay_approved_request
            try:
                result_payload = await replay_approved_request(connector, request_ref)
                is_err = bool((result_payload or {}).get("isError") or (result_payload or {}).get("error"))
                exec_note = json.dumps(result_payload, ensure_ascii=False, default=str)[:2000]
            except Exception as exc:  # noqa: BLE001 — surface, never hang the approve
                is_err = True
                exec_note = f"execution failed: {exc}"[:2000]
            ejecutado_en = now_utc().replace(tzinfo=None)
            resultado_ejecucion = ("ERROR: " if is_err else "OK: ") + (exec_note or "")
            payload_action = {**ap, "executed": not is_err, "execution_result": exec_note}
            logger.info(
                "tool_call approval %s replayed (%s, ref=%s, error=%s)",
                approval_id, connector, request_ref[:8], is_err,
            )
        elif model and method:
            # v7.106.0 — ENGINE gate path (e.g. wai-odoo): the gate staged NO
            # request_ref, so there is nothing to replay. Instead mint a
            # SINGLE-USE, args-bound grant in the platform (INC-AGO-100) and
            # let the agent RE-EXECUTE the EXACT approved operation through the
            # APPROVAL_GRANTED run: the engine gate will consume the grant and
            # allow that one write. executed stays false so the template tells
            # the agent to carry it out.
            gconn = connector or "wai-odoo"
            from app.runtime_policy_sync import push_write_grant, write_fingerprint
            fpw = write_fingerprint(model, method, op_payload)
            grant_id = await push_write_grant(gconn, fpw, ttl_s=900)
            payload_action = {**ap, "connector": gconn, "executed": False,
                              "write_grant": grant_id}
            logger.info(
                "tool_call approval %s granted for engine replay (%s, fp=%s, grant=%s)",
                approval_id, gconn, fpw[:12], str(grant_id)[:8],
            )
            if not grant_id:
                logger.warning(
                    "tool_call approval %s: write-grant mint failed; the agent's "
                    "re-execution will re-park (safe). Check WAI_BACKEND_URL/"
                    "BACKEND_API_SECRET and migration V60.", approval_id,
                )
        else:
            # Neither a Composio request_ref nor an executable operation → there
            # is genuinely nothing to run. Refuse loudly. The approval stays
            # marked (section 1 already committed) but WITHOUT an event: the reconciler
            # does not pick it up because there is nothing executable — it is a 422 for
            # the caller, not lost work.
            raise HTTPException(
                status_code=422,
                detail="action_payload sin datos ejecutables (ni request_ref ni "
                       "model/method de la operación) — no se puede ejecutar la aprobación.",
            )

    # ── SECTION 3: persist the result and dispatch ─────────────────────────
    payload = {
        "approval_id": approval_id,
        "title": titulo,
        "summary": resumen,
        "action_type": tipo,
        "action_payload": payload_action,
        "requested_for": para_quien,
        "decided_by": user.email,
        "feedback": str(body.get("feedback") or "") or None,
        "trigger": "approval",
    }
    async with app_write() as s:
        a = (await s.execute(
            select(Approval).where(Approval.id == approval_id)
        )).scalar_one_or_none()
        if a is None:  # pragma: no cover — deleted while it was executing
            raise HTTPException(status_code=404, detail="not found")
        if ejecutado_en is not None:
            a.executed_at = ejecutado_en
            a.execution_result = resultado_ejecucion
        event_id = await _dispatch_approval_event(s, approval_id, decidido_en, payload)
        await s.commit()
        result = _serialize(a)
    logger.info("approval %s approved by %s (event=%s)", approval_id, user.email, event_id)
    return {**result, "event_id": int(event_id) if event_id else None}


def _approval_event_fingerprint(approval_id: int, decided_at) -> str:
    """Fingerprint of APPROVAL_GRANTED, derived from DURABLE data.

    It used to come from `now.isoformat()` — the instant the handler ran—, so
    nobody else could recompute it: if the process died between marking approved and
    emitting the event, the approval stayed approved and silent forever.
    Deriving it from `decided_at` (which is in the row) the reconciler reaches the
    SAME fingerprint, and `on_conflict_do_nothing` makes the re-dispatch
    harmless: emitting twice duplicates nothing.
    """
    seed = f"approval::{approval_id}::{decided_at.isoformat() if decided_at else ''}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


async def _dispatch_approval_event(s, approval_id: int, decided_at, payload: dict):
    """Emits the APPROVAL_GRANTED (idempotent by fingerprint) and returns its id."""
    fp = _approval_event_fingerprint(approval_id, decided_at)
    await s.execute(
        sqlite_insert(Event).values(
            fingerprint=fp,
            type=APPROVAL_GRANTED_EVENT_TYPE,
            source=f"approval:{approval_id}",
            payload_json=json.dumps(payload, default=str),
            status="PENDING",
            attempts=0,
            detected_at=(decided_at or now_utc().replace(tzinfo=None)),
        ).on_conflict_do_nothing(index_elements=[Event.fingerprint])
    )
    return (await s.execute(
        select(Event.id).where(Event.fingerprint == fp)
    )).scalar_one_or_none()


async def dispatch_orphan_approvals() -> int:
    """APPROVED approvals whose event never went out. Returns how many.

    It closes the only window that splitting the lock leaves: if the process dies
    between "mark approved" and "emit the event", the operator has already given their
    sign-off and the employee never finds out — the worst possible silence on the
    governance path. NOTE: that window **already existed** before splitting (the Composio
    path committed before calling the CRM); what is new is that now it gets repaired.

    It runs at startup, like `recover_stale_runs`. It is idempotent by the event's
    fingerprint, so running it too often duplicates nothing.
    """
    from app.app_models.runtime import Event as _Event
    reparadas = 0
    try:
        async with app_session() as s:
            pendientes = list((await s.execute(
                select(Approval).where(Approval.status == "approved")
                .order_by(Approval.decided_at.desc()).limit(200)
            )).scalars().all())
            huellas = {
                a.id: _approval_event_fingerprint(a.id, a.decided_at) for a in pendientes
            }
            existentes = set((await s.execute(
                select(_Event.fingerprint).where(_Event.fingerprint.in_(list(huellas.values())))
            )).scalars().all()) if huellas else set()
            huerfanas = [a for a in pendientes if huellas[a.id] not in existentes]
        for a in huerfanas:
            try:
                payload_action = json.loads(a.action_payload_json or "{}")
            except Exception:  # noqa: BLE001
                payload_action = {}
            payload = {
                "approval_id": a.id, "title": a.title, "summary": a.summary_md or "",
                "action_type": a.action_type, "action_payload": payload_action,
                "requested_for": a.requested_for, "decided_by": a.decided_by,
                "feedback": a.feedback, "trigger": "approval",
            }
            async with app_write() as s:
                await _dispatch_approval_event(s, a.id, a.decided_at, payload)
                await s.commit()
            reparadas += 1
        if reparadas:
            logger.warning(
                "approvals: %d aprobación(es) estaban aprobadas SIN evento y se han "
                "despachado ahora (el operador ya había dicho que sí)", reparadas,
            )
    except Exception:  # pragma: no cover — must never prevent startup
        logger.exception("dispatch_orphan_approvals failed")
    return reparadas


@router.post("/{approval_id}/reject")
async def reject(
    approval_id: int,
    body: dict = Body(default_factory=dict),
    user: TokenPayload = Depends(require_approver),
):
    """Reject a parked action; the feedback becomes a learned boundary."""
    feedback = str(body.get("feedback") or "").strip()
    async with app_write() as s:
        a = (await s.execute(
            select(Approval).where(Approval.id == approval_id)
        )).scalar_one_or_none()
        if a is None:
            raise HTTPException(status_code=404, detail="not found")
        if a.status not in ("pending",):
            raise HTTPException(
                status_code=409,
                detail=f"approval already {a.status}",
            )
        now = now_utc()
        a.status = "rejected"
        a.decided_by = user.email
        a.decided_at = now.replace(tzinfo=None)
        a.feedback = feedback or None

        # Teach the employee: store the boundary as a pinned preference on
        # the affected user's memory so future runs respect it.
        target = (a.requested_for or "").strip().lower()
        if target and feedback:
            from app.agent_locale import resolve_agent_language, rotulo
            content = rotulo(await resolve_agent_language(), "rechazo",
                             titulo=a.title, tipo=a.action_type, motivo=feedback)
            await s.execute(
                sqlite_insert(UserMemory).values(
                    idempotency_key=f"approval-reject-{uuid.uuid4().hex}",
                    user_email=target,
                    kind="preference",
                    content=content,
                    pinned=True,
                    source="operator",
                    provenance=f"approval:{a.id}",
                ).on_conflict_do_nothing(index_elements=[UserMemory.idempotency_key])
            )
        await s.commit()
        result = _serialize(a)
    logger.info("approval %s rejected by %s", approval_id, user.email)
    return result


@router.delete("/{approval_id}")
async def delete_approval(
    approval_id: int,
    user: TokenPayload = Depends(require_admin),
):
    """Delete an approval record. The operator loses this request: a PENDING one
    is effectively DISCARDED (its parked action will not run — approve it instead
    if you want it done); a decided one is removed from the history. Best-effort;
    404 when it's already gone."""
    async with app_write() as s:
        a = (await s.execute(select(Approval).where(Approval.id == approval_id))).scalar_one_or_none()
        if a is None:
            raise HTTPException(status_code=404, detail="not found")
        await s.execute(sql_delete(Approval).where(Approval.id == approval_id))
        # Its notice goes with it: the id is reused by SQLite and the next approval would inherit «already notified».
        from app.app_models.push_devices import ApprovalNotice
        await s.execute(sql_delete(ApprovalNotice).where(ApprovalNotice.approval_id == approval_id))
        await s.commit()
    logger.info("approval %s deleted by %s", approval_id, user.email)
    return {"deleted": approval_id}
