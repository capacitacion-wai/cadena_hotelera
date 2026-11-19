"""Approval notices to the phones (14.14.0).

What is pinned here is what no screen shows until it fails in someone's pocket:

  · a new pending approval reaches the phones of whoever may DECIDE, and only theirs;
  · it is notified once, whatever passes come after;
  · on the first run, the backlog already pending is not sent;
  · the text is generic (locked screen) and in the phone's language;
  · a phone Expo no longer knows is forgotten;
  · registering a phone: only Expo tokens, and the phone belongs to whoever signed in last.
"""
from __future__ import annotations

import asyncio

import pytest

from app import avisos_de_aprobacion as av  # noqa: F401 — registers push_devices on the metadata

TOKEN_JEFA = "ExponentPushToken[jefaaaaaaaaaaaaaaaa1]"
TOKEN_MARIA = "ExponentPushToken[mariaaaaaaaaaaaaaaa2]"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def gente(monkeypatch):
    """An admin (decides) and a member (does not), each with a phone."""
    monkeypatch.setenv("AGENT_AUTONOMOUS_USER_EMAIL", "empleado@wai.com")
    from app.app_database import app_write
    from app.app_models.push_devices import PushDevice
    from app.users_directory import upsert_directory_user

    async def go():
        await upsert_directory_user(email="jefa@acme.com", role="admin", source="manual",
                                    created_by="x", allow_role=True)
        await upsert_directory_user(email="maria@acme.com", role="member", source="manual",
                                    created_by="x", allow_role=True)
        async with app_write() as s:
            s.add(PushDevice(token=TOKEN_JEFA, email="jefa@acme.com", idioma="en"))
            s.add(PushDevice(token=TOKEN_MARIA, email="maria@acme.com", idioma="es"))
            await s.commit()

    _run(go())


def _aprobacion(clave: str, status: str = "pending") -> int:
    from app.app_database import app_write
    from app.app_models.business import Approval

    async def go():
        async with app_write() as s:
            a = Approval(idempotency_key=clave, title="Create Red Quay in HubSpot",
                         summary_md="secret details", action_type="tool_call", status=status)
            s.add(a)
            await s.commit()
            return a.id

    return _run(go())


@pytest.fixture()
def expo(monkeypatch):
    enviados: list[list[dict]] = []
    respuesta: dict = {"tickets": None}

    async def enviar(mensajes):
        enviados.append(mensajes)
        return respuesta["tickets"] or [{"status": "ok"} for _ in mensajes]

    monkeypatch.setattr("app.aviso_push.enviar", enviar)
    monkeypatch.setattr("app.aviso_push._nombre_del_empleado", lambda: "Prospecting Analyst")
    monkeypatch.setattr(av, "host_publico", lambda: "prospecting-analyst-demo.usewai.com")
    return enviados, respuesta


def test_avisa_solo_a_quien_puede_decidir_y_una_vez(gente, expo):
    enviados, _ = expo
    _run(av._linea_base())
    aid = _aprobacion("k1")
    assert _run(av.revisar()) == 1
    (mensajes,) = enviados
    assert [m["to"] for m in mensajes] == [TOKEN_JEFA], "a member does not decide"
    m = mensajes[0]
    assert m["data"] == {"que": "aprobacion", "id": aid, "empleado": "prospecting-analyst-demo.usewai.com"}
    assert m["title"] == "Prospecting Analyst"
    assert m["body"] == av.texto_para("en"), "the phone's language"
    assert "Red Quay" not in m["body"] and "secret" not in m["body"], "locked screen"
    assert _run(av.revisar()) == 0 and len(enviados) == 1, "once per approval"


def test_lo_que_ya_estaba_pendiente_no_se_manda(gente, expo):
    enviados, _ = expo
    _aprobacion("viejo")
    _run(av._linea_base())
    assert _run(av.revisar()) == 0 and enviados == []
    _aprobacion("nuevo")
    assert _run(av.revisar()) == 1 and len(enviados) == 1


def test_lo_ya_decidido_no_se_avisa(gente, expo):
    enviados, _ = expo
    _run(av._linea_base())
    _aprobacion("hecha", status="approved")
    assert _run(av.revisar()) == 0 and enviados == []


def test_un_telefono_que_expo_ya_no_conoce_se_olvida(gente, expo):
    from sqlalchemy import select
    from app.app_database import app_session
    from app.app_models.push_devices import PushDevice
    _, respuesta = expo
    respuesta["tickets"] = [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]
    _run(av._linea_base())
    _aprobacion("k2")
    _run(av.revisar())

    async def tokens():
        async with app_session() as s:
            return set((await s.execute(select(PushDevice.token))).scalars())

    assert _run(tokens()) == {TOKEN_MARIA}


def test_textos():
    assert av.texto_para("es").startswith("Tienes algo pendiente")
    assert av.texto_para("en-GB").startswith("Something is waiting")
    assert av.texto_para("fr") == av.texto_para("es")


def test_registrar_un_telefono():
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.app_database import app_session
    from app.app_models.push_devices import PushDevice
    from app.auth import TokenPayload
    from app.routes import devices_routes as R

    def quien(email):
        return TokenPayload(email=email, is_superuser=False, project_ids=[], data_areas=[])

    with pytest.raises(HTTPException):
        _run(R.registrar(R.Dispositivo(token="https://evil.example.com"), quien("ana@acme.com")))
    _run(R.registrar(R.Dispositivo(token=TOKEN_JEFA, idioma="en-US"), quien("ana@acme.com")))
    _run(R.registrar(R.Dispositivo(token=TOKEN_JEFA, idioma="es"), quien("luis@acme.com")))

    async def filas():
        async with app_session() as s:
            return [(d.email, d.idioma) for d in (await s.execute(select(PushDevice))).scalars()]

    assert _run(filas()) == [("luis@acme.com", "es")], "the phone moves to whoever signed in"
    assert [d["token"] for d in _run(R.mios(quien("luis@acme.com")))["devices"]] == [TOKEN_JEFA]
    assert _run(R.mios(quien("ana@acme.com")))["devices"] == [], "only your own phones"
    _run(R.quitar(TOKEN_JEFA, quien("ana@acme.com")))
    assert _run(filas()) == [("luis@acme.com", "es")], "somebody else cannot remove it"
    _run(R.quitar(TOKEN_JEFA, quien("luis@acme.com")))
    assert _run(filas()) == []


def test_se_puede_consultar_si_una_aprobacion_se_aviso(gente, expo):
    from app.routes import devices_routes as R
    _run(av._linea_base())
    aid = _aprobacion("k3")
    assert _run(R.aviso_de(aid, None))["notice"] is None
    _run(av.revisar())
    assert _run(R.aviso_de(aid, None))["notice"]["recipients"] == 1


def test_una_aprobacion_con_el_id_de_una_borrada_tambien_se_avisa(gente, expo):
    """AGO-184 (19/10/2026): SQLite reuses the id of a deleted approval; its old notice made the new one «already
    notified» and nobody was told."""
    from sqlalchemy import delete
    from app.app_database import app_write
    from app.app_models.business import Approval
    enviados, _ = expo
    _run(av._linea_base())
    aid = _aprobacion("primera")
    assert _run(av.revisar()) == 1

    async def borrar():
        async with app_write() as s:
            await s.execute(delete(Approval).where(Approval.id == aid))
            await s.commit()
    _run(borrar())
    otra = _aprobacion("segunda")
    assert otra == aid, "SQLite reuses the id (the case this guards)"
    assert _run(av.revisar()) == 1 and len(enviados) == 2, "the new approval is notified"
