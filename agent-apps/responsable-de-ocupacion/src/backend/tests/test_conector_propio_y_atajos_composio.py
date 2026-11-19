"""Three small lies seen on 24/09/2026 in the travel assistant (12.57.0).

1. Capabilities reported WAI Viajes, a working OWN connector, as "not available": it only
   looked at the tools cache and not at the catalog the connector registered when published.
2. The shortcut `llévame al Prado` called a Composio tool by its name (in the router only
   its meta-tools exist) and, when the response carried `isError`, it wrote it up as if it
   were the data: `No se ha podido encontrar el Museo del Prado`.
3. The reception sent `¿cómo llego andando…?` to a shortcut the assistant does not have.
"""
from __future__ import annotations

import asyncio
import json

from app import atajos, capabilities as caps, recepcion


# ── 1. Capabilities and the own connector's catalog ─────────────────────────────────────

def test_un_conector_propio_con_catalogo_no_sale_no_disponible(monkeypatch):
    import app.mcp_runtime as rt
    from app import mcp_policy

    monkeypatch.setattr(rt, "composio_connectors", lambda: [])
    monkeypatch.setattr(rt, "custom_connectors", lambda fresco=False: [{
        "name": "wai-viajes", "toolkit": "wai-viajes", "display_name": "WAI Viajes",
        "tools_catalogo": [{"name": "consultar_viaje"}, {"name": "gestionar_viaje"}]}])

    async def cache_vacia():
        return {}

    async def tabla(**_k):
        # A table that does not list the connector yet: its catalogue still enumerates it,
        # judged by the table's row for what it does not name.
        return {"connectors": [], "unlisted_connector": {"permission": "ask"}}

    async def sin_autonomia():
        return mcp_policy.POLICY_WRITE

    monkeypatch.setattr(caps, "_tools_cache", cache_vacia)
    monkeypatch.setattr(mcp_policy, "load_table", tabla)
    monkeypatch.setattr(mcp_policy, "_por_autonomia", sin_autonomia)

    grupos = asyncio.run(caps._connector_groups("act_autonomous"))
    g = next(x for x in grupos if x["connector"] == "wai-viajes")
    assert g["enumerated"] is True, "con catálogo registrado, el conector SÍ está enumerado"
    assert g["read_verdict"] != caps.V_NA, "WAI Viajes funcionando no puede leerse «no disponible»"


# ── 2. Shortcuts: an error is an error, and Composio goes through its router ─────────────

def test_isError_es_un_fallo_aunque_venga_como_resultado():
    salida = {"content": [{"type": "text", "text": "MCP error -32602: Tool X not found"}],
              "isError": True}
    assert "not found" in atajos._fallo_de(salida)
    assert atajos._fallo_de({"content": [{"type": "text", "text": "{}"}]}) == ""
    assert atajos._fallo_de({"error": "caído"}) == "caído"


def _envoltorio(respuesta: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(
        {"data": {"results": [{"response": respuesta}]}, "successful": True})}]}


def test_composio_se_desenvuelve_hasta_el_dato_de_la_herramienta():
    ok = atajos._desenvolver_composio(_envoltorio(
        {"successful": True, "data": {"lat": 40.41, "lon": -3.69}}))
    assert atajos._fallo_de(ok) == ""
    assert json.loads(atajos._texto_de(ok)) == {"lat": 40.41, "lon": -3.69}

    mal = atajos._desenvolver_composio(_envoltorio(
        {"successful": False, "error": "Following fields are missing: {'address_query'}"}))
    assert "address_query" in atajos._fallo_de(mal), "el motivo de Composio llega entero"


def test_un_atajo_de_composio_llama_por_la_meta_herramienta(monkeypatch):
    import app.routes.mcp_proxy_routes as proxy
    from app import mcp_policy
    import app.mcp_actor as actor

    llamadas = []

    async def llamar(servidor, herramienta, argumentos, acting_user=""):
        llamadas.append((servidor, herramienta, argumentos, acting_user))
        return _envoltorio({"successful": True, "data": {"ok": 1}})

    async def sin_veto(*_a, **_k):
        return None

    async def persona(_run):
        return "ana@x.com"

    monkeypatch.setattr(proxy, "llamar_herramienta", llamar)
    monkeypatch.setattr(mcp_policy, "veto_for_call", sin_veto)
    monkeypatch.setattr(actor, "run_serving_user", persona)
    monkeypatch.setattr(atajos, "_es_de_composio", lambda s: s == "composio-google-maps")

    r = asyncio.run(atajos.ejecutar(
        {"id": "abrir_destino_directo",
         "herramienta": "composio-google-maps_GOOGLE_MAPS_GEOCODE_ADDRESS_WITH_QUERY"},
        {"address_query": "Museo del Prado"}, run_id=7))
    assert r["ok"] is True
    servidor, herramienta, argumentos, quien = llamadas[0]
    assert (servidor, herramienta) == ("composio-google-maps", "COMPOSIO_MULTI_EXECUTE_TOOL")
    assert argumentos["tools"] == [{"tool_slug": "GOOGLE_MAPS_GEOCODE_ADDRESS_WITH_QUERY",
                                    "arguments": {"address_query": "Museo del Prado"}}]
    assert quien == "ana@x.com", "quién pregunta viaja también por el router"


# ── 3. The reception only picks shortcuts that exist ──────────────────────────────────────

def test_un_atajo_que_no_esta_declarado_se_convierte_en_delegar(monkeypatch):
    monkeypatch.setattr(atajos, "buscar",
                        lambda c: {"id": c} if c == "abrir_destino_directo" else None)
    inventado = {"accion": "atajo", "capacidad": "calcular_distancia_duracion",
                 "argumentos": {"destino": "Prado"}}
    assert recepcion._solo_atajos_declarados(inventado)["accion"] == "delego"
    bueno = {"accion": "atajo", "capacidad": "abrir_destino_directo", "argumentos": {}}
    assert recepcion._solo_atajos_declarados(bueno) == bueno
    assert recepcion._solo_atajos_declarados({"accion": "pregunto", "texto": "¿?"})["accion"] == "pregunto"
