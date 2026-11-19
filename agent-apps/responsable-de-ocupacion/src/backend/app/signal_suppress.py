"""v7.64.0 — deterministic SIGNAL SUPPRESSION ("no repetir") enforcement + helpers.

The loop-killer: when the employee tries to create an alert / recommendation /
decision whose IDENTITY is muted by an active suppression, the platform SKIPS it
SERVER-SIDE (never relying on the prompt) and reports it, so the agent stops
re-raising the same thing. Scope = the EXACT recurring identity (a signal's stable
key), never a broad category. Expiry is evaluated at the gate (no cron/events —
same "materialize on read" philosophy as the rest of the app).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from app.app_models.signal_suppressions import SignalSuppression
from app.timeutil import iso_utc

logger = logging.getLogger(__name__)

SUPPRESSIBLE_TABLES = ("alerts", "recommendations", "decisions")
# Cap on how long the AGENT (not a human) may mute something. A PERMANENT mute is a
# human-only decision — the employee must not silence a signal forever by mistake.
AGENT_MAX_SNOOZE_DAYS = 30
_KIND_TO_TABLE = {"alert": "alerts", "recommendation": "recommendations", "decision": "decisions"}
_TABLE_TO_PREFIX = {"alerts": "alert", "recommendations": "rec", "decisions": "dec"}


def _get(row: Any, attr: str):
    return row.get(attr) if isinstance(row, dict) else getattr(row, attr, None)


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())[:160]


def _meta_dedupe_key(row: Any) -> Optional[str]:
    """A ``dedupe_key`` the agent may stash in ``metadata_json`` to give recs /
    decisions a robust identity (no schema change on those tables)."""
    raw = _get(row, "metadata_json")
    if not raw:
        return None
    try:
        m = json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
        v = (m or {}).get("dedupe_key")
        return _norm(v) if v else None
    except Exception:
        return None


def suppression_key(table: str, row: Any) -> Optional[str]:
    """Stable identity of a signal row for matching. Deterministic, so the SAME
    recurring signal always maps to the SAME key across runs.

    * alerts → its ``dedupe_key`` (already target+cause); fallback target+title.
    * recommendations → explicit metadata ``dedupe_key``, else target+title.
    * decisions → ``ref_kind:ref_id``, else metadata key, else normalized reason.
    ``row`` is an ORM object OR a plain dict (raw SQL row)."""
    t = (table or "").lower()
    if t == "alerts":
        dk = _norm(_get(row, "dedupe_key"))
        if dk:
            return f"alert:{dk}"
        tgt = f"{_norm(_get(row, 'target_entity_kind'))}:{_norm(_get(row, 'target_entity_id'))}"
        return f"alert:{tgt}:{_norm(_get(row, 'title'))}"
    if t == "recommendations":
        dk = _meta_dedupe_key(row)
        if dk:
            return f"rec:{dk}"
        tgt = f"{_norm(_get(row, 'target_entity_kind'))}:{_norm(_get(row, 'target_entity_id'))}"
        return f"rec:{tgt}:{_norm(_get(row, 'title'))}"
    if t == "decisions":
        rk, rid = _norm(_get(row, "ref_kind")), _norm(_get(row, "ref_id"))
        if rk and rid:
            return f"dec:{rk}:{rid}"
        dk = _meta_dedupe_key(row)
        if dk:
            return f"dec:{dk}"
        reason = _norm(_get(row, "reason_md") or _get(row, "reason"))
        return f"dec:{_norm(_get(row, 'decision'))}:{reason[:80]}"
    return None


def signal_label(table: str, row: Any) -> str:
    """Human-readable label for the muted signal, stored on the rule so the list
    reads ('Stock bajo — Producto X') instead of a raw key."""
    if (table or "").lower() == "decisions":
        base = _get(row, "reason_md") or _get(row, "reason") or _get(row, "decision") or "decisión"
    else:
        base = _get(row, "title") or "señal"
    return re.sub(r"\s+", " ", str(base).strip())[:120]


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def is_active(rule: SignalSuppression, now: Optional[datetime] = None) -> bool:
    """Enabled, IN FORCE and not expired. A NULL ``muted_until`` is forever.

    v7.140.0 — delegates to ``signal_conditions.in_force``, which also refuses a
    ``proposed`` rule (what the EMPLOYEE asked for silences nothing until an
    admin accepts it) and honours ``expires_at``. One definition of "in force",
    so the gate and the preview can never disagree.
    """
    from app.signal_conditions import in_force
    return in_force(rule, now)


async def find_active_suppression(session, table: str, key: str) -> Optional[SignalSuppression]:
    if not key:
        return None
    rows = (await session.execute(
        select(SignalSuppression).where(
            SignalSuppression.signal_table == table,
            SignalSuppression.match_key == key,
            SignalSuppression.enabled == True,  # noqa: E712
        )
    )).scalars().all()
    now = datetime.now(timezone.utc)
    for r in rows:
        if is_active(r, now):
            return r
    return None


async def _log_silence(session, rule: SignalSuppression, table: str, row: Any,
                       key: Optional[str], effect: str, reason: str) -> None:
    """Append the NON-ACTION row — "why didn't it warn me?".

    The signal was never created, so there is nothing to join to later: the label
    and severity are denormalized here on purpose, or the feed would be
    unreadable. Best-effort; a logging failure must never turn into a signal
    being created twice.
    """
    try:
        from app.app_models.signal_silence_log import SignalSilenceLog
        payload = None
        try:
            payload = json.dumps({
                k: str(_get(row, k))[:200] for k in
                ("title", "severity", "target_entity_kind", "target_entity_id",
                 "decision", "ref_kind", "ref_id")
                if _get(row, k) is not None
            }, ensure_ascii=False)[:2000]
        except Exception:  # pragma: no cover
            payload = None
        session.add(SignalSilenceLog(
            suppression_id=rule.id, signal_table=table, match_key=key,
            label=signal_label(table, row),
            severity=(str(_get(row, "severity")) if _get(row, "severity") else None),
            effect_applied=effect, reason=(reason or "")[:200],
            payload_summary=payload,
            run_id=_get(row, "created_by_run_id"),
        ))
    except Exception:  # pragma: no cover
        logger.debug("silence log write failed", exc_info=True)


async def gate_signal_creation(session, table: str, row: Any) -> Optional[dict]:
    """If a NEW signal is muted by an active suppression, record the hit and return
    the ``suppressed`` result the tool must return INSTEAD of creating the row. Else
    return None (let the creation proceed). Best-effort: never raises.

    Two paths, cheapest first: the EXACT-IDENTITY lookup (an indexed point read,
    today's behaviour) and, only if that misses, the CONDITIONAL rules (pattern
    scope + condition, evaluated deterministically). Either way the inhibition is
    logged, so behaviour is audited by effects instead of by reading rules.
    """
    if (table or "").lower() not in SUPPRESSIBLE_TABLES:
        return None
    try:
        key = suppression_key(table, row)
        rule = await find_active_suppression(session, table, key) if key else None
        verdict = {"effect": "skip", "reason": "identidad silenciada",
                   "defer_minutes": 0}
        if rule is None:
            from app.signal_conditions import find_matching_rule
            found = await find_matching_rule(session, table, row, key)
            if found is None:
                return None
            rule, verdict = found
        rule.suppress_count = (rule.suppress_count or 0) + 1
        rule.last_suppressed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        effect = verdict.get("effect") or "skip"
        await _log_silence(session, rule, table, row, key, effect,
                           verdict.get("reason") or "")
        await session.commit()
        deferred_id = None
        if effect == "defer":
            from app.signal_conditions import schedule_reevaluation
            deferred_id = await schedule_reevaluation(
                rule, table, row, int(verdict.get("defer_minutes") or 60),
                signal_label(table, row),
            )
        msg = ("Not created: there is an active DO NOT REPEAT rule for this "
               f"signal («{rule.match_label or key}»). Do not try it again; "
               "it is silenced on purpose.")
        if effect == "defer":
            msg = ("Not created now: the rule asks to WAIT AND RE-EVALUATE "
                   f"(«{rule.match_label or key}»). A task has been left to check "
                   "whether it persists; if it does, it will be notified then.")
        return {
            "ok": True,
            "suppressed": True,
            "id": None,
            "rule_id": rule.id,
            "reason": rule.reason,
            "condition_reason": verdict.get("reason"),
            "effect": effect,
            "deferred_item_id": deferred_id,
            "muted_until": iso_utc(rule.muted_until),
            "message": msg,
        }
    except Exception:  # pragma: no cover — a gate failure must never break a write
        try:
            await session.rollback()
        except Exception:
            pass
        logger.debug("suppression gate failed for %s", table, exc_info=True)
        return None


async def create_suppression(
    session, *, table: str, key: str, label: str, muted_until: Optional[datetime],
    reason: str, created_by: str, created_by_run_id: Optional[int] = None,
    source_signal_id: Optional[int] = None,
    subject_pattern: Optional[str] = None, condition: Optional[dict] = None,
    severity_max: Optional[str] = None, expires_at: Optional[datetime] = None,
    review_state: str = "active",
) -> SignalSuppression:
    """Create — or RE-ACTIVATE/refresh — a suppression. If an ENABLED-or-disabled
    rule for the same (table, key) already exists, refresh its window/reason instead
    of stacking duplicates (so re-silencing the same thing is idempotent).

    v7.140.0 — the optional ``subject_pattern`` + ``condition`` make it a silence
    WITH A CONDITION (same concept, extra columns), and ``review_state='proposed'``
    is how the employee ASKS for one without granting it: the gate ignores a
    proposed rule entirely.
    """
    # A PATTERN rule has no exact identity, so the dedup below (which keys on
    # ``match_key``) must not fold two different patterns into one row.
    pattern = (subject_pattern or "").strip() or None
    existing = None if pattern else (await session.execute(
        select(SignalSuppression).where(
            SignalSuppression.signal_table == table,
            SignalSuppression.match_key == key,
        ).order_by(SignalSuppression.id.desc()).limit(1)
    )).scalar_one_or_none()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    mu = (muted_until.replace(tzinfo=None)
          if isinstance(muted_until, datetime) and muted_until.tzinfo else muted_until)
    if existing is not None:
        # v7.68.0 — the AGENT may only STRENGTHEN an existing mute, never weaken a
        # human decision: (a) don't silently re-enable a mute a person turned OFF
        # (they chose to allow the signal again); (b) never shrink a human's
        # PERMANENT (NULL) mute or a later expiry down to the agent's bounded snooze.
        # A human (UI/route) keeps full freedom to tighten or loosen.
        is_agent = (created_by or "") == "agent"
        if is_agent and not existing.enabled:
            return existing
        existing.enabled = True
        if is_agent:
            # v7.83.0 — the agent may only EXTEND a bounded window to a LATER bounded
            # expiry. A ``mu=None`` (forever) request is IGNORED — a permanent mute is a
            # human-only decision — and a stronger existing window (forever, or a later
            # expiry) is kept. Defense-in-depth: never assign ``muted_until=None`` here.
            if mu is not None and existing.muted_until is not None and mu > existing.muted_until:
                existing.muted_until = mu
        else:
            existing.muted_until = mu
        existing.reason = reason or existing.reason
        existing.match_label = label or existing.match_label
        existing.updated_at = now
        if source_signal_id and not existing.source_signal_id:
            existing.source_signal_id = source_signal_id
        return existing
    exp = (expires_at.replace(tzinfo=None)
           if isinstance(expires_at, datetime) and expires_at.tzinfo else expires_at)
    row = SignalSuppression(
        signal_table=table, match_key=key, match_label=label,
        muted_until=mu, reason=reason or None,
        created_by=created_by or None, created_by_run_id=created_by_run_id,
        source_signal_id=source_signal_id, enabled=True,
        suppress_count=0, created_at=now, updated_at=now,
        review_state=(review_state if review_state in ("active", "proposed") else "active"),
        subject_pattern=pattern,
        condition_json=(json.dumps(condition, ensure_ascii=False)
                        if isinstance(condition, dict) else None),
        severity_max=((severity_max or "").strip() or None),
        expires_at=exp,
    )
    session.add(row)
    return row
