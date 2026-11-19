"""v7.140.0 — the SCHEMA gate: boot must survive both a fresh PVC and an old one.

There is no migration runner here: ``create_all`` builds a fresh DB and
``_ensure_columns`` / ``_ensure_indexes`` backfill an existing PVC at boot. Two
failure modes are therefore invisible until an app boots for real, and both
shipped as broken installs before:

1. **A duplicate index NAME.** In SQLite index names are global, so declaring the
   same name column-level (``index=True``) and in ``__table_args__`` makes
   ``create_all`` fail — the app never starts, on a FRESH install only.
2. **Backfill drifting from the model.** Same name, different columns depending on
   whether the DB was created before or after the release.

Plus the cheap-but-vital one: running the whole boot backfill TWICE must be a
no-op, because it runs on every single pod start.
"""
from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_no_duplicate_index_names_in_metadata() -> None:
    from collections import Counter

    import app.app_models  # noqa: F401 — registers every model
    from app.app_database import AppBase

    names = Counter(
        ix.name for t in AppBase.metadata.tables.values() for ix in t.indexes
    )
    dupes = sorted(n for n, k in names.items() if k > 1)
    assert not dupes, (
        f"nombres de índice repetidos {dupes}: SQLite los tiene en un espacio "
        "global, así que create_all falla y la app NO arranca en una instalación "
        "nueva (no en un PVC existente, que es lo que lo hace invisible)")


def test_boot_backfill_is_idempotent_and_matches_the_models() -> None:
    """Run the boot path twice on a real SQLite and compare with the models."""
    from sqlalchemy import text

    import app.app_models  # noqa: F401
    from app.app_database import AppBase, app_engine
    from app.main import _ensure_columns, _ensure_indexes

    async def go():
        async with app_engine.begin() as conn:
            await conn.run_sync(AppBase.metadata.drop_all)
            await conn.run_sync(AppBase.metadata.create_all)
            # Twice on purpose: this runs on EVERY pod start.
            for _ in range(2):
                await _ensure_columns(conn)
                await _ensure_indexes(conn)
            rows = (await conn.execute(text(
                "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index' "
                "AND name NOT LIKE 'sqlite_%'"
            ))).all()
        await app_engine.dispose()
        return {r[0]: r[1] for r in rows}

    live = _run(go())

    # Every index the models declare must exist, with the table they declare.
    for table in AppBase.metadata.tables.values():
        for ix in table.indexes:
            assert ix.name in live, f"falta el índice {ix.name}"
            assert live[ix.name] == table.name

    # And the v7.140.0 access paths that make the feature affordable.
    for name in (
        "ix_agent_memory_live",              # the injected block, every run
        "ix_learning_effects_target",        # the reverse "learned from" lookup
        "ix_signal_silence_log_rule",        # "why didn't it warn me?"
        "ix_signal_suppressions_pattern",    # the conditional gate
        "ix_alerts_run_status",              # provenance + open counts
        "ix_events_source_detected",         # the automation funnel by range
        "ix_scheduled_items_source_event",   # deterministic-fire chain
    ):
        assert name in live, f"falta el índice de rendimiento {name}"


def test_new_tables_exist_after_create_all() -> None:
    from sqlalchemy import text

    import app.app_models  # noqa: F401
    from app.app_database import AppBase, app_engine

    async def go():
        async with app_engine.begin() as conn:
            await conn.run_sync(AppBase.metadata.drop_all)
            await conn.run_sync(AppBase.metadata.create_all)
            rows = (await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ))).all()
        await app_engine.dispose()
        return {r[0] for r in rows}

    tables = _run(go())
    for t in ("agent_memory", "agent_episodes", "learning_effects",
              "signal_silence_log", "automation_daily_stats"):
        assert t in tables, f"la tabla {t} no se crea en una instalación nueva"


@pytest.mark.parametrize("table,column", [
    ("signal_suppressions", "review_state"),
    ("signal_suppressions", "subject_pattern"),
    ("signal_suppressions", "condition_json"),
    ("signal_suppressions", "severity_max"),
    ("signal_suppressions", "expires_at"),
    ("scheduled_items", "source_event_id"),
])
def test_additive_columns_are_declared_in_the_models(table, column) -> None:
    """The boot backfill adds these to an OLD PVC; the model must declare them too
    or a fresh install would lack the column the code writes to."""
    import app.app_models  # noqa: F401
    from app.app_database import AppBase
    assert column in AppBase.metadata.tables[table].c, (
        f"{table}.{column} se añade en el arranque pero no está en el modelo")
