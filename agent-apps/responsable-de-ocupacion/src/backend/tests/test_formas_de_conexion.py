"""Connecting a Composio connector with a method that is not the main one (12.59.0).

Google Maps connects through Composio's app (a quota SHARED with all their customers, which
ran out on 24/09/2026 without anyone here having done anything) or with an API key from
the customer's Google Cloud project. The employee's connectors screen only knew the
first. This tests what decides what it connects with and on whose behalf:

* choosing the key goes through the PLATFORM, with THIS connector's identity, and does not
  open the provider's window;
* going back to the account stops using the key BEFORE opening the window (with the key in
  place, the window cannot open); but if it is unknown which kind the own configuration is,
  it is NOT touched: a working own OAuth app is not removed over an "I don't know".
"""
from __future__ import annotations

import asyncio

import pytest

from app import mcp_connectors as mc
from app.routes import mcp_connectors_routes as rutas

CONECTOR = {"name": "composio-google-maps", "toolkit": "google_maps",
            "display_name": "Google Maps", "mcp_server_id": 7}

INFO_GOOGLE_MAPS = {
    "auth_scheme": "OAUTH2", "connection_mode": "redirect",
    "formas": [
        {"auth_scheme": "OAUTH2", "connection_mode": "redirect", "connection_fields": []},
        {"auth_scheme": "API_KEY", "connection_mode": "credentials",
         "connection_fields": [{"name": "generic_api_key", "label": "API Key",
                                "required": True, "secret": True}]},
    ],
}


@pytest.fixture
def entorno(monkeypatch):
    llamadas: dict = {"quitadas": []}
    monkeypatch.setattr(rutas, "_require_known_connector", lambda n: dict(CONECTOR))
    monkeypatch.setattr(rutas, "AUTONOMOUS_USER_EMAIL", "admin@wai.com")
    monkeypatch.setattr(mc, "is_enabled", lambda: True)

    async def auth_info(tk):
        return dict(INFO_GOOGLE_MAPS)

    async def conectar(server_id, forma, credenciales, identidad):
        llamadas["conectar"] = (server_id, forma, credenciales, identidad)
        return {"status": "ACTIVE", "forma": forma, "connected_account_id": "ca_1"}

    async def quitar(server_id):
        llamadas["quitadas"].append(server_id)

    async def apps(fresco=False):
        return llamadas.get("apps", {})

    async def forma_de(acid, identidad=""):
        return llamadas.get("forma_propia", "")

    monkeypatch.setattr(mc, "auth_info", auth_info)
    monkeypatch.setattr(mc, "conectar_con_forma", conectar)
    monkeypatch.setattr(mc, "quitar_config_propia", quitar)
    monkeypatch.setattr(mc, "apps_propias", apps)
    monkeypatch.setattr(mc, "forma_de_config", forma_de)
    monkeypatch.setattr(mc, "oauth_start_url",
                        lambda *a, **k: llamadas.setdefault("oauth", k) and "https://oauth")
    return llamadas


def _conectar(cuerpo):
    return asyncio.run(rutas.connect_mcp_connector("composio-google-maps", cuerpo, None))


def test_con_la_clave_va_por_la_plataforma_y_no_abre_ventana(entorno):
    r = _conectar({"forma": "api_key", "credentials": {"generic_api_key": "AIza"}})
    server_id, forma, cred, identidad = entorno["conectar"]
    assert (server_id, forma, cred) == (7, "API_KEY", {"generic_api_key": "AIza"})
    assert identidad.endswith("#composio-google-maps"), identidad
    assert r["mode"] == "credentials" and r["connected"] is True
    assert r["credencial_comprobada"] is False
    assert "oauth" not in entorno


def test_la_clave_sin_credenciales_se_rechaza(entorno):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _conectar({"forma": "API_KEY"})
    assert e.value.status_code == 422 and "conectar" not in entorno


def test_la_forma_principal_sigue_por_la_ventana(entorno):
    r = _conectar({"forma": "OAUTH2"})
    assert r["mode"] == "oauth" and "conectar" not in entorno


def test_volver_a_la_cuenta_deja_de_usar_la_clave_antes(entorno):
    entorno["apps"] = {"7": "ac_clave"}
    entorno["forma_propia"] = "API_KEY"
    r = _conectar({})
    assert entorno["quitadas"] == [7]
    assert r["mode"] == "oauth"


def test_una_app_oauth_propia_no_se_quita(entorno):
    entorno["apps"] = {"7": "ac_app"}
    entorno["forma_propia"] = "OAUTH2"
    _conectar({})
    assert entorno["quitadas"] == []


def test_si_no_se_sabe_la_forma_no_se_toca(entorno):
    # An auth-service without that query returns "": that is not "it is a key".
    entorno["apps"] = {"7": "ac_desconocida"}
    entorno["forma_propia"] = ""
    _conectar({})
    assert entorno["quitadas"] == []


def test_conectar_con_forma_manda_lo_que_la_plataforma_comprueba(monkeypatch):
    import app.config as cfg
    enviado = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"status": "ACTIVE", "forma": "API_KEY"}

    class _Cliente:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            enviado.update(url=url, json=json, headers=headers)
            return _Resp()

    refrescos = []

    async def apps(fresco=False):
        refrescos.append(fresco)
        return {}

    monkeypatch.setattr(cfg, "AGENT_ID", "1826")
    monkeypatch.setattr(cfg, "BACKEND_API_SECRET", "s")
    monkeypatch.setattr(cfg, "WAI_BACKEND_URL", "http://backend")
    monkeypatch.setattr(mc.httpx, "AsyncClient", _Cliente)
    monkeypatch.setattr(mc, "apps_propias", apps)
    asyncio.run(mc.conectar_con_forma(7, "api_key", {"generic_api_key": "AIza"},
                                      "demo:admin@wai.com+a1826#composio-google-maps"))
    assert enviado["url"] == "http://backend/backend/mcp-servers/7/composio-connect"
    assert enviado["json"] == {"forma": "API_KEY", "credentials": {"generic_api_key": "AIza"},
                               "agent_app_id": 1826,
                               "identity": "demo:admin@wai.com+a1826#composio-google-maps"}
    assert enviado["headers"] == {"X-API-Secret": "s"}
    # The agent's gate has to see the new configuration NOW, not in 2 minutes.
    assert refrescos == [True]


def test_la_lista_de_la_pantalla_trae_el_id_de_la_ficha(monkeypatch):
    """With the REAL function, not a fake: the screen's one is a copy of the runtime's and
    it left out the catalog id; without it, "With API key" answered "not in the
    catalog" (12.59.0, seen in the travel assistant)."""
    import app.mcp_runtime as rt
    monkeypatch.setattr(rt, "fichas_de_conectores", lambda fresco=False: {
        "composio-google-maps": {"id": 337, "name": "composio-google-maps",
                                 "auth_type": "composio", "composio_toolkit": "google_maps"}})
    monkeypatch.setattr(rutas, "_manifest_mcps", lambda: [{"name": "composio-google-maps"}])
    conns = rutas._composio_connectors()
    assert conns and conns[0]["mcp_server_id"] == 337
