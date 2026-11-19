"""The turn context: what almost every turn reads, read before the model (07/10/2026).

What is pinned is what fails silently: a failed read hidden as if it were data, a slow one
eating the time it should save, and the block appearing when nothing was declared."""
from __future__ import annotations

import asyncio

import pytest

from app import atajos, contexto_de_turno as C
from app.medicion import MedicionDelTurno


def _declara(monkeypatch, llamadas):
    import app.conversacion as conv
    monkeypatch.setattr(conv, "declaracion", lambda: {"contexto_de_turno": llamadas})


@pytest.mark.asyncio
async def test_sin_nada_declarado_no_hay_bloque(monkeypatch):
    _declara(monkeypatch, [])
    assert await C.bloque(7) == ("", {})


@pytest.mark.asyncio
async def test_las_lecturas_van_juntas_y_un_fallo_se_dice(monkeypatch):
    _declara(monkeypatch, [{"herramienta": "viajes_contexto", "para": "who is asking"},
                           {"herramienta": "viajes_hora", "fijos": {"tz": "Europe/Madrid"}}])
    vistas = []

    async def falso(atajo, argumentos, run_id):
        vistas.append((atajo["herramienta"], argumentos, run_id))
        if atajo["herramienta"] == "viajes_hora":
            return {"ok": False, "error": "boom"}
        return {"ok": True, "resultado": {"content": [{"type": "text", "text": "Ana, trip 5"}]}}

    monkeypatch.setattr(atajos, "ejecutar", falso)
    texto, medida = await C.bloque(7)
    assert ("viajes_hora", {"tz": "Europe/Madrid"}, 7) in vistas
    assert "[TURN CONTEXT" in texto and "Ana, trip 5" in texto
    # A failure is NOT hidden as data: the agent knows it has to make that call itself.
    assert "`viajes_hora` — FAILED" in texto and "boom" in texto
    assert medida["n"] == 2 and medida["fallos"] == 1


@pytest.mark.asyncio
async def test_una_lectura_lenta_no_se_come_el_turno(monkeypatch):
    _declara(monkeypatch, [{"herramienta": "lenta"}])

    async def lenta(atajo, argumentos, run_id):
        await asyncio.sleep(5)
        return {"ok": True, "resultado": "x"}

    monkeypatch.setattr(atajos, "ejecutar", lenta)
    monkeypatch.setattr(C, "_PLAZO_S", 0.05)
    texto, medida = await C.bloque(1)
    assert "not fetched" in texto and medida["fallos"] == 1


@pytest.mark.asyncio
async def test_un_resultado_enorme_se_recorta(monkeypatch):
    _declara(monkeypatch, [{"herramienta": "grande"}])

    async def grande(atajo, argumentos, run_id):
        return {"ok": True, "resultado": {"content": [{"type": "text", "text": "x" * 50_000}]}}

    monkeypatch.setattr(atajos, "ejecutar", grande)
    texto, _ = await C.bloque(1)
    assert len(texto) < C._TOPE_RESULTADO + 500 and "[trimmed]" in texto


def test_la_medida_del_turno_lleva_el_contexto_solo_si_lo_hubo():
    m = MedicionDelTurno(run_id=1, hilo=None, origen="chat")
    assert "contexto" not in m.resumen()
    m.contexto_previo(ms=900, n=2, fallos=0)
    assert m.resumen()["contexto"] == {"ms": 900, "n": 2, "fallos": 0}
