"""v12.59.2 — three failures that gave no visible error and really broke something.

1. The colleague card with a STRUCTURED definition brought down `/mcp/colleague` (500): the
   dict from `_presentacion_legible` went as-is into `card_to_text` and the final `strip()`
   blew up. Nobody could consult that employee as a colleague.
2. The daily chat cleanup (`_cleanup_old_chat_runs`) failed on a foreign key: the tool
   ledgers hang from the run without `ondelete`, so the table stopped being pruned.
3. Connecting Odoo 17/18 authorized `#odoo` and the status asked about `#odoo-17-18`: the
   employee worked and the screen said "not connected".

All against the REAL functions: a mock of the fixed function would have caught none of them.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest


# ── 1 · colleague card ───────────────────────────────────────────────────────────────────
_ESTRUCTURADA = {
    "soy": "El asistente de viajes de la agencia.",
    "hago": [
        {"que": "Buscar vuelos", "detalle": "Cuando alguien quiere volar entre dos ciudades.",
         "ejemplos": ["vuelos a Estambul en mayo"], "aprobacion": False},
        {"que": "Reservar hotel", "detalle": "", "ejemplos": [], "aprobacion": True},
        {"detalle": "sin titular: no es una capacidad"},
    ],
    "no_hago": ["Facturar", "Cambiar precios"],
}


def test_ficha_estructurada_se_lee_en_prosa():
    from app.colleagues import _presentacion_legible, card_to_text

    card = {"nombre": "Viajes", "rol": "Asistente",
            "presentacion": _presentacion_legible(_ESTRUCTURADA)}
    texto = card_to_text(card)  # before: AttributeError 'dict' object has no attribute 'strip'
    assert "El asistente de viajes de la agencia." in texto
    assert "- Buscar vuelos — Cuando alguien quiere volar entre dos ciudades." in texto
    assert "«vuelos a Estambul en mayo»" in texto
    assert "Reservar hotel (asks for approval before doing it)" in texto
    assert "I do not: Facturar; Cambiar precios." in texto
    assert "{" not in texto and "'que'" not in texto  # no dumped dict


def test_ficha_en_texto_libre_sigue_igual():
    from app.colleagues import _presentacion_legible, card_to_text

    card = {"nombre": "Odoo", "presentacion": _presentacion_legible("Llevo las ventas de Odoo.")}
    assert card_to_text(card) == "**Odoo**\nLlevo las ventas de Odoo."


def test_ficha_sin_presentacion_usa_la_descripcion_y_lo_dice():
    from app.colleagues import card_to_text

    texto = card_to_text({"nombre": "X", "presentacion": {}, "que_resuelve": "Algo"})
    assert "Algo" in texto and "has not written its presentation yet" in texto


# ── 2 · daily chat cleanup ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_limpieza_borra_chats_viejos_con_sus_libros_de_herramientas():
    from sqlalchemy import text
    from app.app_database import app_engine
    from app.main import _cleanup_old_chat_runs

    viejo = (datetime.now(timezone.utc) - timedelta(days=45)).replace(tzinfo=None)
    reciente = datetime.now(timezone.utc).replace(tzinfo=None)
    async with app_engine.begin() as conn:
        for rid, mode, fin in ((1, "chat", viejo), (2, "chat", reciente), (3, "autonomous", viejo)):
            await conn.execute(text(
                "INSERT INTO agent_runs (id, idempotency_key, status, mode, attempts, prompt, started_at, finished_at) "
                "VALUES (:id, :k, 'DONE', :m, 1, 'p', :f, :f)"), {"id": rid, "k": f"k{rid}", "m": mode, "f": fin})
            await conn.execute(text(
                "INSERT INTO tool_use_log (run_id, call_id, step_idx, tool_name, status, started_at) "
                "VALUES (:r, 'c', 0, 't', 'ok', :f)"), {"r": rid, "f": fin})
            await conn.execute(text(
                "INSERT INTO tool_call_log (run_id, step_idx, tool_name, arguments_hash, idempotency_key, executed_at) "
                "VALUES (:r, 0, 't', 'h', :k, :f)"), {"r": rid, "k": f"i{rid}", "f": fin})

    await _cleanup_old_chat_runs()  # before: FOREIGN KEY constraint failed

    async with app_engine.begin() as conn:
        runs = {r[0] for r in (await conn.execute(text("SELECT id FROM agent_runs"))).all()}
        usos = {r[0] for r in (await conn.execute(text("SELECT run_id FROM tool_use_log"))).all()}
        llamadas = {r[0] for r in (await conn.execute(text("SELECT run_id FROM tool_call_log"))).all()}
    assert runs == {2, 3}          # only the old chat goes
    assert usos == {2, 3} and llamadas == {2, 3}


def test_las_hijas_declaradas_son_todas_las_que_no_tienen_ondelete():
    """If tomorrow another table hangs from agent_runs without ondelete, the cleanup breaks
    silently again: this test forces it onto the list."""
    from app.app_database import AppBase
    import app.app_models  # noqa: F401
    from app.main import _HIJAS_DE_AGENT_RUNS

    sin_ondelete = set()
    for tabla in AppBase.metadata.sorted_tables:
        for fk in tabla.foreign_keys:
            if fk.column.table.name == "agent_runs" and not fk.ondelete:
                sin_ondelete.add(tabla.name)
    assert sin_ondelete == set(_HIJAS_DE_AGENT_RUNS)


# ── 3 · identity of the Odoo connectors ─────────────────────────────────────────────────
# 15.0.0: each Odoo connector has its own slot — the usual one `#odoo`, the 17/18 one through the translator
# `#odoo-17-18` (the engine's `oauth_proxy.odoo_slot`). What this guards stays: connect and resolve use the SAME one.
_FICHAS = {"odoo-17-18": {"composio_toolkit": "odoo", "translator_base": "https://mcp.usewai.com/t/x"},
           "composio-odoo": {"composio_toolkit": "odoo"}}


def test_odoo_17_18_se_conecta_y_se_resuelve_con_la_misma_identidad(monkeypatch):
    from app import mcp_runtime
    from app.config import segmento_de_identidad
    from app.routes.mcp_connectors_routes import _identity_connector
    monkeypatch.setattr(mcp_runtime, "fichas_de_conectores", lambda fresco=False: _FICHAS)

    odoo_17_18 = {"name": "odoo-17-18", "toolkit": "odoo"}
    odoo_19 = {"name": "composio-odoo", "toolkit": "odoo"}
    gmail = {"name": "composio-gmail", "toolkit": "gmail"}
    # The routes (connect/status) and the runtime (resolve_connector) use the SAME rule.
    assert _identity_connector(odoo_17_18) == segmento_de_identidad(odoo_17_18) == "odoo-17-18"
    assert _identity_connector(odoo_19) == segmento_de_identidad(odoo_19) == "odoo"
    assert _identity_connector(gmail) == segmento_de_identidad(gmail) == "composio-gmail"


def test_resolve_connector_pregunta_por_la_identidad_que_se_conecto(monkeypatch):
    """The runtime asks Composio about the SAME entity the Connectors screen authorized."""
    from app import mcp_runtime

    vistas: list[str] = []

    async def _cuentas(user_email, connector="", *a, **k):
        vistas.append(connector)
        return []

    import app.mcp_connectors as mc
    monkeypatch.setattr(mc, "list_connections", _cuentas, raising=False)
    monkeypatch.setattr(mcp_runtime, "fichas_de_conectores", lambda fresco=False: _FICHAS)
    asyncio.run(mcp_runtime.resolve_connector({"name": "odoo-17-18", "toolkit": "odoo"},
                                              "robot@acme.com", necesita_url=False))
    assert vistas, "resolve_connector no llegó a preguntar por la cuenta"
    assert vistas == ["odoo-17-18"], vistas
