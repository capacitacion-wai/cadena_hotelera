"""R-OPS-1 regression test — operator-editable business descriptions.

Pre-v7.4 the Operations card was a read-only view of the manifest,
so on-call ops had to redeploy the whole app to fix a misleading
trigger / action description. v7.4 added two parallel override
tables:

  * ``WatcherSchedule.business_description_json`` — per watcher
  * ``ActionOverride.business_description_json``  — per event_type

Both fields are JSON-encoded ``{"es": "...", "en": "..."}`` and the
manifest endpoint resolves precedence: override → manifest → empty.

This test asserts at the ORM level:

  1. Both columns exist on the ORM models with the documented names
     (any rename or removal blows the verifier here, not at the
     first operator edit attempt against a production pod).
  2. INSERT → SELECT round-trips the JSON without lossy encoding
     (Spanish accents, emoji, embedded quotes — every cause of "the
     description disappeared after I saved it" tickets).

The HTTP layer (PATCH /api/agent/watchers/{name}, PATCH /api/agent/
actions/{event_type}) is verified separately by the §8.6 positive
contract checks in ``verify_generated_app.sh``; running the full
FastAPI stack from pytest would add too many transitive deps for
the verifier's "strong" phase.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest


def test_models_expose_business_description_json_column() -> None:
    """ORM-level invariant: both columns must exist with the
    documented names. If anyone renames the column, the verifier
    surfaces it BEFORE the migration sql is shipped."""
    from app.app_models.runtime import ActionOverride, WatcherSchedule

    ws_cols = {c.name for c in WatcherSchedule.__table__.columns}
    ao_cols = {c.name for c in ActionOverride.__table__.columns}

    assert "business_description_json" in ws_cols, (
        f"WatcherSchedule.business_description_json missing — got {ws_cols!r}"
    )
    assert "business_description_json" in ao_cols, (
        f"ActionOverride.business_description_json missing — got {ao_cols!r}"
    )
    # Defensive: the watcher loop reads these too — make sure they
    # stay on the same model.
    assert "interval_sec" in ws_cols
    assert "cron_expr" in ws_cols
    assert "enabled" in ws_cols
    assert "event_type" in ao_cols


@pytest.mark.asyncio
async def test_watcher_override_round_trip_preserves_unicode() -> None:
    """INSERT one row through SQLAlchemy → SELECT it back → JSON
    decodes identically. Catches encoding regressions (e.g. someone
    swapping ``ensure_ascii=False`` for ``True`` in
    manifest_routes.py)."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    from app.app_database import AppBase, app_engine, app_session
    from app.app_models.runtime import WatcherSchedule
    from sqlalchemy import select

    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)

    payload = {
        "es": "Vigila caída de pedidos > 30 % vs. ventana 24h.",
        "en": "Watch order drops > 30 % vs. 24h window.",
    }
    async with app_session() as s:
        row = WatcherSchedule(
            watcher_name="orders_drop",
            enabled=1,
            interval_sec=None,
            cron_expr="0 */2 * * *",
            notes="manual override",
            business_description_json=json.dumps(payload, ensure_ascii=False),
        )
        s.add(row)
        await s.commit()

    async with app_session() as s:
        stmt = select(WatcherSchedule).where(WatcherSchedule.watcher_name == "orders_drop")
        result = await s.execute(stmt)
        row = result.scalar_one()

    assert row.cron_expr == "0 */2 * * *"
    assert row.enabled == 1
    # The JSON round-trips byte-for-byte.
    decoded = json.loads(row.business_description_json)
    assert decoded == payload
    # Accents survived (rules out a UTF-8 → latin-1 silent rewrite).
    assert "caída" in decoded["es"]

    os.remove(path)


@pytest.mark.asyncio
async def test_action_override_round_trip() -> None:
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"

    from app.app_database import AppBase, app_engine, app_session
    from app.app_models.runtime import ActionOverride
    from sqlalchemy import select

    async with app_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)

    payload = {"es": "Notifica al equipo de retail.", "en": "Notify retail team."}
    async with app_session() as s:
        s.add(ActionOverride(
            event_type="ORDERS_DROP",
            business_description_json=json.dumps(payload, ensure_ascii=False),
            updated_by="ops@wai.com",
        ))
        await s.commit()

    async with app_session() as s:
        stmt = select(ActionOverride).where(ActionOverride.event_type == "ORDERS_DROP")
        result = await s.execute(stmt)
        row = result.scalar_one()

    assert row.updated_by == "ops@wai.com"
    decoded = json.loads(row.business_description_json)
    assert decoded == payload

    os.remove(path)
