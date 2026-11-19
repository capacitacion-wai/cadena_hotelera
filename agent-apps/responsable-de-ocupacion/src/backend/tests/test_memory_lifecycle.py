"""Memory lifecycle: merge, extend, measure redundancy and forget (7.153.0).

What motivated these layers, measured on the Vigía in production: 107 live entries, piles
of 23 and 20 on the same subject, and **`superseded_by = 0`** — consolidation had run 14
paid LLM executions without merging ANYTHING. The cause was textbook: the maintenance run
was told to write the baseline "with `supersedes=[ids]`" and `remember_learning` silently
ignored the parameter, so it completed green without cleaning anything.

Each test here pins one of the missing pieces:
  · LAYER 0 — `supersedes` really retires, and respects what backs an approved rule.
  · LAYER 1 — writing is DECIDING: the more complete version EXTENDS instead of opening a new row.
  · LAYER 2 — redundancy is MEASURED in pairs, not in rows.
  · LAYER 3 — what nobody consults is retired; what is used survives.
  · LAYER 4 — two entries on the same subject with opposite polarity are surfaced.
"""
from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _guardar(content: str, subject: str = "job:barrido", kind: str = "observation"):
    from app.agent_memory_utils import remember
    # No `run_id`: the test DB has no `agent_runs` rows and the FK would reject it.
    return await remember(content=content, subject=subject, kind=kind)


# ── LAYER 0 ──────────────────────────────────────────────────────────────────
def test_supersedes_retira_de_verdad():
    from app.agent_memory_utils import remember
    from app import agent_memory_utils as M

    # Two DIFFERENT facts on the same subject (if they were near-identical the dedupe
    # would merge them before getting here, which is exactly what layer 1 does).
    a = _run(_guardar("El barrido de indexing tarda unos 40 minutos"))
    b = _run(_guardar("El barrido de indexing se lanza desde el cron de las 3"))
    assert a["id"] and b["id"] and a["id"] != b["id"]

    base = _run(remember(content="El barrido de indexing tarda entre 35 y 45 minutos",
                         subject="job:barrido", kind="baseline",
                         supersedes=[a["id"], b["id"]]))
    assert base.get("superseded") == 2, "la línea base debe retirar lo que resume"

    vivas = _run(M.stats())["total"]
    assert vivas >= 1
    # And the retired ones no longer count as live.
    from app.app_database import app_session
    from app.app_models.agent_memory import AgentMemory
    from sqlalchemy import select

    async def _ids_vivas():
        async with app_session() as s:
            return set((await s.execute(
                select(AgentMemory.id).where(AgentMemory.superseded_by.is_(None))
            )).scalars().all())

    vivas_ids = _run(_ids_vivas())
    assert a["id"] not in vivas_ids and b["id"] not in vivas_ids
    assert base["id"] in vivas_ids


def test_no_se_retira_lo_que_sostiene_una_regla():
    """Removing a learning that backs an approved rule is the operator's decision."""
    from sqlalchemy import update as sql_update
    from app.agent_memory_utils import remember, supersede
    from app.app_database import app_write
    from app.app_models.agent_memory import AgentMemory

    a = _run(_guardar("Los domingos el cluster va más lento", subject="cluster:domingos"))

    async def _marcar():
        async with app_write() as s:
            await s.execute(sql_update(AgentMemory)
                            .where(AgentMemory.id == a["id"])
                            .values(behavior_effect="silencio:1"))
            await s.commit()
    _run(_marcar())

    b = _run(remember(content="Los domingos hay backup y el cluster rinde peor",
                      subject="cluster:domingos", kind="baseline"))
    assert _run(supersede([a["id"]], b["id"], reason="test")) == 0


# ── LAYER 1 ──────────────────────────────────────────────────────────────────
def test_la_version_mas_completa_amplia_en_vez_de_duplicar():
    # Pair MEASURED in the extension band (0.72 with the local provider): above
    # 0.75 it would be "the same fact" and reinforcement would catch it; below 0.55, another fact.
    corto = "El barrido de indexing tarda entre 30 y 45 minutos"
    largo = "El barrido de indexing tarda entre 30 y 45 minutos y es lo normal en este entorno"
    a = _run(_guardar(corto, subject="job:indexing"))
    b = _run(_guardar(largo, subject="job:indexing"))
    # What matters is the RESULT: a single entry, with the most complete version.
    assert b["id"] == a["id"], "debía quedarse en una entrada, no abrir otra"
    assert b.get("reinforced") is True
    from app.app_database import app_session
    from app.app_models.agent_memory import AgentMemory
    from sqlalchemy import select

    async def _contenido():
        async with app_session() as s:
            return (await s.execute(
                select(AgentMemory.content).where(AgentMemory.id == a["id"]))).scalar()

    assert "normal" in _run(_contenido()), "debe conservar la versión que dice MÁS"


