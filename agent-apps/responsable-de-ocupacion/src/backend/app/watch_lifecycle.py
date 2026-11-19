"""PER-ITEM lifecycle of an automation in ``watch`` mode — PURE logic (stdlib only).

Kept apart from automations_service.py on purpose: that way it is tested without dragging in
FastAPI/SQLAlchemy. It is domain-agnostic (cluster incidents, leads, tickets…): it only compares
sets of items by their ``key`` and decides what to fire.
"""
from __future__ import annotations

from typing import Optional


def key_ignored(key: Optional[str], patterns: list) -> bool:
    """Is this item_key known noise (allowlist)? EXACT match or PREFIX match with a trailing ``*``
    (e.g. ``metabase*`` ignores everything starting with metabase). Domain-agnostic: the
    employee puts third-party noise here (Metabase, presenton, a known 42P01…)."""
    if key is None:
        return False
    for p in patterns:
        p = str(p)
        if p == key or (p.endswith("*") and key.startswith(p[:-1])):
            return True
    return False


def diff_watch_items(stored: dict, current: list, key_field: str,
                     signature_field: Optional[str]) -> dict:
    """Compare the current items against the stored state and decide WHAT to fire.

    ``stored`` = {item_key: {"signature":…, "state":…}} (what was persisted).
    ``current`` = list of dicts (what the tool returns now).
    Returns:
      * ``fresh``     — NEW items, or items that reappear after being resolved (they fire; anti-false-negatives);
      * ``escalated`` — the ``signature`` changed (they fire once);
      * ``ongoing``   — same as before (silence, only the lifecycle is updated);
      * ``resolved``  — they were open and NO longer appear (auto-resolution = "it recovered");
      * ``seen``      — {key: {"item","signature"}} to persist.
    """
    fresh, escalated, ongoing, seen = [], [], [], {}
    for it in current:
        if not isinstance(it, dict):
            it = {"value": it}
        k = it.get(key_field)
        if k is None:
            continue
        k = str(k)
        sig = str(it.get(signature_field)) if signature_field else None
        seen[k] = {"item": it, "signature": sig}
        prev = stored.get(k)
        if prev is None:
            fresh.append(it)
        elif prev.get("state") == "resolved":
            fresh.append(it)                       # reappeared after being resolved → fire again
        elif signature_field and prev.get("signature") != sig:
            escalated.append(it)
        else:
            ongoing.append(k)
    resolved = [k for k, v in stored.items() if v.get("state") != "resolved" and k not in seen]
    return {"fresh": fresh, "escalated": escalated, "ongoing": ongoing,
            "resolved": resolved, "seen": seen}
