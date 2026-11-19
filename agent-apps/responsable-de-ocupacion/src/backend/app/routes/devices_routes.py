"""14.14.0 — the PHONES that receive this employee's notices (`avisos_de_aprobacion.py`).

The mobile app registers its token with every employee it lists (`PUT`) and removes it when the
person signs out (`DELETE`). A token belongs to ONE person: if another person signs in on the
same phone, the row moves to them — the previous person must not keep receiving notices on a
phone that is no longer theirs.

Anyone with access to this employee may register a phone; who RECEIVES approval notices is
decided when sending (only the people who may decide), not here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.app_database import app_session, app_write
from app.app_models.push_devices import ApprovalNotice, PushDevice
from app.auth import TokenPayload, require_admin, require_project_access
from app.aviso_push import token_valido
from app.timeutil import now_utc

router = APIRouter(prefix="/api/agent/devices", tags=["devices"])

IDIOMAS = ("es", "en")


class Dispositivo(BaseModel):
    token: str
    idioma: str = "es"


@router.put("")
async def registrar(body: Dispositivo, user: TokenPayload = Depends(require_project_access)):
    token = token_valido(body.token)
    if not token:
        raise HTTPException(status_code=422, detail="token must be an Expo push token")
    idioma = (body.idioma or "").split("-")[0].lower()
    idioma = idioma if idioma in IDIOMAS else "es"
    email = (user.email or "").strip().lower()
    async with app_write() as s:
        fila = (await s.execute(select(PushDevice).where(PushDevice.token == token))).scalars().first()
        if fila is None:
            s.add(PushDevice(token=token, email=email, idioma=idioma))
        else:
            fila.email, fila.idioma, fila.last_seen_at = email, idioma, now_utc().replace(tzinfo=None)
        await s.commit()
    return {"ok": True}


@router.delete("")
async def quitar(token: str, user: TokenPayload = Depends(require_project_access)):
    """`?token=…` (a query parameter: a DELETE with a body is dropped by many clients)."""
    email = (user.email or "").strip().lower()
    async with app_write() as s:
        await s.execute(delete(PushDevice).where(PushDevice.token == token.strip(),
                                                 PushDevice.email == email))
        await s.commit()
    return {"ok": True}


@router.get("")
async def mios(user: TokenPayload = Depends(require_project_access)):
    """The phones registered for the signed-in person (never anyone else's)."""
    email = (user.email or "").strip().lower()
    async with app_session() as s:
        filas = (await s.execute(select(PushDevice).where(PushDevice.email == email))).scalars()
        return {"devices": [{"token": d.token, "idioma": d.idioma,
                             "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None}
                            for d in filas]}


@router.get("/notices/{approval_id}")
async def aviso_de(approval_id: int, _: TokenPayload = Depends(require_admin)):
    """Was this approval notified, and to how many phones? (``null`` = not yet). For whoever
    governs the employee —and the validation that proves the notices go out—."""
    async with app_session() as s:
        n = (await s.execute(select(ApprovalNotice).where(
            ApprovalNotice.approval_id == approval_id))).scalars().first()
    return {"approval_id": approval_id,
            "notice": None if n is None else {"at": n.noticed_at.isoformat(), "recipients": n.recipients}}
