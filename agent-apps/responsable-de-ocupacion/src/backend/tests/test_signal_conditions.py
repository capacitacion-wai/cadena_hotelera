"""v7.140.0 — CONDITIONAL silences ("normal on Sunday during the backup").

A silence is the one learned rule that changes what the human SEES, so every
guarantee here is about not swallowing something that matters:

* a ``proposed`` rule (what the employee asked for) silences NOTHING until an
  admin accepts it — the anti-self-escalation promise;
* ``severity_max`` is a HARD cap evaluated BEFORE the condition: a critical alert
  is never silenced, however well the pattern matched;
* a malformed condition lets the signal THROUGH (fail-open for visibility);
* an expired rule stops silencing on its own;
* ``preview`` replays the real gate over history and writes nothing, which is the
  only honest way to approve conditional behaviour.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _rule(**over):
    from app.app_models.signal_suppressions import SignalSuppression
    base = dict(signal_table="alerts", match_key="", match_label="prueba",
                enabled=True, review_state="active", suppress_count=0,
                subject_pattern="indexing")
    base.update(over)
    return SignalSuppression(**base)


def _signal(**over):
    row = {"title": "Barrido de indexing lento", "severity": "medium",
           "dedupe_key": "indexing-sweep", "target_entity_kind": "job",
           "target_entity_id": "indexing"}
    row.update(over)
    return row


def test_proposed_rule_silences_nothing() -> None:
    from app import signal_conditions as sc
    rule = _rule(review_state="proposed")
    assert sc.in_force(rule) is False
    assert sc.matches(rule, "alerts", _signal()) is None, (
        "una regla propuesta por el empleado no puede silenciar antes de que un "
        "administrador la acepte")


def test_severity_cap_is_a_hard_guard() -> None:
    from app import signal_conditions as sc
    rule = _rule(severity_max="media",
                 condition_json=json.dumps({"kind": "always"}))
    assert sc.matches(rule, "alerts", _signal(severity="medium")) is not None
    assert sc.matches(rule, "alerts", _signal(severity="critical")) is None, (
        "una alerta crítica llega SIEMPRE al humano, aunque el patrón encaje")


def test_broken_condition_lets_the_signal_through() -> None:
    from app import signal_conditions as sc
    # Unparseable JSON → parse_condition None → classic identity silence, which
    # this pattern rule does not have… and an unknown kind never holds.
    rule = _rule(condition_json='{"kind": "sin_sentido"}')
    assert sc.matches(rule, "alerts", _signal()) is None
    assert sc.parse_condition("no es json") is None


def test_expired_rule_stops_silencing() -> None:
    from app import signal_conditions as sc
    past = datetime.utcnow() - timedelta(hours=1)
    rule = _rule(condition_json=json.dumps({"kind": "always"}), expires_at=past)
    assert sc.matches(rule, "alerts", _signal()) is None
    future = datetime.utcnow() + timedelta(hours=1)
    rule.expires_at = future
    assert sc.matches(rule, "alerts", _signal()) is not None


def test_time_window_crossing_midnight() -> None:
    from app import signal_conditions as sc
    rule = _rule(condition_json=json.dumps(
        {"kind": "time_window", "from": "23:00", "to": "02:00"}))
    inside = datetime(2026, 7, 27, 0, 30, tzinfo=timezone.utc)
    outside = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    # A backup window normally crosses midnight; treating from>to as empty would
    # silently disable the rule.
    assert sc.matches(rule, "alerts", _signal(), None, inside) is not None
    assert sc.matches(rule, "alerts", _signal(), None, outside) is None


def test_field_and_composite_conditions() -> None:
    from app import signal_conditions as sc
    rule = _rule(condition_json=json.dumps({
        "kind": "all",
        "of": [
            {"kind": "field", "field": "target_entity_id", "op": "eq",
             "value": "indexing"},
            {"kind": "field", "field": "severity", "op": "neq",
             "value": "critical"},
        ],
    }))
    assert sc.matches(rule, "alerts", _signal()) is not None
    assert sc.matches(rule, "alerts", _signal(target_entity_id="otro")) is None


def test_scope_pattern_vs_exact_identity() -> None:
    from app import signal_conditions as sc
    exact = _rule(subject_pattern=None, match_key="indexing-sweep")
    assert sc.matches(exact, "alerts", _signal(), "indexing-sweep") is not None
    assert sc.matches(exact, "alerts", _signal(), "otra-cosa") is None
    glob = _rule(subject_pattern="*indexing*",
                 condition_json=json.dumps({"kind": "always"}))
    assert sc.matches(glob, "alerts", _signal(), "cualquier-clave") is not None
    # A rule for another table never applies, whatever the pattern says.
    assert sc.matches(glob, "recommendations", _signal(), "x") is None


def test_defer_effect_is_bounded() -> None:
    from app import signal_conditions as sc
    rule = _rule(condition_json=json.dumps(
        {"kind": "always", "effect": "defer", "defer_minutes": 99999999}))
    verdict = sc.matches(rule, "alerts", _signal())
    assert verdict["effect"] == sc.EFFECT_DEFER
    assert verdict["defer_minutes"] <= 60 * 24 * 7, (
        "un aplazamiento sin techo equivale a un silencio permanente por la puerta de atrás")


def test_preview_reads_only_and_reports_effect() -> None:
    """The "Probar": over the last N REAL alerts, how many would it hit — and not
    a single write, because an operator must be able to try a rule freely."""
    from app import signal_conditions as sc
    from app.app_database import app_session
    from app.app_models.business import Alert
    from app.app_models.signal_suppressions import SignalSuppression
    from sqlalchemy import func, select

    async def go():
        async with app_session() as s:
            for i in range(6):
                s.add(Alert(
                    idempotency_key=f"prev-{i}",
                    title=("Barrido de indexing lento" if i % 2 == 0
                           else "Fallo de cobro"),
                    severity="critical" if i == 4 else "medium",
                    target_entity_kind="job",
                    target_entity_id="indexing" if i % 2 == 0 else "billing",
                    created_at=datetime.utcnow() - timedelta(minutes=i),
                ))
            await s.commit()
        out = await sc.preview({
            "signal_table": "alerts",
            "subject_pattern": "indexing",
            "condition": {"kind": "always"},
            "severity_max": "media",
        }, last_n=50)
        async with app_session() as s:
            rules = (await s.execute(
                select(func.count(SignalSuppression.id)))).scalar()
        return out, rules

    out, rules = _run(go())
    assert out["ok"] is True
    assert out["checked"] == 6
    # 3 alerts mention indexing (i = 0, 2, 4), but i == 4 is critical and the cap
    # is "media" — so the preview reports 2, and shows the operator that the
    # critical one would still reach them.
    assert out["would_affect"] == 2
    assert rules == 0, "el dry-run no puede crear la regla que está probando"


def test_preview_rejects_unsupported_table() -> None:
    from app import signal_conditions as sc
    out = _run(sc.preview({"signal_table": "app_settings"}))
    assert out["ok"] is False


@pytest.mark.parametrize("value,rank_order", [
    ("info", 1), ("media", 2), ("alta", 3), ("crítica", 4), ("desconocida", 2),
])
def test_severity_ladder_is_language_agnostic(value, rank_order) -> None:
    """Signals arrive labelled in Spanish or English depending on the surface; a
    cap that only understood one of them would leak criticals."""
    from app.signal_conditions import severity_rank
    assert severity_rank(value) == rank_order
