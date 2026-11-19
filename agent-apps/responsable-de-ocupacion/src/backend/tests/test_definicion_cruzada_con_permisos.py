"""The definition is crossed with the LIVE permissions, and the cross has to really match.

Voice offers what the employee DECLARES it does, filtered by what it can do NOW. That
filter was first built against the readable veto list —«Crear / modificar / enviar»— while
`apoyo` declares tool keys —`wai-viajes_crear_viaje`—, so the comparison never matched:
the filter looked in place and let everything through. A filter that does not filter is
worse than none, because nobody looks at it again.

This pins both halves: that the keys are built the way `apoyo` writes them (with an
underscore — with a dot nothing matches, also silently) and that a snapshot that cannot
be read does not hide capabilities — offering too much beats offering too little, because
«you don't have that» the user corrects in one sentence, and a hidden capability nobody
corrects.
"""
from __future__ import annotations

import pytest

from app.voz import _apoyos_caidos


FOTO = {
    "groups": [
        {"kind": "connector", "connector": "wai-viajes", "tools": [
            {"name": "crear_viaje", "write": True, "verdict": "allow"},
            {"name": "borrar_viaje", "write": True, "verdict": "deny"},
            {"name": "buscar_vuelo", "write": False, "verdict": "unavailable"},
        ]},
        {"kind": "app", "capabilities": [
            {"key": "act", "verdict": "deny",
             "tools": ["upsert_alert", "log_decision"]},
            {"key": "see_data", "verdict": "allow", "tools": ["list_alerts"]},
        ]},
    ]
}


@pytest.mark.asyncio
async def test_las_claves_llevan_guion_bajo_y_el_nombre_del_servidor():
    caidos = await _apoyos_caidos(FOTO)
    assert "wai-viajes_borrar_viaje" in caidos
    assert "wai-viajes_buscar_vuelo" in caidos, "«unavailable» también es no tenerla"
    assert "wai-viajes_crear_viaje" not in caidos
    # With a dot nothing matches, and silently: if someone changes the separator, it shows here.
    assert not any("." in c for c in caidos if c.startswith("wai-viajes"))


@pytest.mark.asyncio
async def test_las_herramientas_locales_van_sin_servidor():
    caidos = await _apoyos_caidos(FOTO)
    assert "upsert_alert" in caidos and "log_decision" in caidos
    assert "list_alerts" not in caidos


@pytest.mark.asyncio
async def test_una_foto_vacia_no_esconde_nada():
    assert await _apoyos_caidos({}) == set()
    assert await _apoyos_caidos({"groups": []}) == set()