# ── LAYER 2 ──────────────────────────────────────────────────────────────────
def test_la_redundancia_se_mide_en_pares():
    from app.agent_memory_utils import redundancy_report

    _run(_guardar("La replicación de Airbyte se queda a medias con frecuencia",
                  subject="airbyte:replicacion"))
    _run(_guardar("El catálogo de Metabase pierde referencias de bases de datos",
                  subject="airbyte:replicacion"))
    rep = _run(redundancy_report("airbyte:replicacion"))
    # Two DIFFERENT facts on the same subject are not redundancy.
    assert not rep or rep[0]["redundant_pairs"] == 0


# ── LAYER 3 ──────────────────────────────────────────────────────────────────
def test_lo_que_nadie_usa_se_retira_y_lo_usado_sobrevive():
    from datetime import datetime, timedelta
    from sqlalchemy import update as sql_update
    from app.agent_memory_utils import compact, note_used
    from app.app_database import app_write
    from app.app_models.agent_memory import AgentMemory

    viejo = _run(_guardar("Dato que nadie consultará jamás", subject="ruido:uno"))
    usado = _run(_guardar("Dato que sí se consulta", subject="ruido:dos"))
    _run(note_used([usado["id"]]))

    async def _envejecer():
        async with app_write() as s:
            await s.execute(sql_update(AgentMemory)
                            .where(AgentMemory.id.in_([viejo["id"], usado["id"]]))
                            .values(created_at=datetime.utcnow() - timedelta(days=30)))
            await s.commit()
    _run(_envejecer())

    out = _run(compact())
    assert out.get("unused_retired", 0) >= 1

    from sqlalchemy import select
    from app.app_database import app_session

    async def _estado(mid):
        async with app_session() as s:
            return (await s.execute(
                select(AgentMemory.review_state).where(AgentMemory.id == mid))).scalar()

    assert _run(_estado(viejo["id"])) == "rejected", "sin uso y con plazo cumplido → fuera"
    assert _run(_estado(usado["id"])) != "rejected", "lo consultado sobrevive"


