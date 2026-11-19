"""A new order does not inherit what the DELETED order that had its same id produced.

Seen in demo on 06/09/2026: a freshly created order, without a single firing, showed up in the list
with «Pendientes 2 / 7» while its page said «Todavía no ha generado nada trazable».

The chain: SQLite reuses the id of a deleted row (``INTEGER PRIMARY KEY`` without
``AUTOINCREMENT``), so when an order is deleted ``delete_automation`` relabels its receipts to
``automation:{id}:deleted`` to unlink them. But four readers resolved the id with
``source.split(":")[1]`` — and ``automation:2:deleted`` yields «2» just like ``automation:2``. The
page (``produced_by``) compares the exact source; the others did not. Two readers, two truths.

We seed EXACTLY that situation: a live receipt of order 2 with one decision, and a
``automation:2:deleted`` receipt with two decisions. The right answer is 1; inheriting gives 3 — with those
numbers, «right» and «wrong» look nothing alike.
"""
from __future__ import annotations

import asyncio

import pytest

AID = 2


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed() -> None:
    from app.app_database import app_write
    from app.app_models.automations import Automation
    from app.app_models.business import Decision
    from app.app_models.runtime import AgentRun, Event

    async with app_write() as s:
        s.add(Automation(id=AID, title="Revisión semanal", enabled=True,
                         signal_type="scheduled", signal_spec="{}"))
        await s.flush()

        def recibo(marca: str, n: int) -> Event:
            return Event(fingerprint=f"fp-{marca}-{n}", type="automation_fired", source=marca)

        vivo = recibo(f"automation:{AID}", 1)
        borrado = recibo(f"automation:{AID}:deleted", 2)
        s.add_all([vivo, borrado])
        await s.flush()
        run_vivo = AgentRun(event_id=vivo.id, idempotency_key="run-vivo", prompt="x",
                            status="COMPLETED", cost_usd="0.10")
        run_borrado = AgentRun(event_id=borrado.id, idempotency_key="run-borrado", prompt="x",
                               status="COMPLETED", cost_usd="0.90")
        s.add_all([run_vivo, run_borrado])
        await s.flush()
        s.add(Decision(idempotency_key="d-vivo", created_by_run_id=run_vivo.id,
                       decision="no_action", status="open"))
        s.add_all([
            Decision(idempotency_key=f"d-borrado-{i}", created_by_run_id=run_borrado.id,
                     decision="no_action", status="open")
            for i in range(2)
        ])
        await s.commit()


def test_el_resolutor_solo_acepta_la_forma_exacta():
    from app.automation_provenance import automation_id_of
    assert automation_id_of("automation:2") == 2
    assert automation_id_of("automation:2:deleted") is None
    assert automation_id_of("automation:") is None
    assert automation_id_of("odoo:2") is None
    assert automation_id_of(None) is None


def test_la_lista_y_la_ficha_cuentan_lo_mismo_tras_un_borrado():
    from app import automation_provenance as prov

    _run(_seed())
    ficha = _run(prov.produced_by(AID))
    lote = _run(prov.open_counts_batch())
    assert ficha["total"] == 1 and ficha["open"] == 1
    # Before: {2: {"produced": 3, "open": 3}} — the order inherited the two from the deleted one.
    assert lote == {AID: {"produced": 1, "open": 1}}


def test_quien_produjo_decisiones_no_incluye_a_la_borrada():
    from app import automation_provenance as prov

    _run(_seed())
    productores = _run(prov.producers_of("decisions"))
    assert [(p["id"], p["count"]) for p in productores] == [(AID, 1)]


def test_el_gasto_de_la_borrada_no_se_le_cobra_a_la_nueva():
    from app import automation_stats

    _run(_seed())
    gasto = _run(automation_stats.cost(None, None, None))
    assert gasto[AID]["runs"] == 1
    assert gasto[AID]["cost_usd"] == pytest.approx(0.10)


def test_la_ultima_ejecucion_de_la_lista_es_la_de_un_recibo_vivo():
    from app import automations_service

    _run(_seed())
    filas = _run(automations_service.list_automations())
    fila = next(f for f in filas if f["id"] == AID)
    ultima = fila.get("last_run") or {}
    # With the deleted receipt inherited, the «last run» could be the dead order's.
    assert ultima.get("id") is not None
    from app.app_database import app_session
    from app.app_models.runtime import AgentRun
    from sqlalchemy import select

    async def _idem(rid: int) -> str:
        async with app_session() as s:
            return (await s.execute(select(AgentRun.idempotency_key).where(AgentRun.id == rid))).scalar()
    assert _run(_idem(ultima["id"])) == "run-vivo"
