"""Conditional silences — "normal on Sundays during the backup, but warn me on a
Tuesday" (v7.140.0).

A silence with a condition is still ONE silence (same table, same gate, same
manager); this module is the deterministic part: does the condition hold for
THIS candidate signal, and what would it have done to the signals we already
have? Three pieces:

* ``matches`` — the decision, deterministic and side-effect free. Never consults
  the model: a rule that changes behaviour must not depend on prose being read
  the same way twice.
* ``preview`` — the "Probar" in dry-run over the last N REAL signals. This is the
  only honest way to approve something conditional: you don't read the
  condition, you see its effect ("of the last 40 signals it would have hit 6 —
  here they are"). Cost 0, writes nothing. Same idea as ``test_automation``.
* ``schedule_reevaluation`` — the ``defer`` effect: instead of silencing, park an
  Encargo to re-check later, so "wait and see, and warn me if it persists" is
  expressible. It rides the scheduled-items planner that already exists, not a
  new cron.

Condition shapes, all JSON in ``signal_suppressions.condition_json`` and
deliberately the SAME vocabulary as an automation's "solo si":

    {"kind": "always"}
    {"kind": "weekday",     "days": [6, 7]}                # ISO: Mon=1 … Sun=7
    {"kind": "time_window", "from": "01:00", "to": "05:30"}
    {"kind": "field",       "field": "target_entity_id",
                            "op": "eq|neq|contains|gt|lt", "value": "pabisan"}
    {"kind": "all"|"any",   "of": [ …nested… ]}

Plus two modifiers on the rule itself: ``severity_max`` (a HARD guard — a
critical signal is never silenced) and ``effect`` inside the condition
(``skip`` default, or ``defer`` with ``defer_minutes``).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.app_models.signal_suppressions import SignalSuppression

logger = logging.getLogger(__name__)

EFFECT_SKIP = "skip"
EFFECT_DEFER = "defer"

# Severity ladder shared with the signal surfaces. A rule's ``severity_max`` is
# the HIGHEST severity it is allowed to silence; anything above always gets
# through, no matter how well the pattern matched.
_SEVERITY_RANK = {
    "info": 1, "informativa": 1, "low": 1, "baja": 1,
    "medium": 2, "media": 2, "warning": 2, "aviso": 2,
    "high": 3, "alta": 3,
    "critical": 4, "critica": 4, "crítica": 4, "urgent": 4, "urgente": 4,
}


def _get(row: Any, attr: str):
    return row.get(attr) if isinstance(row, dict) else getattr(row, attr, None)


def severity_rank(value: Any) -> int:
    return _SEVERITY_RANK.get(str(value or "").strip().lower(), 2)


def parse_condition(raw) -> dict | None:
    """Tolerant parse of ``condition_json``. A malformed condition returns None,
    which the gate reads as "do not silence" — the safe direction: a broken rule
    must let the signal through, never swallow it."""
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def condition_effect(cond: dict | None) -> str:
    eff = str((cond or {}).get("effect") or EFFECT_SKIP).strip().lower()
    return eff if eff in (EFFECT_SKIP, EFFECT_DEFER) else EFFECT_SKIP


def _parse_hhmm(value: Any) -> tuple[int, int] | None:
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(value or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h, mi
    return None


def _cmp(op: str, left: Any, right: Any) -> bool:
    op = (op or "eq").strip().lower()
    ls, rs = str(left or "").strip().lower(), str(right or "").strip().lower()
    if op in ("eq", "="):
        return ls == rs
    if op in ("neq", "!="):
        return ls != rs
    if op == "contains":
        return bool(rs) and rs in ls
    if op in ("gt", ">", "lt", "<", "gte", ">=", "lte", "<="):
        try:
            lf, rf = float(left), float(right)
        except (TypeError, ValueError):
            return False
        if op in ("gt", ">"):
            return lf > rf
        if op in ("lt", "<"):
            return lf < rf
        if op in ("gte", ">="):
            return lf >= rf
        return lf <= rf
    return False


def _eval(cond: dict, row: Any, now: datetime) -> tuple[bool, str]:
    """Evaluate one condition node. Returns (holds, human reason)."""
    kind = str(cond.get("kind") or "always").strip().lower()

    if kind == "always":
        return True, "siempre"

    if kind in ("all", "any"):
        nodes = [n for n in (cond.get("of") or []) if isinstance(n, dict)]
        if not nodes:
            return False, "condición vacía"
        results = [_eval(n, row, now) for n in nodes]
        if kind == "all":
            holds = all(r[0] for r in results)
        else:
            holds = any(r[0] for r in results)
        joined = (" y " if kind == "all" else " o ").join(r[1] for r in results)
        return holds, joined

    if kind == "weekday":
        # ISO weekday so "Sunday" is 7 for everyone, and in the BUSINESS
        # timezone — a naive-UTC weekday is wrong for a late-Sunday event west
        # of Greenwich, which is exactly when "it's normal during the backup"
        # would misfire.
        days = {int(d) for d in (cond.get("days") or []) if str(d).isdigit()}
        local = _to_local(now)
        holds = local.isoweekday() in days if days else False
        return holds, f"día de la semana {local.isoweekday()} en {sorted(days)}"

    if kind == "time_window":
        start = _parse_hhmm(cond.get("from"))
        end = _parse_hhmm(cond.get("to"))
        if not start or not end:
            return False, "ventana horaria inválida"
        local = _to_local(now)
        cur = local.hour * 60 + local.minute
        a, b = start[0] * 60 + start[1], end[0] * 60 + end[1]
        # A window may cross midnight (01:00→05:30 is normal, 23:00→02:00 too).
        holds = (a <= cur <= b) if a <= b else (cur >= a or cur <= b)
        return holds, f"hora {local:%H:%M} en {cond.get('from')}-{cond.get('to')}"

    if kind == "field":
        field = str(cond.get("field") or "").strip()
        if not field:
            return False, "campo no indicado"
        left = _get(row, field)
        if left is None and isinstance(row, dict):
            left = (row.get("metadata") or {}).get(field) if isinstance(
                row.get("metadata"), dict) else None
        holds = _cmp(str(cond.get("op") or "eq"), left, cond.get("value"))
        return holds, f"{field} {cond.get('op') or 'eq'} {cond.get('value')}"

    return False, f"tipo de condición desconocido ({kind})"


def _to_local(now: datetime) -> datetime:
    try:
        from app.timeutil import to_local
        return to_local(now)
    except Exception:  # pragma: no cover — timezone helper is best-effort
        return now


def scope_matches(rule: SignalSuppression, table: str, row: Any,
                  key: str | None) -> bool:
    """Does this rule even apply to this signal? Exactly one of ``match_key``
    (exact identity, today's semantics — untouched) or ``subject_pattern`` (a
    scope, e.g. every alert whose identity mentions a tenant)."""
    if (rule.signal_table or "") != (table or ""):
        return False
    pattern = (getattr(rule, "subject_pattern", None) or "").strip().lower()
    if pattern:
        haystack = " ".join(
            str(x or "").lower() for x in (
                key, _get(row, "title"), _get(row, "target_entity_id"),
                _get(row, "target_entity_kind"), _get(row, "dedupe_key"),
            )
        )
        # Simple glob: '*' is the only wildcard operators need, and it keeps the
        # pattern something a human can read in the manager.
        if "*" in pattern:
            rx = "^" + ".*".join(re.escape(p) for p in pattern.split("*")) + "$"
            return re.search(rx, haystack) is not None or any(
                re.match(rx, tok) for tok in haystack.split()
            )
        return pattern in haystack
    return bool(key) and (rule.match_key or "") == key


def in_force(rule: SignalSuppression, now: datetime | None = None) -> bool:
    """Only an ACTIVE rule silences. A ``proposed`` one — what the employee asked
    for — silences NOTHING until an admin accepts it. This is the whole
    anti-self-escalation guarantee, and it lives here so no caller can forget it.
    """
    if not rule.enabled:
        return False
    if (getattr(rule, "review_state", "active") or "active") != "active":
        return False
    now = now or datetime.now(timezone.utc)
    for stamp in (rule.muted_until, getattr(rule, "expires_at", None)):
        if stamp is not None:
            aware = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
            if aware <= now:
                return False
    return True


def matches(rule: SignalSuppression, table: str, row: Any,
            key: str | None = None, now: datetime | None = None) -> dict | None:
    """Decide whether ``rule`` silences this candidate signal.

    Returns None when it does not apply, else
    ``{"effect", "reason", "defer_minutes"}``. Deterministic and side-effect
    free, so ``preview`` can replay it over history and get exactly what the
    gate would have done.
    """
    now = now or datetime.now(timezone.utc)
    if not in_force(rule, now):
        return None
    if not scope_matches(rule, table, row, key):
        return None

    # HARD GUARD, evaluated before the condition: a signal above the rule's
    # ``severity_max`` is never silenced. A critical alert reaches the human even
    # if the pattern matched perfectly — that promise cannot depend on a
    # condition being written correctly.
    cap = (getattr(rule, "severity_max", None) or "").strip()
    if cap:
        if severity_rank(_get(row, "severity")) > severity_rank(cap):
            return None

    cond = parse_condition(getattr(rule, "condition_json", None))
    if cond is None:
        # Classic identity silence: no condition to evaluate.
        return {"effect": EFFECT_SKIP, "reason": "identidad silenciada",
                "defer_minutes": 0}
    holds, why = _eval(cond, row, now)
    if not holds:
        return None
    effect = condition_effect(cond)
    try:
        defer_minutes = int(cond.get("defer_minutes") or 60)
    except (TypeError, ValueError):
        defer_minutes = 60
    return {"effect": effect, "reason": why,
            "defer_minutes": max(5, min(defer_minutes, 60 * 24 * 7))}


async def find_matching_rule(session, table: str, row: Any,
                             key: str | None) -> tuple[SignalSuppression, dict] | None:
    """The conditional half of the gate: scan the rules that COULD apply to this
    table and return the first that matches.

    Bounded on purpose: only rules that are enabled + active are read (indexed by
    ``ix_signal_suppressions_pattern``), and a real app has tens of them, not
    thousands. The exact-identity path stays a direct indexed lookup in
    ``signal_suppress`` — this one only runs for pattern rules.
    """
    rows = list((await session.execute(
        select(SignalSuppression).where(
            SignalSuppression.signal_table == table,
            SignalSuppression.enabled == True,  # noqa: E712
            SignalSuppression.review_state == "active",
            SignalSuppression.subject_pattern.isnot(None),
        )
    )).scalars().all())
    now = datetime.now(timezone.utc)
    for rule in rows:
        verdict = matches(rule, table, row, key, now)
        if verdict is not None:
            return rule, verdict
    return None


async def schedule_reevaluation(rule: SignalSuppression, table: str, row: Any,
                                defer_minutes: int, label: str) -> int | None:
    """The ``defer`` effect: don't warn now, but park an Encargo to look again.

    Uses the scheduled-items planner that already exists (the employee's own
    follow-up rail) instead of a new cron: the operator sees a normal Encargo,
    and if the thing is still there when it comes due, the signal is raised then.
    """
    try:
        import hashlib
        from app.app_database import app_write
        from app.app_models.business import ScheduledItem
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        due = datetime.utcnow() + timedelta(minutes=max(5, int(defer_minutes)))
        # Bucketed key: several repeats inside the same window must produce ONE
        # follow-up, not a queue of identical ones.
        bucket = int(due.timestamp()) // (max(5, int(defer_minutes)) * 60)
        key = "sigdefer-" + hashlib.sha1(
            f"{rule.id}|{table}|{label}|{bucket}".encode()
        ).hexdigest()[:24]
        async with app_write() as s:
            await s.execute(
                sqlite_insert(ScheduledItem).values(
                    idempotency_key=key,
                    title=f"Revisar si sigue ocurriendo: {label}"[:200],
                    description=(
                        f"El silencio «{rule.match_label or rule.match_key}» pidió "
                        "esperar y reevaluar en vez de avisar. Comprueba si la "
                        "situación persiste: si sigue, avisa; si se resolvió, "
                        "ciérralo sin ruido."
                    ),
                    kind="followup", assignee="agent", origin="followup",
                    status="pending", priority="medium", due_at=due,
                    created_by="agent", metadata_json=json.dumps(
                        {"suppression_id": rule.id, "signal_table": table},
                        ensure_ascii=False,
                    ),
                ).on_conflict_do_nothing(index_elements=[ScheduledItem.idempotency_key])
            )
            await s.commit()
            sid = (await s.execute(
                select(ScheduledItem.id).where(ScheduledItem.idempotency_key == key)
            )).scalar()
        return int(sid) if sid else None
    except Exception:  # pragma: no cover — deferral must never break a write
        logger.debug("deferred re-evaluation scheduling failed", exc_info=True)
        return None


# ── The "Probar" (dry run) ────────────────────────────────────────────────
_PREVIEW_TABLES = {
    "alerts": ("app.app_models.business", "Alert",
               ("title", "severity", "dedupe_key", "target_entity_kind",
                "target_entity_id", "created_at")),
    "recommendations": ("app.app_models.business", "Recommendation",
                        ("title", "severity", "target_entity_kind",
                         "target_entity_id", "created_at")),
    "decisions": ("app.app_models.business", "Decision",
                  ("decision", "ref_kind", "ref_id", "created_at")),
}


async def preview(spec: dict, last_n: int | None = None) -> dict:
    """Replay a candidate silence over the last N REAL signals and report which
    ones it would have affected. Reads only; creates and modifies nothing.

    ``spec`` is the shape the manager (or the employee's ``propose_silence``)
    would save: ``{signal_table, match_key?, subject_pattern?, condition?,
    severity_max?}``. The point is that an operator approves conditional
    behaviour by seeing its EFFECT, not by parsing a JSON condition.
    """
    from app.app_database import app_session
    from app.config import SIGNAL_CONDITION_PREVIEW_LAST_N
    table = str(spec.get("signal_table") or "alerts").strip().lower()
    entry = _PREVIEW_TABLES.get(table)
    if entry is None:
        return {"ok": False, "error": f"tabla no soportada: {table}"}
    limit = max(1, min(int(last_n or SIGNAL_CONDITION_PREVIEW_LAST_N), 200))

    # A THROW-AWAY, unsaved rule: the preview must never touch the DB, and
    # building it this way guarantees it goes through exactly the same
    # ``matches`` code path the gate uses (otherwise the test would be a
    # different program than the thing being tested).
    cond = spec.get("condition")
    probe = SignalSuppression(
        signal_table=table,
        match_key=str(spec.get("match_key") or ""),
        match_label=str(spec.get("match_label") or ""),
        subject_pattern=(str(spec.get("subject_pattern")).strip()
                         if spec.get("subject_pattern") else None),
        condition_json=(json.dumps(cond, ensure_ascii=False)
                        if isinstance(cond, dict) else None),
        severity_max=(str(spec.get("severity_max")).strip()
                      if spec.get("severity_max") else None),
        enabled=True, review_state="active", suppress_count=0,
    )

    module_name, class_name, _cols = entry
    import importlib
    Model = getattr(importlib.import_module(module_name), class_name)
    from app.signal_suppress import suppression_key, signal_label
    now = datetime.now(timezone.utc)
    hits: list[dict] = []
    checked = 0
    try:
        async with app_session() as s:
            rows = list((await s.execute(
                select(Model).order_by(Model.created_at.desc()).limit(limit)
            )).scalars().all())
        for row in rows:
            checked += 1
            key = suppression_key(table, row)
            verdict = matches(probe, table, row, key, now)
            if verdict is None:
                continue
            hits.append({
                "id": getattr(row, "id", None),
                "label": signal_label(table, row),
                "severity": getattr(row, "severity", None),
                "created_at": (row.created_at.isoformat()
                               if getattr(row, "created_at", None) else None),
                "effect": verdict["effect"],
                "reason": verdict["reason"],
            })
    except Exception as exc:  # pragma: no cover
        logger.debug("silence preview failed", exc_info=True)
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True, "signal_table": table, "checked": checked,
        "would_affect": len(hits), "hits": hits[:50],
        "summary": (
            f"De las últimas {checked} señales, este silencio habría afectado a "
            f"{len(hits)}."
        ),
    }
