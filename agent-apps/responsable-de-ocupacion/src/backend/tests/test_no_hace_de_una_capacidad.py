"""14.25.0 — what a capability does NOT cover reaches whoever speaks for the employee.

Without it, the conversation stretches the nearest capability over a request that belongs
to another one and offers it. Against the REAL renderers, with the Definition and the
permissions snapshot replaced.
"""
from __future__ import annotations

import asyncio

DEF = {
    "soy": "Travel assistant.",
    "hago": [
        {"id": "contexto", "que": "Know who is asking and which trips they can see",
         "necesita": "a signed-in session", "no_hace": ["compare or recommend trips", " "]},
        {"id": "otra", "que": "Something without limits"},
    ],
}


def test_the_conversation_line_says_where_the_capability_ends(monkeypatch):
    import app.conversacion as conv
    import app.voz as voz

    async def _ninguno(_snap):
        return set()

    async def _snap():
        return {}

    monkeypatch.setattr(conv, "declaracion", lambda: DEF)
    monkeypatch.setattr(voz, "_apoyos_caidos", _ninguno)
    import app.capabilities as caps
    monkeypatch.setattr(caps, "build_snapshot", _snap)

    lineas = asyncio.run(conv.capacidades_vivas())
    assert "Not part of this (route it elsewhere or say so): compare or recommend trips" in lineas[0]
    assert "Not part of this" not in lineas[1], "nothing declared, nothing said"


def test_the_colleague_card_carries_no_hace():
    from app.colleagues import _presentacion_legible
    card = _presentacion_legible(DEF)
    assert card["hago"][0]["no_hace"] == ["compare or recommend trips"]
    assert card["hago"][1]["no_hace"] == []
