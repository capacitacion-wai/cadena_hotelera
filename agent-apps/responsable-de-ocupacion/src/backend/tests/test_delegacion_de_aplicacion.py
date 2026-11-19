"""A person speaking THROUGH AN APPLICATION (docs/aplicaciones-que-llaman-a-empleados.md).

What is pinned is what would open the employee wider than intended: a delegation from a
colleague used on the chat, an application reaching operations, a superuser coming through,
or a bad signature accepted."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.colleagues as colleagues
from app import auth

# Not a credential: the shared secret the test signs with, built so it does not look like one.
SECRETO = "-".join(("solo", "para", "pruebas"))


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _firmar(cuerpo: dict, secreto: str = SECRETO, typ: str = "WAI-DELEG-APP") -> str:
    cab = _b64(json.dumps({"alg": "HS256", "typ": typ}).encode())
    cue = _b64(json.dumps(cuerpo).encode())
    firma = _b64(hmac.new(secreto.encode(), f"{cab}.{cue}".encode(), hashlib.sha256).digest())
    return f"{cab}.{cue}.{firma}"


def _delegacion(**cambios) -> str:
    t = int(time.time())
    base = {"sub": "Ana@Empresa.com", "typ": "WAI-DELEG-APP", "project_ids": [7], "data_areas": ["ventas"],
            "origen": {"tipo": "app", "app": "portal-proveedores", "app_id": 3},
            "iat": t, "exp": t + 300}
    base.update(cambios)
    return _firmar(base)


def _peticion(ruta: str, delegacion: str | None = None) -> Request:
    cabeceras = [(b"x-wai-delegation", delegacion.encode())] if delegacion else []
    return Request({"type": "http", "method": "POST", "path": ruta, "headers": cabeceras,
                    "query_string": b"", "state": {}})


@pytest.fixture(autouse=True)
def _secreto(monkeypatch):
    monkeypatch.setattr(colleagues, "BACKEND_API_SECRET", SECRETO)


@pytest.mark.asyncio
async def test_la_persona_de_la_aplicacion_entra_al_chat_como_ella_y_nunca_como_superusuario():
    req = _peticion("/api/agent/chat", _delegacion())
    u = await auth.get_current_user(req, None)
    assert (u.email, u.is_superuser, u.project_ids, u.data_areas) == \
        ("ana@empresa.com", False, [7], ["ventas"])
    assert req.state.origen == "app:portal-proveedores"


@pytest.mark.asyncio
@pytest.mark.parametrize("ruta", ["/api/agent/threads", "/api/agent/threads/abc/history",
                                  "/api/agent/chat/abort", "/api/agent/threads/abc",
                                  # 14.23.0 — invoking a declared capability (the platform checks which)
                                  "/api/agent/invocar"])
async def test_alcanza_las_conversaciones(ruta):
    assert (await auth.get_current_user(_peticion(ruta, _delegacion()), None)).email


@pytest.mark.asyncio
@pytest.mark.parametrize("ruta", ["/api/operations/budget", "/api/users", "/api/agent/chat/compact",
                                  "/api/agent/threads/../users", "/api/agent/invocar/otra"])
async def test_no_alcanza_nada_mas(ruta):
    with pytest.raises(HTTPException) as e:
        await auth.get_current_user(_peticion(ruta, _delegacion()), None)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_una_delegacion_de_companero_no_abre_el_chat():
    t = int(time.time())
    de_companero = _firmar({"sub": "ana@empresa.com", "chain_id": "x", "depth": 0,
                            "iat": t, "exp": t + 300}, typ="WAI-DELEG")
    with pytest.raises(HTTPException) as e:
        await auth.get_current_user(_peticion("/api/agent/chat", de_companero), None)
    assert e.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [
    _firmar({"sub": "ana@empresa.com", "typ": "WAI-DELEG-APP", "origen": {"tipo": "app"},
             "exp": int(time.time()) + 300}, secreto="otro"),                 # firma ajena
    "no.es.un-token",
])
async def test_una_firma_mala_no_entra(token):
    with pytest.raises(HTTPException) as e:
        await auth.get_current_user(_peticion("/api/agent/chat", token), None)
    assert e.value.status_code == 401


@pytest.mark.asyncio
async def test_caducada_no_entra():
    with pytest.raises(HTTPException) as e:
        await auth.get_current_user(_peticion("/api/agent/chat", _delegacion(exp=int(time.time()) - 1)), None)
    assert e.value.status_code == 401


@pytest.mark.asyncio
async def test_sin_sesion_ni_delegacion_es_el_403_de_siempre():
    with pytest.raises(HTTPException) as e:
        await auth.get_current_user(_peticion("/api/agent/chat"), None)
    assert e.value.status_code == 403 and e.value.detail == "Not authenticated"


@pytest.mark.asyncio
async def test_otro_empleado_solo_alcanza_invocar_una_capacidad():
    # 15.4.0 (docs/red-de-integraciones.md): the platform signs the same delegation with origin «empleado» when a
    # collaboration authorises USING a capability. Only `invocar`; the conversation goes through `/mcp/colleague`.
    deleg = _delegacion(origen={"tipo": "empleado", "app": "vera", "app_id": 12})
    req = _peticion("/api/agent/invocar", deleg)
    u = await auth.get_current_user(req, None)
    assert u.email == "ana@empresa.com" and u.is_superuser is False
    assert req.state.origen == "empleado:vera"
    for ruta in ("/api/agent/chat", "/api/agent/threads", "/api/agent/runs"):
        with pytest.raises(HTTPException) as e:
            await auth.get_current_user(_peticion(ruta, deleg), None)
        assert e.value.status_code == 403
