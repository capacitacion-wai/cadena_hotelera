"""Connecting a custom connector to the employee without having a key at hand (12.59.3).

A custom connector's key belongs to EACH owner. On the platform, «Conectar» issues it by itself;
in the employee you first had to go to the connector's page, issue it and paste it — a step that
nobody knew about (Merkalia, 25/09/2026). Now, with no key, it asks the platform for one on behalf
of THIS employee, and waits as long as the connector takes to recognise it.
"""
from __future__ import annotations

import asyncio

from app.routes import mcp_connectors_routes as rutas

CONECTOR = {"name": "supabase-datos-tecnico", "wai_mcp_server_id": 9}


class _Respuesta:
    status_code = 200

    def json(self):
        return {"connected": True}


def _capturar(monkeypatch):
    visto: dict = {}

    class _Cliente:
        def __init__(self, timeout=None, **_):
            visto["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            visto["url"], visto["json"] = url, json
            return _Respuesta()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Cliente)
    import app.config as cfg
    monkeypatch.setattr(cfg, "WAI_BACKEND_URL", "http://backend", raising=False)
    monkeypatch.setattr(cfg, "BACKEND_API_SECRET", "s", raising=False)
    monkeypatch.setattr(cfg, "AGENT_ID", "42", raising=False)
    import app.mcp_runtime as rt
    monkeypatch.setattr(rt, "olvidar_fichas", lambda: None)
    return visto


def test_sin_llave_la_emite_la_plataforma_para_este_empleado(monkeypatch):
    visto = _capturar(monkeypatch)
    r = asyncio.run(rutas._conectar_conector_propio(dict(CONECTOR), {}))
    assert r["status"] == "connected"
    assert visto["json"] == {"access_key": "", "agent_app_id": 42}
    assert visto["timeout"] >= 120          # issuing restarts the connector: we have to wait for it


def test_con_llave_se_guarda_la_que_se_pega(monkeypatch):
    visto = _capturar(monkeypatch)
    asyncio.run(rutas._conectar_conector_propio(dict(CONECTOR), {"credentials": {"access_key": "k1"}}))
    assert visto["json"]["access_key"] == "k1" and visto["timeout"] == 45.0
