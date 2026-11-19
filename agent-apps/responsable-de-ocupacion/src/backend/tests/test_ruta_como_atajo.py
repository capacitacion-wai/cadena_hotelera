"""14.26.0 — the whole route as a shortcut: rounds in parallel, data from the request or from an
earlier result, conditional steps skipped, and any doubt takes the normal path.

Against the REAL executor, with the per-call `ejecutar` replaced (that one is covered by the
shortcut's own tests: gates, Composio router, errors inside a "good" answer)."""
from __future__ import annotations

import asyncio
import json

GEO, MAT, HORA = "maps_GEOCODE", "maps_MATRIX", "viajes_HORA"
DEF = {"soy": "x", "hago": [{
    "id": "dist", "que": "Distance to a place",
    "resuelve_con": {"ruta": True, "argumentos": ["address_query", "travelMode"],
                     "salvo_si": "they ask to compare", "no_vale_si": ["they ask about a restriction"]},
    "ruta": {"rondas": [
        {"llamadas": [{"herramienta": GEO, "rellena": {"address_query": "the place"}},
                      {"herramienta": HORA, "solo_si": "they ask about a restriction",
                       "rellena": {"timezone": "zone"}}]},
        {"llamadas": [{"herramienta": MAT, "fijos": {"units": "METRIC"}, "rellena": {
            "travelMode": "the mode", "destinations": {"de": GEO, "campo": "results[0].location"}}}]}]},
}]}


def _mcp(data):
    return {"content": [{"type": "text", "text": json.dumps(data)}]}


def test_the_catalog_carries_the_route_and_its_conditions(monkeypatch):
    import app.atajos as A
    monkeypatch.setattr(A, "_definicion", lambda: DEF)
    c = A.buscar("dist")
    assert c and c["ruta"] and c["argumentos"] == ["address_query", "travelMode"]
    assert "they ask to compare" in c["salvo_si"] and "restriction" in c["salvo_si"]


def test_a_value_is_read_from_an_earlier_result():
    from app.atajos import valor_en
    d = {"results": [{"location": {"lat": 1, "lng": 2}}]}
    assert valor_en(d, "results[0].location") == {"lat": 1, "lng": 2}
    assert valor_en(d, "results[1].location") is None
    assert valor_en(d, "nada") is None


def test_the_route_runs_round_by_round_without_the_conditional_step(monkeypatch):
    import app.atajos as A
    monkeypatch.setattr(A, "_definicion", lambda: DEF)
    llamadas = []

    async def falso(atajo, args, run_id):
        llamadas.append((atajo["herramienta"], args))
        if atajo["herramienta"] == GEO:
            return {"ok": True, "resultado": _mcp({"results": [{"location": {"lat": 1, "lng": 2}}]})}
        return {"ok": True, "resultado": _mcp({"duration": "15 min"})}
    monkeypatch.setattr(A, "ejecutar", falso)
    salida = asyncio.run(A._ejecutar_ruta(A.buscar("dist"), {"address_query": "Prado", "travelMode": "WALK"}, 1))
    assert salida["ok"] and salida["resultado"][MAT] == {"duration": "15 min"}
    assert [h for h, _ in llamadas] == [GEO, MAT], "the conditional step is not made"
    assert llamadas[1][1] == {"units": "METRIC", "travelMode": "WALK", "destinations": {"lat": 1, "lng": 2}}


def test_a_missing_value_or_a_failed_call_takes_the_normal_path(monkeypatch):
    import app.atajos as A
    monkeypatch.setattr(A, "_definicion", lambda: DEF)

    async def sin_resultados(atajo, args, run_id):
        return {"ok": True, "resultado": _mcp({"results": []})}
    monkeypatch.setattr(A, "ejecutar", sin_resultados)
    salida = asyncio.run(A._ejecutar_ruta(A.buscar("dist"), {"address_query": "X", "travelMode": "WALK"}, 1))
    assert not salida["ok"] and "destinations" in salida["error"]

    async def falla(atajo, args, run_id):
        return {"ok": False, "error": "boom"}
    monkeypatch.setattr(A, "ejecutar", falla)
    assert not asyncio.run(A._ejecutar_ruta(A.buscar("dist"), {"address_query": "X", "travelMode": "W"}, 1))["ok"]


def test_the_conversation_sees_it_as_direct_with_its_conditions():
    from app.conversacion import atajo_de
    d = atajo_de(DEF["hago"][0])
    assert d["argumentos"] == ["address_query", "travelMode"] and "restriction" in d["salvo_si"]


def test_brackets_without_index_read_every_element():
    """13/10/2026: a route read `places[].displayName.text` (the names of all the candidates)."""
    from app.atajos import valor_en
    d = {"places": [{"displayName": {"text": "A"}}, {"displayName": {"text": "B"}}, {"x": 1}]}
    assert valor_en(d, "places[].displayName.text") == ["A", "B"]
    assert valor_en({"a": [{"b": [1, 2]}, {"b": [3]}]}, "a[].b[]") == [1, 2, 3]
    assert valor_en(d, "places[].nada") is None
    assert valor_en(d, "places[1].displayName.text") == "B"
