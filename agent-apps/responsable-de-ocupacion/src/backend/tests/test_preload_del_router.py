"""The tools an employee declares travel PRELOADED in its connector's router session (14.24.0).

Measured on 12/10/2026 in the travel assistant: 30 % of the time of its answers went on
`SEARCH_TOOLS` + `GET_TOOL_SCHEMAS` before using tools its own definition names. Composio's router
can expose them DIRECT in the same session (`preload`). Pinned here: which slugs come out of the
definition (and only those), that a change in them re-creates the cached session, and that the
request carries them only when there are any (an employee with nothing declared sends the usual one).
"""
from __future__ import annotations

import pytest

from app import mcp_connectors as mc
from app import mcp_runtime as R

DEF = {
    "hago": [
        {"id": "ruta", "apoyo": ["composio-google-maps_GOOGLE_MAPS_TEXT_SEARCH",
                                 {"herramienta": "composio-google-maps_GOOGLE_MAPS_DIRECTIONS", "para": "x"},
                                 "wai-viajes_listar_viajes"],
         "resuelve_con": {"herramienta": "composio-google-maps_GOOGLE_MAPS_TEXT_SEARCH"},
         "ruta": {"rondas": [{"llamadas": [{"herramienta": "composio-google-maps_GOOGLE_MAPS_PLACE_DETAILS"}]},
                             ["composio-weathermap_WEATHERMAP_WEATHER"]]}},
        {"id": "meta", "apoyo": ["composio-google-maps_COMPOSIO_SEARCH_TOOLS"]},
        "no es una capacidad",
    ],
    "contexto_de_turno": [{"herramienta": "composio-weathermap_WEATHERMAP_WEATHER"}],
}


@pytest.fixture
def definicion(monkeypatch):
    import app.conversacion as conv
    monkeypatch.setattr(conv, "declaracion", lambda: DEF)


def test_solo_lo_declarado_de_ese_conector_y_sin_repetir(definicion):
    assert R.herramientas_declaradas("composio-google-maps") == [
        "GOOGLE_MAPS_TEXT_SEARCH", "GOOGLE_MAPS_DIRECTIONS", "GOOGLE_MAPS_PLACE_DETAILS"]
    assert R.herramientas_declaradas("composio-weathermap") == ["WEATHERMAP_WEATHER"]
    # Neither the meta-tools, nor what belongs to another connector, nor what is not declared.
    assert R.herramientas_declaradas("composio-kommo") == []


def test_sin_definicion_no_se_precarga_nada(monkeypatch):
    import app.conversacion as conv
    monkeypatch.setattr(conv, "declaracion", lambda: {})
    assert R.herramientas_declaradas("composio-google-maps") == []


def test_cambiar_lo_precargado_rehace_la_sesion():
    base = ("demo:a@b.c#maps", "google_maps", {}, False, "ca_1", "ac_1")
    assert R._router_session_key(*base, ["A_X"]) != R._router_session_key(*base, ["A_X", "A_Y"])
    assert R._router_session_key(*base, ["A_Y", "A_X"]) == R._router_session_key(*base, ["A_X", "A_Y"])


@pytest.mark.asyncio
async def test_la_peticion_lleva_preload_solo_si_hay(monkeypatch):
    enviados = []

    async def post(path, payload, timeout=0):
        enviados.append(payload)
        return {"url": "https://composio.test/mcp/s"}
    monkeypatch.setattr(mc, "_post", post)
    monkeypatch.setattr(mc, "composio_identity", lambda e, c="": f"demo:{e}#{c}", raising=False)
    await mc.create_tool_router_session("a@b.c", "composio-google-maps", ["google_maps"],
                                        preload=["GOOGLE_MAPS_TEXT_SEARCH"])
    await mc.create_tool_router_session("a@b.c", "composio-google-maps", ["google_maps"])
    assert enviados[0]["preload"] == ["GOOGLE_MAPS_TEXT_SEARCH"]
    assert "preload" not in enviados[1]
