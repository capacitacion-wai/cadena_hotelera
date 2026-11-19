"""Inject operator-added KNOWLEDGE/INSTRUCTIONS into the agent prompt, filtered
by the run's scope.

Scopes (see ``app_models.runtime.ScopedKnowledge``):
  * ``all``        — every run.
  * ``autonomous`` — background runs (watchers/cron): no human is being served.
  * ``attending``  — serving a human (chat OR an inbound channel message).
  * ``user``       — a specific human, matched on ``user_email`` == ``memory_user``.

Call sites pass an explicit ``attending`` flag + the ``memory_user`` (the HUMAN
being served, if any) because that is exactly the per-run context the four
scopes need — see ``agent_sessions.SessionScope`` / ``channels.executor`` /
``autonomous.executor``.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.app_database import app_session
from app.app_models.runtime import KnowledgeRequest, ScopedKnowledge

logger = logging.getLogger(__name__)

# Soft cap so a careless operator can't blow the context window with knowledge.
_BLOCK_CHAR_BUDGET = 6_000


async def build_scoped_knowledge_block(
    *, attending: bool, memory_user: str | None = None,
) -> str:
    """Return the operator-knowledge block for this run, or ``""`` when none
    applies. ``attending`` = a human is being served (chat / inbound channel);
    ``memory_user`` = that human's email (for ``user``-scoped entries)."""
    mu = (memory_user or "").strip().lower()
    # Tags this run matches (besides per-user, handled separately).
    tags = {"all", "attending"} if attending else {"all", "autonomous"}
    try:
        async with app_session() as s:
            rows = list((await s.execute(
                select(ScopedKnowledge)
                .where(ScopedKnowledge.enabled == 1)
                .order_by(ScopedKnowledge.position.asc(), ScopedKnowledge.id.asc())
            )).scalars().all())
    except Exception as exc:  # noqa: BLE001 — never break a run on knowledge
        logger.warning("scoped knowledge fetch failed: %s", exc)
        return ""

    matched = []
    for r in rows:
        scope = (r.scope or "all").strip().lower()
        if scope == "user":
            if attending and mu and (r.user_email or "").strip().lower() == mu:
                matched.append(r)
        elif scope in tags:
            matched.append(r)

    scoped_part = ""
    if matched:
        lines = [
            "\n\nKNOWLEDGE AND INSTRUCTIONS (defined by the operator — follow them):",
        ]
        used = 0
        for r in matched:
            title = (r.title or "").strip() or "(untitled)"
            content = (r.content or "").strip()
            if not content:
                continue
            chunk = f"\n**{title}**\n{content}"
            if used + len(chunk) > _BLOCK_CHAR_BUDGET:
                lines.append("\n(… additional knowledge omitted due to space limit …)")
                break
            lines.append(chunk)
            used += len(chunk)
        scoped_part = "\n".join(lines) + "\n"

    # Business PARAMETERS the operator answered (global, always injected) — the
    # values the agent asked for via request_knowledge. Appended for EVERY run so
    # the agent treats them as the source of truth for thresholds/criteria.
    # Then the DOCUMENT LIBRARY awareness block (catalogue + rules; content is
    # retrieved on demand via search_knowledge, not dumped here).
    from app.knowledge_prompt import build_knowledge_library_block
    library_part = await build_knowledge_library_block(
        attending=attending, memory_user=mu,
    )
    return scoped_part + await build_business_knowledge_block() + library_part


async def build_business_knowledge_block() -> str:
    """The CONFIRMED business parameters (``KnowledgeRequest`` with a value) as a
    prompt block, or ``""`` when none. Global — applies to every run; the agent
    reads these instead of guessing thresholds/criteria and requests new ones
    with ``request_knowledge`` when a needed value is missing."""
    try:
        async with app_session() as s:
            rows = list((await s.execute(
                select(KnowledgeRequest)
                .where(KnowledgeRequest.status == "provided")
                .order_by(KnowledgeRequest.label.asc(), KnowledgeRequest.id.asc())
            )).scalars().all())
    except Exception as exc:  # noqa: BLE001 — never break a run on knowledge
        logger.warning("business knowledge fetch failed: %s", exc)
        return ""

    lines = []
    used = 0
    for r in rows:
        val = (r.value or "").strip()
        if not val:
            continue
        unit = f" {r.unit}".rstrip() if (r.unit or "").strip() else ""
        chunk = f"\n- **{(r.label or r.key).strip()}** (`{r.key}`): {val}{unit}"
        if used + len(chunk) > _BLOCK_CHAR_BUDGET:
            break
        lines.append(chunk)
        used += len(chunk)
    if not lines:
        return ""
    header = (
        "\n\nBUSINESS PARAMETERS (given by the operator — they are the SOURCE OF TRUTH "
        "for thresholds, limits and criteria; use them, do not invent them. If you need "
        "a piece of data that is NOT here, ask for it with the request_knowledge tool):"
    )
    return header + "".join(lines) + "\n"
