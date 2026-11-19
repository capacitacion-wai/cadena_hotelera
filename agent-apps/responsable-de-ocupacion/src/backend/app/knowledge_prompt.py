"""Inject the DOCUMENT KNOWLEDGE LIBRARY awareness block into the agent prompt.

Unlike ScopedKnowledge (free-text instructions injected whole) or the business
parameters, the library's CONTENT is NOT dumped into the prompt — it is retrieved
on demand with the ``search_knowledge`` / ``list_knowledge_docs`` /
``get_knowledge_doc`` MCP tools. So this block is small on purpose: it makes the
agent AWARE the library exists, lists the in-scope document TITLES (a catalogue,
not their text), and states the governance rules — cite the source, never invent
an official figure, never quote an ``internal`` document to a person, and route
promises about ``sensitive topics`` through an approval.

Appended by ``scoped_knowledge.build_scoped_knowledge_block`` so it reaches every
call site (chat, channels, autonomous) with no extra wiring.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app import config
from app.app_database import app_session
from app.app_models.knowledge_library import KnowledgeDocument
from app.app_models.runtime import AppSetting

logger = logging.getLogger(__name__)

# Operator-defined comma/newline separated list of topics on which the employee
# must NOT commit on its own (promises, discounts, scholarships, refunds, legal /
# medical advice…). Empty by default — the operator fills it in Configuración.
SENSITIVE_TOPICS_KEY = "knowledge_sensitive_topics"

_CATALOGUE_CAP = 40  # max document titles listed (a careless library stays bounded)


async def sensitive_topics() -> list[str]:
    """The operator's configured sensitive topics (deduped, trimmed), or []."""
    try:
        async with app_session() as s:
            raw = (await s.execute(
                select(AppSetting.value).where(AppSetting.key == SENSITIVE_TOPICS_KEY)
            )).scalar_one_or_none()
    except Exception:  # pragma: no cover — never block a run on a read
        logger.debug("sensitive topics read failed", exc_info=True)
        return []
    if not raw:
        return []
    parts = [p.strip() for p in str(raw).replace("\n", ",").split(",")]
    out: list[str] = []
    for p in parts:
        if p and p.lower() not in {x.lower() for x in out}:
            out.append(p)
    return out


def _in_scope(doc: KnowledgeDocument, attending: bool, mu: str) -> bool:
    scope = (doc.scope or "all").strip().lower()
    if scope == "all":
        return True
    if attending:
        if scope == "attending":
            return True
        if scope == "user":
            return bool(mu) and (doc.user_email or "").strip().lower() == mu
        return False
    return scope == "autonomous"


async def build_knowledge_library_block(
    *, attending: bool, memory_user: str | None = None,
) -> str:
    """The library-awareness prompt block for this run, or ``""`` when the library
    is disabled / has no ready, in-scope document."""
    if not config.KNOWLEDGE_LIBRARY_ENABLED:
        return ""
    mu = (memory_user or "").strip().lower()
    try:
        async with app_session() as s:
            rows = list((await s.execute(
                select(KnowledgeDocument)
                .where(KnowledgeDocument.enabled == 1, KnowledgeDocument.status == "ready")
                .order_by(KnowledgeDocument.title.asc())
            )).scalars().all())
    except Exception as exc:  # noqa: BLE001 — never break a run on knowledge
        logger.warning("knowledge library block fetch failed: %s", exc)
        return ""

    docs = [r for r in rows if _in_scope(r, attending, mu)]
    if not docs:
        return ""

    lines = [
        "\n\nREFERENCE MATERIAL (documents the operator has uploaded — "
        "catalogs, rates, policies, requirements, FAQs…). Consult it with the "
        "search_knowledge tool ALWAYS when you are asked something whose "
        "answer must come from official material (a price, a condition, a "
        "requirement, a date) instead of assuming it. Rules:",
        "\n- Answer from what the documents return and **cite the "
        "document** it comes from (its title). Do not invent official figures.",
        "\n- If no document covers it, say so clearly; do not "
        "invent it.",
        "\n- A document marked **internal** is only for your reasoning: do NOT "
        "cite it or forward it to a person; you can only reproduce verbatim "
        "the shareable documents.",
        "\n- Pass your `run_id` when searching; the system already knows whom you are "
        "serving and applies the material scoped to that person automatically.",
    ]

    topics = await sensitive_topics()
    if topics:
        lines.append(
            "\n- SENSITIVE TOPICS (" + ", ".join(topics[:20]) + "): do not "
            "commit on your own. If you are asked for something on these topics "
            "(promising, granting, applying), put it through approval (request_approval) "
            "or refer it to a manager before confirming it."
        )

    lines.append("\nDocuments available now:")
    for r in docs[:_CATALOGUE_CAP]:
        tag = f" — {r.tags}" if (r.tags or "").strip() else ""
        vis = " [internal]" if r.visibility == "internal" else ""
        lines.append(f"\n- {(r.title or '').strip()}{tag}{vis}")
    if len(docs) > _CATALOGUE_CAP:
        lines.append(f"\n- (… and {len(docs) - _CATALOGUE_CAP} more — use search_knowledge …)")

    return "".join(lines) + "\n"
