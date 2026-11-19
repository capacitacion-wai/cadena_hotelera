"""v7.140.0 — the employee's OWN memory (what it learns about its ENVIRONMENT).

Four promises are worth a test, because breaking any of them silently makes the
feature worse than not having it:

1. **Reinforcement, not duplication.** Seeing the same thing 40 nights must build
   ONE baseline with 40 observations. If it duplicated, "evidence" would just
   measure how long the employee has been running.
2. **Expiry is honoured.** A learning past ``valid_until`` is kept (history) but
   NOT injected: an environment fact that expired and keeps being asserted is how
   an agent confidently says something false.
3. **The injected block is budgeted and honest.** It must respect its character
   budget and mark an unvalidated lesson AS unvalidated, so the model weighs it
   accordingly instead of treating a guess as an established fact.
4. **Isolation from PEOPLE's memory.** ``remember_learning`` must never write to
   ``user_memory`` — the two axes answer different questions and mixing them would
   leak "what I know about the cluster" into "what I know about Ana".
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_repeated_learning_reinforces_instead_of_duplicating() -> None:
    from app import agent_memory_utils as amu

    async def go():
        first = await amu.remember(
            content="El barrido de indexación tarda unos 40 minutos y es normal.",
            subject="job:indexing", kind="baseline")
        second = await amu.remember(
            content="El barrido de indexación tarda unos 40 minutos y es normal.",
            subject="job:indexing", kind="baseline")
        return first, second

    first, second = _run(go())
    assert first["id"] is not None
    assert second["reinforced"] is True
    assert second["id"] == first["id"], "un mismo aprendizaje NO debe duplicar fila"

    from app.app_database import app_session
    from app.app_models.agent_memory import AgentMemory
    from sqlalchemy import select

    async def read():
        async with app_session() as s:
            return (await s.execute(
                select(AgentMemory.observations, AgentMemory.confidence)
                .where(AgentMemory.id == first["id"])
            )).first()

    obs, conf = _run(read())
    assert obs == 2, "la evidencia debe crecer con cada confirmación"
    assert conf is not None and conf <= 0.99, "la confianza satura, no crece sin límite"


def test_expired_learning_is_kept_but_not_injected() -> None:
    from app import agent_memory_prompt as amp
    from app import agent_memory_utils as amu

    async def go():
        live = await amu.remember(content="Los domingos hay backup de 02:00 a 05:00.",
                                  subject="cluster:backup", kind="baseline")
        dead = await amu.remember(
            content="La migración de julio está en curso.",
            subject="cluster:migracion", kind="lesson",
            valid_until=datetime.utcnow() - timedelta(days=1))
        # A fresh build: the block is cached behind a version stamp, and every
        # write bumps it, so this reads the current world.
        detail = await amp.build_agent_memory_block_detail(enqueue_consolidation=False)
        return live, dead, detail

    live, dead, detail = _run(go())
    assert live["id"] and dead["id"]
    text = detail["text"] or ""
    assert "backup" in text
    assert "migración de julio" not in text.lower(), (
        "un aprendizaje caducado no se inyecta (pero sigue en la base)")
    assert dead["id"] not in (detail.get("entry_ids") or [])


def test_block_respects_budget_and_flags_unvalidated() -> None:
    from app import agent_memory_prompt as amp
    from app import agent_memory_utils as amu
    from app import config

    async def go():
        for i in range(40):
            # The content must not cite «observación número N»: the memory REJECTS a learning that points at other
            # entries by number (`_SELF_REF_RE`), and then nothing was stored and the mark never appeared (15.0.1).
            await amu.remember(
                content=f"The sweep-{i} batch takes several minutes to finish in this environment "
                        f"and that slowness is normal, not an incident.",
                subject=f"job:sweep-{i}", kind="lesson")
        return await amp.build_agent_memory_block_detail(enqueue_consolidation=False)

    detail = _run(go())
    text = detail["text"] or ""
    assert len(text) <= config.AGENT_SELF_MEMORY_BLOCK_CHARS + 400, (
        "el bloque debe respetar su presupuesto: es coste en CADA run")
    # A FRAGILE learning must SAY SO; otherwise the model treats an inference as
    # an established fact. Since 7.154.0 the mark is by CONFIDENCE (`barely corroborated`:
    # seen once and never used) and not by "nobody has signed it": when 103 of 107
    # entries carried the mark it stopped warning about anything.
    assert "barely corroborated" in text.lower(), (
        "una lección vista una sola vez y nunca usada se marca como poco contrastada")


def test_self_memory_never_writes_user_memory() -> None:
    from app import agent_memory_utils as amu
    from app.app_database import app_session
    from app.app_models.memory import UserMemory
    from sqlalchemy import func, select

    async def go():
        await amu.remember(content="El pod wai-engine-api reinicia solo y vuelve.",
                           subject="deployment:wai-engine-api", kind="lesson")
        async with app_session() as s:
            return (await s.execute(select(func.count(UserMemory.id)))).scalar()

    assert _run(go()) == 0, (
        "la memoria del ENTORNO y la de las PERSONAS son ejes separados: "
        "un aprendizaje no puede acabar en la ficha de nadie")


def test_disabled_capability_costs_nothing() -> None:
    """With the capability off the block build must not query at all — the block is
    built on EVERY autonomous run, so "off" has to mean zero cost, not "cheap"."""
    from app import agent_memory_prompt as amp
    import app.config as config

    original = config.AGENT_SELF_MEMORY_ENABLED
    config.AGENT_SELF_MEMORY_ENABLED = False
    try:
        detail = _run(amp.build_agent_memory_block_detail(enqueue_consolidation=False))
        assert not (detail.get("text") or "")
    finally:
        config.AGENT_SELF_MEMORY_ENABLED = original


@pytest.mark.parametrize("subject,expected", [
    ("  Job:Indexing  ", "job:indexing"),
    ("deployment:WAI-Engine-API", "deployment:wai-engine-api"),
    ("", ""),
])
def test_subject_normalisation_is_stable(subject, expected) -> None:
    """The subject is the grouping key of the whole viewer; if it were
    case/whitespace sensitive the same thing would split into several subjects and
    the evidence count would be meaningless."""
    from app.agent_memory_utils import normalize_subject
    assert normalize_subject(subject) == expected
