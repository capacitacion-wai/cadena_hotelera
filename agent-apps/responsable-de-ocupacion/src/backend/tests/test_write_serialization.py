"""The autonomy brake must WAIT ITS TURN, not compete for the lock.

Reproduces the Vigía incident (INC-VIG-59): `PATCH /api/agent/settings` with
`triggers_active=false` returned **500** with `sqlite3.OperationalError: database is locked`.
SQLite allows ONE writer; the skill has a process lock (`app_write`) that puts writers
in a **fair queue (FIFO)** above the pool, but 7.139.4 only wired it into the
executor. Everything else —including the `INSERT ... ON CONFLICT` on `app_settings`— went straight
to SQLite and depended on winning the `busy_timeout` lottery against whoever held the lock. Under an
autonomous round (8 workers claiming + poller) that lottery is lost: the operator could NOT
switch off their employee exactly when it was most needed.

The test is deterministic in BOTH directions because it does not simulate "load" (that would be
fragile): it keeps the write lock busy LONGER than the `busy_timeout` and checks what happens to
the brake. Without serialization, the brake exhausts the timeout and blows up; serialized, it queues
and gets in as soon as the other releases. To take milliseconds instead of 15 s, the test lowers the
`busy_timeout` of new connections — what matters is not its value, but whether the writer
competes or queues.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio

# A busy turn longer than the timeout: whoever COMPETES is left out, whoever
# QUEUES gets in. The difference between the two is exactly what is being tested.
BUSY_TIMEOUT_MS = 150
HOLD_SECONDS = 1.0


async def _shorten_busy_timeout() -> None:
    """Lowers `busy_timeout` on NEW connections and drops the pooled ones.

    The `app_database` listener sets 15000 ms on each connection; here another
    listener is added AFTER it (they run in order), so the last value wins. Without
    `dispose()` the already-open connections would keep the 15 s and the test would take
    that long to fail.
    """
    from sqlalchemy import event
    from app.app_database import app_engine

    @event.listens_for(app_engine.sync_engine, "connect")
    def _shorten(dbapi_conn, _rec):  # pragma: no cover — test infra
        cur = dbapi_conn.cursor()
        try:
            cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        finally:
            cur.close()

    await app_engine.dispose()


async def _occupy_write_turn(started: asyncio.Event) -> None:
    """Occupies the write turn the way the executor does: through `app_write`.

    It REALLY writes before sleeping: without a prior write SQLite would not have
    taken the lock and there would be nothing to contend for.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from app.app_database import app_write
    from app.app_models.runtime import AppSetting

    async with app_write() as s:
        await s.execute(sqlite_insert(AppSetting).values(
            key="ocupado", value="1",
        ).on_conflict_do_nothing(index_elements=[AppSetting.key]))
        await s.flush()
        started.set()
        await asyncio.sleep(HOLD_SECONDS)
        await s.commit()


async def test_el_freno_espera_su_turno_y_no_muere_por_database_is_locked() -> None:
    """Switching off autonomy works even when the write turn is busy."""
    await _shorten_busy_timeout()

    from app.routes.manifest_routes import _upsert_setting
    from app.triggers_state import SETTING_KEY, triggers_active

    started = asyncio.Event()
    occupier = asyncio.create_task(_occupy_write_turn(started))
    await started.wait()  # the turn is already taken: the brake arrives mid-contention

    try:
        # Without serialization this raises OperationalError('database is locked').
        await _upsert_setting(SETTING_KEY, "0", SimpleNamespace(email="jefe@wai.dev"))
    finally:
        await occupier

    assert await triggers_active() is False, (
        "el freno respondió pero la autonomía sigue encendida"
    )


async def test_una_lectura_no_ocupa_el_turno_de_escritura() -> None:
    """Reading must NOT consume the write lock.

    The executor polls every 2 s with 8 workers; if its READS (the spend of the
    last 24 h) take the lock, the queue fills with callers that do not write and the
    real writers —the brake among them— wait for nothing. `app_write` is
    only for writing; `app_session` for reading.
    """
    from app.app_database import app_session, write_lock
    from app.autonomous.executor import _budget_consumed_24h

    async with app_session() as s:
        spent = await _budget_consumed_24h(s)
    assert spent == 0.0  # no runs, no spend

    # With the lock TAKEN, an executor read must still go through: if it
    # blocks here, the read was requesting it.
    async with write_lock():
        spent = await asyncio.wait_for(_read_budget(), timeout=5.0)
    assert spent == 0.0


async def _read_budget() -> float:
    from app.app_database import app_session
    from app.autonomous.executor import _budget_consumed_24h

    async with app_session() as s:
        return await _budget_consumed_24h(s)