def test_lo_importante_no_lo_puede_borrar_el_segador():
    """The queue asks you about it; the reaper cannot take it away behind your back.

    Both parts now read the SAME floor (`AGENT_SELF_MEMORY_IMPORTANT_FLOOR`), which
    is what prevents the system from asking for judgement on a row whose days it is
    itself counting down.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import select, update as sql_update
    from app.agent_memory_utils import compact
    from app.app_database import app_session, app_write
    from app.app_models.agent_memory import AgentMemory
    from app.config import AGENT_SELF_MEMORY_IMPORTANT_FLOOR

    grave = _run(_guardar("Este nodo se queda sin disco de forma recurrente",
                          subject="nodo:disco"))

    async def _importante_y_sin_usar():
        # Still valid on purpose: that isolates the DISUSE rule. If it had also
        # expired, the expiry rule would retire it, which is something else and is right
        # (that is what pinning is for).
        async with app_write() as s:
            await s.execute(sql_update(AgentMemory)
                            .where(AgentMemory.id == grave["id"])
                            .values(importance=float(AGENT_SELF_MEMORY_IMPORTANT_FLOOR),
                                    created_at=datetime.utcnow() - timedelta(days=30),
                                    valid_until=datetime.utcnow() + timedelta(days=30)))
            await s.commit()
    _run(_importante_y_sin_usar())
    _run(compact())

    async def _estado():
        async with app_session() as s:
            return (await s.execute(
                select(AgentMemory.review_state)
                .where(AgentMemory.id == grave["id"]))).scalar()
    assert _run(_estado()) != "rejected", (
        "el segador retiró por desuso algo marcado como importante — justo lo que la "
        "cola de criterio te está pidiendo decidir")


# ── LAYER 4 ──────────────────────────────────────────────────────────────────
def test_las_contradicciones_salen_a_la_luz():
    from app.agent_memory_utils import contradictions

    _run(_guardar("El barrido cubre los 18 tenants correctamente",
                  subject="cluster:cobertura"))
    _run(_guardar("El barrido no cubre los 18 tenants",
                  subject="cluster:cobertura"))
    pares = _run(contradictions("cluster:cobertura"))
    assert pares, "afirmar y negar lo mismo es una contradicción, no dos aprendizajes"
    assert pares[0]["older_id"] != pares[0]["newer_id"]


@pytest.mark.parametrize("texto,afirma", [
    ("El gate cubre los 18 tenants", True),
    ("El gate no cubre todos los tenants", False),
    ("La sincronización falla de forma persistente", False),
    ("La replicación termina correctamente", True),
])
def test_polaridad(texto, afirma):
    from app.memory_embeddings import is_affirmative
    assert is_affirmative(texto) is afirma


def test_no_se_fusiona_una_afirmacion_con_su_negacion():
    """Uncovered by the contradictions test: "X covers the 18 tenants" and "X does NOT cover
    the 18 tenants" score 0.77 similarity, so the dedupe treated them as the same fact
    and the old one —the wrong one— silently won. If they contradict, both go in."""
    a = _run(_guardar("El barrido cubre los 18 tenants correctamente",
                      subject="cluster:polaridad"))
    b = _run(_guardar("El barrido no cubre los 18 tenants", subject="cluster:polaridad"))
    assert b["id"] != a["id"], "una negación no puede reforzar a su afirmación"


# ── The short queue has to be SHORT (7.157.0) ────────────────────────────────
def test_la_cola_agrupa_por_asunto_y_exige_peso():
    """The real panel showed 40 cards: 20 pairs on the SAME subject, all between
    loose observations. Twenty pairs on the same subject are ONE problem, and a
    contradiction between two anecdotes is resolved by consolidation, not by the operator."""
    from app.agent_memory_utils import needs_attention

    # Two opposite anecdotes, neither backed → must NOT reach the queue.
    _run(_guardar("El barrido cubre los tenants correctamente", subject="c:uno"))
    _run(_guardar("El barrido no cubre los tenants", subject="c:uno"))
    cola = _run(needs_attention())
    assert not cola["contradictions"], "dos observaciones sueltas no son trabajo del humano"

    # Now one gains weight (it is seen again): it is more recent AND better backed,
    # so it DOMINATES — the evidence already decided and there is nothing to ask (7.164.0).
    # Before, this landed in the operator's queue and was exactly the noise that
    # flooded it: asking about a decision the data had already made.
    _run(_guardar("El barrido cubre los tenants correctamente", subject="c:uno"))
    cola = _run(needs_attention())
    assert not cola["contradictions"], "una versión domina: no es trabajo del humano"
    assert len(cola.get("resolvable") or []) == 1
    assert cola["resolvable"][0]["subject"] == "c:uno"
    assert "no cubre" in cola["resolvable"][0]["drop"]


def test_una_contradiccion_EMPATADA_sigue_siendo_tuya():
    """If no version dominates, the machine must not choose for you.

    Dominating requires BOTH: more recent AND better backed. With both equally
    seen, the only honest way out is to ask — that is the difference between resolving
    with evidence and resolving by coin toss.
    """
    from app.agent_memory_utils import needs_attention

    _run(_guardar("El backup nocturno termina siempre", subject="c:empate"))
    _run(_guardar("El backup nocturno no termina", subject="c:empate"))
    _run(_guardar("El backup nocturno termina siempre", subject="c:empate"))
    _run(_guardar("El backup nocturno no termina", subject="c:empate"))

    cola = _run(needs_attention())
    asuntos = {c["subject"] for c in cola["contradictions"]}
    assert "c:empate" in asuntos, "con las dos versiones igual de respaldadas hay que preguntar"


def test_lo_que_confirmo_una_persona_no_lo_tumba_una_heuristica():
    """Even if the other version is newer and seen more often."""
    from app.agent_memory_utils import needs_attention
    from sqlalchemy import update as sql_update
    from app.app_database import app_write
    from app.app_models.agent_memory import AgentMemory

    fijada = _run(_guardar("El clúster tiene tres nodos de control", subject="c:humano"))

    async def _confirmar():
        async with app_write() as s:
            await s.execute(sql_update(AgentMemory)
                            .where(AgentMemory.id == fijada["id"])
                            .values(review_state="validated"))
            await s.commit()
    _run(_confirmar())

    _run(_guardar("El clúster no tiene tres nodos de control", subject="c:humano"))
    _run(_guardar("El clúster no tiene tres nodos de control", subject="c:humano"))

    cola = _run(needs_attention())
    tirados = {r["drop_id"] for r in (cola.get("resolvable") or [])}
    assert fijada["id"] not in tirados, (
        "una heurística retiró lo que una persona había confirmado")


def test_el_lastre_no_reforzable_se_retira_solo():
    """An entry with the date inside can NEVER be reinforced (the fingerprint changes
    every day), so it neither gains confidence nor consolidates: it is dead weight that
    also pollutes contradiction detection."""
    from sqlalchemy import select, update as sql_update
    from app.agent_memory_utils import compact
    from app.app_database import app_session, app_write
    from app.app_models.agent_memory import AgentMemory

    # Inserted bypassing the content discipline (like legacy entries).
    async def _legacy():
        async with app_write() as s:
            s.add(AgentMemory(
                idempotency_key="legacy-1", kind="observation", subject="viejo:uno",
                content="El 28 de julio de 2026 el gate agotó su presupuesto",
                observations=1, review_state="new", content_hash="x"))
            await s.commit()
    _run(_legacy())

    out = _run(compact())
    assert out.get("unreinforceable_retired", 0) >= 1

    async def _estado():
        async with app_session() as s:
            return (await s.execute(
                select(AgentMemory.review_state)
                .where(AgentMemory.idempotency_key == "legacy-1"))).scalar()
    assert _run(_estado()) == "rejected"
