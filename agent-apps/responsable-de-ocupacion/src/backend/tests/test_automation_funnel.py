"""The funnel of a standing order has to COUNT, not say «yes».

Reported on the Vigía: the Governance panel showed «124 checks · 1 passed the only-if ·
1 firing», while the evaluations tab —looking at the SAME table— said 4 firings. The cause
is neither a filter nor a range: `met`, `fired` and `silenced` are **Boolean** columns, and they
were summed with `func.sum(func.coalesce(col, 0))`. SQLAlchemy applies the column type to the
aggregate's result, so `SUM` returns `True` and passing it through `int()` gives **1**. Any number
greater than zero is flattened to one, and the funnel turns into a yes/no that looks like a figure —
the worst kind of bug in a governance tool, because it seems to work.

And it was not only the view: the SAME pattern was in `rollup_eval_log`, which freezes the totals
before the prune deletes the detail. A day with 12 firings was recorded as 1 **forever**: the view
is fixed by re-reading, the summary is not.

It is tested with numbers that tell the two hypotheses apart: 7 met / 4 fired / 2 silenced. If
someone sums booleans again, all of them would be 1 and the test fails on all three sides at once.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

# With these numbers, «flattened to boolean» and «correctly counted» look nothing alike.
CHECKS, MET, FIRED, SILENCED, ERRORS = 10, 7, 4, 2, 1


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed(aid: int = 1, *, at: datetime | None = None) -> None:
    """A batch of evaluations with the real shape of a watch.

    The rows are distributed as in production: the ones that fire also met the condition, and the
    silenced ones met it but the anti-repetition stopped them. So `met > fired + silenced` and a
    remainder is left —«it met the condition but there was nothing new»— which is exactly the gap
    the operator did not understand.
    """
    from app.app_database import app_write
    from app.app_models.automations import Automation, AutomationEvalLog

    at = at or (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1))
    async with app_write() as s:
        s.add(Automation(id=aid, title="Salud de tenants", enabled=True,
                         signal_type="scheduled", signal_spec="{}"))
        await s.flush()
        for i in range(CHECKS):
            met = i < MET
            s.add(AutomationEvalLog(
                automation_id=aid, at=at + timedelta(minutes=i), mode="watch",
                met=met, fired=(i < FIRED), silenced=(MET - SILENCED) <= i < MET,
                error=("timeout del check" if i == CHECKS - 1 else None),
            ))
        await s.commit()


def test_el_embudo_cuenta_de_verdad_y_no_se_aplasta_a_uno() -> None:
    """checks/met/fired/silenced are COUNTS; none of them may get stuck at 1."""
    from app import automation_stats

    async def go():
        await _seed()
        return (await automation_stats.funnel([1], None, None))[1]

    f = _run(go())
    assert f["checks"] == CHECKS
    assert f["met"] == MET, (
        f"«pasaron el solo si» dice {f['met']} y son {MET}: se está sumando una columna "
        f"Boolean, así que cualquier cifra >0 se aplasta a 1"
    )
    assert f["fired"] == FIRED, f"«disparos» dice {f['fired']} y son {FIRED}"
    assert f["silenced"] == SILENCED, f"«frenados» dice {f['silenced']} y son {SILENCED}"
    assert f["errors"] == ERRORS


def test_el_embudo_explica_el_hueco_entre_cumplir_y_avisar() -> None:
    """The remainder of met − fired − silenced has a NAME, it is not a hole.

    With the real Vigía, 116 checks met the condition and did not notify: that is not a failure,
    it is the anti-repetition saying «I already told you this». Without counting it, the operator
    sees «121 passed, 5 firings» and concludes that their employee is keeping things quiet.
    """
    from app import automation_stats

    async def go():
        await _seed()
        return (await automation_stats.funnel([1], None, None))[1]

    f = _run(go())
    assert f["held"] == MET - FIRED - SILENCED
    assert f["checks"] >= f["met"] >= f["fired"] + f["silenced"] + f["held"]


def test_el_resumen_diario_congela_cifras_reales_no_booleanos() -> None:
    """What the rollup records is the only thing that survives the prune: it cannot be 0/1.

    The rows are seeded YESTERDAY because the rollup only summarises COMPLETE days (today's is
    read live from the detail).
    """
    from app import automations_service as svc
    from app.app_database import app_session
    from app.app_models.automation_daily_stats import AutomationDailyStats
    from app.timeutil import business_tz
    from sqlalchemy import select

    async def go():
        tz = business_tz()
        ayer = (datetime.now(tz) - timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0)
        await _seed(at=ayer.astimezone(timezone.utc).replace(tzinfo=None))
        await svc.rollup_eval_log(days_back=2)
        async with app_session() as s:
            return (await s.execute(select(AutomationDailyStats))).scalars().all()

    rows = _run(go())
    assert rows, "el rollup no grabó nada: sin él, los totales se encogen al podar el detalle"
    got = {"checks": sum(r.checks for r in rows), "met": sum(r.met for r in rows),
           "fired": sum(r.fired for r in rows), "silenced": sum(r.silenced for r in rows)}
    assert got == {"checks": CHECKS, "met": MET, "fired": FIRED, "silenced": SILENCED}, (
        f"el resumen diario grabó {got}: si los valores son 0/1 la historia queda CORRUPTA de "
        f"forma irreversible (el detalle se poda a los 14 días)"
    )


def test_el_resumen_diario_es_idempotente() -> None:
    """Running it twice does not duplicate: it is an upsert per day, not an append."""
    from app import automations_service as svc
    from app.app_database import app_session
    from app.app_models.automation_daily_stats import AutomationDailyStats
    from app.timeutil import business_tz
    from sqlalchemy import func, select

    async def go():
        tz = business_tz()
        ayer = (datetime.now(tz) - timedelta(days=1)).replace(hour=12, minute=0, second=0)
        await _seed(at=ayer.astimezone(timezone.utc).replace(tzinfo=None))
        await svc.rollup_eval_log(days_back=2)
        await svc.rollup_eval_log(days_back=2)
        async with app_session() as s:
            return (await s.execute(select(
                func.count(AutomationDailyStats.automation_id),
                func.sum(AutomationDailyStats.fired)))).one()

    n_rows, fired = _run(go())
    assert n_rows == 1 and int(fired) == FIRED


def test_lo_generado_habla_el_idioma_del_empleado(monkeypatch) -> None:
    """The figures are labelled with the name THIS employee gives the entity.

    The Vigía calls its alerts «Incidencias detectadas» (slug `incidencias`); the panel said
    «Avisos» and linked to `/domain/alertas`, a slug that does not exist in that app. Result: four
    figures with foreign vocabulary and four dead links. The label and the route can only come
    from the manifest.
    """
    from app import automation_provenance as prov

    monkeypatch.setattr(prov, "APP_MANIFEST", None, raising=False)
    manifest = {"agent_app": {"domain_entities": [
        {"slug": "incidencias", "label": "Incidencias detectadas", "table": "alerts",
         "source": "local"},
        {"slug": "planes-mitigacion", "label": "Planes de mitigación",
         "table": "recommendations", "source": "local"},
    ]}}
    monkeypatch.setattr("app.config.APP_MANIFEST", manifest, raising=False)

    alerts = prov.surface_of("alerts")
    assert alerts["label"] == "Incidencias detectadas"
    assert alerts["path"] == "/domain/incidencias"
    # An entity the app does NOT expose does not get an invented route: figure without a link.
    assert prov.surface_of("decisions")["path"] is None
    # Tasks and approvals have a fixed page in the SPA.
    assert prov.surface_of("scheduled_items")["path"] == "/encargos"


def test_el_enlace_de_lo_generado_filtra_por_su_automatizacion() -> None:
    """`?created_by_automation=N` narrows to what THAT order left, and `only_open=1` to what is live.

    Without this, «left 4 incidents, 2 open» linked to the full list, where those four are
    indistinguishable: linking to everything is the same as not linking. The important guard is the
    one below: if the provenance columns did not exist, the filter must NOT return the whole table.
    """
    from app.routes.domain_routes import _provenance_filter

    class _Req:
        def __init__(self, **qp): self.query_params = qp

    entity = {"table": "alerts", "source": "local", "primary_key": "id",
              "list_columns": [{"column": "title"}]}
    where: list[str] = []
    params: dict = {}
    _provenance_filter(entity, _Req(created_by_automation="7", only_open="1"), where, params)
    sql = " AND ".join(where)
    assert "created_by_run_id" in sql and "source_event_id" in sql, (
        "faltan los dos caminos: el agente escribió la fila durante su reacción, o una reacción "
        "determinista la escribió sin run"
    )
    assert params["__prov_src"] == "automation:7"
    assert '"status" IN' in sql
    # CONSTANT-size SQL: subqueries, not an IN (…) with one id per firing.
    assert "SELECT id FROM events" in sql and ":__prov_src" in sql

    # Without the parameter nothing is touched (the normal list stays the normal list).
    where2: list[str] = []
    _provenance_filter(entity, _Req(), where2, {})
    assert where2 == []


async def _sembrar_alerta_de_automatizacion(aid: int = 3) -> int:
    """An alert written by a DETERMINISTIC reaction (no run): it is the path most
    often forgotten and the one that leaves `created_by_run_id` null."""
    from app.app_database import app_write
    from app.app_models.automations import Automation
    from app.app_models.runtime import Event

    async with app_write() as s:
        s.add(Automation(id=aid, title="Salud de tenants", enabled=True,
                         signal_type="scheduled", signal_spec="{}"))
        ev = Event(type="automation_fired", source=f"automation:{aid}",
                   status="processed", fingerprint=f"automation:{aid}:test")
        s.add(ev)
        await s.flush()
        from app.app_models.business import Alert
        alerta = Alert(title="Indexación parada en plexamx", status="open",
                       idempotency_key=f"test-alert-{aid}", source_event_id=ev.id)
        s.add(alerta)
        await s.flush()
        alert_id = int(alerta.id)
        await s.commit()
    return alert_id


def test_una_fila_dice_quien_la_creo_y_el_filtro_solo_ofrece_lo_que_existe() -> None:
    """Without the per-ROW stamp, a list of alerts cannot tell autonomous work from
    human work: you had to open them one by one. And the dropdown can only offer
    automations that have written HERE — an option that returns an empty list
    makes people doubt the whole filter."""
    from app import automation_provenance as prov

    async def go():
        alert_id = await _sembrar_alerta_de_automatizacion(3)
        origen = await prov.origin_of("alerts", [alert_id])
        productores = await prov.producers_of("alerts")
        return alert_id, origen, productores

    alert_id, origen, productores = _run(go())
    sello = origen[str(alert_id)]
    assert sello["automation_id"] == 3
    assert sello["title"] == "Salud de tenants", "el nombre, no el número"
    assert sello["via"] == "event", (
        "una reacción determinista escribe sin run: si solo se mirara "
        "`created_by_run_id`, esta fila parecería obra de una persona")
    assert productores == [{"id": 3, "title": "Salud de tenants", "count": 1}]
    # An entity nobody has produced offers no filter (and does not fail).
    assert _run(prov.producers_of("decisions")) == []


def test_ninguna_agregacion_suma_una_columna_booleana() -> None:
    """Static guard: the pattern that caused the bug cannot reappear.

    It is static because the bug does NOT show in a single query —it returns a plausible number—
    and is only noticed when someone compares two screens. Here the shape of the code is forbidden:
    summing a Boolean column. The correct alternative (`case(...)` or `cast(..., Integer)`) is
    explicit about rows being what is counted.
    """
    import re
    from pathlib import Path

    app = Path(__file__).resolve().parents[1] / "app"
    # Only the eval-log Booleans: the namesakes in `automation_daily_stats` are Integer
    # (already counts) and summing them is correct. Approved ways of counting rows:
    # the shared helper (`rows_where`/`_rows`) or an explicit CASE/CAST.
    bad = re.compile(r"func\.sum\((?![^)]*(?:case\(|cast\(|_rows\(|rows_where\())"
                     r"[^)]*AutomationEvalLog\.(?:met|fired|silenced)\b")
    offenders: list[str] = []
    for path in (app / "automation_stats.py", app / "automations_service.py"):
        for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if bad.search(line):
                offenders.append(f"{path.name}:{idx}: {line.strip()}")
    assert not offenders, (
        "Estas líneas suman una columna Boolean: SQLAlchemy devuelve True/False y la cifra se "
        "aplasta a 1. Usa case((col.is_(True), 1), else_=0) o cast(col, Integer):\n  "
        + "\n  ".join(offenders)
    )


# ── The list asks only for the latest, not the whole history (7.158.0) ─────
def test_la_lista_devuelve_la_ULTIMA_ejecucion_de_cada_automatizacion():
    """The SQL was changed so as not to load the full history: before, ALL executions of
    all automations were fetched and the most recent was picked in Python (cheap with
    120 rows, unsustainable with a year's worth). This test pins what the card must
    show, which is what that change could break silently."""
    from datetime import datetime, timedelta, timezone
    from app.app_database import app_write
    from app.app_models.automations import Automation
    from app.app_models.runtime import AgentRun, Event

    async def _seed():
        ahora = datetime.now(timezone.utc).replace(tzinfo=None)
        async with app_write() as s:
            s.add(Automation(id=7, title="Vigilar A", enabled=True,
                             signal_type="scheduled", signal_spec="{}"))
            s.add(Automation(id=8, title="Vigilar B", enabled=True,
                             signal_type="scheduled", signal_spec="{}"))
            await s.flush()
            # Three executions of #7 (the last one is FAILED) and one of #8.
            for i, (aid, estado) in enumerate(
                    [(7, "COMPLETED"), (7, "COMPLETED"), (8, "COMPLETED"), (7, "FAILED")]):
                s.add(Event(id=100 + i, type="automation_fired",
                            source=f"automation:{aid}", fingerprint=f"fp-{i}",
                            status="COMPLETED", detected_at=ahora))
                s.add(AgentRun(id=200 + i, event_id=100 + i, mode="autonomous",
                               idempotency_key=f"run-{i}", prompt="x", status=estado,
                               started_at=ahora + timedelta(minutes=i)))
            await s.commit()
    _run(_seed())

    from app import automations_service as svc
    filas = {a["id"]: a for a in _run(svc.list_automations())}
    # #7 must show its MOST RECENT execution (the failed one), not the first.
    assert filas[7]["last_run"]["status"] == "FAILED"
    assert filas[8]["last_run"]["status"] == "COMPLETED"
