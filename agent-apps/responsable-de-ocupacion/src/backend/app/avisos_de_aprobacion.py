"""14.14.0 — NOTIFY THE PHONES of whoever may decide that something is waiting for approval.

An approval is born without anybody waiting for it —an automation at night, a connector write
the employee parked— and, until now, nobody found out until they opened the panel. This loop
notices every NEW pending approval and sends one notice to each registered phone of the people
who may decide (`users_directory.list_approvers`: admins + the people with «can approve»).

Why a loop and not a call where each approval is created: approvals are created in several
places (the agent's `request_approval`, automations, the check-before-approve settle) and a
new one tomorrow would be one more place to remember. The loop looks at the TABLE, which is
where all of them end up; ~15 s of delay is nothing for a decision that waits for a person.

* **Once per approval** (`ApprovalNotice`), whatever restarts in between. On the very first run
  (no notices recorded yet) everything already pending is marked WITHOUT sending: an employee
  that updates does not wake everyone up with its backlog.
* **The text is generic**: the notice is read on the LOCKED screen, in view of anyone. Never
  the title or what it wants to do — only that there is something to decide, in the language
  of the phone (`PushDevice.idioma`).
* **What the phone needs to open it**: the approval id and this employee's public host
  (`APP_PUBLIC_BASE_URL`), which is how the mobile app knows its employees.
* A phone Expo reports as no longer registered is forgotten.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from urllib.parse import urlparse

from sqlalchemy import delete, exists, or_, select

from app.app_database import app_session, app_write
from app.app_models.business import Approval
from app.app_models.push_devices import ApprovalNotice, PushDevice
from app.timeutil import now_utc

logger = logging.getLogger(__name__)

INTERVALO_S = 15.0
#: Older pending approvals are not notified (a long downtime does not end in a burst).
VENTANA = timedelta(hours=24)

_TEXTOS = {
    "es": "Tienes algo pendiente de aprobar. Toca para revisarlo.",
    "en": "Something is waiting for your approval. Tap to review it.",
}


def texto_para(idioma: str) -> str:
    return _TEXTOS.get((idioma or "").split("-")[0].lower(), _TEXTOS["es"])


def host_publico() -> str:
    from app.config import APP_PUBLIC_BASE_URL
    return urlparse(APP_PUBLIC_BASE_URL or "").netloc


async def _linea_base() -> None:
    """First run ever: what is already pending counts as notified (without sending)."""
    async with app_session() as s:
        hay = (await s.execute(select(ApprovalNotice.approval_id).limit(1))).first()
        if hay:
            return
        ids = list((await s.execute(
            select(Approval.id).where(Approval.status == "pending"))).scalars())
    async with app_write() as s:
        for i in ids:
            await s.merge(ApprovalNotice(approval_id=i, recipients=0))
        await s.commit()


async def revisar() -> int:
    """One pass: notify the new pending approvals. Returns how many approvals it handled."""
    from app.aviso_push import enviar
    from app.aviso_push import _nombre_del_empleado
    from app.users_directory import list_approvers

    # Naive UTC, like `Approval.created_at` (its column default is `datetime.utcnow`).
    desde = now_utc().replace(tzinfo=None) - VENTANA
    # A notice whose approval no longer exists goes first: SQLite REUSES the id of a deleted row (the table has no
    # AUTOINCREMENT), and a new approval with a recycled id «had already been notified» — so it never was (AGO-184,
    # 19/10/2026: the approval said 0 phones notified, and nobody got it).
    # A notice OLDER than the approval that now has its id belongs to the deleted one, even if the new one was created
    # before this pass runs.
    otra = select(Approval.id).where(Approval.id == ApprovalNotice.approval_id,
                                     Approval.created_at > ApprovalNotice.noticed_at)
    async with app_write() as s:
        await s.execute(delete(ApprovalNotice).where(or_(ApprovalNotice.approval_id.not_in(select(Approval.id)),
                                                         exists(otra))))
        await s.commit()
    async with app_session() as s:
        avisadas = select(ApprovalNotice.approval_id)
        nuevas = list((await s.execute(
            select(Approval.id).where(Approval.status == "pending", Approval.created_at >= desde,
                                      Approval.id.not_in(avisadas)).order_by(Approval.id)
        )).scalars())
        if not nuevas:
            return 0
        quienes = {a["email"] for a in await list_approvers()}
        telefonos = list((await s.execute(
            select(PushDevice.token, PushDevice.idioma).where(PushDevice.email.in_(quienes))
        )).all()) if quienes else []

    # Recorded BEFORE sending: at most once. A crash in between loses a notice, never sends two.
    async with app_write() as s:
        for i in nuevas:
            await s.merge(ApprovalNotice(approval_id=i, recipients=len(telefonos)))
        await s.commit()
    if not telefonos:
        return len(nuevas)

    titulo, host = _nombre_del_empleado() or "WAI", host_publico()
    mensajes = [{"to": token, "title": titulo, "body": texto_para(idioma), "sound": "default",
                 "priority": "high",
                 "data": {"que": "aprobacion", "id": i, "empleado": host}}
                for i in nuevas for token, idioma in telefonos]
    tickets = await enviar(mensajes)
    olvidar = {m["to"] for m, t in zip(mensajes, tickets)
               if (t.get("details") or {}).get("error") == "DeviceNotRegistered"}
    if olvidar:
        async with app_write() as s:
            await s.execute(delete(PushDevice).where(PushDevice.token.in_(olvidar)))
            await s.commit()
    logger.info("approval notices: %d approval(s), %d phone(s), %d forgotten",
                len(nuevas), len(telefonos), len(olvidar))
    return len(nuevas)


async def run_forever(intervalo_s: float = INTERVALO_S) -> None:
    try:
        await _linea_base()
    except Exception:  # noqa: BLE001 — without the baseline it tries again on the next pass
        logger.warning("approval notices: baseline failed", exc_info=True)
    while True:
        try:
            await revisar()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a notice never breaks the employee
            logger.warning("approval notices: pass failed", exc_info=True)
        await asyncio.sleep(intervalo_s)
