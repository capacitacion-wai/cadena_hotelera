"""Memory trust model — provenance-based anti-poisoning (v7.29.0).

A persistent memory that any inbound message can plant is a persistent
prompt-injection vector (MINJA-style: planted today, triggered weeks
later). The defence is a TRUST dimension whose CEILING is fixed by the
RUN ORIGIN, server-side — the model can lower a fact's trust but never
raise it. So a "fact" the agent extracts from an inbound channel body is
capped at ``untrusted`` even if the tool argument asks for ``verified``.

Levels (most → least trusted):
  verified   — asserted by the operator through the Memoria UI (REST).
  stated     — said by a tenant user in interactive chat.
  inferred   — produced by an autonomous/watcher run (the agent's own read).
  untrusted  — extracted from an inbound channel message (email/WhatsApp) —
               usable as context, segregated in retrieval, never enough on
               its own to drive a consequential action.

Retrieval weights by trust; consolidation forbids a lower-trust fact from
superseding/deleting a higher-trust one (``can_supersede``).
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

VERIFIED = "verified"
STATED = "stated"
INFERRED = "inferred"
UNTRUSTED = "untrusted"
DISPUTED = "disputed"  # operator-flagged; retrieved but "do not rely" (F4)

# Ordering for ceiling clamping and merge rules. ``disputed`` sits below
# everything usable so it can never win a supersede and is easy to gate.
_RANK = {
    DISPUTED: -1, UNTRUSTED: 0, INFERRED: 1, STATED: 2, VERIFIED: 3,
}
# Retrieval weight per level (0..1) — the ``w_trust`` term's per-fact value.
_WEIGHT = {
    VERIFIED: 1.0, STATED: 0.8, INFERRED: 0.55, UNTRUSTED: 0.3, DISPUTED: 0.1,
}
_VALID = frozenset(_RANK)


def normalize(trust: Optional[str]) -> str:
    """A stored/legacy trust value → a valid level. NULL/unknown = ``inferred``
    (safe middle: not quarantined, not authoritative)."""
    t = (trust or "").strip().lower()
    return t if t in _VALID else INFERRED


def rank(trust: Optional[str]) -> int:
    return _RANK[normalize(trust)]


def weight(trust: Optional[str]) -> float:
    return _WEIGHT[normalize(trust)]


def clamp_to_ceiling(requested: Optional[str], ceiling: str) -> str:
    """Return the requested trust bounded by the origin ceiling. The model
    may ask for LESS trust (e.g. flag its own guess ``inferred``) but never
    MORE than the origin allows. ``disputed`` can only come from the operator
    path, never from a tool argument."""
    ceil = normalize(ceiling)
    if requested is None:
        return ceil
    req = (requested or "").strip().lower()
    if req not in _VALID or req == DISPUTED:
        return ceil
    return req if _RANK[req] <= _RANK[ceil] else ceil


def can_supersede(new_trust: Optional[str], old_trust: Optional[str],
                  *, old_pinned: bool = False) -> bool:
    """Consolidation guardrail: a new fact may supersede/replace an existing
    one ONLY if it is at least as trusted (and never a pinned/operator one
    from a lower-trust source). Blocks the "forget that the limit is X"
    inbound-poisoning delete."""
    if old_pinned and rank(new_trust) < _RANK[VERIFIED]:
        return False
    return rank(new_trust) >= rank(old_trust)


async def trust_ceiling_for_run(run_id: Optional[int]) -> str:
    """Maximum trust a memory written during ``run_id`` may carry, derived
    from the run's ORIGIN (event type / mode) — the model cannot influence
    this. Best-effort; unknown origin → ``inferred`` (never higher)."""
    if not run_id:
        return INFERRED
    try:
        from sqlalchemy import select
        from app.app_database import app_session
        from app.app_models.runtime import AgentRun, Event

        async with app_session() as s:
            run = (await s.execute(
                select(AgentRun).where(AgentRun.id == int(run_id))
            )).scalar_one_or_none()
            if run is None:
                return INFERRED
            if run.event_id:
                etype = (await s.execute(
                    select(Event.type).where(Event.id == run.event_id)
                )).scalar_one_or_none() or ""
                etype = etype.strip().lower()
                # Anything born from an INBOUND channel message is untrusted:
                # we cannot tell "the human asserted X" from "the agent parsed
                # X out of a possibly-hostile body", so we take the safe floor.
                if etype == "external_message":
                    return UNTRUSTED
                # Watcher / cron / automation / maintenance = the agent acting
                # on its own read of the data → inferred.
                return INFERRED
            # No event → interactive chat run. A tenant user is present and
            # speaking for themselves → stated.
            if (run.mode or "").strip().lower() == "chat":
                return STATED
    except Exception:  # pragma: no cover — never block a write
        logger.debug("trust_ceiling_for_run failed for %s", run_id, exc_info=True)
    return INFERRED


def trust_from_legacy_source(source: Optional[str]) -> str:
    """Backfill map for pre-v7.29.0 rows, from the EXISTING ``source`` column
    (better than a flat default): operator→verified, chat→stated, the rest
    (agent/email/whatsapp/unknown)→inferred. Going forward, NEW inbound facts
    get ``untrusted`` from the run-origin ceiling."""
    s = (source or "").strip().lower()
    if s == "operator":
        return VERIFIED
    if s == "chat":
        return STATED
    return INFERRED
